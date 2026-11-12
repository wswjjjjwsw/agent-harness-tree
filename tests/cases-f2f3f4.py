"""F2/F3/F4 回归（离线 + 沙箱真实验，t-97db6c553326）。跑法：cd <候选根> && python3 tests/cases-f2f3f4.py"""
import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import audit as audit_mod, driver as driver_mod, evidence, leafrun, role as role_mod, session as session_mod

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

# ---------- F3: TimedRunner 透传 timeout ----------
class _InnerTO:
    def __init__(self): self.seen = "unset"
    def __call__(self, node, act, knobs, prompt, session_id, run_dir, timeout=None):
        self.seen = timeout
        return "{}"
class _InnerNoTO:
    def __init__(self): self.seen = "called"
    def __call__(self, node, act, knobs, prompt, session_id, run_dir):
        return "{}"
_w = _InnerTO()
_r = leafrun.TimedRunner(_w)
_r({}, "leaf", {}, "p", "sid", "/tmp", timeout=300.0)
check("F3 TimedRunner 透传 timeout=300", _w.seen == 300.0, _w.seen)
_n = _InnerNoTO()
_r2 = leafrun.TimedRunner(_n)
try:
    _r2({}, "leaf", {}, "p", "sid", "/tmp", timeout=300.0)
    check("F3 TimedRunner 兼容无 timeout 参数的 runner", _n.seen == "called")
except TypeError as e:
    check("F3 TimedRunner 兼容无 timeout 参数的 runner", False, e)

