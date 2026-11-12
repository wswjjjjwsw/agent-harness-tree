"""V2.2 逻辑树自愈闭环（t-1d3f11824339）。跑法：cd <候选根> && python3 tests/cases-v22selfheal.py
离线：不联网、不打 8092/8903、不起 pi、不调 DeepSeek。
覆盖：schema 未决归一化、执行叶工具、SerialLock 孤儿锁 PID 存活、
机械 merge 兜底恒过 schema、同 role 连续失败预算、transport 路径健壮定位。
"""
import copy, json, os, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import arbitration as arb_mod
from harness import contract as ct
from harness import driver as driver_mod
from harness import leafrun, node as node_mod
from harness import recovery as rec_mod, role as role_mod, schema, session as session_mod

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

TABLE = role_mod.load_roles()

# ---------- A. schema.normalize_undecided ---------- #
out = schema.normalize_undecided([{"type": "冲突", "what": "w", "why": "y"}])
check("A1 normalize preserves valid type", out == [{"type": "冲突", "what": "w", "why": "y"}], out)
out = schema.normalize_undecided([{"type": "BLOCKER 缺信息", "what": "w", "why": "y"}])
check("A2 normalize extracts enum from 'BLOCKER 缺信息'", out[0]["type"] == "缺信息", out)
out = schema.normalize_undecided([{"what": "w", "why": "y"}])
check("A3 normalize missing type defaults to 缺信息", out[0]["type"] == "缺信息", out)
out = schema.normalize_undecided([{"type": "foobar", "what": "w", "why": "y"}])
check("A4 normalize invalid type defaults to 缺信息", out[0]["type"] == "缺信息", out)
out = schema.normalize_undecided([{"type": "缺信息"}])
check("A5 normalize fills missing what/why", out[0]["what"] == "（未注明）" and out[0]["why"] == "叶未决", out)
out = schema.normalize_undecided(["一条未决", 123, None, {"type": "超范围", "what": "w", "why": "y"}])
check("A6 normalize str item + drops non-dict",
      len(out) == 2 and out[0] == {"type": "缺信息", "what": "一条未决", "why": "叶未决"}
      and out[1]["type"] == "超范围", out)
check("A7 normalize does not mutate input",
      (lambda _o: (schema.normalize_undecided(_o), "type" not in _o[0])[1])
      ([{"what": "w", "why": "y"}]), "mutated")

# ---------- B. session.build_argv: 执行叶给工具 ---------- #
def _argv(role, act="leaf"):
    node = {"id": "root.c2.1", "role": role}
    return session_mod.build_argv(node, act, role_mod.effective(role, None, table=TABLE),
                                  "prompt", "sid-1")
a = _argv("执行")
check("B1 exec leaf gets tools incl write",
      "--tools" in a and "read,bash,grep,find,ls,write" in a and "--no-tools" not in a, a)
a = _argv("调研")
check("B2 research leaf keeps read-only tools (no write)",
      "--tools" in a and a[a.index("--tools") + 1] == "read,bash,grep,find,ls", a)
a = _argv("审计")
check("B3 audit leaf stays --no-tools", "--no-tools" in a and "--tools" not in a, a)
a = _argv("根", act="merge")
check("B4 merge act stays --no-tools", "--no-tools" in a and "--tools" not in a, a)

# ---------- R. role 表 tools 旋钮（V2.2，Leo 设计问题回应） ---------- #
check("R1 tools knob exists (13 knobs)", "tools" in role_mod.KNOBS and len(role_mod.KNOBS) == 13,
      role_mod.KNOBS)
check("R2 exec tools include write",
      role_mod.effective("执行", None, table=TABLE).get("tools") == "read,bash,grep,find,ls,write",
      role_mod.effective("执行", None, table=TABLE).get("tools"))
check("R3 research tools are read-only",
      role_mod.effective("调研", None, table=TABLE).get("tools") == "read,bash,grep,find,ls",
      role_mod.effective("调研", None, table=TABLE).get("tools"))
check("R4 parent/audit roles have no tools",
      all(role_mod.effective(r, None, table=TABLE).get("tools") == "" for r in ("根", "规划", "决策", "审计")),
      [role_mod.effective(r, None, table=TABLE).get("tools") for r in ("根", "规划", "决策", "审计")])
check("R5 may_write=true implies tools contain write",
      "write" in role_mod.effective("执行", None, table=TABLE).get("tools", ""),
      "exec tools without write = role contract broken")
_out, _rej = role_mod.apply_overrides(
    role_mod.effective("执行", None, table=TABLE), {"tools": "read"})
