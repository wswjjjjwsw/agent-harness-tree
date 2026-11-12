"""节点：20 项字段表 + 状态机 + 三条 assert（76 §3.1 / §3.2，task-def §2.1）。

★ 本模块**不含任何 role 名字的字面值**：一切从节点字段与传进来的 knobs dict 读。
★ 三条 assert 的违反一律走 harness/gate.py 的 log_violation(...)，落 runs/<run_id>/violations.jsonl。
★ 冻结用【派生判定】（state == 等子节点），不新增第 21 个字段 —— 字段表必须恰 20 项。
"""
from __future__ import annotations

import os
from pathlib import Path

from harness import gate

# ---- 20 项字段表（task-def §2.1 表逐行；顺序 = 表的行序） --------------------
FIELD_WRITERS = (
    ("id", "代码"), ("parent", "代码"), ("state", "代码"), ("result", "代码"),
    ("llaat_id", "代码"), ("context", "父"), ("judge", "父"), ("验收标准", "父"),
    ("merge", "父"), ("role", "父"), ("overrides", "父"), ("round", "代码"),
    ("act", "代码"), ("role_prompt", "role表"), ("auth", "父"), ("reply", "模型"),
    ("未决", "模型"), ("judge_ok", "LLAAT"), ("judge_why", "LLAAT"), ("batch", "代码"),
)
FIELDS = tuple(k for k, _ in FIELD_WRITERS)
FIELD_OWNER = dict(FIELD_WRITERS)

# ---- 状态机（76 §3.2，含多轮回路） -----------------------------------------
WAIT_CHILDREN = "等子节点"
WAIT_ADJUDICATION = "等裁决"
STATES = ("待跑", "在跑", "完成", "失败", WAIT_CHILDREN, WAIT_ADJUDICATION)
TERMINAL = ("完成", "失败")
# 允许的迁移边：完成/失败 = 终态，封闭（空元组）
TRANSITIONS = {
    "待跑": ("在跑", "失败"),
    "在跑": ("待跑", "完成", "失败", WAIT_CHILDREN, WAIT_ADJUDICATION),
    WAIT_CHILDREN: ("待跑", "失败"),
    WAIT_ADJUDICATION: ("待跑", "失败"),
    "完成": (),
    "失败": (),
}

# ---- 本批新增的违规 kind（追加进 gate.VIOLATION_KINDS） --------------------
NEW_VIOLATION_KINDS = ("状态机_终态", "状态机_冻结", "状态机_round",
                       "驱动_连续spawn", "驱动_产出形状")

# 新建节点的缺省值（父/模型/LLAAT 写的字段一律空，等它们填）
_EMPTY = {
    "parent": "", "state": "待跑", "result": "", "llaat_id": "", "context": "",
    "judge": None, "验收标准": "", "merge": None, "overrides": {}, "round": 0,
    "act": "", "role_prompt": "", "auth": "", "reply": "", "未决": [],
    "judge_ok": None, "judge_why": "", "batch": 0,
}


def sink_for(run_dir_or_file: str) -> str:
    """把一个 run 目录（或直接一个 jsonl 路径）归一成 violations.jsonl 的落点。"""
    p = Path(run_dir_or_file)
    if p.suffix == ".jsonl":
        return str(p)
    return str(p / "violations.jsonl")


def default_run_dir(run_id: str = None) -> str:
    """缺省 run 目录：$TREE_RUNS_DIR/<run_id>（TREE_RUNS_DIR 缺省 = <树根>/runs）。"""
    runs = os.environ.get("TREE_RUNS_DIR")
    if not runs:
        runs = str(Path(os.environ.get("TREE_ROOT") or Path(__file__).resolve().parent.parent)
                   / "runs")
    return str(Path(runs) / (run_id or gate.DEFAULT_RUN_ID))


def new_node(node_id: str, role: str, parent: str = "", **kw) -> dict:
    """建一个节点：20 项字段齐全（缺省的填空值），其余由调用方覆盖。"""
    node = dict(_EMPTY)
    node["id"] = str(node_id)
    node["role"] = str(role)
    node["parent"] = str(parent or "")
    node["未决"] = []
    for k, v in (kw or {}).items():
        if k not in FIELDS:
            raise KeyError("字段表里没有 %r（恰 20 项：%s）" % (k, list(FIELDS)))
        node[k] = v
    return node


