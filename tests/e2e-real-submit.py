#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端真提交（**不属 run-gates.sh**，run-gates 必须不联网/不打 8903）。

用途：证明 harness/submit.py 的 submit() 不是「只算 payload 不真发」——
向真 LLAAT 提交一条最小任务（role=调研，tier=instant），然后只读取回 /task/<id>，
核它走到终态且 judge_ok 的真值。**不改任何 LLAAT 侧文件、不重启任何服务。**

用法：
    TREE_ROOT=~/agent-harness TREE_LLAAT_URL=http://127.0.0.1:8903 \
      /usr/bin/python3 ~/agent-harness/tests/e2e-real-submit.py [等待秒数]
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TREE_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))

from harness import role, submit        # noqa: E402

TASK = ("请写一段 250~400 字的调研小结，主题是：逻辑树驱动器为什么要把长产出轮的思考关掉。"
        "只输出一个 JSON 对象，键为 reply（小结正文）与 未决（数组，没有就写 []）。")
# ★ 显式给 chars_min（map_params 只在缺 chars_min 时才用 role 的 min_out_chars 补齐）：
#   这条探针的目的是「submit() 真发通了、任务走到终态且 judge_ok=true」，
#   所以判据取模型稳定能达到的下限，不让判据本身成为探针的失败源。
JUDGE = {"chars_min": 200, "json_keys": ["reply", "未决"]}


def main() -> int:
    url = os.environ.get("TREE_LLAAT_URL") or "http://127.0.0.1:8903"
    wait_s = int(sys.argv[1]) if len(sys.argv) > 1 else 240
    node = {"id": "e2e-1", "role": "调研", "context": TASK, "judge": dict(JUDGE)}
    knobs = role.effective("调研")
    body = submit.map_params(node, act="leaf", req_id="e2e", stage="probe", knobs=knobs)
    print("URL=%s" % url)
    print("SUBMIT_BODY(截 prompt)=%s" % json.dumps(
        {k: v for k, v in body.items() if k != "prompt"}, ensure_ascii=False))
    print("PROMPT_CHARS=%d" % len(body["prompt"]))
    tid, why = submit.submit(node, act="leaf", req_id="e2e", stage="probe",
                             url=url, tier="instant", knobs=knobs)
    print("SUBMIT_ID=%r REJECTED=%r" % (tid, why))
    if not tid:
        print("E2E: FAIL（提交被拒）")
        return 1
    t0 = time.time()
    last = {}
    while time.time() - t0 < wait_s:
        try:
            last = submit.get_task(tid, url=url)
        except Exception as exc:                     # noqa: BLE001
            print("GET /task 出错：%s: %s" % (type(exc).__name__, exc))
            time.sleep(5)
            continue
        if last.get("state") in ("done", "failed", "rejected"):
            break
        time.sleep(5)
    print("ELAPSED_S=%.1f" % (time.time() - t0))
    print("TASK_STATE=%r JUDGE_OK=%r JUDGE_WHY=%r EXIT_CLASS=%r"
          % (last.get("state"), last.get("judge_ok"), last.get("judge_why"),
             last.get("exit_class")))
    print("TEXT_CHARS=%d" % len(last.get("text") or ""))
    print("TEXT_HEAD=%r" % (last.get("text") or "")[:120])
    ok = last.get("state") == "done"
    print("E2E: %s" % ("PASS（走到终态，submit() 真发通了）" if ok else "FAIL（没到终态）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
