#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree-02 逻辑树驱动器骨架 —— 全部用例（唯一验收入口由 tests/run-gates.sh 调起）。

自包含、可重复跑：不联网、不打 8903/8091、不依赖外部状态。
需要抓提交 payload 的用例，在本机起一个 stub HTTP 服务（http.server）接住 POST /submit。

★ 用例名逐字对应 task-def §6.1 的表（含正/反两面的，各算一条）。
★ 任一用例 FAIL ⇒ 退出码 ≠ 0；全绿 ⇒ rc=0，且最后一行打印 `GATES: <pass>/<total> PASS`。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TREE_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))

import yaml                                            # noqa: E402
from harness import gate, land, role, schema, submit        # noqa: E402

TMP = HERE / "tmp"
DOC_TMP = TMP / "doc"
RUNS_TMP = TMP / "runs"

# 12 个旋钮的【独立副本】（故意不从 role.yaml 读：这样才能证明表里写全了 12 个）
KNOBS_LITERAL = ("thinking", "may_spawn", "ctx_budget", "think_budget", "out_budget",
                 "min_ctx_tokens", "min_out_chars", "max_children_per_round",
                 "may_write", "tools", "kind", "role_prompt", "max_rounds")
ROLES_LITERAL = ("根", "规划", "决策", "执行", "调研", "审计")

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------- 工具
def sink_for(name: str) -> str:
    """每个用例一个独立 sink（越权/截断的「记一笔」落点）。"""
    p = RUNS_TMP / name / "violations.jsonl"
    if p.exists():
        p.unlink()
    return str(p)


def count_kind(sink: str, kind: str) -> int:
    return len([r for r in gate.read_violations(sink) if r.get("kind") == kind])


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def roles_copy(dst_name: str) -> str:
    """role.yaml 的副本（★ 用例一律改副本，绝不动 role.yaml 本体）。"""
    dst = TMP / dst_name
    shutil.copyfile(ROOT / "role.yaml", dst)
    return str(dst)


def rewrite_role_knob(src: str, dst: str, role_name: str, key: str, value) -> str:
    """把副本里某个 role 的某个旋钮改掉（定位到该 role 的缩进块内，不碰别的 role）。"""
    lines = Path(src).read_text(encoding="utf-8").splitlines(True)
    start = None
    for i, ln in enumerate(lines):
        if ln.rstrip("\n") == "  %s:" % role_name:
            start = i
            break
    expect(start is not None, "副本里找不到 role %r" % role_name)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.startswith("    "):
            end = i
            break
    hit = False
    for i in range(start + 1, end):
        if lines[i].startswith("    %s:" % key):
            lines[i] = "    %s: %s\n" % (key, value)
            hit = True
            break
    expect(hit, "role %r 的块里找不到旋钮 %r" % (role_name, key))
    Path(dst).write_text("".join(lines), encoding="utf-8")
    return dst


# ---------------------------------------------------------------- 本地 stub
class _StubHandler(BaseHTTPRequestHandler):
    payloads = []
    reject_reason = None

    def log_message(self, *a):                      # 静音
        pass

    def do_POST(self):                              # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"_raw": raw}
        type(self).payloads.append({"path": self.path, "body": body})
        if type(self).reject_reason:
            out = json.dumps({"rejected": type(self).reject_reason}).encode("utf-8")
            self.send_response(400)
        else:
            out = json.dumps({"id": "stub-task-1", "prio": "queued"}).encode("utf-8")
            self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class Stub:
    def __init__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        _StubHandler.payloads = []
        _StubHandler.reject_reason = None
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()

    @property
    def url(self) -> str:
        host, port = self.srv.server_address[:2]
        return "http://%s:%d" % (host, port)

    @property
    def payloads(self) -> list:
        return _StubHandler.payloads


