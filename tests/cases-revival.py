#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree-03 会话复活 + 恢复对账 —— 全部用例（由 tests/run-revival.sh 调起）。

★ 本文件是**唯一**允许打真模型的地方（真 pi，串行，总真实调用数 ≤ 12、单次超时 300s）。
  凡不真跑的用例（reconcile / overflow / 串行自证）都标 [桩]，与真跑读数分节。
★ 真跑读数只用来证「复活成立 + 不编造 + 轮次流水」，**不作** 76 §八 的单价替换值。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TREE_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))

from harness import driver, gate, node as node_mod, recover, role, session, submit  # noqa: E402

TMP = HERE / "tmp" / "tree03" / "revival"
CASES, NC = [], []
MARK = "ZQ-7391"                     # owner 实测用过的编号（照抄，便于对照）
CODE = "琥珀-七"                      # 第二段记忆，用于「第二轮必须看到第一轮原文」的强证据
REAL = {"calls": 0, "t0": None, "t1": None, "usage": []}


def case(name, negative_control=False, stub=True):
    def deco(fn):
        CASES.append((name, fn, stub))
        if negative_control:
            NC.append(name)
        return fn
    return deco


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def mkd(name: str) -> Path:
    d = TMP / "runs" / name
    if d.exists():
        shutil.rmtree(d)
    (d / "doc").mkdir(parents=True, exist_ok=True)
    return d


def vio_of(run_dir, kind: str = None) -> list:
    """读该 run 目录的 violations.jsonl（按 kind 过滤）。"""
    rows = gate.read_violations(str(Path(run_dir) / "violations.jsonl"))
    return [r for r in rows if kind is None or r["kind"] == kind]


# ---------------------------------------------------------------- 桩 LLAAT
class _Handler(BaseHTTPRequestHandler):
    TASKS = {}

    def do_GET(self):                                        # noqa: N802
        if self.path.startswith("/task/"):
            tid = self.path[len("/task/"):].split("?")[0]
            t = self.TASKS.get(tid)
            body = json.dumps(t if t else {"error": "没有这条任务"},
                              ensure_ascii=False).encode()
            self.send_response(200 if t else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):                               # 静音
        pass


def start_stub_llaat(tasks: dict):
    _Handler.TASKS = tasks
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def count_get(url: str) -> int:
    """抽查桩服务收到几次查询：用打点计数（本文件不走网络日志，改由 recover 的 queries 计数自证）。"""
    return 0


# ================================================================ 1 对账（桩）
@case("reconcile_round_act: 「已派 3 轮」的父重启后接着 round=3 ⇒ 第一次 act 是 4")
def t_recover_round(_):
    d0 = mkd("recover.round")
    tasks = {"T-ROOT-1": {"id": "T-ROOT-1", "state": "done", "judge_ok": True, "judge_why": "",
                          "prompt": "## 本轮输入（act=merge / round=3；L0 契约在你的会话里）",
                          "tags": {"node_id": "root", "act": "merge", "round": 3, "batch": 3}}}
    srv, url = start_stub_llaat(tasks)
    try:
        tr = {"root": node_mod.new_node("root", "根", llaat_id="T-ROOT-1",
                                        state="在跑", round=1, act="plan", batch=1)}
        recover.dump_snapshot(tr, d=str(d0))
        res = recover.reconcile(tr, url=url, d=str(d0), run_id="recover.round")
        rows = recover.read_recover(res["recover_log"])
        print("      查询次数=%d（只查一次/节点）  recover.jsonl 行数=%d" % (res["queries"], len(rows)))
        for r in rows:
            print("        node=%s 对得上=%s 改了=%s 从[%s]到[%s] 结论=%s" % (
                r["node_id"], r["对得上的字段"], r["改了哪个字段"], r["从"], r["到"], r["结论"]))
        print("      对账后 node: state=%s round=%s act=%s batch=%s" % (
            tr["root"]["state"], tr["root"]["round"], tr["root"]["act"], tr["root"]["batch"]))
        # 重启后第一次 act：应当是 merge 的第 4 轮，而不是第 1 轮
        dd = driver.Driver(tr["root"], tr, run_id="recover.round", run_dir=str(d0),
                           act_seq=["merge"])
        row = dd.step()
        print("      重启后第一次 act: act=%s round=%s（不是 1）" % (row.get("act"), row.get("round")))
        vio = gate.read_violations(str(d0 / "violations.jsonl"))
        vio_round = [v for v in vio if v["kind"] == "状态机_round"]
        print("      违规笔数=%d（其中状态机_round=%d；round 若被当成 1 会在这里抓出「回退」）"
              % (len(vio), len(vio_round)))
        check(res["queries"] == 1, "应只查一次，实际 %d" % res["queries"])
        check(tr["root"]["round"] == 3 and tr["root"]["act"] == "merge",
              "父的 round/act 没按真值补落：%s" % tr["root"])
        check(row.get("round") == 4, "重启后第一次 act 的 round 应为 4，实际 %s" % row.get("round"))
        check(not vio_round, "出现了状态机_round 违规 ⇒ round 被当成回退：%s" % vio_round)
    finally:
        srv.shutdown()


