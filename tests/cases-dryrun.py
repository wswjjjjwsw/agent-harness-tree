"""干跑脚手架 + 逐叶进度 + 增量审计回归（t-f6f9d7325d93）。跑法：cd <候选根> && python3 tests/cases-dryrun.py"""
import json, os, sqlite3, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import contract as ct, driver as driver_mod, evidence as ev, leafrun, node as node_mod, progress as pg, role as role_mod, session as session_mod

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

TABLE = role_mod.load_roles()

# ---------- 1) 叶超时 profile ----------
check("timeout dryrun research=90", ct.leaf_timeout_for("research", dryrun=True) == 90.0)
check("timeout dryrun repair=90", ct.leaf_timeout_for("contract_repair", dryrun=True) == 90.0)
check("timeout light research=90", ct.leaf_timeout_for("research", work_mode="light") == 90.0)
check("timeout standard research=300", ct.leaf_timeout_for("research", work_mode="standard") == 300.0)
check("timeout standard repair=180", ct.leaf_timeout_for("contract_repair", work_mode="standard") == 180.0)
check("timeout deep research=600", ct.leaf_timeout_for("research", work_mode="deep") == 600.0)
check("timeout default research=300", ct.leaf_timeout_for("research") == 300.0)
check("timeout unknown work_mode falls back to kind", ct.leaf_timeout_for("research", work_mode="nope") == 300.0)
_old = os.environ.get("TREE_DRYRUN")
os.environ["TREE_DRYRUN"] = "1"
check("is_dryrun env on", ct.is_dryrun() is True)
if _old is None:
    os.environ.pop("TREE_DRYRUN", None)
else:
    os.environ["TREE_DRYRUN"] = _old
check("is_dryrun env off", ct.is_dryrun() is False)

# ---------- 2) 失败分类 timeout 放宽 ----------
check("failure timeout by bare 超时",
      ct.classify_failure("超时", "pi 到单叶上限：超时", {}) == "timeout")
check("failure timeout by TimeoutExpired",
      ct.classify_failure("环境", "subprocess.TimeoutExpired", {}) == "timeout")

# ---------- 3) 诊断策略 kind=synthesis ----------
from harness import recovery as rec_mod
with tempfile.TemporaryDirectory() as tmp:
    c = rec_mod.RecoveryController(tmp)
    s1 = c.choose("root", "root.fix.1", "merge_missing_field", "缺必填键 '下一批要点'", None)
    check("recovery merge 1st is contract_repair",
          s1 is not None and s1.get("kind") == "contract_repair", s1)
    s2 = c.choose("root", "root.fix.1", "merge_missing_field", "缺必填键 '下一批要点'", None)
    check("recovery merge 2nd is synthesis tools none",
          s2 is not None and s2.get("kind") == "synthesis" and s2.get("tools") == "none", s2)

# ---------- 4) 砍空叶 ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    parent = node_mod.new_node("root", "根", parent="", context="x", judge=None,
                               验收标准="", auth="")
    tree = {"root": parent}
    knobs = dict(role_mod.effective("根", "spawn", table=TABLE))
    d = driver_mod.Driver(parent, tree, TABLE, run_id="t", run_dir=str(run_dir),
                          act_runner=lambda *a, **k: "{}")
    good = {"role": "调研", "context": "查内存", "judge": {"chars_min": 10},
            "验收标准": "给出字节数", "auth": ""}
    bad_no_ctx = {"role": "调研", "context": "", "judge": {"chars_min": 10},
                  "验收标准": "给出字节数", "auth": ""}
    bad_no_criteria = {"role": "调研", "context": "查内存", "judge": None,
                       "验收标准": "", "auth": ""}
    out = d.spawn_children(parent, {"children": [good, bad_no_ctx, bad_no_criteria]}, knobs)
    check("empty-leaf drops 2 keeps 1", len(out) == 1 and out[0] == "root.c0.1", out)
    check("empty-leaf result counts", json.loads(parent.get("result") or "{}").get("空叶拒绝") == 2,
          parent.get("result"))
    ret = d.apply_output(parent, "spawn",
                         {"children": [dict(bad_no_ctx), dict(bad_no_criteria)]}, knobs)
    check("spawn all-empty rejected", isinstance(ret, str) and "无有效子节点" in ret, ret)

# ---------- 5) 诊断子节点 kind=synthesis ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    sch = leafrun.Scheduler(str(run_dir), "t", TABLE, "ctx")
    sch.build_root()
    root = sch.tree["root"]
    root["batch"] = 1
    root["_merge_reject_notes"] = ["缺必填键 'uncovered'"]
    cid = sch._spawn_diagnosis_child(root, "uncovered", "缺必填键 'uncovered'")
    dn = sch.tree[cid]
    check("diagnosis id is diag.N", cid == "root.diag.1", cid)
    check("diagnosis kind synthesis", ct.classify(dn) == "synthesis", dn.get("overrides"))
    check("diagnosis role is 调研 (may_write=false, no doc required)", dn.get("role") == "调研", dn.get("role"))
    check("diagnosis cites reject notes", "uncovered" in (dn.get("context") or ""), dn.get("context", "")[:80])

