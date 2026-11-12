#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree-04 审计枝 + 全覆盖分批 + 终审触发 + 形状审查 D10 —— 全部用例。

唯一验收入口由 tests/run-audit.sh 调起。
★ 桩驱动：**不打模型、不占单卡**（$TREE_ACT_CMD 指到 tests/stub-audit.py，一个 pi 进程都不许起）。
★ 每个「正例」都配**阴性对照**（同一条判据必须有区分力），末行打印对照条数。
★ 任一用例 FAIL ⇒ rc ≠ 0；total < 18 或 阴性对照 < 5 ⇒ 不合格。
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

from harness import (audit, driver, final_audit, gate, node as node_mod,  # noqa: E402
                 role, schema, shape_review)

TMP = HERE / "tmp" / "tree04"
STUB = str(HERE / "stub-audit.py")
CASES, NC = [], []
ROLE_AUDIT = "审计"
CHILD = {"role": "执行", "context": "第 1 块的落点", "judge": {"chars_min": 120},
         "验收标准": "第 1 块写完且非空", "auth": "doc/s1.md"}
SNAP_KEYS = ["节点总数", "最大深度", "每层宽度", "节点"]


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
    d = TMP / "runs" / name
    if d.exists():
        shutil.rmtree(d)
    (d / "doc").mkdir(parents=True, exist_ok=True)
    return d


def sane_env(d0: Path, mode: str = "正常", children=3, merge: str = "收束") -> Path:
    """一条用例一套环境（互不干扰）；trace 落在本用例自己的 run 目录里。"""
    os.environ["TREE_AUDIT_MODE"] = mode
    os.environ["TREE_AUDIT_CHILDREN"] = str(children)
    os.environ["TREE_AUDIT_MERGE"] = merge
    tp = d0 / "trace.jsonl"
    if tp.exists():
        tp.unlink()
    os.environ["TREE_AUDIT_TRACE"] = str(tp)
    return tp


def trace_of(p: Path) -> list:
    if not Path(p).exists():
        return []
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]


def trace_kind(p: Path, kind: str) -> list:
    return [r for r in trace_of(p) if r.get("kind") == kind]


def rows_of(d0: Path) -> list:
    p = Path(d0) / "rounds.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if p.exists() else []


def log_of(d0: Path) -> str:
    p = Path(d0) / "driver.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def vio_of(d0: Path, kind: str = None) -> list:
    rows = gate.read_violations(str(Path(d0) / "violations.jsonl"))
    return [r for r in rows if kind is None or r["kind"] == kind]


def table_with(role_name: str, **knob_over):
    t = copy.deepcopy(role.load_roles())
    t["roles"][role_name].update(knob_over)
    return t


def table_driver(**over):
    """role.yaml 副本，改 driver 段（证明 width_max/depth_max/nodes_max 跟着数据走）。"""
    t = copy.deepcopy(role.load_roles())
    t["driver"].update(over)
    return t


def new_root(**kw) -> dict:
    n = node_mod.new_node("root", "根")
    n.update(kw)
    return n


def add_child(tr: dict, cid: str, parent: str = "root", batch: int = 1, state: str = "完成",
              reply: str = "", role_name: str = "执行", result: str = "") -> dict:
    n = node_mod.new_node(cid, role_name, parent=parent, batch=batch, state=state,
                          reply=reply, result=result)
    tr[cid] = n
    return n


def terminate_batch(parent: dict, tree: dict) -> list:
    ids = node_mod.batch_members(parent, tree)
    for cid in ids:
        tree[cid]["state"] = "完成"
    return ids


def audit_nodes(tr: dict, parent: str = "root", slot: str = None) -> list:
    """父名下由代码挂的检查点（缺省全槽位；slot="a" 只数抽检、"s" 只数形状审查、"f" 只数终审）。"""
    out = []
    for nid, n in tr.items():
        if n.get("role") != ROLE_AUDIT or n.get("parent") != parent:
            continue
        spec = audit.slot_of(parent, nid)
        if spec and (slot is None or spec[0] == slot):
            out.append(nid)
    return sorted(out)


def run(d0: Path, name: str, tr: dict, table=None, act_seq=None):
    return driver.Driver(tr["root"], tr, table=table, run_id=name, run_dir=str(d0),
                         act_seq=act_seq)


def spawn_then_merge(d0: Path, name: str, tr: dict, table=None, merge_go: str = "收束",
                     children=3, mode: str = "正常"):
    """跑 plan → spawn → [子终结] → merge，返回 (driver, 三次 step 的流水)。"""
    tp = sane_env(d0, mode=mode, children=children, merge=merge_go)
    d = run(d0, name, tr, table=table)
    r1 = d.step()
    r2 = d.step()
    terminate_batch(tr["root"], tr)
    r3 = d.step()
    return d, [r1, r2, r3], tp


# ================================================================ 场景 1
@case("audit_trigger_3: 本批 3 个子 ⇒ 恰 1 次抽检（节点 1 个 + audit.jsonl 一行已审 + trace 1 次）")
def t_trigger_3(_):
    d0 = mkd("audit.trigger3")
    tr = {"root": new_root()}
    d, rows, tp = spawn_then_merge(d0, "audit.trigger3", tr)
    led = audit.read_ledger(d)
    an = audit_nodes(tr, slot="a")
    cps = trace_kind(tp, "抽检")
    print("      audit.jsonl 行数=%d 内容=%s" % (
        len(led), json.dumps(led[0], ensure_ascii=False) if led else ""))
    print("      树里的审计节点=%s（role/parent/batch=%s/%s/%s）" % (
        an, tr[an[0]]["role"] if an else "-", tr[an[0]]["parent"] if an else "-",
        tr[an[0]]["batch"] if an else "-"))
    print("      桩 trace 里抽检调用 %d 次；父终态=%s round=%s" % (
        len(cps), tr["root"]["state"], tr["root"]["round"]))
    check(len(an) == 1, "审计节点应恰 1 个，实际 %d" % len(an))
    check(len(led) == 1 and led[0]["状态"] == "已审", "audit.jsonl 应恰一行 已审：%s" % led)
    check(len(cps) == 1, "抽检调用应恰 1 次，实际 %d" % len(cps))
    check(an[0] == "root.a2.1", "审计节点 id 形态不对：%s" % an)
    check(tr[an[0]]["batch"] == 0 and tr[an[0]]["act"] == "leaf", "审计节点属性不对")
    check(led[0]["批号"] == "root#2" and led[0]["承载轮"] == 3,
          "批号/承载轮不对：%s" % led[0])
    check(led[0]["发现数"] == 1 and led[0]["blocker数"] == 0, "发现计数不对：%s" % led[0])


# ================================================================ 场景 2（阴性对照）
@case("neg_ctl_audit_skip_2: 本批 2 个子 ⇒ 不触发（0 个审计节点 + 一行未审 + trace 0 次）",
      negative_control=True)
def t_skip_2(_):
    d0 = mkd("audit.skip2")
    tr = {"root": new_root()}
    d, _rows, tp = spawn_then_merge(d0, "audit.skip2", tr, children=2)
    led = audit.read_ledger(d)
    an = audit_nodes(tr, slot="a")
    print("      审计节点数=%d（应为 0）  trace 抽检调用=%d（应为 0）" % (
        len(an), len(trace_kind(tp, "抽检"))))
    print("      audit.jsonl=%s" % json.dumps(led[0], ensure_ascii=False) if led else "（空）")
    check(not an, "2 个子不该触发抽检：%s" % an)
    check(len(led) == 1 and led[0]["状态"] == "未审"
          and led[0]["原因"] == "批次子数不足阈值（2<3）", "未审行为不对：%s" % led)
    check(not trace_kind(tp, "抽检"), "不该有抽检调用")

