"""V2.1 收尾缺口专项回归（t-a90a7a1658f9）。跑法：cd <候选根> && python3 tests/cases-v21close.py
离线：不联网、不打真 8092/8903（adapter 测试用本地桩 LLAAT）、不起 pi、不调 DeepSeek。
缺口 (A) pi 直连 8092 绕过 LLAAT；缺口 (B) 前提证伪无生产者。
"""
import json, os, sys, tempfile, threading
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import driver as driver_mod, leafrun, node as node_mod, premise as premise_mod
from harness import role as role_mod, session as session_mod, submit as submit_mod, trust as tr
from harness import recovery as rec_mod

try:
    from harness import llaat_adapter as adapter_mod
    HAS_ADAPTER = True
except Exception:
    adapter_mod = None
    HAS_ADAPTER = False

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

TABLE = role_mod.load_roles()

# ---------- A1: models.json baseUrl 默认走 LLAAT 适配器 ----------
_saved = {k: os.environ.get(k) for k in ("TREE_USE_LLAAT", "TREE_LLAAT_ADAPTER_URL")}
for k in ("TREE_USE_LLAAT", "TREE_LLAAT_ADAPTER_URL"):
    os.environ.pop(k, None)
try:
    mj = json.loads(session_mod.models_json())
    base = mj["providers"]["bonsai"]["baseUrl"]
    check("A1 default baseUrl routes via LLAAT adapter (not 8092)",
          base == "http://127.0.0.1:8904/v1", base)
finally:
    for k, v in _saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

os.environ["TREE_USE_LLAAT"] = "0"
try:
    mj = json.loads(session_mod.models_json())
    base = mj["providers"]["bonsai"]["baseUrl"]
    check("A1 escape hatch TREE_USE_LLAAT=0 keeps direct 8092",
          base == "http://127.0.0.1:8092/v1", base)
finally:
    os.environ.pop("TREE_USE_LLAAT", None)

os.environ["TREE_LLAAT_ADAPTER_URL"] = "http://127.0.0.1:9999/v1"
try:
    mj = json.loads(session_mod.models_json())
    base = mj["providers"]["bonsai"]["baseUrl"]
    check("A1 custom adapter URL env is honoured",
          base == "http://127.0.0.1:9999/v1", base)
finally:
    os.environ.pop("TREE_LLAAT_ADAPTER_URL", None)

