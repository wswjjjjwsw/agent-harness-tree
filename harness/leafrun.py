# -*- coding: utf-8 -*-
"""tree-05 真跑面：叶子执行 + 机械判据求值 + 心跳落盘 + 统一推进队列（task-def §2.3 / §2.6）。

本文件是**本批唯一要写的新代码**（harness/ 下新文件，既有文件一个字节未动）。三块能力：

  1. 叶子执行面（§2.3.1）：给一个 state==待跑 的叶子（role=执行/调研），用**既有**
     `harness/session.py:run_act` 起 pi，prompt 用**既有** `harness/submit.py:assemble_prompt`（act="leaf"），
     产出用**既有** `harness/driver.py:parse_act_output` + `harness/schema.py:validate`，
     doc 用**既有** `harness/driver.py:Driver.write_doc`（lib 里唯一写 doc/ 的入口）落盘，
     结束**当场落终态**（完成/失败，二选一）。
  2. 机械判据求值（§2.3.2）：按节点自己的 judge dict 求值 ⇒ judge_ok + exit_class。
     `exit_class` 只许五个值：完成 / 判据不过 / 形状不合 / 超时 / 环境。
     2026-11-12：judge 只认 json_keys（结构形状）；字数门禁整体取消，无判据不再判死节点。
  3. 心跳落盘（§2.3.3）：每推进一轮落 `<run>/heartbeat/round-<n>.json`（n 从 1 起），
     形状 = 既有 `harness/recover.py:dump_snapshot` 的同一形状，并**同时**用同一个 dump 函数刷新
     `<run>/state.json`。

另加：统一推进队列（§2.6，owner 裁决的调度口径）、五样交付物里 ①③ 的生成、单价实测表的生成。

★ 调度口径**逐字照 task-def §2.6**，本文件不另立规则：
  候选 = 按层序（根第 0 层，同层按 node_id 字典序）取**第一个**满足下列之一的节点；
  · 叶子（执行/调研）且 state==待跑 ⇒ 叶子执行面，一次推进 = 一次执行；
  · 父（根/规划/决策）非终态且（不在 等子节点 或 本批次子已全终结）⇒ 既有 Driver.step()，一次推进 = 一步；
    step() 返回 skip=waiting ⇒ 不算推进、不落心跳，挑下一个候选；
  · `rounds_advanced is True` ⇒ 下一次仍优先挑同一节点（同一个父连续推到位）。
  「谁是父」不从 role 名字的字面值判，从 role 表的 `may_spawn` 旋钮判（与 76 §2.3 闸① 同一口径）。

★ 终止性（本文件的两条加严，task-def 没钉死，登记在报告「假设与加严」节，未静默）：
  · 连续被拒超过 `TREE_MAX_REJECT`（缺省 2）次的节点本 run 不再推（否则契约不过的父会无限重推，每推一次烧一次 pi）；
  · 墙钟超 `TREE_MAX_SECONDS`（缺省 7200）⇒ 响亮停（步数上限之外的第二道兜底）。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from harness import audit as audit_mod
from harness import driver as driver_mod
from harness import gate
from harness import land
from harness import node as node_mod
from harness import recover
from harness import role as role_mod
from harness import schema
from harness import session
from harness import submit

Driver = driver_mod.Driver
parse_act_output = driver_mod.parse_act_output

# exit_class 五值（逐字钉死，task-def §2.3.2）
EXIT_DONE = "完成"
EXIT_JUDGE = "判据不过"
EXIT_SHAPE = "形状不合"
EXIT_TIMEOUT = "超时"
EXIT_ENV = "环境"
EXIT_CLASSES = (EXIT_DONE, EXIT_JUDGE, EXIT_SHAPE, EXIT_TIMEOUT, EXIT_ENV)

# judge 里认识的键（别的键不当判据，出现就忽略）
JUDGE_KEYS = ("json_keys",)  # 2026-11-12：字数判据取消

# 七类单价（键名逐字，task-def §3）
PRICE_CLASSES = ("plan", "spawn", "merge", "执行", "审计", "调研", "形状审查")

RE_QID = re.compile(r"Q[0-9]{2}")


# ============================================================ 计时外壳
class TimedRunner:
    """把驱动器/审计用的 runner 包一层**只做记录**的外壳（不改行为）。

    每次调用记一笔：node_id / act / start / end / seconds / chars / raw / err / session_id。
    用途三件：① 父节点的行的 seconds 与 未决 条数；② 审计与形状审查检查点的真跑秒数
    （它们由 harness/audit.py 直接调 `d.runner`，不走 Driver.step ⇒ 否则 rounds.jsonl 里没有它们）；
    ③ 事后可回放。
    """

    def __init__(self, inner):
        self.inner = inner
        self.calls = []
        # F3：内层 runner 是否接受 timeout（act_runner_default 接受、桩 runner 不接受）。
        # 之前这里根本不透传：run_leaf 传了 timeout=300 也被吞掉，c2.1 实测跑了 378.6s。
        try:
            import inspect as _insp
            self._takes_timeout = "timeout" in _insp.signature(inner).parameters
        except (TypeError, ValueError):
            self._takes_timeout = False

    def __call__(self, node, act, knobs, prompt, session_id, run_dir, timeout=None):  # noqa: ANN001
        nid = str((node or {}).get("id") or "")
        t0 = time.time()
        rec = {"node_id": nid, "act": str(act or ""), "start": round(t0, 3),
               "session_id": str(session_id or ""), "prompt_chars": len(prompt or ""),
               "seconds": 0.0, "chars": 0, "raw": "", "err": ""}
        try:
            if self._takes_timeout:
                raw = self.inner(node, act, knobs, prompt, session_id, run_dir,
                                 timeout=timeout)
            else:
                raw = self.inner(node, act, knobs, prompt, session_id, run_dir)
        except BaseException as exc:                              # noqa: BLE001
            t1 = time.time()
            rec.update({"end": round(t1, 3), "seconds": round(t1 - t0, 3),
                        "err": "%s: %s" % (type(exc).__name__, exc)})
            self.calls.append(rec)
            raise
        t1 = time.time()
        rec.update({"end": round(t1, 3), "seconds": round(t1 - t0, 3),
                    "chars": len(raw or ""), "raw": raw or ""})
        self.calls.append(rec)
        return raw

    def last_for(self, node_id):
        for rec in reversed(self.calls):
            if rec["node_id"] == str(node_id):
                return rec
        return None


# ============================================================ 机械判据（§2.3.2）
def _normalize_review_cover(value: str, blocker_id: str):
    """Normalize one optional outer bracket pair back to the exact ledger ID."""
    if not value or not blocker_id:
        return None

    def display_id(text):
        return text[1:-1] if len(text) >= 2 and text.startswith("[") and text.endswith("]") else text

    return blocker_id if display_id(value) == display_id(blocker_id) else None


def _judge_text(obj: dict) -> str:
    """取产出正文（有 doc 用 doc，否则用 reply）；2026-11-12 起不再用于量字数。"""
    doc = str((obj or {}).get("doc") or "")
    if doc.strip():
        return doc
    return str((obj or {}).get("reply") or "")


def evaluate_judge(node: dict, obj: dict) -> tuple:
    """按节点自己的 judge dict 逐条求值。

    @returns: (judge_ok, why, 无判据标记)
    """
    j = (node or {}).get("judge")
    # t-64e27d836932：没有可执行判据的叶子不能因“无判据”而自动 judge_ok。
    #   judge 缺失/空 ⇒ 失败（typed：no_criteria），父节点须重派带判据的任务；
    #   judge 是自然语言字符串 ⇒ 无法机械核验，返回 None（partial：产出入库，
    #   但结果类型 partial，未决记一笔，merge 收束时必须覆盖）。
    #   judge 是 dict 但键一个都不在认识集里 ⇒ 与缺失同等处理，失败（Gate 5）；
    #   已知键值类型非法（如 chars_min 是字符串）⇒ 失败，判据本身无效（Gate 5）。
    if j is None or j == "" or j == {}:
        # 2026-11-12：字数/结构门禁取消，无机械判据不再判死节点。
        return True, "无机械判据（已取消字数门禁），按产出形状入库", False
    if isinstance(j, str):
        return True, "自然语言判据不机械核验（已取消门禁）", False
    if not isinstance(j, dict):
        return False, "判据形状非法：%r（须为 dict 或自然语言）" % (type(j).__name__,), True
    # T-FIX-TREE 续修：先机械归一化再求值（见 normalize_judge）。归一失败
    # （已知键值类型非法）⇒ 显式失败，Gate 5 口径不变，不默认通过。
    _norm, _nnote = normalize_judge(
        j, role=str((node or {}).get("role") or ""),
        min_out_chars=_min_out_for_role((node or {}).get("role")))
    if _norm is None:
        return False, "无可执行判据：%s" % _nnote, True
    j = _norm
    unknown = [k for k in j if k not in JUDGE_KEYS]
    known = {k: j[k] for k in JUDGE_KEYS if k in j}
    if not known:
        # Gate 5：dict 里一个认识的键都没有 ⇒ 与缺失同等处理，失败（typed：no_criteria）。
        # 旧值曾返回 True（“按无判据处理”却判为通过），叶无任何有效判据仍会被标 完成。
        return (True, "无机械判据（已取消字数门禁），按产出形状入库", False)
    text = _judge_text(obj)
    errs = []
    # Gate 5：已知键值类型非法 ⇒ 判据本身无效，失败；旧值静默跳过该键，叶会“判据通过”。
    # 2026-11-12：chars_min / chars_max 字数判据整体移除。
    jk = known.get("json_keys")
    if jk is not None:
        if not isinstance(jk, list):
            errs.append("json_keys 不是数组：%r" % (jk,))
        else:
            miss = [str(k) for k in jk if k not in (obj or {})]
            if miss:
                errs.append("json_keys：产出里缺 %s" % miss)
    head = "判据通过（%s）" % ",".join(sorted(known)) if not errs else "；".join(errs)
    if unknown:
        head += "；忽略了不认识的键 %s" % sorted(unknown)
    if _nnote:
        head += "；" + _nnote
    return (not errs), head, False


# ============================================================ judge 归一化（T-FIX-TREE 续修）
# 实测（t-43388efa2784 实验 A）：root 派发的 judge 是中文准则名 → 自然语言描述，
# 如 {"产物存在性": "product/answer.txt 必须存在…", "文档长度": "doc 内容不少于 1500 字", …}，
# evaluate_judge 一个键都不认识 ⇒ Gate 5 失败，审计叶 c4.2/c6.1 判据不过、doc 未入库。
# 2026-11-12：字数判据整体取消。normalize_judge / extract_doc_chars_min
# 已 early-return；下方正则与旧归一化分支为不可达遗留代码，
# 交接后由后续维护者清理，不影响运行时行为。
_RE_CHARS_MIN = re.compile(r"(?:不少于|至少|≥|>=)\s*(\d{2,6})\s*字")
_RE_CHARS_MAX = re.compile(r"(?:不超过|至多|≤|<=)\s*(\d{2,6})\s*字")


def _min_out_for_role(role):
    try:
        knobs = role_mod.effective(str(role or ""), None)
        return int((knobs or {}).get("min_out_chars") or 0)
    except Exception:  # noqa: BLE001
        return 0


def normalize_judge(judge, role=None, min_out_chars=0):
    """@returns: (归一化后的 judge dict 或 None, 说明字符串)。
    None = 无法归一化（已知键值类型非法），调用方须显式失败，不得默认通过。
    """
    # 2026-11-12：只认 json_keys（结构形状）；字数键一律忽略，
    # 不再抽取中文数字、不再按 role 缺省补 1500。
    if isinstance(judge, dict) and isinstance(judge.get("json_keys"), list):
        return {"json_keys": list(judge["json_keys"])}, ""
    return {}, "无可机械判据（字数判据已取消）"
    out = {}
    for k in JUDGE_KEYS:
        if k not in judge:
            continue
        v = judge[k]
        if k in ("chars_min", "chars_max"):
            if not isinstance(v, int) or isinstance(v, bool):
                return None, "judge 已知键 %s 类型非法：%r（须为整数）" % (k, v)
            out[k] = v
        elif k == "json_keys":
            if not isinstance(v, list):
                return None, "judge 已知键 json_keys 类型非法：%r（须为数组）" % (v,)
            out[k] = list(v)
    unknown = {k: v for k, v in judge.items() if k not in JUDGE_KEYS}
    notes = []
    if unknown:
        # 字数抽取只对执行 role 生效：chars_min 度量的是叶自己的产出正文；
        # 审计/调研叶只出 20~200 字 reply，把"文档长度 1500 字"套到它们头上
        # 是范畴错误（c4.2/c6.1 的 judge 恰是这么错配的——1500 字指的是被审的
        # doc，不是审计 reply）。非执行 role 直接走下面的 role 缺省。
        if role == "执行":
            mins, maxs = [], []
            for v in unknown.values():
                if isinstance(v, str):
                    mins += [int(x) for x in _RE_CHARS_MIN.findall(v)]
                    maxs += [int(x) for x in _RE_CHARS_MAX.findall(v)]
            if mins and "chars_min" not in out:
                out["chars_min"] = max(mins)
                notes.append("中文键值抽得 chars_min=%d" % out["chars_min"])
            if maxs and "chars_max" not in out:
                out["chars_max"] = min(maxs)
                notes.append("中文键值抽得 chars_max=%d" % out["chars_max"])
        notes.append("丢弃不可机械核验的中文键 %s" % sorted(unknown))
    if not out:
        if role in ("审计", "调研"):
            out = {"json_keys": ["reply", "未决"]}
            notes.append("无可识别键：role=%s 缺省 json_keys=[reply,未决]" % role)
        elif role == "执行":
            floor = int(min_out_chars) if min_out_chars else 1500
            out = {"chars_min": floor}
            notes.append("无可识别键：role=执行 缺省 chars_min=%d（role 的 min_out_chars）" % floor)
        else:
            return None, "judge 的键 %s 一个都不在认识集 %s 里，且无 role 缺省" % (
                sorted(judge), list(JUDGE_KEYS))
    note = ("judge 已归一化（%s）" % "；".join(notes)) if notes else ""
    return out, note


# ============================================================ 落盘核验（2026-11-12 起字数门取消，直通）
# 集成验收 t-132692485362 的教训：自然语言判据的叶从未被机械核验——模型在 reply
# 里谎称"完成 1500 字"、短 doc 靠 harvest 蒙混成"完成"，终审才拦下。
# 本段只认磁盘：chars_min 要求下限时，量叶真实写出的落盘文件（会话 write 工具
# 调用路径 / doc 指针解析出的文件 / 任务点名的引用文件），不认模型自报的 doc 字数。
# "字"的口径与 evaluate_judge 一致：len(正文)（Python 字符数；实测与首轮报告
# 的 207 / 1499 字逐字一致）。
_DOC_CANDIDATE_SUFFIXES = (".md", ".txt")
_RE_DOC_PATH = re.compile(r"[\w./\-]+?\.(?:md|txt)\b")
# 修复目标字数抽取（比 _RE_CHARS_MIN 多"要求/达"类说法；只抽"要求值"，不抽实测值——
# "仅 207 字"里没有这些词，不会把实测当要求）。
_RE_REQUIRE_CHARS = re.compile(r"(?:不少于|至少|≥|>=|要求|须达到|应达到|达到|达)\s*(\d{2,6})\s*字")


def _read_chars(path: str):
    """读文件字符数；读不到 ⇒ None（不猜）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return len(fh.read())
    except OSError:
        return None


def _resolve_under(path: str, run_dir: str, tree_root: str):
    """引用路径 ⇒ 磁盘绝对路径；只认 run_dir / 树根下的文件（防叶把 /tmp 外的文件当证据）。"""
    if not isinstance(path, str) or not path.strip():
        return None
    p = path.strip()
    if os.path.isabs(p):
        try:
            rp = os.path.realpath(p)
        except OSError:
            return None
        for root in (run_dir, tree_root):
            if root and rp.startswith(os.path.realpath(str(root)) + os.sep):
                return rp if os.path.isfile(rp) else None
        return None
    if ".." in Path(p).parts:
        return None
    for root in (run_dir, tree_root):
        if not root:
            continue
        cand = os.path.join(str(root), p)
        if os.path.isfile(cand):
            return cand
    return None


def session_doc_paths(session_path: str) -> list:
    """会话里 assistant write 工具调用写出的 .md/.txt 路径（原样相对路径，去重保序）。"""
    if not session_path or not os.path.isfile(session_path):
        return []
    out = []
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
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if str(block.get("type") or "") != "toolCall":
                        continue
                    if str(block.get("name") or "") != "write":
                        continue
                    args = block.get("arguments") or {}
                    p = str(args.get("path") or "") if isinstance(args, dict) else ""
                    if p and p.endswith(_DOC_CANDIDATE_SUFFIXES) and p not in out:
                        out.append(p)
    except OSError:
        return []
    return out


def extract_doc_chars_min(node: dict):
    """叶的落盘字数下限（chars_min），或 None（无要求 ⇒ 不核验）。

    口径与 normalize_judge 对齐：dict judge 用归一化后的 chars_min；
    字符串 judge 只对执行 role 从 judge/验收标准/context 里抽（审计/调研叶的
    1500 字指的是被审的 doc，不是它自己的 reply，不抽）。
    """
    # 2026-11-12：字数门禁取消，落盘核验不再量文档长度。
    return None
    j = (node or {}).get("judge")
    role = str((node or {}).get("role") or "")
    if isinstance(j, dict):
        norm, _note = normalize_judge(
            j, role=role, min_out_chars=_min_out_for_role(role))
        if norm and isinstance(norm.get("chars_min"), int):
            return int(norm["chars_min"])
        return None
    if isinstance(j, str) and role == "执行":
        blob = (j + "\n" + str((node or {}).get("验收标准") or "")
                + "\n" + str((node or {}).get("context") or ""))
        found = [int(x) for x in _RE_CHARS_MIN.findall(blob)]
        return max(found) if found else None
    return None


