"""审计枝：代码挂 / 扁平 / 分批抽检 / 承载 / 未审账（76 §5.1 §5.2 §5.4，task-def §2.1 §3.1~§3.10）。

四件事（task-def §2.1）：
  1. 代码挂   —— 父的 act=spawn 轮落子之后，由本模块**直接往 tree 里写**审计节点（不经父的 children[]）。
  2. 分批抽检 —— 本批次子数（只数 role != 审计）≥ 3 ⇒ 挂 1 个审计点（每批至多 1 个）。
  3. 承载     —— 把发现整理成「待回应清单」，注入【紧接着的那一轮】merge（§3.3 的 对审计发现）。
  4. 未审账   —— 每批一行落 runs/<run-id>/audit.jsonl（已审 / 未审 / 审失败），供终审当输入。

★ 三条硬口径（照 §3 实现，不许自创）：
  · 计数只读 `node.batch_members(父, tree)`，再过滤 role == 审计（§3.2；不另写子树遍历）。
  · 审计/检查点节点的 `batch=0` ⇒ `batch_members` 天然永不含它们（§3.4）。
  · 派发**复用驱动器的 act_runner**（`d.runner(...)`，签名同 driver.act_runner_default）；
    本模块**不另起第二套派发**（不 subprocess、不直接 import session 跑 run_act）——
    桩模式下天然不起 pi，真跑模式下天然走既有串行锁与隔离配置面（§3.5）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from harness import gate, node as node_mod, role as role_mod, schema, submit

# ---- 钉死的常量（§3.2 / §3.5 / §3.6） --------------------------------------
AUDIT_ROLE = "审计"          # 唯一的 role 字面值：它是本模块的语义锚（§3.2「口径简化」）
BLOCKER_WORD = "BLOCKER"     # 未决[].what 首词 == BLOCKER（大写逐字）⇒ 该条级别 = blocker
NOTE_WORD = "NOTE"           # 首词 == NOTE ⇒ 级别 = 一般
SAMPLE_MIN = 3               # 每批满 3 ⇒ 拉一次抽检（§5.1①）
SAMPLE_SLOT = "a"            # 抽检检查点 id 槽位：<父id>.a<触发轮>.<序号>
SHAPE_SLOT = "s"             # 形状审查检查点 id 槽位：<父id>.s<触发轮>.<序号>
FINAL_SLOT = "f"             # 全量终审检查点 id 槽位：<父id>.f<触发轮>.<序号>
CHECKPOINT_SLOTS = (SAMPLE_SLOT, SHAPE_SLOT, FINAL_SLOT)

# 派给检查点的格式要求（§3.5：prompt_extra 里只写格式，不写业务）
PROMPT_EXTRA_SAMPLE = (
    "\n## 本轮格式要求（只写格式，不写业务）\n"
    "只输出 {\"reply\": \"…\", \"未决\": [{\"type\": \"缺信息|冲突|超范围|判不了\", "
    "\"what\": \"…\", \"why\": \"…\"}]}；\n"
    "每条 未决[].what 的【首词】必须是 BLOCKER（阻断级）或 NOTE（一般），"
    "否则该条不会被认作发现（首词与正文之间用空格隔开）。"
)
PROMPT_EXTRA_FINAL = PROMPT_EXTRA_SAMPLE.replace(
    "\n## 本轮格式要求（只写格式，不写业务）\n",
    "\n## 本轮格式要求（只写格式，不写业务）\n"
    "这是【全量终审】：重点回到 audit.jsonl 的【未审】条目（它们没被审过，是终审的主要输入）。\n")


# ---------------------------------------------------------------- 落点 / 账
def append_jsonl(path, obj: dict) -> str:
    """追加一行 JSONL（append 模式，永不整文件重写 —— 并发写者各追加一行）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return str(p)


def nodes_max(table: dict = None) -> int:
    """驱动器侧总量上限（从 role 表读，**不硬编码 80**）。"""
    t = table if isinstance(table, dict) else role_mod.load_roles()
    return int((t.get("driver") or {}).get("nodes_max") or 0)