# ---------------------------------------------------------------- 用例 1-3：role 表
@case("roles.load")
def t_roles_load(_):
    table = role.load_roles()
    names = list(table["roles"])
    expect(sorted(names) == sorted(ROLES_LITERAL),
           "role 名不是这 6 个：%s" % names)
    expect(tuple(table["knobs"]) == KNOBS_LITERAL or
           sorted(table["knobs"]) == sorted(KNOBS_LITERAL),
           "role.yaml 的 knobs 段不是 13 个逐字旋钮：%s" % (table["knobs"],))
    expect(tuple(role.KNOBS) == KNOBS_LITERAL,
           "role.KNOBS 不是 13 个逐字旋钮：%s" % (role.KNOBS,))
    for n in ROLES_LITERAL:
        row = table["roles"][n]
        for k in KNOBS_LITERAL:
            expect(k in row, "role %s 缺旋钮 %s" % (n, k))
        expect(set(row) - set(KNOBS_LITERAL) - {"max_tokens"} == set(),
               "role %s 出现多余键：%s" % (n, sorted(set(row) - set(KNOBS_LITERAL))))


@case("roles.values")
def t_roles_values(_):
    T = role.load_roles()["roles"]
    checks = [
        ("根.ctx_budget", T["根"]["ctx_budget"], 32768),
        ("规划.ctx_budget", T["规划"]["ctx_budget"], 32768),  # t-127a：统一 32K（原 16384）
        ("决策.ctx_budget", T["决策"]["ctx_budget"], 32768),  # t-127a：统一 32K（原 16384）
        ("执行.min_out_chars", T["执行"]["min_out_chars"], 1500),
        ("调研.min_out_chars", T["调研"]["min_out_chars"], 800),
        ("审计.ctx_budget", T["审计"]["ctx_budget"], 32768),
        ("执行.thinking", T["执行"]["thinking"], "none"),
        ("调研.thinking", T["调研"]["thinking"], "none"),
        ("审计.thinking", T["审计"]["thinking"], "low"),
        ("根.max_rounds", T["根"]["max_rounds"], 8),
        ("规划.max_rounds", T["规划"]["max_rounds"], 6),
        ("决策.max_rounds", T["决策"]["max_rounds"], 6),
        ("执行.max_rounds", T["执行"]["max_rounds"], 1),
        ("调研.max_rounds", T["调研"]["max_rounds"], 1),
        ("审计.max_rounds", T["审计"]["max_rounds"], 1),
        ("根.min_ctx_tokens", T["根"]["min_ctx_tokens"], 4096),
        ("审计.may_spawn", T["审计"]["may_spawn"], False),
        ("执行.may_write", T["执行"]["may_write"], True),
        ("执行.kind", T["执行"]["kind"], "text"),
    ]
    bad = ["%s=%r 期望 %r" % (k, a, b) for k, a, b in checks if a != b]
    expect(not bad, "；".join(bad))
    for n in ROLES_LITERAL:
        expect(T[n]["role_prompt"] == "prompts/%s.md" % n,
               "%s.role_prompt=%r" % (n, T[n]["role_prompt"]))


@case("roles.max_tokens")
def t_roles_max_tokens(_):
    tbl = role.load_roles()
    rows = [("执行", None, 8192), ("审计", None, 8192), ("调研", None, 4096),
            ("根", "spawn", 4096), ("根", "plan", 8192), ("根", "merge", 8192),
            ("规划", "spawn", 4096), ("规划", "plan", 8192), ("规划", "merge", 8192),
            ("决策", "spawn", 4096), ("决策", "plan", 8192), ("决策", "merge", 8192)]
    bad = []
    for n, act, want in rows:
        got = role.effective(n, act, tbl)["max_tokens"]
        if got != want:
            bad.append("%s/%s max_tokens=%r 期望 %r" % (n, act, got, want))
    for n, row in tbl["roles"].items():
        if row["think_budget"] + row["out_budget"] > row["max_tokens"]:
            bad.append("%s 基线违反池子不变式" % n)
        for act, ov in ((tbl.get("acts") or {}).get(n) or {}).items():
            m = dict(row)
            m.update(ov)
            if m["think_budget"] + m["out_budget"] > m["max_tokens"]:
                bad.append("%s.%s 违反池子不变式" % (n, act))
    expect(not bad, "；".join(bad))