# ---------- 6) 进度事件 + bridge 写入（临时库） ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    db = str(Path(tmp) / "bridge.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT, progress TEXT, heartbeat_at TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('t-test', 'running', NULL, NULL)")
    conn.commit(); conn.close()
    os.environ["TREE_BRIDGE_TASK_ID"] = "t-test"
    os.environ["TREE_BRIDGE_DB"] = db
    node = node_mod.new_node("root.c2.1", "调研", parent="root", context="x",
                             judge={"chars_min": 10}, 验收标准="x", auth="")
    node["state"] = "完成"
    row = {"result_type": "complete", "start": 1.0, "end": 2.0, "seconds": 1.0}
    ev_out = pg.emit_leaf_progress(str(run_dir), "t", node, row, "tree-t-root.c2.1")
    lines = (run_dir / "progress.jsonl").read_text(encoding="utf-8").strip().splitlines()
    check("progress.jsonl one leaf_terminal event",
          len(lines) == 1 and json.loads(lines[0]).get("event") == "leaf_terminal", lines)
    check("progress event carries node+seconds",
          json.loads(lines[0]).get("node_id") == "root.c2.1"
          and json.loads(lines[0]).get("seconds") == 1.0, lines)
    conn = sqlite3.connect(db)
    prow = conn.execute("SELECT progress, heartbeat_at FROM tasks WHERE task_id='t-test'").fetchone()
    conn.close()
    check("bridge progress written", prow[0] is not None and json.loads(prow[0]).get("last_leaf", {}).get("node_id") == "root.c2.1", prow)
    check("bridge heartbeat refreshed", prow[1] is not None, prow)
    os.environ.pop("TREE_BRIDGE_TASK_ID", None)
    os.environ.pop("TREE_BRIDGE_DB", None)
    check("progress without bridge env still lands jsonl",
          pg.emit_leaf_progress(str(run_dir), "t", node, row, "tree-t-root.c2.1") is not None
          and len((run_dir / "progress.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 2)

# ---------- 7) 增量审计排序（双桩叶调度回归） ----------
with tempfile.TemporaryDirectory() as tmp:
    stub = Path(tmp) / "stub2.py"
    _stub_lines = [
        '#!/usr/bin/env python3',
        'import json, os, sys',
        'act = sys.argv[1]; nid = sys.argv[2]; rnd = int(sys.argv[3])',
        'sys.stdin.read()',
        'if act == "plan":',
        '    print(json.dumps({"要点": ["查内存"]}, ensure_ascii=False))',
        'elif act == "spawn":',
        '    print(json.dumps({"children": [',
        '        {"role": "调研", "context": "叶1", "judge": {"chars_min": 5}, "验收标准": "给出事实", "auth": "只读本机"},',
        '        {"role": "调研", "context": "叶2", "judge": {"chars_min": 5}, "验收标准": "给出事实", "auth": "只读本机"}]}, ensure_ascii=False))',
        'elif act == "merge":',
        '    print(json.dumps({"去向": "收束", "reply": "收束", "未决": [], "uncovered": []}, ensure_ascii=False))',
        'else:',
        '    print(json.dumps({"reply": "桩叶完成", "未决": []}, ensure_ascii=False))',
    ]
    stub.write_text("\n".join(_stub_lines) + "\n", encoding="utf-8")
    stub.chmod(0o755)
    os.environ["TREE_ACT_CMD"] = str(stub)
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(Path(tmp) / "sess")
    Path(tmp, "sess").mkdir(exist_ok=True)
    run_dir = Path(tmp) / "run"
    sch = leafrun.Scheduler(str(run_dir), "t", TABLE, "ctx",
                            max_steps=20, max_seconds=600.0)
    sch.build_root()
    res = sch.run()
    rows = leafrun.read_rounds(str(run_dir))
    acts = [(r.get("node_id"), r.get("act")) for r in rows]
    leaf1_i = next((i for i, (n, a) in enumerate(acts) if n == "root.c2.1" and a == "leaf"), -1)
    audit_i = next((i for i, (n, a) in enumerate(acts) if n == "root.c2.1.audit"), -1)
    leaf2_i = next((i for i, (n, a) in enumerate(acts) if n == "root.c2.2" and a == "leaf"), -1)
    check("B audit queued after leaf1 before leaf2",
          leaf1_i >= 0 and audit_i > leaf1_i and leaf2_i > audit_i,
          "order=%s" % (acts,))
    prog = [json.loads(l) for l in (run_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    check("B progress has leaf_terminal for c2.1",
          any(p.get("node_id") == "root.c2.1" and p.get("event") == "leaf_terminal" for p in prog), prog)
    aud = [json.loads(l) for l in (run_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] if (run_dir / "audit.jsonl").is_file() else []
    check("B audit.jsonl has per-leaf audit row",
          any(a.get("检查点id") == "root.c2.1.audit" for a in aud), aud)
    os.environ.pop("TREE_ACT_CMD", None)
    os.environ.pop("TREE_PI_AGENT_SESSION_DIR", None)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
