#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桩 act 脚本：`stub-act.py <act> <node_id> <round>`，prompt 走 stdin，产出正文走 stdout。

★ 由 $TREE_ACT_CMD 指到这里 ⇒ 一个 pi 进程都不许起（tests/run-multiround.sh 会证明）。
  产出形状 = 契约 JSON（plan ⇒ 要点 / spawn ⇒ children / merge ⇒ 去向+reply+未决）。
  merge 的「去向」：$TREE_STUB_MERGE 指定则一律用它；否则 round < 7 ⇒ 再派，否则 ⇒ 收束。
  $TREE_STUB_CHOICE=STALE ⇒ 造一个不合契约的产出（缺 uncovered）；=RAW ⇒ 造一个非 JSON 的产出。
  $TREE_STUB_TRACE=<path> ⇒ 每被调一次追加一行（证明桩真被调到、且拿到的是哪个 act/round）。
"""
from __future__ import annotations

import json
import os
import sys

CHILD = {"role": "执行", "context": "写第 1 段", "judge": {"chars_min": 120},
         "验收标准": "写完第 1 段且非空", "auth": "doc/r2.1.md"}


def merge(go: str, node_id: str, rnd: int) -> dict:
    """按去向拼 merge 契约。切分目标 = 上一轮 spawn 拉出来的那个子。"""
    n = max(1, rnd - 3)
    obj = {"去向": go, "reply": "本轮收拢结论：%s（第 %d 次）" % (go, n), "未决": []}
    if go == "再派":
        obj["下一批要点"] = ["再派第 %d 批的第 1 块" % n]
    elif go == "切分":
        obj["下一批要点"] = ["切分第 %d 批" % n]
        # 子 id 形态：<父>.c<spawn 轮号>.<序号>；spawn 轮 = 本轮轮号 - 1（merge 紧跟在 spawn 后）
        obj["切分目标"] = ["%s.c%d.1" % (node_id, max(1, rnd - 1))]
    else:
        obj["uncovered"] = [{"what": "本层剩下的第 %d 条" % n, "why": "轮到数/已敲定"}]
    return obj


def main() -> int:
    act = sys.argv[1] if len(sys.argv) > 1 else ""
    nid = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        rnd = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    except ValueError:
        rnd = 0
    try:
        prompt = sys.stdin.read()
    except Exception:                                        # noqa: BLE001
        prompt = ""
    trace = os.environ.get("TREE_STUB_TRACE")
    if trace:
        with open(trace, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"act": act, "node_id": nid, "round": rnd,
                                 "prompt_chars": len(prompt)}, ensure_ascii=False) + "\n")
    choice = os.environ.get("TREE_STUB_CHOICE") or ""
    if choice == "STALE":
        print(json.dumps({"去向": "收束", "reply": "缺 uncovered"}, ensure_ascii=False))
        return 0
    if choice == "RAW":
        print("这不是 JSON，模型胡说了。")
        return 0
    if act == "plan":
        obj = {"要点": ["这一层要两块：第 1 段 / 第 2 段"]}
    elif act == "spawn":
        obj = {"children": [dict(CHILD)]}
    elif act == "merge":
        go = os.environ.get("TREE_STUB_MERGE") or ("再派" if rnd < 7 else "收束")
        obj = merge(go, nid, rnd)
    else:
        obj = {"reply": "桩：未知 act %s" % act, "未决": []}
    print(json.dumps(obj, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
