#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree-03 多轮回路 —— 全部用例（唯一验收入口由 tests/run-multiround.sh 调起）。

★ 桩驱动：**不打模型、不占单卡、可秒级重跑**（$TREE_ACT_CMD 指到 tests/stub-act.py）。
★ 每个「正例」都配一条**阴性对照**（同一条判据必须有区分力），末行打印对照条数。
★ 任一用例 FAIL ⇒ rc ≠ 0；total < 19 或 阴性对照 < 3 ⇒ 不合格。
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TREE_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))

from harness import driver, gate, node as node_mod, role, schema, session  # noqa: E402

TMP = HERE / "tmp" / "tree03"
STUB = str(HERE / "stub-act.py")
CASES, NC = [], []
CHILD = {"role": "执行", "context": "写第 1 段", "judge": {"chars_min": 120},
         "验收标准": "写完第 1 段且非空", "auth": "doc/r2.1.md"}

# task-def §2.1 的 20 行字段表（逐字，含「谁写」列）—— 用来机械核对 FIELDS
TABLE = [("id", "代码"), ("parent", "代码"), ("state", "代码"), ("result", "代码"),
         ("llaat_id", "代码"), ("context", "父"), ("judge", "父"), ("验收标准", "父"),
         ("merge", "父"), ("role", "父"), ("overrides", "父"), ("round", "代码"),
         ("act", "代码"), ("role_prompt", "role表"), ("auth", "父"), ("reply", "模型"),
         ("未决", "模型"), ("judge_ok", "LLAAT"), ("judge_why", "LLAAT"), ("batch", "代码")]


def case(name, negative_control=False):
    def deco(fn):
        CASES.append((name, fn))
        if negative_control:
            NC.append(name)
        return fn
    return deco


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ---------------------------------------------------------------- 工具
def mkd(name: str) -> Path:
    """每个用例一个干净 run 目录（只删自己建的 tmp，不碰别的）。"""
    d = TMP / "runs" / name
    if d.exists():
        shutil.rmtree(d)
    (d / "doc").mkdir(parents=True, exist_ok=True)
    return d


def table_with(role_name: str, **knob_over):  # noqa: ANN003
    """role.yaml 的**副本**，把某个 role 的旋钮改掉（证明值跟着数据走，不是写死在代码里）。"""
    t = copy.deepcopy(role.load_roles())
    t["roles"][role_name].update(knob_over)
    return t


def new_root(**kw) -> dict:  # noqa: ANN003
    n = node_mod.new_node("root", "根")
    n.update(kw)
    return n


def terminate_batch(parent: dict, tree: dict, state: str = "完成") -> list:
    """把本批次的子全推成终态（模拟子跑完）。"""
    ids = node_mod.batch_members(parent, tree)
    for cid in ids:
        tree[cid]["state"] = state
    return ids


def rows_of(run_dir: Path) -> list:
    p = run_dir / "rounds.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if p.exists() else []


def log_of(run_dir: Path) -> str:
    p = run_dir / "driver.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def vio_of(run_dir: Path, kind: str = None) -> list:
    rows = gate.read_violations(str(run_dir / "violations.jsonl"))
    return [r for r in rows if kind is None or r["kind"] == kind]


def drive_cycle(d, tr, merge_go: str, tag: str = ""):
    """跑一轮完整循环（plan→spawn→[等]→merge），返回这一循环的 (act, round) 序列。"""
    seq = []
    os.environ["TREE_STUB_MERGE"] = merge_go
    for _i in range(4):
        row = d.step()
        if row.get("rounds_advanced"):
            seq.append((row["act"], row["round"]))
        if row.get("act") == "spawn" and not row.get("rejected"):
            terminate_batch(tr["root"], tr)
        if row.get("act") == "merge" and row.get("rounds_advanced"):
            break
    print("      %s一个循环：%s ⇒ state=%s round=%s batch=%s" % (
        tag, seq, tr["root"]["state"], tr["root"]["round"], tr["root"]["batch"]))
    return seq


# ================================================================ 1 字段表
@case("fields_table_20: FIELDS 恰 20 项且与 task-def §2.1 表逐字一致")
def t_fields(_):
    check(len(node_mod.FIELDS) == 20, "FIELDS 不是 20 项：%d" % len(node_mod.FIELDS))
    check(list(node_mod.FIELDS) == [k for k, _ in TABLE],
          "FIELDS 与表不符：%s" % list(node_mod.FIELDS))
    bad = [(k, node_mod.FIELD_OWNER.get(k), w) for k, w in TABLE
           if node_mod.FIELD_OWNER.get(k) != w]
    n = node_mod.new_node("x", "执行")
    errs = node_mod.validate_fields(n)
    print("      FIELDS(%d)=%s" % (len(node_mod.FIELDS), list(node_mod.FIELDS)))
    print("      「谁写」列不符的项=%s（应为空）   新建节点字段数=%d" % (bad, len(n)))
    check(not bad, "谁写列对不上：%s" % bad)
    check(not errs and len(n) == 20, "新建节点字段不齐：%s" % errs)


