#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阴性对照（可原样重跑版）：改 role.yaml【副本】里的一个字符 ⇒ 断言必须翻面。

证明「执行 ⇒ needs_thinking=False」读的是**数据文件**，不是硬编码。
只用一条命令即可复现：
    TREE_ROOT=~/agent-harness /usr/bin/python3 ~/agent-harness/tests/neg-control-demo.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TREE_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import cases                                     # noqa: E402  （复用同一套副本改写工具）
from harness import role, submit                     # noqa: E402

NODE = {"id": "neg-1", "role": "执行", "context": "x", "judge": {"chars_min": 1500}}


def main() -> int:
    base = role.effective("执行")
    b0 = submit.map_params(dict(NODE), act="leaf", req_id="req1", stage="stage1")
    print("A 原表       : 执行.thinking=%r  needs_thinking=%r  max_tokens=%r"
          % (base["thinking"], b0["needs_thinking"], b0["max_tokens"]))

    src = cases.roles_copy("role.demo-base.yaml")
    dst = cases.rewrite_role_knob(src, str(cases.TMP / "role.demo-neg.yaml"),
                                  "执行", "thinking", "low")
    tbl = role.load_roles(dst)
    k2 = role.effective("执行", None, tbl)
    b1 = submit.map_params(dict(NODE), act="leaf", req_id="req1", stage="stage1", knobs=k2)
    print("B 副本改一字 : 执行.thinking=%r  needs_thinking=%r  max_tokens=%r"
          % (k2["thinking"], b1["needs_thinking"], b1["max_tokens"]))
    print("副本=%s" % dst)
    flipped = (b0["needs_thinking"] is False) and (b1["needs_thinking"] is True)
    print("NEG-CONTROL: %s" % ("PASS（断言翻面 ⇒ 判据读的是数据文件）" if flipped else
                               "FAIL（没翻面 ⇒ 判据可能是硬编码）"))
    return 0 if flipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
