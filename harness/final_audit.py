"""全量终审：机械触发 + blocker 作废 + 回退一轮 merge + 到数兜底（76 §5.5，task-def §2.2 / §3.8）。

唯一触发判据 = `merge.去向 == "收束"`（**代码看字段，不看父的意图**）。
终审出 blocker ⇒ 该轮收束**作废**、回退成一轮 merge（父必须逐条回应）。

★ 落点在 `driver.apply_output` 的 merge 分支、且必须在 `change_state(父,"完成")` **之前**
  —— `node.TRANSITIONS` 里 完成/失败 是封闭终态（assert①），先置完成再退回就是给状态机开后门。
★ 回退成一轮 merge **不动 `next_act`**：作废路径把 `p["act"]` 显式改回 `"spawn"` 并把 `round` 回退 1
  ⇒ `next_act` 自己算出 merge、round 重新 +1 = **同一个轮号**。
★ 终止性（防死循环，第二轮 B 改动，集成验收 t-132692485362）：
  到数轮 / 作废次数上限 `FINAL_AUDIT_MAX_RETRY` 时**不再兜底通过**——blocker 未消除
  就不能仅靠上限标记根成功。驱动器改为：轮次未到数且修复预算未用完 ⇒ 自动派发
  修复叶（`FINAL_AUDIT_REPAIR_MAX` 轮为限，修复叶带机械判据）；否则父明确失败，
  blocker 仍挂在父未决上。作废上限数值本身（2 次）未改——收紧与否待 Leo 定。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from harness import audit, escalation, gate, node as node_mod, role as role_mod, schema, submit

# 同一父的收束作废次数上限（钉死，§3.8）
FINAL_AUDIT_MAX_RETRY = 2
# 作废达上限后自动修复派发的最大轮数（第二轮 B：有界选择，不是 Leo 定的语义——
# 作废上限本身 FINAL_AUDIT_MAX_RETRY=2 未改动）。
FINAL_AUDIT_REPAIR_MAX = 2
# 数值/事实冲突的修复口径：先重算或回读权威输入，判不了就上抛，不许按文本自信度选边。
# 用法见 build_repair_child；它只写进任务描述，不参与任何机械判定。
CONFLICT_RULE_LINES = (
    "## 冲突处理（本 blocker 涉及数值或事实不一致）",
    "1) 先用工具重算或回读权威输入（input/ 下登记的文件、原始数据），以重算结果定案；",
    "   不要按两份文本哪份写得更自信来选。",
    "2) 能定案：按定案值修正目标文件，并在 reply 里写出你用的命令与结果。",
    "3) 定不了案（无法重算，或重算结果与两边都不同）：不要改写目标文件，",
    "   把两边的原文数值与缺口写进 未决[]，交上级裁定。",
)

# 结构性 blocker 里的批号（"未审批次 root#2"）。

PROMPT_EXTRA_FINAL_AUDIT = (
    "\n## 本轮格式要求（只写格式，不写业务）\n"
    "这是【全量终审】：重点回到 audit.jsonl 的【未审】条目（它们没被审过，是本次终审的主要输入）。\n"
    "若出现“已应用裁决”，先核对其绑定 blocker 范围和当前证据；相同范围且证据未变时引用原裁决并留痕，不重复列同一 blocker；新证据或越出范围的问题照常判定。该记录不自动豁免问题，也不清除原未决。\n"
    "只输出 {\"reply\": \"…\", \"未决\": [{\"type\": \"缺信息|冲突|超范围|判不了|超时\", "
    "\"what\": \"…\", \"why\": \"…\"}]}；\n"
    "每条 未决[].what 的【首词】必须是 BLOCKER（阻断级）或 NOTE（一般），"
    "否则该条不会被认作发现（首词与正文之间用空格隔开）。"
    "注意：BLOCKER/NOTE 只写在 what 开头，绝不能填进 type——"
    "type 只能取上面五者之一（第四轮：模型曾把 BLOCKER/NOTE 填进 type 致终审 ValueError）。"
)

_SEQ = {"n": 0}


def _next_seq() -> int:
    """同一 run 内终审检查点的序号（跨批不撞车）。"""
    _SEQ["n"] += 1
    return _SEQ["n"]


def restore_sequence(tree: dict) -> int:
    """Resume the final-audit checkpoint sequence from persisted node IDs."""
    maximum = 0
    for node_id in (tree or {}):
        match = re.search(r"\.f\d+\.(\d+)$", str(node_id))
        if match:
            maximum = max(maximum, int(match.group(1)))
    _SEQ["n"] = maximum
    return maximum


def _canonical_sha256(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def blocker_identity(blocker: dict) -> str:
    """稳定的 blocker id；优先用 audit ledger id，旧发现退回文本摘要。"""
    bid = str((blocker or {}).get("id") or "").strip()
    if bid:
        return bid
    return "b-" + review_blocker_key(str((blocker or {}).get("item") or ""))


def verify_blockers(blockers: list, d) -> tuple:
    """终审 blocker 逐条来源核对（V2.1 P3，机械，不读语义）。
    每条 blocker 的 item 文本里出现的路径引用逐条验存在（复用 evidence.check_citations）。
    无引用可验的 blocker 视为已核验；引用了不存在路径的 ⇒ 未核验。
    @returns: (verified, unverified)。"""
    from harness import evidence as _ev_mod
    verified, unverified = [], []
    for b in (blockers or []):
        try:
            bad = _ev_mod.check_citations(str((b or {}).get("item") or ""),
                [str(d.run_dir), str(role_mod.ROOT)])
        except Exception:
            bad = []
        (unverified if bad else verified).append(b)
    return verified, unverified


# ---------------------------------------------------------------- 终审 blocker 机械再核验

def verify_structural_blocker(item: str, d, parent_id: str = "") -> tuple:
    """结构性 blocker（"未审批次 root#N"/"批次子数不足阈值"）的机械判定。

    拿真实树状态核验：批号 N 的实际子节点 = parent 的直接子节点中 batch==N 的节点；
    每子须已终结，且（role==审计且审计完成）或有完成态增量审计兄弟 <子>.audit。
    @returns: (status, detail)
      "not_structural"：不是结构性 blocker，不管；
      "dropped"：断言与事实不符——子节点身份已确定且均已审计 ⇒ 丢弃
        （抽检未跑只是批次子数不足阈值的框架口径，仍记在 audit.jsonl 里，不抹）；
      "kept"：属实（有子未终结/缺审计）⇒ 保留，detail 点名具体子节点
        （调用方把 detail 追加进 blocker 文本；具体修复规格由 repair_specs
        的失败叶回补口径产出，无可修目标时按 P1⑤ 不派空修复叶、走明确失败）。
    """
    if "未审批次" not in item and "批次子数不足" not in item:
        return "not_structural", ""
    tree = (d.tree if d is not None else {}) or {}
    pid = str(parent_id or "")
    if not pid:
        return "kept", "结构性 blocker 缺父节点 id，无法限定批次归属"
    batches = [int(x) for x in re.findall(re.escape(pid) + r"#(\d+)", item or "")]
    if not batches:
        return "kept", "结构性 blocker 未点名当前父节点 %s 的批号，无法机械核验" % pid
    problems, identities = [], []
    for bn in batches:
        members = sorted(
            nid for nid, candidate in tree.items()
            if str((candidate or {}).get("parent") or "") == pid
            and int((candidate or {}).get("batch") or 0) == bn)
        if not members:
            problems.append("批次 %s#%d 在树里无直接子节点" % (pid, bn))
            continue
        for mid in members:
            m = tree.get(mid) or {}
            state = str(m.get("state") or "")
            role = str(m.get("role") or "")
            aud = tree.get(mid + ".audit") or {}
            aud_state = str(aud.get("state") or "")
            identities.append("%s（role=%s，state=%s，增量审计=%s）"
                              % (mid, role, state, aud_state or "无"))
            if not node_mod.is_terminal(m):
                problems.append("%s 未终结（state=%s）" % (mid, state))
            elif role == audit.AUDIT_ROLE and state != "完成":
                problems.append("审计节点 %s 未完成（state=%s）" % (mid, state))
            elif role != audit.AUDIT_ROLE and aud_state != "完成":
                problems.append("%s 缺完成态增量审计（%s.audit state=%s）"
                                % (mid, mid, aud_state or "无"))
    if problems:
        return "kept", "；".join(problems)
    return ("dropped",
            "批次 %s 实际子节点均已终结且有增量审计：%s；抽检未跑是批次子数不足阈值"
            "（框架口径，见 audit.jsonl 未审行），非任务缺陷"
            % ("、".join("%s#%d" % (pid, b) for b in batches), "；".join(identities)))


def missing_incremental_audits(d) -> list:
    """未审批次里仍缺失的直接子节点增量审计（只报能由现有队列审的叶）。"""
    from harness import contract as contract_mod
    tree = (d.tree if d is not None else {}) or {}
    missing = set()
    for row in audit.read_ledger(d):
        if row.get("状态") != "未审" or "批次子数不足阈值" not in str(row.get("原因") or ""):
            continue
        parent_id, sep, batch_no = str(row.get("批号") or "").rpartition("#")
        if not sep or not parent_id or not batch_no.isdigit():
            continue
        batch = int(batch_no)
        for leaf_id, leaf in tree.items():
            if (str((leaf or {}).get("parent") or "") != parent_id
                    or int((leaf or {}).get("batch") or 0) != batch
                    or not node_mod.is_terminal(leaf)):
                continue
            if contract_mod.classify(leaf) not in ("research", "synthesis"):
                continue
            audit_node = tree.get(str(leaf_id) + ".audit") or {}
            if str(audit_node.get("state") or "") != "完成":
                if not audit_node:
                    missing.add(str(leaf_id))
    return sorted(missing)


_MISMATCH_WORDS = ("不一致", "差异", "不符", "不是", "不对", "错误", "对不上")
_MISSING_WORDS = ("不存在", "缺失", "未写", "未生成", "找不到", "没有")


def _reverify_measurable(item: str, run_dir: str) -> tuple:
    """可度量 blocker 的三项重测。@returns: (drop, note)——
    drop=True 时 note 是丢弃理由；drop=False 且 note 非空时 note 是追加给
    保留 blocker 的实测注记；两者皆空 = 无可度量项，不判（宁可保留）。"""
    from harness import evidence as _ev_mod
    cites = _ev_mod.seen_citations(item or "")
    resolved = []
    for c in cites:
        t, ta = _resolve_target(c, run_dir)
        if t and ta:
            resolved.append((t, ta))
    if not resolved:
        return False, ""
    # ② 文件存在性：称某引用文件不存在/未写，但磁盘上有且仅引用的这一个文件非空 ⇒ 过期。
    if any(w in item for w in _MISSING_WORDS) and len(resolved) == 1:
        t, ta = resolved[0]
        try:
            if os.path.getsize(ta) > 0:
                return True, "%s 磁盘上存在且非空（过期 blocker，审计未重测）" % t
        except OSError:
            pass
    # ③ 答案值：互称不一致的引用文件内容相同，或单值文件内容等于文本里唯一的要求数 ⇒ 过期。
    if any(w in item for w in _MISMATCH_WORDS):
        if len(resolved) >= 2:
            texts = []
            for _t, ta in resolved:
                try:
                    with open(ta, "r", encoding="utf-8", errors="replace") as fh:
                        texts.append(fh.read().strip())
                except OSError:
                    texts.append(None)
            if texts and all(x is not None and x != "" and x == texts[0] for x in texts):
                return True, "引用文件内容一致（%s），所谓差异已不存在" % "、".join(t for t, _ in resolved)
        elif len(resolved) == 1:
            nums = sorted(set(re.findall(r"\d+", item or "")))
            if len(nums) == 1:
                try:
                    with open(resolved[0][1], "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read().strip()
                except OSError:
                    content = None
                if content is not None and content == nums[0]:
                    return True, "%s 内容就是 %s（过期 blocker，审计未重测）" % (resolved[0][0], nums[0])
    return False, ""


def reverify_blockers(blockers, d, parent_id: str = "") -> tuple:
    """终审 blocker 事实状态机械再核验（第三轮 P0①/P0③）。

    对真实落盘文件 / 真实树状态重测可度量项；已消除的 blocker 不得再计入。
    结构性 blocker 见 verify_structural_blocker；其余见 _reverify_measurable。
    @returns: (kept, dropped)；kept 的 item 可能追加"（机械核验：…）"注记。
    """
    kept, dropped = [], []
    run_dir = str(d.run_dir) if d is not None else ""
    for b in (blockers or []):
        item = str((b or {}).get("item") or "")
        status, detail = verify_structural_blocker(item, d, parent_id)
        if status == "dropped":
            dropped.append({"item": item, "reason": detail})
            continue
        if status == "kept":
            nb = dict(b)
            nb["item"] = item + "（机械核验：%s）" % detail
            nb["reason"] = escalation.E_EVIDENCE
            kept.append(nb)
            continue
        drop, note = _reverify_measurable(item, run_dir)
        if drop:
            dropped.append({"item": item, "reason": note})
            continue
        nb = dict(b)
        if note:
            nb["item"] = item + "（%s）" % note
        # 2026-11-13：先按「为什么不能本地继续」归类，再做权威事实改写。
        # 事实冲突不走复核/Leo；能按磁盘事实改写的，直接换成可执行的派生侧描述。
        reason_row = {}
        if run_dir:
            try:
                reason_row = escalation.classify_blocker(str(nb["item"]), run_dir)
            except Exception:                                    # noqa: BLE001
                reason_row = {}
        reason = str(reason_row.get("reason") or "")
        if reason:
            nb["reason"] = reason
        if reason == escalation.E_FACT and reason_row.get("canonical_item"):
            nb["item"] = str(reason_row["canonical_item"])
            nb["authority_direction"] = {
                "authority": str(reason_row.get("authority") or ""),
                "target": str(reason_row.get("target") or ""),
            }
        kept.append(nb)
    return kept, dropped



def ledger_path(d) -> str:
    return str(Path(d.run_dir) / "final_audit.jsonl")


def ledger(d, 轮号: int, 检查点id: str, blocker数: int, 作废: bool, 兜底: str = "",
           原因: str = "") -> str:
    """终审落点日志：每跑一次一行（字段逐字钉死，§3.8）。"""
    row = {"ts": round(time.time(), 3), "轮号": int(轮号 or 0), "检查点id": str(检查点id or ""),
           "blocker数": int(blocker数 or 0), "作废": bool(作废), "兜底": str(兜底 or "")}
    if 原因:
        row["原因"] = str(原因)
    return audit.append_jsonl(ledger_path(d), row)


def read_ledger(d) -> list:
    p = Path(ledger_path(d))
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------------------------------------------------------------- 终审输入
def build_context(d) -> str:
    """「全量」= 该审计点的相关全集（§5.3 边界）：

    · `audit.jsonl` 的**全部行**（含 `未审` —— 它们必须在终审输入里，§5.1⑦）；
    · 全树**内容会计视图**（各节点 reply / 未决 / result 原文，一条不丢）。
    """
    rows = []
    for source in audit.read_ledger(d):
        row = dict(source)
        reason = str(row.get("原因") or "")
        batch = str(row.get("批号") or "")
        if row.get("状态") == "未审" and "批次子数不足阈值" in reason:
            parent_id, sep, batch_no = batch.rpartition("#")
            if sep and parent_id and batch_no.isdigit():
                item = "未审批次 %s 批次子数不足阈值" % batch
                status, detail = verify_structural_blocker(item, d, parent_id)
                if status in ("dropped", "kept"):
                    row["原状态"] = row["状态"]
                    row["机械核验"] = detail
                    if status == "dropped":
                        row["状态"] = "已机械核验"
                        row["原因"] = "%s；已核实现存子节点均终结且有增量审计，阈值差仅属抽检口径" % reason
        rows.append(row)
    lines = ["## audit.jsonl 全部行（原始账本只读；已机械核验为本次上下文视图）",
             json.dumps({"行数": len(rows), "行": rows}, ensure_ascii=False, indent=1),
             "\n## 全树内容会计视图（逐节点 reply / 未决 / result 原文，不裁剪）",
             audit.tree_view(d.tree)]
    # 2026-11-13：把权威文件的真实内容放进终审输入。审计叶/父节点可以编数值，
    # 但终审看到的必须是程序从磁盘读出来的值（B：oracle 实际 168，模型声称 186）。
    try:
        from harness import authority as _auth
        sheet = _auth.fact_sheet(str(getattr(d, "run_dir", "") or ""))
    except Exception:                                            # noqa: BLE001
        sheet = ""
    if sheet:
        lines.append(sheet)
    return "\n".join(lines)


# ---------------------------------------------------------------- 触发 + 判定
def on_converge(d, p: dict, obj: dict):
    """收束轮的落点：**只判定、不写流水**（§3.8）。

    @returns: (ok, why)。ok=False ⇒ 该轮收束作废（调用方走既有 `_reject` 路径）。
    """
    if str(obj.get("去向") or "") != "收束":
        return True, ""                                       # 唯一触发判据没命中 ⇒ 不跑终审
    rnd = int(p.get("round") or 0)
    knobs = role_mod.effective(str(p.get("role") or ""), "merge", table=d.table)
    pid = str(p.get("id") or "")
    # Hard doc gaps are mechanically known before an audit call. Route them through
    # the existing repair/terminal-failure exit even if context, checkpoint, or Sage fails.
    hard_doc_blockers = [
        {"id": "[disk-doc:%s]" % str(spec.get("repair_of") or ""),
         "item": str(spec.get("item") or ""), "级别": "blocker"}
        for spec in hard_doc_specs(d.tree, str(d.run_dir), parent=p)
    ]
    if hard_doc_blockers:
        p["_audit_cap_blockers"] = hard_doc_blockers
        p["_audit_cap_reason"] = "硬文档机械核验未达标"
        p["_audit_cap_cid"] = ""
        return False, "已知硬文档不达标 ⇒ 转修复派发/明确失败"
    ctx = build_context(d)
    est = submit.estimate_tokens(ctx)
    budget = int(knobs.get("ctx_budget") or 0)

    if budget and est > budget:
        # 绝不静默截断：超预算 ⇒ 记一行「未审」，**不当作废理由**（不许无声地把收束掐掉）
        ledger(d, rnd, "", 0, False, 原因="终审输入超 ctx_budget（%d > %d）⇒ 未审、不作废"
               % (est, budget))
        return True, "终审输入超 ctx_budget ⇒ 未审、不作废"

    cid = "%s.%s%d.%d" % (pid, audit.FINAL_SLOT, rnd, _next_seq())
    node = audit.make_checkpoint(d, cid, pid, ctx, knobs)
    if node is None:
        ledger(d, rnd, "", 0, False, 原因="检查点创建被 nodes_max 拒（总量已到上限）"
               "⇒ 未审、不作废")
        return True, "终审检查点被 nodes_max 拒 ⇒ 未审、不作废"

    # CC runner 时 role_prompt 已在 agent 文件的 system prompt 里，不重复注入
    # （见 harness/submit.py:assemble_prompt include_role_prompt；pi 路径一字不动）。
    prompt = submit.assemble_prompt(
        node, knobs, act="leaf",
        include_role_prompt=not getattr(d.runner, "is_cc", False)) + PROMPT_EXTRA_FINAL_AUDIT
    from harness import driver as driver_mod                      # 局部导入：避免 import 环
    _und_note = ""
    try:
        raw = audit.run_checkpoint(d, node, knobs, prompt)
        out = driver_mod.parse_act_output(raw, "leaf")
        # 第四轮（integ3-q01 实测）：终审检查点产出的 未决[].type 曾越出枚举
        # （模型把 what 首词 BLOCKER/NOTE 填进 type），schema.validate 直接抛
        # ValueError、整轮终审失败。按 merge/leaf 轮同一模式先机械归一再校验：
        # BLOCKER/NOTE 等非法值 → "判不了"（what/why 原样保留，不编造事实）；
        # 归一记录进检查点 result 与 ledger，终审不因此抛错。
        if isinstance(out.get("未决"), list):
            _und, _und_note = schema.normalize_model_undecided(out["未决"])
            if _und_note:
                out["未决"] = _und
        # 2026-11-13：审计 role 无写权限，模型误带 doc/edits 时丢弃字段，
        # 不让正确的终审结论被形状门整条判死。
        out, _ro_note = schema.normalize_readonly_output(out, knobs)
        if _ro_note:
            _und_note = (_und_note + "；" if _und_note else "") + _ro_note
        ok_schema, why = schema.validate("leaf", out, knobs)
        if not ok_schema:
            raise ValueError("产出不契约：%s" % why)
    except Exception as exc:                                 # noqa: BLE001
        audit.finish(node, "失败", d)
        detail = "%s: %s" % (type(exc).__name__, exc)
        ledger(d, rnd, cid, 0, False, 原因="终审失败：%s（未审、不作废收束，诚实记账）" % detail)
        return True, "终审失败 ⇒ 未审、不作废收束（诚实记账）"

    node["reply"] = str(out.get("reply") or "")
    node["未决"] = out.get("未决") or []
    blockers = audit.parse_findings(out, cid, d)
    blockers = [b for b in blockers if b.get("级别") == "blocker"]
    # V2.1 P3: blocker per-source verification (Sage batch review is not truth).
    # A blocker citing a nonexistent path -> unverified, downgraded, does NOT void convergence.
    _verified, _unverified = verify_blockers(blockers, d)
    if _unverified:
        for _b in _unverified:
            _b["级别"] = "未核验"
        ledger(d, rnd, cid, 0, False,
            原因="终审 blocker %d 条引用无法核查 ⇒ 未核验，不作废（仍记入发现）" % len(_unverified))
    blockers = _verified
    # 第三轮 P0①/P0③：终审 blocker 事实状态机械再核验——doc 长度量磁盘、
    # 答案值重算、结构性 blocker 对真实树状态。已消除的不再计入。
    blockers, _dropped = reverify_blockers(blockers, d, pid)
    # 第四轮决策 C：已通过纯复核叶确认不阻断的 blocker 不再重复计入——
    # 不是自动丢弃（A/B）：复核叶是真实跑过的独立只读叶，其结论与产物哈希
    # 均在 ledger/result 里留痕；blocker 文本仍保留在"已消除"里。
    blockers, _reviewed = apply_reviewed_drops(blockers, d, pid)
    _dropped = list(_dropped) + list(_reviewed)
    _result = {"回复": node["reply"],
               "发现": blockers + _unverified,
               "已消除": _dropped}
    if _und_note:
        _result["归一"] = _und_note
    node["result"] = json.dumps(_result, ensure_ascii=False)
    audit.finish(node, "完成", d)

    if not blockers:
        _reasons = []
        if _dropped:
            _reasons.append("终审 blocker %d 条经机械重测/复核已消除 ⇒ 不计入" % len(_dropped))
        if _und_note:
            _reasons.append("未决归一：%s" % _und_note)
        ledger(d, rnd, cid, 0, False, 原因=("；".join(_reasons)))
        return True, ""

    n_blockers = len(blockers)
    voids = int(p.get("_audit_voids") or 0)
    limit = int(knobs.get("max_rounds") or 0)
    reason = None
    if limit and rnd > limit:
        reason = "到数"                                         # round > max_rounds 的那一轮
    # 第二轮续修（integ2-q01c 实测 off-by-one）：旧条件 voids >= MAX 让 cap 在第 3 次
    # 评估才触发（第 1 次 voids=0 作废、第 2 次 voids=1 作废、第 3 次才 cap），而驱动
    # 的连续拒收上限 2 通常先杀父，修复派发几乎不可达。本次作废已是第 voids+1 次，
    # 达到上限 2 次即转修复派发/明确失败，不再记一次 plain 作废。
    elif voids + 1 >= FINAL_AUDIT_MAX_RETRY:
        reason = "次数上限"
    if reason:
        # 第二轮 B：作废达上限 ⇒ 不再爬升进终态（旧 _climb 已删）。把 blocker 交给
        # 驱动器做"自动有限修复派发 / 明确失败"（见 driver.apply_output 的收束分支）；
        # 本函数只判定、不写流水（§3.8），仍返回 False。
        p["_audit_cap_blockers"] = list(blockers)
        p["_audit_cap_reason"] = reason
        p["_audit_cap_cid"] = cid
        ledger(d, rnd, cid, n_blockers, False,
               原因=("终审 blocker %d 条且作废已达上限（%s）⇒ 不兜底通过，转修复派发/明确失败"
                     % (n_blockers, reason))
               + (("；未决归一：%s" % _und_note) if _und_note else ""))
        return False, ("终审出 blocker %d 条且作废已达上限（%s）⇒ 不再兜底通过，"
                       "待修复派发/明确失败" % (n_blockers, reason))
    p["_audit_voids"] = voids + 1
    p["_audit_blockers"] = list(blockers)
    _log_void(d, pid, rnd, n_blockers, voids + 1)
    ledger(d, rnd, cid, n_blockers, True,
           原因=("未决归一：%s" % _und_note) if _und_note else "")
    return False, "终审出 blocker %d 条（作废第 %d 次）" % (n_blockers, voids + 1)


def _log_void(d, pid: str, rnd: int, n_blockers: int, voids: int) -> None:
    """作废日志：driver.log 一行可 grep 的 `收束作废`（与 `_log_forced` 同落点同风格，不许静默）。"""
    line = ("收束作废 父=%s 轮号=%s blocker条数=%d 作废次数=%d/%d ⇒ 该轮不算数（回退成一轮 merge："
            "act 改回 spawn + round 回退 1，下一轮由 next_act 算出 merge）"
            % (pid, rnd, n_blockers, voids, FINAL_AUDIT_MAX_RETRY))
    with open(d.driver_log, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------- 修复派发（第二轮 B）
def _resolve_target(cite: str, run_dir: str) -> tuple:
    """blocker 引用 ⇒ (run 相对路径, 绝对路径)；解不出 ⇒ ("", "")。

    只认 run_dir / 树根下的真实文件（复用 evidence 的粘连抢救：相对引用先直解，
    再试后缀抢救；绝对引用只认 run 内/树根内）。"""
    from harness import evidence as _ev_mod
    if not isinstance(cite, str) or not cite.strip():
        return "", ""
    p = cite.strip()
    roots = [str(run_dir or ""), str(role_mod.ROOT)]
    if os.path.isabs(p):
        try:
            rp = os.path.realpath(p)
        except OSError:
            return "", ""
        for root in roots:
            if root and rp.startswith(os.path.realpath(root) + os.sep) and os.path.isfile(rp):
                return os.path.relpath(rp, str(run_dir or "")), rp
        return "", ""
    if ".." in Path(p).parts:
        return "", ""
    for root in roots:
        if root and os.path.isfile(os.path.join(root, p)):
            return p, os.path.join(root, p)
    rescued = _ev_mod._rescue_relative_cite(p, roots) or _ev_mod._rescue_by_suffix(p, roots)
    if rescued:
        for root in roots:
            if root and os.path.isfile(os.path.join(root, rescued)):
                return rescued, os.path.join(root, rescued)
    return "", ""


def _find_failed_leaf(target_rel: str, item: str, tree: dict, parent: dict):
    """blocker 指向的失败叶：父子树内终结节点里 context/验收标准/reply 提到目标文件
    （或其 basename）的那一个；找不到 ⇒ None（修复叶不挂 repair_of，照常派发）。"""
    if not parent:
        return None
    needle = (target_rel or "").strip()
    base = os.path.basename(needle) if needle else ""
    try:
        ids = node_mod.subtree_ids(str(parent.get("id") or ""), tree or {})
    except Exception:                                            # noqa: BLE001
        return None
    for nid in sorted(ids):
        if nid == str(parent.get("id") or ""):
            continue
        n = (tree or {}).get(nid) or {}
        if not node_mod.is_terminal(n):
            continue
        hay = "\n".join(str(n.get(k) or "") for k in ("context", "验收标准", "reply"))
        if needle and needle in hay:
            return n
        if base and base in hay:
            return n
    return None


def repair_specs(blockers: list, tree: dict, run_dir: str, parent: dict = None) -> list:
    """终审 blocker ⇒ 机械修复叶规格（纯函数，不建节点、不写盘）。

    每条 blocker 出一个规格；同一目标文件去重（多条 blocker 指同一文件时只修一次）。
    目标文件只认 blocker item 引用、位于 run 内、且不属于 input/ 的落盘文件——
    修复叶只写可写的 run 内产物，输入与 run 外路径永不成为修复目标。
    文档长度不再参与任何判定，因此不抽字数、不做长度重测。
    @returns: [{item, blocker_id, target, target_abs, repair_of}]。
    """
    specs, seen_targets = [], set()
    from harness import evidence as _ev_mod
    run_root = Path(str(run_dir or "")).resolve()
    for b in (blockers or []):
        item = str((b or {}).get("item") or "")
        target, target_abs = "", ""
        skipped_target = False
        # 2026-11-13：权威冲突已经被 reverify_blockers 改写成「权威 vs 派生」，
        # 这里优先用改写时绑定的可写目标；不再从模型原文里猜目标。
        direction = (b or {}).get("authority_direction") or {}
        if direction.get("target"):
            candidate, candidate_abs = _resolve_target(str(direction["target"]), run_dir)
            if candidate:
                try:
                    from harness import authority as _auth
                    if _auth.is_writable(candidate, _auth.load_policy(run_dir)):
                        target, target_abs = candidate, candidate_abs
                except Exception:                                # noqa: BLE001
                    target, target_abs = "", ""
            if not target:
                continue
        else:
            for cite in _ev_mod.seen_citations(item):
                candidate, candidate_abs = _resolve_target(cite, run_dir)
                if not candidate:
                    continue
                try:
                    absolute = Path(candidate_abs).resolve()
                    relative = absolute.relative_to(run_root)
                except (OSError, ValueError):
                    skipped_target = True
                    continue
                if not relative.parts or relative.parts[0].lower() == "input":
                    skipped_target = True
                    continue
                target, target_abs = relative.as_posix(), str(absolute)
                break
        if not target and skipped_target:
            continue
        key = target or ("item:" + item[:120])
        if key in seen_targets:
            continue
        seen_targets.add(key)
        failed = _find_failed_leaf(target, item, tree, parent)
        specs.append({"item": item, "blocker_id": str((b or {}).get("id") or ""),
                      "target": target, "target_abs": target_abs,
                      "repair_of": str(failed.get("id") or "") if failed else ""})
    return specs


def hard_doc_specs(tree: dict, run_dir: str, parent: dict = None) -> list:
    """2026-11-12：字数门禁取消，不再因文档长度产生硬收敛失败。

    blocker 驱动的结构性修复仍由 repair_specs() 提供。
    """
    return []


def hard_convergence_failures(d, parent: dict, blockers: list = None) -> list:
    """Current objective failures that no Sage/Leo verdict can waive."""
    tree = (d.tree if d is not None else {}) or {}
    run_dir = str(d.run_dir) if d is not None else ""
    failures = []
    for spec in hard_doc_specs(tree, run_dir, parent=parent):
        failures.append({"kind": "disk_doc", "item": str(spec.get("item") or ""),
                         "target": str(spec.get("target") or ""),
                         "chars_min": int(spec["chars_min"])})
    parent_id = str((parent or {}).get("id") or "")
    for blocker in blockers or []:
        item = str((blocker or {}).get("item") or "")
        status, detail = verify_structural_blocker(item, d, parent_id)
        if status == "kept":
            failures.append({"kind": "structural", "id": blocker_identity(blocker),
                             "item": item, "detail": detail})
        elif status == "not_structural":
            drop, note = _reverify_measurable(item, run_dir)
            if note and not drop:
                failures.append({"kind": "measurable", "id": blocker_identity(blocker),
                                 "item": item, "detail": note})
    return failures


def make_adjudication_issue(parent: dict, candidate: dict, blockers: list,
                            tree: dict, run_dir: str, attempted_recovery: dict,
                            remaining_budget: dict, sage_reviews: list = None,
                            hard_checks: list = None, stage: str = None) -> dict:
    """Build a stable issue/card bound to the original merge and current evidence."""
    owner_id = str((parent or {}).get("id") or "")
    candidate_sha = _canonical_sha256(candidate or {})
    artifact_snapshot = snapshot_product_files(run_dir, tree or {}, owner_id)
    blocker_rows = []
    for blocker in blockers or []:
        row = {"id": blocker_identity(blocker),
               "item": str((blocker or {}).get("item") or ""),
               "level": str((blocker or {}).get("级别") or "blocker")}
        row["evidence"] = _review_input_evidence(
            blocker, tree or {}, run_dir, owner_id)
        blocker_rows.append(row)
    binding = {
        "owner_id": owner_id,
        "origin_node_id": owner_id,
        "round": int((parent or {}).get("round") or 0),
        "candidate_sha256": candidate_sha,
        "artifact_snapshot": artifact_snapshot,
        "blockers_sha256": _canonical_sha256(
            [{"id": row["id"], "item": row["item"]} for row in blocker_rows]),
    }
    issue_id = "adj-" + _canonical_sha256(binding)[:24]
    auth_gap = any(
        bool((blocker or {}).get("authorization_gap"))
        or any(word in str((blocker or {}).get("item") or "").lower()
               for word in ("授权", "权限", "许可", "approval", "permission"))
        for blocker in blockers or [])
    issue = {
        "id": issue_id,
        "owner_id": owner_id,
        "origin_node_id": owner_id,
        "kind": "terminal_audit_dispute",
        "stage": (stage or ("leo" if sage_reviews is not None else "sage")),
        "candidate_sha256": candidate_sha,
        "blockers": blocker_rows,
        "binding": binding,
        "hard_checks": list(hard_checks or []),
        "attempted_recovery": dict(attempted_recovery or {}),
        "remaining_budget": dict(remaining_budget or {}),
        "authorization_gap": auth_gap,
        "sage_reviews": list(sage_reviews or []),
    }
    payload = {
        "issue_id": issue_id,
        "binding": binding,
        "blockers": blocker_rows,
        "attempted_recovery": issue["attempted_recovery"],
        "remaining_budget": issue["remaining_budget"],
        "sage_reviews": issue["sage_reviews"],
        "hard_checks": issue["hard_checks"],
        "authorization_gap": auth_gap,
    }
    issue["card"] = {
        "id": issue_id + ".leo",
        "question": "只裁决绑定快照中的终审争议，不能豁免任何硬性机械检查。",
        "problem": json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1),
        "auth_affected": auth_gap,
    }
    if str(stage or "") == "user":
        issue["question"] = (
            "以下终审 blocker 涉及授权/红线或不可由模型决定的事项，需要用户确认：%s"
            % "；".join(str(row.get("item") or "")[:200] for row in blocker_rows))
    return issue


def build_repair_child(spec: dict, run_dir: str, tree: dict = None,
                       parent: dict = None, managed_doc: bool = False) -> dict:
    """Build a repair leaf; hard docs may return a document for one bound atomic write."""
    target = str(spec.get("target") or "")
    item = str(spec.get("item") or "")
    lines = ["【终审修复派发（机械派发）】终审 blocker 未消除：%s" % item[:300]]
    lines.append("工作区绝对路径（run 目录）：%s" % os.path.realpath(str(run_dir or "")))
    if target:
        label = "目标文件（由 Harness 写回，不调用文件工具）" if managed_doc else \
                "目标文件（先 Read 实况，再重写/补写）"
        lines.append("%s：%s" % (label, target))
        lines.append("要求：按 blocker 逐条修复目标文件，修复后重新入库，可被机械核验")
    else:
        lines.append("要求：按 blocker 逐条修复（未点名具体文件，以 blocker 文本为准），"
                     "修复结果必须落盘入库，可被机械核验")

    # 2026-11-13：修复叶必须看到程序读出的权威事实，而不是父/审计叶转述的数值。
    try:
        from harness import authority as _auth
        sheet = _auth.fact_sheet(str(run_dir or ""))
        if target:
            target_sheet = _auth.render_facts(
                str(run_dir or ""), [target], title="Harness 目标文件实况（程序读取）")
            if target_sheet:
                sheet = (sheet + "\n" + target_sheet).strip()
        if sheet:
            lines.append(sheet)
    except Exception:                                            # noqa: BLE001
        pass
    lines.append("写入边界：只允许修改上面点名的目标文件；input/ 与受保护产物不得修改。")

    if managed_doc:
        def clip(value, limit):
            text = str(value or "")
            return text if len(text) <= limit else text[:limit] + "\n……（原文截断；共 %d 字）" % len(text)

        source = (tree or {}).get(str(spec.get("repair_of") or "")) or {}
        if source:
            lines.append("\n## 原失败叶任务（只读参考）")
            for label, key, limit in (("任务", "context", 900),
                                      ("验收标准", "验收标准", 350),
                                      ("判据", "judge", 150)):
                value = source.get(key)
                if value:
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                    lines.append("%s：\n%s" % (label, clip(value, limit)))
        try:
            root = Path(run_dir).resolve()
            rel = Path(target)
            current = (root / rel).resolve()
            if (rel.is_absolute() or ".." in rel.parts
                    or os.path.commonpath((str(root), str(current))) != str(root)):
                raise ValueError("target outside run")
            current_text = current.read_text(encoding="utf-8") if current.is_file() else ""
        except (OSError, UnicodeDecodeError, ValueError):
            current_text = ""
        lines.append("\n## 当前目标文件磁盘内容（Harness 实测）")
        lines.append("以下内容是当前磁盘实况，作为修复起点：\n%s" %
                     (clip(current_text, 1800) if current_text else "（文件不存在或不可读）"))
        if tree is not None and parent is not None:
            try:
                sources = _review_input_evidence(
                    {"id": spec.get("blocker_id"), "item": item}, tree, run_dir,
                    str(parent.get("id") or ""))
            except (OSError, ValueError, TypeError):
                sources = []
            tool_rows = [row for row in sources if str(row.get("source_id") or "").startswith("tool:")][:3]
            lines.append("\n## 已验证的相关执行工具结果（最多 3 条）")
            if tool_rows:
                lines.extend("- %s：%s" % (row.get("source"), clip(row.get("observation"), 140))
                             for row in tool_rows)
            else:
                lines.append("证据台账中没有命中目标文件的成功执行工具结果；不要据此编造外部事实。")
        lines.append("\n不得调用任何文件工具。返回契约 JSON：reply 为简短结果说明，未决照实填写，"
                     "doc 必须原样保留当前目标文件的全部有效内容并补足为完整修复后正文，"
                     "不得是路径、摘要或 JSON 文本。文档必须真实：不得声称执行过未执行的操作，"
                     "不得编造数据。Harness 只会将 doc 写入已绑定目标文件。")
    else:
        lines.append("只改目标文件/相关产物，不另开新题；完成后只输出契约 JSON。")

    if any(word in item for word in _MISMATCH_WORDS) or "不一致" in item:
        lines.append("")
        lines.extend(CONFLICT_RULE_LINES)

    acc = "终审 blocker 逐条消除，目标文件已更新入库"
    judge = {"json_keys": ["reply"]}
    child = {"role": "执行", "context": "\n".join(lines), "验收标准": acc,
             "judge": judge,
             "auth": ("修复终审 blocker：重写 %s" % target) if target else "修复终审 blocker",
             "overrides": {}}
    if spec.get("repair_of"):
        child["overrides"] = {"repair_of": str(spec["repair_of"])}
    return child


# ---------------------------------------------------------------- 决策 C：纯复核型修复叶（第四轮，GPT 设计决策）
# 背景（integ3-agg2 实测）：产物已验全对，唯一 blocker 是装饰性审计发现
# （"日志中出现重复时间戳"，根已说明属日志重复提交、不影响统计结果），无文件目标、
# 无可度量修复项；P1⑤ 门正确拒派修复叶后，终审除明确失败外无合法出路。
# 决策 C（不选 A/B 的自动丢弃 blocker）：允许**有界、只读、纯复核型修复叶**
# 处理不可修非安全 blocker。
#   有界：每父至多 1 轮复核（FINAL_AUDIT_REVIEW_MAX=1）；复核派发消耗 1 轮
#     修复预算（FINAL_AUDIT_REPAIR_MAX=2，共用计数，不另开上限）；每条 blocker
#     至多 1 个复核叶；到数复核同样回放强制收束轮（与修复派发同一有界性）。
#   只读：复核叶 role=审计（may_write=false、无工具），context 只给重读材料；
#     派发时对 product/ 与 doc/ 下产物取 sha256 快照，复核结论采纳前重测哈希——
#     产物变了 ⇒ 复核作废（blocker 照旧，不兜底）。
#   纯复核：复核叶不改任何文件、不派修复叶；其 verdict 以 未决 为载体——
#     未决==[] ⟺ 确认不阻断；任一条确认阻断 ⇒ 记入未决。blocker 只有在复核叶
#     完成、未决为空、reply 含"不阻断"实测结论、产物哈希一致时才不再计入，
#     且仍记在终审"已消除"里（留痕，不抹除）。
#   不可修：只有 repair_specs 无可派发规格的 blocker 才进复核（有可修规格时
#     先走修复派发，复核不并行——修与核不能同时进行）。
#   非安全：含安全词（越权/密钥/凭证/…）或结构性（未审批次/批次子数不足）的
#     blocker 永不进复核——安全与结构问题必须走修复或明确失败，不得复核放行。
FINAL_AUDIT_REVIEW_MAX = 1

REVIEW_SAFETY_WORDS = ("越权", "密钥", "私钥", "密码", "secret", "token", "凭证",
                       "隐私", "身份证", "银行卡", "提示词注入", "指令注入", "命令注入",
                       "代码注入", "SQL注入", "提权", "后门", "恶意",
                       "泄露", "删库", "生产环境", "生产服务", "api key", "apikey")
REVIEW_STRUCTURAL_WORDS = ("未审批次", "批次子数不足")


def review_blocker_key(item: str) -> str:
    """blocker 去重/复核匹配键：空白折叠后 sha256 前 16 位（确定性，不读语义）。"""
    import hashlib as _hl
    norm = re.sub(r"\s+", "", str(item or "").strip())
    return _hl.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _review_prefix(item: str) -> str:
    """复核匹配的前缀回落：空白折叠后前 15 字（终审重述措辞微调时仍能对上号；
    只有前缀相同才算同一条，不做语义判断）。"""
    return re.sub(r"\s+", "", str(item or "").strip())[:15]


def _blocker_matches_review(item: str, reviewed_items: list) -> bool:
    """复核匹配：键精确相等，或空白折叠后的前缀（前 15 字）互为前缀。"""
    key = review_blocker_key(item)
    pre = _review_prefix(item)
    for r in (reviewed_items or []):
        if not isinstance(r, str):
            continue
        if review_blocker_key(r) == key:
            return True
        rp = _review_prefix(r)
        if pre and rp and (pre.startswith(rp) or rp.startswith(pre)):
            return True
    return False


def is_reviewable_blocker(b, tree, run_dir, parent=None,
                          allow_executable: bool = False) -> tuple:
    """Blocker 是否适合只读 Sage 复核；可执行修复优先，预算耗尽后可复核。"""
    item = str((b or {}).get("item") or "")
    low = item.lower()
    for w in REVIEW_SAFETY_WORDS:
        if w.lower() in low:
            return False, "含安全词 %r，不进复核" % w
    for w in REVIEW_STRUCTURAL_WORDS:
        if w in item:
            return False, "结构性 blocker（%s），不进复核" % w
    bid = str((b or {}).get("id") or "")
    if bid:
        for s in repair_specs([b], tree, run_dir, parent=parent):
            if (not allow_executable and str(s.get("blocker_id") or "") == bid
                    and str(s.get("target_abs") or "")):  # 2026-11-12：字数门取消
                return False, "有可派发的修复规格（%s），先走修复派发" % (s.get("target") or "")
    return True, ""


def _bound_input_evidence_files(tree: dict, run_dir: str) -> list:
    """Return declared run-local source files, resolved once from disk."""
    base = Path(str(run_dir or "")).resolve()
    out = []
    for node in (tree or {}).values():
        refs = ((node or {}).get("overrides") or {}).get("input_evidence_ids") or []
        for ref in refs:
            text = str(ref or "").strip()
            if not text:
                continue
            path = Path(text)
            if not path.is_absolute():
                path = base / path
            try:
                rel = path.resolve().relative_to(base).as_posix()
            except (OSError, ValueError):
                continue
            if rel and (base / rel).is_file() and rel not in out:
                out.append(rel)
    return out


def _review_measurements(run_dir: str) -> list:
    """复核叶的产物实测（Harness 在派发时从磁盘量得，非模型自报）。

    @returns: [(run 相对路径, 实测摘要)]。product/、blocks/ 与 doc/ 下文件逐个量字数、
    小文件附全文（≤4000 字），大文件附头尾；无产物文件时给一句空结果（不编造）。
    """
    out = []
    base = Path(str(run_dir or ""))
    files = []
    for sub in ("product", "blocks", "doc"):
        root = base / sub
        if not root.is_dir():
            continue
        for fp in sorted(root.rglob("*")):
            if fp.is_file():
                files.append(fp)
    total = 0
    for fp in files[:12]:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = fp.relative_to(base).as_posix()
        n = len(text)
        total += n
        if n <= 4000:
            summary = "%d 字；内容：%s" % (n, text)
        else:
            summary = "%d 字；内容头 2000 字：%s …… 内容尾 1000 字：%s" % (
                n, text[:2000], text[-1000:])
        out.append((rel, summary))
        if total > 12000:
            out.append(("(其余产物文件略)", "共 %d 个文件，总字数已超 12000，余下未附全文" % len(files)))
            break
    if not out:
        out.append(("(无产物文件)", "product/、blocks/ 与 doc/ 下无产物文件"))
    return out


def _review_input_evidence(blocker: dict, tree: dict, run_dir: str,
                           parent_id: str = "") -> list:
    """Build bounded Sage evidence with stable source IDs bound to this input snapshot."""
    from harness import contract as contract_mod, evidence as evidence_mod

    item = str((blocker or {}).get("item") or "")
    base = Path(str(run_dir or "")).resolve()
    rows = []
    # Artifact measurements are already read from disk and included in the review prompt.
    for rel, summary in _review_measurements(run_dir):
        path = base / rel
        if not path.is_file():
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        rows.append({"source_id": "file:%s:%s" % (rel, digest),
                     "source": rel, "observation": summary})

    try:
        descendants = set(node_mod.subtree_ids(str(parent_id), tree or {})) if parent_id else set()
    except (KeyError, TypeError, ValueError):
        descendants = set()
    scoped_tree = {nid: (tree or {}).get(nid) or {} for nid in descendants}
    # Declared source inputs (e.g. audit-data/events.psv) are part of the bound
    # review snapshot, not optional prose in a model prompt.
    for rel in _bound_input_evidence_files(scoped_tree, run_dir):
        path = base / rel
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source_id = "file:%s:%s" % (rel, digest)
        if any(row["source_id"] == source_id for row in rows):
            continue
        rows.append({"source_id": source_id, "source": rel,
                     "observation": "%d 字；内容：%s" % (len(text), text[:4000])})
    ledger = contract_mod.read_ledger(str(run_dir or ""))
    # Blockers in the real run often name only answer.txt or a document basename. Match
    # those names against cited paths in this owner's ledger, never against other branches.
    basenames = set(re.findall(
        r"(?<![\w.-])([\w.-]+\.(?:md|txt|csv|json|html|pdf|yaml|yml))(?=$|[^\w.-])",
        item, flags=re.IGNORECASE))
    explicit_ids = {nid for nid in descendants if nid and nid in item}
    candidates = []
    for nid in descendants:
        node = (tree or {}).get(nid) or {}
        entry = ledger.get(nid) or {}
        tools = entry.get("tools") or {}
        if (str(node.get("role") or "") != "执行"
                or str(entry.get("result_type") or "") != "complete"
                or int(tools.get("successes") or 0) <= 0):
            continue
        cited_paths = [str(value) for value in (entry.get("citations") or [])]
        matching = basenames.intersection(Path(value).name for value in cited_paths)
        explicit = nid in explicit_ids
        if not explicit and not matching:
            continue
        candidates.append((0 if explicit else 1, nid, entry))
    candidates.sort(key=lambda row: (row[0], row[1]))

    included = 0
    for _rank, nid, entry in candidates:
        if included >= 3:
            break
        raw_session_path = str(entry.get("session_path") or "")
        if not raw_session_path:
            continue
        session_file = Path(raw_session_path)
        if not session_file.is_absolute():
            session_file = base / session_file
        session_path = str(session_file)
        try:
            display_path = session_file.resolve().relative_to(base).as_posix()
        except (OSError, ValueError):
            display_path = session_file.name
        result = evidence_mod.count_tool_results(session_path)
        details = [line for line in (result.get("details") or [])
                   if str(line).startswith("tool 成功：")]
        for index, detail in enumerate(details[:3], 1):
            digest = hashlib.sha256(
                (str(entry.get("session_id") or "") + "\n" + str(detail)).encode("utf-8")
            ).hexdigest()
            rows.append({
                "source_id": "tool:%s:%d:%s" % (nid, index, digest),
                "source": "evidence_ledger/%s (%s)" % (nid, display_path),
                "observation": str(detail),
            })
        included += 1
    return rows


def build_review_child(blocker: dict, run_dir: str, parent_reply: str = "",
                       tree: dict = None, issue_id: str = "",
                       parent_id: str = "") -> dict:
    """复核 blocker ⇒ spawn 形态的 child dict（role=审计，只读）。

    context 由 Harness 按机械记录生成（blocker 原文 + 产物实测 + 对应会话工具证据 +
    工作区绝对路径），不是模型自由撰写；judge 要求 reply/未决 和结构化 verdict 字段。
    复核叶不写 doc/edits、不派子节点。
    """
    item = str((blocker or {}).get("item") or "")
    bid = str((blocker or {}).get("id") or "")
    bid = bid or blocker_identity(blocker)
    lines = ["【终审复核派发（机械派发）】终审 blocker 未消除且本地修复未能解决：",
             "issue_id=%s；blocker_id=%s：%s" % (str(issue_id or ""), bid, item),
             # 第三轮 P1④：任务文本里的 run 目录用真实路径（realpath）。
             "工作区绝对路径（run 目录）：%s" % os.path.realpath(str(run_dir or "")),
             "只读复核：只用下面给出的产物实测重核该 blocker，不改任何文件，"
             "不写 doc/edits，不派子节点，不做新现场调查。",
             "产物实测（Harness 在派发时从磁盘量得）："]
    evidence_rows = _review_input_evidence(blocker, tree or {}, run_dir, parent_id)
    lines.append("本次可引用证据（source_id 是唯一允许的来源标识）：")
    lines.extend("  - source_id=%s；source=%s；observation=%s"
                 % (row["source_id"], row["source"], row["observation"])
                 for row in evidence_rows or
                 [{"source_id": "none", "source": "none", "observation": "无匹配证据"}])
    if str(parent_reply or "").strip():
        lines.append("父节点对该 blocker 的说明（供复核参考，不是结论）：%s"
                     % str(parent_reply)[:1000])
    lines.append("必须输出 JSON {reply, 未决, verdict, covers, evidence, note}。"
                 "verdict 只能是 pass/rework/escalate；covers 必须包含 blocker_id=%s。"
                 "evidence 必须是非空数组，每项为 {source, observation}；source 必须逐字使用"
                 "上方某个 source_id，observation 只能陈述该行给出的事实。"
                 "没有 source_id 时不得判 pass；note 为简短说明。pass 只表示本 blocker 被这些"
                 "证据证明不阻断；rework 只表示 blocker 仍成立，可由 Harness 检查已有规格；"
                 "无法判断、缺证据或权限相关必须选 escalate。未决保留实际未解决事项，"
                 "不得为了 pass 清空真实未决。" % bid)
    child = {"role": "审计",
             "context": "\n".join(lines),
             "验收标准": "只读复核输出显式 verdict、覆盖 blocker_id、非空来源证据；未决保留真实问题",
             "judge": {"json_keys": ["reply", "未决", "verdict", "covers", "evidence", "note"]},
             "auth": "只读复核终审 blocker（不改任何文件）",
             "overrides": {"node_kind": "audit"},
             "_structured_review": True, "_review_blocker_id": bid,
             "_review_issue_id": str(issue_id or ""),
             "_review_evidence_ids": [row["source_id"] for row in evidence_rows]}
    return child


def snapshot_product_files(run_dir: str, tree: dict = None, parent_id: str = "") -> dict:
    """产物及声明的 run 内源数据的 sha256 快照（{run 相对路径: hex}）。
    只快照内容变化——复核结论采纳前重测，变了 ⇒ 复核作废。"""
    import hashlib as _hl
    out = {}
    base = Path(str(run_dir or ""))
    for sub in ("product", "blocks", "doc"):
        root = base / sub
        if not root.is_dir():
            continue
        for fp in sorted(root.rglob("*")):
            if not fp.is_file():
                continue
            try:
                rel = fp.relative_to(base).as_posix()
                out[rel] = _hl.sha256(fp.read_bytes()).hexdigest()
            except OSError:
                continue
    scoped_tree = tree or {}
    if parent_id:
        try:
            descendants = set(node_mod.subtree_ids(str(parent_id), scoped_tree))
        except (KeyError, TypeError, ValueError):
            descendants = set()
        scoped_tree = {nid: scoped_tree.get(nid) or {} for nid in descendants}
    for rel in _bound_input_evidence_files(scoped_tree, run_dir):
        try:
            out[rel] = _hl.sha256((base / rel).read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def snapshot_matches(snapshot: dict, run_dir: str) -> bool:
    """快照里每个文件的内容与快照时一致（新增文件不影响复核结论，不计）。"""
    import hashlib as _hl
    base = Path(str(run_dir or ""))
    for rel, hexd in (snapshot or {}).items():
        try:
            cur = _hl.sha256((base / rel).read_bytes()).hexdigest()
        except OSError:
            return False
        if cur != hexd:
            return False
    return True


def _review_leaf_passes(leaf: dict, run_dir: str, blocker_id: str,
                        issue_id: str) -> bool:
    """只有明确、绑定 blocker 且有来源证据的 Sage pass 才可解决争议。"""
    if not isinstance(leaf, dict):
        return False
    if str(leaf.get("state") or "") != "完成" or leaf.get("judge_ok") is not True:
        return False
    verdict = leaf.get("_review_verdict") or {}
    unresolved_for_blocker = any(
        isinstance(row, dict)
        and blocker_id in (str(row.get("what") or "") + " " + str(row.get("why") or ""))
        for row in (leaf.get("未决") or []))
    return (str(leaf.get("_review_issue_id") or "") == str(issue_id or "")
            and verdict.get("verdict") == "pass"
            and blocker_id in (verdict.get("covers") or [])
            and not unresolved_for_blocker
            and snapshot_matches(leaf.get("_review_snapshot") or {}, run_dir))


def apply_reviewed_drops(blockers, d, parent_id: str = "") -> tuple:
    """已复核 blocker 的机械落点。@returns: (kept, dropped)；dropped 形如
    {"item":..., "reason":...}，与 reverify_blockers 同形（进"已消除"留痕）。"""
    kept, dropped = [], []
    tree = (d.tree if d is not None else {}) or {}
    run_dir = str(d.run_dir) if d is not None else ""
    try:
        ids = node_mod.subtree_ids(str(parent_id or ""), tree)
    except Exception:                                            # noqa: BLE001
        ids = []
    reviews = [(nid, tree.get(nid) or {}) for nid in ids
               if ".fix.review" in nid]
    owner = tree.get(str(parent_id or "")) or {}
    review_meta = owner.get("_adjudication") or {}
    issue_id = str(review_meta.get("issue_id")
                   or (review_meta.get("issue") or {}).get("id") or "")
    for b in (blockers or []):
        item = str((b or {}).get("item") or "")
        hit = ""
        for nid, leaf in reviews:
            if not _review_leaf_passes(leaf, run_dir, blocker_identity(b), issue_id):
                continue
            if _blocker_matches_review(item, list(leaf.get("_review_of") or [])):
                hit = nid
                break
        if hit:
            dropped.append({"item": item,
                            "reason": "已复核：%s 确认不阻断（只读复核叶，reply 含实测依据；"
                                      "产物哈希与复核时一致）" % hit})
        else:
            kept.append(b)
    return kept, dropped


# ---------------------------------------------------------------- merge 轮的注入（③ 插入点）
def prepare_merge(p: dict, knobs: dict, d) -> str:
    """merge 轮拼完 prompt 之后、跑产出之前由驱动器调用（§2.4 ③）。三件事都在这里：

    ① 设 `knobs["审计发现"]` = 待回应清单（触发轮 == 本轮 round − 1；§3.6）——
       这是**机械触发位**（schema.validate 见 AUDIT_KEY ⇒ 卡 `对审计发现` 必填）；
    ② 返回【待回应清单段 + blocker 段】文本，驱动器 `prompt +=` 它
       ⇒ **代码注入**：模型才有「回应谁」的 id 串可抄（§5「必须由代码在拼 prompt 时注入」）。
       两段都为空 ⇒ 返回 `""`；
    ③ **清守**（把 `p["_audit_blockers"]` 清空，防对后续轮重复设守）。
    """
    rows = audit.injectable(p, d.tree, int(p.get("round") or 0))
    if rows:
        # ★ 待回应清单的**机械触发位**：放进来 ⇒ schema.validate 的审计分支被激活。
        #   ★★ 实测偏差（已登记报告「待裁决」#1，schema.py 属禁改文件，未动手）：
        #      tree-02 的 `schema.AUDIT_KEY` 字面是「审计发现」，而 merge 契约的**装载键**是
        #      「对审计发现」（`CONTRACTS["merge"]["条件必填"]`）。把「审计发现」放进 knobs，schema 会反过来
        #      要求产出里必须有**同名**的「审计发现」键 ⇒ 契约合规的产出（只写 对审计发现）反而被拒。
        #      实证：
        #        >>> schema.validate("merge", obj_合规, {"审计发现": [...]})
        #        (False, "本轮有审计发现 ⇒ '审计发现' 必填（每条含 item/处置/说明）")
        #      ⇒ 本模块按**契约装载键**注入触发位；「逐条必须回应」的硬性由 driver 插入点④ 机械兜住。
        knobs["对审计发现"] = rows    # role.effective 返回的是新 dict ⇒ 可安全写入
    parts = []
    if rows:
        parts.append("## 本轮待回应清单（代码注入；每条必须在 `对审计发现[].item` 里逐字带上 id 串）\n"
                     + json.dumps({"清单": rows}, ensure_ascii=False)
                     + "\n写法：`\"对审计发现\": [{\"item\": \"[<检查点id>#<n>] 你的回应\", "
                       "\"处置\": \"解掉|上冒|记未决\", \"说明\": \"…\"}]`"
                       "（`建议` 是审查给的、可选：你没采纳也不报错）")
    blockers = list(p.get("_audit_blockers") or [])
    p["_audit_blockers"] = []                                   # ③ 清守
    if blockers:
        parts.append("## 终审 blocker（上一轮收束被作废 ⇒ 本轮【必须逐条回应】）\n"
                     + json.dumps({"blocker": blockers}, ensure_ascii=False)
                     + "\n回应写在 `对审计发现[]` 里（`item` 里必须【逐字】含 id 串）。"
                       "回应后仍可再次收束（若 blocker 已解掉）。")
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


# ---------------------------------------------------------------- 「必须逐条回应」的机械落点（④）
# ★ G1 修正（owner 判决 attempt-1 §二 G1）：注入键从 `审计发现` 改成 `对审计发现`（= 契约装载键）
#   之后，`schema.py:148` 的审计分支**不再被触发**（它判的是 `AUDIT_KEY = "审计发现"`）⇒
#   `对审计发现[]` 的条目形状（`item`/`处置`/`说明` 必填 + `处置 ∈ 解掉/上冒/记未决`
#   + `建议 ∈ 继续派/切分/收束`）**没有任何执行者**（owner 用 `g1_boundary.py` 实测：连
#   `对审计发现[{}]`、`处置="彻底瞎写"`、`建议="瞎建议"` 都 `schema=True`）。
#   ⇒ 本批在**自己的新代码**里把这段形状补回来（`schema.py` 属禁改文件，一个字节不动），
#     与「id 串逐条出现」合并在同一处（driver 插入点④）。
AUDIT_REPLY_KEYS = ("item", "处置", "说明")     # 必填（与 schema 收紧四 同源）
AUDIT_REPLY_ENUM = {"处置": ("解掉", "上冒", "记未决"), "建议": ("继续派", "切分", "收束")}


def _enum_of(field: str):
    """枚举取值**从 schema 读**（不另定义一份，避免两处漂移）；schema 缺项时退回本模块的兜底表。"""
    src = getattr(schema, "DISPOSALS" if field == "处置" else "ADVICES", None)
    return tuple(src) if src else AUDIT_REPLY_ENUM[field]


def reply_shape_problems(obj: dict) -> list:
    """`对审计发现[]` 的**形状**自检（G1：换键后 `schema` 的审计分支不触发 ⇒ 这段形状没执行者）。

    口径逐字照 `schema._check_audit_replies`：每条必填 `item`/`处置`/`说明`，
    `处置` ∈ 解掉/上冒/记未决，`建议`（可选）∈ 继续派/切分/收束。
    ★ 键整个不给 ⇒ 返回 `[]`（不在这里造新硬性；那种情况由「id 串逐条出现」兜住）。
    @returns: [问题描述, …]；空 = 形状合规。
    """
    lst = obj.get("对审计发现")
    if lst is None:
        return []
    if not isinstance(lst, list):
        return ["`对审计发现` 必须是数组（实际 %s）" % type(lst).__name__]
    out = []
    for i, it in enumerate(lst):
        if not isinstance(it, dict):
            out.append("对审计发现[%d] 必须是对象 {item,处置,说明}" % i)
            continue
        for k in AUDIT_REPLY_KEYS:
            if not str(it.get(k) or "").strip():
                out.append("对审计发现[%d] 缺 '%s'" % (i, k))
        for k in ("处置", "建议"):
            v = it.get(k)
            if v is not None and v not in _enum_of(k):
                out.append("对审计发现[%d].%s=%r 不在 %s" % (i, k, v, list(_enum_of(k))))
    return out


def missing_replies(obj: dict, p: dict, d) -> list:
    """缺回应的 id 串清单（§3.6）。**只查 id 串**，不查模型对 item 正文的措辞。

    口径：本轮待回应清单里每条发现的 id 串，必须**逐字出现在**某条 `对审计发现[].item` 里。
    父**不采纳也不报错**（处置/去向与建议相反不是错误 —— D10 裁决权在父）。
    ★ 形状自检是**另一件**（`reply_shape_problems`，driver 插入点 ④a 调用）—— 本函数语义未变。
    """
    rows = audit.injectable(p, d.tree, int(p.get("round") or 0))
    if not rows:
        return []
    blob = "\n".join(str(it.get("item") or "") for it in (obj.get("对审计发现") or [])
                     if isinstance(it, dict))
    return [r["id"] for r in rows if r.get("id") and str(r["id"]) not in blob]