@case("audit_count_excludes_audit_role: 本批 3 个子含 1 个审计 role ⇒ 只数 2 个（未审 2<3）")
def t_count_excl_audit(_):
    # 第三轮 P0③ 根因钉死（integ2-agg2 实测）：batch2 有 c2.1/c2.2/c2.3 三子，
    # 但 c2.3 的 role=审计 ⇒ batch_siblings 只数 2 ⇒ 未审行 2<3。
    # 终审据此问"未审批次实际承载的子节点身份"时，机械答案就是这 3 个 id
    # （verify_structural_blocker 在 final_audit 里核）。
    d0 = mkd("audit.count.excl")
    tr = {"root": new_root(batch=2, round=2, act="spawn", state="在跑")}
    add_child(tr, "root.c2.1", batch=2, state="完成", reply="第 1 块")
    add_child(tr, "root.c2.2", batch=2, state="完成", reply="第 2 块")
    add_child(tr, "root.c2.3", batch=2, state="完成", reply="第 3 块（审计叶）",
              role_name="审计")
    d = run(d0, "audit.count.excl", tr)
    audit.run_sample(d, tr["root"])
    led = audit.read_ledger(d)
    check(len(led) == 1 and led[0]["状态"] == "未审"
          and led[0]["原因"] == "批次子数不足阈值（2<3）", "审计 role 不应计入抽检基数：%s" % led)
    check(led[0]["批号"] == "root#2" and led[0]["承载轮"] == 3, "批号/承载轮不对：%s" % led)
    sibs = audit.batch_siblings(tr["root"], tr)
    check(sorted(sibs) == ["root.c2.1", "root.c2.2"], "批次兄弟应恰为两执行叶：%s" % sibs)



# ================================================================ 场景 3 / 3b
@case("final_audit_trigger: 合并去向=收束 ⇒ 该轮跑一次全量终审（在 blocks 内闭环，无作废）")
def t_final_trigger(_):
    d0 = mkd("final.trigger")
    tr = {"root": new_root()}
    d, rows, tp = spawn_then_merge(d0, "final.trigger", tr)
    led = final_audit.read_ledger(d)
    fins = trace_kind(tp, "终审")
    print("      final_audit.jsonl=%s" % json.dumps(led, ensure_ascii=False))
    print("      桩 trace 终审调用 %d 次；收束轮=%s" % (len(fins), rows[2].get("round")))
    print("      父: state=%s round=%s 违规笔数=%d" % (
        tr["root"]["state"], tr["root"]["round"], len(vio_of(d0))))
    check(len(led) == 1, "final_audit.jsonl 应恰一行，实际 %d" % len(led))
    check(led[0]["轮号"] == rows[2]["round"] == 3, "终审轮号应 == 收束轮：%s" % led[0])
    check(led[0]["作废"] is False and led[0]["blocker数"] == 0, "不该作废/无 blocker：%s" % led[0])
    check(len(fins) == 1, "终审调用应恰 1 次，实际 %d" % len(fins))
    check(tr["root"]["state"] == "完成", "收束成立 ⇒ 父完成：%s" % tr["root"]["state"])
    check("收束作废" not in log_of(d0), "不该有 收束作废 行")
    check(rows[2]["rounds_advanced"] and rows[2]["去向"] == "收束", "该轮应算数且去向=收束")


# ================================================================ 场景 4（前半）
@case("final_audit_void_once: 终审出 blocker ⇒ 收束作废 + round 回退 + 下一轮 act==merge")
def t_void_once(_):
    d0 = mkd("final.void")
    # 第三轮：BLOCKER 桩改为 doc 长度形态，fixture 须有该文件（207 字 < 1500），
    # 否则 blocker 引用无法核查会被 verify_blockers 降级、merge 直接放行。
    # 注意：缺文件时套件结果还会随 tests/tmp 残留的同名文件数翻转（后缀抢救
    # 恰 1 个副本时命中、多副本时含糊拒绝），自带文件后走 run_dir 直解、确定性。
    (d0 / "blocks").mkdir(parents=True, exist_ok=True)
    (d0 / "blocks" / "sum_calculation_doc.md").write_text("x" * 207, encoding="utf-8")
    tr = {"root": new_root()}
    d, _rows, tp = spawn_then_merge(d0, "final.void", tr, mode="BLOCKER", merge_go="收束")
    rows = rows_of(d0)
    led = final_audit.read_ledger(d)
    r3 = [r for r in rows if r.get("round") == 3]
    nxt = driver.next_act(tr["root"])
    print("      final_audit.jsonl=%s" % json.dumps(led, ensure_ascii=False))
    print("      rounds.jsonl 里 round=3 的行数=%d 该行 rounds_advanced=%s" % (
        len(r3), r3[0].get("rounds_advanced") if r3 else "-"))
    print("      driver.log=%r" % log_of(d0).strip())
    print("      作废后：父 state=%s round=%s act=%s merge=%r ⇒ next_act=%s" % (
        tr["root"]["state"], tr["root"]["round"], tr["root"]["act"], tr["root"]["merge"], nxt))
    check(tr["root"]["state"] != "完成", "作废 ⇒ 父不许进终态：%s" % tr["root"]["state"])
    check(len(r3) == 1, "该轮在 rounds.jsonl 里应恰一行，实际 %d" % len(r3))
    check(r3[0]["rounds_advanced"] is False, "该行 rounds_advanced 应为 false：%s" % r3[0])
    check("收束作废" in log_of(d0) and "作废次数=1/2" in log_of(d0),
          "driver.log 缺可 grep 的 收束作废 行：%r" % log_of(d0))
    check(led and led[0]["作废"] is True and led[0]["blocker数"] == 1, "终审应记 作废=true：%s" % led)
    check(tr["root"]["round"] == 2, "round 应回退 1 ⇒ 2，实际 %s" % tr["root"]["round"])
    check(tr["root"]["act"] == "spawn", "p['act'] 必须显式改回 spawn：%r" % tr["root"]["act"])
    check(nxt == "merge", "★ 作废后下一轮的 act 必须是 merge（不是 spawn）：%r" % nxt)


# ================================================================ 场景 4（后半，修复派发/明确失败，第二轮 B）
# 旧语义（第 3 次作废被兜底通过）已废：作废达上限后不再爬升进终态，改为自动修复派发
# （最多 2 轮），仍未消除 ⇒ 父明确失败。
# 续修（integ2-q01c 实测 off-by-one）：cap 在第 2 次作废评估即触发（voids+1>=2），
# 不再等第 3 次评估——第 3 次通常到不了（驱动连续拒收上限 2 先杀父）。
@case("final_audit_retry_capped: 第 2 次作废即转修复派发 ⇒ 修复派发 ×2 后明确失败")
def t_retry_capped(_):
    d0 = mkd("final.capped")
    # 第三轮：BLOCKER 桩改为 doc 长度形态，fixture 须有该文件（207 字 < 1500），
    # 修复派发才能走到可执行规格（P1⑤）；无文件时目标解不出，仍走明确失败。
    (d0 / "blocks").mkdir(parents=True, exist_ok=True)
    (d0 / "blocks" / "sum_calculation_doc.md").write_text("x" * 207, encoding="utf-8")
    tr = {"root": new_root()}
    d = run(d0, "final.capped", tr)
    sane_env(d0, mode="BLOCKER", children=3, merge="收束")
    acts = []
    row = d.step()                                        # plan round 1
    acts.append((row.get("act"), row.get("round"), bool(row.get("rejected"))))
    row = d.step()                                        # spawn round 2
    acts.append((row.get("act"), row.get("round"), bool(row.get("rejected"))))
    terminate_batch(tr["root"], tr)
    row = d.step()                                        # merge#1 round 3 ⇒ 作废 1
    acts.append((row.get("act"), row.get("round"), bool(row.get("rejected"))))
    row = d.step()                                        # merge#2 round 3 ⇒ 达上限 ⇒ 修复派发 1
    acts.append((row.get("act"), row.get("round"), bool(row.get("rejected"))))
    print("      每步 (act, round, 被拒)=%s" % acts)
    print("      merge#2 行 why=%r" % row.get("why"))
    fix1 = [nid for nid in tr if ".fix.audit1." in nid]
    check(isinstance(row.get("why"), str) and row["why"].startswith("【修复派发】")
          and row.get("rounds_advanced") is True and not row.get("rejected"),
          "达上限: merge#2 应返回【修复派发】哨兵（不回退 round）：%s" % row.get("why"))
    check(len(fix1) == 1, "达上限: 建出修复叶：%s" % fix1)
    check(tr["root"]["state"] == "等子节点" and not node_mod.is_terminal(tr["root"]),
          "达上限: 父等子节点（等修复叶），未进终态：%s" % tr["root"]["state"])
    check(tr["root"]["round"] == 3,
          "达上限: 父 round 不回退（仍为 3）：%s" % tr["root"]["round"])
    terminate_batch(tr["root"], tr)                       # 修复叶终结
    row = d.step()                                        # merge#3 round 4 ⇒ 修复派发 2
    fix2 = [nid for nid in tr if ".fix.audit2." in nid]
    check(isinstance(row.get("why"), str) and row["why"].startswith("【修复派发】")
          and len(fix2) == 1,
          "修复 2: 再派发一轮修复叶：%s / %s" % (row.get("why"), fix2))
    terminate_batch(tr["root"], tr)
    row = d.step()                                        # merge#4 round 5 ⇒ 预算用完 ⇒ 明确失败
    check(isinstance(row.get("why"), str) and row["why"].startswith("【明确失败】"),
          "预算用完: 应返回【明确失败】哨兵：%s" % row.get("why"))
    check(tr["root"]["state"] == "失败",
          "预算用完: 父进失败终态（不冒充完成）：%s" % tr["root"]["state"])
    led = final_audit.read_ledger(d)
    print("      final_audit.jsonl：")
    for r in led:
        print("        %s" % json.dumps(r, ensure_ascii=False))
    print("      收束作废 次数=%d（cap 在第 2 次评估即触发，故恰 1 = MAX-1）" % len(
        [r for r in led if r["作废"]]))
    print("      修复派发笔数=%d 明确失败笔数=%d" % (
        len(vio_of(d0, "终审_修复派发")), len(vio_of(d0, "终审_blocker_明确失败"))))
    print("      父: state=%s 未决=%s" % (tr["root"]["state"],
                                        json.dumps(tr["root"]["未决"], ensure_ascii=False)))
    check(len([r for r in led if r["作废"]]) == final_audit.FINAL_AUDIT_MAX_RETRY - 1,
          "作废次数应恰 %d（cap 在第 2 次评估触发，只记 1 次 plain 作废）：%s"
          % (final_audit.FINAL_AUDIT_MAX_RETRY - 1, led))
    check(not any(r.get("兜底") in ("次数上限", "到数") for r in led),
          "旧兜底语义必须消失（不得再有 兜底=次数上限/到数）：%s" % led)
    check(any(r.get("兜底") == "明确失败" for r in led),
          "明确失败应记终审账（兜底=明确失败）：%s" % led)
    check(len(vio_of(d0, "终审_修复派发")) == 2, "修复派发应恰 2 笔")
    check(len(vio_of(d0, "终审_blocker_明确失败")) == 1, "明确失败应恰 1 笔")
    check(len(vio_of(d0, "终审_blocker_到数")) == 0, "旧 终审_blocker_到数 必须为 0")
    up = [u for u in tr["root"]["未决"] if str(u.get("what") or "").startswith("BLOCKER")]
    check(up and up[0]["type"] == "超范围", "blocker 应留在父未决（type=超范围）：%s"
          % tr["root"]["未决"])