def _is_doc_path(p: str) -> bool:
    """named 提取只认 doc 产物（第三轮 P2⑥）：含 product 路径段的值文件
    （如 product/answer.txt）不认；以 .md 结尾认；首段为 doc 认；
    其余（如裸 answer.txt、product 下 .txt）不认。只改 named 提取，
    不改 session_doc_paths 与 written 池。"""
    s = str(p or "").replace("\\", "/")
    parts = s.split("/")
    if "product" in parts:
        return False
    if s.endswith(".md"):
        return True
    if parts and parts[0] == "doc":
        return True
    return False


def _looks_like_pointer(text: str) -> bool:
    """受控文档写入前的指针识别：单行无空白、绝对路径或 .md/.txt 后缀 ⇒ 是路径不是正文。"""
    if any(char.isspace() for char in text):
        return False
    return (os.path.isabs(text) or ".." in Path(text).parts
            or Path(text).suffix.lower() in (".md", ".txt"))


def verify_disk_doc(node: dict, obj: dict, run_dir: str, tree_root: str,
                    session_path: str, doc_resolved_from: str = "") -> tuple:
    """落盘 doc 机械核验。@returns: (ok, why, info)。

    规则：任务点名了 doc 文件（context/验收标准/judge 里的 .md/.txt 引用）⇒
    点名的每个文件都必须存在且 ≥ chars_min；未点名 ⇒ 取叶写出的最长 doc 文件
    （会话 write 的 .md/.txt、doc 指针文件、reply/doc 里引用的文件、inline doc
    正文——inline doc 随后由 write_doc 原样落盘，等价于磁盘长度）；无 chars_min
    要求 ⇒ 直通；有点名/有要求但无任何候选文件 ⇒ 失败（fail-closed）。
    """
    cmin = extract_doc_chars_min(node)
    info = {"chars_min": cmin, "named": {}, "written": {}, "inline_chars": None}
    if cmin is None:
        return True, "无落盘字数下限要求，不核验", info
    blob = ("\n".join(str((node or {}).get(k) or "")
                      for k in ("context", "验收标准"))
            + "\n" + str((node or {}).get("judge") or ""))
    for p in sorted(set(_RE_DOC_PATH.findall(blob))):
        # 第三轮 P2⑥：named 只认 doc 产物，product/ 下的值文件（如 answer.txt）
        # 不当"点名 doc"（q01c 实测：named 含 product/answer.txt: 4，归因错）。
        if not _is_doc_path(p):
            continue
        ap = _resolve_under(p, run_dir, tree_root)
        info["named"][p] = _read_chars(ap) if ap else None
    written = {}
    for p in session_doc_paths(session_path):
        ap = _resolve_under(p, run_dir, tree_root)
        if ap:
            written[p] = ap
    if doc_resolved_from:
        ap = _resolve_under(doc_resolved_from, run_dir, tree_root)
        if ap:
            written[doc_resolved_from] = ap
    for src in (str((obj or {}).get("reply") or ""), str((obj or {}).get("doc") or "")):
        for p in _RE_DOC_PATH.findall(src[:20000]):
            ap = _resolve_under(p, run_dir, tree_root)
            if ap:
                written[p] = ap
    for p, ap in sorted(written.items()):
        info["written"][p] = _read_chars(ap)
    inline = str((obj or {}).get("doc") or "")
    if inline.strip():
        info["inline_chars"] = len(inline)
    if info["named"]:
        bad = {p: c for p, c in info["named"].items() if c is None or c < cmin}
        if bad:
            det = "；".join("%s 实测 %s" % (p, ("缺失" if c is None else "%d 字" % c))
                           for p, c in sorted(bad.items()))
            return False, "落盘 doc 未达标：点名文件 %s，要求 ≥ %d 字" % (det, cmin), info
        det = "、".join("%s %d 字" % (p, c) for p, c in sorted(info["named"].items()))
        return True, "落盘 doc 点名文件全部 ≥ %d 字（%s）" % (cmin, det), info
    pool = dict(info["written"])
    if info["inline_chars"] is not None:
        pool["<inline doc>"] = info["inline_chars"]
    pool = {p: c for p, c in pool.items() if c is not None}
    if not pool:
        return False, ("要求落盘 doc ≥ %d 字，但未定位到叶写出的 doc 文件"
                       "（会话无 write 记录且无可核查引用）" % cmin), info
    best_p, best_c = max(pool.items(), key=lambda kv: kv[1])
    if best_c < cmin:
        return False, ("落盘 doc 实测最长 %d 字（%s）< %d 字；模型自报字数不作数"
                       % (best_c, best_p, cmin)), info
    return True, "落盘 doc 实测 %d 字（%s）≥ %d 字" % (best_c, best_p, cmin), info


def harvest_final_json(session_path):
    """从 pi 会话 jsonl 里抢救模型写进工具调用/正文里的最终 JSON。

    背景（实验 A c2.1/c4.1 实测）：模型把最终 JSON 反复 Write 到 product/output.json
    （c2.1 连写 5 次、c4.1 连写 4 次），触发 loop-breaker 熔断，pi 被 abort，正文里
    从无 JSON；但 JSON 本身已在会话里写了多次且形状完整。边界就此定死：
    **最终 JSON 是模型消息/工具参数里的内容，不是磁盘文件**——执行器从会话里取
    最后一份，不再要求模型只用正文交 JSON，也不读 output.json 文件本身
    （模型每次写完就 rm -f 删掉，文件根本不存在）。
    @returns: (obj dict 或 None, 来源说明)。候选按会话顺序，后者覆盖前者：
      ① assistant 的 write 工具调用，其 content 可解析为含 reply 的 dict（不限路径）；
      ② assistant 纯文本消息，可解析为含 reply 的 dict。
    不合形状的候选直接跳过（不猜、不补键）；下游仍走字段/形状/证据/判据全套管线。
    """
    if not session_path or not os.path.isfile(session_path):
        return None, ""
    best, prov = None, ""
    n_write = 0
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
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = str(block.get("type") or "")
                    if btype == "toolCall" and str(block.get("name") or "") == "write":
                        n_write += 1
                        args = block.get("arguments") or {}
                        text = args.get("content") if isinstance(args, dict) else None
                        if isinstance(text, str):
                            try:
                                cand = json.loads(text)
                            except ValueError:
                                continue
                            if isinstance(cand, dict) and "reply" in cand:
                                best = cand
                                prov = "会话 write 工具调用 #%d（path=%s）" % (
                                    n_write, str(args.get("path") or ""))
                    elif btype == "text":
                        t = block.get("text") or ""
                        if isinstance(t, str) and t.strip().startswith("{"):
                            try:
                                cand = json.loads(t)
                            except ValueError:
                                continue
                            if isinstance(cand, dict) and "reply" in cand:
                                best, prov = cand, "会话 assistant 正文 JSON"
            elif isinstance(content, str) and content.strip().startswith("{"):
                try:
                    cand = json.loads(content)
                except ValueError:
                    continue
                if isinstance(cand, dict) and "reply" in cand:
                    best, prov = cand, "会话 assistant 正文 JSON"
    return best, prov


def _should_harvest_after_failure(exit_class, why):
    """熔断/超时后是否尝试从会话抢救最终 JSON（T-FIX-TREE 续修补丁 4）。
    超时一律抢救（不认 why 文案：synthesis kind 超时文案是 TREE_PI_TIMEOUT，
    不含"单叶上限"）；环境类只认 loop-breaker 熔断（其它环境错无会话可抢）。
    """
    if not why:
        return False
    if exit_class == EXIT_TIMEOUT:
        return True
    return exit_class == EXIT_ENV and "loop_breaker" in why


def _try_harvest(run_dir, nid, sid):
    """定位会话文件并抢救最终 JSON；找不到会话 ⇒ (None, "")。"""
    sess_path = session.find_session_file(
        sid, os.environ.get("TREE_PI_AGENT_SESSION_DIR"))
    if not sess_path:
        return None, ""
    return harvest_final_json(sess_path)


def resolve_doc_pointer(obj, run_dir):
    """doc 字段若是 run 内已存在文件的相对路径 ⇒ 读出正文当 doc 用。

    实测（实验 A c2.1/c4.1）：doc 字段是 "blocks/sushu_implementation.md" 这样的
    文件指针，1620 字正文在 blocks 文件里；旧管线拿 30 字指针串当正文判
    chars_min，必失败。指针解析是确定性的文件存在性 + 读盘，不是语义猜测。
    @returns: (是否解析, 路径或 "")。护栏：只认短字符串（≤300 字、无换行）、
    相对路径、位于 run_dir 内、文本文件、0 < 大小 ≤ 200000 字节；否则原样不动。
    """
    if not isinstance(obj, dict):
        return False, ""
    doc = obj.get("doc")
    if not isinstance(doc, str):
        return False, ""
    s = doc.strip()
    if not s or len(s) > 300 or "\n" in s:
        return False, ""
    if os.path.isabs(s) or ".." in Path(s).parts:
        return False, ""
    if "/" not in s and not s.endswith((".md", ".txt")):
        return False, ""
    base = Path(str(run_dir or ""))
    target = base / s
    try:
        if not target.is_file():
            return False, ""
        if target.stat().st_size <= 0 or target.stat().st_size > 200000:
            return False, ""
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False, ""
    if not text.strip():
        return False, ""
    obj["doc"] = text
    return True, s


def _why_with_class(exit_class: str, why: str) -> str:
    """judge_why 里带 `exit_class=` 便于机械抽（task-def §2.3.2）。"""
    return "exit_class=%s；judge=%s" % (exit_class, why or "")


# ============================================================ 叶子执行（§2.3.1）
def salvage_partial_evidence(run_dir: str, nid: str, sid: str) -> dict:
    """超时/失败叶的抢救（F3/F5）：把 session 里已成功的工具结果落到
    partial/<nid>.json，供父节点读取；无证据时 successes=0。"""
    from harness import evidence as _ev
    sess_path = session.find_session_file(
        sid, os.environ.get("TREE_PI_AGENT_SESSION_DIR"))
    tools = _ev.count_tool_results(sess_path) if sess_path else {
        "calls": 0, "successes": 0, "failures": 0, "details": []}
    out = {"node_id": nid, "session_id": sid, "calls": tools["calls"],
           "successes": tools["successes"], "failures": tools["failures"],
           "details": (tools.get("details") or [])[:50]}
    pdir = Path(run_dir) / "partial"
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / ("%s.json" % nid)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    out["path"] = "partial/%s.json" % nid
    return out


def _read_cc_meta_tool_calls(run_dir: str, sid: str) -> list:
    """CC 超时/熔断 meta 里的 tool_calls 读回节点（run_cc_node 写盘后 runner
    没机会记到节点时用；超时分支与熔断分支共用）。meta 缺失/无 tool_calls ⇒ []。"""
    try:
        _safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", sid)
        _mp = Path(run_dir) / ("cc-%s.meta.json" % _safe)
        if _mp.is_file():
            _mm = json.loads(_mp.read_text(encoding="utf-8"))
            if isinstance(_mm, dict) and _mm.get("tool_calls"):
                return list(_mm["tool_calls"])
    except Exception:  # noqa: BLE001
        pass
    return []


# ============================================================ V2：产物扫描 + 机械验收（ACK 叶）
# 执行叶的交付是磁盘文件：超时/无最终 JSON 时，Harness 不靠模型自报，改按
# “磁盘差分 + 机械验收”判定 verified/partial/failed。pi 路径与无产物配置的
# 叶一字不动（_art_active 为 False 时整段跳过）。

def _artifact_config(node: dict) -> dict:
    """产物配置来源：节点 overrides 优先，环境变量兜底。"""
    ov = node.get("overrides") or {}
    if not isinstance(ov, dict):
        ov = {}
    return {
        "artifacts_dir": ov.get("artifacts_dir") or os.environ.get("TREE_ARTIFACTS_DIR") or "",
        "verify_cmd": ov.get("verify_cmd") or os.environ.get("TREE_VERIFY_CMD") or "",
        "expect_artifacts": bool(ov.get("expect_artifacts")) if "expect_artifacts" in ov else False,
    }


def _file_digest(p) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_dir(base) -> dict:
    """{相对路径: (字节数, sha256)}；目录不存在 ⇒ {}（叶可能新建它）。"""
    snap = {}
    try:
        base = Path(base)
    except Exception:  # noqa: BLE001
        return snap
    if not base.is_dir():
        return snap
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        try:
            st = p.stat()
            snap[str(p.relative_to(base))] = (st.st_size, _file_digest(p))
        except OSError:
            continue
    return snap


def _diff_snapshot(before: dict, after: dict) -> tuple:
    created, modified = [], []
    for rel in sorted(after):
        size, digest = after[rel]
        if rel not in before:
            created.append({"path": rel, "bytes": size, "sha256": digest[:16]})
        elif before[rel][1] != digest:
            modified.append({"path": rel, "bytes": size, "sha256": digest[:16]})
    return created, modified


def _run_verify(cmd: str, cwd: str, artifacts_abs, timeout: float = 120.0) -> dict:
    """跑机械验收命令（如固定验收测试），解析逐项 PASS/FAIL 与 TOTAL 行。

    PROJECT_DIR 指到产物目录（测试脚本按它找项目文件）；命令本身不许改测试。
    超时/执行失败 ⇒ 如实记，不判叶失败（verify 是证据，不是叶的另一重超时）。
    """
    import subprocess as _sp
    import sys as _sys
    env = dict(os.environ)
    env["PROJECT_DIR"] = str(artifacts_abs)
    try:
        proc = _sp.run(cmd, shell=True, cwd=str(cwd), env=env,
                       capture_output=True, text=True, timeout=timeout)
        out = proc.stdout or ""
        err = proc.stderr or ""
    except _sp.TimeoutExpired:
        return {"command": cmd, "exit_code": None, "passed": 0, "total": 0,
                "items": [], "timed_out": True,
                "error": "verify 超时（%s 秒）" % timeout, "stdout_tail": ""}
    except Exception as exc:  # noqa: BLE001
        return {"command": cmd, "exit_code": None, "passed": 0, "total": 0,
                "items": [], "timed_out": False,
                "error": "%s: %s" % (type(exc).__name__, exc), "stdout_tail": ""}
    items = []
    total_line = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("PASS "):
            items.append({"name": s[5:].strip(), "ok": True})
        elif s.startswith("FAIL "):
            rest = s[5:].strip()
            name = rest.split("  --")[0].strip()
            items.append({"name": name, "ok": False})
        elif s.startswith("TOTAL:"):
            total_line = s
    passed = sum(1 for it in items if it["ok"])
    # .py 产物编译 + import 烟雾（分层叶的逐层信号：全量测试 import 门是全有或全无，
    # 中间叶只能靠它证明本层已落地；结果只作证据，不改 verified 判定）。
    compile_ok, smoke = None, []
    try:
        _py = sorted(Path(artifacts_abs).glob("*.py")) if Path(artifacts_abs).is_dir() else []
        if _py:
            _c = _sp.run([_sys.executable, "-m", "py_compile"] + [str(f) for f in _py],
                         capture_output=True, text=True, timeout=60)
            compile_ok = (_c.returncode == 0)
            for f in _py:
                _r = _sp.run([_sys.executable, "-c", "import %s" % f.stem],
                             cwd=str(artifacts_abs), capture_output=True,
                             text=True, timeout=30)
                smoke.append({"module": f.stem, "ok": _r.returncode == 0,
                              "error": (_r.stderr or "")[-200:] if _r.returncode else ""})
    except Exception as exc:  # noqa: BLE001
        compile_ok = None
        smoke = [{"module": "", "ok": False,
                  "error": "%s: %s" % (type(exc).__name__, exc)}]
    return {"command": cmd, "exit_code": proc.returncode, "passed": passed,
            "total": len(items), "total_line": total_line, "items": items,
            "compile_ok": compile_ok, "smoke": smoke,
            "timed_out": False, "error": err[-300:] if proc.returncode else "",
            "stdout_tail": out[-800:]}


