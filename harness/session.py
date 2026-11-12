"""pi `--session-id` 外置保真历史（76 §4.6，task-def §2.3）。

一条命令模板（照 loop/bonsai-revival/如何用pi复活Agent.md §2 抄，不改）：
    pi -p --no-tools --mode text --thinking <low|off> --session-id <sid> \
       --model "bonsai/~/mini/models/sage-a1.5-4bit-mlx" "<本轮输入>"

三条硬口径：
  ① 配置面必须在树内的隔离目录（TREE_PI_AGENT_DIR，缺省 <树根>/runs/pi-agent），
     **绝不落** ~/.pi/agent（那是 llaat 生产目录）。
  ② 保真：read_session 保留每条的**原文**（raw = 该行逐字节原文），并区分 user / assistant / tool（+ system）。
  ③ 历史不许删：history_tokens + 本轮估算 > ctx_budget ⇒ raise ContextOverflow（响亮），
     绝不静默截断、绝不改用摘要账本（76 §4.6 硬约束① + D4）。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

from harness import role as role_mod, submit

# 模型串（owner 实测：8092 /v1/models 单条（Sage-A1.5；8091 bonsai 已下线，见 t-d301b69799d2 续跑））—— 逐字，不许改
MODEL_ID = os.path.expanduser("~/mini/models/sage-a1.5-4bit-mlx")
MODEL_ARG = "bonsai/" + MODEL_ID

# V2.1 收尾 (A)：LLAAT 适配器。pi 的 baseUrl 默认指到本地适配器（127.0.0.1:8904/v1），
# 适配器把 OpenAI-completions 调用翻译成 LLAAT kind="tree" 入队请求。
# TREE_USE_LLAAT=0 ⇒ 逃生口：baseUrl 回直连 8092（应急/调试用，缺省不用）。
ADAPTER_DEFAULT_URL = "http://127.0.0.1:8904/v1"
DIRECT_BASE_URL = "http://127.0.0.1:8092/v1"
_ADAPTER_PROC = None  # ensure_adapter 起的适配器子进程句柄（测试可据此清理）


def use_llaat() -> bool:
    """是否走 LLAAT（缺省走；TREE_USE_LLAAT=0 显式关）。"""
    return str(os.environ.get("TREE_USE_LLAAT") or "").strip() != "0"


def adapter_url() -> str:
    """给 pi 的 baseUrl（适配器地址）。"""
    return str(os.environ.get("TREE_LLAAT_ADAPTER_URL") or ADAPTER_DEFAULT_URL).rstrip("/")


def llaat_url() -> str:
    """LLAAT 队列地址。"""
    return str(os.environ.get("TREE_LLAAT_URL") or "http://127.0.0.1:8903").rstrip("/")


def _local_llaat_adapter() -> bool:
    return (use_llaat()
            and urlsplit(adapter_url()).hostname in {"127.0.0.1", "localhost", "::1"})


def ensure_adapter(agent: str = None, run_dir: str = None):
    """确保 LLAAT 适配器在跑，返回它给 pi 用的 baseUrl。
    已在跑 ⇒ 直接复用；没跑 ⇒ 起一个（无状态独立进程）；起不来 ⇒ 响亮失败，绝不静默回直连 8092。
    TREE_USE_LLAAT=0 时返回 None（逃生口，不起适配器）。"""
    global _ADAPTER_PROC
    if not use_llaat():
        return None
    url = adapter_url()
    health = url.rstrip("/") + "/health"
    for _attempt in range(3):
        try:
            with urllib.request.urlopen(health, timeout=2.0) as resp:
                if resp.status == 200:
                    return url
        except Exception:
            pass
        # 没在跑：起一个（日志落 agent 目录；端口冲突时它自己退出，下一轮重试会复用已跑的那个）。
        try:
            adapter_path = str(Path(role_mod.ROOT) / "lib" / "llaat_adapter.py")
            log_dir = Path(agent or agent_dir())
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "llaat-adapter.log"
            env = dict(os.environ)
            env["TREE_LLAAT_URL"] = llaat_url()
            try:
                port = str(int(url.rsplit(":", 1)[-1].split("/")[0]))
            except (ValueError, IndexError):
                port = "8904"
            env["TREE_LLAAT_ADAPTER_PORT"] = port
            with open(str(log_path), "ab") as _lf:
                _ADAPTER_PROC = subprocess.Popen(
                    [sys.executable, adapter_path],
                    stdout=_lf, stderr=_lf, start_new_session=True, env=env)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("LLAAT 适配器起不来（%s），不静默回直连 8092" % exc)
        deadline = time.time() + 15.0
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(health, timeout=2.0) as resp:
                    if resp.status == 200:
                        return url
            except Exception:
                pass
            time.sleep(0.5)
    raise RuntimeError("LLAAT 适配器 15s 内未就绪（%s），不静默回直连 8092" % health)

# 旋钮 → pi 的 thinking 值（task-def §2.3.2：none → off；low/medium → low）
THINKING_MAP = {"none": "off", "low": "low", "medium": "low"}

SINGLE_CALL_TIMEOUT = float(os.environ.get("TREE_PI_TIMEOUT") or 300.0)

# F3 硬上限：研究叶单叶 300 秒（不得被 TREE_PI_TIMEOUT=900 等环境变量覆盖；
# 实测 e2e-20261105-f1f7：c2.1 跑了 378.6s，因为 TimedRunner 没透传 timeout，
# run_act 退回了 TREE_PI_TIMEOUT=900）。
LEAF_TIMEOUT_S = 300.0

DEFAULT_SETTINGS = {"compaction": {"enabled": False}}     # 76 §4.6：关自动压缩（模型写摘要会压掉逐字事实）


class ContextOverflow(RuntimeError):
    """历史 + 本轮 > ctx_budget ⇒ 响亮失败（绝不静默截断历史）。"""


def session_id(req_id: str, node_id: str) -> str:
    """一节点一 id、跨轮不变：tree-<req_id>-<node_id>。"""
    return "tree-%s-%s" % (req_id, node_id)


# ---------------------------------------------------------------- 隔离配置面
def agent_dir() -> str:
    root = os.environ.get("TREE_PI_AGENT_DIR")
    if not root:
        root = str(Path(role_mod.ROOT) / "runs" / "pi-agent")
    return root


def session_dir() -> str:
    return os.environ.get("TREE_PI_AGENT_SESSION_DIR") or str(Path(agent_dir()) / "sessions")


def models_json() -> str:
    """provider 名 bonsai、模型 id 逐字 MODEL_ID、thinkingLevelMap.off = "none"。

    形状照 loop/bonsai-revival/pi-config/models.json 抄。
    """
    return json.dumps({"providers": {"bonsai": {
        "baseUrl": (adapter_url() if use_llaat() else DIRECT_BASE_URL),
        "api": "openai-completions",
        "apiKey": "local",
        "models": [{
            "id": MODEL_ID, "name": "bonsai2-27b", "contextWindow": 114688, "maxTokens": 8192,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "compat": {"maxTokensField": "max_tokens", "supportsReasoningEffort": True,
                       "supportsStore": False, "supportsDeveloperRole": False,
                       "thinkingFormat": "qwen-chat-template"},
            "reasoning": True,
            "thinkingLevelMap": {"off": "none"},
        }],
    }}}, ensure_ascii=False, indent=2)


def ensure_agent_dir(agent: str = None, sess: str = None) -> tuple:
    """建隔离目录 + 写 models.json / settings.json。@returns (agent_dir, session_dir)。"""
    a = Path(agent or agent_dir())
    s = Path(sess or session_dir())
    a.mkdir(parents=True, exist_ok=True)
    s.mkdir(parents=True, exist_ok=True)
    mj = a / "models.json"
    if not mj.exists() or mj.read_text(encoding="utf-8") != models_json():
        mj.write_text(models_json(), encoding="utf-8")
    sj = a / "settings.json"
    if not sj.exists() or json.loads(sj.read_text(encoding="utf-8") or "{}") != DEFAULT_SETTINGS:
        sj.write_text(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return str(a), str(s)


def pi_env(agent: str = None, sess: str = None) -> dict:
    """pi 的环境：PATH 里加 $HOME/node/bin；配置面指到树内隔离目录。"""
    a, s = ensure_agent_dir(agent, sess)
    env = dict(os.environ)
    home = env.get("HOME") or str(Path.home())
    env["PATH"] = str(Path(home) / "node" / "bin") + os.pathsep + env.get("PATH", "")
    env["PI_CODING_AGENT_DIR"] = a
    env["PI_CODING_AGENT_SESSION_DIR"] = s
    return env


# ---------------------------------------------------------------- 串行（并发=1）
class SerialLock:
    """极简文件锁：bonsai 并发=1 ⇒ 连跑的轮次之间不许有并发 pi。

    拿不到锁就**等**（不并发起进程）。锁的年龄超过 stale_s 视为残留（进程被杀留下的），
    偷锁并记一行（响亮，不静默）。
    """

    def __init__(self, path: str, poll_s: float = 0.5, stale_s: float = 1800.0,
                 max_wait_s: float = 1800.0):
        self.path = str(path)
        self.poll_s, self.stale_s, self.max_wait_s = poll_s, stale_s, max_wait_s
        self.stole = False
        self.waited_s = 0.0

    def __enter__(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, ("%d\n" % os.getpid()).encode())
                os.close(fd)
                self.waited_s = time.time() - t0
                return self
            except FileExistsError:
                # V2.2 自愈：锁文件里写着 PID；PID 已死 ⇒ 孤儿锁，立刻偷锁，
                # 不必等 1800 秒年龄（收尾专项：被 kill 的 run 残留 pi.lock 会挡后续 run_act）。
                # PID 读不到/解析不了 ⇒ 回落到年龄判断（保守，不误偷活锁）。
                try:
                    _owner = int(Path(self.path).read_text(encoding="utf-8").strip().split()[0])
                    try:
                        os.kill(_owner, 0)
                    except ProcessLookupError:
                        os.unlink(self.path)
                        self.stole = True
                        continue
                    except (PermissionError, OSError):
                        pass
                except (ValueError, IndexError, OSError):
                    pass
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    age = 0.0
                if age > self.stale_s:
                    os.unlink(self.path)
                    self.stole = True
                    continue
                if time.time() - t0 > self.max_wait_s:
                    raise TimeoutError("等 pi 文件锁超时 %.0fs（%s）—— 不并发起进程"
                                       % (self.max_wait_s, self.path))
                time.sleep(self.poll_s)

    def __exit__(self, *exc):                                # noqa: ANN002
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------- 真跑一次
def build_argv(node: dict, act: str, knobs: dict, prompt: str, sid: str) -> list:
    """照模板拼 argv（叶工具由 role 表的 tools 旋钮定，其余 act 一律 --no-tools）。

    V2.2（Leo 2026-11-06 设计问题回应）：工具权限是 role 的能力，不是 build_argv
    的硬编码分支。role.yaml 第 13 个旋钮 `tools` 声明该 role 的 pi 工具 allowlist
    （逗号分隔，空串 = --no-tools）；role 表是工具的唯一事实来源，build_argv 只从
    knobs（= role.effective(role, act) 的返回值）读 tools，不再按 role 名分流。
    knobs 里没有 tools（调用方没走 role.effective）⇒ fail closed 给 --no-tools，
    绝不猜工具。tools 非空的叶同时挂 loop-breaker 扩展 -e（防重复调同一 bash 命令，
    F7 t-d301b69799d2）；扩展文件缺失时不加 -e、不崩。

    原因对照（P5 tier1-2）：父派唯一 may_write 的角色是对的，但旧 build_argv
    按 role 名硬编码、只给只读调查角色工具，写角色叶拿 --no-tools 跑 role_prompt
    + may_write + min_out_chars 1500 的契约 ⇒ 0 工具调用 + 编造“已跑 sysctl”。
    修法不是换个硬编码分支，而是让 role 表同时声明需求（may_write/role_prompt）
    与能力（tools）。
    """
    th = THINKING_MAP.get(str((knobs or {}).get("thinking") or "none"), "off")
    _tools = str((knobs or {}).get("tools") or "")
    if act == "leaf" and _tools:
        # F7（t-d301b69799d2）：把 loop-breaker 扩展真正接进 pi 命令行。
        # 之前 build_argv 从不传 --extension，扩展只存在于候选文件里、从未上场
        # （c2.2 同一条 log show 重复 70 次无人断）。扩展文件缺失时不加 -e、不崩。
        _ext = Path(__file__).resolve().parent.parent / "extensions" / "loop-breaker.ts"
        _base = ["pi", "-p", "--tools", _tools, "--mode", "text",
                 "--thinking", th, "--session-id", str(sid), "--model", MODEL_ARG, prompt]
        if _ext.is_file():
            return ["pi", "-p", "-e", str(_ext)] + _base[2:]
        return _base
    argv = ["pi", "-p"]
    structured_review = act == "leaf" and bool((node or {}).get("_structured_review"))
    managed_doc = act == "leaf" and bool((node or {}).get("_managed_doc"))
    if (act in {"plan", "merge", "spawn"} or structured_review or managed_doc) \
            and _local_llaat_adapter():
        extension = Path(__file__).resolve().parent.parent / "extensions" / "plan-json-schema.ts"
        argv.extend(["-e", str(extension)])
    argv.extend(["--no-tools", "--mode", "text", "--thinking", th,
                 "--session-id", str(sid), "--model", MODEL_ARG, prompt])
    return argv


# ---------------------------------------------------------------- F2 沙箱
def _sandbox_profile_text(run_dir: str) -> str:
    """F2：给 pi 进程的 macOS sandbox-exec profile（机制性硬保护，非提示词）。

    语义（实测 profile H：后匹配的规则赢）：先 (allow default)，再 deny 大面，
    然后 **deny 整个 run_dir**，最后只 allow authority 白名单 + 会话/配置目录。
    注意：/tmp 在 macOS 上是到 /private/tmp 的 symlink，profile 里必须写 canonical 路径，
    否则 deny 规则永远匹配不上（实测 profile A/B/E 的教训）。
    拒绝写：/private/tmp、~/agent-harness（生产树）、~/mini、~/Desktop、~/.ssh；
    再拒绝写：整个 run 目录（input/ 权威、product/ 等受保护产物都在里面）；
    放行写：authority 策略的 writable 路径（可写产物）+ pi 会话/配置目录。
    读、执行、联网一律放行。

    为什么先 deny 整个 run 再 allow 白名单：sandbox 规则是「后匹配优先」，
    整目录 deny 给了 fail-closed 的底（新增的 input/ 文件、模型自造的路径默认都不可写），
    再逐条 allow 只把 authority.writable 里点名的目录/文件抬回可写——
    允许清单之外的 run 内路径（B 验收里的 product/answer.txt）就此写不进去，
    从提示词约束变成机制性拦截。白名单必须写在 deny 之后，否则被整目录 deny 盖掉。
    """
    from harness import authority as authority_mod

    home = os.environ.get("HOME") or str(Path.home())
    run = str(Path(run_dir).resolve())
    sess = str(Path(session_dir()).resolve())
    agent = str(Path(agent_dir()).resolve())
    lines = ["(version 1)", "(allow default)",
             '(deny file-write* (subpath "/private/tmp"))',
             '(deny file-write* (subpath "%s/agent-harness"))' % home,
             '(deny file-write* (subpath "%s/mini"))' % home,
             '(deny file-write* (subpath "%s/Desktop"))' % home,
             '(deny file-write* (subpath "%s/.ssh"))' % home,
             # 先整目录不可写（fail closed），白名单只在后面的 allow 里开洞。
             '(deny file-write* (subpath "%s"))' % run]
    # 可写白名单：authority.json 的 writable（缺省 product/ blocks/ doc/）。
    # 目录用 subpath（连同其下新文件），已存在的单文件用 literal（不波及同目录其它文件）。
    try:
        writable = authority_mod.writable_abs_paths(run_dir)
    except Exception:                                        # noqa: BLE001
        writable = []
    for _p in sorted(set(str(Path(x).resolve()) for x in writable)):
        kind = "literal" if Path(_p).is_file() else "subpath"
        lines.append('(allow file-write* (%s "%s"))' % (kind, _p))
    # pi 必须能写会话/配置目录（sessions/*.jsonl、models.json 等），否则起不来。
    for _a in sorted({sess, agent}):
        lines.append('(allow file-write* (subpath "%s"))' % _a)
    return "\n".join(lines) + "\n"


def _sandbox_wrap(argv: list, run_dir: str = None) -> list:
    """F2：用 sandbox-exec 包 pi（真跑唯一路径；TREE_PI_SANDBOX=0 可关，供回归）。"""
    if os.environ.get("TREE_PI_SANDBOX") == "0":
        return argv
    if not run_dir or os.uname().sysname != "Darwin":
        return argv
    import shutil
    if not shutil.which("sandbox-exec"):
        return argv
    prof = Path(run_dir) / "pi-sandbox.sb"
    try:
        prof.parent.mkdir(parents=True, exist_ok=True)
        prof.write_text(_sandbox_profile_text(run_dir), encoding="utf-8")
    except OSError:
        return argv
    return ["sandbox-exec", "-f", str(prof)] + list(argv)


def run_act(node: dict, act: str, knobs: dict, prompt: str, session_id: str = None,
            run_dir: str = None, agent: str = None, sess: str = None,
            timeout: float = None) -> str:
    """真跑一次 pi。★ 只有这条路径会起 pi 进程。

    @returns: pi 的 stdout 原文
    @raises ContextOverflow: 历史 + 本轮估算 > ctx_budget（响亮，绝不静默截断）
    """
    if os.environ.get("TREE_ACT_CMD"):
        raise RuntimeError("TREE_ACT_CMD 已设却走到了真 pi 路径：驱动器接线错了"
                           "（桩路径下一个 pi 进程都不许起）")
    if not session_id:
        raise ValueError("真跑必须给 session-id（复活凭据）")
    sid = str(session_id)
    hist = history_tokens(sid, sess)
    budget = int((knobs or {}).get("ctx_budget") or 0)
    est = submit.estimate_tokens(prompt)
    # V2.1 P2 (GPT 680): pre-submission budget by Role + full request —
    # input + max generatable output must fit the server per-request window
    # (prompt + max_tokens <= 163840, max-kv-size).
    _mt = int((knobs or {}).get("max_tokens") or 0)
    if hist + est + _mt > 163840:
        raise RuntimeError(
            "单请求超服务端上限：历史 %d + 本轮估算 %d + max_tokens %d = %d > 163840（max-kv-size）⇒ 拒"
            % (hist, est, _mt, hist + est + _mt))
    # Budget ladder: overflow -> widen one rung, max 2 retries; still overflowing
    # -> loud ContextOverflow (overflow.jsonl is the mechanical Checkpoint).
    _tries = 0
    while budget and hist + est > budget:
        from harness import budget as _budget_mod
        _nxt = _budget_mod.next_budget_rung(budget)
        try:
            _budget_mod.record_overflow(str(run_dir or ""), {
                "node_id": str((node or {}).get("id") or ""), "act": str(act or ""),
                "history_tokens": hist, "estimate": est, "budget": budget,
                "overage": hist + est - budget, "next_rung": _nxt, "session_id": sid})
        except Exception:
            pass
        if _nxt is None or _tries >= 2:
            raise ContextOverflow(
                "history_tokens(%s)=%d + 本轮估算 %d = %d > ctx_budget=%d（已按档扩容 %d 次到顶，"
                "溢出记录见 overflow.jsonl）⇒ 拒（不许静默截断历史，76 §4.6 / D4）"
                % (sid, hist, est, hist + est, budget, _tries))
        budget = _nxt
        _tries += 1

    # V2.1 收尾 (A)：pi 起跑前确保 LLAAT 适配器在跑（起不来就响亮失败，不回直连）。
    ensure_adapter(agent=agent)
    env = pi_env(agent, sess)
    env.pop("TREE_PI_RESPONSE_FORMAT", None)
    structured_review = act == "leaf" and bool((node or {}).get("_structured_review"))
    managed_doc = act == "leaf" and bool((node or {}).get("_managed_doc"))
    if (act in {"merge", "spawn"} or structured_review or managed_doc) \
            and _local_llaat_adapter():
        from harness import schema as schema_mod
        if managed_doc:
            floor = int(((node or {}).get("_managed_doc") or {}).get("chars_min") or 1)
            response_format = schema_mod.managed_doc_response_format(floor)
        elif act == "merge":
            response_format = schema_mod.merge_response_format(
                bool((knobs or {}).get(schema_mod.AUDIT_KEY)))
        elif act == "spawn":
            response_format = schema_mod.spawn_response_format()
        else:
            response_format = schema_mod.structured_review_response_format(
                (node or {}).get("_review_blocker_id") or "",
                (node or {}).get("_review_evidence_ids") or [])
        env["TREE_PI_RESPONSE_FORMAT"] = json.dumps(response_format, ensure_ascii=False)
    # F2 收容：有工具的叶（knobs.tools 非空）的 pi 进程 cwd 钉在 run_dir，相对路径写入
    # （blocks/ 等）只能落在 run 内，够不到生产树/工作树根。绝对路径写入另由证据门扫描拒收。
    # V2.2：工具由 role 表 tools 旋钮定，cwd 钉选跟随 knobs.tools 走，不再按 role 名分流；
    # 执行叶写 doc 的 blocks/ 相对路径必须落在 run 内，沙箱才放行（不钉时 cwd 是候选根，
    # blocks/ 写到 ~/agent-harness 下会被沙箱 deny）。
    _cwd = str(run_dir) if (run_dir and str((knobs or {}).get("tools") or "")) else None
    _timeout = float(timeout or SINGLE_CALL_TIMEOUT)
    if str((node or {}).get("role") or "") == "调研":
        # F3：研究叶硬上限 300 秒（run_leaf 的 300 只是请求值；这里是执行层兜底，
        # TREE_PI_TIMEOUT 再大也压不住）。干跑脚手架（t-f6f9d7325d93）：TREE_DRYRUN=1
        # 时压到 90 秒；回撤时删 _cap 这一行。
        _cap = 90.0 if os.environ.get("TREE_DRYRUN") == "1" else LEAF_TIMEOUT_S
        _timeout = min(_timeout, _cap)
    _argv = _sandbox_wrap(build_argv(node, act, knobs, prompt, sid), run_dir)
    lock = SerialLock(str(Path(run_dir or Path(role_mod.ROOT) / "runs") / "pi.lock"))
    with lock:
        # pi 在 -p 模式下从 argv 取 prompt，不读 stdin；但 stdin 若是父进程继承来的
        # 开放管道（ssh / 循环调度器），pi 会等 stdin EOF 才往下走、表现为挂住。
        # 实测：stdin=/dev/null 时 5 秒出结果，继承 ssh 管道时 150 秒超时。
        # 因此一律给 DEVNULL（立即 EOF），不依赖调用方的 stdin 状态。
        proc = subprocess.run(_argv, env=env,
                              stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, cwd=_cwd,
                              timeout=_timeout)
    if proc.returncode != 0:
        raise RuntimeError("pi rc=%d stderr=%s" % (proc.returncode, (proc.stderr or "")[-400:]))
    return proc.stdout


# ---------------------------------------------------------------- 读回（保真）
def find_session_file(sid: str, sess: str = None) -> str:
    """定位 <session dir>/*_<sid>.jsonl；找不到 ⇒ 空串。"""
    d = sess or session_dir()
    hit = sorted(glob.glob(str(Path(d) / ("*_%s.jsonl" % sid))))
    return hit[-1] if hit else ""


def _norm_line(lineno: int, raw: str) -> dict:
    """一条记录：type / role / 原文 raw（逐字节，不截断入库）。"""
    rec = {"line": lineno, "raw": raw.rstrip("\n"), "type": "", "role": ""}
    try:
        obj = json.loads(rec["raw"])
    except Exception:                                        # noqa: BLE001
        rec["type"] = "<unparsable>"
        return rec
    rec["type"] = str(obj.get("type") or "")
    msg = obj.get("message") or {}
    if isinstance(msg, dict):
        rec["role"] = str(msg.get("role") or "")
        rec["content"] = msg.get("content")
        rec["usage"] = msg.get("usage") or {}
        rec["timestamp"] = msg.get("timestamp")
    else:
        rec["content"] = msg
        rec["usage"] = {}
    if rec["type"] != "message":
        rec["role"] = rec["role"] or ("system" if rec["type"] == "session" else "")
    return rec


def read_session(sid: str, sess: str = None, path: str = None) -> list:
    """把一个会话 jsonl 解析成记录列表。

    ★ 保留每条原文（rec["raw"] = 该行逐字节原文，不摘要、不截断）。
    ★ 角色区分 user / assistant / tool（+ system）：tool 通道本批**未观测到**（--no-tools），
      但解析器支持它 —— 真出现 role="tool" 的记录会被如实标出。
    """
    p = path or find_session_file(sid, sess)
    if not p or not os.path.isfile(p):
        return []
    out = []
    with open(p, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if line.strip():
                out.append(_norm_line(i, line))
    return out


def roles_seen(records: list) -> list:
    """出现过的角色清单（保真证据用）：按首次出现的顺序去重。"""
    out = []
    for r in records:
        if r.get("type") == "message":
            role = r.get("role") or "?"
            if role not in out:
                out.append(role)
    return out


def history_tokens(sid: str, sess: str = None) -> int:
    """历史量 = **最后一次 assistant 记录的 usage.input**（服务端真值，不是估算）。"""
    last = 0
    for r in read_session(sid, sess):
        if r.get("role") == "assistant" and isinstance(r.get("usage"), dict):
            v = r["usage"].get("input")
            if isinstance(v, int):
                last = v
    return last