@case("neg_ctl_reconcile_404: 查不到的任务 ⇒ 标失败（不是完成、不是静默跳过）",
      negative_control=True)
def t_recover_404(_):
    d0 = mkd("recover.404")
    srv, url = start_stub_llaat({})                          # 桩里没有这个 id ⇒ 404
    try:
        tr = {"root": node_mod.new_node("root", "根", llaat_id="T-NOPE", state="在跑")}
        recover.dump_snapshot(tr, d=str(d0))
        res = recover.reconcile(tr, url=url, d=str(d0), run_id="recover.404")
        rows = recover.read_recover(res["recover_log"])
        for r in rows:
            print("        node=%s 结论=%s 从[%s]到[%s] why=%s" % (
                r["node_id"], r["结论"], r["从"], r["到"], r.get("why")))
        print("      标失败数=%d  对账后 state=%s" % (res["failed"], tr["root"]["state"]))
        check(res["failed"] == 1 and tr["root"]["state"] == "失败",
              "查不到应标失败：%s" % tr["root"]["state"])
        check(rows and rows[0]["结论"] == "标失败", "recover.jsonl 应记「标失败」")
    finally:
        srv.shutdown()


@case("session_id_rule: 一节点一 id、跨轮不变；不同节点不同 id")
def t_sid(_):
    a1 = session.session_id("req1", "root")
    a2 = session.session_id("req1", "root")
    b = session.session_id("req1", "root.c2.1")
    c = session.session_id("req2", "root")
    print("      同节点两次=%s / %s 相同=%s" % (a1, a2, a1 == a2))
    print("      不同节点=%s 不同=%s；不同 req=%s 不同=%s" % (b, b != a1, c, c != a1))
    check(a1 == a2 == "tree-req1-root" and b != a1 and c != a1, "session_id 规则不成立")


@case("agent_dir_isolated: 配置面只在树内 runs/pi-agent，绝不落 ~/.pi/agent")
def t_agentdir(_):
    a, s = session.ensure_agent_dir()
    mj = json.loads(Path(a, "models.json").read_text(encoding="utf-8"))
    sj = json.loads(Path(a, "settings.json").read_text(encoding="utf-8"))
    prov = mj["providers"]["bonsai"]
    argv = session.build_argv({"id": "root"}, "merge", role.effective("根", "merge"), "P", "SID")
    home_pi = os.path.expanduser("~/.pi/agent")
    print("      agent_dir=%s" % a)
    print("      session_dir=%s" % s)
    print("      models.json: provider=%s model=%s thinkingLevelMap=%s" % (
        list(mj["providers"]), prov["models"][0]["id"], prov["models"][0]["thinkingLevelMap"]))
    print("      settings.json=%s" % sj)
    print("      argv=%s" % " ".join(argv[:8] + ["<prompt>"]))
    print("      落点是否在树内=%s；是否等于生产目录 %s = %s" % (
        str(ROOT) in a, home_pi, a == home_pi))
    check(a.startswith(str(ROOT)), "配置面不在树内：%s" % a)
    check(a != home_pi and s != home_pi, "配置面指到了生产 ~/.pi/agent")
    check(sj == {"compaction": {"enabled": False}}, "settings.json 必须关自动压缩")
    check(argv[0] == "pi" and argv[1:5] == ["-p", "--no-tools", "--mode", "text"],
          "argv 不符模板：%s" % argv)
    check("--session-id" in argv and "SID" in argv, "argv 里没有 --session-id")