# ================================================================ 场景 4（到数明确失败，§3.8，第二轮 B）
# 旧语义（到数兜底通过）已废：到数轮出 blocker ⇒ 父明确失败，不冒充完成。
@case("final_audit_capped_at_limit: 到数轮（round > max_rounds）出 blocker ⇒ 明确失败")
def t_capped_at_limit(_):
    d0 = mkd("final.atlimit")
    # 第三轮：BLOCKER 桩改为 doc 长度形态，fixture 须有该文件（207 字 < 1500），
    # 否则 blocker 引用无法核查会被 verify_blockers 降级、merge 直接放行。
    (d0 / "blocks").mkdir(parents=True, exist_ok=True)
    (d0 / "blocks" / "sum_calculation_doc.md").write_text("x" * 207, encoding="utf-8")
    sane_env(d0, mode="BLOCKER", children=1, merge="收束")
    t = table_with("根", max_rounds=2)
    tr = {"root": new_root(round=2, act="spawn", batch=2, state="等子节点")}
    add_child(tr, "root.c2.1", batch=2, state="完成", reply="第 1 块")
    d = driver.Driver(tr["root"], tr, table=t, run_id="final.atlimit", run_dir=str(d0))
    row = d.step()                             # 轮到数 ⇒ 强制收束的那一轮（round=3=max_rounds+1）
    led = final_audit.read_ledger(d)
    vio = vio_of(d0, "终审_blocker_明确失败")
    print("      该轮 act=%s round=%s 去向=%s（强制收束行=%s）" % (
        row.get("act"), row.get("round"), (tr["root"]["merge"] or {}).get("去向"),
        "强制收束" in log_of(d0)))
    print("      该轮 why=%r" % row.get("why"))
    print("      final_audit.jsonl=%s" % json.dumps(led, ensure_ascii=False))
    print("      到数修复后的父: state=%s round=%s 未决=%s" % (
        tr["root"]["state"], tr["root"]["round"],
        json.dumps(tr["root"]["未决"], ensure_ascii=False)))
    check(tr["root"]["round"] == 2 and row.get("rounds_advanced") is True,
          "§2.1.1(B)：round == max_rounds+1 的正常收束合法（回放后 round=2）：%s" % row)
    # 第三轮 P0②：到数 + 修复预算有余 + 规格可派 ⇒ 先派修复叶，不直接明确失败。
    check(isinstance(row.get("why"), str) and row["why"].startswith("【修复派发】"),
          "到数轮出 blocker ⇒ 【修复派发】哨兵（不直接明确失败）：%s" % row.get("why"))
    check(led and all(r.get("作废") is False for r in led)
          and not any(r.get("兜底") == "明确失败" for r in led),
          "★ 到数轮出 blocker ⇒ 不作废、不明确失败（先修）：%s" % led)
    check(len(vio_of(d0, "终审_修复派发")) == 1, "终审_修复派发 应恰 1 笔")
    check(len(vio) == 0, "终审_blocker_明确失败 应为 0 笔，实际 %d" % len(vio))
    check(len(vio_of(d0, "终审_blocker_到数")) == 0, "旧 终审_blocker_到数 必须为 0")
    check(tr["root"]["state"] == "等子节点", "到数修复 ⇒ 父等子节点：%s" % tr["root"]["state"])
    check("收束作废" not in log_of(d0), "到数轮不许再作废：%r" % log_of(d0))
    fix1 = [nid for nid in tr if ".fix.audit1." in nid]
    check(len(fix1) == 1, "到数修复: 建出修复叶：%s" % fix1)
    check(led[0]["轮号"] == 3, "终审行轮号应是收束轮 3：%s" % led)

    # 第三轮 P0②：到数 + 修复预算有余 + 规格可派 ⇒ 先派修复叶（回放强制收束轮），
    # 预算用完才明确失败（全程走 Driver.step，含强制收束 + assert③）。
    d0b = mkd("final.atlimit.repair")
    (d0b / "blocks").mkdir(parents=True, exist_ok=True)
    (d0b / "blocks" / "sum_calculation_doc.md").write_text("x" * 207, encoding="utf-8")
    sane_env(d0b, mode="BLOCKER", children=1, merge="收束")
    trb = {"root": new_root(round=2, act="spawn", batch=2, state="等子节点")}
    add_child(trb, "root.c2.1", batch=2, state="完成", reply="第 1 块")
    db = driver.Driver(trb["root"], trb, table=t, run_id="final.atlimit.repair",
                       run_dir=str(d0b))
    rowb = db.step()                              # 强制收束轮 3 ⇒ 修复派发 1（回放）
    check(isinstance(rowb.get("why"), str) and rowb["why"].startswith("【修复派发】"),
          "到数修复: 应返回【修复派发】哨兵（不直接明确失败）：%s" % rowb.get("why"))
    fix1 = [nid for nid in trb if ".fix.audit1." in nid]
    check(len(fix1) == 1, "到数修复: 建出修复叶：%s" % fix1)
    check(trb["root"]["round"] == 2, "到数修复: 父 round 回退 1（3→2，重放强制收束轮）：%s"
          % trb["root"]["round"])
    check(trb["root"]["state"] == "等子节点", "到数修复: 父等子节点：%s" % trb["root"]["state"])
    check("到数修复派发" in log_of(d0b), "到数修复: driver.log 应有回放行：%r" % log_of(d0b))
    terminate_batch(trb["root"], trb)
    rowb = db.step()                              # 强制收束轮 3 ⇒ 修复派发 2（回放）
    fix2 = [nid for nid in trb if ".fix.audit2." in nid]
    check(isinstance(rowb.get("why"), str) and rowb["why"].startswith("【修复派发】")
          and len(fix2) == 1,
          "到数修复 2: 再派发一轮修复叶：%s / %s" % (rowb.get("why"), fix2))
    check(trb["root"]["round"] == 2, "到数修复 2: 父 round 再次回退到 2：%s"
          % trb["root"]["round"])
    terminate_batch(trb["root"], trb)
    rowb = db.step()                              # 强制收束轮 3 ⇒ 预算用完 ⇒ 明确失败
    check(isinstance(rowb.get("why"), str) and rowb["why"].startswith("【明确失败】"),
          "到数预算用完: 应返回【明确失败】哨兵：%s" % rowb.get("why"))
    check(trb["root"]["state"] == "失败", "到数预算用完: 父进失败终态：%s"
          % trb["root"]["state"])
    check(trb["root"]["round"] == 3, "到数预算用完: 父 round 停在 3（明确失败不回放）：%s"
          % trb["root"]["round"])
    check(len(vio_of(d0b, "终审_修复派发")) == 2, "到数修复派发应恰 2 笔")
    check(len(vio_of(d0b, "终审_blocker_明确失败")) == 1, "到数明确失败应恰 1 笔")


