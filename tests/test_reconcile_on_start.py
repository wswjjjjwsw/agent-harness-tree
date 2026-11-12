#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F3：启动 reconcile 开关（TREE_RECONCILE_ON_START，默认关）。

跑法：cd <候选根> && python3 tests/test_reconcile_on_start.py
离线：本地桩 HTTP 服务（cases-revival.py 同款做法），绝不打生产 8903。
桩任务的 state 用 LLAAT 真实中文值（完成/失败/在跑）：truth_from_task 只认这些，
英文 done 不会被认成完成（cases-revival.py 的桩用 done 只够验 round/act）。

覆盖：
  R1 开关关（缺省/0/yes/空）⇒ 零调用、零改动（快照字节不变、不新建 recover.jsonl、桩零请求）
  R2 开关开 + 桩返回「完成」⇒ 节点状态被补落，快照回写，recover.jsonl 有行
  R3 开关开 + 桩 404 ⇒ 节点标失败
  R4 开关开 + 无 TREE_LLAAT_URL ⇒ 跳过并记一行，快照不变、桩零请求
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from harness import node as node_mod, recover  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:400]) if extra and not cond else ""))


class _Handler(BaseHTTPRequestHandler):
    TASKS = {}
    HITS = 0

    def do_GET(self):  # noqa: N802
        type(self).HITS += 1
        if self.path.startswith("/task/"):
            tid = self.path[len("/task/"):].split("?")[0]
            t = self.TASKS.get(tid)
            body = json.dumps(t if t else {"error": "没有这条任务"}, ensure_ascii=False).encode()
            self.send_response(200 if t else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def _start_stub(tasks):
    _Handler.TASKS = tasks
    _Handler.HITS = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def _mk_tree(d, llaat_id="T-ROOT-1", state="在跑"):
    tr = {"root": node_mod.new_node("root", "根", llaat_id=llaat_id, state=state,
                                    round=1, act="plan", batch=1)}
    recover.dump_snapshot(tr, d=str(d))
    return tr


class _Env:
    """临时改环境变量，用完恢复（值 None ⇒ 临时删掉）。"""
    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _snap_bytes(d):
    p = Path(d) / "state.json"
    return p.read_bytes() if p.exists() else None


def _recover_rows(d):
    p = Path(d) / "recover.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------- R1：开关关 ⇒ 零调用、零改动 ----------
for _val in (None, "0", "yes", ""):
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        srv, url = _start_stub({"T-ROOT-1": {"id": "T-ROOT-1", "state": "完成",
                                             "judge_ok": True, "judge_why": "",
                                             "prompt": "", "tags": {}}})
        try:
            _mk_tree(d)
            before = _snap_bytes(d)
            with _Env(TREE_RECONCILE_ON_START=_val, TREE_LLAAT_URL=url):
                res = recover.maybe_reconcile_on_start(run_id="r1", d=str(d))
            check("R1 off(%r): enabled False, queries 0" % _val,
                  res.get("enabled") is False and res.get("queries") == 0, res)
            check("R1 off(%r): snapshot bytes unchanged" % _val,
                  _snap_bytes(d) == before, "")
            check("R1 off(%r): no recover.jsonl created" % _val,
                  not (d / "recover.jsonl").exists(), "")
            check("R1 off(%r): stub got 0 hits" % _val, _Handler.HITS == 0, _Handler.HITS)
        finally:
            srv.shutdown()

# ---------- R2：开关开 + 桩返回「完成」⇒ 补落 + 回写 ----------
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    srv, url = _start_stub({"T-ROOT-1": {"id": "T-ROOT-1", "state": "完成",
                                         "judge_ok": True, "judge_why": "",
                                         "prompt": "", "tags": {}}})
    try:
        _mk_tree(d)
        with _Env(TREE_RECONCILE_ON_START="1", TREE_LLAAT_URL=url):
            res = recover.maybe_reconcile_on_start(run_id="r2", d=str(d))
        after = recover.load_snapshot(str(d / "state.json"))
        rows = _recover_rows(d)
        check("R2 on+done: enabled, queries 1", res.get("enabled") is True and res.get("queries") == 1, res)
        check("R2 on+done: snapshot node state now 完成",
              (after.get("root") or {}).get("state") == "完成", after.get("root"))
        check("R2 on+done: recover.jsonl has rows", len(rows) >= 1, rows)
        check("R2 on+done: stub got 1 hit", _Handler.HITS == 1, _Handler.HITS)
    finally:
        srv.shutdown()

# ---------- R2b：开关开认 "true"（不只认 "1"）----------
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    srv, url = _start_stub({"T-ROOT-1": {"id": "T-ROOT-1", "state": "完成",
                                         "judge_ok": True, "judge_why": "",
                                         "prompt": "", "tags": {}}})
    try:
        _mk_tree(d)
        with _Env(TREE_RECONCILE_ON_START="true", TREE_LLAAT_URL=url):
            res = recover.maybe_reconcile_on_start(run_id="r2b", d=str(d))
        after = recover.load_snapshot(str(d / "state.json"))
        check("R2b on(true)+done: enabled, node 完成",
              res.get("enabled") is True and (after.get("root") or {}).get("state") == "完成", res)
    finally:
        srv.shutdown()

# ---------- R3：开关开 + 桩 404 ⇒ 标失败 ----------
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    srv, url = _start_stub({})  # 桩里没有这个 id ⇒ 404
    try:
        _mk_tree(d, llaat_id="T-NOPE")
        with _Env(TREE_RECONCILE_ON_START="1", TREE_LLAAT_URL=url):
            res = recover.maybe_reconcile_on_start(run_id="r3", d=str(d))
        after = recover.load_snapshot(str(d / "state.json"))
        rows = _recover_rows(d)
        check("R3 on+404: failed 1, node 失败",
              res.get("failed") == 1 and (after.get("root") or {}).get("state") == "失败", res)
        check("R3 on+404: recover.jsonl says 标失败",
              bool(rows) and rows[0].get("结论") == "标失败", rows)
    finally:
        srv.shutdown()

# ---------- R4：开关开 + 无 TREE_LLAAT_URL ⇒ 跳过并记账，不打生产 ----------
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    srv, url = _start_stub({"T-ROOT-1": {"id": "T-ROOT-1", "state": "完成",
                                         "judge_ok": True, "judge_why": "",
                                         "prompt": "", "tags": {}}})
    try:
        _mk_tree(d)
        before = _snap_bytes(d)
        with _Env(TREE_RECONCILE_ON_START="1", TREE_LLAAT_URL=None):
            res = recover.maybe_reconcile_on_start(run_id="r4", d=str(d))
        rows = _recover_rows(d)
        check("R4 on+no-url: skipped no_url", res.get("skipped") == "no_url", res)
        check("R4 on+no-url: snapshot bytes unchanged", _snap_bytes(d) == before, "")
        check("R4 on+no-url: recover.jsonl has one 跳过 row",
              len(rows) == 1 and rows[0].get("结论") == "跳过", rows)
        check("R4 on+no-url: stub got 0 hits (never touched production)",
              _Handler.HITS == 0, _Handler.HITS)
    finally:
        srv.shutdown()

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: %s" % ", ".join(FAIL))
    sys.exit(1)