@case("overflow_loud: history + 本轮 > ctx_budget ⇒ 响亮失败（绝不静默截断历史）")
def t_overflow(_):
    sid = "tree-revival-overflow-node"
    hist = session.history_tokens(sid, str(TMP / "pi-agent" / "sessions"))
    saved = os.environ.pop("TREE_ACT_CMD", None)
    try:
        kw = role.effective("根", "merge")
        knobs = dict(kw)
        knobs["ctx_budget"] = 50                             # 故意小到必溢出
        raised = ""
        try:
            session.run_act({"id": "node-of-overflow"}, "merge", knobs, "本轮输入" * 40,
                            "tree-revival-overflow-node",
                            run_dir=str(TMP / "runs" / "overflow"))
        except session.ContextOverflow as exc:
            raised = str(exc)
        except Exception as exc:                             # noqa: BLE001
            raised = "WRONG:%s: %s" % (type(exc).__name__, exc)
        print("      history_tokens=%d  本轮估算=%d  ctx_budget=50" % (
            hist, submit.estimate_tokens("本轮输入" * 40)))
        print("      抛出的错误=%s" % raised[:200])
        check(raised.startswith("history_tokens"), "应抛 ContextOverflow，实际 %s" % raised[:80])
    finally:
        if saved:
            os.environ["TREE_ACT_CMD"] = saved


# ================================================================ 2 真跑：复活
def _real_call(role_name: str, act: str, prompt: str, sid: str, tag: str) -> dict:
    """真跑一次（串行、计时、记录 usage）。失败**如实抛**。"""
    kw = role.effective(role_name, act)
    t0 = time.time()
    real_calls_guard()
    out = session.run_act({"id": "probe"}, act, kw, prompt, sid,
                          run_dir=str(TMP / "runs" / "real"))
    dt = time.time() - t0
    recs = session.read_session(sid, str(TMP / "pi-agent" / "sessions"))
    last = [r for r in recs if r.get("role") == "assistant"]
    usage = (last[-1].get("usage") or {}) if last else {}
    REAL["usage"].append({"tag": tag, "sid": sid, "seconds": round(dt, 1),
                          "input": usage.get("input"), "output": usage.get("output")})
    print("      [真跑 %s] %.1fs  会话记录 %d 条  usage.input=%s output=%s" % (
        tag, dt, len(recs), usage.get("input"), usage.get("output")))
    return {"out": out, "records": recs, "usage": usage, "seconds": dt}


def real_calls_guard():
    REAL["calls"] += 1
    if REAL["calls"] > 12:
        raise RuntimeError("真跑调用数超过 12 次上限（%d）" % REAL["calls"])


@case("real_pi_revival: 同 session-id 跨两次真 pi ⇒ 记得住；第一轮原文逐字节仍在",
      stub=False)
