#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性离线桩：`stub-prime.py <act> <node_id> <round>`，prompt 走 stdin，正文走 stdout。

它代替真实模型，只证明**驱动器 + 机械验收 + 修复闭环**跑得通，不证明模型能力：
  plan   ⇒ 本层要点
  spawn  ⇒ 一个核对报告与权威 oracle 的执行叶
  leaf   ⇒ 全量终审时按磁盘事实产出冲突 blocker；修复叶返回改好的完整文档
  merge  ⇒ 收束（带 uncovered=[]），把收束交给终审

所有判断都读工作区磁盘内容（$TREE_DEMO_WORKDIR），因此：
  · 报告已是 168 ⇒ 终审不再报冲突（负例：不制造假阳性）；
  · 报告仍是 186 ⇒ 报冲突，由驱动器派修复叶改回 168。
"""
from __future__ import annotations

import json
import os
import re
import sys

REPORT = "blocks/prime-report.md"
ORACLE = "input/prime-oracle.json"
WRONG = "186 primes"
RIGHT = "168 primes"


def _workdir(prompt: str) -> str:
    """run 目录：优先环境变量，其次从修复叶 prompt 的「工作区绝对路径」行读。"""
    env = os.environ.get("TREE_DEMO_WORKDIR")
    if env:
        return env
    for line in (prompt or "").splitlines():
        match = re.search(r"工作区绝对路径（run 目录）[:：]\s*(.+)$", line.strip())
        if match:
            return match.group(1).strip()
    return ""


def _report_text(root: str) -> str:
    try:
        with open(os.path.join(root, REPORT), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def main() -> int:
    act = sys.argv[1] if len(sys.argv) > 1 else ""
    node_id = sys.argv[2] if len(sys.argv) > 2 else ""
    prompt = sys.stdin.read()
    root = _workdir(prompt)

    if act == "plan":
        out = {"要点": ["核对 %s 与 %s 的素数计数" % (REPORT, ORACLE)]}
    elif act == "spawn":
        out = {"children": [{
            "role": "执行",
            "context": "读取 %s 与 %s，核对 prime_count" % (ORACLE, REPORT),
            "judge": {"json_keys": ["reply"]},
            "验收标准": "说明报告里的素数计数与权威 oracle 是否一致",
            "auth": "doc/",
        }]}
    elif act == "merge":
        out = {"去向": "收束", "reply": "本批核对完成，收束交终审", "未决": [], "uncovered": []}
    elif "全量终审" in prompt:
        text = _report_text(root)
        if WRONG in text:
            out = {"reply": "终审：报告与权威 oracle 的素数计数不一致。",
                   "未决": [{
                       "type": "冲突",
                       "what": "BLOCKER %s 写 186，与权威 %s 的 prime_count=168 不一致"
                               % (REPORT, ORACLE),
                       "why": "186 ≠ 168",
                   }]}
        else:
            out = {"reply": "终审：报告与权威 oracle 一致，无 blocker。", "未决": []}
    elif ".fix." in node_id:
        text = _report_text(root)
        out = {"reply": "已按权威 oracle 把报告改为 168", "未决": [],
               "doc": text.replace(WRONG, RIGHT)}
    else:
        body = "已读 %s：prime_count=168；报告见 %s" % (ORACLE, REPORT)
        out = {"reply": body, "未决": [], "doc": body + "\n"}

    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