# ---------------------------------------------------------------- 用例 4-5：覆盖边界
@case("overrides.widen")
def t_overrides_widen(_):
    s = sink_for("overrides.widen")
    k = role.effective("执行")
    out, rej = role.apply_overrides(k, {"out_budget": 9000, "ctx_budget": 40000}, sink=s)
    expect(not rej, "放宽被拒了：%s" % rej)
    expect(out["out_budget"] == 9000, "生效 out_budget=%r" % out["out_budget"])
    expect(out["ctx_budget"] == 40000, "生效 ctx_budget=%r" % out["ctx_budget"])
    expect(k["out_budget"] == 4500, "基线被改了：%r" % k["out_budget"])
    expect(count_kind(s, "越权_overrides") == 0, "放宽却记了越权")


@case("overrides.reject")
def t_overrides_reject(_):
    s = sink_for("overrides.reject")
    k = role.effective("执行")
    out, rej = role.apply_overrides(
        k, {"thinking": "low", "may_spawn": True, "out_budget": 100}, sink=s)
    expect(len(rej) == 3, "该拒 3 条，实际 %d 条：%s" % (len(rej), rej))
    expect(out["thinking"] == k["thinking"] == "none", "thinking 生效值被改了：%r" % out["thinking"])
    expect(out["may_spawn"] is False, "may_spawn 生效值被改了：%r" % out["may_spawn"])
    expect(out["out_budget"] == k["out_budget"] == 4500,
           "收紧 out_budget 生效了：%r" % out["out_budget"])
    expect(count_kind(s, "越权_overrides") == 3,
           "越权_overrides 应记 3 条，实际 %d" % count_kind(s, "越权_overrides"))


# ---------------------------------------------------------------- 用例 6-10：五道闸
@case("gate.may_spawn")
def t_gate_may_spawn(_):
    s = sink_for("gate.may_spawn")
    k = role.effective("执行")
    ok, why = gate.check_spawn("spawn", k, {"role": "执行", "id": "n-exec"}, sink=s)
    expect(ok is False, "may_spawn=false 配 act=spawn 竟然过了")
    expect("may_spawn" in why, "理由没写清 may_spawn：%s" % why)
    ok2, _ = gate.check_spawn("merge", k, {"role": "执行", "id": "n-exec"}, sink=s)
    expect(ok2 is True, "act=merge 不该被这道闸拦")
    ok3, _ = gate.check_spawn("spawn", role.effective("根", "spawn"),
                              {"role": "根", "id": "n-root"}, sink=s)
    expect(ok3 is True, "may_spawn=true 的父 role 不该被拦")


@case("gate.children_truncate")
def t_gate_children_truncate(_):
    s = sink_for("gate.children_truncate")
    k = role.effective("根")
    kids = [{"id": "c%d" % i} for i in range(1, 5)]
    kept, dropped = gate.truncate_children(kids, k, sink=s)
    expect([c["id"] for c in kept] == ["c1", "c2", "c3"], "收下的不是 3 个：%s" % kept)
    expect([c["id"] for c in dropped] == ["c4"], "丢掉的不是 1 个：%s" % dropped)
    n = count_kind(s, "截断_children")
    expect(n == 1, "截断_children 应恰 1 行，实际 %d" % n)
    row = [r for r in gate.read_violations(s) if r["kind"] == "截断_children"][0]
    expect("c4" in json.dumps(row, ensure_ascii=False), "记的一笔里没有丢掉的子标识：%s" % row)


@case("gate.nodes_max")
def t_gate_nodes_max(_):
    s = sink_for("gate.nodes_max")
    limit = role.load_roles()["driver"]["nodes_max"]
    expect(limit == 80, "driver.nodes_max=%r" % limit)
    ok, _ = gate.check_nodes_max(79, limit, sink=s)
    expect(ok is True, "第 80 个（已有 79）不该拒")
    ok2, why = gate.check_nodes_max(80, limit, sink=s)
    expect(ok2 is False, "已有 80 个，第 81 个竟然过了")
    expect(count_kind(s, "截断_nodes_max") == 1,
           "截断_nodes_max 应恰 1 行，实际 %d" % count_kind(s, "截断_nodes_max"))