# ---------- A2-A4: adapter 翻译 + 预算 + 拒收（桩 LLAAT） ----------
class _FakeLLAAT(BaseHTTPRequestHandler):
    submits = []
    task_gets = 0
    reject_submit = False
    def log_message(self, *a):
        pass
    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "tree_backend": {"ok": True, "model": "fake-sage-model",
                          "base_url": "http://127.0.0.1:8092/v1", "context_limit": 163840, "in_flight": 0}})
        elif self.path.startswith("/task/"):
            type(self).task_gets += 1
            self._send(200, {"id": "tree-fake-1", "state": "done", "text": "FAKE SAGE TEXT",
                             "tree_response": {"choices": [{"message": {"role": "assistant", "content": "FAKE SAGE TEXT"},
                                                          "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
                             "judge_ok": True, "error": ""})
        else:
            self._send(404, {"error": "nope"})
    def do_POST(self):
        if self.path.startswith("/submit"):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode() or "{}")
            type(self).submits.append(body)
            if type(self).reject_submit:
                self._send(400, {"rejected": "fake rejection"})
            else:
                self._send(202, {"id": "tree-fake-1", "state": "queued"})
        else:
            self._send(404, {"error": "nope"})

def _serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()

def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

if not HAS_ADAPTER:
    check("A2 adapter module exists", False, "harness/llaat_adapter.py missing")
    check("A3 adapter module exists", False, "harness/llaat_adapter.py missing")
    check("A4 adapter module exists", False, "harness/llaat_adapter.py missing")
    check("A5 adapter module exists", False, "harness/llaat_adapter.py missing")
else:
    _FakeLLAAT.submits = []
    _FakeLLAAT.task_gets = 0
    _FakeLLAAT.reject_submit = False
    fake = ThreadingHTTPServer(("127.0.0.1", 18903), _FakeLLAAT)
    _serve(fake)
    ad = adapter_mod.create_server(18904, "http://127.0.0.1:18903")
    _serve(ad)
    try:
        code, out = _post("http://127.0.0.1:18904/v1/chat/completions",
                          {"model": "pi-model-arg", "messages": [{"role": "user", "content": "hello"}],
                           "max_tokens": 100, "temperature": 0.0})
        check("A2 adapter returns fake Sage text",
              code == 200 and out.get("choices", [{}])[0].get("message", {}).get("content") == "FAKE SAGE TEXT",
              (code, out))
        sub = _FakeLLAAT.submits[0] if _FakeLLAAT.submits else {}
        check("A2 adapter submits kind=tree to LLAAT",
              sub.get("kind") == "tree" and isinstance(sub.get("request"), dict), sub)
        check("A2 adapter uses served model id (not pi model arg)",
              (sub.get("request") or {}).get("model") == "fake-sage-model",
              (sub.get("request") or {}).get("model"))
        check("A2 adapter polled /task until done",
              _FakeLLAAT.task_gets >= 1, _FakeLLAAT.task_gets)
        # A3: 超预算不提交
        _FakeLLAAT.submits = []
        code, _ = _post("http://127.0.0.1:18904/v1/chat/completions",
                        {"model": "m", "messages": [{"role": "user", "content": "x" * 200000}],
                         "max_tokens": 50000})
        check("A3 adapter rejects over-budget request (400) without submitting",
              code == 400 and len(_FakeLLAAT.submits) == 0, (code, len(_FakeLLAAT.submits)))
        # A9 (F4)：tools 的 JSON 字符计入预算——小消息 + 巨 tools 也超限，拒收不提交
        _FakeLLAAT.submits = []
        big_tools = [{"type": "function", "function": {"name": "f", "description": "x" * 250000}}]
        code, _ = _post("http://127.0.0.1:18904/v1/chat/completions",
                        {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                         "max_tokens": 50, "tools": big_tools})
        check("A9 adapter counts tools JSON in budget (400, no submit)",
              code == 400 and len(_FakeLLAAT.submits) == 0, (code, len(_FakeLLAAT.submits)))
        # A4: LLAAT 拒收 -> 502，且只提交一次（不自动重试）
        _FakeLLAAT.submits = []
        _FakeLLAAT.reject_submit = True
        code, _ = _post("http://127.0.0.1:18904/v1/chat/completions",
                        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50})
        check("A4 adapter surfaces LLAAT rejection as 502 with no auto-retry",
              code == 502 and len(_FakeLLAAT.submits) == 1, (code, len(_FakeLLAAT.submits)))
        _FakeLLAAT.reject_submit = False
        # A6: stream:true -> SSE（单块全文 + [DONE]），pi 等流式客户端不再挂住
        req = urllib.request.Request("http://127.0.0.1:18904/v1/chat/completions",
                                     data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                                    "max_tokens": 50, "stream": True}, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            sse = resp.read().decode()
            ctype = resp.headers.get("Content-Type") or ""
        check("A6 stream request gets SSE with full text + [DONE]",
              "text/event-stream" in ctype and "FAKE SAGE TEXT" in sse and "data: [DONE]" in sse,
              (ctype, sse[:200]))
        # A7: 旧版 /v1/completions -> text_completion 形状（choices[0].text）
        code, out = _post("http://127.0.0.1:18904/v1/completions",
                          {"model": "m", "prompt": "hi", "max_tokens": 50})
        check("A7 legacy completions endpoint returns text_completion shape",
              code == 200 and out.get("object") == "text_completion"
              and out.get("choices", [{}])[0].get("text") == "FAKE SAGE TEXT", (code, out))
        # A8: GET /v1/models 返回服役模型（pi 启动时拉列表校验；缺了会 404 后挂住）
        with urllib.request.urlopen("http://127.0.0.1:18904/v1/models", timeout=10) as resp:
            models_out = json.loads(resp.read().decode())
        check("A8 GET /v1/models returns served model id",
              models_out.get("object") == "list"
              and models_out.get("data", [{}])[0].get("id") == "fake-sage-model",
              models_out)
        # A5: 已有适配器在跑时 ensure_adapter 复用，不另起
        os.environ["TREE_LLAAT_ADAPTER_URL"] = "http://127.0.0.1:18904/v1"
        os.environ.pop("TREE_USE_LLAAT", None)
        try:
            url = session_mod.ensure_adapter()
            check("A5 ensure_adapter reuses a running adapter",
                  url == "http://127.0.0.1:18904/v1", url)
        finally:
            os.environ.pop("TREE_LLAAT_ADAPTER_URL", None)
    finally:
        ad.shutdown()
        ad.server_close()
        fake.shutdown()
        fake.server_close()

# ---------- B1: spawn 即登记前提 ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None, 验收标准="", auth="")
    p = node_mod.new_node("root.c2", "决策", parent="root", context="决策章", batch=4, 验收标准="x", auth="")
    p["round"] = 2
    p["merge"] = {"去向": "再派", "下一批要点": ["CPU 核心数是 4", "内存容量是 16GB"]}
    tree = {"root": root, "root.c2": p}
    def _stub(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        return "{}"
    timed = leafrun.TimedRunner(_stub)
    d = leafrun.TreeDriver(p, tree, TABLE, run_id="t-close-b1",
                           run_dir=str(run_dir), act_runner=timed, timed=timed)
    knobs = dict(role_mod.effective("决策", None, table=TABLE))
    knobs["max_children_per_round"] = 5
    out = d.spawn_children(p, {"children": [
        {"role": "调研", "context": "调查 CPU 核心数", "验收标准": "有数字", "auth": "只读", "judge": {"chars_min": 5}},
        {"role": "调研", "context": "调查内存容量", "验收标准": "有数字", "auth": "只读", "judge": {"chars_min": 5}}]}, knobs)
    check("B1 spawn creates 2 children", len(out) == 2, out)
    prems = premise_mod.read_premises(str(run_dir))
    check("B1 spawn registers one premise per 要点",
          set(prems) >= {"P-root.c2-r2-1", "P-root.c2-r2-2"}, list(prems))
    check("B1 premise 1 statement + dependent is child 1",
          prems.get("P-root.c2-r2-1", {}).get("statement", "").startswith("CPU 核心数是 4")
          and prems.get("P-root.c2-r2-1", {}).get("dependents") == [out[0]], prems.get("P-root.c2-r2-1"))
    check("B1 premise 2 dependent is child 2",
          prems.get("P-root.c2-r2-2", {}).get("dependents") == [out[1]], prems.get("P-root.c2-r2-2"))

# ---------- B2: 叶 prompt 注入其依赖的前提 + 标记格式 ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    premise_mod.record_premise(str(run_dir), "P-root.c2-1", "CPU 核心数是 4",
                               dependents=["root.c2.c2.1"], source_node="root.c2")
    knobs = role_mod.effective("调研", None, table=TABLE)
    node = node_mod.new_node("root.c2.c2.1", "调研", parent="root.c2",
                             context="调查 CPU 核心数", judge={"chars_min": 5},
                             验收标准="有数字", auth="只读")
    node["_run_dir"] = str(run_dir)
    prompt = submit_mod.assemble_prompt(node, knobs, act="leaf")
    check("B2 leaf prompt lists its dependent premise",
          "P-root.c2-1" in prompt and "CPU 核心数是 4" in prompt, prompt[-400:])
    check("B2 leaf prompt gives the falsification marker format",
          "前提被证伪" in prompt and "冲突" in prompt, prompt[-400:])
    node2 = node_mod.new_node("root.c2.c2.9", "调研", parent="root.c2",
                              context="调查别的东西", judge={"chars_min": 5},
                              验收标准="有数字", auth="只读")
    node2["_run_dir"] = str(run_dir)
    prompt2 = submit_mod.assemble_prompt(node2, knobs, act="leaf")
    check("B2 leaf without dependent premises gets no injection",
          "前提被证伪" not in prompt2, prompt2[-300:])

# ---------- B3: 生产者->消费者全链（登记依赖，非回落） ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None, 验收标准="", auth="")
    p = node_mod.new_node("root.c2", "决策", parent="root", context="决策章", batch=4, 验收标准="x", auth="")
    p["round"] = 2
    p["merge"] = {"去向": "再派", "下一批要点": ["CPU 核心数是 4", "内存容量是 16GB"]}
    tree = {"root": root, "root.c2": p}
    def _stub3(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        return "{}"
    timed = leafrun.TimedRunner(_stub3)
    d = leafrun.TreeDriver(p, tree, TABLE, run_id="t-close-b3",
                           run_dir=str(run_dir), act_runner=timed, timed=timed)
    d.recovery = rec_mod.RecoveryController(str(run_dir))
    knobs = dict(role_mod.effective("决策", None, table=TABLE))
    knobs["max_children_per_round"] = 5
    out = d.spawn_children(p, {"children": [
        {"role": "调研", "context": "调查 CPU 核心数", "验收标准": "有数字", "auth": "只读", "judge": {"chars_min": 5}},
        {"role": "调研", "context": "调查内存容量", "验收标准": "有数字", "auth": "只读", "judge": {"chars_min": 5}}]}, knobs)
    c1, c2 = out[0], out[1]
    for cid in (c1, c2):
        e = tr.trust_entry({"id": cid, "role": "调研", "state": "完成"}, "complete", "完成", [],
                           {"calls": 1, "successes": 1, "failures": 0}, "sid", [], [])
        e["kind"] = "research"
        tr.append_trust(str(run_dir), e)
    # 子 2 报告：子 1 的前提 P-root.c2-1 被证伪（交叉证据，区分登记依赖与回落）
    leaf2 = d.tree[c2]
    leaf2["未决"] = [{"type": "冲突", "what": "前提被证伪：P-root.c2-r2-1：实测 12 核", "why": "sysctl -n hw.ncpu 返回 12"}]
    sched = leafrun.Scheduler(str(run_dir), "t-close-b3", TABLE, "t", act_runner=timed)
    sched.tree = d.tree
    sched._handle_leaf_failure_signals(c2, leaf2, {})
    t = tr.read_trust(str(run_dir))
    check("B3 falsify marks the REGISTERED dependent (child1), not the reporter",
          t.get(c1, {}).get("trust_status") == "suspect", t.get(c1))
    check("B3 falsify leaves the reporter unmarked (not a dependent)",
          "trust_status" not in t.get(c2, {}), t.get(c2))
    check("B3 parent gets a replan request for the falsified premise",
          (p.get("_replan_request") or {}).get("premise_id") == "P-root.c2-r2-1", p.get("_replan_request"))
    p["_tree"] = d.tree
    mknobs = role_mod.effective("决策", "merge", table=TABLE)
    mp = driver_mod.build_prompt(p, "merge", mknobs, TABLE, first_round=False)
    check("B3 merge prompt tells the parent to replan the affected subtree",
          "P-root.c2-r2-1" in mp and "再派" in mp, mp[-600:])

# ---------- B4: 真实 run_leaf 路径（复核 F1 回归） ----------
# F1 教训：B2 只测了 assemble_prompt（手动伪造 _run_dir），真实 run_leaf 从不给
# 节点打 _run_dir，注入永远跳过。此条走真实 run_leaf + 捕获桩 runner 收到的 prompt，
# 断言注入确实出现，且节点被打上 _run_dir。
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    (Path(tmp) / "sess").mkdir(exist_ok=True)
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(Path(tmp) / "sess")
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None, 验收标准="", auth="")
    p = node_mod.new_node("root.c2", "决策", parent="root", context="决策章", batch=4, 验收标准="x", auth="")
    p["round"] = 2
    p["merge"] = {"去向": "再派", "下一批要点": ["CPU 核心数是 4", "内存容量是 16GB"]}
    tree = {"root": root, "root.c2": p}
    captured = []
    def _stub4(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        captured.append(prompt)
        return json.dumps({"reply": "x" * 60, "未决": []}, ensure_ascii=False)
    timed = leafrun.TimedRunner(_stub4)
    d = leafrun.TreeDriver(p, tree, TABLE, run_id="t-close-b4",
                           run_dir=str(run_dir), act_runner=timed, timed=timed)
    knobs = dict(role_mod.effective("决策", None, table=TABLE))
    knobs["max_children_per_round"] = 5
    out = d.spawn_children(p, {"children": [
        {"role": "调研", "context": "调查 CPU 核心数", "验收标准": "有数字", "auth": "只读", "judge": {"chars_min": 5}},
        {"role": "调研", "context": "调查内存容量", "验收标准": "有数字", "auth": "只读", "judge": {"chars_min": 5}}]}, knobs)
    child1 = d.tree[out[0]]
    child1["state"] = "待跑"
    err = None
    try:
        leafrun.run_leaf(d, child1)
    except Exception as exc:  # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, exc)
    prompt4 = captured[0] if captured else ""
    check("B4 run_leaf stamps _run_dir on the node (F1 fix)",
          child1.get("_run_dir") == str(run_dir), child1.get("_run_dir"))
    check("B4 real run_leaf prompt carries the dependent premise",
          "P-root.c2-r2-1" in prompt4 and "CPU 核心数是 4" in prompt4
          and "前提被证伪" in prompt4 and err is None,
          (prompt4[-400:] if prompt4 else "NO PROMPT CAPTURED", err))

# ---------- B5: F2 回归——中间子被过滤（空叶）时前提不错位 ----------
# children [空, B, C] + 要点 [p1, p2, p3]：空子被拒不登记；B 拿 p2（r2-2）、C 拿 p3（r2-3），
# p1 不静默挂到 B 身上，也不丢失无声。
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None, 验收标准="", auth="")
    p = node_mod.new_node("root.c2", "决策", parent="root", context="决策章", batch=4, 验收标准="x", auth="")
    p["round"] = 2
    p["merge"] = {"去向": "再派", "下一批要点": ["CPU 核心数是 4", "内存容量是 16GB", "磁盘是 512GB"]}
    tree = {"root": root, "root.c2": p}
    def _stub5(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        return "{}"
    timed = leafrun.TimedRunner(_stub5)
    d = leafrun.TreeDriver(p, tree, TABLE, run_id="t-close-b5",
                           run_dir=str(run_dir), act_runner=timed, timed=timed)
    knobs = dict(role_mod.effective("决策", None, table=TABLE))
    knobs["max_children_per_round"] = 5
    out = d.spawn_children(p, {"children": [
        {"role": "调研", "context": "", "验收标准": "", "auth": "只读", "judge": None},
        {"role": "调研", "context": "调查内存容量", "验收标准": "有数字", "auth": "只读", "judge": {"chars_min": 5}},
        {"role": "调研", "context": "调查磁盘容量", "验收标准": "有数字", "auth": "只读", "judge": {"chars_min": 5}}]}, knobs)
    prems5 = premise_mod.read_premises(str(run_dir))
    check("B5 filtered empty child creates no premise for its slot",
          "P-root.c2-r2-1" not in prems5 and len(out) == 2, (list(prems5), out))
    check("B5 surviving children keep their original premise indices",
          prems5.get("P-root.c2-r2-2", {}).get("statement", "").startswith("内存容量是 16GB")
          and prems5.get("P-root.c2-r2-2", {}).get("dependents") == [out[0]]
          and prems5.get("P-root.c2-r2-3", {}).get("statement", "").startswith("磁盘是 512GB")
          and prems5.get("P-root.c2-r2-3", {}).get("dependents") == [out[1]], prems5)

print("GATES: %d/%d PASS" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("FAILURES: " + ", ".join(FAIL))
    sys.exit(1)
