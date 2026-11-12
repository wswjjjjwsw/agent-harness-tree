"""CC 节点执行器：把 harness 的 act_runner 接口接到 Claude Code（走 ccswitch 15721 → Sage）。

第 3 步接线（/tmp/cc-role-customization-report.md §6.3/§6.4，A 架构：驱动器编排）。
本模块是 pi 路径（harness/session.run_act）的平行实现：同一个 act_runner 签名
(node, act, knobs, prompt, session_id, run_dir, timeout=None) -> str（模型正文），
Driver / leafrun 不用改调用方式，只要注入本模块的 cc_act_runner 即可。

路由（Leo 2026-11-06 13:18 纠正）：**一律走 ccswitch 15721（Leo 现有路由，
已配到 Sage，从不 stalls）**。不启动、不用实验代理 8899（已删）。
- ANTHROPIC_BASE_URL=http://127.0.0.1:15721（env 里显式钉死）。
- 模型名用 CC 认识的标签 claude-sonnet-4-5（--model 与 ANTHROPIC_DEFAULT_*_MODEL）；
  15721 代理按 provider（sage-local-20261106）把模型名覆盖成 Sage 真实 id。
- ★ 隔离最小配置（2026-11-06 ccswitch 调查后定）：harness 调 CC 时**不要用
  Leo 的 live ~/.claude/ 配置**，用隔离的最小 CLAUDE_CONFIG_DIR
  （缺省 /tmp/cc-minimal-config，仅一个空 settings.json；见 ensure_minimal_config）。
  另传 ANTHROPIC_BASE_URL=15721 与 ANTHROPIC_AUTH_TOKEN=PROXY_MANAGED
  （ccswitch 的约定；最小配置的 settings.json 是空的，不含它们）。
  树自己的 agent 文件放每个 run 的 worktree 下项目级 .claude/agents/
  （CC 不论 CLAUDE_CONFIG_DIR 都读项目级 agent）。**不要动 Leo 的 live 配置**
  （ECC 是他日常用的）。
- cwd = run_dir（该目录含 .claude/agents/，项目级 agent 优先发现）。

★ (a) 全部 role 不调工具（2026-11-06 重测后定，见 harness/cc_agents.py）：
  生成器缺省 no_tools=True，全部 role 的 agent 文件 tools=[]；模型只出 JSON，
  驱动器做工具活。重测（/tmp/cc-role-retest-15721-report.md）6 role 各 1 次
  真实 Sage 调用、全部走 ccswitch 15721：无工具 role 4/4 PASS（根/规划/决策/
  审计，rc=0）；用工具的 role 0/2 FAIL（执行/调研各调 3 次 Bash、工具结果
  全对，但始终没产出最终 JSON，120s timeout 杀掉 rc=124）。同一 agent、同一
  prompt、同一路由，根因调查时调研成功过一次（rc=0、49s、最终 JSON）——
  **工具 role 在 15721 上是非确定性的**，有时收敛、有时停滞；实验代理 framing
  修复解决的是确定性停滞，工具 role 不收敛是另一个模型侧问题，修代理修不掉。
  **接线不要指望工具 role 每次都收敛。** 本模块的硬兜底（工具 role 开启时）：
  ① 工具调用次数上限 MAX_TOOL_CALLS（超限 ⇒ 判失败，不许空转到超时）；
  ② deadline（超时 ⇒ 抛 subprocess.TimeoutExpired，leafrun 超时抢救复用）。

★ prompt 异常大的根因已定位（2026-11-06 ccswitch 调查，/tmp/ccswitch-prompt-
  investigation-report.md）：**ccswitch 不加 prompt**（对照实验：绕开 CC 直接
  curl 15721 发最小请求，发 28 tokens、Sage 实收 28 tokens，一字未加）。
  多出来的约 14,799 tokens 全部来自 CC 自身的 live 配置——ECC 插件的能力清单
  （219 个 skill 的 description 约 8.9k、60 个 agent 约 2.9k、75 个命令约 2.3k、
  AGENTS.md 约 2.1k），CC 启动时把这份清单拼进 system prompt，每轮正式请求都
  跟着发。差分实验证实：同一 agent/任务，只换 CC 用户级配置——live 配置正式
  请求 19,515 tokens，最小临时配置（仅空 settings.json）正式请求 4,716 tokens，
  差值 14,799。**接线因此用隔离最小配置（见上），正式请求回到约 4.7k tokens、
  prefill 约 4 秒（live 配置约 19.5k、约 18 秒）。** 预算预检的开销只剩
  CC_OVERHEAD_TOKENS=1000 的小安全余量（估算误差），不再是 20000。

防御：
- 调用方必须设 deadline（subprocess timeout）；超时 ⇒ 抛 subprocess.TimeoutExpired
  （与 pi 路径同款，leafrun 的超时抢救逻辑直接复用）。
- stdin=DEVNULL（与 run_act 同款，防 pi/CC 等 stdin EOF 才往下走）。
- 预算硬控（驱动器侧）：发请求前估算输入 tokens（prompt + agent 正文 + 工具开销
  + 小安全余量），超 ctx_budget 或 prompt+max_tokens > 163840（服务端
  max-kv-size）⇒ 响亮拒发。
- 输出 tokens 事后核对 out_budget / max_tokens（CC 的 max_tokens 是它自身行为，
  驱动器侧只能事后记一笔，见 normalize/verify 的 notes；真想卡输出上限须另配 CC
  settings，属后续工作）。
- 每次调用的 stream-json 原文与 .meta.json 落到 run_dir（cc-<node>-<act>.jsonl/.meta.json），
  供证据门与事后审计；实际 tool 调用列表同时记到节点（_cc_tool_calls）与 meta 文件。

★ 权限 fail-closed（2026-11-07，cc-permission-failclosed）：
- 缺省 --permission-mode acceptEdits（原先 build_cc_argv 硬编码 bypassPermissions，
  在 harness/cc_run.py:181；纠正：之前工单说的 182 行是错的，182 行是 --agent/--model）。
- bypassPermissions 只有同时满足三项才放行（resolve_bypass_permission 核验，缺一不可，
  否则 RuntimeError 拒跑，不静默降级）：TREE_CC_ALLOW_BYPASS=1、
  TREE_CC_SANDBOX_PROFILE 指向真实存在的 .sb 文件、shutil.which("sandbox-exec")
  找得到。放行时整条 CC argv 包进 sandbox-exec -f <profile> 再执行（argv[0] 变
  sandbox-exec），meta 记 sandboxed=true 与 profile 路径。
- Bash 白名单（CC_BASH_ALLOWLIST，写死在代码里）：只放行只读/测试类简单命令前缀；
  驱动器侧 verify_bash_allowlist 逐条核验模型实际调的 Bash，含 shell 元字符或不在
  白名单里 ⇒ 判节点失败（meta 记 violations）。-p 非交互 acceptEdits 会拒复合 Bash，
  白名单只放简单命令正是 Gate 2 P0-2 的修复方向。
- settings.json allow/deny（ensure_safe_settings，只写隔离配置目录，绝不碰 ~/.claude/）：
  allow = 白名单 Bash 规则（形如 Bash(ls *)，空格+* 同时覆盖裸命令）+ Read/Edit/Write；
  deny = 具体危险命令（Bash(rm *)/Bash(curl *)/Bash(wget *)/Bash(sudo *)/Bash(ssh *)/
  Bash(scp *)）。语义已按 CC 官方文档核实（code.claude.com/docs/en/permissions.md 与
  permission-modes.md，2026-11-07 查）：规则按 deny → ask → allow 评估，先匹配者胜，
  deny 在所有模式下都生效（含 bypassPermissions），allow 在 bypass 下无效；`Bash(*)`
  等价于 `Bash`，作 deny 会把 Bash 工具整个从模型上下文里摘掉——所以 deny 里绝不用
  `Bash(*)` 兜底。deny 只是纵深防御：同一程序换绝对路径或包进 sh -c 就绕过规则文本
  匹配，硬 enforcement 永远是驱动器侧 verify_bash_allowlist + sandbox-exec。
  未设 permissions.disableBypassPermissionsMode：它会连同经 sandbox 放行的 bypass
  一起禁掉（同一隔离配置目录服务两条路），bypass 的闸只放在 resolve_bypass_permission。
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import time
from pathlib import Path

from harness import cc_agents

# ccswitch 15721（Leo 现有路由，已配到 Sage）。不要改成实验代理 8899（已删）。
CC_BASE_URL = "http://127.0.0.1:15721"
# CC 认识的模型标签（路由标签；真实 Sage id 由 15721 provider 侧映射）。
CC_MODEL_LABEL = "claude-sonnet-4-5"
# 缺省 deadline（秒）。调用方（run_leaf 的叶上限 / Driver）传 timeout 时以调用方为准。
# V2：环境变量 TREE_CC_TIMEOUT 可覆盖缺省（单叶 4 模块约 460 秒，300 秒不够一轮写完）。
# ★ 300 秒（2026-11-06 演示实测后调高）：Sage 服务端排队/争用可让小请求等几十秒
# （CC 慢速根因：探测请求 45 秒里约 44.6 秒是排队；演示 plan 轮在两路大 prefill
# 并发时 decode 仅 2.7 tok/s、120 秒不够）。无工具 role 本身是确定性的（重测 4/4 PASS），
# 300 秒不是纵容挂起，而是给排队留余量；工具 role 的挂起仍由 MAX_TOOL_CALLS + deadline 兜底。
CC_TIMEOUT_DEFAULT = float(os.environ.get("TREE_CC_TIMEOUT") or 300.0)
# 服务端单请求上限（max-kv-size，见 t-6161aa7cc933 / t-5ce41e0e82c9）。
SERVER_MAX_TOKENS = 163840
# 工具定义 token 开销的粗估（每工具约 1000 tokens；第 1 步实测 25 工具约 24k）。
# 只用于预算预检的保守估计，不是精确计量。
TOKENS_PER_TOOL = 1000
# 预算预检的安全余量（tokens）：只 cover 字符/4 估算误差与 CC 固定前缀（约 35 tokens）。
# ★ 2026-11-06 ccswitch 调查后：原 20000 的开销已作废——那是 live 配置 ECC 能力
# 清单（约 14,799 tokens）造成的，harness 改用隔离最小配置后正式请求回到约 4.7k
# tokens（调查 Test B 实测），估算（prompt+agent 正文+工具）已与实收接近，不再需要大开销。
CC_OVERHEAD_TOKENS = 1000
# 隔离最小 CC 配置目录（缺省：调查留下的 /tmp/cc-minimal-config，仅一个空 settings.json）。
# 可用环境变量 TREE_CC_CONFIG_DIR 覆盖。不要指到 Leo 的 live ~/.claude/（ECC 是他日常用的）。
CC_CONFIG_DIR_DEFAULT = "/tmp/cc-minimal-config"
# ccswitch 的约定：最小配置的 settings.json 是空的，auth 靠 env 传（与调查 Test B 一致）。
CC_AUTH_TOKEN = "PROXY_MANAGED"
# 工具 role 的硬兜底：单次 act 的工具调用次数上限（重测：成功/失败的工具 role 各调 3 次；
# 5 次是正常使用的上限，超限 ⇒ 判失败，不许空转到超时）。(a) 缺省无工具 role 时恒为 0，
# 该上限不触发；工具 role 开启后才生效。
MAX_TOOL_CALLS = 5
# 无进展重复调用熔断（t-dc62df942275 项 5）：同一工具 + 等价参数连续出现该次数
# 即判定叶已陷入无进展循环，终止该叶并记 failure_class=loop（见 contract.py）。
# 只计连续相同调用：内容/输入变化后的合法重查会重置计数，不被阻断。
LOOP_MAX_CONSECUTIVE_SAME_CALL = 3

# 权限模式缺省值（2026-11-07 fail-closed：原先 build_cc_argv 硬编码 bypassPermissions，
# 见模块 docstring；缺省改为 acceptEdits，bypass 须经 resolve_bypass_permission 显式放行）。
CC_PERMISSION_MODE_DEFAULT = "acceptEdits"
# build_cc_argv 接受的 permission_mode 取值（CC 官方 modes 的子集；auto/dontAsk 不开放，
# 避免无人值守下出现 classifier/全拒等未验证行为）。
CC_PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions", "plan")
# Bash 白名单：只读 + 测试类简单命令前缀（写死在代码里，不读外部配置）。
# 复合命令（; && || | > < $() 等）一律不在白名单里——见 verify_bash_allowlist。
CC_BASH_ALLOWLIST = (
    "ls", "cat", "head", "tail", "grep", "find", "sed -n", "wc", "sort", "uniq",
    "diff", "file", "stat", "pwd", "echo", "test",
    "python3 -m pytest", "python3 -m unittest", "python3 -m py_compile",
    "git status", "git diff", "git log", "git show", "git branch",
)
# settings.json deny 纵深防御：具体危险命令（deny 先于 allow 评估，在所有模式下生效，
# 含 bypassPermissions；但规则只匹配命令文本，绝对路径/sh -c 可绕过，硬 enforcement
# 靠驱动器侧 verify_bash_allowlist + sandbox-exec——见 ensure_safe_settings）。
CC_BASH_DENYLIST = (
    "rm", "curl", "wget", "sudo", "ssh", "scp",
)

# thinking 旋钮 → CC --effort 映射（none = 不传 --effort，CC 默认不开扩展思考）。
EFFORT_MAP = {"none": None, "low": "low", "medium": "medium"}


def _find_cc() -> str:
    """定位 claude 二进制（~/.local/bin/claude；不在 PATH 里就补上再找）。"""
    path = os.environ.get("PATH") or ""
    home_bin = str(Path.home() / ".local" / "bin")
    if home_bin not in path.split(os.pathsep):
        path = home_bin + os.pathsep + path
    found = shutil.which("claude", path=path)
    if not found:
        raise RuntimeError("找不到 claude 二进制（PATH 里没有 ~/.local/bin/claude）")
    return found


def ensure_minimal_config(config_dir: str = None) -> str:
    """确保隔离最小 CC 配置目录存在且含一个空 settings.json（离线可测，不起进程）。

    缺省用 CC_CONFIG_DIR_DEFAULT（/tmp/cc-minimal-config，调查留下的）；可用环境变量
    TREE_CC_CONFIG_DIR 或参数覆盖。目录不存在 ⇒ 建目录 + 写 "{}" 的 settings.json；
    settings.json 已存在 ⇒ 不动它（不覆盖 CC 运行时写的状态）。
    /tmp 是易失的：被清掉后下次调用重建即可（空 settings.json，无状态可丢）。
    @returns: 配置目录的绝对路径字符串。
    """
    d = Path(config_dir or os.environ.get("TREE_CC_CONFIG_DIR") or CC_CONFIG_DIR_DEFAULT)
    d.mkdir(parents=True, exist_ok=True)
    settings = d / "settings.json"
    if not settings.is_file():
        settings.write_text("{}", encoding="utf-8")
    return str(d)


def _bash_allow_rule(prefix: str) -> str:
    """白名单前缀 → CC settings.json 的 Bash allow 规则（`Bash(<前缀> *)`）。

    按官方语义：`*` 前的空格是规则的一部分（`Bash(ls *)` 匹配 `ls`/`ls -la`，
    不匹配 `lsof`）；行尾单独的 ` *` 也匹配裸命令（`Bash(git log *)` 匹配 `git log`）。
    """
    return "Bash(%s *)" % prefix


def ensure_safe_settings(config_dir: str = None) -> str:
    """给隔离配置目录的 settings.json 合并写入安全的 permissions 规则（离线可测）。

    只管理隔离配置目录（缺省 CC_CONFIG_DIR_DEFAULT，TREE_CC_CONFIG_DIR 可覆盖）。
    读现有 settings.json（不存在 ⇒ {}；不是合法 JSON 对象 ⇒ 响亮拒写，不静默覆盖），
    保留全部原有键，只合并 permissions.allow / permissions.deny（去重，原有规则在前）：
    - allow：CC_BASH_ALLOWLIST 的每条前缀一条 `Bash(<前缀> *)`，另加 `Read`/`Edit`/`Write`。
    - deny：CC_BASH_DENYLIST 的每条危险命令一条 `Bash(<命令> *)`（rm/curl/wget/sudo/ssh/scp）。
      语义已按 CC 官方文档核实（code.claude.com/docs/en/permissions.md、
      permission-modes.md，2026-11-07）：规则按 deny → ask → allow 评估，先匹配者胜；
      deny 在所有模式下都生效（含 bypassPermissions），allow 在 bypass 下无效。
      deny 里**绝不用 `Bash(*)` 兜底**：`Bash(*)` 等价于 `Bash`，作 deny 会把 Bash
      工具整个从模型上下文里摘掉，白名单规则随之失效。deny 只是纵深防御——同一程序
      用绝对路径或包进 sh -c 就绕过规则文本匹配，硬 enforcement 永远是驱动器侧
      verify_bash_allowlist（+ bypass 路的 sandbox-exec）。
    - 不写 permissions.disableBypassPermissionsMode：它会连同经 sandbox 放行的
      bypass 一起禁掉（同一隔离配置目录服务两条路）；bypass 的闸只放在
      resolve_bypass_permission。
    ★ 绝不碰 Leo 的 live ~/.claude/：解析后的 config_dir 等于 ~/.claude 或在其
      之下 ⇒ 直接抛错拒写。
    @returns: 配置目录的绝对路径字符串。
    @raises: RuntimeError（指到 live 配置 / settings.json 不是合法 JSON 对象 /
             permissions/allow/deny 形状不对 / 写盘失败）。
    """
    raw = config_dir or os.environ.get("TREE_CC_CONFIG_DIR") or CC_CONFIG_DIR_DEFAULT
    d = Path(raw).expanduser()
    live = Path("~/.claude").expanduser()
    try:
        d_resolved = d.resolve()
        live_resolved = live.resolve()
    except Exception:  # noqa: BLE001
        d_resolved, live_resolved = d, live
    if d_resolved == live_resolved or live_resolved in d_resolved.parents:
        raise RuntimeError(
            "ensure_safe_settings 拒写：config_dir %s 是 Leo 的 live ~/.claude/（只写隔离配置目录）"
            % raw)
    d.mkdir(parents=True, exist_ok=True)
    settings_path = d / "settings.json"
    data: dict = {}
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("settings.json 不是合法 JSON，拒写：%s（%s）" % (settings_path, exc))
        if not isinstance(data, dict):
            raise RuntimeError("settings.json 顶层不是对象，拒写：%s" % settings_path)
    perms = data.get("permissions") or {}
    if not isinstance(perms, dict):
        raise RuntimeError("settings.json 的 permissions 不是对象，拒写：%s" % settings_path)
    allow = perms.get("allow") or []
    deny = perms.get("deny") or []
    if not isinstance(allow, list) or not isinstance(deny, list):
        raise RuntimeError("settings.json 的 permissions.allow/deny 不是列表，拒写：%s" % settings_path)
    for rule in [_bash_allow_rule(p) for p in CC_BASH_ALLOWLIST] + ["Read", "Edit", "Write"]:
        if rule not in allow:
            allow.append(rule)
    for rule in ["Bash(%s *)" % cmd for cmd in CC_BASH_DENYLIST]:
        if rule not in deny:
            deny.append(rule)
    perms["allow"] = allow
    perms["deny"] = deny
    data["permissions"] = perms
    settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return str(d)


def _cc_env() -> dict:
    """CC 子进程 env：钉死 15721、模型标签与隔离最小配置。

    ★ 隔离最小配置（ccswitch 调查后定）：CLAUDE_CONFIG_DIR 指到最小配置目录
      （缺省 /tmp/cc-minimal-config，仅空 settings.json），**不要用 Leo 的 live
      ~/.claude/**（ECC 能力清单约 14,799 tokens，每轮正式请求都跟着发）。
      树自己的 agent 文件在 run 的 worktree 下项目级 .claude/agents/，
      CC 不论 CLAUDE_CONFIG_DIR 都读项目级 agent。
    ★ ANTHROPIC_AUTH_TOKEN=PROXY_MANAGED：最小配置的 settings.json 是空的，
      auth 靠 env 传（与调查 Test B 一致；ccswitch 的约定）。
    """
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = ensure_minimal_config()
    # 隔离配置的 settings.json 合并写入安全 permissions 规则（只写隔离目录，绝不碰
    # live ~/.claude/；失败 ⇒ 响亮拒跑，不静默跳过——见 ensure_safe_settings）。
    ensure_safe_settings(env["CLAUDE_CONFIG_DIR"])
    env["ANTHROPIC_BASE_URL"] = CC_BASE_URL
    env["ANTHROPIC_AUTH_TOKEN"] = CC_AUTH_TOKEN
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
        env[key] = CC_MODEL_LABEL
    for key in ("ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME", "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
                "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME", "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME"):
        env[key] = CC_MODEL_LABEL
    path = env.get("PATH") or ""
    home_bin = str(Path.home() / ".local" / "bin")
    if home_bin not in path.split(os.pathsep):
        env["PATH"] = home_bin + os.pathsep + path
    return env


def _bypass_sandbox_profile() -> Path:
    """取 bypass 放行用的 sandbox profile（TREE_CC_SANDBOX_PROFILE）。

    @returns: 存在的 .sb profile 文件路径。
    @raises: RuntimeError（未设 / 不是 .sb / 文件不存在）。
    """
    raw = os.environ.get("TREE_CC_SANDBOX_PROFILE") or ""
    if not raw:
        raise RuntimeError(
            "bypassPermissions 拒跑：未设 TREE_CC_SANDBOX_PROFILE（须指向真实存在的 .sb 文件）")
    p = Path(raw).expanduser()
    if p.suffix != ".sb":
        raise RuntimeError(
            "bypassPermissions 拒跑：TREE_CC_SANDBOX_PROFILE 不是 .sb 文件：%s" % raw)
    if not p.is_file():
        raise RuntimeError(
            "bypassPermissions 拒跑：sandbox profile 不存在：%s" % raw)
    return p


def resolve_bypass_permission(permission_mode: str = None) -> str:
    """解析最终 permission_mode（离线可测，不起进程）。

    None ⇒ CC_PERMISSION_MODE_DEFAULT（acceptEdits）。显式值须在 CC_PERMISSION_MODES
    内，否则 ValueError。bypassPermissions 只有同时满足三项才放行，缺一不可，
    否则 RuntimeError 拒跑，不静默降级：
    ① TREE_CC_ALLOW_BYPASS=1；② TREE_CC_SANDBOX_PROFILE 指向真实存在的 .sb 文件；
    ③ shutil.which("sandbox-exec") 找得到。
    @returns: 最终 mode 字符串。
    """
    mode = permission_mode or CC_PERMISSION_MODE_DEFAULT
    if mode not in CC_PERMISSION_MODES:
        raise ValueError("permission_mode %r 不在 %s" % (mode, list(CC_PERMISSION_MODES)))
    if mode != "bypassPermissions":
        return mode
    if os.environ.get("TREE_CC_ALLOW_BYPASS") != "1":
        raise RuntimeError(
            "bypassPermissions 拒跑：未设 TREE_CC_ALLOW_BYPASS=1（bypass 须显式放行）")
    _bypass_sandbox_profile()
    if shutil.which("sandbox-exec") is None:
        raise RuntimeError(
            "bypassPermissions 拒跑：找不到 sandbox-exec（PATH 里没有 sandbox-exec）")
    return mode


def build_cc_argv(role: str, cc_tools: list, prompt: str, effort: str = None,
                  permission_mode: str = None) -> list:
    """拼 CC 命令行（离线可测，不起进程）。

    --agent tree-<role>（agent 文件在 cwd 的 .claude/agents/ 里）；工具只由 agent
    文件的 frontmatter 定（重测证实 CC 尊重它），**不传 --tools**。
    ★ 不传 --tools 的原因（2026-11-06 演示实测）：--tools 是 commander.js 的
      variadic 选项（<tools...>），传 `--tools ""`（空列表）时 commander 会把
      紧跟的 prompt 也当成 tools 值吃掉，导致 prompt 缺失、CC 报
      "Input must be provided either through stdin or as a prompt argument"、
      rc=1。演示的 spawn 轮就是这么失败的（plan 轮侥幸没触发，原因未深究）。
      agent 文件已是工具的唯一事实来源，--tools 是多余的第二道防线，且有 bug，
      故去掉。cc_tools 参数保留（供 .meta.json 记录与预算预检），不进 argv。
    --model 用 CC 认识的标签；--effort 按 thinking 旋钮（none 不传）。
    --permission-mode：缺省 CC_PERMISSION_MODE_DEFAULT（acceptEdits；原先硬编码
      bypassPermissions 在 181 行，2026-11-07 改掉——之前说的 182 行是错的，
      182 行是 --agent/--model）。显式值须在 CC_PERMISSION_MODES 内，否则
      ValueError；bypassPermissions 须经 resolve_bypass_permission 放行
      （TREE_CC_ALLOW_BYPASS=1 + 真实 .sb profile + sandbox-exec 在 PATH 里），
      否则 RuntimeError 拒跑。注意：bypass 放行只决定 argv 里的 mode，包进
      sandbox-exec 由 run_cc_node 做（build_cc_argv 只拼 CC argv）。
    """
    agent = cc_agents.agent_name(role)
    mode = resolve_bypass_permission(permission_mode)
    argv = [_find_cc(), "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", mode,
            "--agent", agent, "--model", CC_MODEL_LABEL]
    if effort:
        argv += ["--effort", effort]
    argv.append(prompt)
    return argv


def _est_tokens(text: str) -> int:
    """粗估 tokens（字符/4；与 submit.estimate_tokens 同量级，预检够用）。"""
    return max(1, len(text or "") // 4)


def check_budget(prompt: str, agent_body: str, cc_tools: list, knobs: dict) -> dict:
    """发请求前的预算硬控（驱动器侧）。

    估算输入 = (prompt + agent 正文)/4 + 工具开销（每工具约 1000 tokens）
               + 安全余量（CC_OVERHEAD_TOKENS，见模块 docstring：隔离最小配置后
               正式请求回到约 4.7k tokens，估算已与实收接近，只留小余量）。
    超 role 的 ctx_budget，或 估算 + max_tokens > 163840（服务端上限）⇒ 响亮拒发。
    @returns: {"estimated_input_tokens": int}（通过时）。
    @raises: RuntimeError（拒发，写明哪项超了）。
    """
    knobs = knobs or {}
    est = (_est_tokens(prompt) + _est_tokens(agent_body)
           + len(cc_tools or []) * TOKENS_PER_TOOL + CC_OVERHEAD_TOKENS)
    ctx_budget = int(knobs.get("ctx_budget") or 0)
    max_tokens = int(knobs.get("max_tokens") or 0)
    if ctx_budget and est > ctx_budget:
        raise RuntimeError(
            "CC 调用拒发：估算输入 %d tokens > role ctx_budget=%d（prompt %d + agent 正文 %d + 工具 %d×%d + 余量 %d）"
            % (est, ctx_budget, _est_tokens(prompt), _est_tokens(agent_body),
               len(cc_tools or []), TOKENS_PER_TOOL, CC_OVERHEAD_TOKENS))
    if max_tokens and est + max_tokens > SERVER_MAX_TOKENS:
        raise RuntimeError(
            "CC 调用拒发：估算输入 %d + max_tokens %d = %d > 服务端上限 %d（max-kv-size）"
            % (est, max_tokens, est + max_tokens, SERVER_MAX_TOKENS))
    return {"estimated_input_tokens": est}


def normalize_leaf_output(obj: dict, knobs: dict) -> tuple:
    """叶产出的空字段机械补全（第 2 步 minor 项 3）。

    契约：第 2 步明确——空字段必须显式给 []，不许省略（决策 role 的 prompt 写明时
    模型能做到；执行 role 的 prompt 已同样写明，见 prompts/执行.md）。
    驱动器侧再补一道防线（模型仍省略时不直接判形状不合，而是显式补 [] 并记一笔）：
    - "未决" 缺失 ⇒ 补 []（并用 schema.normalize_undecided 归一化已有条目）。
    - may_write=true 且 "edits" 缺失 ⇒ 补 []。
    - "doc" 缺失不补（内容无法编造，交给 schema.validate 判）；may_write=false 的
      role 返回 doc/edits 也不删（交给 validate 判越权，不静默吞）。
    @returns: (归一化后的新 dict（不改原对象）, notes 列表)。
    @raises: obj 不是 dict ⇒ ValueError。
    """
    from harness import schema as _schema_mod
    if not isinstance(obj, dict):
        raise ValueError("normalize_leaf_output 只接受 dict，实际 %r" % type(obj).__name__)
    out = dict(obj)
    notes: list = []
    if "未决" not in out:
        out["未决"] = []
        notes.append("未决缺失 ⇒ 补 []（空字段必须显式给，不许省略）")
    else:
        normed = _schema_mod.normalize_undecided(out.get("未决"))
        if normed != out.get("未决"):
            out["未决"] = normed
            notes.append("未决条目已归一化（type/what/why 补齐）")
    if bool((knobs or {}).get("may_write")) and "edits" not in out:
        out["edits"] = []
        notes.append("edits 缺失 ⇒ 补 []（空字段必须显式给，不许省略）")
    return out, notes


_CLAIM_KEYWORDS = (
    # （doc 里的说法，须在工具调用里出现的相关词）
    ("sysctl", "sysctl"),
    ("压力测试", "stress"),
    ("stress", "stress"),
    ("实测", None),      # 泛指：须至少有一次工具调用
    ("运行", None),
    ("执行了", None),
    ("使用", None),
)


def verify_tool_claims(doc: str, tool_calls: list) -> dict:
    """doc 里的工具动作说法 vs 实际 tool 调用记录（第 2 步 minor 项 4 的最小核验）。

    证据门不能只信 doc 的说法（第 2 步：执行 role 的 doc 声称“使用压力测试工具”，
    实际一次工具都没调）。本函数做关键词级核验：
    - doc 提到具体命令（如 sysctl）⇒ 须有一次 Bash 调用含该命令；
    - doc 用“实测/运行/执行了/使用”等泛指说法 ⇒ 须至少有一次工具调用。
    @returns: {"verified": [...], "unverified": [...]}（各为命中的说法列表）。
    局限：关键词启发式，会有误报/漏报；完整证据门集成（harness/evidence.py 接 CC
    会话）属后续工作，这里只做到“说法与调用记录对得上”的最小闭环。
    """
    doc = str(doc or "")
    calls = list(tool_calls or [])
    bash_cmds = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        if str(c.get("name") or "") == "Bash":
            bash_cmds.append(str((c.get("input") or {}).get("command") or ""))
        elif str(c.get("name") or "") == "Read":
            bash_cmds.append(str((c.get("input") or {}).get("file_path") or ""))
    verified, unverified = [], []
    for keyword, need in _CLAIM_KEYWORDS:
        if keyword not in doc:
            continue
        if need is None:
            ok = bool(calls)
        else:
            ok = any(need in cmd for cmd in bash_cmds)
        (verified if ok else unverified).append(keyword)
    return {"verified": verified, "unverified": unverified}


# 拒绝一切含 shell 元字符的命令：命令分隔/管道/重定向/命令替换/变量展开/换行。
# 单个 & 也是命令分隔符（`ls & rm -rf x` 会后台跑 rm；CC 官方文档同样把 & 列为
# compound 命令分隔符），同样拒。
_SHELL_META_CHARS = (";", "&&", "||", "|", ">", "<", "`", "$(", "${", "&", "\n", "\r")


def _bash_violation_reason(command: str) -> str:
    """返回命令违反白名单的原因，合规则返回 ""。"""
    cmd = (command or "").strip()
    if not cmd:
        return "Bash 命令为空"
    for meta in _SHELL_META_CHARS:
        if meta in cmd:
            return "含 shell 元字符 %r（复合/管道/重定向/替换一律不放行）" % meta
    for prefix in CC_BASH_ALLOWLIST:
        if cmd == prefix or cmd.startswith(prefix + " "):
            return ""
    return "不在 Bash 白名单前缀里（只放行只读/测试类简单命令，见 CC_BASH_ALLOWLIST）"


def verify_bash_allowlist(tool_calls: list) -> dict:
    """逐条核验模型实际调的 Bash（离线可测）。

    只检查 name=="Bash" 的 tool_call（Read 等其他工具不在此函数管辖内）。
    拒绝：含 shell 元字符的命令；不在 CC_BASH_ALLOWLIST 前缀里的命令
    （前缀须整词匹配：`lsof` 不匹配 `ls`，`git push` 不在白名单里）。
    @returns: {"allowed": [放行的 Bash 命令 ...],
               "violations": [{"command": ..., "reason": ...}, ...]}。
    局限：只看命令文本——模型用绝对路径（/bin/rm）或包进 sh -c 时文本匹配会漏；
    真正的硬边界是 sandbox-exec（bypass 路）与 CC 侧 settings.json deny 规则，
    本函数是驱动器侧的最后一道文本闸。
    """
    allowed: list = []
    violations: list = []
    for c in (tool_calls or []):
        if not isinstance(c, dict):
            continue
        if str(c.get("name") or "") != "Bash":
            continue
        cmd = str(((c.get("input") or {}).get("command") or ""))
        reason = _bash_violation_reason(cmd)
        if reason:
            violations.append({"command": cmd, "reason": reason})
        else:
            allowed.append(cmd)
    return {"allowed": allowed, "violations": violations}


def _canonical_tool_key(name: str, inp) -> tuple:
    """工具调用的等价键（熔断用）：工具名 + 归一化后的输入 JSON。

    字符串值折叠空白（`ls  /tmp` 与 `ls /tmp` 等价）；dict 按键排序；
    不可 JSON 化的值退化为 repr。只用于“同一工具+等价参数”的判定，
    不改工具调用本身的记录（meta 里仍存原始 input）。
    """
    def _norm(v):
        if isinstance(v, str):
            return " ".join(v.split())
        if isinstance(v, dict):
            return {k: _norm(val) for k, val in sorted(v.items())}
        if isinstance(v, list):
            return [_norm(x) for x in v]
        return v
    try:
        return (str(name or ""),
                json.dumps(_norm(inp or {}), sort_keys=True, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return (str(name or ""), repr(inp))


def _track_tool_repeat(state: dict, call: dict) -> None:
    """更新连续相同调用计数（流式熔断用）：与上一次等价 ⇒ consec+1，否则重置为 1。

    连续达到 LOOP_MAX_CONSECUTIVE_SAME_CALL 时置 state["loop_tripped"]
    （{tool, count, key}）；调用方（流式读取器）据此停读、杀进程。
    离线解析（_parse_stream_json）同样走本函数，但不消费 loop_tripped。
    """
    key = _canonical_tool_key(call.get("name"), call.get("input"))
    if key == state.get("last_key"):
        state["consec"] = int(state.get("consec") or 0) + 1
    else:
        state["last_key"] = key
        state["consec"] = 1
    if (state["consec"] >= LOOP_MAX_CONSECUTIVE_SAME_CALL
            and state.get("loop_tripped") is None):
        state["loop_tripped"] = {"tool": str(call.get("name") or ""),
                                 "count": int(state["consec"]),
                                 "key": key}


def _apply_stream_event(ev: dict, state: dict) -> list:
    """把一个已解析的 stream-json 事件并入 state，返回本事件新抽出的 tool_call 列表。

    state 键：tool_calls / result_ev / init / in_tokens / out_tokens /
    last_key / consec / loop_tripped（后三者缺失时自动初始化，仅流式熔断用）。
    抽取口径与旧 _parse_stream_json 逐字一致（tool_use 块 + assistant usage 累加）。
    """
    new_calls: list = []
    if not isinstance(ev, dict):
        return new_calls
    etype = str(ev.get("type") or "")
    if etype == "system" and str(ev.get("subtype") or "") == "init":
        state["init"] = ev
    elif etype == "assistant":
        msg = ev.get("message") or {}
        content = msg.get("content") or []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    call = {"name": str(block.get("name") or ""),
                            "input": block.get("input") or {}}
                    state.setdefault("tool_calls", []).append(call)
                    new_calls.append(call)
                    _track_tool_repeat(state, call)
        _u = msg.get("usage") or {}
        if isinstance(_u, dict):
            state["in_tokens"] = int(state.get("in_tokens") or 0) + int(_u.get("input_tokens") or 0)
            state["out_tokens"] = int(state.get("out_tokens") or 0) + int(_u.get("output_tokens") or 0)
    elif etype == "result":
        state["result_ev"] = ev
    return new_calls


def _parse_stream_json(raw_text: str) -> dict:
    """解析 CC stream-json 输出。

    @returns: {"result_text": str, "is_error": bool, "usage": dict,
               "num_turns": int, "duration_ms": int, "tool_calls": [...],
               "init": dict, "unparsable_lines": int}。
    @raises: RuntimeError（无 result 事件 ⇒ 没有最终交付）。
    """
    state: dict = {"tool_calls": [], "result_ev": {}, "init": {},
                   "in_tokens": 0, "out_tokens": 0}
    unparsable = 0
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            unparsable += 1
            continue
        _apply_stream_event(ev, state)
    result_ev = state["result_ev"]
    if not result_ev:
        raise RuntimeError("CC 输出里没有 result 事件（进程可能被杀或未正常结束）")
    usage = {"input_tokens": state["in_tokens"], "output_tokens": state["out_tokens"]}
    return {
        "result_text": str(result_ev.get("result") or ""),
        "is_error": bool(result_ev.get("is_error")),
        "usage": usage,
        "num_turns": int(result_ev.get("num_turns") or 0),
        "duration_ms": int(result_ev.get("duration_ms") or 0),
        "tool_calls": state["tool_calls"],
        "init": state["init"],
        "unparsable_lines": unparsable,
    }


def _parse_partial_stream_json(raw_text: str) -> dict:
    """容忍截断的 stream-json 解析（超时路径用：无 result 事件也不抛）。

    只抽工具调用 + 计数，不要求最终交付；与 _parse_stream_json 同源，
    差别是缺 result 事件时返回空结果而不是 RuntimeError。
    """
    state: dict = {"tool_calls": [], "result_ev": {}}
    unparsable = 0
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            unparsable += 1
            continue
        _apply_stream_event(ev, state)
    return {"tool_calls": state["tool_calls"],
            "num_turns": int((state["result_ev"] or {}).get("num_turns") or 0),
            "unparsable_lines": unparsable}


def _feed_stream_line(line_b: bytes, raw_parts: list, state: dict, events_fh) -> None:
    """流式读取的一行：记 raw、解析事件、每抽出一个工具调用就追加落盘（flush + fsync）。

    未解析行记 state["unparsable"]（它可能含一个未写完的事件，如实记 lost，
    不静默丢掉）；空行跳过（与 _parse_stream_json 同口径）。
    """
    text = line_b.decode("utf-8", errors="replace")
    raw_parts.append(text + "\n")
    s = text.strip()
    if not s:
        return
    try:
        ev = json.loads(s)
    except Exception:  # noqa: BLE001
        state["unparsable"] = int(state.get("unparsable") or 0) + 1
        return
    for call in _apply_stream_event(ev, state):
        try:
            events_fh.write(json.dumps(call, ensure_ascii=False) + "\n")
            events_fh.flush()
            os.fsync(events_fh.fileno())  # F1：flush 只到 OS，fsync 才落盘（与 land.py/blocks.py 同口径；失败沿用本 try 的吞掉口径，不炸流式读）
        except Exception:  # noqa: BLE001
            pass


def _drain_fd_available(fd: int, parts: list) -> bool:
    """非阻塞排空一个 fd 一次：有数据 ⇒ 读进 parts（解码后），返回 True；
    无数据/EOF/读错 ⇒ 返回 False。只用于进程已退后的收尾，不阻塞。"""
    try:
        r, _, _ = select.select([fd], [], [], 0)
    except Exception:  # noqa: BLE001
        return False
    if not r:
        return False
    try:
        chunk = os.read(fd, 65536)
    except OSError:
        return False
    if not chunk:
        return False
    parts.append(chunk.decode("utf-8", errors="replace"))
    return True


def _read_streaming(proc, deadline_s: float, events_path, t0: float) -> dict:
    """流式读 CLI stdout（select + os.read 行切分），边读边把工具事件落盘。

    V2 §6.3 的修法：旧 `subprocess.run(capture_output=True, timeout=...)`
    超时杀掉 `claude -p` 时管道里未 flush 的 stream-json 全丢，超时 meta 的
    tool_calls 恒为 []。这里每行一到就解析、每解析出一个工具事件就增量追加
    写进 events_path（每事件 flush），超时/kill 后 meta 的 tool_calls 从
    该文件重建——kill 前已观测到的事件一个不少。
    @returns: {"timed_out": bool, "loop_tripped": dict|None, "raw_text": str,
               "stderr_text": str, "tool_calls": [...], "unparsable_lines": int,
               "result_ev": dict, "init": dict, "usage": dict, "num_turns": int,
               "duration_ms": int}。超时/熔断时调用方负责 kill；本函数只读到
               EOF、deadline 或熔断为止。残行（无换行结尾）：正常结束时仍尝试
               解析；超时/熔断时记 1 行 lost（它可能含未写完的事件）。
    """
    state: dict = {"tool_calls": [], "result_ev": {}, "init": {},
                   "in_tokens": 0, "out_tokens": 0, "unparsable": 0,
                   "last_key": None, "consec": 0, "loop_tripped": None}
    raw_parts: list = []
    err_parts: list = []
    out_fd = proc.stdout.fileno()
    err_fd = proc.stderr.fileno()
    buf = b""
    eof_out = False
    deadline_abs = float(t0) + float(deadline_s)
    timed_out = False
    events_fh = open(events_path, "w", encoding="utf-8")
    try:
        while True:
            if state["loop_tripped"] is not None:
                break
            alive = proc.poll() is None
            if eof_out and not alive:
                while _drain_fd_available(err_fd, err_parts):
                    pass
                break
            remaining = deadline_abs - time.time()
            if remaining <= 0:
                timed_out = True
                break
            fds = [err_fd] if eof_out else [out_fd, err_fd]
            wait = 0 if not alive else min(remaining, 0.2)
            try:
                r, _, _ = select.select(fds, [], [], wait)
            except Exception:  # noqa: BLE001
                r = []
            if not r:
                if not alive and eof_out:
                    break
                continue
            if out_fd in r:
                try:
                    chunk = os.read(out_fd, 65536)
                except OSError:
                    chunk = b""
                if chunk == b"":
                    eof_out = True
                else:
                    buf += chunk
                    while b"\n" in buf:
                        line_b, buf = buf.split(b"\n", 1)
                        _feed_stream_line(line_b, raw_parts, state, events_fh)
                        if state["loop_tripped"] is not None:
                            break
            if err_fd in r:
                try:
                    echunk = os.read(err_fd, 65536)
                except OSError:
                    echunk = b""
                if echunk:
                    err_parts.append(echunk.decode("utf-8", errors="replace"))
        if buf.strip():
            if timed_out or state["loop_tripped"] is not None:
                state["unparsable"] = int(state.get("unparsable") or 0) + 1
                raw_parts.append(buf.decode("utf-8", errors="replace"))
            else:
                _feed_stream_line(buf, raw_parts, state, events_fh)
    finally:
        try:
            events_fh.close()
        except Exception:  # noqa: BLE001
            pass
    result_ev = state["result_ev"] or {}
    return {
        "timed_out": timed_out,
        "loop_tripped": state["loop_tripped"],
        "raw_text": "".join(raw_parts),
        "stderr_text": "".join(err_parts),
        "tool_calls": state["tool_calls"],
        "unparsable_lines": int(state.get("unparsable") or 0),
        "result_ev": result_ev,
        "init": state["init"],
        "usage": {"input_tokens": int(state.get("in_tokens") or 0),
                  "output_tokens": int(state.get("out_tokens") or 0)},
        "num_turns": int(result_ev.get("num_turns") or 0),
        "duration_ms": int(result_ev.get("duration_ms") or 0),
    }


def _read_tool_events(events_path, fallback=None) -> list:
    """从增量落盘的 tool-events JSONL 重建 tool_calls（meta 用）。

    文件读不到/行解析失败 ⇒ 退回内存里的 fallback 列表（不静默变 []）。
    """
    try:
        out = []
        for line in Path(events_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out
    except Exception:  # noqa: BLE001
        return list(fallback or [])


def _events_note(observed: int, lost: int, complete: bool) -> str:
    """meta 里工具事件计数的诚实文案：事件数只代表已观测到的。

    事件数为 0 时必须如实记“观测到 0 个事件”，绝不能把“空事件”写成
    “0 次实际调用”——流被截断时未观测到的调用无法证伪，文案须写明。
    """
    if complete:
        return ("观测到 %d 个工具事件；未解析行 %d 行；流完整（进程正常结束）。"
                % (observed, lost))
    return ("观测到 %d 个工具事件；未解析行 %d 行（每行可能含未记录的事件）；"
            "流被截断（超时/熔断杀掉进程），事件数只代表已观测到的，不等于实际调用次数。"
            % (observed, lost))


def run_cc_node(role: str, prompt: str, run_dir: str, knobs: dict = None,
                timeout: float = None, session_id: str = "",
                permission_mode: str = None) -> dict:
    """跑一个 CC 节点（一次 claude -p 调用）。

    @param role: 树 role 名（中文，如 "执行"）；agent 文件须已生成在
                 <run_dir>/.claude/agents/tree-<拼音>.md（否则响亮报错）。
    @param prompt: 用户 prompt（驱动器已组装；role_prompt 不在里头，它在 agent 文件正文）。
    @param knobs: 该 role 的 effective knobs（ctx_budget/max_tokens/thinking/tools）。
    @param timeout: deadline 秒（None ⇒ CC_TIMEOUT_DEFAULT）。超时 ⇒ 抛
                     subprocess.TimeoutExpired（leafrun 的超时抢救逻辑复用）。
    @param permission_mode: 见 build_cc_argv（缺省 acceptEdits；bypass 须经
                     resolve_bypass_permission 放行，放行时整条 argv 包进
                     sandbox-exec -f <profile> 再执行，meta 记 sandboxed=true）。
    @returns: {"ok": True, "text": str（最终交付正文）, "tool_calls": [...],
               "input_tokens": int, "output_tokens": int, "num_turns": int,
               "duration_ms": int, "wall_s": float, "raw_jsonl": str,
               "meta_path": str, "normalize_notes": [...]}。
    @raises: RuntimeError（agent 文件缺失 / 预算拒发 / 进程非零退出 / 无 result / is_error /
             工具调用超限 / Bash 白名单违规 / 无进展重复调用熔断（loop）——后三项先写 meta 再抛，
             meta 里记 violations/loop 事件）。
    """
    knobs = knobs or {}
    run_path = Path(run_dir)
    agent_file = run_path / ".claude" / "agents" / cc_agents.agent_filename(role)
    if not agent_file.is_file():
        raise RuntimeError(
            "CC 节点拒跑：agent 文件不存在：%s（须先由 harness/cc_agents.py 生成）"
            % agent_file)
    agent_body = agent_file.read_text(encoding="utf-8")
    # (a) 缺省无工具：与生成器同一开关（harness/cc_agents.CC_NO_TOOLS_DEFAULT），
    # 工具 role 不在接线里出现；工具 role 开启后须配 MAX_TOOL_CALLS 硬兜底。
    cc_tools = cc_agents.cc_tools_for_role(knobs, no_tools=cc_agents.CC_NO_TOOLS_DEFAULT)
    budget = check_budget(prompt, agent_body, cc_tools, knobs)
    effort = EFFORT_MAP.get(str(knobs.get("thinking") or "none"))
    if effort is None and str(knobs.get("thinking") or "none") not in EFFORT_MAP:
        raise ValueError("thinking 旋钮 %r 不在 %s" % (knobs.get("thinking"), sorted(EFFORT_MAP)))
    resolved_mode = resolve_bypass_permission(permission_mode)
    argv = build_cc_argv(role, cc_tools, prompt, effort=effort,
                         permission_mode=resolved_mode)
    # bypass 放行时包进 sandbox-exec（resolve 已在上两行核验三项条件；这里只在
    # 放行后包壳，sandbox-exec 不可用/profile 缺失 ⇒ build 阶段已拒跑，
    # 绝不静默降级为裸跑）。
    sandboxed = False
    sandbox_profile = None
    if resolved_mode == "bypassPermissions":
        sandbox_profile = str(_bypass_sandbox_profile())
        argv = ["sandbox-exec", "-f", sandbox_profile] + argv
        sandboxed = True
    node_id = str(session_id or "cc-node")
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", node_id)
    raw_path = run_path / ("cc-%s.jsonl" % safe_id)
    meta_path = run_path / ("cc-%s.meta.json" % safe_id)
    deadline = float(timeout) if timeout else CC_TIMEOUT_DEFAULT
    t0 = time.time()
    events_path = run_path / ("cc-%s.tool-events.jsonl" % safe_id)
    # 流式读（t-dc62df942275 项 2+5）：Popen + 逐行读 stdout，工具事件边读边落盘；
    # 旧 subprocess.run(capture_output=True, timeout=...) 超时杀进程时管道里未
    # flush 的 stream-json 全丢（V2 §6.3，超时 meta tool_calls 恒为 []）。
    # 超时/熔断时先 kill 再写 meta，meta 的 tool_calls 从 events 文件重建。
    proc = subprocess.Popen(argv, env=_cc_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(run_path))
    stream = _read_streaming(proc, deadline, events_path, t0)
    wall = time.time() - t0
    if stream["timed_out"] or stream["loop_tripped"] is not None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            _tail_err = proc.stderr.read()
            if isinstance(_tail_err, bytes):
                _tail_err = _tail_err.decode("utf-8", errors="replace")
            stream["stderr_text"] = (stream["stderr_text"] or "") + (_tail_err or "")
        except Exception:  # noqa: BLE001
            pass
        raw_path.write_text(stream["raw_text"] or "", encoding="utf-8")
        rebuilt = _read_tool_events(events_path, fallback=stream["tool_calls"])
        _meta_cut = {
            "role": str(role),
            "agent": cc_agents.agent_name(role),
            "session_id": node_id,
            "tools": cc_tools,
            "timeout": bool(stream["timed_out"]),
            "loop": stream["loop_tripped"] is not None,
            "deadline_s": deadline,
            "wall_s": round(wall, 3),
            "rc": None,
            "tool_calls": rebuilt,
            "tool_events_path": str(events_path),
            "events_observed": len(rebuilt),
            "events_lost": stream["unparsable_lines"],
            "stream_complete": False,
            "events_note": _events_note(len(rebuilt), stream["unparsable_lines"], False),
            "num_turns": stream["num_turns"],
            "duration_ms": stream["duration_ms"],
            "unparsable_lines": stream["unparsable_lines"],
            "raw_jsonl": str(raw_path),
        }
        if stream["loop_tripped"] is not None:
            _meta_cut["loop_tool"] = stream["loop_tripped"]["tool"]
            _meta_cut["loop_count"] = stream["loop_tripped"]["count"]
        try:
            meta_path.write_text(json.dumps(_meta_cut, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        if stream["loop_tripped"] is not None:
            raise RuntimeError(
                "无进展重复调用熔断（loop）：工具 %s 连续 %d 次相同调用（等价参数），"
                "该叶终止；已观测工具事件 %d 个，见 %s"
                % (stream["loop_tripped"]["tool"], stream["loop_tripped"]["count"],
                   len(rebuilt), events_path))
        raise subprocess.TimeoutExpired(argv, deadline,
                                        output=stream["raw_text"],
                                        stderr=stream["stderr_text"])
    try:
        _tail_err = proc.stderr.read()
        if isinstance(_tail_err, bytes):
            _tail_err = _tail_err.decode("utf-8", errors="replace")
        stream["stderr_text"] = (stream["stderr_text"] or "") + (_tail_err or "")
    except Exception:  # noqa: BLE001
        pass
    raw_path.write_text(stream["raw_text"] or "", encoding="utf-8")
    if proc.returncode is None:
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("claude rc=%d stderr=%s"
                           % (proc.returncode, (stream["stderr_text"] or "")[-400:]))
    if not stream["result_ev"]:
        raise RuntimeError("CC 输出里没有 result 事件（进程可能被杀或未正常结束）")
    parsed = {
        "result_text": str(stream["result_ev"].get("result") or ""),
        "is_error": bool(stream["result_ev"].get("is_error")),
        "usage": stream["usage"],
        "num_turns": stream["num_turns"],
        "duration_ms": stream["duration_ms"],
        "tool_calls": stream["tool_calls"],
        "init": stream["init"],
        "unparsable_lines": stream["unparsable_lines"],
    }
    if parsed["is_error"]:
        raise RuntimeError("CC result 事件 is_error=true（num_turns=%d，result=%r）"
                           % (parsed["num_turns"], parsed["result_text"][:200]))
    # 硬兜底：工具调用次数上限（重测：工具 role 成功/失败各调 3 次；5 次是正常上限）。
    # 超限 ⇒ 判失败（不许空转到超时；调用方按失败走未决/重派）。(a) 无工具 role 时恒为 0，不触发。
    # Bash 白名单核验（与次数上限并列的第二道硬兜底）：模型实际调的 Bash 逐条过
    # verify_bash_allowlist，含 shell 元字符或不在白名单里 ⇒ 判失败。
    # 两项检查都在 meta 落盘后才抛（meta 里记 violations，不静默吞）。
    n_calls = len(parsed["tool_calls"] or [])
    bash_check = verify_bash_allowlist(parsed["tool_calls"])
    usage = parsed["usage"] or {}
    meta = {
        "role": str(role),
        "agent": cc_agents.agent_name(role),
        "session_id": node_id,
        "tools": cc_tools,
        "permission_mode": resolved_mode,
        "sandboxed": sandboxed,
        "sandbox_profile": sandbox_profile,
        "estimated_input_tokens": budget["estimated_input_tokens"],
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "num_turns": parsed["num_turns"],
        "duration_ms": parsed["duration_ms"],
        "wall_s": round(wall, 3),
        "rc": int(proc.returncode),
        "tool_calls": parsed["tool_calls"],
        "tool_events_path": str(events_path),
        "events_observed": len(parsed["tool_calls"]),
        "events_lost": parsed["unparsable_lines"],
        "stream_complete": True,
        "events_note": _events_note(len(parsed["tool_calls"]),
                                    parsed["unparsable_lines"], True),
        "bash_allowlist": bash_check,
        "unparsable_lines": parsed["unparsable_lines"],
        "raw_jsonl": str(raw_path),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    meta["meta_path"] = str(meta_path)
    if n_calls > MAX_TOOL_CALLS:
        raise RuntimeError("CC 工具调用超限：%d 次 > 上限 %d（模型疑似空转不收敛，判失败）"
                           % (n_calls, MAX_TOOL_CALLS))
    if bash_check["violations"]:
        detail = "; ".join("%s（%s）" % (v["command"][:80], v["reason"])
                           for v in bash_check["violations"][:3])
        raise RuntimeError("CC Bash 白名单违规（%d 条，meta 已落盘 %s）：%s"
                           % (len(bash_check["violations"]), meta_path, detail))
    return {
        "ok": True,
        "text": parsed["result_text"],
        "tool_calls": parsed["tool_calls"],
        "input_tokens": meta["input_tokens"],
        "output_tokens": meta["output_tokens"],
        "num_turns": parsed["num_turns"],
        "duration_ms": parsed["duration_ms"],
        "wall_s": meta["wall_s"],
        "raw_jsonl": str(raw_path),
        "meta_path": str(meta_path),
        "normalize_notes": [],
    }


def cc_act_runner(node: dict, act: str, knobs: dict, prompt: str,
                  session_id: str, run_dir: str, timeout: float = None,
                  permission_mode: str = None) -> str:
    """act_runner 接口（与 harness/driver.act_runner_default 同签名）：一个节点 act 跑一次 CC。

    返回模型最终交付正文（str）。Driver / leafrun 拿到它后走既有解析/校验
    （parse_act_output / schema.validate / judge），与 pi 路径同一条流水线。
    叶 act（act=="leaf"）时：先把交付解析成 JSON 做空字段机械补全
    （normalize_leaf_output：未决/edits 缺失 ⇒ 补 [] 并记一笔），再把补全后的
    JSON 文本返回；解析失败 ⇒ 返回原文（交给 Driver 的 parse_act_output 判）。
    超时 ⇒ 抛 subprocess.TimeoutExpired（leafrun 复用超时抢救）。
    permission_mode：透传给 run_cc_node（缺省 acceptEdits；bypass 须显式放行，见
    build_cc_argv）。Driver 调本函数时不传该参数（固定签名），缺省即 fail-closed。
    元数据（tool 调用、tokens、normalize notes）记到节点（_cc_* 键）与 .meta.json。
    """
    node = node or {}
    knobs = knobs or {}
    res = run_cc_node(str(node.get("role") or ""), prompt, run_dir, knobs=knobs,
                      timeout=timeout, session_id=session_id or str(node.get("id") or ""),
                      permission_mode=permission_mode)
    text = res["text"]
    notes: list = []
    if str(act) == "leaf":
        try:
            obj = json.loads((text or "").strip())
        except Exception:  # noqa: BLE001
            obj = None
        if isinstance(obj, dict):
            normed, notes = normalize_leaf_output(obj, knobs)
            if notes:
                text = json.dumps(normed, ensure_ascii=False)
    res["normalize_notes"] = notes
    # 元数据落到节点（_cc_* 是驱动器侧元数据口径，与 _run_dir/_last_reject 同款）
    # 与 .meta.json（run_cc_node 已写），供证据门与演示取用。
    try:
        node["_cc_tool_calls"] = res["tool_calls"]
        node["_cc_usage"] = {"input_tokens": res["input_tokens"],
                             "output_tokens": res["output_tokens"]}
        node["_cc_normalize_notes"] = notes
        node["_cc_meta"] = res["meta_path"]
    except Exception:  # noqa: BLE001
        pass
    return text


# Driver 识别 CC runner 的标记（stateless prompt 用；见 harness/driver.build_prompt）。
cc_act_runner.is_cc = True