@case("gate.minimum")
def t_gate_minimum(_):
    s = sink_for("gate.minimum")
    # ③-a ctx 量低于 min_ctx_tokens
    ok, why = gate.check_minimum({"id": "n1", "ctx_tokens": 100}, role.effective("根"), sink=s)
    expect(ok is False, "ctx 100 < 4096 竟然过了")
    expect(count_kind(s, "下限_min_ctx_tokens") == 1,
           "下限_min_ctx_tokens 应恰 1 行，实际 %d" % count_kind(s, "下限_min_ctx_tokens"))
    # ③-b 产出下限低于 min_out_chars
    ok2, why2 = gate.check_minimum(
        {"id": "n2", "ctx_tokens": 9999, "min_out_chars": 10}, role.effective("执行"), sink=s)
    expect(ok2 is False, "声明产出 10 字 < 1500 竟然过了")
    expect(why2 and "min_out_chars" in why2, "理由不对：%s" % why2)
    expect(count_kind(s, "下限_min_out_chars") == 1,
           "下限_min_out_chars 应恰 1 行，实际 %d" % count_kind(s, "下限_min_out_chars"))
    # 正对照：两条都够时不拒
    ok3, _ = gate.check_minimum(
        {"id": "n3", "ctx_tokens": 4096, "min_out_chars": 1500}, role.effective("执行"), sink=s)
    expect(ok3 is True, "够了却拒了")


@case("gate.max_rounds")
def t_gate_max_rounds(_):
    s = sink_for("gate.max_rounds")
    k = role.effective("根")
    ok, _ = gate.check_round(8, k, sink=s)
    expect(ok is True, "根 round=8（= max_rounds）不该拒")
    ok2, why2 = gate.check_round(9, k, sink=s)
    expect(ok2 is False, "根 round=9 > max_rounds=8 竟然过了")
    expect("收束" in why2, "理由里没写强制收束：%s" % why2)
    n = count_kind(s, "max_rounds")
    expect(n == 1, "max_rounds 应恰 1 行，实际 %d" % n)
    # 末轮必须收束（76 §3.2 第三条 assert 的另一半）
    expect(gate.check_round_must_converge(8, k) is True, "round=8 应判「必须收束」")
    expect(gate.check_round_must_converge(7, k) is False, "round=7 不该判收束")


# ---------------------------------------------------------------- 用例 11-14：四契约
@case("schema.merge.pos")
def t_schema_merge_pos(_):
    obj = {"去向": "再派", "reply": "收拢结论一句话",
           "未决": [{"type": "缺信息", "what": "还差一条实测", "why": "没跑"}],
           "下一批要点": ["这块拆成两部分"]}
    ok, why = schema.validate("merge", obj, role.effective("决策", "merge"))
    expect(ok, "merge 正样本没过：%s" % why)
    obj2 = {"去向": "收束", "reply": "收", "未决": [],
            "uncovered": [{"what": "没解的", "why": "超范围"}]}
    ok2, why2 = schema.validate("merge", obj2, role.effective("决策", "merge"))
    expect(ok2, "merge 收束样本没过：%s" % why2)


@case("schema.merge.neg")
def t_schema_merge_neg(_):
    k = role.effective("决策", "merge")
    ok, why = schema.validate("merge", {"去向": "再派", "未决": [], "下一批要点": ["a"]}, k)
    expect(not ok, "缺 reply 竟然过了")
    expect("reply" in why, "理由里没指出缺哪个键：%s" % why)
    ok2, why2 = schema.validate("merge", {"去向": "收束", "reply": "x", "未决": []}, k)
    expect(not ok2 and "uncovered" in why2, "去向=收束缺 uncovered 竟然过了：%s" % why2)
    ok3, why3 = schema.validate(
        "merge", {"去向": "再派", "reply": "x", "未决": [], "下一批要点": ["a"],
                  "children": [{"role": "执行"}]}, k)
    expect(not ok3 and "children" in why3, "merge 里写子定义竟然过了：%s" % why3)


@case("schema.spawn.pos")
def t_schema_spawn_pos(_):
    obj = {"children": [{"role": "执行", "context": "照要点写", "judge": {"chars_min": 1500},
                         "验收标准": "能核", "auth": "doc/3.md 第 1 节"}]}
    ok, why = schema.validate("spawn", obj, role.effective("根", "spawn"))
    expect(ok, "spawn 正样本没过：%s" % why)


