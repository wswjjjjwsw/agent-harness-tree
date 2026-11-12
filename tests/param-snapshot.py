#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「role 旋钮 → LLAAT 参数」实际映射快照（issue 验收④ 要求的那张表）。

由**真跑代码**产出（不是手抄）：6 个 role × 各自适用的 act，逐行打印
  role | act | needs_thinking | max_tokens | kind | retry_max | ctx_budget | max_rounds
判据（needs_thinking / max_tokens / kind / retry_max）全部由 submit.map_params() 给出，
ctx_budget / max_rounds 由 role.effective() 给出 —— 与真正提交时用的是同一条代码路径。

用法（唯一入口之外的只读快照工具）：
    TREE_ROOT=~/agent-harness /usr/bin/python3 ~/agent-harness/tests/param-snapshot.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TREE_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))

from harness import role, submit          # noqa: E402

# 每个 role 适用的 act：父三个 role 有 plan/spawn/merge；叶子只有 leaf（76 §4.5.3 / §3.3）
PARENT_ROLES = [n for n, r in role.load_roles()["roles"].items() if r["may_spawn"]]
LEAF_ROLES = [n for n, r in role.load_roles()["roles"].items() if not r["may_spawn"]]

ROWS = []
for n in PARENT_ROLES:
    for act in role.ACTS:
        ROWS.append((n, act))
for n in LEAF_ROLES:
    ROWS.append((n, "leaf"))

HDR = ("role", "act", "needs_thinking", "max_tokens", "kind", "retry_max",
       "ctx_budget", "max_rounds")


def main() -> int:
    print("| " + " | ".join(HDR) + " |")
    print("|" + "|".join(["---"] * len(HDR)) + "|")
    table = []
    for rname, act in ROWS:
        k = role.effective(rname, act if act in role.ACTS else None)
        node = {"id": "snap-%s-%s" % (rname, act), "role": rname, "context": "快照用上下文",
                "judge": {"chars_min": int(k["min_out_chars"] or 1)}}
        b = submit.map_params(node, act=act, req_id="snap", stage=act, knobs=k)
        row = {"role": rname, "act": act,
               "needs_thinking": b["needs_thinking"], "max_tokens": b["max_tokens"],
               "kind": b["kind"], "retry_max": b["retry_max"],
               "ctx_budget": k["ctx_budget"], "max_rounds": k["max_rounds"]}
        table.append(row)
        print("| " + " | ".join(str(row[h]) for h in HDR) + " |")
    print()
    print("TREE_ROOT=%s python=%s" % (ROOT, sys.version.split()[0]))
    print("ROWS=%d" % len(table))
    print("SNAPSHOT_JSON=" + json.dumps(table, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
