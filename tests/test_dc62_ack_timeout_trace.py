"""t-dc62df942275 定点修复测试（逻辑树 V2 ACK/超时 trace 补丁）。

跑法：cd <候选根> && python3 tests/test_dc62_ack_timeout_trace.py
离线：不联网、不打 8092/8903、不起 pi、不调 DeepSeek、不调真实 claude。
故障注入用假 CLI 脚本（写 stream-json 事件后 sleep，等超时杀掉），绝不用
真实 claude/模型做注入测试。

覆盖（按任务项）：
  项 1（verify-only）：无终态 ACK 但 Write 已发生 ⇒ partial/verified（T1a/T1b）。
  项 2（核心）：流式读 + 工具事件增量落盘；超时 meta 从 events 文件重建；
        0 事件如实记“观测到 0 个事件”；半截 JSON 行记 lost；超大输出不丢事件；
        超时 why 标签 TREE_CC_TIMEOUT（T2a–T2f）。
  项 2 补（F1）：工具事件落盘 flush 后 fsync（T2g；OS 崩溃不丢事件）。
  项 3：修复叶 context 模板化——模板含绝对路径/失败测试/复测命令原文；
        spawn 落盘的修复叶 context 即模板；stub 干跑不再跑错相对测试路径（T3a–T3c）。
  项 4（verify-only）：同类重派预算拒收时 apply_output 返回真实拒因，
        经 _reject 覆写后 _last_reject 仍是真实拒因（T4a）。
  项 5：无进展重复调用熔断——连续 3 次相同调用触发 loop，有变化重查放行，
        等价参数折叠空白（T5a–T5c）。
  项 6（verify-only）：role.yaml doc>=1500 保留 pi；调研 800 字判据仍在；
        spawn 五字段仍必填；执行-ack.md 无 1500 字负担（T6a–T6d）。
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import cc_run, contract as ct, driver as driver_mod
from harness import leafrun, node as node_mod, role as role_mod, schema
from harness import submit as submit_mod

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:400]) if extra and not cond else ""))

TABLE = role_mod.load_roles()
KNOBS_EXEC = role_mod.effective("执行", None, table=TABLE)
KNOBS_ROOT = role_mod.effective("根", None, table=TABLE)

# ---------- 假 CLI：按 FAKE_CC_MODE 发 stream-json 事件（绝不用真实 claude） ---------- #
FAKE_CLI = '''#!/usr/bin/env python3
"""Fake claude -p: emit stream-json tool events per FAKE_CC_MODE, then sleep/exit."""
import json, os, sys, time

def ev(tool, inp):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": tool, "input": inp}],
        "usage": {"input_tokens": 10, "output_tokens": 5}}}

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
    sys.stdout.flush()

mode = os.environ.get("FAKE_CC_MODE", "ok")
if mode == "timeout3":
    emit(ev("Read", {"file_path": "/tmp/full-tree/spec.md"}))
    emit(ev("Write", {"file_path": "project/store.py"}))
    emit(ev("Bash", {"command": "python3 -m py_compile project/store.py"}))
    time.sleep(30)
elif mode == "timeout0":
    time.sleep(30)
elif mode == "timeout_trunc":
    emit(ev("Write", {"file_path": "project/store.py"}))
    sys.stdout.write('{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "project/sto')
    sys.stdout.flush()
    time.sleep(30)
elif mode == "huge":
    for i in range(50):
        emit(ev("Read", {"file_path": "project/f%02d.py" % i}))
    emit({"type": "result", "result": "{\\"reply\\": \\"ok\\", \\"未决\\": []}", "is_error": False, "num_turns": 3, "duration_ms": 10})
elif mode == "loop":
    for _ in range(3):
        emit(ev("Bash", {"command": "ls"}))
    time.sleep(30)
elif mode == "varied":
    emit(ev("Bash", {"command": "ls /a"}))
    emit(ev("Bash", {"command": "ls /b"}))
    emit(ev("Bash", {"command": "ls /a"}))
    emit({"type": "result", "result": "{\\"reply\\": \\"ok\\", \\"未决\\": []}", "is_error": False, "num_turns": 2, "duration_ms": 10})
else:  # ok
    emit({"type": "system", "subtype": "init", "session_id": "fake"})
    emit(ev("Write", {"file_path": "project/store.py"}))
    emit({"type": "result", "result": "{\\"reply\\": \\"ok\\", \\"未决\\": []}", "is_error": False, "num_turns": 1, "duration_ms": 10})
'''


def _fake_run_dir(tmp, mode):
    """建一个 run_cc_node 可跑的 run 目录：agent 文件 + 假 CLI，monkeypatch 接线。"""
    run_dir = Path(tmp) / "run"
    (run_dir / ".claude" / "agents").mkdir(parents=True)
    (run_dir / ".claude" / "agents" / cc_run.cc_agents.agent_filename("执行")).write_text(
        "---\nname: tree-zhixing\ntools: Read, Write, Bash\n---\n执行 agent（测试桩）\n", encoding="utf-8")
    cli = Path(tmp) / "fake-claude"
    cli.write_text(FAKE_CLI, encoding="utf-8")
    cli.chmod(0o755)
    old_find, old_env = cc_run._find_cc, cc_run._cc_env
    cc_run._find_cc = lambda: str(cli)
    cc_run._cc_env = lambda: dict(os.environ)
    old_mode = os.environ.get("FAKE_CC_MODE")
    os.environ["FAKE_CC_MODE"] = mode
    def _restore():
        cc_run._find_cc, cc_run._cc_env = old_find, old_env
        if old_mode is None:
            os.environ.pop("FAKE_CC_MODE", None)
        else:
            os.environ["FAKE_CC_MODE"] = old_mode
    return run_dir, _restore


def _read_meta(run_dir, sid="cc-node"):
    p = Path(run_dir) / ("cc-%s.meta.json" % sid)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _read_events(run_dir, sid="cc-node"):
    p = Path(run_dir) / ("cc-%s.tool-events.jsonl" % sid)
    if not p.is_file():
        return None
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

# ---------- 项 2：流式读 + 增量落盘（故障注入） ---------- #
with tempfile.TemporaryDirectory() as tmp:
    run_dir, restore = _fake_run_dir(tmp, "timeout3")
    try:
        try:
            cc_run.run_cc_node("执行", "写 store.py", str(run_dir), knobs=KNOBS_EXEC,
                               timeout=2.0, session_id="cc-node")
            check("T2a timeout raises TimeoutExpired", False, "no raise")
        except subprocess.TimeoutExpired:
            check("T2a timeout raises TimeoutExpired", True)
        meta = _read_meta(run_dir)
        evs = _read_events(run_dir)
        check("T2a timeout meta keeps 3 tool calls (streamed, not [])",
              [c["name"] for c in meta.get("tool_calls", [])] == ["Read", "Write", "Bash"], meta.get("tool_calls"))
        check("T2a timeout meta events_observed=3 events_lost=0 stream_complete=False",
              meta.get("events_observed") == 3 and meta.get("events_lost") == 0
              and meta.get("stream_complete") is False, meta)
        check("T2a tool-events JSONL has 3 flushed lines",
              isinstance(evs, list) and len(evs) == 3, evs)
        check("T2a timeout meta timeout=true deadline=2.0",
              meta.get("timeout") is True and meta.get("deadline_s") == 2.0, meta)
    finally:
        restore()

with tempfile.TemporaryDirectory() as tmp:
    run_dir, restore = _fake_run_dir(tmp, "timeout0")
    try:
        try:
            cc_run.run_cc_node("执行", "写 store.py", str(run_dir), knobs=KNOBS_EXEC,
                               timeout=2.0, session_id="cc-node")
            check("T2b zero-event timeout raises", False, "no raise")
        except subprocess.TimeoutExpired:
            check("T2b zero-event timeout raises", True)
        meta = _read_meta(run_dir)
        check("T2b zero events honestly recorded (tool_calls [] + observed 0)",
              meta.get("tool_calls") == [] and meta.get("events_observed") == 0, meta)
        check("T2b zero-event note says observed-0, never claims 0 real calls",
              "观测到 0 个工具事件" in str(meta.get("events_note") or "")
              and "不等于实际调用次数" in str(meta.get("events_note") or ""), meta.get("events_note"))
    finally:
        restore()

with tempfile.TemporaryDirectory() as tmp:
    run_dir, restore = _fake_run_dir(tmp, "timeout_trunc")
    try:
        try:
            cc_run.run_cc_node("执行", "写 store.py", str(run_dir), knobs=KNOBS_EXEC,
                               timeout=2.0, session_id="cc-node")
        except subprocess.TimeoutExpired:
            pass
        meta = _read_meta(run_dir)
        check("T2c truncated line: valid event kept, partial line counted lost",
              [c["name"] for c in meta.get("tool_calls", [])] == ["Write"]
              and meta.get("events_observed") == 1 and meta.get("events_lost", 0) >= 1, meta)
    finally:
        restore()

with tempfile.TemporaryDirectory() as tmp:
    run_dir, restore = _fake_run_dir(tmp, "huge")
    try:
        try:
            cc_run.run_cc_node("执行", "读 50 个文件", str(run_dir), knobs=KNOBS_EXEC,
                               timeout=30.0, session_id="cc-node")
            check("T2d huge output still hits MAX_TOOL_CALLS guard", False, "no raise")
        except RuntimeError as exc:
            check("T2d huge output still hits MAX_TOOL_CALLS guard",
                  "工具调用超限" in str(exc), str(exc))
        meta = _read_meta(run_dir)
        evs = _read_events(run_dir)
        check("T2d huge output: all 50 events persisted (meta + JSONL)",
              len(meta.get("tool_calls", [])) == 50 and isinstance(evs, list) and len(evs) == 50,
              (len(meta.get("tool_calls", [])), len(evs or [])))
        check("T2d huge output meta stream_complete=True",
              meta.get("stream_complete") is True, meta)
    finally:
        restore()

with tempfile.TemporaryDirectory() as tmp:
    run_dir, restore = _fake_run_dir(tmp, "ok")
    try:
        res = cc_run.run_cc_node("执行", "写 store.py", str(run_dir), knobs=KNOBS_EXEC,
                                 timeout=30.0, session_id="cc-node")
        meta = _read_meta(run_dir)
        check("T2e streaming success path still returns result text",
              res.get("ok") is True and "ok" in res.get("text", ""), res)
        check("T2e success meta events_observed=1 stream_complete=True",
              meta.get("events_observed") == 1 and meta.get("stream_complete") is True, meta)
    finally:
        restore()

# ---------- 项 2：超时 why 标签（TREE_CC_TIMEOUT vs TREE_PI_TIMEOUT） ---------- #
def _run_leaf_timeout(tmp, is_cc):
    def _to(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        raise subprocess.TimeoutExpired(["claude", "-p"], 300)
    _to.is_cc = is_cc
    run_dir = Path(tmp) / "run"
    run_dir.mkdir(exist_ok=True)
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None,
                             验收标准="x", auth="只读", batch=2)
    tree = {"root": root}
    d = driver_mod.Driver(parent=root, tree=tree, table=TABLE, run_id="t-dc62",
                          run_dir=str(run_dir), act_runner=_to)
    leaf = node_mod.new_node("root.c2.1", "执行", parent="root",
                             context="写 project/store.py", judge={"json_keys": ["reply"]},
                             验收标准="store.py 可 import", auth="只读", batch=2)
    tree["root.c2.1"] = leaf
    return leafrun.run_leaf(d, leaf)

with tempfile.TemporaryDirectory() as tmp:
    row = _run_leaf_timeout(tmp, True)
    check("T2f CC timeout why says TREE_CC_TIMEOUT, not TREE_PI_TIMEOUT",
          "TREE_CC_TIMEOUT" in str(row.get("why") or "")
          and "TREE_PI_TIMEOUT" not in str(row.get("why") or ""), row.get("why"))
with tempfile.TemporaryDirectory() as tmp:
    row = _run_leaf_timeout(tmp, False)
    check("T2f pi timeout why still says TREE_PI_TIMEOUT",
          "TREE_PI_TIMEOUT" in str(row.get("why") or ""), row.get("why"))

# ---------- 项 2 补（F1）：工具事件落盘 flush 后 fsync（OS 崩溃不丢） ---------- #
class _FakeEventsFh:
    """只记调用顺序的假 events 文件对象（不碰磁盘；fileno 只在 fsync 路径上被调）。"""
    def __init__(self):
        self.calls = []
        self.buf = []
    def write(self, s):
        self.calls.append("write")
        self.buf.append(s)
        return len(s)
    def flush(self):
        self.calls.append("flush")
    def fileno(self):
        self.calls.append("fileno")
        return 7

def _tool_use_line(tool="Read", inp=None):
    return (json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": tool, "input": inp or {"file_path": "/tmp/x.md"}}],
        "usage": {"input_tokens": 10, "output_tokens": 5}}}, ensure_ascii=False) + "\n").encode("utf-8")

_fsync_seen = []
_real_fsync = os.fsync
def _rec_fsync(fd):
    _fsync_seen.append(fd)
try:
    os.fsync = _rec_fsync
    _fh = _FakeEventsFh()
    cc_run._feed_stream_line(_tool_use_line(), [], {}, _fh)
    check("T2g fsync called once per flushed tool event",
          _fsync_seen == [7] and _fh.calls.count("write") == 1, _fsync_seen)
    check("T2g write->flush->fsync order (fileno only reached via fsync)",
          _fh.calls == ["write", "flush", "fileno"], _fh.calls)
    check("T2g event still landed in fh buffer",
          len(_fh.buf) == 1 and "Read" in _fh.buf[0], _fh.buf)
    # fsync 失败也不炸流式读：沿用同一 try 的吞掉口径
    def _boom_fsync(fd):
        raise OSError("disk gone")
    os.fsync = _boom_fsync
    _fh2 = _FakeEventsFh()
    cc_run._feed_stream_line(_tool_use_line("Write", {"file_path": "project/store.py"}), [], {}, _fh2)
    check("T2g fsync failure does not break streaming (swallowed)",
          len(_fh2.buf) == 1 and "Write" in _fh2.buf[0], _fh2.buf)
finally:
    os.fsync = _real_fsync

with tempfile.TemporaryDirectory() as tmp:
    _p = Path(tmp) / "events.jsonl"
    with open(_p, "w", encoding="utf-8") as _fh3:
        cc_run._feed_stream_line(_tool_use_line("Bash", {"command": "true"}), [], {}, _fh3)
    check("T2g real file keeps the event after feed (flush+fsync)",
          "Bash" in _p.read_text(encoding="utf-8"), _p.read_text(encoding="utf-8"))

# ---------- 项 1（verify-only）：无终态 ACK 但 Write 已发生 ⇒ partial/verified ---------- #
def _run_leaf_write_then_timeout(tmp, verify_cmd):
    """stub 写出 project/store.py 后超时；verify 命令决定 partial 还是 verified。"""
    run_dir = Path(tmp) / "run"
    (run_dir / "project").mkdir(parents=True)
    def _stub(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        (Path(run_dir) / "project" / "store.py").write_text(
            "def get_store():\n    return {}\n", encoding="utf-8")
        raise subprocess.TimeoutExpired(["claude", "-p"], 300)
    _stub.is_cc = True
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None,
                             验收标准="x", auth="只读", batch=2)
    tree = {"root": root}
    d = driver_mod.Driver(parent=root, tree=tree, table=TABLE, run_id="t-dc62",
                          run_dir=str(run_dir), act_runner=_stub)
    leaf = node_mod.new_node("root.c2.1", "执行", parent="root",
                             context="写 project/store.py", judge={"json_keys": ["reply"]},
                             验收标准="store.py 可 import", auth="只读", batch=2,
                             overrides={"artifacts_dir": "project",
                                        "verify_cmd": verify_cmd,
                                        "expect_artifacts": True})
    tree["root.c2.1"] = leaf
    return leafrun.run_leaf(d, leaf), leaf

with tempfile.TemporaryDirectory() as tmp:
    cmd_ok = sys.executable + " -c \"print('PASS 01-store'); print('TOTAL: 1/1')\""
    row, leaf = _run_leaf_write_then_timeout(tmp, cmd_ok)
    check("T1a timeout after Write + verify green => verified complete",
          row.get("result_type") == "complete" and row.get("verified") is True
          and row.get("exit_class") == "完成", row)
with tempfile.TemporaryDirectory() as tmp:
    cmd_bad = sys.executable + " -c \"print('PASS 01-store'); print('FAIL 02-smoke  -- boom'); print('TOTAL: 2/2')\""
    row, leaf = _run_leaf_write_then_timeout(tmp, cmd_bad)
    check("T1b timeout after Write + verify partial => partial, artifacts kept",
          row.get("result_type") == "partial" and leaf.get("artifacts")
          and row.get("verified") is False, row)

# ---------- 项 3：修复叶 context 模板化 ---------- #
_FAILED = {
    "id": "root.c2.1", "role": "执行", "parent": "root", "state": "失败",
    "failure_class": "timeout", "judge_why": "超时",
    "artifacts": [{"path": "main.py", "bytes": 930, "sha256": "aabbcc"}],
    "verify": {"command": "python3 /tmp/full-tree/tests/test_acceptance.py",
               "exit_code": 1, "passed": 15, "total": 17,
               "items": [{"name": "dispatch_create_then_lookup_e2e", "ok": False},
                         {"name": "dispatch_unknown_key_404_and_unknown_path_404", "ok": False}]},
}
with tempfile.TemporaryDirectory() as tmp:
    rd = str(Path(tmp) / "run")
    tmpl = driver_mod.build_repair_context(_FAILED, rd, "修 main.py，补 create_app 入口")
    check("T3a template has run-dir absolute path",
          rd in tmpl and ("工作区绝对路径" in tmpl), tmpl)
    check("T3a template has failed test names",
          "dispatch_create_then_lookup_e2e" in tmpl
          and "dispatch_unknown_key_404_and_unknown_path_404" in tmpl, tmpl)
    check("T3a template has verify command verbatim (absolute tests path)",
          "python3 /tmp/full-tree/tests/test_acceptance.py" in tmpl, tmpl)
    check("T3a template has artifact absolute path",
          os.path.join(rd, "project", "main.py") in tmpl, tmpl)
    check("T3a template keeps root hint (what to fix)",
          "修 main.py，补 create_app 入口" in tmpl, tmpl)
    check("T3a template never tells leaf the broken relative test path",
          ("cd " + rd + " && python3 tests/test_acceptance.py") not in tmpl, tmpl)
    check("T3a empty failed node => empty template (no fabrication)",
          driver_mod.build_repair_context({}, rd, "x") == "")

# ---------- 项 4（verify-only）：同类重派拒收时 apply_output 返回真实拒因 ---------- #
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
    from harness import recovery as rec_mod
    d.recovery = rec_mod.RecoveryController(str(run_dir))
    return d, root, run_dir

with tempfile.TemporaryDirectory() as tmp:
    d, root, run_dir = _mk_spawn_parent(tmp, ["执行", "执行"])
    msg = d.apply_output(root, "spawn", {"children": [
        {"role": "执行", "context": "重新执行 CPU 核数实测", "验收标准": "有数字", "auth": "只读"}]},
        KNOBS_ROOT)
    check("T4a apply_output names the true cause (same-kind budget, not empty leaf)",
          isinstance(msg, str) and "同类重派被拒 1 条" in msg
          and "空叶" not in msg, msg)
    d._reject(root, {"act": "spawn"}, msg)
    check("T4a _reject overwrite keeps the true cause in _last_reject",
          "同类重派被拒" in str((root.get("_last_reject") or {}).get("detail") or ""),
          root.get("_last_reject"))

# ---------- 项 3（续）：spawn 落盘的修复叶 context 即模板 + stub 干跑 ---------- #
with tempfile.TemporaryDirectory() as tmp:
    d, root, run_dir = _mk_spawn_parent(tmp, ["执行", "执行"])
    proj = run_dir / "project"
    proj.mkdir()
    (proj / "main.py").write_text("class App:\n    pass\n", encoding="utf-8")
    failed = d.tree["root.c2.1"]
    failed["artifacts"] = [{"path": "main.py", "bytes": 24, "sha256": "aabbcc"}]
    failed["verify"] = dict(_FAILED["verify"])
    out = d.spawn_children(root, {"children": [
        {"role": "执行", "context": "修 main.py，补 create_app 入口",
         "验收标准": "测试 17/17", "auth": "只读",
         "overrides": {"repair_of": "root.c2.1"}}]}, KNOBS_ROOT)
    check("T3b repair leaf still spawned (exemption kept)",
          len(out) == 1 and out[0] in d.tree, out)
    ctx = str(d.tree[out[0]].get("context") or "")
    check("T3b spawned repair context is the harness template",
          "Harness 模板生成" in ctx and str(run_dir) in ctx
          and "dispatch_create_then_lookup_e2e" in ctx
          and "修 main.py，补 create_app 入口" in ctx, ctx)
    check("T3b spawned repair context has absolute artifact path",
          str(proj / "main.py") in ctx, ctx)

with tempfile.TemporaryDirectory() as tmp:
    d, root, run_dir = _mk_spawn_parent(tmp, ["执行", "执行"])
    proj = run_dir / "project"
    proj.mkdir()
    (proj / "main.py").write_text("class App:\n    pass\n", encoding="utf-8")
    failed = d.tree["root.c2.1"]
    failed["artifacts"] = [{"path": "main.py", "bytes": 24, "sha256": "aabbcc"}]
    failed["verify"] = dict(_FAILED["verify"])
    out = d.spawn_children(root, {"children": [
        {"role": "执行", "context": "修 main.py，补 create_app 入口",
         "验收标准": "测试 17/17", "auth": "只读",
         "overrides": {"repair_of": "root.c2.1"}}]}, KNOBS_ROOT)
    captured = {}
    def _cap(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        captured["prompt"] = prompt
        return json.dumps({"reply": "已修 main.py，测试 17/17", "未决": []}, ensure_ascii=False)
    _cap.is_cc = True
    old_env = dict(os.environ)
    os.environ["TREE_CC_ACK_LEAF"] = "1"
    try:
        d2 = driver_mod.Driver(parent=root, tree=d.tree, table=TABLE, run_id="t-dc62",
                               run_dir=str(run_dir), act_runner=_cap)
        row = leafrun.run_leaf(d2, d.tree[out[0]])
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    prompt = captured.get("prompt") or ""
    check("T3c stub dry-run: repair leaf prompt carries absolute verify command",
          "python3 /tmp/full-tree/tests/test_acceptance.py" in prompt, prompt)
    check("T3c stub dry-run: repair leaf prompt has no broken relative test path",
          ("cd " + str(run_dir) + " && python3 tests/test_acceptance.py") not in prompt, prompt)
    check("T3c stub dry-run: repair leaf prompt has absolute project path",
          str(proj / "main.py") in prompt, prompt)

# ---------- 项 5：无进展重复调用熔断 ---------- #
with tempfile.TemporaryDirectory() as tmp:
    run_dir, restore = _fake_run_dir(tmp, "loop")
    try:
        try:
            cc_run.run_cc_node("执行", "ls", str(run_dir), knobs=KNOBS_EXEC,
                               timeout=10.0, session_id="cc-node")
            check("T5a loop fuse trips", False, "no raise")
        except RuntimeError as exc:
            check("T5a loop fuse trips on 3rd identical call",
                  "无进展重复调用熔断" in str(exc), str(exc))
            check("T5a loop failure_class=loop",
                  ct.classify_failure("环境", str(exc), {}) == "loop",
                  ct.classify_failure("环境", str(exc), {}))
        meta = _read_meta(run_dir)
        check("T5a loop meta: loop=true, 3 events kept",
              meta.get("loop") is True and meta.get("loop_tool") == "Bash"
              and meta.get("loop_count") == 3 and meta.get("events_observed") == 3, meta)
    finally:
        restore()

with tempfile.TemporaryDirectory() as tmp:
    run_dir, restore = _fake_run_dir(tmp, "varied")
    try:
        res = cc_run.run_cc_node("执行", "ls", str(run_dir), knobs=KNOBS_EXEC,
                                 timeout=10.0, session_id="cc-node")
        check("T5b varied re-checks pass the fuse (no trip)",
              res.get("ok") is True and len(res.get("tool_calls", [])) == 3, res)
    finally:
        restore()

check("T5c equivalent params fold whitespace (ls  /tmp == ls /tmp)",
      cc_run._canonical_tool_key("Bash", {"command": "ls  /tmp"})
      == cc_run._canonical_tool_key("Bash", {"command": "ls /tmp"}))
check("T5c different params are different keys",
      cc_run._canonical_tool_key("Bash", {"command": "ls /tmp"})
      != cc_run._canonical_tool_key("Bash", {"command": "ls /other"}))
check("T5c loop class registered (FAILURE_CLASSES + recovery)",
      "loop" in ct.FAILURE_CLASSES and bool((ct.RECOVERY_STRATEGIES or {}).get("loop")),
      ct.FAILURE_CLASSES)

# ---------- 项 6（verify-only）：字数门禁已取消，角色边界仍保留 ---------- #
check("T6a role.yaml 执行 min_out_chars=0 (length gate removed)",
      KNOBS_EXEC.get("min_out_chars") == 0, KNOBS_EXEC)
KNOBS_RESEARCH = role_mod.effective("调研", None, table=TABLE)
check("T6b role.yaml 调研 min_out_chars=0 + may_write=false kept",
      KNOBS_RESEARCH.get("min_out_chars") == 0
      and KNOBS_RESEARCH.get("may_write") is False, KNOBS_RESEARCH)
_research_node = {"judge": {"chars_min": 800}}
check("T6b research judge no longer blocks on length",
      leafrun.evaluate_judge(_research_node, {"reply": "x" * 100})[0] is True
      and leafrun.evaluate_judge(_research_node, {"reply": "x" * 800})[0] is True)
_ok_spawn, _why_spawn = schema.validate("spawn", {"children": [
    {"role": "执行", "context": "x", "judge": {"json_keys": ["reply"]},
     "验收标准": "x", "auth": "只读"}]}, {})
check("T6c spawn keeps minimal structured fields (5 required)",
      _ok_spawn is True, _why_spawn)
_bad_spawn, _why_bad = schema.validate("spawn", {"children": [
    {"role": "执行", "context": "x", "judge": {"json_keys": ["reply"]},
     "验收标准": "x"}]}, {})
check("T6c spawn missing auth still rejected",
      _bad_spawn is False and "auth" in str(_why_bad), _why_bad)
_ack_md = (ROOT / "prompts" / "执行-ack.md").read_text(encoding="utf-8")
check("T6d 执行-ack.md exists, ACK-only, 1500 mentioned only to forbid it",
      "ACK" in _ack_md and "不要凑 1500 字" in _ack_md and "不要写 doc" in _ack_md,
      _ack_md[:200])

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: %s" % ", ".join(FAIL))
    sys.exit(1)