def ledger_path(d) -> str:
    return str(Path(d.run_dir) / "audit.jsonl")


def ledger(d, 批号: str = "", 检查点id: str = "", 状态: str = "", 原因: str = "",
           承载轮: int = 0, 发现数: int = 0, blocker数: int = 0) -> str:
    """未审账：每批一行（字段逐字钉死，§3.7）。"""
    return append_jsonl(ledger_path(d), {
        "ts": round(time.time(), 3), "批号": str(批号 or ""), "检查点id": str(检查点id or ""),
        "状态": str(状态 or ""), "原因": str(原因 or ""), "承载轮": int(承载轮 or 0),
        "发现数": int(发现数 or 0), "blocker数": int(blocker数 or 0)})


def read_ledger(d) -> list:
    p = Path(ledger_path(d))
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------------------------------------------------------------- 本批次口径
def batch_siblings(parent: dict, tree: dict) -> list:
    """本批次的兄弟子节点 id（**复用** node.batch_members；再排除 role == 审计）。

    ★ §5.1⑤ + §3.2 口径简化：role == 审计 的节点 = 检查点（抽检 / 形状审查 / 终审同角色），
      所以「排除检查点」与「排除 role=审计」是同一条，不写两套判断。
    """
    return [nid for nid in node_mod.batch_members(parent, tree)
            if str((tree.get(nid) or {}).get("role") or "") != AUDIT_ROLE]


def checkpoints(parent: dict, tree: dict, slot: str = None) -> list:
    """父名下由**代码**挂的检查点：@returns [(检查点id, 槽位, 触发轮, 节点)]。"""
    pid = str((parent or {}).get("id") or "")
    out = []
    for nid in sorted(tree or {}):
        n = tree.get(nid) or {}
        if str(n.get("parent") or "") != pid:
            continue
        if str(n.get("role") or "") != AUDIT_ROLE:
            continue
        spec = slot_of(pid, nid)
        if not spec:
            continue
        if slot and spec[0] != slot:
            continue
        out.append((nid, spec[0], spec[1], n))
    return out


def slot_of(pid: str, nid: str):
    """解析检查点 id：<父id>.<槽位><触发轮>.<序号> ⇒ (槽位, 触发轮)；不符 ⇒ None。"""
    head = str(pid) + "."
    s = str(nid)
    if not s.startswith(head):
        return None
    rest = s[len(head):]
    parts = rest.split(".", 1)
    if len(parts) != 2:
        return None
    tag = parts[0]
    if len(tag) < 2 or tag[0] not in CHECKPOINT_SLOTS or not tag[1:].isdigit():
        return None
    if not parts[1].isdigit():
        return None
    return tag[0], int(tag[1:])


# ---------------------------------------------------------------- 待审内容
def build_context(parent: dict, tree: dict) -> str:
    """待审内容（§3.10 全覆盖分批）：本批次每个子产出的 reply/未决/result **逐字**，一条不丢。

    另附「本批次会计视图」—— 直接**复用** driver.batch_view（不另写一套）。
    """
    from harness import driver as driver_mod                     # 局部导入：避免 import 环
    rows = []
    for nid in sorted(batch_siblings(parent, tree)):
        c = tree.get(nid) or {}
        rows.append({"id": nid, "role": c.get("role"), "state": c.get("state"),
                     "reply": c.get("reply") or "", "未决": c.get("未决") or [],
                     "result": c.get("result") or ""})
    body = json.dumps({"本批次": (parent or {}).get("batch"), "子节点": rows},
                      ensure_ascii=False, indent=1)
    return (body
            + "\n\n## 本批次会计视图（driver.batch_view 原文）\n"
            + driver_mod.batch_view(parent, tree))