check("R6 tools override rejected (capability fixed by role)",
      len(_rej) == 1 and _out.get("tools") == "read,bash,grep,find,ls,write", (_rej, _out.get("tools")))
_no_tools_argv = session_mod.build_argv({"id": "root.c2.1", "role": "执行"}, "leaf",
                                        {"thinking": "none"}, "prompt", "sid-x")
check("R7 build_argv without tools knob fails closed to --no-tools",
      "--no-tools" in _no_tools_argv and "--tools" not in _no_tools_argv, _no_tools_argv)
_custom = dict(role_mod.effective("调研", None, table=TABLE))
_custom["tools"] = "read"
_custom_argv = session_mod.build_argv({"id": "root.c2.1", "role": "调研"}, "leaf",
                                      _custom, "prompt", "sid-y")
check("R8 build_argv reads tools from knobs (not role name)",
      "--tools" in _custom_argv and _custom_argv[_custom_argv.index("--tools") + 1] == "read",
      _custom_argv)

_bad_table = copy.deepcopy(TABLE)
_bad_table["roles"]["调研"]["tools"] = "read,fly"
check("R9 illegal tool name fails role validation",
      any("fly" in e for e in role_mod.validate(_bad_table)),
      role_mod.validate(_bad_table))
_dup_table = copy.deepcopy(TABLE)
_dup_table["roles"]["调研"]["tools"] = "read,read"
check("R10 duplicate tool name fails role validation",
      any("重复" in e for e in role_mod.validate(_dup_table)),
      role_mod.validate(_dup_table))

# ---------- C. SerialLock: 孤儿锁 PID 存活检查 ---------- #
with tempfile.TemporaryDirectory() as tmp:
    lp = str(Path(tmp) / "pi.lock")
    # 死 PID（极大，不可能存在）⇒ 立刻偷锁，不等 1800 秒。
    Path(lp).write_text("999999\n", encoding="utf-8")
    os.utime(lp, (time.time(), time.time()))
    t0 = time.time()
    with session_mod.SerialLock(lp, poll_s=0.05, stale_s=1800.0, max_wait_s=5.0) as lk:
        dt = time.time() - t0
    check("C1 dead-PID orphan lock stolen immediately", lk.stole and dt < 2.0, (lk.stole, dt))
with tempfile.TemporaryDirectory() as tmp:
    lp = str(Path(tmp) / "pi.lock")
    # 活 PID（本进程）⇒ 不偷，等满 max_wait 后超时。
    Path(lp).write_text("%d\n" % os.getpid(), encoding="utf-8")
    os.utime(lp, (time.time(), time.time()))
    raised = None
    try:
        with session_mod.SerialLock(lp, poll_s=0.05, stale_s=1800.0, max_wait_s=0.3):
            pass
    except TimeoutError as exc:
        raised = str(exc)
    check("C2 live-PID lock is not stolen (times out)", raised is not None and "超时" in raised, raised)
with tempfile.TemporaryDirectory() as tmp:
    lp = str(Path(tmp) / "pi.lock")
    # 损坏锁文件（无 PID）+ 旧 mtime ⇒ 回落年龄判断，偷锁。
    Path(lp).write_text("garbage-no-pid\n", encoding="utf-8")
    old = time.time() - 2000.0
    os.utime(lp, (old, old))
    with session_mod.SerialLock(lp, poll_s=0.05, stale_s=1800.0, max_wait_s=5.0) as lk:
        pass
    check("C3 corrupt lock falls back to age (stolen)", lk.stole, lk.stole)