def validate_fields(node: dict) -> list:
    """@returns: 错误列表（空 = 形状合规：恰 20 项、无未知键）。"""
    errs = []
    if not isinstance(node, dict):
        return ["节点必须是映射，实际 %r" % type(node).__name__]
    missing = [k for k in FIELDS if k not in node]
    unknown = [k for k in node if k not in FIELDS]
    if missing:
        errs.append("缺字段：%s" % missing)
    if unknown:
        errs.append("未知字段（字段表恰 20 项）：%s" % unknown)
    return errs


def is_terminal(node: dict) -> bool:
    return (node or {}).get("state") in TERMINAL


# ---------------------------------------------------------------- assert ①
def check_transition(node: dict, new_state: str, sink: str = None) -> tuple:
    """assert① 终态集 = {完成, 失败}，封闭。

    三种拒绝：① 目标状态不在状态集（= 出现了第三种终态）；② 当前已是终态（封闭，不许再动）；
    ③ 迁移边不存在（状态机外的跳法）。
    """
    nid = str((node or {}).get("id") or "")
    cur = (node or {}).get("state")
    if new_state not in STATES:
        why = ("状态 %r 不在终态集/状态集 %s ⇒ 出现了第三种终态，拒（assert①）"
               % (new_state, list(STATES)))
        gate.log_violation(sink, kind="状态机_终态", node_id=nid, detail=why)
        return False, why
    if cur not in STATES:
        why = "当前状态 %r 不在状态集 %s ⇒ 拒（assert①）" % (cur, list(STATES))
        gate.log_violation(sink, kind="状态机_终态", node_id=nid, detail=why)
        return False, why
    if cur in TERMINAL:
        why = ("当前状态 %r 是终态（终态集 %s 封闭）⇒ 不许再迁移到 %r（assert①）"
               % (cur, list(TERMINAL), new_state))
        gate.log_violation(sink, kind="状态机_终态", node_id=nid, detail=why)
        return False, why
    if new_state not in TRANSITIONS[cur]:
        why = "迁移 %s → %s 不在状态机里（允许：%s）（assert①）" % (cur, new_state, list(TRANSITIONS[cur]))
        gate.log_violation(sink, kind="状态机_终态", node_id=nid, detail=why)
        return False, why
    return True, ""


def change_state(node: dict, new_state: str, sink: str = None) -> tuple:
    """过 assert① 才改 node["state"]。@returns: (ok, why)。"""
    ok, why = check_transition(node, new_state, sink)
    if ok:
        node["state"] = new_state
    return ok, why


# ---------------------------------------------------------------- assert ②
def layer_of(node_id: str, tree: dict) -> int:
    """层号 = 到根的距离（根节点 = 0）。带环保护（坏数据不许把驱动器挂死）。"""
    depth, seen, cur = 0, set(), node_id
    while True:
        if cur in seen:
            return depth
        seen.add(cur)
        n = (tree or {}).get(cur) or {}
        par = n.get("parent") or ""
        if not par or par not in (tree or {}):
            return depth
        depth += 1
        cur = par


def layer_frozen(layer: int, tree: dict) -> bool:
    """该层是否被冻结：本层只要有节点在「等子节点」⇒ 冻结（派生判定，不占字段）。"""
    for nid in (tree or {}):
        if layer_of(nid, tree) == int(layer) and (tree[nid] or {}).get("state") == WAIT_CHILDREN:
            return True
    return False


def check_write(node_id: str, tree: dict, sink: str = None) -> tuple:
    """assert② 进「等子节点」⇒ 该层写权限冻结（冻结期间该层的落盘/edits 一律拒 + 记一笔）。

    收拢完成后（该层不再有「等子节点」）自动解冻 —— 冻结是派生状态。
    """
    layer = layer_of(node_id, tree)
    if layer_frozen(layer, tree):
        why = ("第 %d 层冻结中（本层有节点在「等子节点」）⇒ 该层的落盘/edits 一律拒"
               "（assert②；父收拢完成后解冻）" % layer)
        gate.log_violation(sink, kind="状态机_冻结", node_id=str(node_id), detail=why)
        return False, why
    return True, ""


def frozen_layers(tree: dict) -> list:
    """当前被冻结的层号列表（自检/报告用）。"""
    return sorted({layer_of(nid, tree) for nid in (tree or {})
                   if (tree[nid] or {}).get("state") == WAIT_CHILDREN})