def tree_view(tree: dict) -> str:
    """全树【内容】会计视图（终审输入用；一条不丢：reply / 未决 / result 原文）。"""
    from harness import driver as driver_mod
    rows = []
    for nid in sorted(tree or {}):
        n = tree.get(nid) or {}
        row = {"id": nid, "role": n.get("role"), "state": n.get("state"),
               "parent": n.get("parent"), "reply": n.get("reply") or "",
               "未决": n.get("未决") or [], "result": n.get("result") or ""}
        adjudication = driver_mod.applied_adjudication_view(n)
        if adjudication:
            row["已应用裁决"] = adjudication
        rows.append(row)
    return json.dumps({"全树": rows}, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 检查点节点
def make_checkpoint(d, cid: str, pid: str, context: str, knobs: dict):
    """建一个检查点节点并落进 tree；**创建之前**过 nodes_max（§2.4 ②）。

    节点属性（§3.4 钉死）：role=审计、parent=触发它的父id、batch=0、act=leaf、round=1、
    llaat_id=""、state 当场跑完并落终态。
    @returns: 节点 dict；被 nodes_max 拒 ⇒ None（已记一笔 截断_nodes_max）。
    """
    from harness import gate as gate_mod
    ok, _why = gate_mod.check_nodes_max(len(d.tree), nodes_max(d.table), d.sink, cid)
    if not ok:
        return None
    node = node_mod.new_node(cid, AUDIT_ROLE, parent=str(pid), context=str(context or ""),
                             batch=0, act="leaf", round=1, llaat_id="")
    node_mod.change_state(node, "在跑", d.sink)
    d.tree[cid] = node
    return node


def finish(node: dict, state: str, d) -> tuple:
    """当场落终态（完成 / 失败 二选一）。★ 理由见报告：审计节点若停在「等子节点」会冻住那一层。"""
    if state not in node_mod.TERMINAL:
        raise ValueError("检查点只许落终态（完成/失败），收到 %r" % state)
    return node_mod.change_state(node, state, d.sink)


def run_checkpoint(d, node: dict, knobs: dict, prompt: str) -> str:
    """派发检查点：**复用** d.runner（= act_runner；桩模式不起 pi）。"""
    from harness import session as session_mod
    sid = session_mod.session_id(d.run_id, str(node.get("id") or ""))
    return d.runner(node, "leaf", knobs, prompt, sid, str(d.run_dir))


# ---------------------------------------------------------------- 发现
def drain_pending(d, parent: dict, trig: int = None) -> list:
    """F4：merge 前补审本批次（叶终结后才执行审计模型调用；发现进本轮 merge）。
    trig 缺省取父当前轮 - 1（merge 轮的前一轮 = 触发轮；pending_findings 同一口径）。
    只补审“已挂节点但未执行”的批次：节点在跑 + 无账行 ⇒ 执行它；节点不存在且
    无账行 ⇒ 走 run_sample(final=True) 全套；其余一律跳过（每批只审一次）。
    叶未全终结时不执行（排空只在 merge 前发生，届时本批已全终结）。
    @returns: 已补审的批号清单。"""
    pid = str((parent or {}).get("id") or "")
    if trig is None:
        trig = int((parent or {}).get("round") or 0) - 1
    batch_no = "%s#%d" % (pid, trig)
    if any(str(r.get("批号") or "") == batch_no for r in read_ledger(d)):
        return []
    _kids = batch_siblings(parent, d.tree)
    if not _kids:
        return []
    if any(not node_mod.is_terminal((d.tree or {}).get(k) or {}) for k in _kids):
        return []
    for _nid, _slot, _t, _node in checkpoints(parent, d.tree):
        if _t != trig or _slot != SAMPLE_SLOT:
            continue
        if node_mod.is_terminal(_node):
            return []
        _knobs = role_mod.effective(AUDIT_ROLE, None, table=d.table)
        _reviewable, _terminal_gaps = _review_gaps(d, _kids)
        _gap_note = ("部分叶无工件（终态复核，缺口本身记入审计发现）：%s" % "；".join(_terminal_gaps)) if _terminal_gaps else ""
        _execute_checkpoint(d, parent, _node, trig, batch_no, trig + 1, _knobs, _gap_note)
        return [batch_no]
    run_sample(d, parent, trig=trig, final=True)
    return [batch_no]


def parse_findings(obj: dict, cid: str, d) -> list:
    """未决[] ⇒ 发现清单（§3.5 钉死：级别靠 what 的**首词**判定，不新增契约键）。

    · 首词 == BLOCKER ⇒ 级别 blocker；· 首词 == NOTE ⇒ 级别 一般；
    · 首词既不是也两者 ⇒ **不认作发现**（丢弃该条 + 记一笔 审计_格式不符），但它**仍留在** 未决[] 里。
    """
    out, n = [], 0
    for it in (obj.get("未决") or []):
        if not isinstance(it, dict):
            continue
        what = str(it.get("what") or "").strip()
        parts = what.split(None, 1)
        head = parts[0] if parts else ""
        body = parts[1].strip() if len(parts) > 1 else ""
        if head not in (BLOCKER_WORD, NOTE_WORD):
            gate.log_violation(d.sink, kind="审计_格式不符", node_id=str(cid),
                               detail=("未决[].what 首词=%r 既不是 BLOCKER 也不是 NOTE ⇒ 该条不认作"
                                       "发现（仍留在未决[] 里，不替模型改它）" % head))
            continue
        n += 1
        out.append({"id": "[%s#%d]" % (cid, n), "item": body,
                    "级别": "blocker" if head == BLOCKER_WORD else "一般"})
    return out


def _audit_findings(node: dict) -> list:
    """从抽检/终审检查点的 result 里读回发现清单。"""
    try:
        obj = json.loads(node.get("result") or "{}")
    except Exception:                                        # noqa: BLE001
        return []
    found = obj.get("发现") if isinstance(obj, dict) else None
    return list(found) if isinstance(found, list) else []


def _shape_finding(node: dict) -> dict:
    """形状审查检查点 ⇒ 一条发现（id 串 = [<检查点id>#1]，item = 「形状审查：<理由>」）。"""
    try:
        obj = json.loads(node.get("result") or "{}")
    except Exception:                                        # noqa: BLE001
        return {}
    if not isinstance(obj, dict) or not obj.get("理由"):
        return {}
    reason = str(obj.get("理由") or "")
    return {"id": "[%s#1]" % node.get("id"), "item": "形状审查：%s" % reason, "级别": "一般",
            "建议": str(obj.get("建议") or ""), "说明": reason}


def pending_findings(parent: dict, tree: dict, current_round) -> list:
    """待回应清单（§3.6 口径）= 触发轮号 == 本轮 round − 1 的那些检查点的发现。

    ★ 触发在父的第 k 轮 spawn 之后、承载在第 k+1 轮 merge ⇒ 本轮 round=R 时取触发轮 = R-1。
    """
    trig = int(current_round or 0) - 1
    out = []
    for cid, slot, rnd, node in checkpoints(parent, tree):
        if rnd != trig:
            continue
        if slot == SAMPLE_SLOT:
            out.extend(_audit_findings(node))
        elif slot == SHAPE_SLOT:
            f = _shape_finding(node)
            if f:
                out.append(f)
    return out


def injectable(parent: dict, tree: dict, current_round) -> list:
    """注入给父 merge 的「待回应清单」（每条带 id 串 / 正文 / 说明 / 建议）。"""
    rows = []
    for f in pending_findings(parent, tree, current_round):
        rows.append({"id": str(f.get("id") or ""), "item": str(f.get("item") or ""),
                     "说明": str(f.get("说明") or ""), "建议": str(f.get("建议") or "")})
    return rows


# ---------------------------------------------------------------- 触发（② 插入点的入口）
def on_spawn(d, parent: dict) -> None:
    """父的 `act=spawn` 轮落子之后由驱动器调用（§3.3 触发时机）。

    内序**钉死**（task-def §2.4 ② / §3.9 末句，两处字面一致）：
    落子（含 nodes_max 硬截）→ **抽检触发** → **形状审查触发**。
    ★ G2 修正（owner 判决 attempt-1 §二 G2）：attempt-1 把两个 try 块写反了
      （形状审查在抽检之前），且当时给的理由（「为了让定宽夹具先过 80 硬闸」）是**本批自己发明的**，
      正文里没有这条 —— 已按正文顺序调正，理由不再保留。
    ★ 两个触发各自**独立兜底**（一个抛错不许拖走另一个），异常一律只记账不上抛（§3.7 不阻塞父收拢）。
    """
    try:
        run_sample(d, parent)
    except Exception as exc:                                 # noqa: BLE001
        _swallow(d, parent, "抽检", exc)
    try:
        from harness import shape_review as shape_mod            # 局部导入：避免 import 环
        shape_mod.on_spawn(d, parent)
    except Exception as exc:                                 # noqa: BLE001
        _swallow(d, parent, "形状审查", exc)


def _swallow(d, parent: dict, what: str, exc: Exception) -> None:
    """审计侧任何意外都不许弄坏父的多轮（§3.7「不阻塞父收拢」）—— 但必须**响亮**记账。

    ★ 记账走**已有的未审渠道**（audit.jsonl 一行 `状态=未审, 原因=审计失败：…`），不新增 kind；
      同一批号已有行时**不重复追加**（「每批一行」的口径不许被兜底破坏）。
    """
    pid = str((parent or {}).get("id") or "")
    trig = int((parent or {}).get("round") or 0)
    batch_no = "%s#%d" % (pid, trig)
    if any(r.get("批号") == batch_no for r in read_ledger(d)):
        return
    try:
        ledger(d, 批号=batch_no, 检查点id="", 状态="未审",
               原因="审计失败：%s 触发时内部异常 %s: %s" % (what, type(exc).__name__, exc),
               承载轮=trig + 1)
    except Exception:                                        # noqa: BLE001
        pass                                                 # 兜底失败也不许上抛


def _sibling_artifact(d, nid: str) -> tuple:
    """F4：该子节点有没有可审工件——终态 +（reply/result 非空 或 session 有成功工具结果）。
    @returns: (有工件, 说明)。"""
    from harness import evidence as _ev
    from harness import session as _sess
    n = (d.tree or {}).get(nid) or {}
    if not node_mod.is_terminal(n):
        return False, "未终结（state=%s）" % n.get("state")
    if (n.get("reply") or n.get("result") or ""):
        return True, "终态且有 reply/result"
    sid = _sess.session_id(d.run_id, nid)
    sp = _sess.find_session_file(sid, os.environ.get("TREE_PI_AGENT_SESSION_DIR"))
    if sp:
        tools = _ev.count_tool_results(sp)
        if tools.get("successes"):
            return True, "终态且 session 有 %d 条成功工具结果" % tools["successes"]
    return False, "终态但无工件（reply/result 为空且 session 无成功工具结果）"




def _review_gaps(d, kids: list) -> tuple:
    """F4：把一批子节点分成“可审”（终结且有工件）与“终结缺口”（终结但无工件）。
    未终结的新鲜叶（待跑/在跑）两边都不进：它的空是“还没跑”，不是缺口，排空时再审。"""
    _reviewable, _terminal_gaps = [], []
    for _kid in kids:
        _ok, _why = _sibling_artifact(d, _kid)
        if _ok:
            _reviewable.append(_kid)
        elif node_mod.is_terminal((d.tree or {}).get(_kid) or {}):
            _terminal_gaps.append("%s（%s）" % (_kid, _why))
    return _reviewable, _terminal_gaps
def _execute_checkpoint(d, parent: dict, node: dict, trig: int, batch_no: str,
                      carry: int, knobs: dict, gaps_note: str = "") -> str:
    """执行一个已挂的检查点节点（模型调用 + 发现解析 + 落终态 + 记账）。@returns: 检查点 id。

    F4：执行时用当前树重建待审内容（排空执行时叶已终结，context 不能沿用 spawn 时的快照）。"""
    from harness import driver as driver_mod
    cid = str(node.get("id") or "")
    node["context"] = build_context(parent, d.tree)
    prompt = submit.assemble_prompt(node, knobs, act="leaf") + PROMPT_EXTRA_SAMPLE
    try:
        raw = run_checkpoint(d, node, knobs, prompt)
        obj = driver_mod.parse_act_output(raw, "leaf")
        ok, why = schema.validate("leaf", obj, knobs)
        if not ok:
            raise ValueError("产出不契约：%s" % why)
    except Exception as exc:                                 # noqa: BLE001
        finish(node, "失败", d)
        ledger(d, 批号=batch_no, 检查点id=cid, 状态="未审",
               原因="审计失败：%s: %s" % (type(exc).__name__, exc), 承载轮=carry)
        return cid
    node["reply"] = str(obj.get("reply") or "")
    node["未决"] = obj.get("未决") or []                     # 模型原文，不替它改
    findings = parse_findings(obj, cid, d)
    node["result"] = json.dumps({"回复": node["reply"], "发现": findings}, ensure_ascii=False)
    finish(node, "完成", d)
    ledger(d, 批号=batch_no, 检查点id=cid, 状态="已审", 原因=gaps_note, 承载轮=carry,
           发现数=len(findings),
           blocker数=len([f for f in findings if f.get("级别") == "blocker"]))
    return cid


def run_sample(d, parent: dict, trig: int = None, final: bool = False):
    """分批抽检（§3.2 / §3.3 / §3.5 / §3.7）。@returns: 检查点 id 或 None。

    F4（挂节点与执行分离）：节点在 spawn 时就挂（结构 + nodes_max 口径不变，检查点
    state=在跑，不会被调度器当叶子执行）；模型调用只在“本批已有可审内容”时立即执行，
    否则不调模型、不留行，等排空（drain_pending）到叶终结后才执行。final=True（排空
    时）无视缺口直接执行（缺口本身记入审计发现）。"""
    from harness import driver as driver_mod
    pid = str((parent or {}).get("id") or "")
    trig = int(trig if trig is not None else (parent or {}).get("round") or 0)
    batch_no = "%s#%d" % (pid, trig)
    carry = trig + 1
    kids = batch_siblings(parent, d.tree)
    knobs = role_mod.effective(AUDIT_ROLE, None, table=d.table)

    if len(kids) < SAMPLE_MIN:
        # 保持既有口径（cases-audit.py 钉死：恰一行 未审、原因逐字）：子数不足 ⇒ 单行 未审，不建检查点。
        ledger(d, 批号=batch_no, 检查点id="", 状态="未审",
               原因="批次子数不足阈值（%d<3）" % len(kids), 承载轮=carry)
        return None

    ctx = build_context(parent, d.tree)
    est = submit.estimate_tokens(ctx)
    budget = int(knobs.get("ctx_budget") or 0)
    if budget and est > budget:
        # ★ 绝不静默截断（76 §4.6）：超 ctx_budget ⇒ 不建节点、不跑、不阻塞，只响亮记一行。
        ledger(d, 批号=batch_no, 检查点id="", 状态="未审",
               原因="审计输入超 ctx_budget（%d token > %d）" % (est, budget), 承载轮=carry)
        return None

    # F4：节点现在就挂（nodes_max 在此过；挂不上 ⇒ 记 未审，与既有口径一致）。
    cid = "%s.%s%d.1" % (pid, SAMPLE_SLOT, trig)
    node = make_checkpoint(d, cid, pid, ctx, knobs)
    if node is None:
        ledger(d, 批号=batch_no, 检查点id="", 状态="未审",
               原因="检查点创建被 nodes_max 拒（总量已到上限）", 承载轮=carry)
        return None

    # F4：本批已有可审内容、且终结叶无缺口，才立即执行；否则只挂节点、不调模型、
    # 不留行，等排空。未终结的新鲜叶不算缺口（排空时再审）。
    _reviewable, _terminal_gaps = _review_gaps(d, kids)
    if (not _reviewable or _terminal_gaps) and not final:
        return cid
    _gap_note = ("部分叶无工件（终态复核，缺口本身记入审计发现）：%s" % "；".join(_terminal_gaps)) if _terminal_gaps else ""
    return _execute_checkpoint(d, parent, node, trig, batch_no, carry, knobs, _gap_note)
