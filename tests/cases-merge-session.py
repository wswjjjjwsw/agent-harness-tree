#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge 会话轮换回归（第二轮 D，集成验收 t-132692485362）。

跑法：cd <候选根> && python3 tests/cases-merge-session.py
根因（integ-q01 实测）：同一 root merge 会话内 prompt 估算逐轮 +~10K
（26,079 → 36,278 → 46,668 → 57,057），2 个任务单会话超 32K。
修复：merge 每 2 轮换一个 pi 会话（sid 后缀 -m<epoch>）；换会话的 merge 轮
仍拿全量注入（契约 base + batch_view 等增量），不丢上下文。
  1. 4 轮 merge 的 session_id：前 2 轮同一 base sid，后 2 轮同一 -m1 sid；
  2. 换会话的 merge prompt 仍含 batch_view（子 reply 原文）与契约（"去向"）；
  3. build_prompt 纯函数：首轮 merge 也含 batch_view（旧代码只返契约）；
  4. 非 merge 轮（plan/spawn）不轮换：sid 始终是 base。
"""
import copy, json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import driver, node as node_mod, role

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

CHILD = {"role": "执行", "context": "本批第 1 块的落点", "judge": {"chars_min": 120},
         "验收标准": "第 1 块写完且非空", "auth": "doc/s1.md"}
CHILD2 = {"role": "执行", "context": "本批第 2 块的落点", "judge": {"chars_min": 120},
          "验收标准": "第 2 块写完且非空", "auth": "doc/s2.md"}


def _table():
    t = copy.deepcopy(role.load_roles())
    t["roles"]["根"]["max_rounds"] = 20
    return t


def _run_merges(td):
    """plan → spawn → merge ×4（每轮 merge 之间 spawn 一批新子并终结），返回 (rows, prompts)。"""
    calls = []
    def runner(node, act, knobs, prompt, session_id, run_dir):
        calls.append({"act": act, "sid": session_id, "prompt": prompt or ""})
        if act == "plan":
            return json.dumps({"要点": ["这一层要几块：第 1 块 / 第 2 块"]}, ensure_ascii=False)
        if act == "spawn":
            return json.dumps({"children": [dict(CHILD), dict(CHILD2)]}, ensure_ascii=False)
        if act == "merge":
            return json.dumps({"去向": "再派", "下一批要点": ["再派一批的第 1 块"],
                               "reply": "本轮收拢结论：再派", "未决": []}, ensure_ascii=False)
        return json.dumps({"reply": "桩：未预期的 act。", "未决": []}, ensure_ascii=False)
    tr = {"root": node_mod.new_node("root", "根")}
    d = driver.Driver(tr["root"], tr, table=_table(), run_id="merge.session",
                      run_dir=str(td), act_runner=runner)
    rows = []
    rows.append(d.step())  # plan round 1
    rows.append(d.step())  # spawn round 2
    merges = []
    for _ in range(4):
        for cid in node_mod.batch_members(tr["root"], tr):
            tr[cid]["state"] = "完成"
            tr[cid]["reply"] = "第 1 块已完成-UNIQUE-%s" % cid
        r = d.step()  # merge
        merges.append(r)
        rows.append(r)
        if node_mod.is_terminal(tr["root"]):
            break
        r2 = d.step()  # spawn 下一批
        rows.append(r2)
    return rows, merges, calls, tr


with tempfile.TemporaryDirectory() as td:
    rows, merges, calls, tr = _run_merges(Path(td))
    check("轮换: 跑出 4 轮 merge", len(merges) == 4, [r.get("round") for r in merges])
    sids = [r.get("session_id") for r in merges]
    base = sids[0] if sids else ""
    check("轮换: merge#1/#2 同一 base sid", len(sids) == 4 and sids[0] == sids[1], sids)
    check("轮换: merge#3/#4 同一 -m1 sid",
          len(sids) == 4 and sids[2] == sids[3] and sids[2] == base + "-m1", sids)
    check("轮换: -m1 与 base 不同", len(sids) == 4 and sids[2] != base, sids)
    m3 = [c for c in calls if c["act"] == "merge"][2] if len(
        [c for c in calls if c["act"] == "merge"]) > 2 else {}
    check("轮换: 换会话的 merge prompt 仍含 batch_view（子 reply 原文）",
          "UNIQUE" in (m3.get("prompt") or ""), (m3.get("prompt") or "")[:200])
    check("轮换: 换会话的 merge prompt 仍含契约（去向）",
          "去向" in (m3.get("prompt") or ""), (m3.get("prompt") or "")[:200])
    plan_sid = [c["sid"] for c in calls if c["act"] == "plan"]
    spawn_sids = [c["sid"] for c in calls if c["act"] == "spawn"]
    check("轮换: plan/spawn 不轮换（始终 base sid）",
          plan_sid == [base] and all(s == base for s in spawn_sids),
          (plan_sid, spawn_sids))

# ---------- 3) build_prompt 纯函数：首轮 merge 也含 batch_view ----------
with tempfile.TemporaryDirectory() as td:
    tr = {"root": node_mod.new_node("root", "根", round=3, act="spawn", batch=2)}
    tr["root.c2.1"] = node_mod.new_node("root.c2.1", "执行", parent="root", batch=2,
                                        state="完成", reply="第 1 块已完成-UNIQUE")
    tr["root"]["_tree"] = tr
    tr["root"]["_run_dir"] = str(td)
    knobs = role.effective("根", "merge")
    p1 = driver.build_prompt(tr["root"], "merge", knobs, role.load_roles(),
                             first_round=True, stateless=False)
    check("首轮 merge: 含 batch_view（子 reply 原文）", "UNIQUE" in p1, p1[:200])
    check("首轮 merge: 含契约", "去向" in p1, p1[:200])
    p2 = driver.build_prompt(tr["root"], "merge", knobs, role.load_roles(),
                             first_round=False, stateless=False)
    check("非首轮 merge: 仍含 batch_view", "UNIQUE" in p2, p2[:200])

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: %s" % FAIL)
    sys.exit(1)