# ---------- F3: run_act 研究叶 timeout 压到 300（离线桩 subprocess） ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"; run_dir.mkdir()
    agent = Path(tmp) / "agent"; sess = agent / "sessions"
    _saved = {k: os.environ.get(k) for k in ("TREE_PI_AGENT_DIR", "TREE_PI_AGENT_SESSION_DIR", "TREE_PI_SANDBOX")}
    os.environ["TREE_PI_AGENT_DIR"] = str(agent)
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(sess)
    os.environ["TREE_PI_SANDBOX"] = "0"
    _orig_run = session_mod.subprocess.run
    _seen = {}
    class _P:
        returncode = 0; stdout = "{}"; stderr = ""
    def _fake_run(argv, **kw):
        _seen["argv"] = list(argv); _seen["timeout"] = kw.get("timeout"); _seen["cwd"] = kw.get("cwd")
        return _P()
    session_mod.subprocess.run = _fake_run
    try:
        session_mod.run_act({"role": "调研"}, "leaf", {"thinking": "none"}, "prompt", session_id="sid-t", run_dir=str(run_dir), timeout=900)
        check("F3 run_act 研究叶 timeout 900→300", _seen.get("timeout") == 300.0, _seen)
        session_mod.run_act({"role": "执行"}, "leaf", {"thinking": "none"}, "prompt", session_id="sid-t2", run_dir=str(run_dir), timeout=900)
        check("F3 run_act 非研究叶 timeout 不压", _seen.get("timeout") == 900.0, _seen)
        check("F3 LEAF_TIMEOUT_S=300", session_mod.LEAF_TIMEOUT_S == 300.0)
    finally:
        session_mod.subprocess.run = _orig_run
        for k, v in _saved.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

    # ---------- F2: 沙箱 profile 文本 + 真实验 ----------
    prof = session_mod._sandbox_profile_text(str(run_dir))
    _home = os.environ.get("HOME", "")
    # 注意：profile 写 canonical 路径（/tmp 会 resolve 成 /private/tmp），断言也要 resolve
    check("F2 profile 含 run_dir 允许", ('(allow file-write* (subpath "%s"))' % str(Path(run_dir).resolve())) in prof)
    check("F2 profile 拒 /private/tmp", '(deny file-write* (subpath "/private/tmp"))' in prof)
    check("F2 profile 拒生产树", ('(deny file-write* (subpath "%s/agent-harness"))' % _home) in prof)
    check("F2 profile 拒 mini", ('(deny file-write* (subpath "%s/mini"))' % _home) in prof)
    check("F2 profile 允许在拒绝之后", prof.rindex("(allow file-write*") > prof.index('(deny file-write* (subpath "%s/agent-harness"))' % _home))
    w_on = session_mod._sandbox_wrap(["pi", "-p"], str(run_dir))
    check("F2 _sandbox_wrap 默认包 sandbox-exec", w_on[0] == "sandbox-exec" and w_on[-2:] == ["pi", "-p"], w_on)
    os.environ["TREE_PI_SANDBOX"] = "0"
    check("F2 _sandbox_wrap TREE_PI_SANDBOX=0 不包", session_mod._sandbox_wrap(["pi", "-p"], str(run_dir)) == ["pi", "-p"])
    os.environ.pop("TREE_PI_SANDBOX", None)
    # 真实验：写 /tmp 标记与候选外生产文件必须被拦且哈希不变；run 内写入放行
    marker = Path("/tmp/f2f3f4-marker.txt")
    marker.write_text("F2-MARKER-ORIGINAL\n", encoding="utf-8")
    prod_file = ROOT / "harness" / "leafrun.py"
    def _h(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    h_m0, h_p0 = _h(marker), _h(prod_file)
    prof_path = run_dir / "pi-sandbox.sb"
    prof_path.write_text(prof, encoding="utf-8")
    subprocess.run(["sandbox-exec", "-f", str(prof_path), "/bin/sh", "-c",
                    "echo HACKED >> /tmp/f2f3f4-marker.txt; echo HACKED >> %s; echo OK > %s/allowed.txt" % (prod_file, run_dir)],
                   capture_output=True, text=True)
    check("F2 沙箱真实验：/tmp 标记哈希不变", _h(marker) == h_m0)
    check("F2 沙箱真实验：生产文件哈希不变", _h(prod_file) == h_p0)
    check("F2 沙箱真实验：run 内写入放行", (run_dir / "allowed.txt").is_file())
    marker.unlink(missing_ok=True)

# ---------- F4: 审计等叶终结（分体设计：spawn 时挂节点，merge 前排空执行） ----------
with tempfile.TemporaryDirectory() as tmp:
    table = role_mod.load_roles()
    def _mk_tree(kid_states):
        tree = {"root": {"id": "root", "parent": "", "batch": 2, "round": 2, "state": "等子节点", "role": "根"}}
        for i, (st, reply) in enumerate(kid_states, 1):
            nid = "root.c2.%d" % i
            tree[nid] = {"id": nid, "parent": "root", "batch": 2, "round": 1, "state": st,
                         "role": "执行", "reply": reply, "result": "", "未决": []}
        return tree
    _calls = {"n": 0}
    def _stub(node, act, knobs, prompt, session_id, run_dir):
        _calls["n"] += 1
        return json.dumps({"reply": "抽检：无发现。", "未决": []}, ensure_ascii=False)
    tree = _mk_tree([("待跑", ""), ("待跑", ""), ("待跑", "")])
    d = driver_mod.Driver(tree["root"], tree=tree, table=table, run_id="f4t", run_dir=tmp, act_runner=_stub)
    cid = audit_mod.run_sample(d, tree["root"])
    rows = audit_mod.read_ledger(d)
    check("F4 叶未终结：挂节点不调模型不留行", cid == "root.a2.1" and _calls["n"] == 0 and rows == [] and tree.get("root.a2.1", {}).get("state") == "在跑", (cid, _calls, rows))
    check("F4 挂起节点不被当叶子候选（state=在跑）", tree.get("root.a2.1", {}).get("state") != "待跑")
    check("F4 叶未终结：排空不执行", audit_mod.drain_pending(d, tree["root"]) == [] and _calls["n"] == 0)
    for nid in ("root.c2.1", "root.c2.2", "root.c2.3"):
        tree[nid]["state"] = "完成"; tree[nid]["reply"] = "实测结果 %s" % nid
    tree["root"]["round"] = 3  # merge 轮：trig = 3-1 = 2
    done = audit_mod.drain_pending(d, tree["root"])
    rows = audit_mod.read_ledger(d)
    check("F4 排空：叶终结后补审（恰一行已审）", len(done) == 1 and _calls["n"] == 1 and len(rows) == 1 and rows[0].get("状态") == "已审", (done, _calls, rows))
    check("F4 排空：检查点 id 形态 root.a2.1 且已落终态", tree.get("root.a2.1", {}).get("state") == "完成")
    check("F4 排空：只补审一次", audit_mod.drain_pending(d, tree["root"]) == [] and _calls["n"] == 1)
    # 2 子批次：既有口径（单行未审、原因逐字），排空不再动
    tree2 = _mk_tree([("待跑", ""), ("待跑", "")])
    _calls["n"] = 0
    d2 = driver_mod.Driver(tree2["root"], tree=tree2, table=table, run_id="f4t2", run_dir=tmp + "2", act_runner=_stub)
    cid2 = audit_mod.run_sample(d2, tree2["root"])
    rows2 = audit_mod.read_ledger(d2)
    check("F4 2 子：单行未审、原因逐字", cid2 is None and len(rows2) == 1 and rows2[0].get("状态") == "未审" and rows2[0].get("原因") == "批次子数不足阈值（2<3）", rows2)
    tree2["root"]["round"] = 3  # merge 轮：trig=2，与 run_sample 写的 root#2 同批
    check("F4 2 子：排空不重复审", audit_mod.drain_pending(d2, tree2["root"]) == [] and _calls["n"] == 0)
    # 无工件叶：非 final 只挂节点不调模型；final 补审且原因记缺口
    tree3 = _mk_tree([("完成", "实测结果"), ("完成", ""), ("完成", "实测结果")])
    _calls["n"] = 0
    d3 = driver_mod.Driver(tree3["root"], tree=tree3, table=table, run_id="f4t3", run_dir=tmp + "3", act_runner=_stub)
    cid3 = audit_mod.run_sample(d3, tree3["root"])
    check("F4 无工件叶：非 final 只挂节点不调模型", cid3 == "root.a2.1" and _calls["n"] == 0 and audit_mod.read_ledger(d3) == [], (cid3, _calls))
    cid3b = audit_mod.run_sample(d3, tree3["root"], final=True)
    rows3 = audit_mod.read_ledger(d3)
    check("F4 无工件叶：final 补审且原因记缺口", cid3b is not None and _calls["n"] == 1 and any(r.get("状态") == "已审" and "无工件" in str(r.get("原因") or "") for r in rows3), rows3)
    # 已有可审内容：spawn 时立即执行（cases-audit 的预填子节点口径）
    tree4 = _mk_tree([("完成", "SIBMARK-1"), ("完成", "SIBMARK-2"), ("完成", "SIBMARK-3")])
    _calls["n"] = 0
    d4 = driver_mod.Driver(tree4["root"], tree=tree4, table=table, run_id="f4t4", run_dir=tmp + "4", act_runner=_stub)
    cid4 = audit_mod.run_sample(d4, tree4["root"])
    rows4 = audit_mod.read_ledger(d4)
    check("F4 有工件：spawn 时立即执行恰一行已审", cid4 == "root.a2.1" and _calls["n"] == 1 and len(rows4) == 1 and rows4[0].get("状态") == "已审", (cid4, _calls, rows4))
    tree4["root"]["round"] = 3  # merge 轮：trig=2，与已审行 root#2 同批
    check("F4 有工件：merge 前排空不再重复", audit_mod.drain_pending(d4, tree4["root"]) == [] and _calls["n"] == 1)
    # 混合批（3 预填有内容 + 3 新鲜叶）：新鲜叶不算缺口，spawn 时立即执行（cases-audit t_siblings 口径）
    tree5 = _mk_tree([("完成", "SIBMARK-1"), ("完成", "SIBMARK-2"), ("完成", "SIBMARK-3"),
                      ("待跑", ""), ("待跑", ""), ("待跑", "")])
    _calls["n"] = 0
    d5 = driver_mod.Driver(tree5["root"], tree=tree5, table=table, run_id="f4t5", run_dir=tmp + "5", act_runner=_stub)
    cid5 = audit_mod.run_sample(d5, tree5["root"])
    rows5 = audit_mod.read_ledger(d5)
    check("F4 混合批：有可审内容即执行（新鲜叶不算缺口）", cid5 == "root.a2.1" and _calls["n"] == 1 and len(rows5) == 1 and rows5[0].get("状态") == "已审", (cid5, _calls, rows5))

print("---- %d 通过，%d 失败 ----" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