# ---------- D. driver._mechanical_merge: 兜底恒过 schema ---------- #
def _mk_driver(tmp, members, ledger_entries, fix_nodes=None):
    """建一个最小 TreeDriver：parent root(batch=2) + members(batch=2) + 证据台账。"""
    run_dir = Path(tmp) / "run"
    run_dir.mkdir(exist_ok=True)
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None,
                             验收标准="", auth="", batch=2)
    root["round"] = 2
    tree = {"root": root}
    for nid, role, state, fc, batch in members:
        n = node_mod.new_node(nid, role, parent="root", context="ctx-%s" % nid,
                              judge={"chars_min": 5}, 验收标准="x", auth="只读", batch=batch)
        n["state"] = state
        if fc:
            n["failure_class"] = fc
        tree[nid] = n
    for nid, node, rt, unresolved in ledger_entries:
        e = ct.ledger_entry(node, rt, {"calls": 0, "successes": 0, "failures": 0},
                            "sess-%s" % nid, [], unresolved)
        with open(run_dir / ct.LEDGER_NAME, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    for nid, repair_uncovered in (fix_nodes or []):
        n = node_mod.new_node(nid, "调研", parent="root", context="fix",
                              judge={"chars_min": 5}, 验收标准="x", auth="只读", batch=3)
        n["state"] = "完成"
        n["repair_uncovered"] = repair_uncovered
        tree[nid] = n
    def _stub(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        return "{}"
    timed = leafrun.TimedRunner(_stub)
    d = leafrun.TreeDriver(root, tree, TABLE, run_id="t-v22",
                           run_dir=str(run_dir), act_runner=timed, timed=timed)
    return d, root, run_dir

with tempfile.TemporaryDirectory() as tmp:
    # P5 tier1-2 原样：失败叶未决缺 type、部分叶 type 带 BLOCKER 前缀。
    d, root, run_dir = _mk_driver(tmp, [
        ("root.c2.1", "执行", "失败", "bad_citation", 2),
        ("root.c2.2", "审计", "完成", None, 2),
    ], [
        ("root.c2.1", {"id": "root.c2.1"}, "failed",
         [{"what": "CPU 核数未测出", "why": "执行叶形状不合"}]),
        ("root.c2.2", {"id": "root.c2.2"}, "partial",
         [{"type": "BLOCKER 缺信息", "what": "内存数未核验", "why": "审计未过"}]),
    ])
    obj, fail = d._mechanical_merge(root, {})
    ok, why = schema.validate("merge", obj, {}) if obj else (False, fail)
    check("D1 mechanical merge returns schema-valid obj (tier1-2 shape)",
          obj is not None and fail == "" and ok, (fail, why, obj))
    check("D1b all 未决 types are valid enum members",
          obj is not None and all(u.get("type") in schema.UNDECIDED_TYPES for u in obj["未决"]),
          obj.get("未决") if obj else None)
    check("D1c BLOCKER-prefixed type normalized to 缺信息",
          obj is not None and any(u.get("type") == "缺信息" for u in obj["未决"]),
          obj.get("未决") if obj else None)

with tempfile.TemporaryDirectory() as tmp:
    # fix.* 兜底值不提失败节点 id ⇒ 机械收束须自己补齐覆盖，否则覆盖检查拒收。
    d, root, run_dir = _mk_driver(tmp, [
        ("root.c2.1", "执行", "失败", "bad_citation", 2),
        ("root.c2.2", "审计", "完成", None, 2),
    ], [
        ("root.c2.1", {"id": "root.c2.1"}, "failed",
         [{"type": "缺信息", "what": "CPU 核数未测出", "why": "执行叶形状不合"}]),
        ("root.c2.2", {"id": "root.c2.2"}, "partial",
         [{"type": "缺信息", "what": "内存数未核验", "why": "审计未过"}]),
    ], fix_nodes=[("root.fix.1", [{"what": "某无关结论", "why": "修复叶结论"}])])
    obj, fail = d._mechanical_merge(root, {})
    cov_ok, cov_why = d._check_partial_coverage(root, obj) if obj else (False, fail)
    check("D2 mechanical merge covers failed/partial members in uncovered",
          obj is not None and cov_ok, (fail, cov_why, obj.get("uncovered") if obj else None))
    check("D2b coverage entries mention the failed node id",
          obj is not None and any("root.c2.1" in (u.get("what") or "") + (u.get("why") or "")
                                  for u in obj["uncovered"]), obj.get("uncovered") if obj else None)

with tempfile.TemporaryDirectory() as tmp:
    # 全完成、无未决 ⇒ 收束，未决=[]、uncovered=[]，合法。
    d, root, run_dir = _mk_driver(tmp, [
        ("root.c2.1", "调研", "完成", None, 2),
    ], [
        ("root.c2.1", {"id": "root.c2.1"}, "complete", []),
    ])
    obj, fail = d._mechanical_merge(root, {})
    ok, why = schema.validate("merge", obj, {}) if obj else (False, fail)
    check("D3 all-complete mechanical merge converges (empty unresolved)",
          obj is not None and obj.get("去向") == "收束" and obj.get("未决") == []
          and obj.get("uncovered") == [] and ok, (fail, why, obj))

with tempfile.TemporaryDirectory() as tmp:
    # 未全终结 ⇒ 再派，下一批要点 + 未决带合法 type。
    d, root, run_dir = _mk_driver(tmp, [
        ("root.c2.1", "调研", "完成", None, 2),
        ("root.c2.2", "调研", "待跑", None, 2),
    ], [
        ("root.c2.1", {"id": "root.c2.1"}, "complete",
         [{"what": "内存数待复核", "why": "审计未过"}]),
    ])
    obj, fail = d._mechanical_merge(root, {})
    ok, why = schema.validate("merge", obj, {}) if obj else (False, fail)
    check("D4 not-all-terminal mechanical merge re-dispatches (schema-valid)",
          obj is not None and obj.get("去向") == "再派" and ok
          and all(u.get("type") in schema.UNDECIDED_TYPES for u in obj["未决"]),
          (fail, why, obj))

# ---------- E. spawn_children: 同 role 连续失败预算 ---------- #
def _mk_spawn_parent(tmp, failed_roles):
    run_dir = Path(tmp) / "run"
    run_dir.mkdir(exist_ok=True)
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None,
                             验收标准="", auth="", batch=2)
    root["round"] = 2
    tree = {"root": root}
    for i, role in enumerate(failed_roles, start=1):
        nid = "root.c2.%d" % i
        n = node_mod.new_node(nid, role, parent="root", context="ctx-%d" % i,
                              judge={"chars_min": 5}, 验收标准="x", auth="只读", batch=2)
        n["state"] = "失败"
        n["failure_class"] = "bad_citation"
        tree[nid] = n
    def _stub(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        return "{}"
    timed = leafrun.TimedRunner(_stub)
    d = leafrun.TreeDriver(root, tree, TABLE, run_id="t-v22",
                           run_dir=str(run_dir), act_runner=timed, timed=timed)
    d.recovery = rec_mod.RecoveryController(str(run_dir))
    return d, root, run_dir

with tempfile.TemporaryDirectory() as tmp:
    d, root, run_dir = _mk_spawn_parent(tmp, ["执行", "执行"])
    knobs = role_mod.effective("根", None, table=TABLE)
    out = d.spawn_children(root, {"children": [
        {"role": "执行", "context": "重新执行 CPU 核数实测（换个措辞）", "验收标准": "有数字", "auth": "只读"}]}, knobs)
    viol = (run_dir / "violations.jsonl").read_text(encoding="utf-8") if (run_dir / "violations.jsonl").is_file() else ""
    rec = (run_dir / "recovery.jsonl").read_text(encoding="utf-8") if (run_dir / "recovery.jsonl").is_file() else ""
    check("E1 same-role 3rd spawn refused after 2 failures",
          out == [] and "驱动_同类重派被拒" in viol, (out, viol))
    check("E1b refusal recorded in recovery ledger",
          "blocked_same_kind" in rec and "same_kind_redispatch" in rec, rec)
    check("E1c refusal fed back via _last_reject",
          "连续失败" in str((root.get("_last_reject") or {}).get("detail") or ""), root.get("_last_reject"))
    out2 = d.spawn_children(root, {"children": [
        {"role": "调研", "context": "用 sysctl 实测 CPU 核数", "验收标准": "有数字", "auth": "只读"}]}, knobs)
    check("E2 different role still allowed after refusals",
          len(out2) == 1 and out2[0] in d.tree, out2)

with tempfile.TemporaryDirectory() as tmp:
    d, root, run_dir = _mk_spawn_parent(tmp, ["执行"])
    knobs = role_mod.effective("根", None, table=TABLE)
    out = d.spawn_children(root, {"children": [
        {"role": "执行", "context": "重新执行 CPU 核数实测", "验收标准": "有数字", "auth": "只读"}]}, knobs)
    check("E3 one failure does not block 2nd spawn (threshold is 2)",
          len(out) == 1 and out[0] in d.tree, out)

# ---------- F. arbitration transport: 路径健壮定位 ---------- #
with tempfile.TemporaryDirectory() as tmp:
    fake = Path(tmp) / "runtime"
    fake.mkdir()
    (fake / "leo_runtime.py").write_text("# fake\n", encoding="utf-8")
    os.environ["LEO_PROFILE_RUNTIME"] = str(fake)
    try:
        got = arb_mod._find_runtime_dir()
    finally:
        os.environ.pop("LEO_PROFILE_RUNTIME", None)
    check("F1 env LEO_PROFILE_RUNTIME wins", got == str(fake), got)
got = arb_mod._find_runtime_dir()
check("F2 candidate worktree resolves to a dir containing leo_runtime.py",
      Path(got, "leo_runtime.py").is_file(), got)
try:
    sys.path.insert(0, got)
    import leo_runtime as _lr  # noqa: F401
    _import_ok = True
except Exception as exc:  # noqa: BLE001
    _import_ok = False
    _import_err = str(exc)
check("F3 leo_runtime imports from the resolved dir (no ModuleNotFoundError)",
      _import_ok, _import_err if not _import_ok else "")

print("GATES: %d/%d PASS" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("FAILURES: " + ", ".join(FAIL))
    sys.exit(1)