# ---------------------------------------------------------------- assert ③
def check_round_step(node: dict, new_round: int, knobs: dict, sink: str = None,
                     forced: bool = False) -> tuple:
    """assert③ 父未达终态前 round 单调递增且 ≤ max_rounds（回退/跳号/超限 ⇒ 拒 + 记一笔）。

    ★ forced=True 是 task-def §2.1.1(B) 的口径：第 max_rounds+1 轮**只允许**作为强制收束的 merge，
      所以那**一轮**（恰 max_rounds+1，不多不少）允许越限；更后的轮仍拒。
    """
    nid = str((node or {}).get("id") or "")
    cur = (node or {}).get("round")
    if not isinstance(cur, int) or isinstance(cur, bool) or cur < 0:
        why = "当前 round 不是非负整数：%r（assert③）" % (cur,)
        gate.log_violation(sink, kind="状态机_round", node_id=nid, detail=why)
        return False, why
    if int(new_round) <= cur:
        why = "round 回退：%s → %s（断言：父未达终态前单调递增）（assert③）" % (cur, new_round)
        gate.log_violation(sink, kind="状态机_round", node_id=nid, detail=why)
        return False, why
    if int(new_round) != cur + 1:
        why = "round 跳号：%s → %s（只许 +1）（assert③）" % (cur, new_round)
        gate.log_violation(sink, kind="状态机_round", node_id=nid, detail=why)
        return False, why
    limit = (knobs or {}).get("max_rounds") or 0
    if limit and int(new_round) > int(limit):
        if not forced:
            why = ("round %s > max_rounds=%s 且本轮不是「强制收束的 merge」⇒ 拒（assert③）"
                   % (new_round, limit))
            gate.log_violation(sink, kind="状态机_round", node_id=nid, detail=why)
            return False, why
        if int(new_round) > int(limit) + 1:
            why = ("round %s 超过 max_rounds+1=%s（强制收束只允许那一轮）⇒ 拒（assert③ / §2.1.1(B)）"
                   % (new_round, int(limit) + 1))
            gate.log_violation(sink, kind="状态机_round", node_id=nid, detail=why)
            return False, why
    return True, ""


def advance_round(node: dict, knobs: dict, sink: str = None, forced: bool = False) -> tuple:
    """过 assert③ 才 +1。@returns: (ok, new_round, why)。"""
    cand = int((node or {}).get("round") or 0) + 1
    ok, why = check_round_step(node, cand, knobs, sink, forced=forced)
    if not ok:
        return False, (node or {}).get("round"), why
    node["round"] = cand
    return True, cand, ""


# ---------------------------------------------------------------- 本批次口径
def subtree_ids(node_id: str, tree: dict) -> list:
    """节点自身 + 它下面所有后代（含孙）的 id。"""
    out, stack = [], [str(node_id)]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.append(cur)
        for nid, n in (tree or {}).items():
            if str((n or {}).get("parent") or "") == cur:
                stack.append(str(nid))
    return out


def batch_members(parent: dict, tree: dict) -> list:
    """本批次的子 = 父子树内 batch == 父.batch 的节点（不含父自身）。

    ★ 本批次口径（task-def §2.1.1(A)）：只认 batch 号那一批；
      更早批次的子（甚至还在跑）不计入，别人挂上来的也不计入。
    """
    pid = str((parent or {}).get("id") or "")
    b = (parent or {}).get("batch")
    if not pid or not b:
        return []
    return [nid for nid in subtree_ids(pid, tree)
            if nid != pid and (tree.get(nid) or {}).get("batch") == b]


def ready_to_merge(parent: dict, tree: dict) -> tuple:
    """本批次的子全【终态】⇒ True（等的是终结，不是完成 —— 三终态语义）。"""
    members = batch_members(parent, tree)
    if not members:
        return False, "本批次（batch=%s）没有任何子节点：父还没拉过这一批" % (parent or {}).get("batch")
    pending = [nid for nid in members if not is_terminal(tree.get(nid) or {})]
    if pending:
        return False, ("本批次（batch=%s）还有 %d 个子未终结：%s"
                       % ((parent or {}).get("batch"), len(pending), sorted(pending)))
    return True, "本批次（batch=%s）%d 个子全终态" % ((parent or {}).get("batch"), len(members))