# ================================================================ 2 完整多轮
@case("multi_round_sequence: plan→spawn→merge(再派)→spawn→merge(收束)，round 单调 +1")
def t_multi_round(_):
    d0 = mkd("multi.rounds")
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, run_id="multi.rounds", run_dir=str(d0))
    seq = drive_cycle(d, tr, "再派", tag="第 1 ")
    seq += drive_cycle(d, tr, "收束", tag="第 2 ")
    rows = [r for r in rows_of(d0) if r.get("rounds_advanced")]
    print("      全部轮次流水：%s" % [(r["act"], r["round"]) for r in rows])
    print("      父终态: state=%s round=%s" % (tr["root"]["state"], tr["root"]["round"]))
    check(seq == [("plan", 1), ("spawn", 2), ("merge", 3), ("spawn", 4), ("merge", 5)],
          "轮次序列不对：%s" % seq)
    check(tr["root"]["state"] == "完成", "收束后父应是完成：%s" % tr["root"]["state"])
    check("强制收束" not in log_of(d0), "max_rounds=8 不该有强制收束行")
    os.environ.pop("TREE_STUB_MERGE", None)


# ================================================================ 3 轮到数
@case("forced_converge: max_rounds=2 ⇒ 第 3 轮的「再派」被改写成收束 + 可 grep 日志")
def t_forced(_):
    d0 = mkd("forced.low")
    t = table_with("根", max_rounds=2)
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, table=t, run_id="forced.low", run_dir=str(d0))
    drive_cycle(d, tr, "再派", tag="第 1 ")
    drive_cycle(d, tr, "再派", tag="第 2 ")          # 桩在第 3 轮仍返回「再派」
    rows = rows_of(d0)
    third = [r for r in rows if r.get("round") == 3 and r.get("rounds_advanced")][0]
    log = log_of(d0)
    vio = vio_of(d0, "驱动_产出形状")
    print("      第 3 轮流水: act=%s 桩给=再派 改写成=%s round=%s rounds_advanced=%s" % (
        third["act"], third.get("rewritten_to"), third["round"], third["rounds_advanced"]))
    print("      driver.log: %s" % log.strip())
    print("      违规（驱动_产出形状）%d 笔：%s" % (len(vio), vio[0]["detail"] if vio else ""))
    print("      父终态: state=%s round=%s" % (tr["root"]["state"], tr["root"]["round"]))
    check(third["act"] == "merge" and third.get("rewritten_to") == "收束",
          "第 3 轮没被改写成收束：%s" % third)
    check(tr["root"]["state"] == "完成", "父终态不是完成：%s" % tr["root"]["state"])
    check(tr["root"]["round"] == 3, "round 应停在 3，实际 %s" % tr["root"]["round"])
    check("强制收束" in log, "driver.log 缺「强制收束」行：%r" % log)
    check(len(vio) == 1, "改写应记恰 1 笔，实际 %d" % len(vio))
    os.environ.pop("TREE_STUB_MERGE", None)


@case("neg_ctl_forced_high: 同一 fixture、max_rounds=99 ⇒ 同一个「再派」不被改写、无强制收束行",
      negative_control=True)
def t_not_forced(_):
    d0 = mkd("forced.high")
    t = table_with("根", max_rounds=99)
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, table=t, run_id="forced.high", run_dir=str(d0))
    drive_cycle(d, tr, "再派", tag="第 1 ")
    drive_cycle(d, tr, "再派", tag="第 2 ")
    rows = rows_of(d0)
    third = [r for r in rows if r.get("round") == 3 and r.get("rounds_advanced")][0]
    log = log_of(d0)
    print("      第 3 轮流水: 改写成=%s 去向=%s round=%s" % (
        third.get("rewritten_to"), third.get("去向"), third["round"]))
    print("      driver.log=%r（应为空）   违规笔数=%d（应为 0）" % (log, len(vio_of(d0))))
    print("      父状态: %s round=%s batch=%s" % (
        tr["root"]["state"], tr["root"]["round"], tr["root"]["batch"]))
    check(third.get("rewritten_to") is None, "不该被改写：%s" % third)
    check(tr["root"]["state"] == "等子节点", "父应回到等子节点：%s" % tr["root"]["state"])
    check("强制收束" not in log, "不该有强制收束行：%r" % log)
    check(not vio_of(d0), "不该记违规")
    os.environ.pop("TREE_STUB_MERGE", None)