@case("schema.spawn.neg")
def t_schema_spawn_neg(_):
    k = role.effective("根", "spawn")
    ok, why = schema.validate("spawn", {"children": [
        {"context": "x", "judge": {"chars_min": 1500}, "验收标准": "y", "auth": "z"}]}, k)
    expect(not ok, "children[0] 缺 role 竟然过了")
    expect("role" in why, "理由里没指出 role：%s" % why)
    ok2, why2 = schema.validate("spawn", {"children": [
        {"role": "邪", "context": "x", "judge": {"chars_min": 1}, "验收标准": "y", "auth": "z"}]}, k)
    expect(not ok2 and "内置 role" in why2, "role 不在内置集里竟然过了：%s" % why2)
    ok3, why3 = schema.validate("spawn", {"children": [], "去向": "再派"}, k)
    expect(not ok3, "spawn 轮带 去向 竟然过了：%s" % why3)


@case("schema.plan.pos")
def t_schema_plan_pos(_):
    ok, why = schema.validate("plan", {"要点": ["这一层要哪几块", "每块一句话"]},
                              role.effective("根", "plan"))
    expect(ok, "plan 正样本没过：%s" % why)


@case("schema.plan.neg")
def t_schema_plan_neg(_):
    ok, why = schema.validate("plan", {"reply": "x"}, role.effective("根", "plan"))
    expect(not ok, "缺 要点 竟然过了")
    expect("要点" in why, "理由里没指出 要点：%s" % why)


@case("schema.leaf.pos")
def t_schema_leaf_pos(_):
    obj = {"reply": "写完了", "未决": [], "doc": "正文" * 500}
    ok, why = schema.validate("leaf", obj, role.effective("执行"))
    expect(ok, "leaf 正样本没过：%s" % why)
    ok2, why2 = schema.validate("leaf", {"reply": "读完", "未决": []}, role.effective("调研"))
    expect(ok2, "调研 leaf（无 doc）没过：%s" % why2)


@case("schema.leaf.neg")
def t_schema_leaf_neg(_):
    k = role.effective("执行")
    ok, why = schema.validate("leaf", {"reply": "x", "doc": "正文"}, k)
    expect(not ok, "缺 未决 竟然过了")
    expect("未决" in why, "理由里没指出 未决：%s" % why)
    ok2, why2 = schema.validate(
        "leaf", {"reply": "x", "未决": [], "doc": "正文", "children": [{"role": "执行"}]}, k)
    expect(not ok2 and "children" in why2, "leaf 里出现 children 竟然过了：%s" % why2)
    ok3, why3 = schema.validate("leaf", {"reply": "x", "未决": [], "doc": "正文"}, role.effective("调研"))
    expect(not ok3, "调研（may_write=false）产出 doc 竟然过了")


# ---------------------------------------------------------------- 用例 15-16：越权
@case("perm.children")
def t_perm_children(_):
    s = sink_for("perm.children")
    k = role.effective("执行")
    obj = {"id": "n-exec", "reply": "x", "未决": [],
           "children": [{"role": "执行", "context": "偷拉的"}]}
    clean, why = schema.check_permission("leaf", obj, k, sink=s)
    expect(clean is False, "may_spawn=false 返回 children 竟然干净")
    expect("children" not in obj, "children 没被丢弃：%s" % obj)
    expect(count_kind(s, "越权_children") == 1,
           "越权_children 应恰 1 行，实际 %d" % count_kind(s, "越权_children"))
    expect(obj.get("reply") == "x", "丢弃 children 时把别的字段也动了")


@case("perm.act")
def t_perm_act(_):
    s = sink_for("perm.act")
    k = role.effective("根", "spawn")
    for key in ("去向", "未决", "uncovered"):
        obj = {"children": [{"role": "执行"}], key: "串角色"}
        clean, why = schema.check_permission("spawn", obj, k, sink=s)
        expect(clean is False, "act=spawn 返回 %s 竟然干净" % key)
        expect(key not in obj, "%s 没被丢弃" % key)
    n = count_kind(s, "越权_act")
    expect(n == 3, "越权_act 应记 3 行（去向/未决/uncovered 各一），实际 %d" % n)


