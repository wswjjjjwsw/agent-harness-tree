#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终审修复派发 / 明确失败回归（第二轮 B，集成验收 t-132692485362）。

跑法：cd <候选根> && python3 tests/cases-audit-repair.py
语义（第二轮改动，作废上限数值 2 未变，变的是上限后的出路）：
  1. 作废达上限后不再兜底通过：on_converge 在 p 上留 _audit_cap_blockers 并返回 False；
  2. 驱动器自动派发修复叶（带机械判据 chars_min，id 含 .fix.，父等子节点）；
  3. 修复预算（2 轮）用完仍有 blocker ⇒ 父明确失败（不冒充完成）；
  4. 到数轮 + blocker ⇒ 直接明确失败；
  5. 修复规格：同一目标文件去重；字数目标从 blocker/被修叶抽取；找不到被修叶也能派发。
"""
import copy, json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import audit as audit_mod, contract as contract_mod, driver, final_audit, node as node_mod, role, schema

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))


def _mk_tree(td):
    """run_dir 下有被 blocker 引用的落盘文件；树：root + 一个失败叶（写过该文件）。"""
    rd = Path(td)
    (rd / "blocks").mkdir(parents=True, exist_ok=True)
    (rd / "blocks" / "sum_calculation_doc.md").write_text("x" * 207, encoding="utf-8")
    (rd / "product").mkdir(parents=True, exist_ok=True)
    (rd / "product" / "sum.txt").write_text("5050\n", encoding="utf-8")
    tree = {}
    parent = node_mod.new_node("root", "根", round=3, act="merge", batch=2, state="在跑")
    parent["merge"] = {"去向": "收束"}
    tree["root"] = parent
    leaf = node_mod.new_node("root.c2.2", "执行", parent="root", batch=2, state="完成",
                             context="写汇总文档到 blocks/sum_calculation_doc.md",
                             验收标准="文档不少于 1500 字",
                             judge="文档字数不少于 1500 字", reply="已写 blocks/sum_calculation_doc.md。")
    tree["root.c2.2"] = leaf
    return tree, parent


BLOCKER = [{"id": "[root.f3.1#1]", "item": "汇总文档 blocks/sum_calculation_doc.md 仅 207 字，远低于要求的 1500 字",
            "级别": "blocker"}]


def _mk_driver(td, tree, parent):
    return driver.Driver(parent, tree, run_dir=str(td))


# ---------- 1) 修复规格：目标文件 + 字数目标 + 被修叶链接 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    specs = final_audit.repair_specs(BLOCKER, tree, str(td), parent=parent)
    check("规格: 1 条 blocker ⇒ 1 个规格", len(specs) == 1, specs)
    check("规格: 目标文件解出", specs[0]["target"] == "blocks/sum_calculation_doc.md", specs)
    check("规格: 不再抽字数目标（字数判据已取消）",
          "chars_min" not in specs[0], specs)
    check("规格: repair_of 指回失败叶", specs[0]["repair_of"] == "root.c2.2", specs)

# ---------- 2) 同一目标文件去重 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    two = BLOCKER + [{"id": "[root.f3.2#1]",
                      "item": "blocks/sum_calculation_doc.md 内容不完整，仍低于 1500 字要求",
                      "级别": "blocker"}]
    specs = final_audit.repair_specs(two, tree, str(td), parent=parent)
    check("规格: 同一文件两条 blocker ⇒ 去重为 1", len(specs) == 1, specs)

# ---------- 3) 找不到被修叶也能派发（无 repair_of、不崩） ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    orphan = [{"id": "[root.f3.1#1]", "item": "产物文件 product/sum.txt 内容不是 5050",
               "级别": "blocker"}]
    specs = final_audit.repair_specs(orphan, tree, str(td), parent=parent)
    check("规格: 无被修叶也能出规格", len(specs) == 1 and specs[0]["repair_of"] == "", specs)
    child = final_audit.build_repair_child(specs[0], str(td))
    check("修复叶: 无字数目标时判据走 json_keys",
          child["judge"] == {"json_keys": ["reply"]}, child)

# ---------- 4) 修复叶形态：机械判据 + context 点名文件 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    specs = final_audit.repair_specs(BLOCKER, tree, str(td), parent=parent)
    child = final_audit.build_repair_child(specs[0], str(td))
    check("修复叶: judge 走 json_keys（不按字数验收）",
          child["judge"] == {"json_keys": ["reply"]}, child)
    check("修复叶: context 点名目标文件", "blocks/sum_calculation_doc.md" in child["context"], child)
    check("修复叶: context 含工作区绝对路径", str(td) in child["context"], child)
    check("修复叶: overrides 挂 repair_of",
          child["overrides"] == {"repair_of": "root.c2.2"}, child)

# ---------- 5) 驱动器级：作废达上限 ⇒ 自动派发修复叶（父等子节点，不回退 round） ----------
def _blocker_runner(node, act, knobs, prompt, session_id, run_dir):
    # 预算用完后的出路必须与"字数不是硬判据"解耦：这条 blocker 带安全词，
    # 修复预算耗尽时不可复核 ⇒ 父明确失败（不冒充完成、不转语义裁决）。
    return json.dumps({"reply": "全量终审：汇总文档仅 207 字，且含生产环境凭证，未消除。",
                       "未决": [
        {"type": "缺信息", "what": "BLOCKER 汇总文档 blocks/sum_calculation_doc.md 仅 207 字，且含生产环境凭证，远低于要求的 1500 字",
         "why": "doc 太短且含敏感信息"}]}, ensure_ascii=False)

with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 2
    d = _mk_driver(td, tree, parent)
    d.runner = _blocker_runner
    d.runner_kind = "injected"
    knobs = role.effective("根", "merge")
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("派发: 返回【修复派发】哨兵", isinstance(detail, str) and detail.startswith("【修复派发】"), detail)
    created = [nid for nid in tree if ".fix.audit1." in nid]
    check("派发: 建出 1 个修复叶", len(created) == 1, created)
    check("派发: 修复叶 batch=本 merge 轮", tree[created[0]]["batch"] == 3, tree[created[0]])
    check("派发: 修复叶判据走 json_keys",
          tree[created[0]]["judge"] == {"json_keys": ["reply"]}, tree[created[0]])
    check("派发: 父等子节点（等修复叶）", parent["state"] == "等子节点", parent["state"])
    check("派发: 父 round 不回退", parent["round"] == 3, parent["round"])
    check("派发: 修复计数 1", parent.get("_audit_repair_rounds") == 1, parent.get("_audit_repair_rounds"))
    check("派发: 父未进终态", not node_mod.is_terminal(parent), parent["state"])

# ---------- 6) 修复预算用完 ⇒ 明确失败 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 2
    parent["_audit_repair_rounds"] = 2
    d = _mk_driver(td, tree, parent)
    d.runner = _blocker_runner
    d.runner_kind = "injected"
    knobs = role.effective("根", "merge")
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("预算用完: 返回【明确失败】哨兵", isinstance(detail, str) and detail.startswith("【明确失败】"), detail)
    check("预算用完: 父进失败终态", parent["state"] == "失败", parent["state"])
    check("预算用完: blocker 仍挂未决（不冒充完成）",
          any("BLOCKER" in str(u.get("what") or "") for u in parent.get("未决") or []),
          parent.get("未决"))
    check("预算用完: 未新建修复叶", not [nid for nid in tree if ".fix." in nid], list(tree))

# ---------- 7) 第三轮 P0②：到数轮 + blocker + 修复预算有余 ⇒ 先派修复叶（回放强制收束轮），不是直接明确失败 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 0
    parent["_audit_repair_rounds"] = 0
    parent["round"] = 9
    d = _mk_driver(td, tree, parent)
    d.runner = _blocker_runner
    d.runner_kind = "injected"
    knobs = dict(role.effective("根", "merge"))
    knobs["max_rounds"] = 5
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("到数修复: 返回【修复派发】哨兵（不直接明确失败）", isinstance(detail, str) and detail.startswith("【修复派发】"), detail)
    created = [nid for nid in tree if ".fix.audit1." in nid]
    check("到数修复: 建出 1 个修复叶", len(created) == 1, created)
    check("到数修复: 修复叶判据走 json_keys",
          tree[created[0]]["judge"] == {"json_keys": ["reply"]}, tree[created[0]])
    check("到数修复: 父等子节点（等修复叶）", parent["state"] == "等子节点", parent["state"])
    check("到数修复: 父 round 回退 1（重放强制收束轮 9→8）", parent["round"] == 8, parent["round"])
    check("到数修复: 修复计数 1", parent.get("_audit_repair_rounds") == 1, parent.get("_audit_repair_rounds"))
    check("到数修复: 父未进终态", not node_mod.is_terminal(parent), parent["state"])

# ---------- 7b) 到数轮 + blocker + 修复预算用完（2/2）⇒ 明确失败 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 0
    parent["_audit_repair_rounds"] = 2
    parent["round"] = 9
    d = _mk_driver(td, tree, parent)
    d.runner = _blocker_runner
    d.runner_kind = "injected"
    knobs = dict(role.effective("根", "merge"))
    knobs["max_rounds"] = 5
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("到数预算用完: 返回【明确失败】哨兵", isinstance(detail, str) and detail.startswith("【明确失败】"), detail)
    check("到数预算用完: 父进失败终态", parent["state"] == "失败", parent["state"])
    check("到数预算用完: 父 round 不动", parent["round"] == 9, parent["round"])
    check("到数预算用完: 未新建修复叶", not [nid for nid in tree if ".fix." in nid], list(tree))

# ---------- 7c) 第三轮 P1⑤：到数轮 + 文本 blocker（无目标文件/chars_min）⇒ 不派空修复叶，直接明确失败 ----------
def _text_blocker_runner(node, act, knobs, prompt, session_id, run_dir):
    return json.dumps({"reply": "全量终审：有未审条目未进最终清单。",
                       "未决": [{"type": "缺信息", "what": "BLOCKER 未审批次 root#2 因子数不足被标记，无实际检查点内容",
                                 "why": "批次约束"}]}, ensure_ascii=False)

with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    # 注意：_mk_tree 的 root.c2.2 是完成态、无 disk_doc_check，repair_specs 的
    # 失败叶回补不触发；纯文本 blocker 本身无目标文件 ⇒ 规格不可派。
    parent["_audit_voids"] = 0
    parent["round"] = 9
    d = _mk_driver(td, tree, parent)
    d.runner = _text_blocker_runner
    d.runner_kind = "injected"
    knobs = dict(role.effective("根", "merge"))
    knobs["max_rounds"] = 5
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("到数无可派规格: 返回【明确失败】哨兵", isinstance(detail, str) and detail.startswith("【明确失败】"), detail)
    check("到数无可派规格: 父进失败终态", parent["state"] == "失败", parent["state"])
    check("到数无可派规格: 未新建修复叶", not [nid for nid in tree if ".fix." in nid], list(tree))

# ---------- 7d) 第三轮 P0② 有界性：到数修复最多重放 2 次，第 3 次到数终审必明确失败 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 0
    parent["round"] = 9
    d = _mk_driver(td, tree, parent)
    d.runner = _blocker_runner
    d.runner_kind = "injected"
    knobs = dict(role.effective("根", "merge"))
    knobs["max_rounds"] = 5
    def _merge_at_limit():
        return d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                                "未决": [], "uncovered": []}, knobs)
    d1 = _merge_at_limit()
    check("到数有界 1: 第 1 次到数派修复（回放）", isinstance(d1, str) and d1.startswith("【修复派发】"), d1)
    check("到数有界 1: round 回退到 8", parent["round"] == 8, parent["round"])
    parent["state"] = "在跑"   # 模拟修复叶终结 + 下一轮强制收束
    parent["round"] = 9
    d2 = _merge_at_limit()
    check("到数有界 2: 第 2 次到数再派修复（回放）", isinstance(d2, str) and d2.startswith("【修复派发】"), d2)
    check("到数有界 2: 修复计数 2", parent.get("_audit_repair_rounds") == 2, parent.get("_audit_repair_rounds"))
    check("到数有界 2: 建出第 2 轮修复叶", [nid for nid in tree if ".fix.audit2." in nid], list(tree))
    check("到数有界 2: round 再次回退到 8", parent["round"] == 8, parent["round"])
    parent["state"] = "在跑"
    parent["round"] = 9
    d3 = _merge_at_limit()
    check("到数有界 3: 第 3 次到数必明确失败", isinstance(d3, str) and d3.startswith("【明确失败】"), d3)
    check("到数有界 3: 父进失败终态", parent["state"] == "失败", parent["state"])
    check("到数有界 3: 父 round 仍为 9（明确失败不回放）", parent["round"] == 9, parent["round"])

# ---------- 8) 无 blocker ⇒ 正常收束（不受 B 影响） ----------
def _clean_runner(node, act, knobs, prompt, session_id, run_dir):
    return json.dumps({"reply": "全量终审：全量条目已逐条核过，未发现 blocker。", "未决": []}, ensure_ascii=False)

with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    d = _mk_driver(td, tree, parent)
    d.runner = _clean_runner
    d.runner_kind = "injected"
    knobs = role.effective("根", "merge")
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("无 blocker: 收束成立（detail 为空）", detail is None, detail)
    check("无 blocker: 父完成", parent["state"] == "完成", parent["state"])

# ---------- 9) 续修（integ2-q01 实测）：blocker 模糊不点名文件时，
# 失败叶的 disk_doc_check 补派修复规格（目标文件 + chars_min） ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    # 失败叶：doc 实测 194 字 < 1500（与 integ2-q01 的 root.c2.1 同形）
    leaf = node_mod.new_node("root.c2.1", "执行", parent="root", batch=2, state="失败",
                             context="写实现说明文档到 blocks/primes_doc.md",
                             验收标准="文档不少于 1500 字",
                             judge={"chars_min": 1500}, reply="已生成满足 1500 字的文档。")
    leaf["judge_ok"] = False
    leaf["disk_doc_check"] = {"chars_min": 1500, "named": {},
                              "written": {"blocks/primes_doc.md": 194,
                                          "product/answer.txt": 4},
                              "inline_chars": 194}
    tree["root.c2.1"] = leaf
    (Path(td) / "blocks" / "primes_doc.md").write_text("x" * 194, encoding="utf-8")
    vague = [{"id": "[root.f6.3#1]",
              "item": "未审批次 root#2/root#4/root#5 因子数不足被标记，无实际检查点内容",
              "级别": "blocker"}]
    specs = final_audit.repair_specs(vague, tree, str(td), parent=parent)
    disk = [s for s in specs if s["target"] == "blocks/primes_doc.md"]
    check("续修: 文档长度不再补派修复规格", len(disk) == 0, specs)
    check("续修: 模糊 blocker 不凭空造可写目标", all(not s["target"] for s in specs), specs)

# ---------- 9b) 修复目标不越界：input/ 与 run 外路径不得成为可写目标 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    (Path(td) / "input").mkdir(exist_ok=True)
    (Path(td) / "input" / "q01.md").write_text("task text", encoding="utf-8")
    guarded = [{"id": "[root.f7.1#1]", "item": "input/q01.md 与任务描述不一致", "级别": "blocker"},
               {"id": "[root.f7.2#1]", "item": "/etc/hosts 被改动", "级别": "blocker"}]
    specs = final_audit.repair_specs(guarded, tree, str(td), parent=parent)
    check("越界: input/ 不作为修复目标",
          all(s["target"] != "input/q01.md" for s in specs), specs)
    check("越界: run 外绝对路径不作为修复目标",
          all(not s["target"] for s in specs), specs)

# ---------- 11) 第三轮 P1④：build_repair_child 的任务文本用真实路径（realpath） ----------
with tempfile.TemporaryDirectory() as td:
    spec = {"item": "BLOCKER 汇总文档 blocks/sum_calculation_doc.md 仅 207 字，远低于要求的 1500 字",
            "blocker_id": "[root.f3.1#1]", "target": "blocks/sum_calculation_doc.md",
            "target_abs": os.path.join(td, "blocks", "sum_calculation_doc.md"),
            "chars_min": 1500, "repair_of": "root.c2.1"}
    child = final_audit.build_repair_child(spec, str(td))
    real = os.path.realpath(str(td))
    check("续修: 修复叶 context 用真实路径（realpath）",
          ("工作区绝对路径（run 目录）：%s" % real) in child["context"], child["context"])
    if real != str(td):
        check("续修: 未用未展开的符号链接路径",
              ("工作区绝对路径（run 目录）：%s" % str(td)) not in child["context"],
              child["context"])

# ---------- 10) 续修：contract_repair 单叶上限 300s（integ2-q01 两修复叶撞 180s） ----------
check("续修: contract_repair 上限 300.0s",
      contract_mod.leaf_timeout_for("contract_repair") == 300.0,
      contract_mod.LEAF_TIMEOUT_BY_KIND)
check("续修: research 上限仍 300.0s（未动）",
      contract_mod.leaf_timeout_for("research") == 300.0,
      contract_mod.LEAF_TIMEOUT_BY_KIND)

# ---------- 12) 第四轮：merge 处置/建议 "再派" 同义映射（integ3-q01d 实测） ----------
# 模型把"再派"同时写进 处置 与 建议（去向也是"再派"）——它是"继续派"的同义词；
# 处置='再派' 的意图是"由再派修复叶解掉"，故归入"解掉"。
_replies = [{"item": "执行叶自述 doc 字数 1500 但实际仅 196", "处置": "再派",
             "说明": "自述与审计核验结果矛盾，需重新执行并验证", "建议": "再派"}]
_norm, _note = schema.normalize_model_audit_replies(_replies)
check("再派同义: 处置 再派→解掉", _norm is not None and _norm[0]["处置"] == "解掉", _norm)
check("再派同义: 建议 再派→继续派", _norm is not None and _norm[0]["建议"] == "继续派", _norm)
check("再派同义: 归一有记录", _norm is not None and "同义" in (_note or ""), _note)
_norm2, _note2 = schema.normalize_model_audit_replies(
    [{"item": "x", "处置": "再派发一轮修复叶", "说明": "y"}])
check("再派同义: 处置子串 再派发→解掉",
      _norm2 is not None and _norm2[0]["处置"] == "解掉", _norm2)
_norm3, _ = schema.normalize_model_audit_replies(
    [{"item": "x", "处置": "解掉", "说明": "y", "建议": "重新派发"}])
check("再派同义: 建议 重新派发→继续派",
      _norm3 is not None and _norm3[0]["建议"] == "继续派", _norm3)
_neg, _negnote = schema.normalize_model_audit_replies(
    [{"item": "x", "处置": "不再派", "说明": "y"}])
check("再派同义: 否定式 不再派 不映射（仍显式失败）", _neg is None, _negnote)
_bad, _badnote = schema.normalize_model_audit_replies(
    [{"item": "x", "处置": "彻底瞎写", "说明": "y"}])
check("再派同义: 无映射仍显式失败（不放水）", _bad is None, _badnote)

# ---------- 13) 第四轮：终审检查点 未决[].type 越出枚举（integ3-q01 实测） ----------
# 模型把 what 首词 BLOCKER/NOTE 填进 type——先机械归一（→判不了）再校验，
# 终审不抛 ValueError；归一记录进 ledger 与检查点 result。
def _blocker_note_runner(node, act, knobs, prompt, session_id, run_dir):
    return json.dumps({"reply": "全量终审：汇总文档仅 207 字，远低于要求的 1500 字，未消除。",
                       "未决": [
        {"type": "BLOCKER", "what": "BLOCKER 汇总文档 blocks/sum_calculation_doc.md 仅 207 字，远低于要求的 1500 字",
         "why": "doc 太短"},
        {"type": "NOTE", "what": "NOTE 仅记录一次复核口径",
         "why": "参考"}]}, ensure_ascii=False)

with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 0
    d = _mk_driver(td, tree, parent)
    d.runner = _blocker_note_runner
    d.runner_kind = "injected"
    knobs = role.effective("根", "merge")
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("终审归一: BLOCKER/NOTE type 不抛错（返回收束作废而非异常）",
          isinstance(detail, str) and "收束作废" in detail, detail)
    rows = final_audit.read_ledger(d)
    check("终审归一: ledger 记归一原因",
          bool(rows) and "未决归一" in str(rows[-1].get("原因") or ""), rows[-1] if rows else [])
    cid = str(rows[-1].get("检查点id") or "") if rows else ""
    cnode = tree.get(cid) or {}
    types = [str(u.get("type") or "") for u in (cnode.get("未决") or [])]
    check("终审归一: 检查点 未决 type 全进枚举",
          types and all(t in schema.UNDECIDED_TYPES for t in types), types)
    check("终审归一: 检查点 result 记归一",
          "归一" in str(cnode.get("result") or ""), cnode.get("result"))

# ---------- 14) 第四轮决策 C：纯复核型修复叶（GPT 设计决策） ----------
DECORATIVE_ITEM = "日志中出现重复时间戳，根已说明属日志重复提交、不影响统计结果"


def _decorative_runner(node, act, knobs, prompt, session_id, run_dir):
    return json.dumps({"reply": "全量终审：日志时间戳重复。",
                       "未决": [{"type": "缺信息", "what": "BLOCKER " + DECORATIVE_ITEM,
                                 "why": "审计发现"}]}, ensure_ascii=False)


def _safety_runner(node, act, knobs, prompt, session_id, run_dir):
    return json.dumps({"reply": "全量终审：越权与密钥问题。",
                       "未决": [{"type": "冲突", "what": "BLOCKER 越权写入生产环境配置文件，密钥已暴露",
                                 "why": "安全"}]}, ensure_ascii=False)


# ---------- 14a) 装饰性 blocker（无目标/无可度量项）⇒ 派纯复核叶 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 2
    d = _mk_driver(td, tree, parent)
    d.runner = _decorative_runner
    d.runner_kind = "injected"
    knobs = role.effective("根", "merge")
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("复核派发: 返回【复核派发】哨兵", isinstance(detail, str) and detail.startswith("【复核派发】"), detail)
    created = [nid for nid in tree if ".fix.review1." in nid]
    check("复核派发: 建出 1 个复核叶", len(created) == 1, created)
    leaf = tree[created[0]] if created else {}
    check("复核派发: 复核叶 role=审计（只读）", leaf.get("role") == "审计", leaf.get("role"))
    check("复核派发: 复核叶 judge 是 json_keys",
          leaf.get("judge") == {"json_keys": ["reply", "未决"]}, leaf.get("judge"))
    check("复核派发: overrides 声明 node_kind=audit",
          (leaf.get("overrides") or {}).get("node_kind") == "audit", leaf.get("overrides"))
    check("复核派发: context 点名 blocker", DECORATIVE_ITEM in str(leaf.get("context") or ""), leaf.get("context"))
    check("复核派发: context 含产物实测（磁盘量值）",
          "product/sum.txt" in str(leaf.get("context") or ""), leaf.get("context"))
    check("复核派发: 记被复核 blocker 原文", leaf.get("_review_of") == [DECORATIVE_ITEM], leaf.get("_review_of"))
    import hashlib as _hl
    check("复核派发: 记产物快照",
          (leaf.get("_review_snapshot") or {}).get("product/sum.txt") == _hl.sha256(b"5050\n").hexdigest(),
          leaf.get("_review_snapshot"))
    check("复核派发: 父等子节点", parent["state"] == "等子节点", parent["state"])
    check("复核派发: 消耗 1 轮修复预算", parent.get("_audit_repair_rounds") == 1, parent.get("_audit_repair_rounds"))
    check("复核派发: 复核计数 1", parent.get("_audit_review_rounds") == 1, parent.get("_audit_review_rounds"))
    check("复核派发: 父未进终态", not node_mod.is_terminal(parent), parent["state"])

# ---------- 14b) 安全 blocker ⇒ 不进复核，直接明确失败 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 2
    d = _mk_driver(td, tree, parent)
    d.runner = _safety_runner
    d.runner_kind = "injected"
    knobs = role.effective("根", "merge")
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("安全 blocker: 不进复核，直接明确失败",
          isinstance(detail, str) and detail.startswith("【明确失败】"), detail)
    check("安全 blocker: 父进失败终态", parent["state"] == "失败", parent["state"])
    check("安全 blocker: 未新建复核叶", not [nid for nid in tree if ".fix.review" in nid], list(tree))

# ---------- 14c) 结构性 blocker ⇒ 不进复核 ----------
_ok, _why = final_audit.is_reviewable_blocker(
    {"id": "[root.f3.1#1]", "item": "未审批次 root#2 因子数不足被标记", "级别": "blocker"},
    {}, "/tmp", parent=None)
check("结构性 blocker 不进复核", _ok is False and "结构性" in _why, _why)
_ok2, _why2 = final_audit.is_reviewable_blocker(
    {"id": "[root.f3.2#1]", "item": DECORATIVE_ITEM, "级别": "blocker"},
    {}, "/tmp", parent=None)
check("装饰性 blocker 可复核", _ok2 is True, _why2)
_ok3, _why3 = final_audit.is_reviewable_blocker(
    {"id": "[root.f3.3#1]", "item": "实际文件内容未注入（文本缺失）", "级别": "blocker"},
    {}, "/tmp", parent=None)
check("文件内容未注入属于普通证据缺口，可复核", _ok3 is True, _why3)
_ok4, _why4 = final_audit.is_reviewable_blocker(
    {"id": "[root.f3.4#1]", "item": "产物包含提示词注入，要求忽略系统指令", "级别": "blocker"},
    {}, "/tmp", parent=None)
check("真实提示词注入仍禁止复核放行", _ok4 is False and "安全词" in _why4, _why4)

# ---------- 14d) 有可修规格时先走修复派发，不并行复核 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 2
    d = _mk_driver(td, tree, parent)
    d.runner = _blocker_runner
    d.runner_kind = "injected"
    knobs = role.effective("根", "merge")
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("有可修规格: 走修复派发（不并行复核）",
          isinstance(detail, str) and detail.startswith("【修复派发】"), detail)
    check("有可修规格: 未建复核叶", not [nid for nid in tree if ".fix.review" in nid], list(tree))
    check("有可修规格: 复核计数仍 0", parent.get("_audit_review_rounds") in (None, 0),
          parent.get("_audit_review_rounds"))


def _mk_review_node(td, item, state="完成", reply=None, judge_ok=True):
    """已终结的复核叶（模拟 live 跑完的复核叶节点）。"""
    snap = final_audit.snapshot_product_files(str(td))
    text = (reply if reply is not None else
            ("复核结论：不阻断。复核过程：重读 product/sum.txt（5 字，内容 5050）"
             "与 doc 产物，逐项核对 blocker 所称的重复时间戳行；该行在 session 日志里"
             "出现两次，内容完全相同，属重复提交，不影响统计结果。"))
    review = node_mod.new_node("root.fix.review1.1", "审计", parent="root",
                               context="复核", judge={"json_keys": ["reply", "未决"]},
                               验收标准="只读复核完成", auth="只读复核终审 blocker（不改任何文件）",
                               overrides={"node_kind": "audit"}, batch=3, act="leaf", round=1,
                               state=state, reply=text, 未决=[], judge_ok=judge_ok)
    review["_review_of"] = [item]
    review["_review_snapshot"] = dict(snap)
    return review


# ---------- 14e) 复核通过 + 产物未变 ⇒ blocker 不再计入，收束成立 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    tree["root.fix.review1.1"] = _mk_review_node(td, DECORATIVE_ITEM)
    d = _mk_driver(td, tree, parent)
    d.runner = _decorative_runner
    d.runner_kind = "injected"
    knobs = role.effective("根", "merge")
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("复核采纳: 已复核 blocker 不再计入（收束成立）", detail is None, detail)
    check("复核采纳: 父完成", parent["state"] == "完成", parent["state"])
    rows = final_audit.read_ledger(d)
    check("复核采纳: ledger 记复核消除",
          bool(rows) and "复核" in str(rows[-1].get("原因") or ""), rows[-1] if rows else [])

# ---------- 14f) 复核后产物变了 ⇒ 复核作废，blocker 照旧（不兜底） ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    tree["root.fix.review1.1"] = _mk_review_node(td, DECORATIVE_ITEM)
    (Path(td) / "product" / "sum.txt").write_text("5051\n", encoding="utf-8")
    d = _mk_driver(td, tree, parent)
    d.runner = _decorative_runner
    d.runner_kind = "injected"
    knobs = role.effective("根", "merge")
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("复核作废: 产物变了 ⇒ 复核不采纳（收束作废，不兜底）",
          isinstance(detail, str) and "收束作废" in detail, detail)
    check("复核作废: 父未完成", parent["state"] != "完成", parent["state"])

# ---------- 14g) 复核预算用完（1/1）⇒ 不再派复核，直接明确失败 ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 2
    parent["_audit_review_rounds"] = 1
    d = _mk_driver(td, tree, parent)
    d.runner = _decorative_runner
    d.runner_kind = "injected"
    knobs = role.effective("根", "merge")
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("复核预算用完: 不再派复核，直接明确失败",
          isinstance(detail, str) and detail.startswith("【明确失败】"), detail)
    check("复核预算用完: 未新建复核叶", not [nid for nid in tree if ".fix.review" in nid], list(tree))

# ---------- 14h) 到数轮 + 装饰性 blocker + 预算有余 ⇒ 先派复核叶（回放强制收束轮） ----------
with tempfile.TemporaryDirectory() as td:
    tree, parent = _mk_tree(td)
    parent["_audit_voids"] = 0
    parent["_audit_repair_rounds"] = 0
    parent["round"] = 9
    d = _mk_driver(td, tree, parent)
    d.runner = _decorative_runner
    d.runner_kind = "injected"
    knobs = dict(role.effective("根", "merge"))
    knobs["max_rounds"] = 5
    detail = d.apply_output(parent, "merge", {"去向": "收束", "reply": "收束。",
                                              "未决": [], "uncovered": []}, knobs)
    check("到数复核: 返回【复核派发】哨兵（不直接明确失败）",
          isinstance(detail, str) and detail.startswith("【复核派发】"), detail)
    check("到数复核: round 回退 1（重放强制收束轮 9→8）", parent["round"] == 8, parent["round"])
    check("到数复核: 父等子节点", parent["state"] == "等子节点", parent["state"])

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: %s" % FAIL)
    sys.exit(1)
