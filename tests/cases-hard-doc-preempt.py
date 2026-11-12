#!/usr/bin/env python3
"""A real 227-character disk-doc failure must preempt a malformed first merge."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import driver, node as node_mod


def check(name: str, condition: bool, detail="") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail!r}")
    print(f"PASS {name}")


with tempfile.TemporaryDirectory() as td:
    target = Path(td) / "blocks/some-prime-calculation.md"
    target.parent.mkdir(parents=True)
    target.write_text("x" * 227, encoding="utf-8")

    parent = node_mod.new_node(
        "root", "根", round=2, act="spawn", batch=2, state=node_mod.WAIT_CHILDREN)
    tree = {"root": parent}
    for nid, state in (
        ("root.c2.1", "完成"),
        ("root.c2.2", "失败"),
        ("root.c2.3", "完成"),
    ):
        tree[nid] = node_mod.new_node(
            nid, "执行", parent="root", batch=2, state=state,
            context="write blocks/some-prime-calculation.md",
            验收标准="document chars_min 1500",
            judge={"chars_min": 1500} if nid == "root.c2.2" else None)
    tree["root.c2.2"]["disk_doc_check"] = {
        "chars_min": 1500, "named": {},
        "written": {"blocks/some-prime-calculation.md": 227},
        "inline_chars": 227,
    }
    Path(td, "audit.jsonl").write_text(
        json.dumps({"批号": "root#2", "检查点id": "root.a2.1.1",
                    "状态": "已审"}) + "\n",
        encoding="utf-8")

    calls = []

    def malformed_first_merge(node, act, knobs, prompt, session_id, run_dir):
        calls.append(act)
        return json.dumps({"去向": "收束", "reply": "Q01 complete", "未决": []})

    d = driver.Driver(parent, tree, run_dir=td)
    d.runner = malformed_first_merge
    d.runner_kind = "test"
    row = d.step()
    repair_id = "root.fix.audit1.1"

    check("hard doc bypasses the malformed merge call", calls == [], calls)
    check("row records the exact mechanical target",
          row.get("hard_doc_preempt") == ["blocks/some-prime-calculation.md"], row)
    check("existing repair path gets a chars_min=1500 child",
          repair_id in tree and tree[repair_id]["judge"] == {"chars_min": 1500},
          tree.get(repair_id))
    check("source hard failure and 227-char artifact remain intact",
          tree["root.c2.2"]["state"] == "失败"
          and tree["root.c2.2"]["disk_doc_check"]["written"][
              "blocks/some-prime-calculation.md"] == 227
          and len(target.read_text(encoding="utf-8")) == 227,
          tree["root.c2.2"])
    check("parent waits within its existing repair budget",
          parent["state"] == node_mod.WAIT_CHILDREN
          and parent.get("_audit_repair_rounds") == 1
          and not parent.get("merge"),
          {"state": parent["state"], "repairs": parent.get("_audit_repair_rounds"),
           "merge": parent.get("merge")})
