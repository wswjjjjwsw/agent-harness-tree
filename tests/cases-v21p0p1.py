"""V2.1 阶段一 P0+P1 回归（t-5df0e8dd3660）。跑法：cd <候选根> && python3 tests/cases-v21p0p1.py"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import contract as ct, driver as driver_mod, leafrun, node as node_mod, role as role_mod, trust as tr

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

TABLE = role_mod.load_roles()

# ---------- P0-1) 越权/盘外路径不进可信台账 ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    node = {"id": "root.c2.1", "role": "调研", "reply": "内存 32GB"}
    row = {"doc": "/etc/passwd"}  # 盘外绝对路径
    prods = tr.collect_products(str(run_dir), "root.c2.1", node, row)
    check("trust refuses outside-run_dir doc path",
          all(p["kind"] != "doc" for p in prods), prods)
    check("trust still records reply product",
          any(p["kind"] == "reply" for p in prods), prods)

# ---------- P0-2) 兄弟正常、某叶失败：产物保留；健康与可信程度分开 ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    (run_dir / "doc").mkdir(parents=True)
    (run_dir / "doc" / "root.c2.1.md").write_text("内存实测 32GB。", encoding="utf-8")
    (run_dir / "partial").mkdir(exist_ok=True)
    (run_dir / "partial" / "root.c2.2.json").write_text(
        json.dumps({"successes": 3, "results": ["a", "b", "c"]}, ensure_ascii=False), encoding="utf-8")
    e1 = tr.trust_entry({"id": "root.c2.1", "role": "调研", "state": "完成"},
                        "complete", "完成",
                        tr.collect_products(str(run_dir), "root.c2.1",
                                            {"id": "root.c2.1", "reply": "内存 32GB"},
                                            {"doc": str(run_dir / "doc" / "root.c2.1.md")}),
                        {"calls": 2, "successes": 2, "failures": 0}, "sid1", [], [])
    e1["kind"] = "research"
    tr.append_trust(str(run_dir), e1)
    e2 = tr.trust_entry({"id": "root.c2.2", "role": "调研", "state": "失败"},
                        "partial", "环境",
                        tr.collect_products(str(run_dir), "root.c2.2",
                                            {"id": "root.c2.2", "reply": ""}, {}),
                        {"calls": 5, "successes": 3, "failures": 2}, "sid2", [],
                        [{"type": "缺信息", "what": "超时，已抢救 3 条", "why": "单叶上限"}])
    e2["kind"] = "research"
    tr.append_trust(str(run_dir), e2)
    t = tr.read_trust(str(run_dir))
    check("trust ledger keeps both siblings",
          set(t) == {"root.c2.1", "root.c2.2"}, list(t))
    check("trust failed leaf keeps partial product",
          any(p["kind"] == "partial" for p in t["root.c2.2"]["products"]), t["root.c2.2"])
    check("trust levels split health vs trust",
          tr.trust_level(t["root.c2.1"]) == "已验证" and tr.trust_level(t["root.c2.2"]) == "部分可信",
          (tr.trust_level(t["root.c2.1"]), tr.trust_level(t["root.c2.2"])))
    out = tr.gen_guaranteed_delivery({"root": {"state": "失败"}}, str(run_dir), "t")
    txt = Path(out).read_text(encoding="utf-8")
    check("guaranteed delivery lists doc product with sha",
          "doc/root.c2.1.md" in txt and "sha256=" in txt, txt[:400])
    check("guaranteed delivery keeps failed-leaf partial",
          "partial/root.c2.2.json" in txt, txt[:400])
    check("guaranteed delivery has health table",
          "执行健康" in txt and "root.c2.2" in txt and "失败" in txt, txt[:400])
    check("guaranteed delivery carries no conclusion claim",
          "不含模型结论" in txt and "零可信成果" not in txt, txt[:400])

# ---------- P0-3) 零可信成果 ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    out = tr.gen_guaranteed_delivery({"root": {"state": "失败"}}, str(run_dir), "t")
    txt = Path(out).read_text(encoding="utf-8")
    check("zero trusted results says so explicitly",
          tr.ZERO_TRUST_LINE in txt and "不得据此声称任何结论" in txt, txt[:300])

# ---------- P1-1) 父看得到来源正文（batch_view 摘录 + 预算） ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    (run_dir / "doc").mkdir(parents=True)
    (run_dir / "doc" / "root.c2.1.md").write_text("内存实测：34359738368 字节。", encoding="utf-8")
    (run_dir / "doc" / "root.c2.2.md").write_text("x" * 5000, encoding="utf-8")
    parent = node_mod.new_node("root", "根", parent="", context="x", judge=None, 验收标准="", auth="")
    parent["_run_dir"] = str(run_dir)
    parent["batch"] = 2
    tree = {"root": parent,
            "root.c2.1": node_mod.new_node("root.c2.1", "调研", parent="root", context="查内存",
                                           judge={"chars_min": 5}, 验收标准="有数字", auth="只读", batch=2),
            "root.c2.2": node_mod.new_node("root.c2.2", "调研", parent="root", context="查 CPU",
                                           judge={"chars_min": 5}, 验收标准="有数字", auth="只读", batch=2)}
    tree["root.c2.1"]["reply"] = "内存 32GB"
    tree["root.c2.1"]["state"] = "完成"
    tree["root.c2.2"]["reply"] = "CPU 8 核"
    tree["root.c2.2"]["state"] = "完成"
    bv = json.loads(driver_mod.batch_view(parent, tree))
    rows = {r["id"]: r for r in bv["子节点"]}
    check("batch_view carries doc excerpt with source",
          "34359738368" in rows["root.c2.1"]["doc摘录"], rows["root.c2.1"]["doc摘录"][:100])
    check("batch_view truncates long doc with locator",
          "截断" in rows["root.c2.2"]["doc摘录"] and "doc/root.c2.2.md" in rows["root.c2.2"]["doc摘录"],
          rows["root.c2.2"]["doc摘录"][-120:])
    check("batch_view total budget respected",
          sum(len(r["doc摘录"]) for r in bv["子节点"]) <= 6200,
          sum(len(r["doc摘录"]) for r in bv["子节点"]))

# ---------- P1-2) merge 缺字段机械补全 ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    ct.append_ledger(str(run_dir), ct.ledger_entry(
        {"id": "root.c2.2", "role": "调研", "state": "完成"}, "partial",
        {"calls": 3, "successes": 2, "failures": 1}, "sid",
        [], [{"type": "缺信息", "what": "Qwen 加载耗时未测", "why": "超时"},
             {"type": "缺信息", "what": "中断延迟未测", "why": "超时"}]))
    parent = node_mod.new_node("root", "根", parent="", context="x", judge=None, 验收标准="", auth="")
    parent["batch"] = 2
    tree = {"root": parent,
            "root.c2.1": node_mod.new_node("root.c2.1", "调研", parent="root", context="a",
                                           judge={"chars_min": 5}, 验收标准="有数字", auth="只读", batch=2),
            "root.c2.2": node_mod.new_node("root.c2.2", "调研", parent="root", context="b",
                                           judge={"chars_min": 5}, 验收标准="有数字", auth="只读", batch=2)}
    d = driver_mod.Driver(parent, tree, TABLE, run_id="t", run_dir=str(run_dir),
                          act_runner=lambda *a, **k: "{}")
    obj = {"去向": "再派", "reply": "需要下一批", "未决": []}
    rep = d._repair_merge_fields(parent, obj)
    check("mechanical repair fills 下一批要点 from ledger unresolved",
          "下一批要点" in rep and len(obj["下一批要点"]) == 2
          and "Qwen 加载耗时未测" in obj["下一批要点"][0], obj)
    tree["root.fix.1"] = node_mod.new_node("root.fix.1", "调研", parent="root", context="修",
                                           judge={"chars_min": 5}, 验收标准="x", auth="只读", batch=2)
    tree["root.fix.1"]["state"] = "完成"
    tree["root.fix.1"]["repair_uncovered"] = [{"what": "Qwen 窗口未验证", "why": "无实测数据"}]
    obj2 = {"去向": "收束", "reply": "收束", "未决": []}
    rep2 = d._repair_merge_fields(parent, obj2)
    check("mechanical repair fills uncovered from fix leaf",
          rep2.get("uncovered") == "root.fix.1"
          and obj2["uncovered"] == [{"what": "Qwen 窗口未验证", "why": "无实测数据"}], obj2)
with tempfile.TemporaryDirectory() as tmp:
    # 无可补来源（空台账、无 fix 叶）⇒ 不填
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    parent = node_mod.new_node("root", "根", parent="", context="x", judge=None, 验收标准="", auth="")
    parent["batch"] = 2
    tree = {"root": parent}
    d = driver_mod.Driver(parent, tree, TABLE, run_id="t", run_dir=str(run_dir),
                          act_runner=lambda *a, **k: "{}")
    obj3 = {"去向": "再派", "reply": "需要下一批", "未决": []}
    rep3 = d._repair_merge_fields(parent, obj3)
    check("mechanical repair fills nothing without verified source",
          rep3 == {} and "下一批要点" not in obj3, rep3)

# ---------- P1-3) E16：拒因回馈 + 成功清空 + spawn 带 plan 约束 ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    parent = node_mod.new_node("root", "根", parent="", context="x", judge=None, 验收标准="", auth="")
    tree = {"root": parent}
    d = driver_mod.Driver(parent, tree, TABLE, run_id="t", run_dir=str(run_dir),
                          act_runner=lambda *a, **k: "{}")
    row = {"act": "spawn", "round": 2}
    d._reject(parent, row, "children[0] 缺 'auth'", kind="驱动_产出形状")
    check("E16 _reject records _last_reject",
          parent.get("_last_reject", {}).get("detail") == "children[0] 缺 'auth'", parent.get("_last_reject"))
    parent["result"] = json.dumps(["查内存", "查 CPU"], ensure_ascii=False)
    prompt = driver_mod.build_prompt(parent, "spawn", {}, TABLE, first_round=False)
    check("E16 next prompt carries rejection reason",
          "上一轮被拒" in prompt and "缺 'auth'" in prompt, prompt[-300:])
    check("spawn prompt carries plan constraint count",
          "要点共 2 条" in prompt, prompt[-300:])

# ---------- P1-4) 指纹重复 ⇒ 机械 merge，不调第三次模型 ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    sess_dir = Path(tmp) / "sess"
    sess_dir.mkdir()
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(sess_dir)
    os.environ["TREE_RUNS_DIR"] = str(Path(tmp))
    calls = {"merge": 0}
    bad_merge = json.dumps({"去向": "收束", "reply": "收束了", "未决": []}, ensure_ascii=False)
    def _scripted(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        nid = str(node.get("id") or "")
        if act == "plan":
            return json.dumps({"要点": ["查内存"]}, ensure_ascii=False)
        if act == "spawn":
            return json.dumps({"children": [
                {"role": "调研", "context": "查本机内存实测", "judge": {"chars_min": 5},
                 "验收标准": "有数字", "auth": "只读"}]}, ensure_ascii=False)
        if act == "merge":
            calls["merge"] += 1
            return bad_merge  # 两次逐字相同：缺 uncovered
        if ".fix." in nid:
            return json.dumps({"reply": "据 root.c2.1 的证据，未发现缺口",
                               "未决": [], "uncovered": []}, ensure_ascii=False)
        if ".diag." in nid:
            return json.dumps({"reply": "根因：merge 模板缺 下一批要点 提示，已换策略",
                               "未决": []}, ensure_ascii=False)
        return json.dumps({"reply": "内存 32GB，实测", "未决": []}, ensure_ascii=False)
    (sess_dir / "z_tree-t-fp-root.c2.1.jsonl").write_text(
        '{"message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "sysctl hw.memsize"}}]}}\n'
        '{"message": {"role": "toolResult", "content": "hw.memsize: 34359738368"}}\n', encoding="utf-8")
    sch = leafrun.Scheduler(str(run_dir), "t-fp", TABLE, "研究题：内存",
                            max_steps=40, max_seconds=120, max_reject=10,
                            act_runner=_scripted)
    sch.build_root()
    res = sch.run()
    tree = sch.tree
    rounds = [json.loads(x) for x in (Path(run_dir) / "rounds.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    mech_rounds = [r for r in rounds if r.get("runner") == "mechanical"]
    check("fingerprint repeat caps model merge calls at 2",
          calls["merge"] == 2, calls)
    check("third merge goes mechanical",
          len(mech_rounds) >= 1, [r.get("runner") for r in rounds])
    check("mechanical merge converges root",
          tree.get("root", {}).get("state") == "完成", tree.get("root", {}).get("state"))
    check("mechanical merge uncovered is explicit empty",
          (tree.get("root", {}).get("merge") or {}).get("uncovered") == [],
          (tree.get("root", {}).get("merge") or {}))
    _trust = tr.read_trust(str(run_dir))
    check("trust entry health.state is terminal not 在跑 (Tier1 bug)",
          _trust.get("root.c2.1", {}).get("health", {}).get("state") == "完成",
          _trust.get("root.c2.1", {}).get("health"))
    check("guaranteed delivery written on scripted run",
          (Path(run_dir) / "out" / tr.DELIVERY_NAME).is_file()
          and "root.c2.1" in (Path(run_dir) / "out" / tr.DELIVERY_NAME).read_text(encoding="utf-8"),
          "")

print("\nV2.1 P0+P1: %d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
    sys.exit(1)