# ================================================================ 4 连续两轮 spawn
@case("double_spawn_rejected: 连续两轮 spawn 被结构性拒绝（拒 + 一笔违规 + 该轮不算数）")
def t_double_spawn(_):
    d0 = mkd("double.spawn")
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, run_id="double.spawn", run_dir=str(d0),
                      act_seq=["plan", "spawn", "spawn", "merge"])
    d.step()                                   # plan r1
    d.step()                                   # spawn r2 ⇒ 进等子节点
    third = d.step()                           # 非法：再请求 spawn
    vio = vio_of(d0, "驱动_连续spawn")
    print("      第 3 次请求的流水: %s" % json.dumps(third, ensure_ascii=False))
    print("      驱动_连续spawn 违规 %d 笔：%s" % (len(vio), vio[0]["detail"] if vio else ""))
    print("      父 round=%s（被拒的轮不算数 ⇒ 仍是 2）state=%s" % (
        tr["root"]["round"], tr["root"]["state"]))
    check(third.get("rejected"), "第 3 次请求没被拒：%s" % third)
    check(len(vio) == 1, "应有恰 1 笔违规，实际 %d" % len(vio))
    check(tr["root"]["round"] == 2, "被拒的轮不算数 ⇒ round 应仍为 2，实际 %s" % tr["root"]["round"])
    check(len([r for r in rows_of(d0) if r.get("rounds_advanced")]) == 2,
          "流水里只该有 2 个算数的轮")


@case("neg_ctl_legal_sequence: 合法序列 plan→spawn→[等]→merge 不产生该笔违规", negative_control=True)
def t_legal_seq(_):
    d0 = mkd("double.spawn.neg")
    os.environ["TREE_STUB_MERGE"] = "收束"
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, run_id="double.spawn.neg", run_dir=str(d0),
                      act_seq=["plan", "spawn", "merge"])
    d.step()                                   # plan
    d.step()                                   # spawn
    terminate_batch(tr["root"], tr)
    d.step()                                   # merge（等子节点 → 在跑 → merge）
    vio = vio_of(d0, "驱动_连续spawn")
    print("      违规笔数=%d（应为 0）   round=%s state=%s" % (
        len(vio), tr["root"]["round"], tr["root"]["state"]))
    check(not vio, "合法序列不该产生违规：%s" % vio)
    check(tr["root"]["round"] == 3, "合法序列应有 3 轮，实际 %s" % tr["root"]["round"])
    check(tr["root"]["state"] == "完成", "merge 收束 ⇒ 完成，实际 %s" % tr["root"]["state"])
    os.environ.pop("TREE_STUB_MERGE", None)


# ================================================================ 5 切分拉孙
@case("split_pulls_grandchildren: merge.去向=切分 ⇒ 新一批子挂在切分目标下、batch 更新")
def t_split(_):
    d0 = mkd("split")
    os.environ["TREE_STUB_MERGE"] = "切分"
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, run_id="split", run_dir=str(d0))
    d.step()                                   # plan r1
    d.step()                                   # spawn r2 ⇒ root.c2.1
    terminate_batch(tr["root"], tr)
    d.step()                                   # merge r3 切分
    print("      merge 后: 去向=%s batch=%s state=%s" % (
        (tr["root"]["merge"] or {}).get("去向"), tr["root"]["batch"], tr["root"]["state"]))
    d.step()                                   # 下一轮 spawn r4 ⇒ 孙
    gc = sorted(nid for nid in tr if nid.startswith("root.c2.1."))
    for g in gc:
        print("        孙 %s parent=%s batch=%s" % (g, tr[g]["parent"], tr[g]["batch"]))
    print("      父: round=%s batch=%s state=%s" % (
        tr["root"]["round"], tr["root"]["batch"], tr["root"]["state"]))
    check(gc, "没拉出孙节点")
    check(all(tr[g]["parent"] == "root.c2.1" for g in gc), "孙没挂在切分目标 root.c2.1 下")
    check(all(tr[g]["batch"] == 4 for g in gc), "孙的 batch 应是新一批 4")
    check(tr["root"]["batch"] == 4, "父自己在等的批号应是 4")
    check(tr["root"]["state"] == "等子节点", "拉孙后父应回等子节点：%s" % tr["root"]["state"])
    os.environ.pop("TREE_STUB_MERGE", None)


