#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例脚本：跑一遍「越权 + 截断」，落出 violations.jsonl 的真实样例行（报告 §5 用）。

    TREE_ROOT=~/agent-harness /usr/bin/python3 ~/agent-harness/tests/sample-violations.py [run-id]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TREE_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))

from harness import gate, role, schema        # noqa: E402


def main() -> int:
    run_id = sys.argv[1] if len(sys.argv) > 1 else "demo"
    sink = os.path.join(role.DEFAULT_RUNS_DIR, run_id, "violations.jsonl")
    print("SINK=%s" % sink)

    # ① 越权：may_spawn=false 的 role 返回 children ⇒ 丢弃 + 记一笔
    obj = {"id": "node-执行-7", "reply": "x", "未决": [],
           "children": [{"role": "执行", "context": "偷拉的"}]}
    clean, why = schema.check_permission("leaf", obj, role.effective("执行"), sink=sink)
    print("越权_children: clean=%r why=%r obj=%s"
          % (clean, why, {k: obj[k] for k in obj}))

    # ② 越权：覆盖 thinking ⇒ 拒 + 记一笔
    _k, rej = role.apply_overrides(role.effective("执行"), {"thinking": "low"},
                                   sink=sink, node_id="node-执行-7")
    print("越权_overrides: rejected=%s" % rej)

    # ③ 截断：每轮 4 个子 ⇒ 收 3 丢 1 + 记一笔
    kept, dropped = gate.truncate_children(
        [{"id": "c%d" % i} for i in range(1, 5)], role.effective("根"), sink=sink)
    print("截断_children: kept=%s dropped=%s"
          % ([c["id"] for c in kept], [c["id"] for c in dropped]))

    print("--- violations.jsonl 全文 ---")
    print(Path(sink).read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