def t_real_revival(_):
    sess = str(TMP / "pi-agent" / "sessions")
    sid = "tree-revival-prov-root"
    shutil.rmtree(str(TMP / "pi-agent"), ignore_errors=True)   # 只清树内自己的
    p1 = "记住编号 %s 与口令 %s。只回复：已记住。" % (MARK, CODE)
    r1 = _real_call("根", "plan", p1, sid, "r1 记忆")
    print("      [真跑 r1 输出] %s" % json.dumps(r1["out"], ensure_ascii=False)[:300])
    hist1 = session.history_tokens(sid, sess)
    p2 = "我上一轮让你记的编号是什么？只回答编号本身。"
    r2 = _real_call("根", "merge", p2, sid, "r2 回忆")
    print("      [真跑 r2 输出] %s" % json.dumps(r2["out"], ensure_ascii=False)[:300])
    hist2 = session.history_tokens(sid, sess)
    recs = session.read_session(sid, sess)
    roles = session.roles_seen(recs)
    path = session.find_session_file(sid, sess)
    # 保真：第一轮 user 那段的原文逐字节仍在；第二轮是独立一条 user 记录
    msg_users = [r for r in recs if r.get("type") == "message" and r.get("role") == "user"]
    sent = json.dumps([{"type": "text", "text": p1}], ensure_ascii=False)
    hit = [i + 1 for i, r in enumerate(recs)
           if r.get("type") == "message" and r.get("role") == "user"
           and p1 in (r.get("raw") or "")]
    hit2 = [i + 1 for i, r in enumerate(recs)
            if r.get("type") == "message" and r.get("role") == "user"
            and p2 in (r.get("raw") or "")]
    print("      会话文件=%s  行数=%d" % (path, len(recs)))
    print("      出现的角色（首见顺序）=%s" % roles)
    print("      记录类型清单=%s" % sorted({r["type"] for r in recs}))
    print("      会话里 message/user 记录数=%d（两轮各一条）" % len(msg_users))
    print("      第一轮 user 原文命中行号=%s（grep -c 计数=%d）" % (hit, len(hit)))
    print("      第二轮 user 原文命中行号=%s（grep -c 计数=%d）" % (hit2, len(hit2)))
    # 逐字节比对：把会话里第一轮 user 的正文抽出来，与发出去的 p1 逐字节比（md5 + 全长）
    import hashlib
    first_user = [r for r in recs if r.get("type") == "message" and r.get("role") == "user"][0]
    content = first_user.get("content")
    got_text = ""
    if isinstance(content, list) and content and isinstance(content[0], dict):
        got_text = str(content[0].get("text") or "")
    md_sent = hashlib.md5(p1.encode("utf-8")).hexdigest()
    md_got = hashlib.md5(got_text.encode("utf-8")).hexdigest()
    md_line = hashlib.md5((first_user.get("raw") or "").encode("utf-8")).hexdigest()
    print("      逐字节比对：发出 md5=%s（%d 字节） 会话抽出 md5=%s（%d 字节） 相同=%s" % (
        md_sent, len(p1.encode("utf-8")), md_got, len(got_text.encode("utf-8")), p1 == got_text))
    print("      第一轮 user 那条记录的整行 md5=%s（%d 字节）" % (
        md_line, len((first_user.get("raw") or "").encode("utf-8"))))
    print("      usage.input 曲线：第一轮=%s ⇒ 第二轮=%s（必须变大）" % (hist1, hist2))
    print("      第二轮 output 里含编号 %s = %s" % (MARK, MARK in r2["out"]))
    check(r1["out"].strip(), "第一次真跑没有输出")
    check(len(hit) == 1, "第一轮 user 原文没在会话里逐字节保住：%s" % hit)
    check(p1 == got_text, "第一轮 user 正文与会话里抽出的不是逐字节相同")
    check(md_sent == md_got, "md5 不一致：%s / %s" % (md_sent, md_got))
    check(len(hit2) == 1 and len(msg_users) == 2,
          "会话里 message/user 记录应恰 2 条（两轮各一条）⇒ 实测 %d 条" % len(msg_users))
    check(roles[:1] == ["system"] and "user" in roles and "assistant" in roles,
          "角色区分不成立：%s" % roles)
    check("tool" not in roles, "本会话不该出现 tool（--no-tools）：%s" % roles)
    check(hist2 > hist1, "第二轮 usage.input 应比第一轮大：%s → %s" % (hist1, hist2))
    check(MARK in r2["out"], "复活没成立（第二轮输出没回忆起 %s）：%r" % (MARK, r2["out"][:200]))


@case("real_pi_control_no_history: 新 session-id（等价于不给历史）⇒ 记录模型是否编造",
      stub=False)