# ================================================================ 场景 4b（阴性对照）
@case("neg_ctl_no_blocker: 终审无 blocker ⇒ 收束不被作废（父完成 + 无 收束作废 行）",
      negative_control=True)
def t_no_blocker(_):
    d0 = mkd("final.noblocker")
    tr = {"root": new_root()}
    d, _rows, tp = spawn_then_merge(d0, "final.noblocker", tr, mode="抽检无发现", merge_go="收束")
    led = final_audit.read_ledger(d)
    print("      final_audit.jsonl=%s" % json.dumps(led, ensure_ascii=False))
    print("      父 state=%s（应为 完成）  收束作废 行=%r" % (
        tr["root"]["state"], "收束作废" in log_of(d0)))
    check(tr["root"]["state"] == "完成", "无 blocker ⇒ 收束成立：%s" % tr["root"]["state"])
    check(led and led[0]["作废"] is False, "不该作废：%s" % led)
    check("收束作废" not in log_of(d0), "不该有 收束作废 行")


# ================================================================ 场景 5
@case("finding_carried_k_plus_1: 发现只进【触发它的那一轮】merge（r3 含 / 末轮 r5 不含）")
def t_carried(_):
    d0 = mkd("carry")
    tp = sane_env(d0, mode="正常", children=3, merge="再派")
    tr = {"root": new_root()}
    d = run(d0, "carry", tr)
    d.step()                                   # plan r1
    d.step()                                   # spawn r2（3 子）⇒ 挂 root.a2.1
    terminate_batch(tr["root"], tr)
    d.step()                                   # merge r3（再派）⇒ 承载 root.a2.1 的发现
    d.step()                                   # spawn r4（3 子）⇒ 挂 root.a4.1
    terminate_batch(tr["root"], tr)
    os.environ["TREE_AUDIT_MERGE"] = "收束"
    d.step()                                   # merge r5（收束）⇒ 只承载 root.a4.1
    merges = [r for r in trace_of(tp) if r.get("act") == "merge"]
    def items(r):
        return [str(x.get("item") or "") for x in (r["out"].get("对审计发现") or [])]
    print("      审计节点=%s" % audit_nodes(tr))
    for r in merges:
        print("      merge round=%s 对审计发现=%s" % (r["round"], json.dumps(items(r), ensure_ascii=False)))
    last = merges[-1]
    check(len(merges) == 2, "应恰 2 次 merge，实际 %d" % len(merges))
    check(any("root.a2.1#1" in i for i in items(merges[0])),
          "r3 那轮必须含 root.a2.1#1：%s" % items(merges[0]))
    check(any("root.a4.1#1" in i for i in items(last)),
          "末轮 r5 必须含 root.a4.1#1：%s" % items(last))
    check(not any("root.a2.1#1" in i for i in items(last)),
          "★ 末轮 r5 不许再出现 root.a2.1#1（发现只进触发它的那轮+1）：%s" % items(last))
    check(merges[0]["round"] == 3 and last["round"] == 5, "承载轮号不对：%s/%s"
          % (merges[0]["round"], last["round"]))
    led = audit.read_ledger(d)
    print("      audit.jsonl 每行 批号/承载轮=%s" % [(r["批号"], r["承载轮"]) for r in led])
    check([(r["批号"], r["承载轮"]) for r in led] == [("root#2", 3), ("root#4", 5)],
          "每批一行 + 承载轮口径不对：%s" % led)


# ================================================================ 场景 6
@case("audit_fail_unreviewed: 审计失败 ⇒ 记未审、不阻塞父（merge 照常放行、收束照常）")
def t_fail(_):
    d0 = mkd("audit.fail")
    tr = {"root": new_root()}
    d, rows, tp = spawn_then_merge(d0, "audit.fail", tr, mode="抽检失败", merge_go="收束")
    led = audit.read_ledger(d)
    fins = trace_kind(tp, "终审")
    print("      audit.jsonl=%s" % json.dumps(led, ensure_ascii=False))
    print("      父: state=%s round=%s  该轮 rounds_advanced=%s" % (
        tr["root"]["state"], tr["root"]["round"], rows[2].get("rounds_advanced")))
    print("      终审调用 %d 次；终审 prompt 含该未审条目=%s" % (
        len(fins), ("未审" in (fins[0]["prompt"] if fins else ""))))
    check(len(led) == 1 and led[0]["状态"] == "未审"
          and str(led[0]["原因"]).startswith("审计失败："), "应记一行 未审/审计失败：%s" % led)
    check(rows[2].get("rounds_advanced") and rows[2].get("去向") == "收束",
          "父的 merge 该照常放行：%s" % rows[2])
    check(tr["root"]["state"] == "完成", "收束照常：%s" % tr["root"]["state"])
    check(len(fins) == 1 and "未审" in fins[0]["prompt"],
          "★ 终审输入必须含未审条目（§5.1⑦）")
    an = audit_nodes(tr)
    check(an and tr[an[0]]["state"] in node_mod.TERMINAL, "审计节点应落终态：%s" % an)


# ================================================================ 场景 7
@case("shape_width_10: 本层宽度 10 ⇒ 触发形状审查、**不截断**（10 个子一个不少）")
def t_width(_):
    d0 = mkd("shape.width")
    tp = sane_env(d0, mode="形状合理", children=1, merge="收束")
    tr = {"root": new_root()}
    for i in range(1, 10):                     # 预置 9 个（历史各批）
        add_child(tr, "root.c1.%d" % i, batch=1, reply="历史批第 %d 块" % i)
    d = run(d0, "shape.width", tr)
    d.step()                                   # plan r1
    r = d.step()                               # spawn r2 ⇒ 第 10 个子
    led = shape_review.read_ledger(d)
    cps = audit.checkpoints(tr["root"], tr)
    same = [nid for nid, n in tr.items()
            if n.get("parent") == "root" and n.get("role") != ROLE_AUDIT]
    print("      本层子节点数=%d（应为 10）  driver.log 里有截断行=%s" % (
        len(same), "截断" in log_of(d0)))
    print("      shape.jsonl=%s" % json.dumps(led, ensure_ascii=False))
    print("      检查点=%s（role=%s）  截断_children 违规=%d 笔" % (
        [(c[0], c[1]) for c in cps], tr[cps[0][0]]["role"] if cps else "-",
        len(vio_of(d0, "截断_children"))))
    check(len(same) == 10, "★ 不截断：10 个子必须一个不少，实际 %d" % len(same))
    check(len(led) == 1 and led[0]["触发"] == "宽度", "shape.jsonl 应一行 触发=宽度：%s" % led)
    check(led[0]["宽度"] == 10 and led[0]["建议值"] == {"width_max": 9, "depth_max": 3},
          "宽度/建议值不对：%s" % led[0])
    check(any(c[1] == "s" for c in cps), "应有形状审查检查点（槽位 s）：%s" % cps)
    check(all(tr[c[0]]["role"] == ROLE_AUDIT and tr[c[0]]["batch"] == 0 for c in cps),
          "检查点属性不对")
    check(len(trace_kind(tp, "形状审查")) == 1, "形状审查应恰调 1 次")


# ================================================================ 场景 8
@case("shape_depth_4: 树深度 4 ⇒ 触发形状审查（触发=深度）")
def t_depth(_):
    d0 = mkd("shape.depth")
    tp = sane_env(d0, mode="形状合理", children=1, merge="收束")
    tr = {"root": new_root()}
    add_child(tr, "n1", batch=1, state="完成")
    add_child(tr, "n2", parent="n1", batch=1, state="完成")
    add_child(tr, "n3", parent="n2", batch=1, state="完成")
    add_child(tr, "n4", parent="n3", batch=1, state="完成")
    d = run(d0, "shape.depth", tr)
    d.step()                                   # plan r1
    d.step()                                   # spawn r2 ⇒ 挂 n5
    led = shape_review.read_ledger(d)
    print("      快照最大深度=%s（应为 4）  每层宽度=%s" % (
        shape_review.depth_of(tr), json.dumps(shape_review.snapshot(tr)["每层宽度"], ensure_ascii=False)))
    print("      shape.jsonl=%s" % json.dumps(led, ensure_ascii=False))
    check(shape_review.depth_of(tr) == 4, "深度应为 4，实际 %s" % shape_review.depth_of(tr))
    check(len(led) == 1 and led[0]["触发"] == "深度" and led[0]["深度"] == 4,
          "应一行 触发=深度：%s" % led)


