"""叶子证据门（t-d301b69799d2 续跑，2026-11-05）。

根因（旧 run e2e-20261105-final-88c2ad4 实测）：
  全部叶子都以 `pi -p --no-tools` 起（harness/session.py 模板），system prompt 里
  `<tools>(none)`；调研叶 prompt 却要求“读现有系统找事实、哪条命令/哪个文件/
  第几行”。模型无工具可用，只能凭权重填形状-valid 的断言；验收只查 JSON 形状
  与字数 ⇒ 断言被当成研究报告入库，merge 聚合，审计（同样 --no-tools）判 0 发现。
  loop-breaker / 失败分类 / rescue / 父自愈全程静默——不是它们失灵，而是这一层
  失败对它们不可见（叶子 exit_class=完成）。

本门（最小修改，只加不改既有路径）：
  ① 作用域：仅 role==调研、且节点 context/任务文本命中现场调查标记的叶子；
     通用叶、审计叶、终审叶、执行叶一律不触发（不给所有叶子加死板工具门槛）。
  ② 工具证据：该 session 必须含 ≥1 条成功工具结果；零调用 ⇒ 失败。
     合法重复查询/轮询不扣分（只计成功条数，不罚重复）。
  ③ 引用核查：reply（+doc）里引用的绝对路径与含目录的相对文件引用必须存在；
     邻近“第 N 行”时文件行数必须 ≥N。查不到 ⇒ 失败。
  失败路由（复用既有机制，不新造状态）：
  调用方把失败记为 exit_class=形状不合（五值集不扩）⇒ run_leaf 不入库 reply、
  节点落失败、judge_why 带结构化错误 ⇒ 父 merge 的 batch_view 能看到该错误，
  必须把对应主题留 uncovered（merge 校验强制显式 uncovered）或再派/切分重规划，
  绝不静默写成成功。
"""
from __future__ import annotations

import json
import os
import re

# 任务文本命中任一 ⇒ 该调研叶声称“现场调查”，门触发。注意：只查节点自身的
# context/任务字段，不查拼好的 prompt（prompt 里有 role 方法 boilerplate，
# 查它会把全部调研叶都误触发）。
INVESTIGATION_MARKERS = (
    "调查", "核查", "实测", "本机", "现场", "日志", "是否真的", "核实", "测量", "可验证",
)

_ABS_RE = re.compile(r"(?<![\w./:])(/(?:Users|tmp|private|var|etc|opt|System|Library)[\w.\-/]*)")
_REL_RE = re.compile(r"(?<![\w./-])([\w.\-]+(?:/[\w.\-]+)+\.(?:md|json|jsonl|log|txt))\b")
_LINE_RE = re.compile(r"第\s*(\d+)\s*(?:[-~～至]\s*(\d+)\s*)?行")
_TRAILING = "，。；、：）」』\"'"

_TOOLCALL_TYPES = ("toolcall", "tool_call", "function_call", "tool_use", "tool")


def task_text(node: dict) -> str:
    parts = []
    for key in ("context", "任务", "题目", "task", "验收标准"):
        val = (node or {}).get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
    return "\n".join(parts)


def needs_field_evidence(node: dict) -> tuple:
    """@returns (触发与否, 命中的标记列表)。仅调研 role 才可能触发。"""
    if str((node or {}).get("role") or "") != "调研":
        return False, []
    text = task_text(node)
    hits = [m for m in INVESTIGATION_MARKERS if m in text]
    return bool(hits), hits


def _is_error_text(text: str) -> bool:
    low = (text or "").strip().lower()
    if low.startswith(("error", "错误", "失败", "failed", "traceback")):
        return True
    return (("enoent" in low) or ("no such file" in low) or ("not found" in low)
            or ("no such field" in low) or ("invalid predicate" in low)
            or ("command not found" in low) or ("permission denied" in low)
            or ("operation not permitted" in low)
            or ("no such " in low) or ("not a valid" in low))