def t_real_control(_):
    sess = str(TMP / "pi-agent" / "sessions")
    sid = "tree-revival-prov-control"
    p = "我上一轮让你记的编号是什么？只回答编号本身。"
    r = _real_call("根", "merge", p, sid, "control 无历史")
    print("      [真跑对照输出] %s" % json.dumps(r["out"], ensure_ascii=False)[:400])
    recs = session.read_session(sid, sess)
    print("      对照会话行数=%d  usage.input=%s" % (len(recs), r["usage"].get("input")))
    hit = sum(1 for x in recs if MARK in (x.get("raw") or ""))
    print("      对照会话里出现编号 %s 的次数=%d（应为 0）" % (MARK, hit))
    check(r["out"].strip(), "对照真跑没有输出")
    check(hit == 0, "对照会话里不该出现编号（那说明串了会话）")
    check(REAL["usage"][0]["input"] and r["usage"].get("input") is not None,
          "两次都要有 usage.input 真值")


@case("real_pi_act_round: 真跑一个 merge 契约轮（走 harness/session.py 的完整接线）", stub=False)
def t_real_act(_):
    d0 = mkd("real.act")
    tr = {"root": node_mod.new_node("root", "根", round=2, act="spawn", batch=2,
                                    state="等子节点",
                                    context="把第 1 段写成 3 行中文说明。",
                                    judge={"chars_min": 1})}
    tr["root.c2.1"] = node_mod.new_node("root.c2.1", "执行", parent="root", batch=2,
                                        state="完成", reply="第 1 段已写完（桩子）",
                                        未决=[], judge_ok=True, judge_why="")
    captured = {}

    def wrap(node, act, knobs, prompt, sid, run_dir):        # noqa: ANN001
        out = session.run_act(node, act, knobs, prompt, sid, run_dir=run_dir)
        captured["raw"] = out
        return out

    REAL["calls"] += 1
    d = driver.Driver(tr["root"], tr, run_id="real.act", run_dir=str(d0), act_runner=wrap)
    t0 = time.time()
    row = d.step()
    dt = time.time() - t0
    sid = row.get("session_id")
    recs = session.read_session(sid, str(TMP / "pi-agent" / "sessions"))
    last = [r for r in recs if r.get("role") == "assistant"]
    vio = vio_of(d0, "驱动_产出形状")
    print("      [真跑 act 轮] %.1fs  act=%s round=%s schema_ok=%s chars=%s" % (
        dt, row.get("act"), row.get("round"), row.get("schema_ok"), row.get("chars")))
    print("      模型正文前 300 字：%s" % json.dumps((captured.get("raw") or "")[:300],
                                                ensure_ascii=False))
    print("      why=%s" % row.get("why"))
    print("      session_id=%s 会话行数=%d usage.input=%s" % (
        sid, len(recs), (last[-1].get("usage") or {}).get("input") if last else None))
    print("      轮次流水行=%s" % json.dumps({k: row.get(k) for k in
                                          ("act", "round", "seconds", "schema_ok",
                                           "rounds_advanced", "chars", "rejected")},
                                         ensure_ascii=False))
    print("      违规（驱动_产出形状）%d 笔" % len(vio))
    # 判据（诚实口径）：这一轮**必须给出明确结论**，不许静默——
    #   要么过 schema（算数），要么被显式拒绝 + 落一笔违规（不算数），二者必居其一。
    explicit = (row.get("schema_ok") is True) or (bool(row.get("rejected")) and len(vio) == 1)
    print("      本轮结论是否明确（过 schema 或被响亮拒绝并记账）=%s" % explicit)
    check(recs, "真跑后应有会话记录")
    check(explicit, "这一轮既没过 schema 也没被显式拒绝 + 记账 ⇒ 静默通过（不合格）：%s" % row)
    if row.get("schema_ok") is False:
        check(tr["root"]["round"] == 2, "被拒的轮不许占 round：%s" % tr["root"]["round"])
        print("      [如实记录] 真跑这一次**没过契约**（输出非契约 JSON）—— 见报告「待裁决」节")


# ================================================================ 3 串行自证
PI_PEAK = {"peak": 0, "n": 0, "samples": []}