# ================================================================ 场景 8b（阴性对照）
@case("neg_ctl_shape_at_limits: 宽度恰 9 / 深度恰 3（都**等于**建议值、未越）⇒ 不触发（无行 + 0 次调用）",
      negative_control=True)
def t_shape_neg(_):
    """★ G3 修正（owner 判决 attempt-1 §二 G3）：验收表 §2.5 #8b 写的是「宽度 9 / **深度 3**」，
    attempt-1 这一格用的是**深度 2**（= 没证边界值）。判据是 `> width_max` / `> depth_max` ⇒
    边界值必须**恰好**取 9 / 3，才算证过「等于建议值不触发」。
    """
    d0 = mkd("shape.neg")
    tp = sane_env(d0, mode="形状合理", children=1, merge="收束")
    tr = {"root": new_root()}
    for i in range(1, 9):                      # 8 个历史子
        add_child(tr, "root.c1.%d" % i, batch=1, reply="历史批第 %d 块" % i)
    # 深度链：root(0) → root.c1.1(1) → gc(2) → gc2(3) ⇒ 逐值取到建议值 3
    add_child(tr, "gc", parent="root.c1.1", batch=1, state="完成")
    add_child(tr, "gc2", parent="gc", batch=1, state="完成")
    w0, d0_dep = shape_review.width_of(tr["root"], tr), shape_review.depth_of(tr)
    print("      【落子前逐值夹具】宽度=%d（建议 ≤9）  深度=%d（建议 ≤3）" % (w0, d0_dep))
    d = run(d0, "shape.neg", tr)
    d.step()
    d.step()                                   # spawn r2 ⇒ 第 9 个子（宽度 9 == 建议值）
    led = shape_review.read_ledger(d)
    w, dep = shape_review.width_of(tr["root"], tr), shape_review.depth_of(tr)
    print("      【落子后逐值】宽度=%d（==9）  深度=%d（==3）  shape.jsonl 行数=%d  审查调用=%d" % (
        w, dep, len(led), len(trace_kind(tp, "形状审查"))))
    check(w == 9 and dep == 3, "★ G3：夹具必须逐值取到 宽度=9 / 深度=3，实际 宽度=%s 深度=%s" % (w, dep))
    check(not led, "未越建议值不该有 shape.jsonl 行：%s" % led)
    check(not trace_kind(tp, "形状审查"), "不该有形状审查调用")
    # ★ 区分力自证（同一判据在 9→10 / 3→4 时必须触发；逐值夹具的意义就在这里）
    print("      区分力：判据是 宽度>9 或 深度>3 ⇒ 本格（9/3）不触发；"
          "场景 7（宽度 10）与场景 8（深度 4）各自触发）")


# ================================================================ 场景 9
@case("shape_three_keys: 审查产出逐字只有 判断/理由/建议 三键（产出键是代码实测打印的键集）")
def t_shape_keys(_):
    d0 = mkd("shape.keys")
    sane_env(d0, mode="形状合理", children=1, merge="收束")
    tr = {"root": new_root()}
    for i in range(1, 10):
        add_child(tr, "root.c1.%d" % i, batch=1, reply="历史批第 %d 块" % i)
    d = run(d0, "shape.keys", tr)
    d.step()
    d.step()
    led = shape_review.read_ledger(d)
    print("      产出键=%s（应为 ['判断','理由','建议']）" % json.dumps(
        led[0]["产出键"], ensure_ascii=False) if led else "（无）")
    print("      判断=%r 建议=%r" % (led[0]["判断"], led[0]["建议"]) if led else "")
    check(led and led[0]["产出键"] == ["判断", "理由", "建议"],
          "★ 产出键必须逐字三键（代码实测打印）：%s" % (led[0]["产出键"] if led else None))
    check(led[0]["判断"] in shape_review.JUDGES and led[0]["建议"] in shape_review.ADVICES,
          "枚举值不对：%s" % led[0])


@case("shape_drops_extra_keys: 出现别的键 ⇒ 丢弃 + 记一笔 审查_越权字段，不中断")
def t_shape_extra(_):
    d0 = mkd("shape.extra")
    sane_env(d0, mode="形状越权", children=1, merge="收束")
    tr = {"root": new_root()}
    for i in range(1, 10):
        add_child(tr, "root.c1.%d" % i, batch=1, reply="历史批第 %d 块" % i)
    d = run(d0, "shape.extra", tr)
    d.step()
    d.step()
    led = shape_review.read_ledger(d)
    vio = vio_of(d0, "审查_越权字段")
    print("      产出键（含越权键）=%s  应丢弃的键=%s" % (
        json.dumps(led[0]["产出键"], ensure_ascii=False) if led else "-", "越权键"))
    print("      审查_越权字段 违规 %d 笔：%s" % (len(vio), vio[0]["detail"] if vio else ""))
    check(len(vio) == 1 and "越权键" in vio[0]["detail"], "应记恰 1 笔 审查_越权字段：%s" % vio)
    check(led and led[0]["产出键"] == ["判断", "理由", "建议", "越权键"],
          "产出键应实测含越权键（证明它确实出现过）：%s" % (led[0]["产出键"] if led else None))
    # 丢弃后仍照常写进承载轮
    check(tr["root"]["state"] in node_mod.STATES, "越权键不许把流程弄断")


# ================================================================ 场景 10
@case("shape_advice_to_merge: 建议写进紧接着那轮 merge、父必须逐条回应；去向与建议相反也不报错")
def t_advice(_):
    d0 = mkd("shape.advice")
    tp = sane_env(d0, mode="形状不合理收束", children=1, merge="再派")
    tr = {"root": new_root()}
    for i in range(1, 10):
        add_child(tr, "root.c1.%d" % i, batch=1, reply="历史批第 %d 块" % i)
    d = run(d0, "shape.advice", tr)
    d.step()                                   # plan r1
    d.step()                                   # spawn r2 ⇒ 宽度 10 ⇒ 形状审查
    terminate_batch(tr["root"], tr)
    row = d.step()                             # merge r3（去向=再派，与建议 收束 相反）
    merges = [r for r in trace_of(tp) if r.get("act") == "merge"]
    items = [(str(x.get("item") or ""), str(x.get("建议") or ""))
             for x in (merges[0]["out"].get("对审计发现") or [])]
    print("      merge 轮=%s 去向=%s rounds_advanced=%s" % (
        merges[0]["round"], merges[0]["out"].get("去向"), row.get("rounds_advanced")))
    print("      对审计发现 (item, 建议)=%s" % json.dumps(items, ensure_ascii=False))
    print("      违规=%s（驱动_审计未回应 %d 笔）" % (
        json.dumps([v["kind"] for v in vio_of(d0)], ensure_ascii=False),
        len(vio_of(d0, "驱动_审计未回应"))))
    sid = shape_review.read_ledger(d)[0]["检查点id"]
    check(any(("[%s#1]" % sid) in it for it, _a in items),
          "★ 父必须逐条回应：id 串 [%s#1] 必须在 item 里：%s" % (sid, items))
    check(all(a == "收束" for _i, a in items if a), "建议应逐字为审查给的 收束：%s" % items)
    check(merges[0]["out"]["去向"] == "再派", "父去向应与建议相反（再派）：%s" % merges[0]["out"])
    check(row.get("rounds_advanced") and not vio_of(d0, "驱动_审计未回应"),
          "★ D10 核心：父不采纳建议不许报错：%s" % row)


# ================================================================ 场景 10 阴性对照
@case("neg_ctl_advice_contrary_ok: 同一 fixture、建议=继续派而父去向=收束 ⇒ 仍不报错、收束照常",
      negative_control=True)
def t_advice_neg(_):
    d0 = mkd("shape.advice.neg")
    sane_env(d0, mode="形状合理", children=1, merge="收束")
    tr = {"root": new_root()}
    for i in range(1, 10):
        add_child(tr, "root.c1.%d" % i, batch=1, reply="历史批第 %d 块" % i)
    d = run(d0, "shape.advice.neg", tr)
    d.step()
    d.step()
    terminate_batch(tr["root"], tr)
    row = d.step()
    print("      父去向=%s 建议（审查给的）=继续派  违规=%s  父状态=%s" % (
        (tr["root"]["merge"] or {}).get("去向"),
        json.dumps([v["kind"] for v in vio_of(d0)], ensure_ascii=False), tr["root"]["state"]))
    check((tr["root"]["merge"] or {}).get("去向") == "收束", "父应自己裁决为收束")
    check(not [v for v in vio_of(d0) if "审计" in v["kind"] or "审查" in v["kind"]],
          "相反方向也不该报错：%s" % vio_of(d0))
    check(row.get("rounds_advanced") is True, "该轮该算数")