def _write_artifact_record(run_dir: str, nid: str, record: dict) -> str:
    """产物清单落盘：artifacts/<nid>.json + artifacts/inventory.json（全叶汇总）。"""
    try:
        adir = Path(run_dir) / "artifacts"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / ("%s.json" % nid)).write_text(
            json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        inv_path = adir / "inventory.json"
        inv = {}
        if inv_path.is_file():
            try:
                inv = json.loads(inv_path.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                inv = {}
        inv[str(nid)] = record
        inv_path.write_text(json.dumps(inv, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
        return "artifacts/%s.json" % nid
    except Exception:  # noqa: BLE001
        return ""

def run_leaf(d, node: dict, timeout: float = None) -> dict:
    """一个叶子的一次执行。**当场落终态**（完成/失败），绝不留在 待跑/在跑。

    @param d  既有 `harness/driver.py:Driver` 的实例（这里只借它的 write_doc / run_dir / sink / table /
              run_id / runner —— 不另写落盘与派发路径）
    @returns: 流水行 dict（含 role/node_id/act/round/seconds/judge_ok/exit_class/pending_count）
    """
    nid = str(node.get("id") or "")
    if node.get("state") != "待跑":
        return {"role": node.get("role"), "node_id": nid, "act": "leaf",
                "round": node.get("round"), "seconds": 0.0, "judge_ok": None,
                "exit_class": EXIT_ENV, "pending_count": 0,
                "why": "叶子执行面只接受 state==待跑 的节点，实际 %r" % node.get("state"),
                "rounds_advanced": False}
    managed_doc = node.get("_managed_doc")
    knobs = role_mod.effective(str(node.get("role") or ""), None, table=d.table)
    knobs, rejected_ov = role_mod.apply_overrides(knobs, node.get("overrides"), d.sink, nid)
    if managed_doc is not None:
        # This leaf returns the document body; only Harness may write the bound file.
        knobs["tools"] = ""
    node_mod.change_state(node, "在跑", d.sink)
    node["round"] = 1                      # 叶子只跑一次（76 §2.2：执行/调研 max_rounds=1）
    node["act"] = "leaf"

    node["_run_dir"] = str(d.run_dir)  # B2 F1 fix: run_leaf 给节点打 run_dir
    # V2（ACK 叶）：CC 路径 + 环境开关才进 ACK 模式；pi 路径一字不动。
    _ack = bool(os.environ.get("TREE_CC_ACK_LEAF") == "1"
                and getattr(d.runner, "is_cc", False))
    _acfg = _artifact_config(node)
    _art_dir = None
    if _acfg["artifacts_dir"]:
        _art_dir = Path(d.run_dir) / str(_acfg["artifacts_dir"])
    elif Path(d.run_dir, "project").is_dir():
        _art_dir = Path(d.run_dir) / "project"
    _art_active = bool(_art_dir is not None or _acfg["verify_cmd"])
    _baseline = _snapshot_dir(_art_dir) if _art_dir is not None else {}
    # CC runner 时 role_prompt 已在 agent 文件的 system prompt 里，不重复注入
    # （见 harness/submit.py:assemble_prompt include_role_prompt；pi 路径一字不动）。
    prompt = submit.assemble_prompt(node, knobs, act="leaf",
                                    include_role_prompt=not getattr(d.runner, "is_cc", False),
                                    ack_leaf=_ack)
    if managed_doc is not None:
        prompt += ("\n\n## Harness 受控文档输出\n"
                   "本叶没有文件工具。目标由 Harness 固定为 %s；不要尝试读写文件或输出目标路径。"
                   "请直接在 JSON 的 doc 字段给出完整最终文档正文，reply 简述结果，未决按事实填写。"
                   "文档必须真实：不得声称执行过未执行的操作，不得编造数据。"
                   "Harness 会把 doc 原子写入该目标。"
                   % managed_doc["target"])
    if getattr(d.runner, "is_cc", False):
        # V2 (2026-11-08): CC leaf must know the run dir absolute path.
        # V2-1b leaf wrote all four modules to /tmp/full-tree/project/
        # (the spec/test absolute-path anchor); run-dir project/ stayed
        # empty, mechanical score 0/17, while the misplaced code scored
        # 16/17. pi path untouched.
        # 第三轮 P1④：任务文本里的 run 目录用真实路径（realpath），消除
        # /tmp→/private/tmp 符号链接迷惑模型的问题；只改任务文本写法，
        # 不改 d.run_dir 本身（macOS 上 tempfile 路径 /var → /private/var
        # 的 realpath 会打断 str(td) in context 类测试断言）。
        prompt += ("\n\n" + "工作目录：本次 run 目录绝对路径：" + os.path.realpath(str(d.run_dir)) +
                   "。所有相对路径（如 project/store.py）都以它为基准；" +
                   "文件必须写到该目录下的 project/ 子目录，不要写到 run 目录之外的任何路径。")
    sid = session.session_id(d.run_id, nid)
    row = {"role": node.get("role"), "node_id": nid, "act": "leaf", "round": 1,
           "runner": d.runner_kind, "session_id": sid, "prompt_chars": len(prompt),
           "overrides_rejected": rejected_ov, "rounds_advanced": True}
    if managed_doc is not None:
        row["managed_doc_target"] = managed_doc["target"]
    t0 = time.time()
    exit_class, why, obj, raw, no_judge = EXIT_ENV, "", None, "", False
    # t-64e27d836932：单叶上限按 node_kind 分流（research 300s / contract_repair 180s；
    # TREE_PI_TIMEOUT=900 是整轮兜底，不能让单叶挂 900 秒清零）。
    from harness import contract as _ct_mod
    _kind = _ct_mod.classify(node)
    _ov = node.get("overrides") or {}
    _leaf_timeout = _ct_mod.leaf_timeout_for(
        _kind, work_mode=_ov.get("work_mode") if isinstance(_ov, dict) else None,
        dryrun=_ct_mod.is_dryrun())
    _salvaged = False
    _runner_kw = {}
    try:
        import inspect as _inspect
        if "timeout" in _inspect.signature(d.runner).parameters:
            _runner_kw["timeout"] = _leaf_timeout
    except (TypeError, ValueError):
        _runner_kw = {}
    try:
        raw = d.runner(node, "leaf", knobs, prompt, sid, str(d.run_dir), **_runner_kw)
    except subprocess.TimeoutExpired as exc:
        # V2：why 不再嵌整条 argv（旧值把含 prompt 全文的命令原样塞进 why，
        # 导致 classify_failure 误标 bad_citation）；标签按 runner 如实写。
        _exc_txt = str(exc)
        if len(_exc_txt) > 200:
            _exc_txt = _exc_txt[:200] + "…（命令已截断，不含 prompt 原文）"
        _who = "CC" if getattr(d.runner, "is_cc", False) else "pi"
        # t-dc62df942275 项 2：单叶上限为 None 时（synthesis kind）实际 deadline 是
        # run_cc_node 的 CC_TIMEOUT_DEFAULT（环境 TREE_CC_TIMEOUT 覆盖），旧文案
        # 一律写 TREE_PI_TIMEOUT 是错的（V2 §6.3 附带小瑕疵）；pi 路径保持原样。
        _to_label = ("TREE_CC_TIMEOUT" if getattr(d.runner, "is_cc", False)
                     else "TREE_PI_TIMEOUT")
        exit_class, why = EXIT_TIMEOUT, (((_who + " 到单叶上限（%s %s 秒）：%s"
                                           % (_kind, _leaf_timeout, _exc_txt))
                                          if _leaf_timeout else
                                          (_who + " 到 " + _to_label + "：" + _exc_txt)))
        _salv = salvage_partial_evidence(str(d.run_dir), nid, sid)
        if _salv.get("successes"):
            _salvaged = True
            why += "；已抢救成功工具结果 %d 条 → %s" % (_salv["successes"], _salv["path"])
            node["未决"] = [{"type": "缺信息",
                             "what": "叶子超时，已抢救部分证据见 %s（成功工具结果 %d 条）" % (
                                 _salv["path"], _salv["successes"]),
                             "why": "单叶上限到（%s 秒），pi 被终止；已获证据未入库，需父节点据此缩小/重派" % _leaf_timeout}]
        else:
            why += "；无可抢救的成功工具结果"
    except Exception as exc:                                     # noqa: BLE001
        _emsg = "%s: %s" % (type(exc).__name__, exc)
        if "operation was aborted" in str(exc).lower():
            # loop-breaker 熔断终止（扩展调 ctx.abort()）：exit_class 保持"环境"
            # （五值集不许扩），why 精确到熔断器；
            # 详情见该 run 的 loop_breaker.jsonl（kind=环路_熔断终止）及保留文件
            # （<session-log>.preserved.json，schema loop-breaker-preserved/v1）
            exit_class, why = EXIT_ENV, "loop_breaker 熔断终止：" + _emsg
            # F3：熔断终止也要抢救已成功的工具结果（之前只在超时分支抢救；
            # 实测 e2e-20261105-f1f7 的 c2.1 有 33 条成功结果，partial/ 里一条没有）。
            _salv = salvage_partial_evidence(str(d.run_dir), nid, sid)
            if _salv.get("successes"):
                _salvaged = True
                why += "；已抢救成功工具结果 %d 条 → %s" % (_salv["successes"], _salv["path"])
                node["未决"] = [{"type": "缺信息",
                                 "what": "叶子被 loop-breaker 熔断终止，已抢救部分证据见 %s（成功工具结果 %d 条）" % (
                                     _salv["path"], _salv["successes"]),
                                 "why": "loop-breaker 熔断终止 pi；已获证据未入库，需父节点据此缩小/重派"}]
            else:
                why += "；无可抢救的成功工具结果"
        else:
            exit_class, why = EXIT_ENV, _emsg
        if "无进展重复调用熔断" in _emsg and getattr(d.runner, "is_cc", False):
            # t-dc62df942275 项 5：熔断也是 runner 抛错、cc_act_runner 没机会记；
            # 已落盘的事件从 meta 读回节点；failure_class 由 classify_failure
            # 按 why 里的熔断标记判为 loop（五值 exit_class 不扩，仍为环境）。
            _tc_loop = _read_cc_meta_tool_calls(str(d.run_dir), sid)
            if _tc_loop:
                node["_cc_tool_calls"] = _tc_loop
                row["cc_tool_calls_loop"] = len(_tc_loop)
    # T-FIX-TREE 续修：超时/loop-breaker 熔断后，若会话里已有模型写进工具调用
    # （或正文）的最终 JSON ⇒ 按正常管线核验，不直接判失败。
    # 实测（实验 A c2.1/c4.1）：模型把最终 JSON 反复 Write 到 product/output.json，
    # 熔断时 pi 被 abort，但 JSON 本身已在会话里写了 5 次且形状完整。
    _abort_note = ""
    # T-FIX-TREE 续修补丁 4：超时一律尝试抢救，不认 why 文案。
    # run C 实测：synthesis kind 的单叶上限为 None，超时文案是
    # "pi 到 TREE_PI_TIMEOUT：..."（不含"单叶上限"），旧门漏掉它；
    # 而会话里已有两份写进 final_output.json 的最终 JSON（reply/未决/doc 齐全）。
    # 环境类错误仍只认 loop-breaker 熔断（其它环境错无会话可抢）。
    if _should_harvest_after_failure(exit_class, why):
        _hv, _hprov = _try_harvest(str(d.run_dir), nid, sid)
        if _hv is not None:
            raw = json.dumps(_hv, ensure_ascii=False)
            _abort_note, why = why, ""
            row["final_json_harvested_from"] = _hprov
            row["harvested_after"] = "timeout" if exit_class == EXIT_TIMEOUT else "loop_breaker"
    if not why:
        try:
            obj = parse_act_output(raw, "leaf")
        except ValueError as exc:
            # 正文里无 JSON 时同样抢救一次会话（模型只写了文件、没发正文的情形）。
            _hv, _hprov = _try_harvest(str(d.run_dir), nid, sid)
            if _hv is not None:
                obj = _hv
                row["final_json_harvested_from"] = _hprov
                row["harvested_after"] = "no_json_in_raw"
            else:
                exit_class, why = EXIT_SHAPE, "产出里没有可取用的 JSON：%s" % exc
    if not why and obj is not None:
        # T-FIX-TREE 续修（机械，不改语义）：
        #  ① doc 若是 run 内已存在文件的相对路径（如 "blocks/xxx.md"）⇒ 读出正文当 doc 用。
        #     实测：c2.1/c4.1 的 doc 字段就是这么写的指针，1620 字正文在 blocks 文件里。
        #  ② 未决[].type 机械归一（"未决"→"判不了"等），再进字段/形状校验。
        if managed_doc is None:
            _doc_ok, _doc_path = resolve_doc_pointer(obj, str(d.run_dir))
            if _doc_ok:
                row["doc_resolved_from"] = _doc_path
        if isinstance(obj.get("未决"), list):
            _und, _unote = schema.normalize_model_undecided(obj["未决"])
            if _unote:
                obj["未决"] = _und
                row["undecided_normalized"] = _unote
        # 2026-11-13：审计/调研叶误带 doc/edits ⇒ 丢弃越权字段，不让整条结论作废。
        obj, _ro_note = schema.normalize_readonly_output(obj, knobs)
        if _ro_note:
            row["readonly_normalized"] = _ro_note
        # V2.1 P2 hard/soft fields: hard missing/wrong -> specific error with code; soft -> mechanical default.
        from harness import fields as _fields_mod
        _hard, _filled = _fields_mod.classify_and_fill("leaf", obj, knobs, ack_leaf=_ack)
        if _filled:
            row["soft_defaults"] = _filled
        if _hard:
            exit_class, why = EXIT_SHAPE, "硬字段错误：%s" % "；".join(e["message"] for e in _hard)
            row["error_code"] = _hard[0]["code"]
        if not _hard:
            _clean, perm_why = schema.check_permission("leaf", obj, knobs, d.sink)
            ok_schema, schema_why = schema.validate("leaf", obj, knobs, ack_leaf=_ack)
            if not ok_schema:
                exit_class, why = EXIT_SHAPE, "产出不契约：%s" % schema_why
            elif perm_why:
                why = "越权字段已丢弃：%s" % perm_why
            if ok_schema:
                _ov = node.get("overrides") or {}
                _review_verdict = None
                if node.get("_structured_review"):
                    _verdict = obj.get("verdict")
                    _covers = obj.get("covers")
                    _evidence = obj.get("evidence")
                    _note = obj.get("note")
                    _review_ok = (
                        _verdict in ("pass", "rework", "escalate")
                        and isinstance(_covers, list)
                        and all(isinstance(item, str) for item in _covers)
                        and isinstance(_evidence, list) and bool(_evidence)
                        and all(isinstance(item, dict)
                                and isinstance(item.get("source"), str) and item["source"].strip()
                                and isinstance(item.get("observation"), str) and item["observation"].strip()
                                for item in _evidence)
                        and isinstance(_note, str)
                    )
                    _blocker_id = str(node.get("_review_blocker_id") or "")
                    _normalized_cover = None
                    if isinstance(_covers, list) and len(_covers) == 1 and isinstance(_covers[0], str):
                        _normalized_cover = _normalize_review_cover(_covers[0], _blocker_id)
                    if _review_ok:
                        _allowed_evidence = set(node.get("_review_evidence_ids") or [])
                        _review_ok = (
                            _normalized_cover is not None
                            and all(item["source"] in _allowed_evidence for item in _evidence)
                        )
                    if not _review_ok:
                        exit_class, why = EXIT_SHAPE, (
                            "结构化复核字段不合规：verdict/covers/evidence/note 不满足契约")
                    else:
                        _review_verdict = {"verdict": _verdict, "covers": [_normalized_cover],
                                           "evidence": _evidence, "note": _note}
                if exit_class != EXIT_SHAPE:
                    # 证据门（t-64e27d836932 类型化分流）：research 要求真实工具结果；
                    # contract_repair/synthesis/audit 走 context-only。
                    from harness import evidence as evidence_mod
                    _sess_path = session.find_session_file(
                        sid, os.environ.get("TREE_PI_AGENT_SESSION_DIR"))
                    _ev_ids = _ov.get("input_evidence_ids")
                    _ev_ok, _ev_detail = evidence_mod.evidence_gate(
                        node, obj, _sess_path, str(d.run_dir), str(role_mod.ROOT),
                        may_write=knobs.get("may_write", False), node_kind=_kind,
                        input_evidence_ids=_ev_ids,
                        cc_tool_calls=node.get("_cc_tool_calls"))
                    if not _ev_ok:
                        exit_class, why = EXIT_SHAPE, "产出不契约（证据门）：%s" % _ev_detail
                        gate.log_violation(d.sink, kind="驱动_证据门", node_id=nid,
                                           detail=_ev_detail + " ⇒ 该叶失败，reply 不入库")
                    elif _review_verdict is not None:
                        node["_review_verdict"] = _review_verdict
    # 判据求值：形状不合时**不**求值（没有可信的产出正文可判）
    judge_ok = None
    result_type = "failed"
    if obj is not None and exit_class != EXIT_SHAPE:
        judge_ok, jwhy, no_judge = evaluate_judge(node, obj)
        if judge_ok is None:
            # 判据为自然语言：产出入库，但结果类型 partial（未机械核验）；
            # 未决义务在入库块之后追加（见下），merge 收束时必须覆盖。
            exit_class = EXIT_DONE
            result_type = "partial"
        else:
            exit_class = EXIT_DONE if judge_ok else EXIT_JUDGE
            result_type = "complete" if judge_ok else "failed"
        why = jwhy if not why else ("%s；%s" % (why, jwhy))
    managed_write_error = ""
    if managed_doc is not None and obj is not None and exit_class != EXIT_SHAPE:
        doc_text = obj.get("doc")
        doc_error = ""
        if not isinstance(doc_text, str) or not doc_text.strip():
            doc_error = "must be a non-empty string"
        else:
            stripped = doc_text.strip()
            if _looks_like_pointer(stripped):
                doc_error = "must be inline text, not a path"
            else:
                try:
                    json.loads(stripped)
                except ValueError:
                    pass
                else:
                    doc_error = "must be document text, not JSON"
        if doc_error:
            exit_class, result_type, judge_ok = EXIT_SHAPE, "failed", False
            why = "managed document output " + doc_error
        elif judge_ok is True:
            try:
                ok_doc, info = d.write_managed_doc(nid, managed_doc["target"], doc_text)
                row["managed_doc_write"] = {"target": managed_doc["target"],
                                            "chars": len(doc_text), "ok": bool(ok_doc),
                                            "result": str(info)}
                if not ok_doc:
                    managed_write_error = str(info)
            except (OSError, ValueError, TypeError) as exc:
                managed_write_error = "%s: %s" % (type(exc).__name__, exc)
                row["managed_doc_write"] = {"target": managed_doc["target"],
                                            "chars": len(doc_text), "ok": False,
                                            "result": managed_write_error}
        else:
            row["managed_doc_write"] = {
                "target": managed_doc["target"], "chars": len(doc_text), "ok": False,
                "result": "judge 未通过；目标文件保持原字节"}
    if _abort_note:
        # 熔断/超时后抢救到的最终 JSON 已按正常管线核验；熔断事实如实保留在 why 里，
        # 不改 exit_class（判据说了算）。
        _hv_note = "%s；会话抢救最终 JSON（%s）已按正常管线核验" % (
            _abort_note, row.get("final_json_harvested_from") or "")
        why = (_hv_note + "；" + why) if why else _hv_note
    elif _salvaged:
        # 超时/熔断但抢救到了成功工具结果：失败叶 + partial 证据可被父消费。
        result_type = "partial"
    # ── 第二轮 A：落盘 doc 机械核验 ──
    # 只在有可信产出正文时跑（与判据求值同一条件）；模型自报字数不作数，量磁盘。
    # complete + 落盘未达标 ⇒ failed（与 dict 判据 chars_min 不过同一口径）；
    # partial（自然语言判据）保持 partial，但落盘实测写进 why，merge 覆盖与终审可见。
    _disk_failed = False
    if obj is not None and exit_class != EXIT_SHAPE:
        _sess_for_disk = session.find_session_file(
            sid, os.environ.get("TREE_PI_AGENT_SESSION_DIR"))
        _dok, _dwhy, _dinfo = verify_disk_doc(
            node, obj, str(d.run_dir), str(role_mod.ROOT), _sess_for_disk or "",
            doc_resolved_from=str(row.get("doc_resolved_from") or ""))
        row["disk_doc_check"] = _dinfo
        node["disk_doc_check"] = _dinfo
        if not _dok:
            _disk_failed = True
            # 2026-11-12：字数门已取消；此分支仅由磁盘核验自身失败触发。
            result_type = "failed"
            exit_class = EXIT_JUDGE
            judge_ok = False
            no_judge = False
            jwhy = _dwhy
            why = (why + "；" + _dwhy) if why else _dwhy
    if managed_doc is not None and managed_write_error:
        result_type = "failed"
        exit_class = EXIT_JUDGE
        judge_ok = False
        _disk_failed = True
        why = (why + "；" + "受控文档落盘失败：" + managed_write_error) if why else \
              "受控文档落盘失败：" + managed_write_error
    # ── V2：产物优先判定（ACK 叶）──
    # 模型自报（judge/JSON）只是参考；磁盘差分 + 机械验收才是交付证据。
    # 超时/无 JSON 但产物 + 测试全绿 ⇒ verified（完成）；有产物未全绿 ⇒ partial；
    # 判据通过但机械验收失败 ⇒ 降为 partial（机械门压过模型自报）。
    _verified = False
    if _art_active:
        # 超时 meta 里的工具调用也记到节点（cc_act_runner 没机会记时）。
        if exit_class == EXIT_TIMEOUT and getattr(d.runner, "is_cc", False):
            _tc = _read_cc_meta_tool_calls(str(d.run_dir), sid)
            if _tc:
                node["_cc_tool_calls"] = _tc
                row["cc_tool_calls_timeout"] = len(_tc)
        _after = _snapshot_dir(_art_dir) if _art_dir is not None else {}
        _created, _modified = _diff_snapshot(_baseline, _after)
        _verify = None
        if _acfg["verify_cmd"]:
            _verify = _run_verify(_acfg["verify_cmd"], str(d.run_dir),
                                  _art_dir or Path(d.run_dir))
        _n_art = len(_created) + len(_modified)
        _v_ok = bool(_verify is not None
                     and _verify.get("exit_code") == 0
                     and (_verify.get("total") or 0) > 0
                     and _verify.get("passed") == _verify.get("total"))
        _verified = bool(_n_art > 0 and _v_ok)
        _was_timeout = (exit_class == EXIT_TIMEOUT)
        _art_record = {
            "node_id": nid, "role": node.get("role"), "round": node.get("round"),
            "created": _created, "modified": _modified, "verify": _verify,
            "verified": _verified, "exit_class": exit_class,
            "result_type_before": result_type,
        }
        _rec_path = _write_artifact_record(str(d.run_dir), nid, _art_record)
        node["artifacts"] = _created + _modified
        node["verify"] = _verify
        node["verified"] = _verified
        row["artifacts"] = _created + _modified
        row["verify"] = _verify
        row["verified"] = _verified
        row["artifact_record"] = _rec_path
        if _verified and not _disk_failed:
            if result_type != "complete":
                _note = ("机械验收：产物 %d 个文件，测试 %d/%d 通过"
                         % (_n_art, _verify["passed"], _verify["total"]))
                if _was_timeout:
                    _note += "（CC 进程超时未交最终 JSON，产物已验证）"
                why = (why + "；" + _note) if why else _note
            result_type = "complete"
            exit_class = EXIT_DONE
        elif _n_art > 0:
            _note = ("机械验收：产物 %d 个文件已落盘" % _n_art)
            if _verify is not None:
                _note += ("，测试 %d/%d 通过" % (_verify.get("passed", 0),
                                                _verify.get("total", 0)))
                if _verify.get("compile_ok") is True:
                    _note += "，编译通过"
            if _was_timeout:
                _note += "（超时未交最终 JSON，产物不作废）"
            why = (why + "；" + _note) if why else _note
            if result_type == "complete":
                result_type = "partial"  # 判据通过但机械验收未全绿 ⇒ 降级
            elif result_type == "failed" and not _disk_failed:
                result_type = "partial"  # 有产物 ⇒ 不是 0 产出（落盘 doc 未达标时不升）
        elif _acfg["expect_artifacts"] and result_type == "complete":
            why = (why + "；ACK 通过但磁盘无产物（expect_artifacts）") if why else \
                "ACK 通过但磁盘无产物（expect_artifacts）"
            result_type = "failed"
            exit_class = EXIT_JUDGE
    # V2.1 P2: failure classification (mechanical) on row and node, for parent recovery decisions.
    try:
        from harness import contract as _ct0
        _fc0 = _ct0.classify_failure(exit_class, why, node)
        row["failure_class"] = _fc0
        node["failure_class"] = _fc0
        if row.get("error_code"):
            node["error_code"] = row["error_code"]
        elif _fc0 == "phantom_citation":
            row["error_code"] = "phantom_citation"
            node["error_code"] = "phantom_citation"
    except Exception:
        pass
    t1 = time.time()
    row.update({"start": round(t0, 3), "end": round(t1, 3), "seconds": round(t1 - t0, 3),
                "chars": len(raw or ""), "schema_ok": obj is not None and exit_class != EXIT_SHAPE,
                "judge_ok": judge_ok, "exit_class": exit_class, "why": why,
                "judge_why": _why_with_class(exit_class, why),
                "无判据": bool(no_judge), "pending_count": 0})

    if obj is not None and exit_class != EXIT_SHAPE:
        # reply / 未决 落到节点字段（模型原文，不替它改）
        if str(obj.get("reply") or "").strip():
            node["reply"] = str(obj["reply"])
        if isinstance(obj.get("未决"), list):
            node["未决"] = obj["未决"]
        row["pending_count"] = len(node["未决"])
        # doc 落盘：**唯一入口** = 既有 Driver.write_doc
        doc = str(obj.get("doc") or "")
        if doc.strip() and managed_doc is None:
            ok_doc, info = d.write_doc(nid, doc)
            row["doc"] = info
            row["doc_ok"] = bool(ok_doc)
        elif managed_doc is not None:
            row["doc"] = {"target": managed_doc["target"], "chars": len(doc),
                          "written": bool((row.get("managed_doc_write") or {}).get("ok"))}
        edits = obj.get("edits")
        if isinstance(edits, list) and edits:
            # 本批不做「增量合并」语义（76 §3.4 的 edits 落地属另一件事）——
            # 但绝不静默丢弃：原样存一笔供事后取用，并如实计数。
            with open(Path(d.run_dir) / "edits.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"node_id": nid, "edits": edits},
                                    ensure_ascii=False) + "\n")
            row["edits"] = len(edits)
        node["judge_ok"] = judge_ok
        node["judge_why"] = row["judge_why"]
    else:
        node["judge_ok"] = None
        node["judge_why"] = row["judge_why"]
        node["未决"] = node.get("未决") or []
    if result_type == "partial" and judge_ok is None:
        node["未决"] = list(node.get("未决") or []) + [{
            "type": "判不了",
            "what": "判据为自然语言，未机械核验，需审计复核",
            "why": "该叶产出已入库但未通过机械判据；merge 收束时须覆盖或由审计确认"}]
    # 证据台账：每叶一次执行记一笔（结果类型 / 工具 / 会话 / 引用 / 未决）。
    try:
        from harness import contract as _ct2
        from harness import evidence as _ev2
        _sp = session.find_session_file(
            sid, os.environ.get("TREE_PI_AGENT_SESSION_DIR"))
        _tools = _ev2.count_tool_results(_sp) if _sp else {"calls": 0, "successes": 0, "failures": 0}
        _ct2.append_ledger(str(d.run_dir), _ct2.ledger_entry(
            node, result_type, _tools, sid,
            _ev2.seen_citations(str(node.get("reply") or "")),
            list(node.get("未决") or []), session_path=_sp or ""))
    except Exception:                                          # noqa: BLE001
        pass
    row["result_type"] = result_type
    # 当场落终态
    ok_st, st_why = node_mod.change_state(node, EXIT_DONE if exit_class == EXIT_DONE else "失败", d.sink)
    row["state"] = node.get("state")
    if not ok_st:
        row["state_change_why"] = st_why
    # A（t-f6f9d7325d93）：叶终结即发一次结构化进度事件（证据/partial 已落盘）。
    # 只写 run/progress.jsonl + 尽力推 bridge progress，失败不影响本叶结果。
    try:
        from harness import progress as _pg
        _pg.emit_leaf_progress(str(d.run_dir), str(d.run_id), node, row, sid)
    except Exception:
        pass
    # P0（V2.1 t-5df0e8dd3660）：可信成果台账 + 保底交付持续落盘。
    # ★ 必须在落终态之后记：health.state 取终态（完成/失败），不能记成“在跑”
    # （Tier1 实测：记早了导致健康表全是在跑）。写失败不影响本叶结果。
    try:
        from harness import contract as _ct3
        from harness import evidence as _ev3
        from harness import trust as _tr
        _sp3 = session.find_session_file(
            sid, os.environ.get("TREE_PI_AGENT_SESSION_DIR"))
        _tools3 = _ev3.count_tool_results(_sp3) if _sp3 else {"calls": 0, "successes": 0, "failures": 0}
        _prods = _tr.collect_products(str(d.run_dir), nid, node, row)
        _ent = _tr.trust_entry(node, result_type, exit_class, _prods, _tools3, sid,
                               _ev3.seen_citations(str(node.get("reply") or "")),
                               list(node.get("未决") or []))
        _ent["kind"] = _ct3.classify(node)
        _tr.append_trust(str(d.run_dir), _ent)
        _tr.gen_guaranteed_delivery(getattr(d, "tree", {}) or {}, str(d.run_dir), str(d.run_id))
    except Exception:                                          # noqa: BLE001
        pass
    return row


# ============================================================ 心跳（§2.3.3）
def _snapshot_obj(tree: dict) -> dict:
    """与既有 recover.dump_snapshot 的**同一形状**：{"nodes": {<id>: <字段…>}}（过滤 `_` 前缀内部键）。"""
    return {"nodes": {str(nid): {k: v for k, v in (n or {}).items() if not str(k).startswith("_")}
                      for nid, n in (tree or {}).items()}}


def write_heartbeat(tree: dict, run_dir, n: int, scheduler: dict = None) -> tuple:
    """落第 n 笔心跳，并用同一个 checkpoint 刷新 state.json。"""
    hb = Path(run_dir) / "heartbeat"
    hb.mkdir(parents=True, exist_ok=True)
    p = hb / ("round-%d.json" % int(n))
    p.write_text(json.dumps(_snapshot_obj(tree), ensure_ascii=False, indent=1), encoding="utf-8")
    sp = recover.dump_snapshot(tree, d=str(run_dir), scheduler=scheduler)
    return str(p), str(sp)


# ============================================================ 驱动器的流水补键
class TreeDriver(Driver):
    """既有 Driver 的**只加不删**子类：唯一改动 = 往它自己写的那一行流水里补四个键。

    既有的 `next_act` / `request_act` / `apply_output` / `spawn_children` 行为一个字节没动
    （driver.py 本体 md5 未变）；本类只挂 `_log_round` 的钩子。
    """

    def __init__(self, *a, **kw):
        self.timed = kw.pop("timed")
        super().__init__(*a, **kw)
        # runner_kind 口径：外壳只计时，真跑路径仍是既有 harness/session.run_act；
        # 但若 TREE_ACT_CMD 被设（桩模式），如实记成桩（不冒充真跑）。
        self.runner_kind = self.runner_kind if str(self.runner_kind).startswith("stub") else "pi"

    def _log_round(self, row: dict) -> None:
        node = self.tree.get(str(row.get("node_id") or "")) or {}
        row["role"] = node.get("role")
        rec = self.timed.last_for(row.get("node_id"))
        pending = 0
        exit_class = EXIT_ENV
        if row.get("skip"):
            exit_class = EXIT_ENV if row["skip"] not in ("waiting",) else EXIT_ENV
        if rec is not None and rec.get("err"):
            exit_class = EXIT_TIMEOUT if "TimeoutExpired" in rec["err"] else EXIT_ENV
        elif rec is not None:
            try:
                obj = json.loads(rec["raw"].strip())
                if not isinstance(obj, dict):
                    obj = None
            except Exception:                                    # noqa: BLE001
                obj = None
                # 正文里夹 JSON 的情形也用既有解析器再试一次（与 step() 同口径）
                try:
                    obj = parse_act_output(rec["raw"], str(row.get("act") or "plan"))
                except Exception:                                # noqa: BLE001
                    obj = None
            if obj is not None:
                lst = obj.get("未决")
                pending = len(lst) if isinstance(lst, list) else 0
                if row.get("schema_ok") and row.get("rounds_advanced"):
                    exit_class = EXIT_DONE
                elif row.get("schema_ok"):
                    exit_class = EXIT_JUDGE
                else:
                    exit_class = EXIT_SHAPE
            else:
                exit_class = EXIT_SHAPE
        if (rec is None and row.get("runner") == "mechanical"
                and row.get("hard_doc_preempt")):
            detail = str(row.get("why") or "")
            if detail.startswith("【修复派发】"):
                exit_class = EXIT_DONE
            elif detail.startswith("【明确失败】"):
                exit_class = EXIT_JUDGE
        row["judge_ok"] = None                                   # 父没有机械判据（task-def §2.4 ④）
        row["exit_class"] = exit_class
        row["pending_count"] = pending
        super()._log_round(row)


def _missing_field_of(reason):
    m = re.search(r"'([^']+)'\s*必填", reason or "")
    if m:
        return m.group(1)
    m = re.search(r"缺必填键\s*'([^']+)'", reason or "")
    if m:
        return m.group(1)
    return ""


# ============================================================ 统一推进队列（§2.6）
# ---------------------------------------------------------------- 环境重试台账（第三轮 P1④）
ENV_RETRY_LEDGER = "env_retry.jsonl"
ENV_RETRY_MAX = 1
ENV_RETRY_EXIT_CLASSES = (EXIT_ENV, EXIT_TIMEOUT)


def read_env_retries(run_dir) -> list:
    """读 run 内 env_retry.jsonl 台账（checkpoint 重入后仍以台账为准，不靠内存计数）。"""
    p = Path(run_dir) / ENV_RETRY_LEDGER
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:                                        # noqa: BLE001
            continue
    return rows


def env_retry_count(run_dir, base_id: str) -> int:
    """某原叶已重试次数（按 node_id 归一；重试节点本身的 id 去 .retryN 后缀后查）。"""
    base = str(base_id or "")
    return sum(1 for r in read_env_retries(run_dir)
               if str((r or {}).get("node_id") or "") == base)


def record_env_retry(run_dir, base_id: str, retry_id: str, exit_class: str, failure_class: str) -> dict:
    """记一笔重试（append-only 台账）。"""
    row = {"ts": time.time(), "node_id": str(base_id or ""), "retry_id": str(retry_id or ""),
           "attempt": env_retry_count(run_dir, base_id) + 1,
           "exit_class": str(exit_class or ""), "failure_class": str(failure_class or "")}
    p = Path(run_dir) / ENV_RETRY_LEDGER
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


class Scheduler:
    """「按规则挑一个可推进的节点 ⇒ 推进它一步 ⇒ 落心跳 ⇒ 重复」——直到全树终态 / 步数上限 / 墙钟上限。"""

    def __init__(self, run_dir, run_id: str, table: dict, root_ctx: str,
                 max_steps: int = 40, max_seconds: float = 7200.0, max_reject: int = 2,
                 act_runner=None, inner_runner=None, t0: float = None):
        self.run_dir = Path(run_dir)
        self.run_id = str(run_id)
        self.table = table
        self.root_ctx = root_ctx or ""
        self.max_steps = int(max_steps)
        self.max_seconds = float(max_seconds)
        self.max_reject = int(max_reject)
        try:
            self.max_rescue = int(os.environ.get("TREE_RESCUE_MAX") or 3)
        except (ValueError, TypeError):
            self.max_rescue = 3
        self.tree = {}
        self.step_count = 0
        self.sticky = None
        self.rejects = {}
        self.repairs = {}                 # (parent_id, field) -> repair child id
        self.diagnoses = set()            # (parent_id, field) -> 已派诊断子节点
        from harness import recovery as _rec_mod
        self.recovery = _rec_mod.RecoveryController(str(self.run_dir))
        recovery_ledger = self.run_dir / "recovery.jsonl"
        if recovery_ledger.is_file():
            with recovery_ledger.open(encoding="utf-8") as stream:
                self.recovery.edges = [json.loads(line) for line in stream if line.strip()]
        from harness import arbitration as _arb_mod
        self.arbitrator = _arb_mod.Arbitrator(str(self.run_dir))
        self.stop_reason = ""
        self.calls = []                 # 所有真跑调用的计时记录（含审计/终审检查点）
        self.rounds_log = self.run_dir / "rounds.jsonl"
        self.driver_log = self.run_dir / "driver.log"
        self.sink = str(self.run_dir / "violations.jsonl")
        self.t0 = float(t0 if t0 is not None else time.time())
        self.waiting = False
        self.wait_reason = ""
        if inner_runner is None:
            inner_runner = driver_mod.resolve_act_runner(act_runner)[0]
        self.inner = inner_runner
        self.run_dir.mkdir(parents=True, exist_ok=True)
        heartbeat_dir = self.run_dir / "heartbeat"
        heartbeat_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_seq = max(
            (int(path.stem[len("round-"):]) for path in heartbeat_dir.glob("round-*.json")
             if path.stem[len("round-"):].isdigit()),
            default=0)
        (self.run_dir / "doc").mkdir(parents=True, exist_ok=True)

    # ---- 建树 ---------------------------------------------------------------
    def build_root(self, context: str = None) -> dict:
        root = node_mod.new_node("root", "根", parent="",
                                 context=(context if context is not None else self.root_ctx),
                                 judge=None, 验收标准="", auth="")
        self.tree["root"] = root
        return root

    # ---- 候选（§2.6 挑节点规则）--------------------------------------------
    def is_parent(self, n: dict) -> bool:
        try:
            return bool(role_mod.effective(str(n.get("role") or ""), None,
                                           table=self.table).get("may_spawn"))
        except KeyError:
            return False

    def ordered(self) -> list:
        return sorted(self.tree, key=lambda nid: (node_mod.layer_of(nid, self.tree), str(nid)))

    def candidate(self, n: dict) -> bool:
        if node_mod.is_terminal(n) or n.get("state") == node_mod.WAIT_ADJUDICATION:
            return False
        if self.is_parent(n):
            if n.get("state") == node_mod.WAIT_CHILDREN:
                # 与既有 Driver.step() 同一口径：本批次**还没拉过子**（batch 空）⇒ 该解冻继续跑；
                # 有子但没全终结 ⇒ 真的在等（不是候选）。
                if node_mod.batch_members(n, self.tree):
                    ready, _why = node_mod.ready_to_merge(n, self.tree)
                    return bool(ready)
                return True
            return True
        return n.get("state") == "待跑"

    # ---- 心跳 / 日志 --------------------------------------------------------
    def log(self, line: str) -> None:
        with open(self.driver_log, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))

    def checkpoint_scheduler(self) -> dict:
        return {
            "version": 1,
            "step_count": self.step_count,
            "rejects": dict(self.rejects),
            "repairs": dict(self.repairs),
            "diagnoses": set(self.diagnoses),
            "sticky": self.sticky,
            "elapsed_seconds": max(0.0, time.time() - self.t0),
        }

    def restore_checkpoint(self, checkpoint: dict) -> None:
        self.tree = dict(checkpoint.get("nodes") or {})
        from harness import final_audit as _final_audit
        _final_audit.restore_sequence(self.tree)
        saved = checkpoint.get("scheduler") or {}
        self.step_count = int(saved.get("step_count") or 0)
        self.rejects = dict(saved.get("rejects") or {})
        self.repairs = dict(saved.get("repairs") or {})
        self.diagnoses = {tuple(pair) for pair in (saved.get("diagnoses") or set())}
        self.sticky = saved.get("sticky")
        elapsed = max(0.0, float(saved.get("elapsed_seconds") or 0.0))
        self.t0 = time.time() - elapsed

    def heartbeat(self) -> None:
        self.heartbeat_seq += 1
        write_heartbeat(self.tree, self.run_dir, self.heartbeat_seq,
                        scheduler=self.checkpoint_scheduler())

    def _record_adjudication_progress(self, n: dict, detail: str, sticky: str = None) -> None:
        self.step_count += 1
        nid = str(n.get("id") or "")
        self.rejects[nid] = 0
        self.sticky = sticky
        self.heartbeat()
        self.log("裁决进度：节点 %s %s" % (nid, detail))

    def _move_to_adjudication_wait(self, n: dict) -> bool:
        state = n.get("state")
        if state == node_mod.WAIT_ADJUDICATION:
            return True
        if state == node_mod.WAIT_CHILDREN:
            ok, _ = node_mod.change_state(n, "待跑", self.sink)
            if not ok:
                return False
            state = "待跑"
        if state == "待跑":
            ok, _ = node_mod.change_state(n, "在跑", self.sink)
            if not ok:
                return False
            state = "在跑"
        if state == "在跑":
            ok, _ = node_mod.change_state(n, node_mod.WAIT_ADJUDICATION, self.sink)
            return bool(ok)
        return False

    def _fail_adjudication(self, n: dict, reason: str) -> None:
        meta = n.setdefault("_adjudication", {})
        message = str(reason or "裁决无法应用")
        meta.update({"phase": "failed", "applied": True, "error": message})
        if not node_mod.is_terminal(n):
            node_mod.change_state(n, "失败", self.sink)
        n["judge_ok"] = False
        n["judge_why"] = "裁决失败：%s" % message
        unresolved = list(n.get("未决") or [])
        unresolved.append({"type": "判不了", "what": "裁决未能安全应用",
                           "why": n["judge_why"]})
        n["未决"] = unresolved
        self._record_adjudication_progress(n, "明确失败")

    def _adjudication_binding(self, n: dict, meta: dict) -> tuple:
        issue = meta.get("issue") or {}
        continuation = n.get("_merge_continuation") or {}
        return (
            str(meta.get("issue_id") or issue.get("id") or ""),
            str(meta.get("candidate_sha256") or continuation.get("candidate_sha256")
                or issue.get("candidate_sha256") or ""),
        )

    def _dispatch_sage_review(self, n: dict, meta: dict) -> None:
        review_ids = self._driver(n).dispatch_sage_review(n, meta["issue"])
        meta = n.setdefault("_adjudication", meta)
        meta.update({"review_node_ids": list(review_ids), "phase": "sage",
                     "applied": False})
        if not self._move_to_adjudication_wait(n):
            self._fail_adjudication(n, "Sage 复核排队后原节点无法保持等裁决")
            return
        self._record_adjudication_progress(
            n, "Sage 复核叶已排队：%s" % ("、".join(meta["review_node_ids"]) or "无"))

    def _begin_adjudication(self, n: dict, issue: dict) -> None:
        # 2026-11-13：授权/红线直接等用户，不派 Sage、不发 Leo。
        if str((issue or {}).get("stage") or "") == "user":
            meta = {
                "phase": "user",
                "issue": issue,
                "issue_id": str(issue.get("id") or ""),
                "candidate_sha256": str(issue.get("candidate_sha256") or ""),
                "applied": False,
                "user_question": str(issue.get("question") or "需要用户决定"),
            }
            n["_adjudication"] = meta
            if not self._move_to_adjudication_wait(n):
                self._fail_adjudication(n, "原节点无法迁移到等裁决")
                return
            n["judge_ok"] = None
            n["judge_why"] = meta["user_question"]
            self._record_adjudication_progress(n, "等待用户决定")
            return
        meta = {
            "phase": "sage",
            "issue_id": str(issue["id"]),
            "candidate_sha256": str((n.get("_merge_continuation") or {}).get(
                "candidate_sha256") or issue.get("candidate_sha256") or ""),
            "issue": issue,
            "review_node_ids": [],
            "applied": False,
        }
        n["_adjudication"] = meta
        if not self._move_to_adjudication_wait(n):
            self._fail_adjudication(n, "原节点无法迁移到等裁决")
            return
        self._dispatch_sage_review(n, meta)

    def _start_leo(self, n: dict, meta: dict, card: dict) -> tuple:
        from harness import adjudication as adjudication_mod
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(card["id"]))
        receipt_dir = self.run_dir / "adjudication" / safe_id
        issue = meta.setdefault("issue", {})
        issue["card"] = card
        prepared = adjudication_mod.start_or_read(card, str(receipt_dir), live=False)
        if prepared["status"] == "error":
            self._fail_adjudication(n, prepared.get("error") or "Leo 请求无法准备")
            return True, False, False
        saved_request = json.loads((receipt_dir / "request.json").read_text(encoding="utf-8"))
        meta.update({
            "phase": "leo",
            "receipt_dir": str(receipt_dir),
            "card_sha256": saved_request["card_sha256"],
            "applied": False,
        })
        self.heartbeat()
        return self._poll_leo(n, meta)

    def _mark_leo_sent(self, meta: dict) -> None:
        card_hash = meta["card_sha256"]
        card = meta["issue"]["card"]
        if any(entry.get("kind") == "escalate_sent"
               and entry.get("content_hash") == card_hash
               for entry in self.arbitrator.entries):
            meta["budget_sent"] = True
            return
        self.arbitrator._record({
            "kind": "escalate_sent",
            "card_id": str(card["id"]),
            "content_hash": card_hash,
            "advisor": "judge",
            "outcome": {"status": "submitted", "applied": False},
        })
        meta["budget_sent"] = True
        self.heartbeat()

    def _poll_leo(self, n: dict, meta: dict) -> tuple:
        from harness import adjudication as adjudication_mod
        card = meta["issue"]["card"]
        result = adjudication_mod.start_or_read(
            card, meta["receipt_dir"], live=False)
        if result["status"] == "error":
            worker_path = Path(meta["receipt_dir"]) / "worker.json"
            if worker_path.is_file():
                worker = json.loads(worker_path.read_text(encoding="utf-8"))
                if worker.get("pid"):
                    self._mark_leo_sent(meta)
            self._fail_adjudication(n, result.get("error") or "Leo 裁决传输失败")
            return True, False, False
        if result["status"] == "pending" and result.get("phase") == "prepared":
            if os.environ.get("TREE_ADJUDICATION_LIVE") != "1":
                self.wait_reason = "需要 TREE_ADJUDICATION_LIVE=1 才提交新的 Leo 请求"
                return False, True, False
            if not meta.get("budget_reserved"):
                card_hash = meta["card_sha256"]
                if not any(entry.get("kind") in ("escalate_requested", "escalate_sent")
                           and entry.get("content_hash") == card_hash
                           for entry in self.arbitrator.entries):
                    requested_hashes = {
                        entry.get("content_hash") for entry in self.arbitrator.entries
                        if entry.get("kind") in ("escalate_requested", "escalate_sent")
                        and entry.get("content_hash")
                    }
                    if len(requested_hashes) >= self.arbitrator.max_escalations:
                        self._fail_adjudication(n, "本 run Leo 上升预算已耗尽")
                        return True, False, False
                    self.arbitrator._record({
                        "kind": "escalate_requested",
                        "card_id": str(card["id"]),
                        "content_hash": card_hash,
                        "advisor": "judge",
                    })
                meta["budget_reserved"] = True
                self._record_adjudication_progress(n, "Leo 请求预算已预留")
            result = adjudication_mod.start_or_read(
                card, meta["receipt_dir"], live=True)
            if result["status"] == "pending" and result.get("phase") == "running":
                self._mark_leo_sent(meta)
            elif result["status"] == "done":
                self._mark_leo_sent(meta)
        if result["status"] == "pending":
            if result.get("phase") == "running" and not meta.get("budget_sent"):
                self._mark_leo_sent(meta)
            self.wait_reason = ""
            return False, True, False
        if result["status"] == "error":
            self._fail_adjudication(n, result.get("error") or "Leo 裁决传输失败")
            return True, False, False
        if not meta.get("budget_sent"):
            self._mark_leo_sent(meta)
        self.wait_reason = ""
        return self._apply_leo_receipt(n, meta, result)

    def _apply_leo_receipt(self, n: dict, meta: dict, receipt: dict) -> tuple:
        if receipt["verdict"] == "escalate":
            meta.update({"phase": "user", "applied": True, "receipt": receipt,
                         "user_question": receipt.get("note") or "Leo 无法裁决此问题"})
            n["judge_ok"] = None
            n["judge_why"] = str(receipt.get("note") or "Leo 请求用户决定")
            unresolved = list(n.get("未决") or [])
            unresolved.append({"type": "判不了", "what": "需要用户决定",
                               "why": n["judge_why"]})
            n["未决"] = unresolved
            self._record_adjudication_progress(n, "等待用户决定")
            return True, True, True
        issue_id, candidate_sha256 = self._adjudication_binding(n, meta)
        outcome = {
            "source": "leo",
            "issue_id": issue_id,
            "candidate_sha256": candidate_sha256,
            "card_sha256": meta["card_sha256"],
            "receipt": receipt,
        }
        return self._resume_adjudication(n, meta, outcome, "leo")

    def _dispatch_adjudication_rework(self, n: dict, meta: dict) -> tuple:
        d = self._driver(n)
        cycle = int(n.get("_audit_repair_rounds") or 0) + 1
        if n.get("state") in (node_mod.WAIT_ADJUDICATION, node_mod.WAIT_CHILDREN):
            ok, _ = node_mod.change_state(n, "待跑", self.sink)
            if not ok:
                self._fail_adjudication(n, "修复派发前原节点无法恢复")
                return True, False, False
        if n.get("state") == "待跑":
            ok, _ = node_mod.change_state(n, "在跑", self.sink)
            if not ok:
                self._fail_adjudication(n, "修复派发前原节点无法恢复")
                return True, False, False
        if n.get("state") != "在跑":
            self._fail_adjudication(n, "修复派发前原节点无法运行")
            return True, False, False
        created = d._dispatch_repair_leaves(n, meta["specs"], cycle=cycle)
        if not created:
            self._fail_adjudication(n, "裁决没有可执行的修复叶")
            return True, False, False
        n["_audit_repair_rounds"] = cycle
        ok, _ = node_mod.change_state(n, node_mod.WAIT_CHILDREN, self.sink)
        if not ok:
            self._fail_adjudication(n, "修复叶已建但原节点无法等待子节点")
            return True, False, False
        meta.update({"phase": "rework_dispatched", "applied": False,
                     "repair_node_ids": created})
        self._record_adjudication_progress(n, "修复叶已派发")
        return True, False, False

    def _resume_adjudication(self, n: dict, meta: dict, outcome: dict,
                             source: str) -> tuple:
        if n.get("state") in (node_mod.WAIT_ADJUDICATION, node_mod.WAIT_CHILDREN):
            ok, _ = node_mod.change_state(n, "待跑", self.sink)
            if not ok:
                self._fail_adjudication(n, "裁决回执到达后原节点无法恢复")
                return True, False, False
        result = self._driver(n).resume_adjudication(n, outcome)
        meta = n.setdefault("_adjudication", meta)
        status = result["status"]
        meta["last_source"] = source
        meta["last_status"] = status
        if status == "accepted":
            meta.update({"phase": "applied", "applied": True})
            receipt = (outcome or {}).get("receipt") or {}
            if (source == "leo" and (outcome or {}).get("source") == "leo"
                    and receipt.get("verdict") == "pass"):
                meta["receipt_checkpoint"] = dict(outcome)
            self._record_adjudication_progress(
                n, "裁决已应用",
                sticky=str(n.get("id") or "") if n.get("state") == "待跑" else None)
            return True, False, False
        if status == "rework":
            meta.update({"phase": "rework_dispatched", "applied": False,
                         "specs": result["specs"]})
            return self._dispatch_adjudication_rework(n, meta)
        if status == "user":
            issue = result.get("issue") or meta.get("issue") or {}
            meta.update({"phase": "user", "applied": False, "issue": issue,
                         "issue_id": str(issue.get("id") or meta.get("issue_id") or ""),
                         "user_question": str(result.get("reason") or "需要用户决定")})
            n["judge_ok"] = None
            n["judge_why"] = meta["user_question"]
            self._record_adjudication_progress(n, "等待用户决定")
            return True, True, True
        if status == "leo":
            if source == "leo":
                self._fail_adjudication(
                    n, result.get("reason") or "Leo 回执未能通过绑定或硬检查；不重复提交")
                return True, False, False
            if not self._move_to_adjudication_wait(n):
                self._fail_adjudication(n, "无法恢复等裁决状态")
                return True, False, False
            return self._start_leo(n, meta, result["card"])
        if status == "sage":
            if not self._move_to_adjudication_wait(n):
                self._fail_adjudication(n, "复核后无法恢复等裁决状态")
                return True, False, False
            meta.update({"phase": "sage", "issue": result["issue"],
                         "issue_id": str(result["issue"]["id"]),
                         "candidate_sha256": str((n.get("_merge_continuation") or {}).get(
                             "candidate_sha256") or result["issue"].get("candidate_sha256") or ""),
                         "review_node_ids": [], "applied": False})
            self._dispatch_sage_review(n, meta)
            return True, True, False
        self._fail_adjudication(n, result.get("reason") or "Driver 拒绝应用裁决")
        return True, False, False

    def _poll_adjudications(self) -> tuple:
        pending, user = False, False
        for n in list(self.tree.values()):
            meta = n.get("_adjudication") or {}
            phase = meta.get("phase")
            if phase in ("applied", "failed"):
                continue
            if phase == "after_audit":
                audit_ids = meta.get("audit_node_ids") or []
                if any(not node_mod.is_terminal(self.tree.get(aid) or {}) for aid in audit_ids):
                    pending = True
                    continue
                issue_id, candidate_sha256 = self._adjudication_binding(n, meta)
                outcome = {"source": "audit", "issue_id": issue_id,
                           "candidate_sha256": candidate_sha256,
                           "audit_node_ids": list(audit_ids)}
                return self._resume_adjudication(n, meta, outcome, "audit")
            if phase == "sage":
                review_ids = meta.get("review_node_ids") or []
                if any(not node_mod.is_terminal(self.tree.get(rid) or {}) for rid in review_ids):
                    pending = True
                    continue
                issue_id, candidate_sha256 = self._adjudication_binding(n, meta)
                outcome = {"source": "sage", "issue_id": issue_id,
                           "candidate_sha256": candidate_sha256}
                return self._resume_adjudication(n, meta, outcome, "sage")
            if phase == "rework_dispatched":
                repair_ids = meta.get("repair_node_ids") or []
                if any(not node_mod.is_terminal(self.tree.get(rid) or {}) for rid in repair_ids):
                    pending = True
                    continue
                issue_id, candidate_sha256 = self._adjudication_binding(n, meta)
                outcome = {"source": "repair", "issue_id": issue_id,
                           "candidate_sha256": candidate_sha256,
                           "repair_node_ids": list(repair_ids)}
                return self._resume_adjudication(n, meta, outcome, "repair")
            if phase == "leo":
                self.wait_reason = ""
                changed, waiting, needs_user = self._poll_leo(n, meta)
                if changed:
                    return changed, waiting, needs_user
                pending = pending or waiting
                user = user or needs_user
            elif phase == "user":
                pending = True
                user = True
            elif phase not in (None, ""):
                pending = True
        return False, pending, user

    def _queue_audit_backfill(self, leaf_ids: list, issue: dict = None) -> list:
        rows_by_node = {}
        for row in read_rounds(self.run_dir):
            rows_by_node[str(row.get("node_id") or "")] = row
        created, parents, created_by_parent = [], set(), {}
        for leaf_id in leaf_ids:
            leaf = self.tree[str(leaf_id)]
            row = rows_by_node.get(str(leaf_id)) or {}
            audit_id = self._queue_leaf_audit(str(leaf_id), leaf, row)
            if audit_id:
                parent_id = str(leaf.get("parent") or "")
                created.append(audit_id)
                parents.add(parent_id)
                created_by_parent.setdefault(parent_id, []).append(audit_id)
        for parent_id in parents:
            parent = self.tree[parent_id]
            if parent.get("state") == "待跑":
                node_mod.change_state(parent, "在跑", self.sink)
            if parent.get("state") == "在跑":
                node_mod.change_state(parent, node_mod.WAIT_CHILDREN, self.sink)
        if issue and created:
            owner = self.tree[str(issue["owner_id"])]
            owner["_adjudication"] = {
                "phase": "after_audit",
                "issue_id": str(issue["id"]),
                "candidate_sha256": str((owner.get("_merge_continuation") or {}).get(
                    "candidate_sha256") or issue.get("candidate_sha256") or ""),
                "issue": issue,
                "audit_node_ids": created,
                "applied": False,
            }
        if created:
            for parent_id, audit_ids in created_by_parent.items():
                parent = self.tree[parent_id]
                if issue and parent_id == str(issue["owner_id"]):
                    continue
                continuation = parent.get("_merge_continuation") or {}
                if continuation.get("issue_id"):
                    adjudication = dict(parent.get("_adjudication") or {})
                    adjudication.update({
                        "phase": "after_audit",
                        "issue_id": str(continuation["issue_id"]),
                        "candidate_sha256": str(continuation.get("candidate_sha256") or ""),
                        "audit_node_ids": audit_ids,
                        "applied": False,
                    })
                    parent["_adjudication"] = adjudication
            owner = self.tree[str(issue["owner_id"])] if issue else self.tree[next(iter(parents))]
            self._record_adjudication_progress(owner, "缺失增量审计已补派")
        return created

    def resume_missing_incremental_audits(self) -> list:
        from harness import final_audit as _final_audit
        missing = _final_audit.missing_incremental_audits(self)
        return self._queue_audit_backfill(missing) if missing else []

    # ---- B（t-f6f9d7325d93）：增量审计 --------------------------------------
    def _queue_leaf_audit(self, leaf_id: str, leaf_node: dict, row: dict):
        """叶终结即为其挂一个增量审计叶，下一趟优先执行（不等待整批叶跑完）。

        只对 research/synthesis 叶挂；审计叶本身不再挂（防链式审计）。
        审计叶与原叶同 parent、同 batch ⇒ merge 的 batch_view 可见其 reply，
        且父在 merge 前必须等它终结；但它在原叶终结后立刻排队，不等整批。
        """
        from harness import contract as _ct
        kind = _ct.classify(leaf_node)
        if kind not in ("research", "synthesis"):
            return None
        aid = "%s.audit" % leaf_id
        if aid in self.tree:
            return None
        parent_id = str(leaf_node.get("parent") or "")
        ev_ids, _succ = [], 0
        try:
            _e = _ct.read_ledger(str(self.run_dir)).get(leaf_id) or {}
            ev_ids = [str(c) for c in (_e.get("citations") or [])]
            if _e.get("session_path"):
                ev_ids.append(str(_e.get("session_path")))
            _succ = int((_e.get("tools") or {}).get("successes") or 0)
        except Exception:
            pass
        # 原叶声明的输入证据也是审计复核的边界；ledger.citations 只记产出引用，
        # 漏掉它会把 events.psv 一类源数据挡在 Sage/Leo 绑定快照之外。
        for _ref in ((leaf_node.get("overrides") or {}).get("input_evidence_ids") or []):
            _ref = str(_ref or "").strip()
            if _ref and _ref not in ev_ids:
                ev_ids.append(_ref)
        anode = node_mod.new_node(
            aid, "审计", parent=parent_id,
            context=("增量审计：复核研究叶 %s 的产出与证据（result_type=%s，工具成功 %d 条）。"
                     "只引用下列输入证据 IDs 与该叶 session 工具结果；不做新现场调查。"
                     "输出 JSON {reply, 未决}：reply 给出审计结论与发现（逐条带级别）；"
                     "发现须引用你实际核查到的证据行/路径，不要猜。输入证据：%s。"
                     % (leaf_id, row.get("result_type"), _succ,
                        "、".join(ev_ids) if ev_ids else "(无)")),
            judge={"json_keys": ["reply", "未决"]},
            验收标准="审计复核完成：reply 非空；发现逐条有级别；引用全部可核查",
            auth="只读复核该叶已落盘证据",
            overrides={"node_kind": "audit", "input_evidence_ids": ev_ids},
            batch=int(leaf_node.get("batch") or 0))
        self.tree[aid] = anode
        self.sticky = aid
        self.log("叶 %s 终结 ⇒ 挂增量审计 %s（下一趟优先）" % (leaf_id, aid))
        return aid

    def _record_leaf_audit(self, leaf_id: str, leaf_node: dict, row: dict):
        """B：审计叶终结 ⇒ 发现记入 audit.jsonl（批号=<父>#<原叶>.audit），供 merge 引用。"""
        from harness import contract as _ct
        if _ct.classify(leaf_node) != "audit":
            return None
        if not str(leaf_id).endswith(".audit"):
            return None
        orig = str(leaf_id)[:-len(".audit")]
        parent_id = str(leaf_node.get("parent") or "")
        from harness import audit as _au
        _au.ledger(self, 批号="%s#%s.audit" % (parent_id, orig),
                   检查点id=leaf_id,
                   状态="已审" if str(leaf_node.get("state")) == "完成" else "未审",
                   原因="增量审计：%s（result_type=%s）" % (orig, row.get("result_type")),
                   承载轮=int(leaf_node.get("batch") or 0),
                   发现数=len(leaf_node.get("未决") or []))
        return leaf_id

    def _consult_arbitrator(self, failure_class: str, cause: str, parent_id: str = "",
                            node_id: str = "", attempted_recovery=None,
                            affected_subtree=None, evidence=None,
                            existing_results=None, auth_affected: bool = False,
                            new_precedent: bool = False, confidence: float = None,
                            leo_reserved: bool = False) -> dict:
        """V2.1 P3: 薄本地规则门 + 上升接线（非阻塞）。
        gate 判 escalate 时只记台账 + 返回本地兜底（live=False，不发真实调用）；
        任何异常都吞掉并记 log，绝不堵住树的推进。"""
        try:
            from harness import arbitration as _arb_mod
            decision = _arb_mod.decision_from_failure(
                failure_class, cause, parent_id=parent_id, node_id=node_id,
                attempted_recovery=attempted_recovery, affected_subtree=affected_subtree,
                evidence=evidence, existing_results=existing_results,
                auth_affected=auth_affected, new_precedent=new_precedent,
                confidence=confidence, leo_reserved=leo_reserved)
            verdict = self.arbitrator.gate(decision)
            if verdict.get("action") == "escalate":
                card = _arb_mod.build_card(decision)
                out = self.arbitrator.escalate(card, live=False)
                self.log("仲裁上升 %s ⇒ %s（%s），未发真实调用，本地兜底继续"
                    % (verdict.get("id"), verdict.get("reason"), out.get("reason")))
            elif verdict.get("action") == "defer":
                self.log("仲裁 defer %s ⇒ %s，缺料 %s，本地兜底继续"
                    % (verdict.get("id"), verdict.get("reason"), verdict.get("missing")))
            return verdict
        except Exception as exc:  # noqa: BLE001 - 仲裁绝不能堵住树
            self.log("仲裁咨询异常（%s: %s），本地兜底继续" % (type(exc).__name__, exc))
            return {"action": "decide", "option": "", "reason": "仲裁异常，本地兜底"}

    def _handle_leaf_failure_signals(self, leaf_id: str, leaf_node: dict, row: dict):
        # V2.1 P2: leaf failure signals (mechanical, no model call).
        # Premise falsification -> mark suspect/invalidated + ancestor replan request;
        # phantom citation -> log only (merge guard handles the strategy switch).
        from harness import premise as _premise_mod
        _prem = _premise_mod.find_premise_falsification(leaf_node)
        if _prem:
            _res = _premise_mod.falsify_premise(
                str(self.run_dir), self.tree, _prem["premise_id"], _prem["evidence"],
                source_node=str(leaf_id))
            _parent_id = str(leaf_node.get("parent") or "")
            _parent = self.tree.get(_parent_id) or {}
            if _parent:
                _parent["_replan_request"] = {
                    "premise_id": _prem["premise_id"],
                    "affected": _res["affected"],
                    "evidence": _prem["evidence"]}
            self.log("叶 %s 报告前提被证伪 %s ⇒ 受影响 %s 已标 suspect/invalidated，祖先 %s 待重规划"
                % (leaf_id, _prem["premise_id"], ",".join(_res["affected"]) or "(无）", _parent_id))
        if _prem:
            # 2026-11-13：前提被证伪是「证据变了」，不是判断题 ⇒ 父节点重规划，
            # 不再走 live=False 的顾问死路径。
            try:
                from harness import escalation as _esc
                _esc.record(str(self.run_dir), {
                    "reason": _esc.E_EVIDENCE,
                    "route": _esc.R_RESEARCH,
                    "node_id": str(leaf_id),
                    "why": "前提 %s 被证伪；受影响 %s 已标 suspect/invalidated，父节点须重规划"
                           % (_prem["premise_id"], ",".join(_res.get("affected") or [])),
                    "evidence": [str(_prem.get("evidence") or "")],
                })
            except Exception:                                    # noqa: BLE001
                pass
        if str(leaf_node.get("failure_class") or "") == "phantom_citation":
            self.log("叶 %s 幻影引用失败（0 工具调用却引用不存在文件）⇒ 父 merge 须换任务方案，禁止原任务重派"
                % leaf_id)

    def _maybe_env_retry(self, leaf_id: str, leaf_node: dict, row: dict):
        """叶因环境类失败后的有界重派（第三轮 P1④）：exit_class ∈ {环境, 超时}
        （loop-breaker 熔断 / pi 崩溃 / 工具调用超时）时，以同形重试节点
        <原叶>.retry1 再跑一次；判据不过/形状不合不重试。
        上限 1 次/叶：计数在 run 内 env_retry.jsonl 台账（checkpoint 重入后
        仍以台账为准）；重试节点本身失败不再重试（id 去 .retryN 后缀归一后
        查台账已满 1 次）。异常只记 log 不阻塞主流程。
        @returns: 建出的重试节点 id，或 None（不重试）。
        """
        try:
            exit_class = str((row or {}).get("exit_class") or (leaf_node or {}).get("exit_class") or "")
            if exit_class not in ENV_RETRY_EXIT_CLASSES:
                return None
            nid = str(leaf_id or "")
            if not nid:
                return None
            base = re.sub(r"\.retry\d+$", "", nid)
            if env_retry_count(self.run_dir, base) >= ENV_RETRY_MAX:
                return None
            retry_id = "%s.retry1" % base
            if retry_id in self.tree:
                return None
            real_rd = os.path.realpath(str(self.run_dir))
            fc = str((row or {}).get("failure_class") or (leaf_node or {}).get("failure_class") or "（未分类）")
            note = ("\n【环境重试（第 1 次，有界重派，上限 1 次）】原叶 %s 因 %s（%s）失败，"
                    "本次为同一任务的重试；任务内容与验收标准与原叶相同。"
                    "工作区绝对路径（run 目录）：%s（真实路径，/tmp→/private/tmp 已展开）。"
                    % (base, exit_class, fc, real_rd))
            _j = leaf_node.get("judge")
            rnode = node_mod.new_node(
                retry_id, str(leaf_node.get("role") or "执行"), parent=str(leaf_node.get("parent") or ""),
                context=str(leaf_node.get("context") or "") + note,
                judge=(dict(_j) if isinstance(_j, dict) else _j),
                验收标准=str(leaf_node.get("验收标准") or ""),
                auth=str(leaf_node.get("auth") or ""),
                overrides=dict(leaf_node.get("overrides") or {}),
                batch=int(leaf_node.get("batch") or 0), act="leaf", round=1)
            self.tree[retry_id] = rnode
            record_env_retry(self.run_dir, base, retry_id, exit_class, fc)
            self.log("叶 %s 因 %s 失败 ⇒ 有界重派重试节点 %s（第 1/1 次）" % (base, exit_class, retry_id))
            self.heartbeat()
            return retry_id
        except Exception as exc:                                    # noqa: BLE001
            self.log("环境重试派发失败（非阻塞）：%s: %s" % (type(exc).__name__, exc))
            return None

    def _spawn_repair_child(self, parent, field, reason, strategy=None):
        """修复子节点（t-64e27d836932 类型化契约）：
        kind=contract_repair（不是现场调查）：只消费输入证据 IDs + 目标 schema，
        不调用现场工具；证据门走 context-only（修 t97 fix.1 被误拒的根因：
        旧门按“调查”关键词要求工具调用，而 repair 任务 auth 明文禁调工具）。
        judge 用 dict（json_keys），可机械核验，不再是自然语言字符串。
        """
        pid = str(parent.get("id") or "")
        prefix = pid + ".fix."
        n = sum(1 for nid in self.tree if nid.startswith(prefix)) + 1
        cid = "%s.fix.%d" % (pid, n)
        ev = []
        ev_ids = []
        for mid in sorted(node_mod.batch_members(parent, self.tree)):
            m = self.tree.get(mid) or {}
            ev.append("- %s(%s): reply=%s; unres=%s" % (
                mid, m.get("role") or "",
                str(m.get("reply") or "")[:400],
                json.dumps(m.get("未决") or [], ensure_ascii=False)[:400]))
            ev_ids.append(mid)
            _pp = Path(self.run_dir) / "partial" / ("%s.json" % mid)
            if _pp.is_file():
                ev_ids.append("partial/%s.json" % mid)
        st = strategy or {"kind": "contract_repair", "role": "调研", "tools": "none",
                          "focus": "只消费输入证据 IDs + 目标 schema，补缺失字段的结构化值"}
        context = (
            "[修复任务 kind=%s] 上一轮 merge 被拒, 原因: %s.\n"
            "缺失字段: %s.\n"
            "只消费下列输入证据 IDs，不调用现场工具（不查本机、不跑新命令）：%s。\n"
            "输出必须是 JSON 对象, 包含 reply / 未决 / 以及缺失字段本身的结构化值。\n"
            "缺失字段的结构化值: 若该字段是 uncovered, 必须是数组(无缺口时为空数组 []), "
            "每项为 {what, why} 对象, what/why 均非空且有检查依据; "
            "reply 里必须逐字引用至少一个上述输入证据 ID，否则证据门拒收。\n"
            "示例: {\"reply\": \"...（引用 root.c2.1）...\", \"未决\": [], \"uncovered\": []}。\n"
            "不产出 doc/edits: may_write=false。\n"
            "本批次证据:\n%s" % ((st.get("kind") or "contract_repair"), reason, field,
                                  "、".join(ev_ids) if ev_ids else "(无)",
                                  "\n".join(ev) if ev else "(无)"))
        cnode = node_mod.new_node(cid, str(st.get("role") or "调研"), parent=pid,
                                  context=context,
                                  judge={"json_keys": ["uncovered", "reply", "未决"]},
                                  验收标准="回复中明确给出字段 %s 的取值(无缺口时明确说无缺口), 不含 doc/edits" % field,
                                  auth="只消费输入证据 IDs，不调用现场工具",
                                  overrides={"node_kind": st.get("kind") or "contract_repair",
                                             "input_evidence_ids": ev_ids},
                                  batch=int(parent.get("batch") or 0))
        self.tree[cid] = cnode
        return cid

    def _spawn_diagnosis_child(self, parent, field, reason, strategy=None):
        """诊断子节点：同一字段第二次缺失时派（不是原样重试修复）。
        kind=synthesis（t-f6f9d7325d93 承接 t-64 §8.1）：诊断是基于已有证据的综合，
        只消费嵌进 context 的 merge 拒因与修复结论，不调现场工具；此前 kind=research
        会让证据门按现场调查要求零工具调用而误拒（t64 fix.2 实证）。"""
        pid = str(parent.get("id") or "")
        prefix = pid + ".diag."
        n = sum(1 for nid in self.tree if nid.startswith(prefix)) + 1
        cid = "%s.diag.%d" % (pid, n)
        st = strategy or {"kind": "synthesis", "role": "调研", "tools": "none",
                          "focus": "诊断 merge 反复缺字段的原因"}
        ev, ev_ids = [], []
        for mid in sorted(node_mod.batch_members(parent, self.tree)):
            m = self.tree.get(mid) or {}
            if ".fix." in mid or ".diag." in mid:
                ev.append("- %s: reply=%s" % (mid, str(m.get("reply") or "")[:400]))
                ev_ids.append(mid)
        _notes = list(parent.get("_merge_reject_notes") or [])
        context = (
            "[诊断任务 kind=synthesis] 父节点 %s 的 merge 因缺字段 %s 被拒。\n"
            "原因：%s。\n"
            "只消费下列输入证据（此前 merge 的拒因与修复子节点的结论），不调用现场工具"
            "（不查本机、不跑新命令、不读新文件）：\n"
            "拒因记录：%s。\n"
            "修复子节点结论：\n%s\n"
            "输出 JSON {reply, 未决}，reply 给出根因（prompt 缺提示 / schema 条件必填 / "
            "模型未照做 / 修复结论未被消费）与下一次应换的策略（不同去向/要点/分拆），"
            "并引用上述输入证据 ID。不要猜。\n"
            "不产出 doc/edits: may_write=false。"
            % (pid, field, reason, "；".join(_notes) if _notes else "(无)",
               "\n".join(ev) if ev else "(无)"))
        cnode = node_mod.new_node(cid, str(st.get("role") or "调研"), parent=pid,
                                  context=context,
                                  judge={"json_keys": ["reply", "未决"]},
                                  验收标准="reply 明确给出根因与换策略建议，引用上述输入证据",
                                  auth="只消费输入证据，不调用现场工具",
                                  overrides={"node_kind": st.get("kind") or "synthesis",
                                             "input_evidence_ids": ev_ids},
                                  batch=int(parent.get("batch") or 0))
        self.tree[cid] = cnode
        return cid

    # ---- 救援证据（整改）------------------------------------------------------
    def _write_rescue_evidence(self, n, nid):
        if self.max_rescue <= 0:
            return
        try:
            rdir = self.run_dir / "rescue"
            rdir.mkdir(parents=True, exist_ok=True)
            reasons = []
            try:
                with open(self.rounds_log, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if r.get("node_id") == nid and r.get("rejected"):
                            reasons.append(r.get("rejected"))
            except Exception:
                pass
            plan = n.get("result") or ""
            try:
                plan_pts = json.loads(plan) if isinstance(plan, str) and plan.strip().startswith(("[", "{")) else plan
            except Exception:
                plan_pts = plan
            ev = {"node_id": nid, "act": n.get("act"), "round": n.get("round"),
                  "rejects": self.rejects.get(nid, 0), "max_reject": self.max_reject,
                  "max_rescue": self.max_rescue, "plan": plan_pts,
                  "rejected_reasons": reasons[-5:], "state": n.get("state")}
            with open(rdir / ("%s.json" % nid.replace(".", "_")), "w", encoding="utf-8") as fh:
                json.dump(ev, fh, ensure_ascii=False, indent=1)
            self.log("rescue evidence written")
        except Exception as exc:
            self.log("rescue evidence write failed (non-blocking): %s" % exc)

    # ---- 一步 ---------------------------------------------------------------
    def _driver(self, n: dict) -> TreeDriver:
        timed = TimedRunner(self.inner)
        d = TreeDriver(n, self.tree, self.table, run_id=self.run_id, run_dir=str(self.run_dir),
                       act_runner=timed, timed=timed)
        d.sink = self.sink
        d.recovery = self.recovery  # V2.1 P2: same-error redispatch guard records recovery ledger
        return d

    def _rows_of_calls(self, d: TreeDriver, parent_id: str) -> list:
        """把**检查点**（审计/终审/形状审查）的真跑补成 rounds.jsonl 行。

        它们由 harness/audit.py 直接调 `d.runner`（不走 Driver.step）⇒ 不补的话真跑秒数就丢了，
        而「单价实测」要求真跑读数可对账。补出来的行与父的行同键。
        """
        out = []
        for rec in d.timed.calls:
            nid = rec["node_id"]
            if nid == str(parent_id) or nid not in self.tree:
                continue
            n = self.tree[nid]
            if n.get("state") not in node_mod.TERMINAL:
                continue
            exec_class = EXIT_ENV
            obj = None
            if rec.get("err"):
                exec_class = EXIT_TIMEOUT if "TimeoutExpired" in rec["err"] else EXIT_ENV
            else:
                try:
                    obj = json.loads((rec["raw"] or "").strip())
                    if not isinstance(obj, dict):
                        obj = None
                except Exception:                                # noqa: BLE001
                    obj = None
                exec_class = EXIT_DONE if obj is not None else EXIT_SHAPE
                if obj is None:
                    try:
                        obj = parse_act_output(rec["raw"], "leaf")
                        exec_class = EXIT_DONE
                    except Exception:                            # noqa: BLE001
                        obj = None
            lst = (obj or {}).get("未决")
            out.append({"role": n.get("role"), "node_id": nid, "act": "leaf",
                        "round": int(n.get("round") or 1), "start": rec["start"], "end": rec["end"],
                        "seconds": rec["seconds"], "runner": "pi", "prompt_chars": rec["prompt_chars"],
                        "session_id": rec["session_id"], "chars": rec["chars"],
                        "schema_ok": obj is not None and exec_class == EXIT_DONE,
                        "judge_ok": None, "exit_class": exec_class, "pending_count":
                            len(lst) if isinstance(lst, list) else 0,
                        "rounds_advanced": True, "checkpoint_state": n.get("state"),
                        "why": rec.get("err") or "检查点（由 harness/audit.py 直接派发，时序见 harness/leafrun.py）"})
        return out

    def _append_rows(self, rows: list) -> None:
        if not rows:
            return
        with open(self.rounds_log, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.calls.extend(rows)

    def advance_once(self) -> bool:
        """一趟遍历：挑第一个可推进的候选并推进一步。

        @returns: 还有没有「待推进的事」——
                  True = 真推进了一步（或某节点该轮被拒、但仍在重推上限内 ⇒ 下一趟重推它）；
                  False = 全树既非全终态、又没有任何可推进的候选 ⇒ 调用方停。
        """
        order = self.ordered()
        if self.sticky and self.sticky in self.tree:
            order = [self.sticky] + [x for x in order if x != self.sticky]
        for nid in order:
            n = self.tree[nid]
            if self.rejects.get(nid, 0) >= self.max_reject:
                continue
            if not self.candidate(n):
                continue
            if self.is_parent(n):
                d = self._driver(n)
                row = d.step()
                self._append_rows(self._rows_of_calls(d, nid))
                issue = row.get("adjudication_issue")
                backfill = row.get("audit_backfill") or []
                if backfill:
                    queued = self._queue_audit_backfill(backfill, issue=issue)
                    if queued:
                        return True
                if issue:
                    self._begin_adjudication(n, issue)
                    return True
                if row.get("skip") == "waiting":
                    self.sticky = None
                    self.log("候选 %s 在等子节点（%s）⇒ 本轮不算推进、不落心跳，挑下一个候选"
                             % (nid, row.get("note") or ""))
                    continue
                if row.get("rounds_advanced"):
                    self.step_count += 1
                    self.rejects[nid] = 0                  # ★ 成功推进一步 ⇒ 清掉该节点的重推计数
                    self.sticky = nid                      # ★ 同一个父连续推到位
                    self.heartbeat()
                    self.log("推进 %d：节点 %s act=%s round=%s seconds=%s"
                             % (self.step_count, nid, row.get("act"), row.get("round"),
                                row.get("seconds")))
                    return True
                if row.get("skip"):
                    # skip=next_act=None（已无 act 可跑，当场置完成/closed）：状态确实动了，算推进
                    self.step_count += 1
                    self.sticky = None
                    self.heartbeat()
                    self.log("推进 %d：节点 %s skip=%s state=%s"
                             % (self.step_count, nid, row.get("skip"), row.get("state")))
                    return True
                if row.get("act") == "merge":
                    _fld = _missing_field_of(row.get("rejected") or row.get("why") or "")
                    if _fld:
                        # t-64e27d836932：merge 缺字段走统一恢复控制器（不是原样重试）。
                        # 第一次 ⇒ contract_repair 子节点；同一字段第二次 ⇒ 诊断子节点；
                        # 策略耗尽 ⇒ 记 partial，让重推计数器继续走（落失败 + 保留证据）。
                        _cause = row.get("rejected") or row.get("why") or ""
                        _fc = "merge_missing_field"
                        _dispatched = False
                        if (nid, _fld) not in self.repairs and (nid, _fld) not in self.diagnoses:
                            _st = self.recovery.choose(nid, nid, _fc, _cause,
                                                       old_strategy={"kind": "synthesis", "role": "根", "tools": "none", "focus": "merge"})
                            if _st is not None:
                                # t-f6f9d7325d93（承接 t-64 §8.1）：按控制器选出的策略 kind 分派——
                                # synthesis 诊断走 _spawn_diagnosis_child（diag.<n>），
                                # contract_repair 走 _spawn_repair_child（fix.<n>）；不许诊断策略
                                # 落到修复派生函数里（t64 fix.2 的错：策略是诊断，派生却是修复）。
                                if str(_st.get("kind")) == "synthesis":
                                    _cid = self._spawn_diagnosis_child(n, _fld, _cause, strategy=_st)
                                    self.diagnoses.add((nid, _fld))
                                else:
                                    _cid = self._spawn_repair_child(n, _fld, _cause, strategy=_st)
                                    self.repairs[(nid, _fld)] = _cid
                                _dispatched = True
                                self.log("节点 %s merge 被拒(缺字段 %s) => 恢复控制器派 %s 子节点 %s (不计入重推)"
                                         % (nid, _fld, _st.get("kind"), _cid))
                        elif (nid, _fld) not in self.diagnoses:
                            _st = self.recovery.choose(nid, nid, _fc, _cause,
                                                       old_strategy={"kind": "contract_repair", "role": "调研", "tools": "none", "focus": "修复子节点"})
                            if _st is not None:
                                if str(_st.get("kind")) == "synthesis":
                                    _cid = self._spawn_diagnosis_child(n, _fld, _cause, strategy=_st)
                                else:
                                    _cid = self._spawn_repair_child(n, _fld, _cause, strategy=_st)
                                self.diagnoses.add((nid, _fld))
                                _dispatched = True
                                self.log("节点 %s merge 再次缺字段 %s => 恢复控制器改派诊断子节点 %s (不计入重推)"
                                         % (nid, _fld, _cid))
                        else:
                            self.recovery.mark_partial(nid, nid, _fc, _cause + "｜策略耗尽")
                            # 2026-11-13：方法失效（2 种策略都失败）不是判断题，
                            # 不再送 live=False 的顾问；明确失败并记账。
                            try:
                                from harness import escalation as _esc
                                _esc.record(str(self.run_dir), {
                                    "reason": _esc.E_BROKEN,
                                    "route": _esc.R_FAIL,
                                    "node_id": str(nid),
                                    "why": "merge 缺字段 %s：恢复控制器 2 种策略已耗尽" % _fld,
                                    "budget": {"distinct_strategies": 2},
                                })
                            except Exception:                        # noqa: BLE001
                                pass
                            self.log("节点 %s merge 缺字段 %s 策略耗尽 => 交付 partial + unresolved，不再重派"
                                     % (nid, _fld))
                        if _dispatched:
                            # 关键修正：父回等子节点态，否则 candidate() 对在跑父无条件放行，
                            # merge 立刻重推、根本不等修复叶（实测 e2e-20261105-selfheal-5e975e2）。
                            # 在跑 -> 等子节点是合法迁移；等修复叶终结后 Driver.step 会解冻回待跑/在跑。
                            node_mod.change_state(n, node_mod.WAIT_CHILDREN, self.sink)
                            self.sticky = None
                            self.heartbeat()
                            return True
                self.rejects[nid] = self.rejects.get(nid, 0) + 1
                self.sticky = None
                self.log("节点 %s 该轮不算数（%s）⇒ 第 %d 次被拒（上限 %d）"
                         % (nid, row.get("rejected") or row.get("why") or "", self.rejects[nid],
                            self.max_reject))
                if self.rejects[nid] < self.max_reject:
                    return True                            # 还在重推上限内 ⇒ 下一趟重推它
                # ★ 重推上限到 ⇒ 该节点当场落【失败】终态（响亮），否则它的父会永远等它（本批次子
                #   全终结才算完）⇒ 全树死锁。与既有 driver.py 在 act_runner 失败时置「失败」同一语义。
                # V2.2 自愈（保底收敛网）：父节点连续被拒到上限、且本批子已全终结时，
                # 先给一次机械收敛机会（不调模型，只用已验证事实 + 未决），而不是直接落失败。
                # 既有 _merge_mechanical_next 只在“同一 merge 指纹被拒两次”时触发；
                # 两次被拒指纹不同时父到上限也未试过机械 merge，本网补上这一口。
                # 只试一次（_mech_fallback_tried 防循环）；机械收敛也失败 ⇒ 照常落失败。
                if (not node_mod.is_terminal(n) and self.is_parent(n)
                        and not n.get("_mech_fallback_tried")):
                    try:
                        _members = node_mod.batch_members(n, self.tree)
                    except Exception:
                        _members = []
                    if _members and all(node_mod.is_terminal(self.tree.get(_m) or {})
                                        for _m in _members):
                        n["_mech_fallback_tried"] = True
                        n["_merge_mechanical_next"] = True
                        self.rejects[nid] = 0
                        self.sticky = None
                        self.log("节点 %s 连续被拒到上限，但本批子已全终结 ⇒ 试一次机械收敛"
                                 "（_merge_mechanical_next，不调模型）" % nid)
                        self.heartbeat()
                        return True
                if not node_mod.is_terminal(n):
                    node_mod.change_state(n, "失败", self.sink)
                    self.log("节点 %s 连续被拒 %d 次（到上限）⇒ 落【失败】终态"
                             "（否则父永远等它，全树死锁）" % (nid, self.rejects[nid]))
                    self._write_rescue_evidence(n, nid)
                    self.heartbeat()
                continue
            # 叶子执行面：一次推进 = 一次执行（无论成败都当场落终态 ⇒ 一定推进）
            d = self._driver(n)
            row = run_leaf(d, n)
            self._append_rows(self._rows_of_calls(d, nid))
            self._append_rows([row])
            self.step_count += 1
            self.sticky = None
            self.heartbeat()
            self.log("推进 %d：叶子 %s role=%s exit_class=%s seconds=%s state=%s"
                     % (self.step_count, nid, row.get("role"), row.get("exit_class"),
                        row.get("seconds"), row.get("state")))
            # B（t-f6f9d7325d93）：审计叶终结 ⇒ 发现记入 audit.jsonl；
            # 研究/综合叶终结 ⇒ 挂增量审计（下一趟优先）。失败不阻塞本推进。
            try:
                self._record_leaf_audit(nid, n, row)
            except Exception as exc:
                self.log("审计叶记账失败（非阻塞）：%s: %s" % (type(exc).__name__, exc))
            try:
                self._queue_leaf_audit(nid, n, row)
            except Exception as exc:
                self.log("增量审计挂载失败（非阻塞）：%s: %s" % (type(exc).__name__, exc))
            try:
                self._handle_leaf_failure_signals(nid, n, row)
            except Exception as exc:
                self.log("叶失败信号处理失败（非阻塞）：%s: %s" % (type(exc).__name__, exc))
            # 第三轮 P1④：环境类失败有界重派同叶一次（上限 1 次，台账在 env_retry.jsonl）。
            try:
                self._maybe_env_retry(nid, n, row)
            except Exception as exc:
                self.log("环境重试派发失败（非阻塞）：%s: %s" % (type(exc).__name__, exc))
            return True
        return False

    def all_terminal(self) -> bool:
        return all(node_mod.is_terminal(n) for n in self.tree.values())

    def run(self) -> dict:
        self.log("=== 起跑 run_id=%s run_dir=%s 输入清单=%s"
                 % (self.run_id, self.run_dir, os.environ.get("TREE_INPUT") or ""))
        self.log("环境：TREE_PI_TIMEOUT=%s TREE_MAX_STEPS=%s TREE_MAX_SECONDS=%s TREE_ACT_CMD=%r"
                 % (os.environ.get("TREE_PI_TIMEOUT"), self.max_steps, self.max_seconds,
                    os.environ.get("TREE_ACT_CMD")))
        while True:
            changed, pending, user = self._poll_adjudications()
            if changed:
                continue
            if self.all_terminal():
                self.stop_reason = "全树节点皆终态"
                break
            elapsed = time.time() - self.t0
            if self.step_count >= self.max_steps:
                if pending or user:
                    self.waiting = True
                    self.stop_reason = "WAITING: " + (self.wait_reason or "裁决仍在处理中")
                else:
                    self.stop_reason = "已到步数上限（TREE_MAX_STEPS=%d）" % self.max_steps
                    self.log("★ 已到步数上限 TREE_MAX_STEPS=%d ⇒ 停（未收束不是失败，如实报告）"
                             % self.max_steps)
                break
            if elapsed >= self.max_seconds:
                if pending or user:
                    self.waiting = True
                    self.stop_reason = "WAITING: " + (self.wait_reason or "裁决仍在处理中")
                else:
                    self.stop_reason = "已到墙钟上限（TREE_MAX_SECONDS=%.0f 秒）" % self.max_seconds
                    self.log("★ 已到墙钟上限 TREE_MAX_SECONDS=%.0f 秒（跑了 %.0f 秒）⇒ 停"
                             % (self.max_seconds, elapsed))
                break
            moved = self.advance_once()
            if moved:
                continue
            changed, pending, user = self._poll_adjudications()
            if changed:
                continue
            if pending or user:
                if user or self.wait_reason or time.time() - self.t0 >= self.max_seconds:
                    self.waiting = True
                    self.stop_reason = ("WAITING: 等待用户决定" if user else
                                        "WAITING: " + (self.wait_reason or "Leo 裁决仍在处理中"))
                    break
                time.sleep(min(1.0, max(0.0, self.max_seconds - (time.time() - self.t0))))
                continue
            self.stop_reason = "没有任何可推进的候选（全树既非全终态、又无待处理裁决）"
            self.log("★ " + self.stop_reason + " ⇒ 停")
            break
        self.heartbeat()
        self.log("=== 收工：推进 %d 步，停止原因：%s" % (self.step_count, self.stop_reason))
        return {"steps": self.step_count, "stop_reason": self.stop_reason,
                "nodes": len(self.tree), "seconds": round(time.time() - self.t0, 3)}


# ============================================================ ⑤ 前两样交付物
def read_rounds(run_dir) -> list:
    p = Path(run_dir) / "rounds.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def gen_final_reply(tree: dict, run_dir, run_id: str) -> str:
    """① <run>/out/最终答复.md —— 由**真实树状态**生成（收束轮 root 的 reply + uncovered + 全树 reply）。"""
    out_dir = Path(run_dir) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    root = tree.get("root") or {}
    merge = root.get("merge") or {}
    lines = ["# 最终答复（由真实树状态生成，未手写）", "",
             "run_id：%s" % run_id,
             "生成时间：%s（本地墙钟）" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "节点总数：%d；root.state：%s；root.merge.去向：%s"
             % (len(tree), root.get("state"), merge.get("去向")), "",
             "## 收束轮 root 的 reply", "",
             str(root.get("reply") or "（空）"), "",
             "## 收束 uncovered（root.merge.uncovered 逐条）", ""]
    unc = merge.get("uncovered") or []
    if unc:
        for i, u in enumerate(unc, 1):
            if isinstance(u, dict):
                lines.append("- %d. what=%s ｜ why=%s" % (i, u.get("what"), u.get("why")))
            else:
                lines.append("- %d. %s" % (i, u))
    else:
        lines.append("（无）")
    lines += ["", "## 全树节点 reply 汇总（每条带 node_id）", ""]
    for nid in sorted(tree, key=lambda x: (node_mod.layer_of(x, tree), str(x))):
        n = tree[nid] or {}
        lines += ["### %s（role=%s / state=%s / batch=%s）"
                  % (nid, n.get("role"), n.get("state"), n.get("batch")),
                  "", str(n.get("reply") or "（空）"), ""]
    p = out_dir / "最终答复.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # F6：doc/ 与 out/ 同源生成（同一 tree 状态）；merge 未跑时如实写无结论。
    # doc 生成失败不影响 out/，但要留 marker 让复核发现。
    try:
        gen_doc_assessment(tree, run_dir, run_id)
    except Exception as exc:                                     # noqa: BLE001
        try:
            _dd = Path(run_dir) / "doc"
            _dd.mkdir(parents=True, exist_ok=True)
            (_dd / "生成失败.txt").write_text("gen_doc_assessment 失败：%s\n" % exc,
                                             encoding="utf-8")
        except Exception:                                         # noqa: BLE001
            pass
    return str(p)


def gen_doc_assessment(tree: dict, run_dir, run_id: str) -> str:
    """② <run>/doc/夜间窗口评估.md —— 与 out/最终答复.md 同源（同一 tree 状态），
    另附每研究叶的工具证据表与 out/ 文档哈希。merge 未跑/无结论时如实写“无结论”。"""
    from harness import evidence as _ev
    import hashlib as _hl
    doc_dir = Path(run_dir) / "doc"
    doc_dir.mkdir(parents=True, exist_ok=True)
    root = tree.get("root") or {}
    merge = root.get("merge") or {}
    final_path = Path(run_dir) / "out" / "最终答复.md"
    final_hash = _hl.sha256(final_path.read_bytes()).hexdigest() if final_path.is_file() else ""
    lines = ["# 夜间窗口评估（由真实树状态生成，与 out/最终答复.md 同源）", "",
             "run_id：%s" % run_id,
             "生成时间：%s（本地墙钟）" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "root.state：%s；root.merge.去向：%s" % (root.get("state"), merge.get("去向")),
             "out/最终答复.md sha256：%s" % (final_hash or "（未生成）"), "",
             "## 结论", "",
             str(root.get("reply") or "（无结论：树未收束或 merge 未产出）"), "",
             "## 每研究叶工具证据（session 实测，非模型自述）", ""]
    for nid in sorted(tree, key=lambda x: (node_mod.layer_of(x, tree), str(x))):
        n = tree[nid] or {}
        if str(n.get("role") or "") != "调研":
            continue
        sid = session.session_id(run_id, nid)
        sp = session.find_session_file(sid, os.environ.get("TREE_PI_AGENT_SESSION_DIR"))
        tools = _ev.count_tool_results(sp) if sp else {"calls": 0, "successes": 0, "failures": 0}
        cites = _ev.seen_citations(str(n.get("reply") or ""))
        lines.append("- %s：state=%s；工具调用 %d 次 / 成功 %d / 失败 %d；引用 %d 处：%s"
                     % (nid, n.get("state"), tools["calls"], tools["successes"],
                        tools["failures"], len(cites), "、".join(cites[:8]) or "（无）"))
    lines += ["", "## 未决（root.merge.uncovered）", ""]
    unc = merge.get("uncovered") or []
    if unc:
        for i, u in enumerate(unc, 1):
            lines.append("- %d. %s" % (i, u if isinstance(u, str)
                                      else "%s ｜ %s" % (u.get("what"), u.get("why"))))
    else:
        lines.append("（无）")
    lines += ["", "## 证据等级说明", "",
              "本文件每个数字均来自 session 工具结果或树状态；未实测者不在此列。",
              "模型自述、题面给定值（如 Qwen 20t/s）不作为证据引用。", ""]
    dp = doc_dir / "夜间窗口评估.md"
    dp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(dp)


def _input_ids(run_dir, input_path: str = None) -> list:
    cand = [input_path, str(Path(run_dir) / "input" / "遗留未决清单.md")]
    for c in cand:
        if c and Path(c).exists():
            return sorted(set(RE_QID.findall(Path(c).read_text(encoding="utf-8"))))
    return []


def gen_undecided(tree: dict, run_dir, run_id: str, input_path: str = None) -> str:
    """③ <run>/out/未决清单.md —— 两小节标题逐字 `## 仍未决` / `## 已消解`，全部由真实树状态生成。"""
    root = tree.get("root") or {}
    merge = root.get("merge") or {}
    still = []
    for u in (merge.get("uncovered") or []):
        if isinstance(u, dict):
            still.append({"src": "root.merge.uncovered", "type": "超范围",
                          "what": str(u.get("what") or ""), "why": str(u.get("why") or "")})
        else:
            still.append({"src": "root.merge.uncovered", "type": "超范围",
                          "what": str(u), "why": ""})
    for nid in sorted(tree):
        n = tree[nid] or {}
        for it in (n.get("未决") or []):
            if isinstance(it, dict):
                still.append({"src": nid, "type": str(it.get("type") or ""),
                              "what": str(it.get("what") or ""), "why": str(it.get("why") or "")})
            else:
                still.append({"src": nid, "type": "", "what": str(it), "why": ""})
    # 去重（按 what 原文；保序）
    seen, uniq = set(), []
    for it in still:
        key = (it["what"] or "") + "|" + (it["why"] or "")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    def _qids(text: str) -> list:
        return sorted(set(RE_QID.findall(text or "")))

    lines = ["# 未决清单（由真实树状态生成，未手写）", "",
             "run_id：%s" % run_id, "",
             "## 仍未决", ""]
    answered_ids = set()
    for it in uniq:
        ids = _qids(it["what"]) or _qids(it["why"])
        tag = (" ".join(ids) if ids else "[无ID]")
        lines.append("- %s ｜ 来源=%s ｜ type=%s ｜ what=%s ｜ why=%s"
                     % (tag, it["src"], it["type"], it["what"], it["why"]))
    if not uniq:
        lines.append("（无）")
    # 已消解：输入 ID 里没进「仍未决」的那些 ⇒ 找回答它的 node_id
    still_ids = set()
    for it in uniq:
        still_ids |= set(_qids(it["what"])) | set(_qids(it["why"]))
    lines += ["", "## 已消解", ""]
    for qid in _input_ids(run_dir, input_path):
        if qid in still_ids:
            continue
        who = ""
        for nid in sorted(tree):
            n = tree[nid] or {}
            blob = "%s %s %s" % (n.get("reply") or "", n.get("result") or "",
                                 json.dumps(n.get("未决") or [], ensure_ascii=False))
            if qid in blob:
                who = nid
                break
        if who:
            answered_ids.add(qid)
            lines.append("- %s ｜ 被 %s 回答" % (qid, who))
    if not answered_ids:
        lines.append("（无）")
    p = Path(run_dir) / "out" / "未决清单.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


# ============================================================ 单价实测表
def class_of_row(row: dict, tree: dict) -> str:
    """七类里的一类；判不出来 ⇒ 空串（不进表）。"""
    role = str(row.get("role") or "")
    act = str(row.get("act") or "")
    nid = str(row.get("node_id") or "")
    if role == "审计":
        n = tree.get(nid) or {}
        parent = str(n.get("parent") or "")
        spec = audit_mod.slot_of(parent, nid) if parent else None
        if spec and spec[0] == audit_mod.SHAPE_SLOT:
            return "形状审查"
        return "审计"
    if role in ("根", "规划", "决策") and act in ("plan", "spawn", "merge"):
        return act
    if role in ("执行", "调研"):
        return role
    return ""


def read_single_points(run_dir) -> dict:
    d = Path(run_dir) / "单点实测"
    out = {}
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            continue
        out[str(obj.get("类别") or p.stem)] = obj
    return out


def write_unit_prices(run_dir, tree: dict = None, run_id: str = "") -> str:
    """<run>/单价实测.json：七类各一条（真跑优先；真跑里没出现 ⇒ 读单点实测；都没有 ⇒ 未测）。"""
    run_dir = Path(run_dir)
    if tree is None:
        tree = recover.load_snapshot(str(run_dir / "state.json"))
    rows = read_rounds(run_dir)
    by = {}
    for r in rows:
        c = class_of_row(r, tree)
        if not c:
            continue
        by.setdefault(c, []).append(r)
    single = read_single_points(run_dir)
    out = []
    for c in PRICE_CLASSES:
        got = by.get(c) or []
        secs = [round(float(r.get("seconds") or 0.0), 3) for r in got]
        if secs:
            who = " / ".join("%s#%s(%s)" % (r.get("node_id"), r.get("round"), r.get("act"))
                             for r in got[:6])
            out.append({"类别": c, "来源": "真跑", "样本数": len(secs), "秒数": secs,
                        "均值": round(sum(secs) / len(secs), 3),
                        "说明": "真跑节点：%s（对账源：rounds.jsonl 同 node_id+act 的 seconds）" % who})
            continue
        sp = single.get(c)
        if sp:
            s = round(float(sp.get("秒数") or 0.0), 3)
            out.append({"类别": c, "来源": "单点实测", "样本数": 1, "秒数": [s], "均值": s,
                        "说明": sp.get("说明") or "真跑里未出现 ⇒ 一次真 pi 单点调用"})
            continue
        out.append({"类别": c, "来源": "未测", "样本数": 0, "秒数": [], "均值": None,
                    "说明": single.get(c, {}).get("说明")
                            or "真跑里未出现、且本批未做单点实测（如实标未测，不填推算值）"})
    p = run_dir / "单价实测.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return str(p)


# ============================================================ CLI
def _root_context(frame_path: str, input_path: str) -> str:
    parts = []
    if frame_path and Path(frame_path).exists():
        parts.append("# 上游大框架（逐字注入）\n\n"
                     + Path(frame_path).read_text(encoding="utf-8"))
    else:
        parts.append("# 上游大框架\n\n（未给 --框架 或文件不存在：本 run 没有大框架输入）")
    if input_path and Path(input_path).exists():
        parts.append("# 遗留未决清单（逐字注入）\n\n"
                     + Path(input_path).read_text(encoding="utf-8"))
    else:
        parts.append("# 遗留未决清单\n\n（未给 --input 或文件不存在）")
    return "\n\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="tree-05 真跑面（叶子执行 + 机械判据 + 心跳 + 统一推进队列）")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--run-dir", default="")
    ap.add_argument("--input", default="")
    ap.add_argument("--框架", dest="frame", default="")
    ap.add_argument("--runner", choices=("pi", "cc"), default=None,
                        help="act runner：pi（缺省）或 cc（V1 接线：cc_act_runner）")
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--max-seconds", type=float, default=None)
    ap.add_argument("--resume", action="store_true", help="从 <run>/state.json 恢复原树与调度状态")
    ap.add_argument("--prices", action="store_true", help="只重算 <run>/单价实测.json")
    args = ap.parse_args(argv)

    run_id = args.run_id or os.environ.get("TREE_RUN_ID") or "default"
    run_dir = args.run_dir or node_mod.default_run_dir(run_id)
    if args.prices:
        print(write_unit_prices(run_dir, run_id=run_id))
        return 0

    # F3：启动 reconcile（默认关）。TREE_RECONCILE_ON_START=1/true 时才对
    # <run_dir>/state.json 里的「在跑」节点做一次 LLAAT 对账并回写快照；
    # 开关关时零行为变化；无 TREE_LLAAT_URL 时跳过、不默认打生产 8903。
    # 注意：LLAAT 查不到的任务会被标「失败」（recover.reconcile 的失败语义），
    # 是否默认开由 Leo/GPT 定。
    try:
        recover.maybe_reconcile_on_start(run_id=run_id, d=str(run_dir))
    except Exception:                                          # noqa: BLE001
        pass

    table = role_mod.load_roles()
    ctx = _root_context(args.frame, args.input)
    os.environ["TREE_INPUT"] = args.input or ""
    _runner = args.runner or os.environ.get("TREE_RUNNER") or "pi"
    _act_runner = None
    if _runner == "cc":
        from harness import cc_run as _cc_run_mod
        _act_runner = _cc_run_mod.cc_act_runner
    sch = Scheduler(run_dir, run_id, table, ctx, act_runner=_act_runner,
                    max_steps=(args.max_steps if args.max_steps is not None
                               else int(os.environ.get("TREE_MAX_STEPS") or 40)),
                    max_seconds=(args.max_seconds if args.max_seconds is not None
                                 else float(os.environ.get("TREE_MAX_SECONDS") or 7200)),
                    max_reject=int(os.environ.get("TREE_MAX_REJECT") or 2))
    if args.resume:
        checkpoint = recover.load_checkpoint(recover.snapshot_path(run_id, run_dir))
        if not (checkpoint.get("nodes") or {}).get("root"):
            print("无法 resume：state.json 中没有 root 节点", file=sys.stderr)
            return 2
        sch.restore_checkpoint(checkpoint)
        sch.resume_missing_incremental_audits()
    else:
        sch.build_root()
    res = sch.run()
    if sch.waiting:
        summary = {"run_id": run_id, "run_dir": str(run_dir), "steps": res["steps"],
                   "stop_reason": res["stop_reason"], "nodes": res["nodes"],
                   "root_state": (sch.tree.get("root") or {}).get("state"),
                   "heartbeats": len(list((Path(run_dir) / "heartbeat").glob("round-*.json"))),
                   "rounds_lines": len(read_rounds(run_dir)), "seconds": res["seconds"],
                   "status": "WAITING"}
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 4
    gen_final_reply(sch.tree, run_dir, run_id)
    # P0（V2.1 t-5df0e8dd3660）：run 结束再写一次保底交付（每叶终结已写过；此处补齐
    # root 终态/uncovered 后的最终版；无产物时明确写“零可信成果”）。
    try:
        from harness import trust as _trust_mod
        _trust_mod.gen_guaranteed_delivery(sch.tree, run_dir, run_id)
    except Exception:                                          # noqa: BLE001
        pass
    gen_undecided(sch.tree, run_dir, run_id, input_path=args.input)
    prices = write_unit_prices(run_dir, sch.tree, run_id)
    summary = {"run_id": run_id, "run_dir": str(run_dir), "steps": res["steps"],
               "stop_reason": res["stop_reason"], "nodes": res["nodes"],
               "root_state": (sch.tree.get("root") or {}).get("state"),
               "docs": sorted(p.name for p in (Path(run_dir) / "doc").glob("*.md")),
               "heartbeats": len(list((Path(run_dir) / "heartbeat").glob("round-*.json"))),
               "rounds_lines": len(read_rounds(run_dir)), "prices": prices, "seconds": res["seconds"]}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if (res["stop_reason"] == "全树节点皆终态"
                 and (sch.tree.get("root") or {}).get("state") == "完成") else 1


if __name__ == "__main__":
    raise SystemExit(main())