def _sampler(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            p = subprocess.run(["pgrep", "-x", "pi"], capture_output=True, text=True, timeout=5)
            ids = [x for x in p.stdout.strip().splitlines() if x]
            PI_PEAK["n"] += 1
            PI_PEAK["peak"] = max(PI_PEAK["peak"], len(ids))
            if ids:
                PI_PEAK["samples"].append(ids)
        except Exception:                                    # noqa: BLE001
            pass
        time.sleep(0.2)


@case("serial_peak_le_1: 真跑期间 pgrep -x pi 峰值 ≤ 1（bonsai 并发=1）")
def t_serial(_):
    peak = PI_PEAK["peak"]
    print("      采样 %d 次，命中样本 %d 次，峰值=%d（必须 ≤ 1）" % (
        PI_PEAK["n"], len(PI_PEAK["samples"]), peak))
    print("      命中样本前 3 条=%s" % PI_PEAK["samples"][:3])
    ps = subprocess.run(["bash", "-c",
                         "ps -eo pid,etime,comm,args | awk '$3==\"pi\"' | head -3"],
                        capture_output=True, text=True).stdout.strip()
    ps_now = subprocess.run(["bash", "-c",
                             "ps -eo pid,etime,comm,args | grep -c \"[ /]pi \" || true"],
                            capture_output=True, text=True).stdout.strip()
    print("      收尾 ps comm=pi 行=%r（真跑已结束，应为空）" % ps)
    print("      ps 里含 ' pi ' 的行数=%s" % ps_now)
    print("      真跑调用数=%d（上限 12）  真跑总耗时≈%.0fs" % (REAL["calls"], sum(
        u["seconds"] for u in REAL["usage"])))
    check(peak <= 1, "峰值 %d > 1 ⇒ 出现并发 pi" % peak)
    check(PI_PEAK["n"] > 20, "采样次数太少：%d" % PI_PEAK["n"])


# ================================================================ 跑
def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    os.environ["TREE_PI_AGENT_DIR"] = str(TMP / "pi-agent")
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(TMP / "pi-agent" / "sessions")
    os.environ["TREE_RUNS_DIR"] = str(TMP / "runs")
    os.environ.pop("TREE_ACT_CMD", None)                     # 本入口不打桩
    real_only = "--real-only" in sys.argv
    only = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")]
    stop = threading.Event()
    th = threading.Thread(target=_sampler, args=(stop,), daemon=True)
    th.start()
    passed, failed, ran = 0, [], []
    import yaml
    print("tree-03 revival · root=%s · python=%s · yaml=%s" % (
        ROOT, sys.version.split()[0], yaml.__version__))
    print("pi agent dir=%s（树内隔离；绝不落 ~/.pi/agent）" % os.environ["TREE_PI_AGENT_DIR"])
    print("真 pi 调用上限 12 次 · 单次超时 %.0fs" % session.SINGLE_CALL_TIMEOUT)
    for name, fn, stub in CASES:
        if real_only and stub:
            continue
        if only and not any(name.startswith(o) for o in only):
            continue
        ran.append(name.split(":")[0])
        try:
            fn(None)
            print("CASE %-32s PASS" % name.split(":")[0])
            passed += 1
        except Exception as exc:                              # noqa: BLE001
            failed.append(name)
            print("CASE %-32s FAIL: %s" % (name.split(":")[0], exc))
            if os.environ.get("TREE_GATES_TRACE"):
                traceback.print_exc()
    stop.set()
    th.join(timeout=3)
    total = len(ran)
    print("-" * 78)
    if failed:
        print("FAILED: %s" % ", ".join(failed))
    print("阴性对照 %d 条 · 真 pi 调用 %d 次" % (len(NC), REAL["calls"]))
    print("usage 曲线（真跑真值）：%s" % json.dumps(REAL["usage"], ensure_ascii=False))
    if total < 5 or (not real_only and len(NC) < 1):
        print("GATES: %d/%d FAIL（total<5 视为不合格）" % (passed, total))
        return 1
    if failed:
        print("GATES: %d/%d FAIL" % (passed, total))
        return 1
    print("GATES: %d/%d PASS" % (passed, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
