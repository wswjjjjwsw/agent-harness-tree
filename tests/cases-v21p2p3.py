"""V2.1 阶段二 P2+P3 回归（t-7aba97efa3d1）。跑法：cd <候选根> && python3 tests/cases-v21p2p3.py
离线：不联网、不打 8092/8903、不起 pi、不调 DeepSeek。
"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import budget as budget_mod, contract as ct, driver as driver_mod, fields as fields_mod
from harness import final_audit as fa_mod, leafrun, node as node_mod, premise as premise_mod
from harness import recovery as rec_mod, role as role_mod, session as session_mod, trust as tr

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

TABLE = role_mod.load_roles()

# ---------- fields: 硬字段缺失不默认 ---------- #
hard, filled = fields_mod.classify_and_fill("leaf", {"reply": "x"}, {})
check("fields leaf missing 未决 is hard error",
      any(e["code"] == "hard_field_missing:未决" for e in hard), hard)
check("fields leaf missing 未决 not defaulted",
      "未决" not in {"reply": "x"} and filled == [], filled)
hard, filled = fields_mod.classify_and_fill("leaf", {"reply": "x", "未决": []}, {})
check("fields leaf explicit 未决=[] is ok", hard == [], hard)
obj = {"reply": "x", "未决": [{"type": "缺信息", "what": "缺数"}]}
hard, filled = fields_mod.classify_and_fill("leaf", obj, {})
check("fields leaf 未决 missing why soft-filled",
      hard == [] and filled == ["未决[0].why"] and obj["未决"][0]["why"] == fields_mod.SOFT_DEFAULT,
      (hard, filled, obj))
hard, _ = fields_mod.classify_and_fill("merge", {"reply": "r", "未决": []}, {})
check("fields merge missing 去向 is hard error",
      any(e["code"] == "hard_field_missing:去向" for e in hard), hard)
hard, _ = fields_mod.classify_and_fill("merge", {"去向": "收束", "reply": "r", "未决": []}, {})
check("fields merge 收束 missing uncovered is hard error",
      any(e["code"] == "hard_field_missing:uncovered" for e in hard), hard)
hard, _ = fields_mod.classify_and_fill("merge", {"去向": "再派", "reply": "r", "未决": []}, {})
check("fields merge 再派 missing 下一批要点 is hard error",
      any(e["code"] == "hard_field_missing:下一批要点" for e in hard), hard)
hard, _ = fields_mod.classify_and_fill("merge",
    {"去向": "收束", "reply": "r", "未决": [], "uncovered": []}, {})
check("fields merge complete is ok", hard == [], hard)
hard, _ = fields_mod.classify_and_fill("leaf", {"reply": "x", "未决": []}, {"may_write": True})
check("fields leaf may_write missing doc is hard error",
      any(e["code"] == "hard_field_missing:doc" for e in hard), hard)

# ---------- contract: 幻影引用分类 + 策略 ---------- #
fc = ct.classify_failure("失败", "零工具调用；无法核查的引用：blocks/cpu_info.md", {})
check("contract phantom_citation combined signal", fc == "phantom_citation", fc)
fc = ct.classify_failure("失败", "零工具调用；未查到数据", {})
check("contract zero-tool alone is zero_tool_research", fc == "zero_tool_research", fc)
fc = ct.classify_failure("失败", "无法核查的引用：blocks/不存在.md", {})
check("contract bad citation alone is bad_citation", fc == "bad_citation", fc)
sts = ct.strategies_for("phantom_citation")
check("contract phantom has 2 distinct strategies",
      len(sts) == 2 and len({ct.action_fingerprint(s) for s in sts}) == 2, sts)

# ---------- recovery: 换策略 + 耗尽 ---------- #
with tempfile.TemporaryDirectory() as tmp:
    rc = rec_mod.RecoveryController(tmp)
    s1 = rc.choose("p1", "p1.c1", "phantom_citation", "幻影引用")
    s2 = rc.choose("p1", "p1.c1", "phantom_citation", "幻影引用仍同错")
    s3 = rc.choose("p1", "p1.c1", "phantom_citation", "第三次")
    check("recovery phantom first strategy dispatched", s1 is not None, s1)
    check("recovery phantom second strategy differs",
          s2 is not None and ct.action_fingerprint(s2) != ct.action_fingerprint(s1), (s1, s2))
    check("recovery phantom third is exhausted", s3 is None, s3)

# ---------- premise: 证伪标记 + 产物保留 ---------- #
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    (run_dir / "doc").mkdir()
    (run_dir / "doc" / "root.c2.1.md").write_text("内存实测 32GB。", encoding="utf-8")
    premise_mod.record_premise(str(run_dir), "P-1", "CPU 核心数为 8",
                               dependents=["root.c2.1", "root.c2.2"], source_node="root")
    e1 = tr.trust_entry({"id": "root.c2.1", "role": "调研", "state": "完成"},
                        "complete", "完成",
                        [{"kind": "doc", "path": str(run_dir / "doc" / "root.c2.1.md")}],
                        {"calls": 2, "successes": 2, "failures": 0}, "sid1", [], [])
    e1["kind"] = "research"
    tr.append_trust(str(run_dir), e1)
    e2 = tr.trust_entry({"id": "root.c2.2", "role": "调研", "state": "失败"},
                        "failed", "失败", [],
                        {"calls": 1, "successes": 0, "failures": 1}, "sid2", [], [])
    e2["kind"] = "research"
    tr.append_trust(str(run_dir), e2)
    res = premise_mod.falsify_premise(str(run_dir), {}, "P-1", "实测核心数为 4", source_node="root")
    t = tr.read_trust(str(run_dir))
    check("premise falsify marks complete dependent suspect",
          t["root.c2.1"].get("trust_status") == "suspect", t["root.c2.1"])
    check("premise falsify marks failed dependent invalidated",
          t["root.c2.2"].get("trust_status") == "invalidated", t["root.c2.2"])
    check("premise falsify preserves product file on disk",
          (run_dir / "doc" / "root.c2.1.md").is_file(), res)
    check("premise falsify returns affected scope",
          set(res["affected"]) == {"root.c2.1", "root.c2.2"}, res)

node = {"id": "root.c2.1", "未决": [{"type": "冲突", "what": "前提被证伪：P-1：实测核心数为 4", "why": "实测核心数为 4"}]}
prem = premise_mod.find_premise_falsification(node)
check("premise find parses falsification marker",
      prem is not None and prem["premise_id"] == "P-1" and prem["evidence"] == "实测核心数为 4", prem)

# ---------- premise: 未登记依赖时只标报告叶自身（不拉无关兄弟） ---------- #
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    premise_mod.record_premise(str(run_dir), "P-9", "某前提", dependents=[], source_node="root")
    e1 = tr.trust_entry({"id": "root.c2.1", "role": "调研", "state": "失败"},
                        "failed", "失败", [],
                        {"calls": 1, "successes": 0, "failures": 1}, "sid1", [], [])
    e1["kind"] = "research"
    tr.append_trust(str(run_dir), e1)
    e2 = tr.trust_entry({"id": "root.c2.2", "role": "调研", "state": "完成"},
                        "complete", "完成", [],
                        {"calls": 2, "successes": 2, "failures": 0}, "sid2", [], [])
    e2["kind"] = "research"
    tr.append_trust(str(run_dir), e2)
    tree = {"root": {"id": "root", "batch": 2},
            "root.c2.1": {"id": "root.c2.1", "parent": "root", "batch": 2},
            "root.c2.2": {"id": "root.c2.2", "parent": "root", "batch": 2}}
    res = premise_mod.falsify_premise(str(run_dir), tree, "P-9", "依据", source_node="root.c2.1")
    t = tr.read_trust(str(run_dir))
    check("premise fallback marks only reporting leaf",
          res["affected"] == ["root.c2.1"] and t["root.c2.1"].get("trust_status") == "invalidated",
          res)
    check("premise fallback leaves sibling unmarked",
          "trust_status" not in t["root.c2.2"], t["root.c2.2"])

# ---------- leafrun: 硬字段错误短路（why 不被后续检查覆盖） ---------- #
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(Path(tmp) / "sess")
    (Path(tmp) / "sess").mkdir(exist_ok=True)
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None, 验收标准="", auth="")
    def _stub_hard(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        # 缺 未决（硬错）+ 引用不存在文件（若证据门跑了会改写 why）
        return json.dumps({"reply": "CPU 核心数为 8，见 blocks/cpu_info.md。"}, ensure_ascii=False)
    timed = leafrun.TimedRunner(_stub_hard)
    d = leafrun.TreeDriver(root, {"root": root}, TABLE, run_id="t-hard",
                           run_dir=str(run_dir), act_runner=timed, timed=timed)
    n = node_mod.new_node("root.c2.1", "调研", parent="root",
                          context="调查 CPU 核心数", judge={"chars_min": 5},
                          验收标准="有数字", auth="只读", batch=2)
    d.tree["root.c2.1"] = n
    row = leafrun.run_leaf(d, n)
    check("leafrun hard error short-circuits (why stays hard-field error)",
          "硬字段错误" in (row.get("why") or "") and "无法核查的引用" not in (row.get("why") or ""),
          row.get("why"))
    check("leafrun hard error keeps error_code",
          row.get("error_code") == "hard_field_missing:未决", row.get("error_code"))
    viol = (run_dir / "violations.jsonl").read_text(encoding="utf-8") if (run_dir / "violations.jsonl").is_file() else ""
    check("leafrun hard error skips evidence gate (no 证据门 violation)",
          "驱动_证据门" not in viol, viol)
    os.environ.pop("TREE_PI_AGENT_SESSION_DIR", None)

# ---------- final_audit: blocker 逐条核对 ---------- #
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    class _D:
        pass
    d = _D()
    d.run_dir = str(run_dir)
    blockers = [
        {"item": "结论见 blocks/cpu_info.md 第 3 行", "级别": "blocker"},
        {"item": "内存 32GB，未见异常", "级别": "blocker"},
    ]
    verified, unverified = fa_mod.verify_blockers(blockers, d)
    check("verify_blockers nonexistent ref is unverified",
          len(unverified) == 1 and unverified[0] is blockers[0], (verified, unverified))
    check("verify_blockers no-ref blocker is verified",
          len(verified) == 1 and verified[0] is blockers[1], (verified, unverified))

# ---------- budget: 档位 + 溢出记录 ---------- #
check("budget next rung 8192->16384", budget_mod.next_budget_rung(8192) == 16384)
check("budget next rung 32768->65536", budget_mod.next_budget_rung(32768) == 65536)
check("budget next rung 65536 is top (None)", budget_mod.next_budget_rung(65536) is None)
with tempfile.TemporaryDirectory() as tmp:
    rec = budget_mod.record_overflow(tmp, {"node_id": "root.c2.1", "act": "leaf",
                                           "history_tokens": 40000, "estimate": 100,
                                           "budget": 8192, "overage": 31908, "next_rung": 16384})
    lines = (Path(tmp) / "overflow.jsonl").read_text(encoding="utf-8").strip().splitlines()
    got = json.loads(lines[0])
    check("budget record_overflow writes one line",
          len(lines) == 1 and got["node_id"] == "root.c2.1" and got["overage"] == 31908, got)

# ---------- session: 按档扩容阶梯（假会话文件，大历史） ---------- #
with tempfile.TemporaryDirectory() as tmp:
    sess_dir = Path(tmp) / "sess"
    sess_dir.mkdir()
    (sess_dir / "20260101_t-ladder.jsonl").write_text(
        json.dumps({"type": "message", "message": {"role": "assistant", "content": "x",
                                                   "usage": {"input": 40000}}}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    os.environ.pop("TREE_ACT_CMD", None)
    raised = None
    try:
        session_mod.run_act({"id": "root.c2.1", "role": "调研"}, "leaf",
                            {"ctx_budget": 8192, "max_tokens": 0}, "test prompt",
                            session_id="t-ladder", run_dir=str(run_dir), sess=str(sess_dir))
    except session_mod.ContextOverflow as exc:
        raised = str(exc)
    except Exception as exc:  # noqa: BLE001
        raised = "WRONG-EXC %s: %s" % (type(exc).__name__, exc)
    check("session ladder raises ContextOverflow after 2 widenings",
          raised is not None and not (raised or "").startswith("WRONG") and "按档扩容" in (raised or ""),
          raised)
    of = Path(run_dir) / "overflow.jsonl"
    nrec = len(of.read_text(encoding="utf-8").strip().splitlines()) if of.is_file() else 0
    check("session ladder records 3 overflows (8192/16384/32768)", nrec == 3, nrec)

# ---------- driver: 幻影同错重派守卫 ---------- #
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None, 验收标准="", auth="")
    p = node_mod.new_node("root.c2", "决策", parent="root", context="决策章", batch=4, 验收标准="x", auth="")
    p["round"] = 2
    leaf = node_mod.new_node("root.c2.1", "调研", parent="root.c2",
                             context="调查 CPU 核心数", judge={"chars_min": 5},
                             验收标准="有数字", auth="只读", batch=2)
    leaf["state"] = "失败"
    leaf["failure_class"] = "phantom_citation"
    # 注意：幻影叶在早批次（batch=2），父当前批是 4——守卫必须扫整棵子树，不只当批。
    tree = {"root": root, "root.c2": p, "root.c2.1": leaf}
    def _stub(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        return "{}"
    timed = leafrun.TimedRunner(_stub)
    d = leafrun.TreeDriver(p, tree, TABLE, run_id="t-p2p3",
                           run_dir=str(run_dir), act_runner=timed, timed=timed)
    d.recovery = rec_mod.RecoveryController(str(run_dir))
    knobs = role_mod.effective("决策", None, table=TABLE)
    out = d.spawn_children(p, {"children": [
        {"role": "调研", "context": "调查 CPU 核心数", "验收标准": "有数字", "auth": "只读"}]}, knobs)
    viol = (run_dir / "violations.jsonl").read_text(encoding="utf-8") if (run_dir / "violations.jsonl").is_file() else ""
    check("driver guard drops same-context redispatch",
          out == [] and "root.c2.c2.1" not in d.tree, out)
    check("driver guard logs violation",
          "驱动_同错重派被拒" in viol, viol)
    out2 = d.spawn_children(p, {"children": [
        {"role": "调研", "context": "调查内存容量", "验收标准": "有数字", "auth": "只读"}]}, knobs)
    check("driver guard allows different-context child",
          len(out2) == 1 and out2[0] in d.tree, out2)

print("GATES: %d/%d PASS" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("FAILURES: " + ", ".join(FAIL))
    sys.exit(1)