# ================================================================ 场景 11
@case("nodes_max_hard_still: nodes_max=80 仍硬截（第 81 个被拒 + 截断_nodes_max 一笔；审查不能破 80）")
def t_nodes_max(_):
    d0 = mkd("nodes.max")
    sane_env(d0, mode="形状合理", children=3, merge="收束")
    tr = {"root": new_root()}
    for i in range(1, 77):                     # root + 76 个子 = 77 个节点（3 个新子全收下 ⇒ 正好 80）
        add_child(tr, "root.c1.%d" % i, batch=1, reply="历史批第 %d 块" % i)
    print("      落子前节点总数=%d" % len(tr))
    d = run(d0, "nodes.max", tr)
    d.step()                                   # plan r1
    d.step()                                   # spawn r2 ⇒ 第 80 个收下、第 81/82 个被拒
    trunc = vio_of(d0, "截断_nodes_max")
    led = shape_review.read_ledger(d)
    auled = audit.read_ledger(d)
    print("      落子后节点总数=%d（== 80）" % len(tr))
    print("      截断_nodes_max %d 笔：%s" % (len(trunc), trunc[0]["detail"] if trunc else ""))
    print("      形状审查触发=%s（越限事实在，但审查点不许破 80） 检查点=%s" % (
        json.dumps([r["触发"] for r in led], ensure_ascii=False), audit_nodes(tr)))
    print("      audit.jsonl=%s" % json.dumps(auled, ensure_ascii=False))
    check(len(tr) == 80, "★ 总量必须仍 == 80，实际 %d" % len(tr))
    # 落子本身不截断（3 个都收下），2 笔来自**审查点/抽检点创建被 80 硬闸拒**
    check(len(trunc) == 2, "应有 2 笔 截断_nodes_max（形状审查点 + 抽检点各 1），实际 %d" % len(trunc))
    check(audit_nodes(tr) == [], "总量到顶 ⇒ 审查点/抽检点也不许创建（不许破 80）：%s" % audit_nodes(tr))
    # 审查点没建起来 ⇒ shape.jsonl 该记一行「未审」（原因=nodes_max 拒），绝不许出现判断/建议
    check(len(led) == 1 and led[0]["状态"] == "未审" and "nodes_max" in led[0]["原因"]
          and not led[0]["判断"] and not led[0]["建议"] and led[0]["检查点id"] == "",
          "★ 审查点没建起来 ⇒ 只许记一行 未审，不许有判断：%s" % led)
    check(led[0]["触发"] == "宽度", "该行应记越限事实 触发=宽度：%s" % led)
    check(auled and auled[-1]["状态"] == "未审" and "nodes_max" in auled[-1]["原因"],
          "抽检点被 nodes_max 拒时应记一行 未审：%s" % auled)
    # ★ 值跟着数据走：换一份 driver 副本（nodes_max=5），同一 fixtures 下截断点跟着变
    d1 = mkd("nodes.max.tbl")
    sane_env(d1, mode="形状合理", children=3, merge="收束")
    t2 = table_driver(nodes_max=5)
    tr2 = {"root": new_root()}
    for i in range(1, 4):
        add_child(tr2, "root.c1.%d" % i, batch=1)
    d2 = run(d1, "nodes.max.tbl", tr2, table=t2)
    d2.step()
    d2.step()
    print("      【同一 fixtures + driver.nodes_max=5】节点总数=%d（应为 5）截断笔数=%d" % (
        len(tr2), len(vio_of(d1, "截断_nodes_max"))))
    check(len(tr2) == 5 and len(vio_of(d1, "截断_nodes_max")) == 2,
          "nodes_max 必须从 role 表读（不许硬编码 80）：%s" % len(tr2))


# ================================================================ 场景 12
@case("audit_branch_by_code: 审计枝由代码挂、父看不见删不掉（三条结构性证据 + 不回应被拒）")
def t_branch(_):
    d0 = mkd("audit.branch")
    tp = sane_env(d0, mode="无回复", children=3, merge="收束")
    tr = {"root": new_root()}
    d = run(d0, "audit.branch", tr)
    d.step()                                   # plan
    r2 = d.step()                              # spawn ⇒ 挂审计枝
    an = audit_nodes(tr)
    in_result = "root.a2.1" in str(tr["root"].get("result") or "")
    in_reply = "root.a2.1" in str(tr["root"].get("reply") or "")
    kids = json.loads(tr["root"]["result"] or "{}").get("children") or []
    terminate_batch(tr["root"], tr)            # 本批子全终结 ⇒ merge 才跑得起来
    r3 = d.step()                              # merge（桩故意不回应 id 串）⇒ 该轮该被拒
    vio = vio_of(d0, "驱动_审计未回应")
    print("      ① 父产出里含审计节点 id：result=%s reply=%s；父产出 children=%s" % (
        in_result, in_reply, kids))
    print("      ② 审计节点 =%s  role=%s parent=%s batch=%s state=%s" % (
        an, tr[an[0]]["role"] if an else "-", tr[an[0]]["parent"] if an else "-",
        tr[an[0]]["batch"] if an else "-", tr[an[0]]["state"] if an else "-"))
    print("      ③ 父不回应 ⇒ 该轮 rounds_advanced=%s 被拒=%r" % (
        r3.get("rounds_advanced"), r3.get("rejected")))
    print("      驱动_审计未回应 %d 笔：%s" % (len(vio), vio[0]["detail"] if vio else ""))
    print("      被拒后审计节点仍在树里=%s  父 round=%s（未增长）" % (
        bool(audit_nodes(tr)), tr["root"]["round"]))
    check(not in_result and not in_reply, "★ 父的产出里不许出现审计节点 id")
    check(all(not str(k).startswith("root.a") for k in kids), "children 里不许有审计节点：%s" % kids)
    check(an == ["root.a2.1"], "审计节点 id 形态不对：%s" % an)
    check(tr[an[0]]["parent"] == "root" and tr[an[0]]["batch"] == 0
          and tr[an[0]]["role"] == ROLE_AUDIT, "★ 审计节点 parent/batch/role 不符合钉死值")
    check(r3.get("rounds_advanced") is False and len(vio) == 1,
          "★ 父不回应 ⇒ 该轮被拒 + 恰 1 笔 驱动_审计未回应：%s" % r3)
    check(bool(audit_nodes(tr)), "★ 被拒后审计节点仍必须在树里（删不掉）")


# ================================================================ 场景 13 / 14
@case("audit_reads_siblings_not_grandchildren: 兄弟标记全出现（==3）；孙的标记不出现")
def t_siblings(_):
    d0 = mkd("audit.scope")
    tp = sane_env(d0, mode="抽检无发现", children=1, merge="收束")
    tr = {"root": new_root()}
    marks = []
    for i in (1, 2, 3):
        m = "SIBMARK-%d" % i
        marks.append(m)
        add_child(tr, "root.c2.%d" % i if False else "root.b2.%d" % i, batch=2, state="完成",
                  reply="兄弟产出 %s 的正文" % m, result=json.dumps({"r": m}, ensure_ascii=False))
    add_child(tr, "root.b2.1.g1", parent="root.b2.1", batch=3, state="完成",
              reply="孙的正文 GRANDMARK-9 不该被读到")
    d = run(d0, "audit.scope", tr)
    d.step()                                   # plan r1
    d.step()                                   # spawn r2（父 batch=2）⇒ 抽检那些 batch==2 的兄弟
    cps = trace_kind(tp, "抽检")
    p0 = cps[0]["prompt"] if cps else ""
    found = [m for m in marks if m in p0]
    print("      抽检 prompt 字长=%d" % len(p0))
    print("      兄弟标记（SIBMARK-1..3）命中=%s（应 == 3）" % found)
    print("      孙的标记 GRANDMARK-9 出现在 prompt 里=%s（应为 False）" % ("GRANDMARK-9" in p0))
    check(len(cps) == 1, "应恰 1 次抽检调用：%d" % len(cps))
    check(len(found) == 3, "★ 3 个兄弟标记必须全部逐字出现：%s" % found)
    check("GRANDMARK-9" not in p0, "★ 孙的内容不许进审计 prompt（只读兄弟产出）")


