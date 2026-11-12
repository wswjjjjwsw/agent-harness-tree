#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓 /submit payload 的真实快照（本地 stub 接住，不联网、不打 8903）。

    TREE_ROOT=~/agent-harness /usr/bin/python3 ~/agent-harness/tests/submit-capture-demo.py

打印 3 条真实 payload 的「除 prompt 外的全部键」+ prompt 字符数，
以及「ctx_budget 改大 ⇒ max_tokens 不变 / prompt 变长」两条对照读数。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TREE_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import cases                                     # noqa: E402
from harness import role, submit                     # noqa: E402

NODES = [
    ("执行", "leaf", {"id": "n-exec", "role": "执行", "context": "照要点写一篇长文",
                     "judge": {"chars_min": 1500}}),
    ("审计", "leaf", {"id": "n-audit", "role": "审计", "context": "待审内容",
                     "judge": {"nonempty": True}}),
    ("根/spawn", "spawn", {"id": "n-root", "role": "根", "context": "上游要点",
                          "judge": {"json_keys": ["children"]}}),
]


def main() -> int:
    with cases.Stub() as stub:
        for name, act, node in NODES:
            tid, why = submit.submit(node, act=act, req_id="req1", stage="stage1",
                                     url=stub.url)
            print("SUBMIT %-9s -> id=%r rejected=%r" % (name, tid, why))
        for (name, _, _), p in zip(NODES, stub.payloads):
            b = p["body"]
            print("PAYLOAD %-9s path=%-8s %s  prompt_chars=%d"
                  % (name, p["path"],
                     json.dumps({k: v for k, v in b.items() if k != "prompt"},
                                ensure_ascii=False), len(b["prompt"])))

    print("--- ctx_budget 只拼 prompt、不进 max_tokens ---")
    node = {"id": "n-ctx", "role": "执行", "context": "长" * 30000,
            "judge": {"chars_min": 1500}}
    k0 = role.effective("执行")
    k1, rej = role.apply_overrides(k0, {"ctx_budget": 64000},
                                   sink=cases.sink_for("submit.ctx_not_maxtokens"))
    b0 = submit.map_params(node, act="leaf", req_id="req1", stage="stage1", knobs=k0)
    b1 = submit.map_params(node, act="leaf", req_id="req1", stage="stage1", knobs=k1)
    print("ctx_budget %d -> %d 时：max_tokens %d -> %d（必须不变）；prompt %d -> %d 字符（必须变长）"
          % (k0["ctx_budget"], k1["ctx_budget"], b0["max_tokens"], b1["max_tokens"],
             len(b0["prompt"]), len(b1["prompt"])))
    ok = (b0["max_tokens"] == b1["max_tokens"] and len(b1["prompt"]) > len(b0["prompt"]))
    print("CTX-CHECK: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