def count_tool_results(session_path: str) -> dict:
    """数 session JSONL 里的工具证据。

    实测形状（2026-11-05 probe-c1，pi --tools read,bash,grep,find,ls）：
      assistant content 块 {"type": "toolCall", "name": ..., "arguments": {...}}
      工具结果行 message.role == "toolResult"（不是 "tool"），content 为结果块。
    调用次数按 toolCall 块计；成功/失败按 toolResult 行计（ENOENT/不存在=失败，
    不是证据）；合法重复查询只计成功条数，不罚重复。
    @returns {calls, successes, failures, details[]}。
    """
    calls, successes, failures, details = 0, 0, 0, []
    if not session_path or not os.path.isfile(session_path):
        return {"calls": 0, "successes": 0, "failures": 0, "details": ["会话文件未找到"]}
    with open(session_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            role = str(msg.get("role") or "")
            content = msg.get("content")
            if role in ("tool", "toolResult"):
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        str(b.get("text") or "") for b in content if isinstance(b, dict))
                else:
                    text = json.dumps(content, ensure_ascii=False)
                if _is_error_text(text):
                    failures += 1
                else:
                    successes += 1
                    details.append("tool 成功：%s" % text[:120])
            elif role == "assistant" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = str(block.get("type") or "").lower()
                    if btype in _TOOLCALL_TYPES or "tool" in btype:
                        calls += 1
                        details.append("toolcall：%s" % json.dumps(block, ensure_ascii=False)[:120])
    return {"calls": calls, "successes": successes, "failures": failures, "details": details}


_WRITE_RES = (
    (r"(^|\s)(>>?|2>>?)\s*\S", "输出重定向"),
    (r"\btee\b", "tee 写文件"),
    (r"\bmkdir\b", "mkdir"),
    (r"\bcp\b", "cp"),
    (r"\bmv\b", "mv"),
    (r"\brm\b", "rm"),
    (r"\btouch\b", "touch"),
    (r"\bchmod\b", "chmod"),
    (r"\bsed\b[^|]*-i\b", "sed -i 原地改"),
    (r"\bcat\b[^|]*>", "cat 写文件"),
    (r"\becho\b[^|]*>", "echo 写文件"),
    (r"\bprintf\b[^|]*>", "printf 写文件"),
    (r"\bcurl\b.*\s-o\b", "curl -o 写文件"),
    (r"\bwget\b.*\s-O\b", "wget -O 写文件"),
)