@case("audit_full_coverage: 3 个兄弟各带独特标记 ⇒ 3 个全在（计数 == 3，一条不丢）")
def t_coverage(_):
    d0 = mkd("audit.cover")
    tp = sane_env(d0, mode="抽检无发现", children=1, merge="收束")
    tr = {"root": new_root()}
    u = []
    for i in (1, 2, 3):
        m = "COVER-%s-%d" % (chr(64 + i), i)
        u.append(m)
        add_child(tr, "root.b2.%d" % i, batch=2, state="完成",
                  reply="第 %d 块的独特正文 %s" % (i, m),
                  result=json.dumps({"标记": m}, ensure_ascii=False))
    d = run(d0, "audit.cover", tr)
    d.step()
    d.step()
    cps = trace_kind(tp, "抽检")
    p0 = cps[0]["prompt"] if cps else ""
    found = [m for m in u if m in p0]
    print("      本批三块标记=%s" % u)
    print("      抽检 prompt 里命中=%s（计数 == %d）" % (found, len(found)))
    print("      prompt 含会计视图（batch_view 原文）=%s" % ("本批次会计视图" in p0))
    check(len(found) == 3, "★ 全覆盖：3 个标记必须全在：%s" % found)
    check("本批次会计视图" in p0, "必须复用 driver.batch_view 附会计视图")


# ================================================================ 场景 15
@case("shape_snapshot_no_content: 审查读结构快照（逐字五键）、不含任何正文（而抽检必须有）")
def t_snapshot(_):
    d0 = mkd("shape.snap")
    tp = sane_env(d0, mode="形状合理", children=3, merge="收束")
    tr = {"root": new_root()}
    marks = []
    for i in range(1, 9):                       # 8 个 batch=2 的兄弟（带正文标记）
        m = "NFX-%d" % i
        marks.append(m)
        add_child(tr, "root.b2.%d" % i, batch=2, state="完成",
                  reply="正文 %s" % m, result=json.dumps({"m": m}, ensure_ascii=False))
    d = run(d0, "shape.snap", tr)
    d.step()                                   # plan r1
    d.step()                                   # spawn r2（3 子，batch=2）⇒ 宽度 11、批次 11
    sh = trace_kind(tp, "形状审查")
    sp = sh[0]["prompt"] if sh else ""
    cps = trace_kind(tp, "抽检")
    cp = cps[0]["prompt"] if cps else ""
    snap = shape_review.snapshot(tr)
    print("      形状审查 prompt 里：五键命中=%s" % json.dumps(
        [k for k in SNAP_KEYS if k in sp], ensure_ascii=False))
    print("      形状审查 prompt 里的正文标记命中=%s（必须为空）" % [m for m in marks if m in sp])
    print("      抽检 prompt 里的正文标记命中=%d 个（>0 ⇒ 有区分力）" % len([m for m in marks if m in cp]))
    print("      快照键集=%s  节点[0]=%s" % (
        json.dumps(list(snap.keys()), ensure_ascii=False),
        json.dumps(snap["节点"][0], ensure_ascii=False)))
    check(all(k in sp for k in SNAP_KEYS), "★ 快照五键必须逐字在审查 prompt 里")
    check(not [m for m in marks if m in sp], "★ 审查 prompt 不许含任何节点正文")
    check(list(snap.keys()) == ["节点总数", "最大深度", "每层宽度", "节点"], "快照键集不对：%s"
          % list(snap.keys()))
    check(list(snap["节点"][0].keys()) == ["id", "role", "parent", "state"],
          "节点条目应只有 id/role/parent/state：%s" % list(snap["节点"][0].keys()))
    check([m for m in marks if m in cp], "★ 抽检 prompt 里必须有正文（区分力）")
    check(snap["节点总数"] == len(tr), "节点总数应含检查点：%s vs %s" % (snap["节点总数"], len(tr)))
    check(snap["最大深度"] == shape_review.depth_of(tr), "最大深度口径不对")


# ================================================================ 场景 16
@case("legacy_hard_gates_inline: 三条既有硬闸零回归（连续 spawn 仍拒 / 到数仍强制收束 / 本批次口径仍成立）")
def t_legacy(_):
    # (a) 连续两轮 spawn 仍被结构性拒绝
    d0 = mkd("legacy.a")
    sane_env(d0, mode="抽检无发现", children=1, merge="收束")
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, run_id="legacy.a", run_dir=str(d0),
                      act_seq=["plan", "spawn", "spawn", "merge"])
    d.step()
    d.step()
    bad = d.step()
    print("      (a) 第 3 次请求 spawn ⇒ 被拒=%r  驱动_连续spawn=%d 笔  round=%s" % (
        bool(bad.get("rejected")), len(vio_of(d0, "驱动_连续spawn")), tr["root"]["round"]))
    check(bad.get("rejected") and len(vio_of(d0, "驱动_连续spawn")) == 1
          and tr["root"]["round"] == 2, "连续 spawn 没被拒：%s" % bad)

    # (b) 到数仍强制收束（含强制收束日志行）
    d1 = mkd("legacy.b")
    sane_env(d1, mode="抽检无发现", children=1, merge="再派")
    t = table_with("根", max_rounds=2)
    tr1 = {"root": new_root()}
    d1r = driver.Driver(tr1["root"], tr1, table=t, run_id="legacy.b", run_dir=str(d1))
    d1r.step()
    d1r.step()
    terminate_batch(tr1["root"], tr1)
    row = d1r.step()
    print("      (b) max_rounds=2 的第 3 轮 act=%s 改写成=%s 父 state=%s 强制收束行=%s" % (
        row.get("act"), row.get("rewritten_to"), tr1["root"]["state"],
        "强制收束" in log_of(d1)))
    check(row.get("act") == "merge" and row.get("rewritten_to") == "收束"
          and "强制收束" in log_of(d1) and tr1["root"]["state"] == "完成",
          "到数强制收束坏了：%s" % row)

    # (c) 本批次口径仍成立：第 1 批还剩一个在跑 ⇒ 本批次 merge 照常放行
    d2 = mkd("legacy.c")
    sane_env(d2, mode="抽检无发现", children=1, merge="收束")
    tr2 = {"root": new_root(round=5, act="spawn", batch=4, state="等子节点")}
    add_child(tr2, "root.c1", batch=2, state="完成")
    add_child(tr2, "root.c2", batch=2, state="在跑")
    add_child(tr2, "root.c3", batch=4, state="完成")
    add_child(tr2, "root.c4", batch=4, state="失败")
    members = sorted(node_mod.batch_members(tr2["root"], tr2))
    ok, why = node_mod.ready_to_merge(tr2["root"], tr2)
    d2r = driver.Driver(tr2["root"], tr2, run_id="legacy.c", run_dir=str(d2))
    row2 = d2r.step()
    print("      (c) 本批次(batch=4)=%s ready=%s 该轮 act=%s rounds_advanced=%s" % (
        members, ok, row2.get("act"), row2.get("rounds_advanced")))
    check(members == ["root.c3", "root.c4"] and ok and row2.get("act") == "merge"
          and row2.get("rounds_advanced"), "本批次口径坏了：%s / %s" % (members, row2))


# ================================================================ 场景 16 阴性对照
@case("neg_ctl_double_spawn_legal_seq: 合法序列 plan→spawn→[等]→merge ⇒ 不产生该笔违规",
      negative_control=True)
def t_legal(_):
    d0 = mkd("legacy.a.neg")
    sane_env(d0, mode="抽检无发现", children=1, merge="收束")
    tr = {"root": new_root()}
    d = driver.Driver(tr["root"], tr, run_id="legacy.a.neg", run_dir=str(d0),
                      act_seq=["plan", "spawn", "merge"])
    d.step()
    d.step()
    terminate_batch(tr["root"], tr)
    row = d.step()
    print("      违规=%s  round=%s state=%s" % (
        json.dumps([v["kind"] for v in vio_of(d0)], ensure_ascii=False),
        tr["root"]["round"], tr["root"]["state"]))
    check(not vio_of(d0, "驱动_连续spawn") and row.get("rounds_advanced")
          and tr["root"]["state"] == "完成", "合法序列不该被拒：%s" % row)


@case("neg_ctl_forced_high: max_rounds=99 时同一个「再派」不被改写、无强制收束行",
      negative_control=True)
