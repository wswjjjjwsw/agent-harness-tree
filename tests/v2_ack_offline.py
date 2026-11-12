"""V2 ACK 叶离线测试。跑法：cd <候选根> && python3 tests/v2_ack_offline.py
离线：不联网、不打 8092/8903、不起 pi、不调 DeepSeek、不调 claude。
覆盖：(1) 超时 failure_class 优先 (2) spawn 空叶可行动反馈 (3) 同类预算修复豁免
(4) 产物扫描 + verify (5) merge 经 _history_block 看见产物/失败叶 (6) ACK 叶 schema
(7) contract_note ACK 版 (8) 截断 stream 解析 (9) 未决 type 超时 (10) run-dir 锚点。
"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import contract as ct
from harness import cc_run
from harness import driver as driver_mod
from harness import leafrun, node as node_mod
from harness import recovery as rec_mod, role as role_mod, schema
from harness import submit as submit_mod

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

TABLE = role_mod.load_roles()
KNOBS_EXEC = role_mod.effective("执行", None, table=TABLE)
KNOBS_ROOT = role_mod.effective("根", None, table=TABLE)

# ---------- A. classify_failure: 超时优先，不再误标 bad_citation ---------- #
TRAP_WHY = ("TimeoutExpired: Command '['claude', '-p', ...]' timed out after 300 seconds; "
            "prompt 内含‘不许引用不存在的文件’与‘引用不可解’（V1 routeB-02 的误分类诱因）")
check("A1 timeout exit_class wins over citation keywords",
      ct.classify_failure("超时", TRAP_WHY) == "timeout",
      ct.classify_failure("超时", TRAP_WHY))
check("A2 non-timeout citation still classified bad_citation",
      ct.classify_failure("失败", "无法核查的引用：引用了不存在的文件 x") == "bad_citation",
      ct.classify_failure("失败", "无法核查的引用：引用了不存在的文件 x"))
check("A3 timeout without citation words still timeout",
      ct.classify_failure("超时", "plain timeout, no keywords") == "timeout",
      ct.classify_failure("超时", "plain timeout, no keywords"))

# ---------- B/C. spawn_children: 空叶反馈 + 修复叶豁免 ---------- #
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

def _viol(run_dir):
    p = run_dir / "violations.jsonl"
    return p.read_text(encoding="utf-8") if p.is_file() else ""

with tempfile.TemporaryDirectory() as tmp:
    d, root, run_dir = _mk_spawn_parent(tmp, [])
    msg = d.apply_output(root, "spawn", {"children": [
        {"role": "执行", "context": "", "验收标准": "", "auth": "只读"}]}, KNOBS_ROOT)
    check("B1 empty leaf refused with kind in violations",
          "驱动_空叶" in _viol(run_dir), _viol(run_dir))
    check("B2 apply_output keeps required prefix",
          isinstance(msg, str) and "spawn 无有效子节点" in msg, msg)
    check("B3 apply_output names the true cause (empty, not same-kind)",
          isinstance(msg, str) and "空叶 1 条" in msg and "同类重派被拒" not in msg, msg)

with tempfile.TemporaryDirectory() as tmp:
    d, root, run_dir = _mk_spawn_parent(tmp, ["执行", "执行"])
    proj = run_dir / "project"
    proj.mkdir()
    (proj / "store.py").write_text("def x():\n    return 1\n", encoding="utf-8")
    out = d.spawn_children(root, {"children": [
        {"role": "执行", "context": "修复 root.c2.1：补写 project/store.py 缺失的函数",
         "验收标准": "store.py 可 import", "auth": "只读",
         "overrides": {"repair_of": "root.c2.1"}}]}, KNOBS_ROOT)
    check("C1 repair leaf exempted from same-kind budget",
          len(out) == 1 and out[0] in d.tree, out)
    check("C1b exemption logged",
          "驱动_同类重派豁免_修复" in _viol(run_dir), _viol(run_dir))

with tempfile.TemporaryDirectory() as tmp:
    d, root, run_dir = _mk_spawn_parent(tmp, ["执行", "执行"])
    proj = run_dir / "project"
    proj.mkdir()
    (proj / "store.py").write_text("def x():\n    return 1\n", encoding="utf-8")
    for i in (3, 4):
        nid = "root.c2.%d" % i
        n = node_mod.new_node(nid, "执行", parent="root", context="repair attempt",
                              judge={"chars_min": 5}, 验收标准="x", auth="只读", batch=2)
        n["state"] = "失败"
        n["overrides"] = {"repair_of": "root.c2.1"}
        d.tree[nid] = n
    out = d.spawn_children(root, {"children": [
        {"role": "执行", "context": "第三次修复 root.c2.1：继续补 store.py",
         "验收标准": "store.py 可 import", "auth": "只读",
         "overrides": {"repair_of": "root.c2.1"}}]}, KNOBS_ROOT)
    check("C2 third repair of same leaf still refused (cap 2)",
          out == [] and "驱动_同类重派被拒" in _viol(run_dir), (out, _viol(run_dir)))

with tempfile.TemporaryDirectory() as tmp:
    d, root, run_dir = _mk_spawn_parent(tmp, ["执行", "执行"])
    out = d.spawn_children(root, {"children": [
        {"role": "执行", "context": "修复 root.c2.1：补写 nosuch.py（磁盘上不存在）",
         "验收标准": "x", "auth": "只读",
         "overrides": {"repair_of": "root.c2.1"}}]}, KNOBS_ROOT)
    check("C3 repair naming no existing file is not exempted",
          out == [] and "驱动_同类重派被拒" in _viol(run_dir), (out, _viol(run_dir)))

with tempfile.TemporaryDirectory() as tmp:
    d, root, run_dir = _mk_spawn_parent(tmp, ["执行", "执行"])
    out = d.spawn_children(root, {"children": [
        {"role": "执行", "context": "重新执行 CPU 核数实测（换个措辞，无 repair_of）",
         "验收标准": "有数字", "auth": "只读"}]}, KNOBS_ROOT)
    check("C4 same-role redispatch without repair_of still refused",
          out == [] and "驱动_同类重派被拒" in _viol(run_dir), (out, _viol(run_dir)))
    check("C4b refusal tells parent the repair-leaf path",
          "修复叶" in str((root.get("_last_reject") or {}).get("detail") or ""),
          root.get("_last_reject"))

# ---------- D. 产物扫描 + verify ---------- #
with tempfile.TemporaryDirectory() as tmp:
    proj = Path(tmp) / "project"
    proj.mkdir()
    before = leafrun._snapshot_dir(proj)
    (proj / "store.py").write_text("def get_store():\n    return {}\n", encoding="utf-8")
    after = leafrun._snapshot_dir(proj)
    created, modified = leafrun._diff_snapshot(before, after)
    check("D1 snapshot diff finds created file",
          len(created) == 1 and created[0]["path"] == "store.py" and created[0]["bytes"] > 0,
          (created, modified))
    (proj / "store.py").write_text("def get_store():\n    return {}\n\ndef extra():\n    return 2\n", encoding="utf-8")
    after2 = leafrun._snapshot_dir(proj)
    created2, modified2 = leafrun._diff_snapshot(after, after2)
    check("D2 snapshot diff finds modified file",
          created2 == [] and len(modified2) == 1 and modified2[0]["path"] == "store.py",
          (created2, modified2))
    cmd = sys.executable + " -c \"print('PASS 01-store'); print('PASS 02-smoke'); print('TOTAL: 2/2')\""
    ver = leafrun._run_verify(cmd, str(tmp), str(proj))
    check("D3 verify parses PASS lines + total",
          ver["passed"] == 2 and ver["total"] == 2 and ver["exit_code"] == 0, ver)
    check("D4 verify compiles + import-smokes artifacts",
          ver["compile_ok"] is True and all(s["ok"] for s in ver["smoke"]), ver)
    cmd2 = sys.executable + " -c \"print('PASS 01-store'); print('FAIL 02-smoke  -- boom'); print('TOTAL: 2/2')\""
    ver2 = leafrun._run_verify(cmd2, str(tmp), str(proj))
    check("D5 verify FAIL line parses name via double-space separator",
          ver2["passed"] == 1 and ver2["total"] == 2
          and ver2["items"][1] == {"name": "02-smoke", "ok": False}, ver2)
    rec_path = leafrun._write_artifact_record(str(Path(tmp) / "run"), "root.c2.1",
        {"created": created, "verify": ver})
    check("D6 artifact record lands on disk",
          rec_path == "artifacts/root.c2.1.json"
          and (Path(tmp) / "run" / rec_path).is_file()
          and json.loads((Path(tmp) / "run" / rec_path).read_text(encoding="utf-8"))["verify"]["passed"] == 2,
          rec_path)

# ---------- E. merge 经 _history_block 看见产物 + 失败叶 ---------- #
with tempfile.TemporaryDirectory() as tmp:
    proj = Path(tmp) / "project"
    proj.mkdir()
    (proj / "store.py").write_text("def x():\n    return 1\n", encoding="utf-8")
    leaf = {"id": "root.c2.1", "role": "执行", "parent": "root", "state": "完成", "judge_ok": True,
            "judge_why": "", "reply": "已写 store.py",
            "artifacts": [{"path": "store.py", "bytes": 24, "sha256": "aabbcc"}],
            "verify": {"passed": 2, "total": 2, "compile_ok": True}, "verified": True}
    failed = {"id": "root.c2.2", "role": "执行", "parent": "root", "state": "失败", "judge_ok": False,
              "judge_why": "超时", "reply": "", "artifacts": [], "verify": {}}
    parent = {"id": "root", "_tree": {"root": {}, "root.c2.1": leaf, "root.c2.2": failed},
              "_run_dir": tmp}
    block = driver_mod._history_block(parent)
    check("E1 history block lists artifact with size",
          "store.py(24B)" in block, block)
    check("E2 history block lists verify pass count",
          "verify=2/2" in block and "编译通过" in block, block)
    check("E3 history block marks verified leaf",
          "已验证" in block, block)
    check("E4 history block lists failed leaf too",
          "root.c2.2" in block and "失败" in block, block)
    check("E5 disk inventory sees project files",
          "store.py" in block, block)

# ---------- F. ACK 叶 schema ---------- #
ok_ack, why_ack = schema.validate("leaf", {"reply": "已写 store.py，测试 2/2", "未决": []},
                                  KNOBS_EXEC, ack_leaf=True)
check("F1 ACK leaf passes without doc",
      ok_ack is True, why_ack)
ok_old, why_old = schema.validate("leaf", {"reply": "已写 store.py，测试 2/2", "未决": []},
                                  KNOBS_EXEC, ack_leaf=False)
check("F2 non-ACK leaf still requires doc (validator kept)",
      ok_old is False and "doc" in str(why_old), why_old)

# ---------- F3/F4. fields hard-doc rule bypassed for ACK leaves only ---------- #
from harness import fields as fields_mod
hard_ack, _ = fields_mod.classify_and_fill("leaf", {"reply": "x", "未决": []},
                                           {"may_write": True}, ack_leaf=True)
check("F3 ACK leaf: no hard doc error",
      hard_ack == [], hard_ack)
hard_old, _ = fields_mod.classify_and_fill("leaf", {"reply": "x", "未决": []},
                                           {"may_write": True}, ack_leaf=False)
check("F4 non-ACK may_write leaf still hard-requires doc",
      any(h.get("code") == "hard_field_missing:doc" for h in hard_old), hard_old)

# ---------- G. contract_note ACK 版 ---------- #
ack_note = submit_mod.contract_note("leaf", KNOBS_EXEC, ack_leaf=True)
plain_note = submit_mod.contract_note("leaf", KNOBS_EXEC, ack_leaf=False)
check("G1 ACK contract note drops the 1500-char burden",
      "1500" not in ack_note, ack_note[:400])
check("G2 plain contract note still carries the doc burden",
      "1500" in plain_note or "doc" in plain_note, plain_note[:400])
check("G3 ACK note is shorter (no block-field convention)",
      len(ack_note) < len(plain_note), (len(ack_note), len(plain_note)))

# ---------- H. 截断 stream 解析 ---------- #
raw = (
    '{"type": "assistant", "message": {"content": ['
    '{"type": "tool_use", "name": "Write", "input": {"file_path": "project/store.py"}}]}}\n'
    'this line is not json, stream was cut here\n'
)
parsed = cc_run._parse_partial_stream_json(raw)
check("H1 partial stream extracts Write call",
      len(parsed["tool_calls"]) == 1 and parsed["tool_calls"][0]["name"] == "Write",
      parsed)
check("H2 partial stream tolerates truncation (no result event, no raise)",
      parsed["num_turns"] == 0 and parsed["unparsable_lines"] == 1, parsed)

# ---------- I. 未决 type 超时 ---------- #
check("I1 UNDECIDED_TYPES includes timeout",
      "超时" in schema.UNDECIDED_TYPES, schema.UNDECIDED_TYPES)
ok_m, why_m = schema.validate("merge",
    {"reply": "两叶一成一败，store 已验证", "去向": "再派",
     "下一批要点": ["修复 root.c2.2 超时叶"],
     "未决": [{"type": "超时", "what": "叶超时", "why": "300s 未收敛"}]}, {})
check("I2 merge with undecided type timeout validates",
      ok_m is True, why_m)

# ---------- J. run-dir 锚点（V2-1b 实测：叶把四模块写到 /tmp/full-tree/project/） ---------- #
def _run_leaf_capture(tmp, is_cc, ack_env):
    captured = {}
    def _cap(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        captured["prompt"] = prompt
        return json.dumps({"reply": "已写 project/store.py", "未决": []}, ensure_ascii=False)
    _cap.is_cc = is_cc
    run_dir = Path(tmp) / "run"
    run_dir.mkdir(exist_ok=True)
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None,
                             验收标准="x", auth="只读", batch=2)
    tree = {"root": root}
    d = driver_mod.Driver(parent=root, tree=tree, table=TABLE, run_id="t-anchor",
                          run_dir=str(run_dir), act_runner=_cap)
    leaf = node_mod.new_node("root.c2.1", "执行", parent="root",
                             context="写 project/store.py", judge={"json_keys": ["reply"]},
                             验收标准="store.py 存在", auth="只读", batch=2)
    tree["root.c2.1"] = leaf
    old_env = os.environ.get("TREE_CC_ACK_LEAF")
    if ack_env:
        os.environ["TREE_CC_ACK_LEAF"] = "1"
    else:
        os.environ.pop("TREE_CC_ACK_LEAF", None)
    try:
        leafrun.run_leaf(d, leaf)
    finally:
        if old_env is None:
            os.environ.pop("TREE_CC_ACK_LEAF", None)
        else:
            os.environ["TREE_CC_ACK_LEAF"] = old_env
    return captured.get("prompt") or "", str(run_dir)

with tempfile.TemporaryDirectory() as tmp:
    p_cc, rd = _run_leaf_capture(tmp, True, True)
    check("J1 CC leaf prompt names the absolute run dir",
          rd in p_cc and "工作目录" in p_cc, p_cc[-300:])
    check("J2 CC leaf prompt forbids writing outside the run dir",
          "不要写到 run 目录之外" in p_cc, p_cc[-300:])

with tempfile.TemporaryDirectory() as tmp:
    p_pi, rd = _run_leaf_capture(tmp, False, False)
    check("J3 pi leaf prompt has no run-dir anchor (pi untouched)",
          "工作目录" not in p_pi, p_pi[-200:])

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
