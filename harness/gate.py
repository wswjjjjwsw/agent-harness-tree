"""五道硬闸 + 越权/截断的「记一笔」（76 §2.3 / §4.5.5，task-def §3.4）。

五道闸（76 §2.3）：
  ① may_spawn            role 定   ⇒ 谁能拉子（形状）
  ② max_children_per_round role   ⇒ 每轮最多拉几个（数量，硬截）
  ③ min_ctx_tokens + min_out_chars role ⇒ 任务不能太短（形状，切不碎）
  ④ nodes_max            驱动器   ⇒ 总量 80
  ⑤ max_rounds           role     ⇒ 一个父最多几轮，到数强制收束

★ 截断**必须记一笔**，不许静默（issue 验收①②）。落点 = runs/<run-id>/violations.jsonl。
★ 闸只读 role 表里的旋钮（旋钮由调用方从 role.yaml 取），本模块**不含任何 role 名字**。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 记一笔的落点（task-def §3.4）：runs/<run-id>/violations.jsonl
RUNS_DIR = os.environ.get("TREE_RUNS_DIR")          # 每次调用重读，测试可换目录
DEFAULT_RUN_ID = "default"

VIOLATION_KINDS = ("越权_children", "越权_act", "越权_overrides",
                   "截断_children", "截断_nodes_max",
                   "下限_min_ctx_tokens", "下限_min_out_chars", "max_rounds",
                   # ★ tree-03 追加（状态机三条 assert + 驱动器硬闸的违规 kind）——
                   #   上面 9 项一个字未动；本模块的函数语义一个字未动。
                   "状态机_终态", "状态机_冻结", "状态机_round",
                   "驱动_连续spawn", "驱动_产出形状",
                   # ★ tree-04 追加（审计枝 / 形状审查 / 全量终审的违规 kind）——
                   #   上面 14 项一个字未动；本模块的函数语义一个字未动。
                   "驱动_审计未回应", "审计_格式不符", "审查_越权字段", "终审_blocker_到数")


def default_sink(run_id: str = None) -> str:
    """缺省落点：$TREE_RUNS_DIR/<run-id>/violations.jsonl（TREE_RUNS_DIR 缺省 = <树根>/runs）。"""
    runs = os.environ.get("TREE_RUNS_DIR") or RUNS_DIR
    if not runs:
        root = os.environ.get("TREE_ROOT") or Path(__file__).resolve().parent.parent
        runs = str(Path(root) / "runs")
    return str(Path(runs) / (run_id or DEFAULT_RUN_ID) / "violations.jsonl")


def log_violation(path: str, kind: str, node_id: str = "", detail: str = "") -> str:
    """追加一行 JSONL（字段：ts/kind/node_id/detail），返回实际写到的路径。

    一次一条（append 模式，永不整文件重写 —— 并发写者各追加一行）。
    """
    path = path or default_sink()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": round(time.time(), 3), "kind": kind, "node_id": node_id, "detail": detail}
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return str(p)


def _knob(knobs: dict, name: str, default=None):
    v = (knobs or {}).get(name)
    return default if v is None else v


# ---- 闸① may_spawn（形状） -------------------------------------------------
def check_spawn(act: str, knobs: dict, node: dict, sink: str = None) -> tuple:
    """may_spawn=false 且 act=spawn ⇒ 拒（「你的 role 不许拉子」）。

    act=spawn 之外的 act 不归这道闸管 ⇒ 直接过。
    """
    if act != "spawn":
        return True, ""
    if _knob(knobs, "may_spawn", False):
        return True, ""
    why = ("role %r 的 may_spawn=false，不许 act=spawn（只能由 根/规划/决策 拉子，76 §2.3 闸①）"
           % (node or {}).get("role"))
    log_violation(sink, kind="越权_act", node_id=str((node or {}).get("id") or ""), detail=why)
    return False, why


# ---- 闸② max_children_per_round（轮内硬截） --------------------------------
def _child_id(child) -> str:
    if isinstance(child, dict):
        return str(child.get("id") or child.get("node_id") or child.get("role") or "?")
    return str(child)


def truncate_children(children: list, knobs: dict, sink: str = None) -> tuple:
    """每轮硬截 max_children_per_round；丢一条记一笔 kind="截断_children"。

    @returns: (收下的 list, 丢掉的 list)
    """
    limit = _knob(knobs, "max_children_per_round", 0) or 0
    children = list(children or [])
    if limit <= 0:
        # 不拉子的 role：一律不收，且逐条记一笔（同样是越权形状）
        for ch in children:
            log_violation(sink, kind="越权_children", node_id=_child_id(ch),
                          detail="role 的 max_children_per_round=0，不接受任何子")
        return [], children
    kept, dropped = children[:limit], children[limit:]
    for ch in dropped:
        log_violation(sink, kind="截断_children", node_id=_child_id(ch),
                      detail="每轮上限 %d，第 %d 个起被丢（截断必须记一笔，不许静默）"
                             % (limit, children.index(ch) + 1))
    return kept, dropped


# ---- 闸③ 下限（min_ctx_tokens / min_out_chars） ----------------------------
def check_minimum(node: dict, knobs: dict, sink: str = None) -> tuple:
    """node 的 ctx 量低于 min_ctx_tokens，或节点声明的产出下限低于 min_out_chars ⇒ 不接受。

    node 里读两个量（父/驱动器写进来的）：
      ctx_tokens     —— 这个节点的 prefill 量级（token）
      min_out_chars  —— 节点自己声明的产出下限（字数）
    缺省 0 = 无从判定 ⇒ 若该 role 的下限 > 0 则判「不接受」（宁严不松）。
    """
    node = node or {}
    nid = str(node.get("id") or "")
    need_ctx = _knob(knobs, "min_ctx_tokens", 0) or 0
    need_out = _knob(knobs, "min_out_chars", 0) or 0
    ctx = node.get("ctx_tokens")
    ctx = int(ctx) if isinstance(ctx, (int, float)) and not isinstance(ctx, bool) else 0
    if need_ctx > 0 and ctx < need_ctx:
        why = ("ctx %d token < role 下限 min_ctx_tokens=%d ⇒ 这个节点不该存在（76 §2.2）"
               % (ctx, need_ctx))
        log_violation(sink, kind="下限_min_ctx_tokens", node_id=nid, detail=why)
        return False, why
    if need_out > 0:
        declared = node.get("min_out_chars")
        got = int(declared) if isinstance(declared, (int, float)) and not isinstance(
            declared, bool) else 0
        if got < need_out:
            why = ("节点声明的产出下限 %d 字 < role 下限 min_out_chars=%d ⇒ 切得太碎，"
                   "不接受（76 §2.3 闸③）" % (got, need_out))
            log_violation(sink, kind="下限_min_out_chars", node_id=nid, detail=why)
            return False, why
    return True, ""


# ---- 闸④ nodes_max（驱动器侧总量） -----------------------------------------─
def check_nodes_max(count: int, limit: int, sink: str = None, node_id: str = "") -> tuple:
    """count >= limit ⇒ 拒（第 limit+1 个不再收），记一笔 kind="截断_nodes_max"。"""
    if limit is None or int(count) < int(limit):
        return True, ""
    why = "已有 %s 个节点，已达/超过 nodes_max=%s ⇒ 第 %s 个被拒" % (
        count, limit, int(count) + 1)
    log_violation(sink, kind="截断_nodes_max", node_id=node_id, detail=why)
    return False, why


# ---- 闸⑤ max_rounds（循环） ------------------------------------------------
def check_round(round_no: int, knobs: dict, sink: str = None, node_id: str = "") -> tuple:
    """round_no > max_rounds ⇒ (False, "到数强制收束")，记一笔 kind="max_rounds"。

    ★ 口径（与 task-def §6.1 验收 10 对齐）：max_rounds=8 时 round=8 **通过**（这是允许的最后一轮，
      它必须收束），round=9 才拒。task-def §3.4 的行内注释写的是 `>=`，与验收 10 冲突 ⇒
      以验收表为准；冲突已写进报告「待裁决」，本批不改设计稿。
    """
    limit = _knob(knobs, "max_rounds", 0) or 0
    if limit > 0 and int(round_no) > limit:
        why = "round %s > max_rounds=%s ⇒ 到数强制收束（76 §4.5.5）" % (round_no, limit)
        log_violation(sink, kind="max_rounds", node_id=node_id, detail=why)
        return False, why
    return True, ""


def check_round_must_converge(round_no: int, knobs: dict) -> bool:
    """本轮是否【必须收束】：round_no == max_rounds 的最后一轮（76 §3.2「round ≥ max_rounds
    ⇒ 驱动器强制以收束结束」）。纯判定，不记一笔（收束是正常路径，不是违规）。"""
    limit = _knob(knobs, "max_rounds", 0) or 0
    return bool(limit) and int(round_no) >= limit


def read_violations(path: str = None) -> list:
    """回放用：读回 violations.jsonl 的每一行（缺文件 ⇒ 空列表）。"""
    p = Path(path or default_sink())
    if not p.exists():
        return []
    rows = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