def _bash_command(block: dict) -> str:
    args = block.get("arguments")
    if isinstance(args, dict):
        for k in ("command", "cmd", "input"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return v
        return json.dumps(args, ensure_ascii=False)
    if isinstance(args, str):
        return args
    return ""


def find_write_attempts(session_path: str) -> list:
    """扫描 session 里 bash 工具调用的写入尝试（F2：may_write=false 叶的 bash
    绕过只读契约）。只认 toolCall 块里 name==bash 的命令文本；读命令不命中。
    @returns [{call, command, pattern}]。"""
    import re as _re
    out = []
    if not session_path or not os.path.isfile(session_path):
        return out
    with open(session_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if str(msg.get("role") or "") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if str(block.get("type") or "").lower() not in _TOOLCALL_TYPES and "tool" not in str(block.get("type") or "").lower():
                    continue
                if str(block.get("name") or "") != "bash":
                    continue
                cmd = _bash_command(block)
                for pat, label in _WRITE_RES:
                    if _re.search(pat, cmd, _re.IGNORECASE):
                        out.append({"call": "bash", "command": cmd[:200], "pattern": label})
                        break
    return out


def _clean_path(p: str) -> str:
    return p.rstrip(_TRAILING).rstrip("/")


_ASCII_PATH = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-/")


def _rescue_relative_cite(p: str, roots: list) -> str:
    """相对引用的粘连抢救（T-FIX-TREE 续修，t-b0424b0f1592 实测）。

    根因：_REL_RE 的字符类是 Unicode \\w，会把中文散文粘进路径——
    模型写 "doc文档已生成至blocks/solution_documentation.md" 时，正则从 "doc"
    起 match，把整句 "doc文档已生成至blocks/solution_documentation.md" 当成
    一个相对路径；该文件不存在 ⇒ 证据门误拒，而真正的 blocks/... 文件存在。
    抢救（确定性，只认磁盘存在性）：沿引用串找"段首"（ASCII 路径字符、且前一
    字符不是 ASCII 路径字符——即中文散文与路径的交界），逐个取后缀，
    第一个在 roots 下存在且含 "/" 的后缀即为真实引用；都没有 ⇒ ""。
    @returns: 抢救到的真实相对路径，或 ""。
    """
    for i, ch in enumerate(p):
        if ch not in _ASCII_PATH or ch == "/":
            continue
        if i > 0 and p[i - 1] in _ASCII_PATH:
            continue
        cand = p[i:]
        if "/" not in cand:
            continue
        for root in (roots or []):
            if root and os.path.isfile(os.path.join(root, cand)):
                return cand
    return ""


def _rescue_by_suffix(p: str, roots: list) -> str:
    """相对引用的后缀唯一匹配抢救（T-FIX-TREE 续修，t-b0424b0f1592 run B 实测）。

    根因：审计叶收到的输入证据是绝对路径
    /tmp/.../pi-agent/sessions/2026-11-10T13-38-11-380Z_tree-...-root.c2.1.jsonl，
    模型引用时缩写成 sessions/2026-11-10T13-38-11-380Z_tree-...-root.c2.1.jsonl
    （丢了 pi-agent/ 前缀）；run 根下没有 sessions/ 目录 ⇒ 证据门误拒，
    而真实文件就在 pi-agent/sessions/ 下，所指别无歧义。
    抢救（确定性，只认磁盘）：在 roots 下找相对路径**以 "/" + p 结尾**的文件；
    恰好 1 个 ⇒ 该文件就是所指（返回其 run 相对路径）；0 个或 ≥2 个 ⇒ ""。
    后缀相等是强约束——"sessions/answer.txt" 不会误匹配 "product/answer.txt"，
    歧义（两个同后缀文件）时宁可失败，不猜。
    """
    hits = []
    for root in (roots or []):
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    rel = os.path.relpath(full, root)
                except ValueError:
                    continue
                if rel == p or rel.endswith("/" + p):
                    hits.append(rel)
                    if len(hits) > 1:
                        return ""
    return hits[0] if len(hits) == 1 else ""


def _rescue_absolute_cite(p: str) -> str:
    """绝对引用的尾部粘连抢救：正则的 \\w* 会吞掉路径后的中文（如 "/tmp/x.md文件"）。
    从尾部逐字剥掉非 ASCII 路径字符，第一个存在的文件即为真实引用；都没有 ⇒ ""。
    """
    q = p
    while q and q[-1] not in _ASCII_PATH:
        q = q[:-1]
    if q and os.path.isfile(os.path.expanduser(q)):
        return q
    return ""


def _rescue_ellipsis_cite(p: str, roots: list) -> str:
    """含 "..." 的缩写引用抢救（第四轮，integ3-q01/integ3-agg2 实测）。

    根因：模型把长绝对路径缩写成 "/tmp/integ-accept3/.../root.c4.1.jsonl"，
    正则把 "..." 当路径字符吞进引用串，字面路径不存在 ⇒ 证据门误拒，
    而真实文件就在 run 内、所指别无歧义。
    抢救（确定性，只认磁盘）：取 "..." 后的尾部，在 roots 下按后缀唯一匹配；
    尾部可能是"路径后缀"（如 "sessions/x.jsonl"）或"文件名后缀"
    （如 "root.c4.1.jsonl"——session 文件名是"<时间戳>_tree-<run>-<节点>.jsonl"，
    尾部对到文件名里 "-" 后的节点段，须对到分隔符边界，不吞半个词）。
    尾部太短/歧义/无匹配 ⇒ ""。
    @returns: 抢救到的文件绝对路径，或 ""。
    """
    if "..." not in p:
        return ""
    tail = p.split("...")[-1].strip().lstrip("/")
    if not tail or ("." not in tail and "/" not in tail):
        return ""
    hits = []
    for root in (roots or []):
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    rel = os.path.relpath(full, root)
                except ValueError:
                    continue
                if rel == tail or rel.endswith("/" + tail):
                    hits.append(full)
                elif os.path.basename(rel).endswith(tail):
                    base = os.path.basename(rel)
                    idx = len(base) - len(tail)
                    if idx > 0 and base[idx - 1] in ("-", "_"):
                        hits.append(full)
                if len(hits) > 1:
                    return ""
    return hits[0] if len(hits) == 1 else ""


# 工具名（pi-agent 工具集 + 模型常用 paraphrase）：模型把工具调用写成路径首段时
# （如 "write_file/answer.txt"），首段是工具名、不是目录。只认这些，其余一律不去。
_TOOL_NAME_SEGMENTS = ("write_file", "write_text", "read_file", "write", "read",
                       "bash", "edit", "patch", "glob", "grep", "find", "ls", "list", "tree")


def _rescue_by_suffix_drop(p: str, roots: list) -> str:
    """相对引用的工具名前缀抢救（第四轮，integ3-q01 实测 "write_file/answer.txt"）。

    根因：模型把工具名写成路径首段（write_file 是工具调用，不是目录），
    字面相对路径不存在 ⇒ 证据门误拒，而所指文件（product/answer.txt）在 run 内唯一。
    抢救（确定性，只认磁盘）：只去首段、且首段须是已知工具名——任意错目录
    （如 "sessions/answer.txt"，见 cases-task-completion 回归）不救援，仍记坏引用；
    余下部分按后缀唯一匹配（复用 _rescue_by_suffix 的歧义即拒口径）。
    @returns: 抢救到的 run 相对路径，或 ""。
    """
    parts = [s for s in str(p or "").split("/") if s]
    if len(parts) < 2 or parts[0] not in _TOOL_NAME_SEGMENTS:
        return ""
    return _rescue_by_suffix("/".join(parts[1:]), roots)


def check_citations(text: str, roots: list) -> list:
    """@returns 坏引用列表 [{cite, reason}]。只查“声称存在”的引用：绝对路径与含目录的
    相对文件引用；行号引用只验行数下限（不验片段措辞，措辞是语义，机械检不了）。"""
    bad = []
    if not text:
        return bad
    seen = set()
    for m in _ABS_RE.finditer(text):
        p = _clean_path(m.group(1))
        if p in seen or p.startswith(("http://", "https://")):
            continue
        seen.add(p)
        if not os.path.isfile(os.path.expanduser(p)):
            # The exact known run root may be mentioned as a location, not a file.
            run_root = roots[0] if roots else ""
            candidate = os.path.realpath(os.path.expanduser(p))
            known_run_root = (os.path.realpath(os.path.expanduser(str(run_root)))
                              if run_root else "")
            if (known_run_root and os.path.isdir(known_run_root)
                    and candidate == known_run_root
                    and not _LINE_RE.search(text[m.end():m.end() + 40])):
                continue
            _rescued = _rescue_absolute_cite(p)
            if not _rescued and "..." in p:
                # 第四轮：缩写引用——"..." 是缩写标记，不是字面路径；
                # 尾部能唯一对应磁盘文件时按该文件核查，否则不把缩写当字面断言。
                _rescued = _rescue_ellipsis_cite(p, roots)
            if _rescued:
                p = _rescued
            elif "..." in p:
                continue  # 缩写引用：无字面断言可核，不计入坏引用（其余引用照常核查）
            else:
                bad.append({"cite": p, "reason": "不存在"})
                continue
        window = text[m.end():m.end() + 40]
        lm = _LINE_RE.search(window)
        if lm:
            need = int(lm.group(2) or lm.group(1))
            try:
                with open(os.path.expanduser(p), "r", encoding="utf-8", errors="replace") as fh:
                    nlines = sum(1 for _ in fh)
            except OSError:
                nlines = 0
            if nlines < need:
                bad.append({"cite": p, "reason": "第 %d 行超出文件行数 %d" % (need, nlines)})
    for m in _REL_RE.finditer(text):
        p = _clean_path(m.group(1))
        if p in seen or p.startswith(("http://", "https://")):
            continue
        seen.add(p)
        found = None
        for root in roots:
            if root and os.path.isfile(os.path.join(root, p)):
                found = os.path.join(root, p)
                break
        if not found:
            # 粘连抢救：正则把中文散文粘进路径时，取真实存在的后缀作引用。
            _rescued = _rescue_relative_cite(p, roots)
            if _rescued:
                p = _rescued
                for root in roots:
                    if root and os.path.isfile(os.path.join(root, p)):
                        found = os.path.join(root, p)
                        break
            if not found:
                # 后缀唯一匹配抢救：模型把绝对路径缩写成相对后缀时，
                # 真实文件以该后缀结尾且全 run 唯一 ⇒ 就是所指。
                _suf = _rescue_by_suffix(p, roots)
                if _suf:
                    p = _suf
                    for root in roots:
                        if root and os.path.isfile(os.path.join(root, p)):
                            found = os.path.join(root, p)
                            break
            if not found:
                # 第四轮：工具名伪路径（"write_file/answer.txt"）——首段是工具名时才去首段
                # 再按后缀唯一匹配；任意错目录仍记坏引用（见 cases-task-completion 回归）。
                _suf = _rescue_by_suffix_drop(p, roots)
                if _suf:
                    p = _suf
                    for root in roots:
                        if root and os.path.isfile(os.path.join(root, p)):
                            found = os.path.join(root, p)
                            break
            if not found and "..." in p:
                # 第四轮：相对引用里的 "..." 同样是缩写标记，不是字面路径；
                # 尾部无法唯一对应时不把缩写当字面断言（与绝对引用同口径）。
                continue
            if not found:
                bad.append({"cite": p, "reason": "相对引用在 run_dir/树根下均不存在"})
                continue
        window = text[m.end():m.end() + 40]
        lm = _LINE_RE.search(window)
        if lm:
            need = int(lm.group(2) or lm.group(1))
            try:
                with open(found, "r", encoding="utf-8", errors="replace") as fh:
                    nlines = sum(1 for _ in fh)
            except OSError:
                nlines = 0
            if nlines < need:
                bad.append({"cite": p, "reason": "第 %d 行超出文件行数 %d" % (need, nlines)})
    return bad


def seen_citations(text: str) -> list:
    out = []
    if not text:
        return out
    for m in _ABS_RE.finditer(text):
        out.append(_clean_path(m.group(1)))
    for m in _REL_RE.finditer(text):
        out.append(_clean_path(m.group(1)))
    return out


def _session_mentions_evidence(session_path: str, input_evidence_ids: list) -> list:
    """会话的 assistant 侧是否提到输入证据 ID（工具调用参数 / 正文）。

    只查 assistant 侧：prompt（user/system）里可能原文含该 ID，查它会误判；
    assistant 工具调用参数或正文里出现 ID ⇒ 修复叶确实以该证据为依据。
    @returns: 命中的 ID 列表（去重保序）。会话文件缺失/无命中 ⇒ []。
    """
    ids = [str(x) for x in (input_evidence_ids or []) if str(x).strip()]
    if not ids or not session_path or not os.path.isfile(session_path):
        return []
    hit = []
    try:
        with open(session_path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
                if str(msg.get("role") or "") != "assistant":
                    continue
                content = msg.get("content")
                hay = ""
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if str(block.get("type") or "") == "text":
                            parts.append(str(block.get("text") or ""))
                        else:
                            parts.append(json.dumps(block, ensure_ascii=False))
                    hay = "\n".join(parts)
                elif isinstance(content, str):
                    hay = content
                for i in ids:
                    if i and i in hay and i not in hit:
                        hit.append(i)
                if len(hit) == len(ids):
                    break
    except OSError:
        return []
    return hit


def check_context_citations(text: str, input_evidence_ids: list, roots: list,
                            session_evidence_hits: list = None) -> list:
    """context-only 叶的引用核查：引用必须存在（复用 check_citations），且 reply
    必须引用至少一个输入证据 ID（节点 id / partial 路径等子串）。
    T-FIX-TREE 续修：证据 ID 也可在未决条目或会话 assistant 侧（工具调用参数/
    正文）出现——调用方把这两处拼进 text 或传 session_evidence_hits；
    三处全无 ⇒ 仍失败（fix.1 式零引用仍被拒，不放水）。
    @returns: 问题列表（空 = 通过）。"""
    problems = []
    bad = check_citations(text or "", roots or [])
    for b in bad:
        problems.append("无法核查的引用：%s（%s）" % (b["cite"], b["reason"]))
    ids = [str(x) for x in (input_evidence_ids or []) if str(x).strip()]
    if ids and not any(i in (text or "") for i in ids) and not session_evidence_hits:
        problems.append("未引用任何输入证据：reply/未决/会话里没有出现输入证据 ID（%s），"
                        "context-only 修复/综合只能引用输入中存在的证据" % "、".join(ids[:6]))
    return problems


def evidence_gate(node: dict, obj: dict, session_path: str, run_dir: str, tree_root: str,
                may_write=None, node_kind=None, input_evidence_ids=None,
                cc_tool_calls=None) -> tuple:
    """@returns (ok, detail)。不触发时 ok=True 且 detail 说明原因。

    分流（t-64e27d836932 类型化契约，替代旧的中文关键词扫描）：
      research → 旧行为：必须 ≥1 成功工具结果 + 引用可核查。
      contract_repair / synthesis / audit → context-only：不要求新工具调用，
        但引用必须可核查；repair/audit 还必须引用至少一个输入证据 ID。
        —— 这正是 t97 fix.1 被误拒的根因：它的 auth 禁调工具，旧门却按
        “调查”关键词要求工具调用。
    node_kind=None 时回落旧行为（role==调研 + 调查标记扫描），保持向后兼容。
    """
    kind = node_kind
    if kind is None:
        from harness import contract as _ct
        kind = _ct.classify(node)
    if kind in ("contract_repair", "synthesis", "audit"):
        problems = []
        if may_write is False:
            for att in find_write_attempts(session_path):
                problems.append("只读违规：may_write=false 的叶用 bash 尝试写入（%s：%s）"
                                % (att["pattern"], att["command"]))
        text = ""
        if isinstance(obj, dict):
            text = str(obj.get("reply") or "") + "\n" + str(obj.get("doc") or "")
            # T-FIX-TREE 续修：引用面拓宽到未决条目（what/why）+ 会话 assistant 侧。
            for _u in (obj.get("未决") or []):
                if isinstance(_u, dict):
                    text += "\n" + str(_u.get("what") or "") + "\n" + str(_u.get("why") or "")
        _sess_hits = (_session_mentions_evidence(session_path, input_evidence_ids)
                      if kind == "contract_repair" else [])
        problems += check_context_citations(
            text, input_evidence_ids if kind in ("contract_repair", "audit") else None,
            [run_dir, tree_root], session_evidence_hits=_sess_hits)
        if problems and kind == "audit":
            # 2026-11-13：审计叶的产出是「发现」，不是「现场调查」。模型常把
            # input/prime-oracle.json 简写成 oracle/answer.txt；只要 reply 里还有
            # 至少一处可核查的真实引用，就不让简写把整条审计判死（记告警）。
            _valid = []
            for _c in seen_citations(text):
                for _root in (run_dir, tree_root):
                    if _root and os.path.isfile(os.path.join(_root, _c)):
                        _valid.append(_c)
                        break
            if _valid:
                return True, ("证据门通过（audit，%d 处引用可核查；忽略简写：%s）"
                              % (len(_valid), "；".join(problems)))
        if problems:
            return False, "；".join(problems)
        return True, ("证据门通过（context-only，kind=%s）：引用 %d 处均可核查，"
                      "不要求新工具调用" % (kind, len(seen_citations(text))))
    triggered, hits = needs_field_evidence(node)
    if not triggered:
        return True, "非现场调查叶（role=%s，未命中调查标记），证据门不触发" % (
            str((node or {}).get("role") or ""))
    tools = count_tool_results(session_path)
    if tools["successes"] < 1 and cc_tool_calls:
        # V1 CC 化：无 pi 会话文件时，用 CC runner 记录的工具调用充当工具证据
        # （调用已过 Bash 白名单与次数上限核验，进程正常返回才到得了这里；
        # 调用记录只含 name/input，不含工具结果正文，见报告口径说明）。
        tools = {"calls": len(cc_tool_calls), "successes": len(cc_tool_calls),
                 "failures": 0,
                 "details": ["CC 工具调用：%s" % str((c or {}).get("name") or "?")
                             for c in cc_tool_calls]}
    problems = []
    if tools["successes"] < 1:
        problems.append("零成功工具结果：任务要求现场调查（命中标记：%s），但会话 %s 里成功工具结果 0 条"
                        "（工具调用 %d 次，失败 %d 次）" % (
                            "、".join(hits), os.path.basename(session_path or "<缺失>"),
                            tools["calls"], tools["failures"]))
    if may_write is False:
        # F2：may_write=false 的叶用 bash 写文件 ⇒ 拒收（写已发生也无用：
        # cwd 已钉在 run 内 + reply 不入库，写出的东西成不了被接受的证据）。
        for att in find_write_attempts(session_path):
            problems.append("只读违规：may_write=false 的叶用 bash 尝试写入（%s：%s）"
                            % (att["pattern"], att["command"]))
    text = ""
    if isinstance(obj, dict):
        text = str(obj.get("reply") or "") + "\n" + str(obj.get("doc") or "")
    roots = [run_dir, tree_root]
    bad = check_citations(text, roots)
    for b in bad:
        problems.append("无法核查的引用：%s（%s）" % (b["cite"], b["reason"]))
    if problems:
        return False, "；".join(problems)
    return True, ("证据门通过：成功工具结果 %d 条；引用 %d 处均可核查"
                  % (tools["successes"], len(seen_citations(text))))