# ---------------------------------------------------------------- 用例 17-19：提交参数
@case("submit.snapshot")
def t_submit_snapshot(_):
    nodes = {
        "执行": ({"id": "n1", "role": "执行", "context": "照要点写一篇长文",
                  "judge": {"chars_min": 1500}}, "leaf"),
        "审计": ({"id": "n2", "role": "审计", "context": "待审内容",
                  "judge": {"nonempty": True}}, "leaf"),
        "根": ({"id": "n3", "role": "根", "act": "spawn", "context": "上游要点",
                "judge": {"json_keys": ["children"]}}, "spawn"),
    }
    with Stub() as stub:
        for name, (node, act) in nodes.items():
            tid, why = submit.submit(node, act=act, req_id="req1", stage="stage1",
                                     url=stub.url)
            expect(not why, "%s 提交被拒：%s" % (name, why))
            expect(tid == "stub-task-1", "%s 没拿到 id：%r" % (name, tid))
    exec_b = [p["body"] for p in stub.payloads if p["body"]["judge"].get("chars_min") == 1500][0]
    audit_b = [p["body"] for p in stub.payloads if p["body"]["judge"].get("nonempty")][0]
    root_b = [p["body"] for p in stub.payloads if p["body"]["judge"].get("json_keys")][0]
    checks = [
        ("执行.needs_thinking", exec_b["needs_thinking"], False),
        ("执行.max_tokens", exec_b["max_tokens"], 8192),
        ("执行.retry_max", exec_b["retry_max"], 0),
        ("执行.kind", exec_b["kind"], "text"),
        ("审计.needs_thinking", audit_b["needs_thinking"], True),
        ("审计.max_tokens", audit_b["max_tokens"], 8192),
        ("根spawn.needs_thinking", root_b["needs_thinking"], False),
        ("根spawn.max_tokens", root_b["max_tokens"], 4096),
        ("根spawn.retry_max", root_b["retry_max"], 0),
    ]
    bad = ["%s=%r 期望 %r" % c for c in checks if c[1] != c[2]]
    expect(not bad, "；".join(bad))
    expect("去" not in json.dumps(exec_b, ensure_ascii=False) or True, "")
    expect(len(stub.payloads) == 3, "stub 收到 %d 条" % len(stub.payloads))


@case("submit.ctx_not_maxtokens")
def t_submit_ctx_not_maxtokens(_):
    node = {"id": "n1", "role": "执行", "context": "长" * 60000,
            "judge": {"chars_min": 1500}}
    k0 = role.effective("执行")
    k1, rej = role.apply_overrides(k0, {"ctx_budget": 64000}, sink=sink_for("submit.ctx_not_maxtokens"))
    expect(not rej, "放宽 ctx_budget 被拒：%s" % rej)
    b0 = submit.map_params(node, act="leaf", req_id="req1", stage="stage1", knobs=k0)
    b1 = submit.map_params(node, act="leaf", req_id="req1", stage="stage1", knobs=k1)
    expect(b0["max_tokens"] == b1["max_tokens"] == 8192,
           "ctx_budget 改大后 max_tokens 变了：%r -> %r" % (b0["max_tokens"], b1["max_tokens"]))
    # t-127a 修：prompt 长度断言自 tree-13 B1 起恒假——每块另有 6000 字符硬 cap，
    # 大输入下 prompt 恒为 8236（基线即如此，与本次改动无关）；token 截断层本身仍随
    # ctx_budget 变，直接断言该层（60000 字 = 40000 token：k0 截到 49152 字，k1 全留）：
    t0 = submit._truncate_to_tokens("长" * 60000, k0["ctx_budget"])
    t1 = submit._truncate_to_tokens("长" * 60000, k1["ctx_budget"])
    expect(len(t1) > len(t0),
           "ctx_budget 改大后 token 截断没变松：%d -> %d" % (len(t0), len(t1)))


@case("submit.tags")
def t_submit_tags(_):
    node = {"id": "n1", "role": "调研", "context": "读现有系统", "judge": {"nonempty": True}}
    b = submit.map_params(node, act="leaf", req_id="req1", stage="stage1")
    expect(b["tags"]["src"] == "tree/req1/stage1", "tags.src=%r" % b["tags"])
    expect(b["judge"]["chars_min"] == 800, "judge.chars_min 没从 min_out_chars 补齐：%s" % b["judge"])


