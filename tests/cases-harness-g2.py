#!/usr/bin/env python3
"""G2 regression cases for optional merge advice, hard doc checks, and audit context."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import audit, final_audit, leafrun, node as node_mod, role, schema  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:400]) if extra and not cond else ""))


TABLE = role.load_roles()

# Unknown optional advice is preserved as a note, then omitted from the executable shape.
normalized, note = schema.normalize_model_audit_replies([{
    "item": "[root.f3.1#1] audit result",
    "处置": "记未决",
    "说明": "尚待判断。",
    "建议": "补一条对 root.fix.1 的审计叶以闭合",
}])
check("free-text suggestion does not reject the merge",
      normalized is not None and len(normalized) == 1, (normalized, note))
check("free-text suggestion is not mapped to an action",
      normalized and "建议" not in normalized[0], normalized)
check("free-text suggestion is retained in explanation and normalization log",
      normalized and "补一条对 root.fix.1 的审计叶以闭合" in normalized[0]["说明"]
      and "已省略" in note, (normalized, note))
check("required audit reply shape still validates",
      not final_audit.reply_shape_problems({"对审计发现": normalized}),
      normalized)

# Unreviewed batch-size metadata is annotated only in the final-audit context when
# the real child and its incremental audit are both terminal; the append-only ledger stays intact.
with tempfile.TemporaryDirectory() as td:
    run_dir = Path(td)
    root = node_mod.new_node("root", "根", state="等子节点")
    child = node_mod.new_node("root.c2.1", "执行", parent="root", batch=2, state="完成")
    child_audit = node_mod.new_node("root.c2.1.audit", "审计", parent="root",
                                    batch=2, state="完成")
    tree = {"root": root, "root.c2.1": child, "root.c2.1.audit": child_audit}
    ledger_path = run_dir / "audit.jsonl"
    ledger_path.write_text(json.dumps({
        "批号": "root#2", "检查点id": "", "状态": "未审",
        "原因": "批次子数不足阈值（1<3）", "承载轮": 3, "发现数": 0,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    d = SimpleNamespace(run_dir=run_dir, tree=tree)
    ctx = final_audit.build_context(d)
    stored = ledger_path.read_text(encoding="utf-8")
    check("fully audited batch metadata is mechanically resolved in context",
          "已机械核验" in ctx and "现存子节点均终结且有增量审计" in ctx, ctx)
    check("mechanical context annotation preserves the source ledger",
          '"状态": "未审"' in stored and "已机械核验" not in stored, stored)

# Missing incremental audit remains open and carries the concrete missing-child evidence.
with tempfile.TemporaryDirectory() as td:
    run_dir = Path(td)
    root = node_mod.new_node("root", "根", state="等子节点")
    child = node_mod.new_node("root.c2.1", "执行", parent="root", batch=2, state="完成")
    tree = {"root": root, "root.c2.1": child}
    (run_dir / "audit.jsonl").write_text(json.dumps({
        "批号": "root#2", "检查点id": "", "状态": "未审",
        "原因": "批次子数不足阈值（1<3）", "承载轮": 3, "发现数": 0,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    ctx = final_audit.build_context(SimpleNamespace(run_dir=run_dir, tree=tree))
    check("missing audit stays unresolved with exact child",
          '"状态": "未审"' in ctx and "root.c2.1 缺完成态增量审计" in ctx, ctx)

# Structural audit resolution is scoped to the named parent's direct batch members.
# Descendants and same-number batches under other parents must not create false blockers.
with tempfile.TemporaryDirectory() as td:
    root = node_mod.new_node("root", "根", state="等子节点")
    child = node_mod.new_node("root.c2.1", "执行", parent="root", batch=2, state="完成")
    child_audit = node_mod.new_node("root.c2.1.audit", "审计", parent="root",
                                    batch=2, state="完成")
    nested_parent = node_mod.new_node("root.branch", "执行", parent="root", batch=1,
                                      state="完成")
    nested_child = node_mod.new_node("root.branch.c2.1", "执行", parent="root.branch",
                                     batch=2, state="完成")
    tree = {n["id"]: n for n in (root, child, child_audit, nested_parent, nested_child)}
    d = SimpleNamespace(run_dir=Path(td), tree=tree)
    status, detail = final_audit.verify_structural_blocker(
        "未审批次 root#2 批次子数不足阈值", d, "root")
    check("structural verification ignores same-batch descendants under other parents",
          status == "dropped" and "root.branch.c2.1" not in detail, (status, detail))
    other_status, other_detail = final_audit.verify_structural_blocker(
        "未审批次 root#2 批次子数不足阈值", d, "root.branch")
    check("batch marker must name the exact parent being verified",
          other_status == "kept" and "root.branch" in other_detail,
          (other_status, other_detail))

# A failed incremental audit is not evidence that a child was audited successfully.
with tempfile.TemporaryDirectory() as td:
    root = node_mod.new_node("root", "根", state="等子节点")
    child = node_mod.new_node("root.c2.1", "执行", parent="root", batch=2, state="完成")
    child_audit = node_mod.new_node("root.c2.1.audit", "审计", parent="root",
                                    batch=2, state="失败")
    tree = {"root": root, child["id"]: child, child_audit["id"]: child_audit}
    d = SimpleNamespace(run_dir=Path(td), tree=tree)
    status, detail = final_audit.verify_structural_blocker(
        "未审批次 root#2 批次子数不足阈值", d, "root")
    check("failed incremental audit keeps the structural blocker",
          status == "kept" and "缺完成态增量审计" in detail, (status, detail))

# Existing disk_doc_check is only a source hint: repair_specs remeasures the current file,
# including when an old snapshot says complete/judge=None.
with tempfile.TemporaryDirectory() as td:
    run_dir = Path(td)
    (run_dir / "doc").mkdir()
    path = run_dir / "doc" / "agg_report.md"
    path.write_text("x" * 1340, encoding="utf-8")
    root = node_mod.new_node("root", "根", state="在跑")
    leaf = node_mod.new_node("root.c2.2.retry1", "执行", parent="root", batch=2,
                             state="完成", context="写 doc/agg_report.md，不少于 1500 字")
    leaf["judge_ok"] = None
    leaf["disk_doc_check"] = {"chars_min": 1500, "named": {"doc/agg_report.md": 1340},
                              "written": {"doc/agg_report.md": 1340}, "inline_chars": None}
    tree = {"root": root, leaf["id"]: leaf}
    specs = final_audit.repair_specs([], tree, str(run_dir), parent=root)
    doc_specs = [s for s in specs if s.get("target") == "doc/agg_report.md"]
    check("completed old snapshot with hard doc failure yields a repair spec",
          len(doc_specs) == 1 and doc_specs[0].get("chars_min") == 1500,
          doc_specs)
    path.write_text("x" * 1600, encoding="utf-8")
    repaired_specs = final_audit.repair_specs([], tree, str(run_dir), parent=root)
    check("repaired file is not re-dispatched from stale written count",
          not [s for s in repaired_specs if s.get("target") == "doc/agg_report.md"],
          repaired_specs)

# A passing audit model cannot bypass a known hard disk-doc gap.
with tempfile.TemporaryDirectory() as td:
    run_dir = Path(td) / "run"
    (run_dir / "doc").mkdir(parents=True)
    (run_dir / "doc" / "agg_report.md").write_text("x" * 1340, encoding="utf-8")
    root = node_mod.new_node("root", "根", state="在跑", round=2)
    leaf = node_mod.new_node("root.c2.2.retry1", "执行", parent="root", batch=2,
                             state="完成", context="写 doc/agg_report.md，不少于 1500 字")
    leaf["judge_ok"] = None
    leaf["disk_doc_check"] = {"chars_min": 1500, "named": {"doc/agg_report.md": 1340},
                              "written": {"doc/agg_report.md": 1340}, "inline_chars": None}
    tree = {"root": root, leaf["id"]: leaf}
    class _D:
        pass
    d = _D()
    d.run_dir = run_dir
    d.tree = tree
    d.table = TABLE
    d.run_id = "g2-hard-doc"
    d.sink = str(run_dir / "violations.jsonl")
    d.runner = lambda *args, **kwargs: json.dumps(
        {"reply": "审计无发现。", "未决": []}, ensure_ascii=False)
    ok, why = final_audit.on_converge(d, root, {"去向": "收束"})
    check("terminal audit cannot pass a known short doc",
          not ok and any("doc/agg_report.md" in b.get("item", "")
                         for b in root.get("_audit_cap_blockers", [])),
          (ok, why, root.get("_audit_cap_blockers")))

# A new live leaf records an explicit mechanical judge failure when its doc is short,
# even if its model judge is natural-language and returned no executable result.
with tempfile.TemporaryDirectory() as td:
    run_dir = Path(td) / "run"
    run_dir.mkdir(parents=True)
    sessions = Path(td) / "sessions"
    sessions.mkdir()
    root = node_mod.new_node("root", "根", context="task", auth="read")
    tree = {"root": root}
    def _stub(node, act, knobs, prompt, session_id, root_dir, timeout=None):
        doc = Path(root_dir) / "doc" / "agg_report.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("x" * 1340, encoding="utf-8")
        return json.dumps({"reply": "已写 doc/agg_report.md。", "未决": [], "doc": "x"},
                          ensure_ascii=False)
    timed = leafrun.TimedRunner(_stub)
    d = leafrun.TreeDriver(root, tree, TABLE, run_id="g2-hard-doc-live",
                           run_dir=str(run_dir), act_runner=timed, timed=timed)
    leaf = node_mod.new_node("root.c2.2.retry1", "执行", parent="root", batch=2,
                             context="写 doc/agg_report.md，不少于 1500 字",
                             judge="内容完整", 验收标准="doc/agg_report.md 不少于 1500 字",
                             auth="写 doc/agg_report.md")
    tree[leaf["id"]] = leaf
    old_sessions = os.environ.get("TREE_PI_AGENT_SESSION_DIR")
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(sessions)
    try:
        row = leafrun.run_leaf(d, leaf)
    finally:
        if old_sessions is None:
            os.environ.pop("TREE_PI_AGENT_SESSION_DIR", None)
        else:
            os.environ["TREE_PI_AGENT_SESSION_DIR"] = old_sessions
    check("short doc makes judge explicit false",
          row.get("judge_ok") is False and leaf.get("judge_ok") is False,
          row)
    check("short doc ends as failed without losing the disk measurement",
          row.get("state") == "失败" and row.get("result_type") == "failed"
          and row.get("disk_doc_check", {}).get("named", {}).get("doc/agg_report.md") == 1340,
          row)
    check("short doc failure states the mechanical reason",
          "落盘 doc 未达标" in row.get("judge_why", ""), row.get("judge_why"))

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    sys.exit(1)