# ================================================================ 6 本批次口径
@case("batch_scope: 第 1 批还剩一个在跑 ⇒ 本批次 merge 照常放行")
def t_batch_ok(_):
    d0 = mkd("batch.ok")
    os.environ["TREE_STUB_MERGE"] = "收束"
    tr = {"root": new_root(round=5, act="spawn", batch=4, state="等子节点")}
    tr["root.c1"] = node_mod.new_node("root.c1", "执行", parent="root", batch=2, state="完成")
    tr["root.c2"] = node_mod.new_node("root.c2", "执行", parent="root", batch=2, state="在跑")
    tr["root.c3"] = node_mod.new_node("root.c3", "执行", parent="root", batch=4, state="完成")
    tr["root.c4"] = node_mod.new_node("root.c4", "执行", parent="root", batch=4, state="失败")
    members = sorted(node_mod.batch_members(tr["root"], tr))
    ok, why = node_mod.ready_to_merge(tr["root"], tr)
    d = driver.Driver(tr["root"], tr, run_id="batch.ok", run_dir=str(d0))
    row = d.step()
    print("      全部子=%s   本批次(batch=4)子=%s" % (
        sorted(node_mod.subtree_ids("root", tr))[1:], members))
    print("      ready_to_merge=%s why=%s" % (ok, why))
    print("      驱动器这一轮: act=%s round=%s rounds_advanced=%s 父终态=%s" % (
        row.get("act"), row.get("round"), row.get("rounds_advanced"), tr["root"]["state"]))
    print("      第 1 批的 root.c2 仍是 %s（未终结，不该阻塞本批次）" % tr["root.c2"]["state"])
    check(members == ["root.c3", "root.c4"], "本批次成员不对：%s" % members)
    check(ok, "第 4 批全终态 ⇒ 应放行：%s" % why)
    check(row.get("rounds_advanced") and row.get("act") == "merge",
          "本批次 merge 没被放行：%s" % row)
    check(tr["root"]["round"] == 6, "父应进到第 6 轮，实际 %s" % tr["root"]["round"])
    os.environ.pop("TREE_STUB_MERGE", None)


@case("neg_ctl_batch_blocked: 本批次还剩一个在跑 ⇒ 不许放行 merge", negative_control=True)
def t_batch_block(_):
    d0 = mkd("batch.block")
    tr = {"root": new_root(round=5, act="spawn", batch=4, state="等子节点")}
    tr["root.c1"] = node_mod.new_node("root.c1", "执行", parent="root", batch=4, state="完成")
    tr["root.c2"] = node_mod.new_node("root.c2", "执行", parent="root", batch=4, state="在跑")
    ok, why = node_mod.ready_to_merge(tr["root"], tr)
    d = driver.Driver(tr["root"], tr, run_id="batch.block", run_dir=str(d0))
    row = d.step()
    print("      ready_to_merge=%s why=%s" % (ok, why))
    print("      驱动器这一轮: skip=%s rounds_advanced=%s 父 round=%s" % (
        row.get("skip"), row.get("rounds_advanced"), tr["root"]["round"]))
    check(not ok, "本批次有未终结子 ⇒ 不许放行")
    check(row.get("skip") == "waiting" and not row.get("rounds_advanced"),
          "merge 不该被放行：%s" % row)
    check(tr["root"]["round"] == 5, "round 不该增长，实际 %s" % tr["root"]["round"])


# ================================================================ 7 三条 assert
@case("assert_terminal_closed: 造第三种终态 ⇒ 拒 + 一笔；终态封闭 ⇒ 拒")
def t_assert1(_):
    d0 = mkd("assert1")
    sink = str(d0 / "violations.jsonl")
    n = new_root(state="在跑")
    ok1, why1 = node_mod.change_state(n, "半死不活", sink)
    n3 = new_root(state="完成")
    ok3, why3 = node_mod.change_state(n3, "在跑", sink)
    vio = vio_of(d0, "状态机_终态")
    print("      第三种终态 ⇒ ok=%s why=%s" % (ok1, why1))
    print("      终态封闭 完成→在跑 ⇒ ok=%s why=%s" % (ok3, why3))
    print("      状态机_终态 违规笔数=%d（第三种终态 1 + 终态封闭 1）" % len(vio))
    check(ok1 is False and n["state"] == "在跑", "第三种终态应被拒且状态不变")
    check(ok3 is False, "终态封闭：完成不该能回在跑")
    check(len(vio) == 2, "应恰 2 笔违规，实际 %d" % len(vio))