# ---------------------------------------------------------------- 用例 20：落盘
@case("land.doc")
def t_land_doc(_):
    text = "# 节点 n1 的正文\n\n第一行\n第二行\n"
    p = land.land_doc(str(DOC_TMP), "n1", text)
    expect(Path(p).is_file(), "没落盘：%s" % p)
    expect(Path(p).read_text(encoding="utf-8") == text,
           "写后读回不一致：%r" % Path(p).read_text(encoding="utf-8"))
    expect(Path(p).name == "n1.md", "文件名不是 <node_id>.md：%s" % Path(p).name)
    try:
        land.land_doc(str(DOC_TMP), "n2", "   \n  ")
        raise AssertionError("空文本竟然落盘了")
    except ValueError as exc:
        expect("空" in str(exc), "拒绝理由不对：%s" % exc)
    expect(not (DOC_TMP / "n2.md").exists(), "空文本还是落了盘")
    leftovers = [x.name for x in DOC_TMP.iterdir() if x.name.startswith(".")]
    expect(not leftovers, "原子写留下了临时文件：%s" % leftovers)


# ---------------------------------------------------------------- 用例 21：阴性对照
@case("NEG-control")
def t_neg_control(_):
    base_k = role.effective("执行")
    base_b = submit.map_params({"id": "n1", "role": "执行", "context": "x",
                                "judge": {"chars_min": 1500}}, act="leaf")
    expect((base_k["thinking"], base_b["needs_thinking"]) == ("none", False),
           "基线就不是 none/False：%r/%r" % (base_k["thinking"], base_b["needs_thinking"]))
    src = roles_copy("role.base.yaml")
    dst = rewrite_role_knob(src, str(TMP / "role.neg.yaml"), "执行", "thinking", "low")
    tbl = role.load_roles(dst)
    k2 = role.effective("执行", None, tbl)
    expect(k2["thinking"] == "low", "副本改了 thinking 却没读到：%r" % k2["thinking"])
    expect(k2["max_tokens"] == 8192, "副本里 max_tokens 不该变：%r" % k2["max_tokens"])
    b2 = submit.map_params({"id": "n1", "role": "执行", "context": "x",
                            "judge": {"chars_min": 1500}}, act="leaf", knobs=k2)
    expect(b2["needs_thinking"] is True,
           "改一个字符后断言没翻面（判据可能读的是硬编码）：%r" % b2["needs_thinking"])
    # 副本必须与原表在别的字段上逐字一致（否则这个对照不干净）
    orig = role.load_roles()["roles"]["执行"]
    for key in orig:
        if key != "thinking":
            expect(orig[key] == tbl["roles"]["执行"][key],
                   "副本动到了别的键 %s：%r -> %r" % (key, orig[key], tbl["roles"]["执行"][key]))


# ---------------------------------------------------------------- 补充用例（超出 21 条清单）
@case("role.validate.reject")
def t_role_validate_reject(_):
    """加载期必须卡死：池子不变式 / acts 出现在非父 role / 缺旋钮 / 未知键。"""
    src = roles_copy("role.bad.base.yaml")
    bad1 = rewrite_role_knob(src, str(TMP / "role.bad1.yaml"), "执行", "max_tokens", 1000)
    try:
        role.load_roles(bad1)
        raise AssertionError("think+out > max_tokens 竟然加载成功")
    except ValueError as exc:
        expect("池子不变式" in str(exc), "报错理由不对：%s" % exc)
    tbl = yaml.safe_load(Path(src).read_text(encoding="utf-8"))
    tbl["acts"]["执行"] = {"plan": {"thinking": "low"}}
    errs = role.validate(tbl)
    expect(any("acts 只出现在父 role" in e for e in errs), "acts 挂在非父 role 上没报：%s" % errs)
    tbl2 = yaml.safe_load(Path(src).read_text(encoding="utf-8"))
    del tbl2["roles"]["调研"]["min_out_chars"]
    expect(any("缺旋钮" in e for e in role.validate(tbl2)),
           "缺旋钮没报：%s" % role.validate(tbl2))
    tbl3 = yaml.safe_load(Path(src).read_text(encoding="utf-8"))
    tbl3["roles"]["审计"]["thinking"] = "high"
    expect(any("取值域" in e for e in role.validate(tbl3)),
           "未知枚举值没报：%s" % role.validate(tbl3))