def t_not_forced(_):
    d0 = mkd("legacy.b.neg")
    sane_env(d0, mode="抽检无发现", children=1, merge="再派")
    t = table_with("根", max_rounds=99)
    tr = {"root": new_root()}
    d = run(d0, "legacy.b.neg", tr, table=t)
    d.step()
    d.step()
    terminate_batch(tr["root"], tr)
    row = d.step()
    print("      改写成=%s 去向=%s 强制收束行=%s 违规=%s" % (
        row.get("rewritten_to"), row.get("去向"), "强制收束" in log_of(d0),
        json.dumps([v["kind"] for v in vio_of(d0)], ensure_ascii=False)))
    check(row.get("rewritten_to") is None and "强制收束" not in log_of(d0),
          "不该改写：%s" % row)


# ================================================================ G1（owner 判决 attempt-1 §二 G1）
@case("g1_reply_shape_enforced: 换键后 `对审计发现[]` 的**形状**由本批新代码机械执行（4 格逐一实测）")
def t_g1_shape(_):
    """owner 的 `g1_boundary.py` 实测：换键（注入 `对审计发现`）后 `schema` 的审计分支不触发
    ⇒ 连 `对审计发现[{}]` / `处置="彻底瞎写"` / `建议="瞎建议"` 都 `schema=True`（形状无执行者）。
    本批把这段形状补在**自己的新代码**里（`final_audit.reply_shape_problems`，driver 插入点 ④a）。
    ★ 四条判据**都由真实调用**给出：① schema.validate ② reply_shape_problems ③ missing_replies
      ④ 驱动器实跑（父产出的 `处置` 是枚举外的值 ⇒ 该轮被拒 + 一笔 `驱动_审计未回应`）。
    """
    d0 = mkd("g1.shape")
    tp = sane_env(d0, mode="正常", children=3, merge="收束")
    tr = {"root": new_root()}
    d = run(d0, "g1.shape", tr)
    d.step()                                    # plan r1
    d.step()                                    # spawn r2 ⇒ 挂 root.a2.1（本批 3 子）
    terminate_batch(tr["root"], tr)
    knobs = {"对审计发现": [{"id": "[root.a2.1#1]"}]}    # 本批实际注入的键
    goods = {"去向": "再派", "reply": "r", "未决": [], "下一批要点": ["x"],
             "对审计发现": [{"item": "[root.a2.1#1] x", "处置": "解掉", "说明": "d"}]}
    print("      ① `对审计发现[{}]`（连 item 都不给）")
    print("         schema=%s   本批形状自检=%s" % (
        schema.validate("merge", {"去向": "再派", "reply": "r", "未决": [], "下一批要点": ["x"],
                                  "对审计发现": [{}]}, knobs)[0],
        final_audit.reply_shape_problems({"对审计发现": [{}]})))
    print("      ② `处置:\"彻底瞎写\"`")
    print("         schema=%s   本批形状自检=%s" % (
        schema.validate("merge", dict(goods, **{"对审计发现": [
            {"item": "[root.a2.1#1] x", "处置": "彻底瞎写", "说明": "d"}]}), knobs)[0],
        final_audit.reply_shape_problems({"对审计发现": [
            {"item": "[root.a2.1#1] x", "处置": "彻底瞎写", "说明": "d"}]})))
    print("      ③ `建议:\"瞎建议\"`")
    print("         schema=%s   本批形状自检=%s" % (
        schema.validate("merge", dict(goods, **{"对审计发现": [
            {"item": "[root.a2.1#1] x", "处置": "解掉", "说明": "d", "建议": "瞎建议"}]}), knobs)[0],
        final_audit.reply_shape_problems({"对审计发现": [
            {"item": "[root.a2.1#1] x", "处置": "解掉", "说明": "d", "建议": "瞎建议"}]})))
    print("      ④ 整个 `对审计发现` 键不给 ⇒ schema=%s  missing_replies=%s（由 id 串兜住）" % (
        schema.validate("merge", {"去向": "再派", "reply": "r", "未决": [], "下一批要点": ["x"]},
                        knobs)[0],
        final_audit.missing_replies({"去向": "再派", "reply": "r", "未决": [], "下一批要点": ["x"]},
                                    tr["root"], d)))
    # ①-③ 都必须是「schema 放行、本批形状自检拦住」⇒ 证明这段形状现在**有执行者**
    for name, bad in (("缺 item/处置/说明", {}),
                      ("处置枚举外", {"item": "[root.a2.1#1] x", "处置": "彻底瞎写", "说明": "d"}),
                      ("建议枚举外", {"item": "[root.a2.1#1] x", "处置": "解掉", "说明": "d",
                                  "建议": "瞎建议"})):
        ok_s, _w = schema.validate("merge", dict(goods, **{"对审计发现": [bad]}), knobs)
        probs = final_audit.reply_shape_problems({"对审计发现": [bad]})
        check(ok_s is True, "★ 该格 schema 本应放行（这正是 G1 的缺陷面）：%s" % name)
        check(bool(probs), "★ 本批形状自检必须拦住它：%s" % name)
    check(not final_audit.reply_shape_problems(goods), "合规条目不许被误拦：%s"
          % final_audit.reply_shape_problems(goods))
    # ④ 驱动器实跑：桩把 `处置` 写成枚举外的值 ⇒ 该轮必须被拒
    os.environ["TREE_AUDIT_MODE"] = "审计回应形状不符"
    row = d.step()
    vio = vio_of(d0, "驱动_审计未回应")
    print("      ⑤ 驱动器实跑（桩产出 处置=彻底瞎写，id 串在）：rounds_advanced=%s 被拒=%r" % (
        row.get("rounds_advanced"), row.get("rejected")))
    print("         驱动_审计未回应 %d 笔：%s" % (len(vio), vio[0]["detail"] if vio else ""))
    check(row.get("rounds_advanced") is False and len(vio) == 1
          and "形状不合规" in vio[0]["detail"],
          "★ 形状不合规必须让该轮不算数（本批新代码执行）：%s / %s" % (row, vio))
    os.environ["TREE_AUDIT_MODE"] = "正常"


# ================================================================ 零 pi 进程
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
    n_before = PI_SAMPLES["n"]
    d, _rows, _tp = spawn_then_merge(d0, "zero.pi", tr, children=3, merge_go="再派")
    t0 = time.time()
    while time.time() - t0 < 2.0:
        time.sleep(0.05)
    raw_now = subprocess.run(["pgrep", "-x", "pi"], capture_output=True, text=True).stdout.strip()
    ps_line = subprocess.run(
        ["bash", "-c", "ps -eo pid,etime,comm,args | awk '$3==\"pi\"' | head -3"],
        capture_output=True, text=True).stdout.strip()
    print("      采样 %d 次（本用例压力段 %d 次），命中 %d 次（应为 0）" % (
        PI_SAMPLES["n"], PI_SAMPLES["n"] - n_before, len(PI_SAMPLES["hits"])))
    print("      收尾直查 `pgrep -x pi` 输出=%r（应为空）" % raw_now)
    print("      `ps -eo pid,etime,comm,args | awk '$3==\"pi\"'`=%r（应为空）" % ps_line)
    check(PI_SAMPLES["n"] - n_before > 20, "压力段采样太少：%d" % (PI_SAMPLES["n"] - n_before))
    check(not PI_SAMPLES["hits"], "桩路径起了 pi：%s" % PI_SAMPLES["hits"])
    check(raw_now == "" and ps_line == "", "桩路径出现了 pi 进程")


# ================================================================ 跑
def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    os.environ["TREE_ACT_CMD"] = STUB
    os.environ["TREE_RUNS_DIR"] = str(TMP / "runs")
    stop = threading.Event()
    th = threading.Thread(target=_pi_sampler, args=(stop,), daemon=True)
    th.start()
    passed, failed = 0, []
    import yaml
    print("tree-04 audit · root=%s · python=%s · yaml=%s" % (
        ROOT, sys.version.split()[0], yaml.__version__))
    print("桩 act 脚本 = %s（$TREE_ACT_CMD；桩路径一个 pi 进程都不起）" % STUB)
    for name, fn in CASES:
        try:
            fn(None)
            print("CASE %-46s PASS" % name.split(":")[0])
            passed += 1
        except Exception as exc:                              # noqa: BLE001
            failed.append(name)
            print("CASE %-46s FAIL: %s" % (name.split(":")[0], exc))
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
    if total < 18 or len(NC) < 5:
        print("GATES: %d/%d FAIL（total<18 或 阴性对照<5 视为不合格）" % (passed, total))
        return 1
    if failed:
        print("GATES: %d/%d FAIL" % (passed, total))
        return 1
    print("GATES: %d/%d PASS" % (passed, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