@case("neg_ctl_legal_terminal: 在跑→完成/失败 都是合法终态迁移，放行且不记违规", negative_control=True)
def t_assert1_neg(_):
    d0 = mkd("assert1.neg")
    sink = str(d0 / "violations.jsonl")
    a, b = new_root(state="在跑"), new_root(state="在跑")
    ok_a, _w = node_mod.change_state(a, "完成", sink)
    ok_b, _w2 = node_mod.change_state(b, "失败", sink)
    print("      在跑→完成 ok=%s；在跑→失败 ok=%s；违规笔数=%d" % (
        ok_a, ok_b, len(vio_of(d0))))
    check(ok_a is True and ok_b is True and not vio_of(d0), "合法迁移不该被拒/记违规")


@case("assert_freeze: 进「等子节点」⇒ 该层落盘被拒；收拢完成解冻后放行")
def t_assert2(_):
    d0 = mkd("assert2")
    tr = {"root": new_root(state="等子节点"),
          "leaf": node_mod.new_node("leaf", "执行", parent="root", state="在跑", batch=4)}
    d = driver.Driver(tr["root"], tr, run_id="assert2", run_dir=str(d0))
    ok_frozen, why_frozen = d.write_doc("root", "冻结期想落盘")
    ok_leaf, path_leaf = d.write_doc("leaf", "第 1 层没冻结，可以落盘")
    vio = vio_of(d0, "状态机_冻结")
    print("      冻结层（第 0 层）落盘 ⇒ ok=%s why=%s" % (ok_frozen, why_frozen))
    print("      未冻结层（第 1 层）落盘 ⇒ ok=%s path=%s" % (ok_leaf, path_leaf))
    print("      冻结层=%s   状态机_冻结 违规笔数=%d" % (node_mod.frozen_layers(tr), len(vio)))
    tr["root"]["state"] = "完成"                       # 收拢完成 ⇒ 解冻
    ok_after, path_after = d.write_doc("root", "解冻后落盘")
    print("      收拢完成后（解冻）落盘 ⇒ ok=%s path=%s" % (ok_after, path_after))
    check(ok_frozen is False and len(vio) == 1, "冻结期落盘应被拒并记一笔")
    check(ok_leaf is True and path_leaf.endswith("leaf.md"), "未冻结层应放行")
    check(ok_after is True, "解冻后应放行")


@case("assert_round: round 回退/跳号/超限 ⇒ 各拒一笔；恰 max_rounds+1 且 forced ⇒ 放行")
def t_assert3(_):
    d0 = mkd("assert3")
    sink = str(d0 / "violations.jsonl")
    n = new_root(round=3, state="在跑")
    ok_back, why_back = node_mod.check_round_step(n, 2, {"max_rounds": 8}, sink)
    ok_skip, why_skip = node_mod.check_round_step(n, 5, {"max_rounds": 8}, sink)
    ok_over, why_over = node_mod.check_round_step(n, 4, {"max_rounds": 3}, sink)
    ok_forced, _w = node_mod.check_round_step(n, 4, {"max_rounds": 3}, sink, forced=True)
    ok_extra, _w2 = node_mod.check_round_step(n, 5, {"max_rounds": 3}, sink, forced=True)
    okl, r_new, _w3 = node_mod.advance_round(n, {"max_rounds": 8}, sink)
    vio = vio_of(d0, "状态机_round")
    print("      回退 3→2 ⇒ ok=%s why=%s" % (ok_back, why_back))
    print("      跳号 3→5 ⇒ ok=%s why=%s" % (ok_skip, why_skip))
    print("      超限 3→4(max_rounds=3) ⇒ ok=%s why=%s" % (ok_over, why_over))
    print("      恰 max_rounds+1=4 且 forced ⇒ ok=%s；再往后 5 且 forced ⇒ ok=%s" % (ok_forced, ok_extra))
    print("      合法 +1 3→4 ⇒ ok=%s round=%s" % (okl, r_new))
    print("      状态机_round 违规笔数=%d（回退/跳号/超限/越限一轮各 1）" % len(vio))
    check(ok_back is False and ok_skip is False and ok_over is False, "三种违规都应被拒")
    check(ok_forced is True, "§2.1.1(B)：恰 max_rounds+1 且 forced ⇒ 允许")
    check(ok_extra is False, "超过 max_rounds+1 仍应拒")
    check(okl is True and n["round"] == 4, "合法 +1 应放行")
    check(len(vio) == 4, "应恰 4 笔违规，实际 %d" % len(vio))


@case("neg_ctl_round_at_limit: round 恰好到 max_rounds 的正常推进 ⇒ 放行且不记违规",
      negative_control=True)