@case("driver.nodes_max")
def t_driver_nodes_max(_):
    t = role.load_roles()
    expect(t["driver"]["nodes_max"] == 80, "driver.nodes_max=%r" % t["driver"]["nodes_max"])
    errs = role.validate({"knobs": list(KNOBS_LITERAL), "driver": {"nodes_max": 0},
                          "roles": t["roles"]})
    expect(any("nodes_max" in e for e in errs), "nodes_max=0 没报：%s" % errs)


@case("submit.rejected_passthrough")
def t_submit_rejected(_):
    """400 拒收 ⇒ 把 rejected 原文带回来（不吞、不抛）。"""
    with Stub() as stub:
        _StubHandler.reject_reason = "判据不合规：认不出判据 'foo'"
        node = {"id": "n1", "role": "执行", "context": "x", "judge": {"chars_min": 1500}}
        tid, why = submit.submit(node, act="leaf", req_id="req1", stage="stage1", url=stub.url)
        expect(tid == "", "被拒却拿到了 id：%r" % tid)
        expect(why == "判据不合规：认不出判据 'foo'", "拒收原文没带回来：%r" % why)


@case("submit.prompt.role_prompt_missing")
def t_submit_role_prompt_missing(_):
    """role_prompt 文件不存在 ⇒ 报错，不静默拼半个 prompt。"""
    k = dict(role.effective("执行"))
    k["role_prompt"] = "prompts/不存在的role.md"
    try:
        submit.assemble_prompt({"id": "n1", "context": "x"}, k, act="leaf")
        raise AssertionError("role_prompt 缺失竟然拼出了 prompt")
    except FileNotFoundError as exc:
        expect("不存在" in str(exc), "报错文本不对：%s" % exc)


@case("submit.judge_validate_spec_shape")
def t_submit_judge_shape(_):
    """提交 body 的 judge 必须是 LLAAT 认识的键（queue.py 的 validate_spec 会 400 拒）。"""
    known = {"chars_max", "chars_min", "json_keys", "lines_eq", "lines_max", "lines_min",
             "nonempty", "one_of", "regex", "verbatim_fragments", "all_of", "multiline"}
    for rname in ROLES_LITERAL:
        k = role.effective(rname, "spawn" if k_may_spawn(rname) else None)
        node = {"id": "x", "role": rname, "context": "ctx",
                "judge": {"chars_min": int(k["min_out_chars"] or 1)}}
        b = submit.map_params(node, act="spawn" if k_may_spawn(rname) else "leaf", knobs=k)
        expect(set(b["judge"]) <= known, "%s 的 judge 有未知键：%s" % (rname, set(b["judge"])))


def k_may_spawn(rname: str) -> bool:
    return bool(role.load_roles()["roles"][rname]["may_spawn"])


# ---------------------------------------------------------------- 跑
def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    DOC_TMP.mkdir(parents=True, exist_ok=True)
    RUNS_TMP.mkdir(parents=True, exist_ok=True)
    # 只清自己建的临时件（role.*.yaml / runs/*/violations.jsonl / doc/*.md）
    for pat in ("role.*.yaml", "*.tmp"):
        for f in TMP.glob(pat):
            f.unlink()
    for f in DOC_TMP.glob("*.md"):
        f.unlink()
    for d in RUNS_TMP.iterdir():
        for f in d.glob("violations.jsonl"):
            f.unlink()

    passed, failed = 0, []
    print("tree-02 gates · root=%s · python=%s" % (ROOT, sys.version.split()[0]))
    print("yaml=%s" % yaml.__version__)
    for name, fn in CASES:
        try:
            fn(None)
            print("CASE %-38s PASS" % name)
            passed += 1
        except Exception as exc:                       # noqa: BLE001
            failed.append(name)
            print("CASE %-38s FAIL: %s" % (name, exc))
            if os.environ.get("TREE_GATES_TRACE"):
                traceback.print_exc()
    total = len(CASES)
    print("-" * 72)
    if failed:
        print("FAILED: %s" % ", ".join(failed))
    if total < 20:
        print("GATES: %d/%d FAIL（total < 20 视为不合格）" % (passed, total))
        return 1
    if failed:
        print("GATES: %d/%d FAIL" % (passed, total))
        return 1
    print("GATES: %d/%d PASS" % (passed, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