def t_assert3_neg(_):
    d0 = mkd("assert3.neg")
    sink = str(d0 / "violations.jsonl")
    n = new_root(round=2, state="在跑")
    ok, r_new, _w = node_mod.advance_round(n, {"max_rounds": 3}, sink)
    print("      2→3（max_rounds=3）⇒ ok=%s round=%s 违规笔数=%d" % (
        ok, r_new, len(vio_of(d0))))
    check(ok is True and r_new == 3 and not vio_of(d0), "恰在 max_rounds 的合法路径不该被拒")


# ================================================================ 8 schema 硬闸三
@case("schema_hard_gate: 收束缺 uncovered ⇒ 该轮拒 + 记一笔 + 不算数")
def t_schema(_):
    d0 = mkd("schema")
    os.environ["TREE_STUB_CHOICE"] = "STALE"
    tr = {"root": new_root(round=2, act="spawn", batch=2, state="等子节点"),
          "root.c1": node_mod.new_node("root.c1", "执行", parent="root", batch=2, state="完成")}
    d = driver.Driver(tr["root"], tr, run_id="schema", run_dir=str(d0))
    row = d.step()
    vio = vio_of(d0, "驱动_产出形状")
    print("      流水: schema_ok=%s why=%s rounds_advanced=%s 父 round=%s" % (
        row.get("schema_ok"), row.get("why"), row.get("rounds_advanced"), tr["root"]["round"]))
    print("      驱动_产出形状 违规 %d 笔：%s" % (len(vio), vio[0]["detail"] if vio else ""))
    check(row.get("schema_ok") is False, "缺 uncovered 的收束应被 schema 拒")
    check(row.get("rounds_advanced") is False and tr["root"]["round"] == 2,
          "该轮不算数 ⇒ round 不增长，实际 %s" % tr["root"]["round"])
    check(len(vio) == 1, "应恰 1 笔违规，实际 %d" % len(vio))
    os.environ.pop("TREE_STUB_CHOICE", None)


@case("neg_ctl_schema_ok: 同一形状带 uncovered ⇒ 过 schema、该轮算数、父收束", negative_control=True)
def t_schema_neg(_):
    d0 = mkd("schema.neg")
    os.environ["TREE_STUB_MERGE"] = "收束"
    tr = {"root": new_root(round=2, act="spawn", batch=2, state="等子节点"),
          "root.c1": node_mod.new_node("root.c1", "执行", parent="root", batch=2, state="完成")}
    d = driver.Driver(tr["root"], tr, run_id="schema.neg", run_dir=str(d0))
    row = d.step()
    print("      流水: act=%s schema_ok=%s round=%s state=%s 违规笔数=%d" % (
        row.get("act"), row.get("schema_ok"), tr["root"]["round"], tr["root"]["state"],
        len(vio_of(d0))))
    check(row.get("schema_ok") is True and tr["root"]["round"] == 3, "带 uncovered 应过")
    check(tr["root"]["state"] == "完成", "收束 ⇒ 父终态完成，实际 %s" % tr["root"]["state"])
    check(not vio_of(d0), "不该记违规")
    os.environ.pop("TREE_STUB_MERGE", None)


@case("raw_output_rejected: 桩回非 JSON ⇒ 响亮失败（不静默猜），该轮不算数")
def t_raw(_):
    d0 = mkd("raw")
    os.environ["TREE_STUB_CHOICE"] = "RAW"
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, run_id="raw", run_dir=str(d0))
    row = d.step()
    print("      流水: schema_ok=%s why=%s" % (row.get("schema_ok"), row.get("why")))
    print("      父: round=%s state=%s" % (tr["root"]["round"], tr["root"]["state"]))
    check(row.get("schema_ok") is False and "找不到一个 JSON" in str(row.get("why")),
          "非 JSON 应响亮失败：%s" % row.get("why"))
    check(row.get("rounds_advanced") is False, "该轮不该算数：%s" % row)
    check(row.get("rejected"), "被拒的轮应带 rejected 原文：%s" % row)
    os.environ.pop("TREE_STUB_CHOICE", None)


# ================================================================ 9 L0 只注入一次
@case("l0_once: 有会话文件 ⇒ 后续轮不重复注入 L0；无会话文件（重启/新会话）⇒ 必须注入")
def t_l0(_):
    """★ 口径（实测得出）：「首轮」的判据是【该节点的会话文件在不在】，不是 round==1。

    理由：重启/中途起的新会话里没有 L0 也没有历史 ⇒ 若只按 round 判，模型拿不到契约
    （实测：真跑一轮 merge 因此输出非契约 JSON，见报告「待裁决」节）。
    """
    l0 = (ROOT / "prompts" / "根.md").read_text(encoding="utf-8")
    marker = [x.strip() for x in l0.splitlines() if x.strip()][0]
    sid = session.session_id("l0", "root")
    sf = Path(session.session_dir()) / ("feed_%s.jsonl" % sid)

    def rec_runner(node, act, knobs, prompt, sid, run_dir):   # noqa: ANN001
        box["rows"].append({"act": act, "round": node["round"], "prompt": prompt})
        if act == "plan":
            return json.dumps({"要点": ["两块"]}, ensure_ascii=False)
        if act == "spawn":
            return json.dumps({"children": [CHILD]}, ensure_ascii=False)
        return json.dumps({"去向": "收束", "reply": "r", "未决": [],
                           "uncovered": [{"what": "w", "why": "y"}]}, ensure_ascii=False)

    box = {"rows": []}
    if sf.exists():
        sf.unlink()                                     # 只删自己造的会话文件
    # ---- A：没有会话文件（= 首轮 / 重启后新会话）⇒ 每轮都要注入 L0
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, run_id="l0", run_dir=str(mkd("l0")), act_runner=rec_runner)
    d.step()
    d.step()
    print("      L0 标记（prompts/根.md 首行）= %r" % marker)
    for r in box["rows"]:
        print("      [无会话文件] round=%s act=%s 注入 L0=%s prompt_chars=%s" % (
            r["round"], r["act"], marker in r["prompt"], len(r["prompt"])))
    check(len(box["rows"]) == 2, "应跑到两轮：%d" % len(box["rows"]))
    check(all(marker in r["prompt"] for r in box["rows"]),
          "会话文件不存在时必须每轮注入 L0（否则模型拿不到契约）")
    check(box["rows"][0]["prompt"].startswith("# role: 根"), "首轮应是 L0 全文开头")

    # ---- B：会话文件存在（= 历史在会话里）⇒ 后续轮只给增量，不重复注入 L0
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text('{"type":"session","version":3,"id":"%s"}\n' % sid, encoding="utf-8")
    box["rows"] = []
    tr2 = {"root": new_root()}
    d2 = driver.Driver(tr2["root"], tr2, run_id="l0", run_dir=str(mkd("l0.hist")),
                       act_runner=rec_runner)
    d2.step()
    d2.step()
    for r in box["rows"]:
        print("      [有会话文件] round=%s act=%s 注入 L0=%s prompt_chars=%s" % (
            r["round"], r["act"], marker in r["prompt"], len(r["prompt"])))
    check(box["rows"][0]["prompt"].startswith("# role: 根"), "首轮（round=1）仍应给 L0")
    check(marker not in box["rows"][1]["prompt"], "有历史时后续轮不该重复注入 L0（格式压缩）")
    check(box["rows"][1]["prompt"].startswith("## 本轮输入"), "后续轮应是增量格式")
    check("你的会话里" in box["rows"][1]["prompt"], "后续轮应说明 L0 在会话里")
    sf.unlink()
    check(session.session_id("r", "root") == "tree-r-root", "session_id 形如 tree-<req_id>-<node_id>")


# ================================================================ 10 无字面判断
@case("no_literals_in_driver: 驱动器里没有 role 名字 / thinking 值的字面判断")
def t_no_literals(_):
    """★ 只扫【代码常量】—— 注释里的中文说明不算（那正是「为什么这么写」的落点）。

    做法：把源码 parse 成 AST，取所有字符串常量与属性/变量名里的标识符，再找 role 名字。
    """
    import ast
    src = (ROOT / "harness" / "driver.py").read_text(encoding="utf-8")
    lits = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lits.append(node.value)
    names = [n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)]
    pool = lits + names
    roles_ = ("根", "规划", "决策", "执行", "调研", "审计")
    thinking_ = ("none", "low", "medium")
    hits = [w for w in roles_ + thinking_ if any(w == s for s in pool)]
    # 另查：thinking 值不许作为【比较对象】出现（模型参数一律从 knobs 取）
    base = role.effective("根", "merge")
    t2 = table_with("根")
    t2["acts"]["根"]["merge"]["thinking"] = "none"
    changed = role.effective("根", "merge", table=t2)
    a1 = session.build_argv(new_root(), "merge", base, "x", "sid")[6]
    a2 = session.build_argv(new_root(), "merge", changed, "x", "sid")[6]
    print("      driver.py 字符串常量 %d 个 / 标识符 %d 个" % (len(lits), len(names)))
    print("      命中 role 名或 thinking 值（必须为空）：%s" % hits)
    print("      原表 merge.thinking=%s ⇒ --thinking %s" % (base["thinking"], a1))
    print("      改过的副本 merge.thinking=%s ⇒ --thinking %s" % (changed["thinking"], a2))
    check(not hits, "driver.py 的代码常量里出现了 role 名字/thinking 值：%s" % hits)
    check((a1, a2) == ("low", "off"), "thinking 没跟着数据走：%s / %s" % (a1, a2))


# ================================================================ 11 零 pi 进程
PI_SAMPLES = {"n": 0, "hits": [], "peak": 0}


def _pi_sampler(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            p = subprocess.run(["pgrep", "-x", "pi"], capture_output=True, text=True, timeout=5)
            PI_SAMPLES["n"] += 1
            lines = [x for x in p.stdout.strip().splitlines() if x]
            PI_SAMPLES["peak"] = max(PI_SAMPLES["peak"], len(lines))
            if lines:
                PI_SAMPLES["hits"].append(lines)
        except Exception:                                     # noqa: BLE001
            pass
        time.sleep(0.05)


@case("zero_pi_processes: 桩路径下 pgrep -x pi 采样 0 命中（含 2 秒压力窗口）")
def t_zero_pi(_):
    d0 = mkd("zero.pi")
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, run_id="zero.pi", run_dir=str(d0))
    n_before = PI_SAMPLES["n"]
    drive_cycle(d, tr, "再派", tag="压力段第 1 ")
    drive_cycle(d, tr, "再派", tag="压力段第 2 ")
    t0 = time.time()
    while time.time() - t0 < 2.0:                             # 压力窗口：桩路径下持续采样
        time.sleep(0.05)
    raw_now = subprocess.run(["pgrep", "-x", "pi"], capture_output=True, text=True).stdout.strip()
    ps_line = subprocess.run(["bash", "-c",
                              "ps -eo pid,etime,comm,args | awk '$3==\"pi\"' | head -3"],
                             capture_output=True, text=True).stdout.strip()
    print("      采样 %d 次（本用例压力段 %d 次），命中 %d 次（应为 0）" % (
        PI_SAMPLES["n"], PI_SAMPLES["n"] - n_before, len(PI_SAMPLES["hits"])))
    print("      收尾直查 `pgrep -x pi` 输出=%r（应为空）" % raw_now)
    print("      `ps -eo pid,etime,comm,args | awk '$3==\"pi\"'`=%r（应为空）" % ps_line)
    check(PI_SAMPLES["n"] - n_before > 20, "压力段采样太少：%d" % (PI_SAMPLES["n"] - n_before))
    check(not PI_SAMPLES["hits"], "桩路径起了 pi：%s" % PI_SAMPLES["hits"])
    check(raw_now == "" and ps_line == "", "桩路径出现了 pi 进程")
    os.environ.pop("TREE_STUB_MERGE", None)


# ================================================================ 跑
def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    os.environ["TREE_ACT_CMD"] = STUB                  # 桩路径：一个 pi 都不许起
    os.environ["TREE_RUNS_DIR"] = str(TMP / "runs")
    stop = threading.Event()
    th = threading.Thread(target=_pi_sampler, args=(stop,), daemon=True)
    th.start()
    passed, failed = 0, []
    import yaml
    print("tree-03 multiround · root=%s · python=%s · yaml=%s" % (
        ROOT, sys.version.split()[0], yaml.__version__))
    print("桩 act 脚本 = %s（$TREE_ACT_CMD；桩路径一个 pi 进程都不起）" % STUB)
    for name, fn in CASES:
        try:
            fn(None)
            print("CASE %-34s PASS" % name.split(":")[0])
            passed += 1
        except Exception as exc:                              # noqa: BLE001
            failed.append(name)
            print("CASE %-34s FAIL: %s" % (name.split(":")[0], exc))
            if os.environ.get("TREE_GATES_TRACE"):
                traceback.print_exc()
    stop.set()
    th.join(timeout=3)
    total = len(CASES)
    print("-" * 78)
    if failed:
        print("FAILED: %s" % ", ".join(failed))
    print("阴性对照 %d 条：%s" % (len(NC), ", ".join(n.split(":")[0] for n in NC)))
    print("pi 采样合计 %d 次，命中 %d 次，峰值 %d" % (
        PI_SAMPLES["n"], len(PI_SAMPLES["hits"]), PI_SAMPLES["peak"]))
    if total < 19 or len(NC) < 3:
        print("GATES: %d/%d FAIL（total<19 或 阴性对照<3 视为不合格）" % (passed, total))
        return 1
    if failed:
        print("GATES: %d/%d FAIL" % (passed, total))
        return 1
    print("GATES: %d/%d PASS" % (passed, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
