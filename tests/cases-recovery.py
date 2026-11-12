"""故障矩阵回归（t-64e27d836932 系统架构主任务）。跑法：cd <候选根> && python3 tests/cases-recovery.py"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import contract as ct, driver as driver_mod, evidence as ev, leafrun, node as node_mod, recovery as rec_mod, role as role_mod, session as session_mod

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

TABLE = role_mod.load_roles()

# ---------- 1) 类型化契约分类 ----------
check("contract classify fix->contract_repair",
      ct.classify({"id": "root.fix.1", "role": "调研"}) == "contract_repair")
check("contract classify research",
      ct.classify({"id": "root.c2.1", "role": "调研"}) == "research")
check("contract classify audit",
      ct.classify({"id": "root.c2.3", "role": "审计"}) == "audit")
check("contract classify synthesis",
      ct.classify({"id": "root.c2.2", "role": "执行"}) == "synthesis")
check("contract classify explicit override wins",
      ct.classify({"id": "root.c2.1", "role": "调研", "overrides": {"node_kind": "audit"}}) == "audit")
check("contract leaf timeout research=300",
      ct.leaf_timeout_for("research") == 300.0)
# 第二轮续修（integ2-q01 实测）：两修复叶都在 180s 上限撞停，提至 300s，与 research 看齐。
check("contract leaf timeout repair=300",
      ct.leaf_timeout_for("contract_repair") == 300.0)

# ---------- 2) 失败分类 ----------
check("failure zero_tool",
      ct.classify_failure("形状", "产出不契约（证据门）：零成功工具结果：任务要求现场调查", {}) == "zero_tool_research")
check("failure bad_citation",
      ct.classify_failure("形状", "产出不契约（证据门）：无法核查的引用：/tmp/nope.md（不存在）", {}) == "bad_citation")
check("failure loop_breaker",
      ct.classify_failure("环境", "loop_breaker 熔断终止：operation was aborted", {}) == "loop_breaker")
check("failure timeout",
      ct.classify_failure("超时", "exit_class=超时；judge=pi 到单叶上限（调研 300 秒）", {}) == "timeout")
check("failure no_criteria",
      ct.classify_failure("判据", "无可执行判据：节点未写验收标准", {}) == "no_criteria")
check("failure role_invalid",
      ct.classify_failure("形状", "children[0].role='spawn' 不是内置 role", {}) == "role_invalid")
check("failure merge_missing_field parent",
      ct.classify_failure("形状", "缺必填键 '下一批要点'", {"role": "根"}) == "merge_missing_field")
check("failure schema_missing_field leaf",
      ct.classify_failure("形状", "缺必填键 '下一批要点'", {"role": "调研"}) == "schema_missing_field")

# ---------- 3) 恢复控制器：换策略 + 耗尽 ----------
with tempfile.TemporaryDirectory() as tmp:
    c = rec_mod.RecoveryController(tmp)
    s1 = c.choose("root", "root.fix.1", "merge_missing_field", "缺必填键 '下一批要点'", None)
    check("recovery first strategy is contract_repair",
          s1 is not None and s1.get("kind") == "contract_repair", s1)
    s2 = c.choose("root", "root.fix.1", "merge_missing_field", "缺必填键 '下一批要点'", None)
    check("recovery second strategy differs (diagnosis)",
          s2 is not None and ct.action_fingerprint(s2) != ct.action_fingerprint(s1), s2)
    s3 = c.choose("root", "root.fix.1", "merge_missing_field", "缺必填键 '下一批要点'", None)
    check("recovery exhausted after 2 distinct strategies", s3 is None)
    check("recovery edges logged", len(c.edges) == 3 and Path(tmp, "recovery.jsonl").is_file())
    check("failure table has 14 classes", len(ct.failure_recovery_table()) == 14)  # 15 类 − unknown：failure_recovery_table() 跳过 unknown；t-dc62 新增 loop 类

# ---------- 4) 证据门分流（离线 fake session） ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    sess_dir = Path(tmp) / "sess"
    sess_dir.mkdir()
    empty_sess = str(sess_dir / "x_tree-t-root.fix.1.jsonl")
    Path(empty_sess).write_text("", encoding="utf-8")
    rep_node = {"id": "root.fix.1", "role": "调研",
                "overrides": {"node_kind": "contract_repair", "input_evidence_ids": ["root.c2.1"]},
                "context": "[修复任务] 缺 uncovered", "验收标准": "x"}
    rep_obj = {"reply": "据 root.c2.1 的 19 条工具结果，未发现缺口", "未决": [], "uncovered": []}
    ok, det = ev.evidence_gate(rep_node, rep_obj, empty_sess, str(run_dir), str(ROOT),
                               may_write=False, node_kind="contract_repair",
                               input_evidence_ids=["root.c2.1"])
    check("gate context-only repair zero-tool passes", ok, det)
    rep_obj2 = {"reply": "我认为没有缺口", "未决": [], "uncovered": []}
    ok, det = ev.evidence_gate(rep_node, rep_obj2, empty_sess, str(run_dir), str(ROOT),
                               may_write=False, node_kind="contract_repair",
                               input_evidence_ids=["root.c2.1"])
    check("gate repair without input evidence ref rejected", not ok, det)
    res_node = {"id": "root.c2.1", "role": "调研",
                "context": "调查本机内存实测", "验收标准": "有数字"}
    res_obj = {"reply": "内存 32GB", "未决": []}
    ok, det = ev.evidence_gate(res_node, res_obj, empty_sess, str(run_dir), str(ROOT),
                               may_write=False, node_kind="research")
    check("gate research zero-tool rejected", not ok, det)
    rep_obj3 = {"reply": "见 /tmp/does-not-exist-xyz.md 第 3 行，结论同 root.c2.1", "未决": [], "uncovered": []}
    ok, det = ev.evidence_gate(rep_node, rep_obj3, empty_sess, str(run_dir), str(ROOT),
                               may_write=False, node_kind="contract_repair",
                               input_evidence_ids=["root.c2.1"])
    check("gate phantom citation rejected", not ok, det)
    bad_sess = sess_dir / "y_tree-t-root.c2.1.jsonl"
    bad_sess.write_text(
        '{"message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "ps"}}]}}\n'
        '{"message": {"role": "toolResult", "content": "/bin/ps: Operation not permitted"}}\n', encoding="utf-8")
    tools = ev.count_tool_results(str(bad_sess))
    check("count Operation not permitted as failure",
          tools["successes"] == 0 and tools["failures"] == 1, tools)

# ---------- 5) 判据 ----------
ok, why, nj = leafrun.evaluate_judge({"judge": None}, {"reply": "x" * 50})
check("judge no-criteria fails (not auto-pass)", ok is False and "无可执行判据" in why, why)
ok, why, nj = leafrun.evaluate_judge({"judge": "写个表格"}, {"reply": "x" * 50})
check("judge string criteria -> partial (None)", ok is None, why)
ok, why, nj = leafrun.evaluate_judge({"judge": {"chars_min": 10}}, {"reply": "0123456789AB"})
check("judge dict chars_min passes", ok is True, why)
ok, why, nj = leafrun.evaluate_judge({"judge": {"chars_min": 100}}, {"reply": "short"})
check("judge dict chars_min fails", ok is False, why)
ok, why, nj = leafrun.evaluate_judge({"judge": {"json_keys": ["uncovered", "reply", "未决"]}},
                                      {"reply": "r", "未决": [], "uncovered": []})
check("judge json_keys passes", ok is True, why)

# ---------- Gate 5：无有效判据的 judge 必须 fail closed，不得自动通过 ----------
# 负例（修前实测）：{"bogus": 1} / {"标准": "写得好"} 均返回 (True, "无判据...")，
# 经 leaf 关闭路径会被标 完成/complete；{"chars_min": "1500"} 亦报“判据通过”。
ok, why, nj = leafrun.evaluate_judge({"judge": {"bogus": 1}}, {"reply": "x" * 2000})
check("judge unknown-keys-only dict fails closed", ok is False and "无可执行判据" in why, why)
ok, why, nj = leafrun.evaluate_judge({"judge": {"标准": "写得好"}}, {"reply": "x" * 2000})
check("judge natural-language keys dict fails closed", ok is False and "无可执行判据" in why, why)
ok, why, nj = leafrun.evaluate_judge({"judge": {"chars_min": "1500"}}, {"reply": "x" * 2000})
check("judge chars_min wrong type fails", ok is False, why)
ok, why, nj = leafrun.evaluate_judge({"judge": {"chars_max": "100"}}, {"reply": "x" * 2000})
check("judge chars_max wrong type fails", ok is False, why)
ok, why, nj = leafrun.evaluate_judge({"judge": {"chars_min": 10, "extra": 1}}, {"reply": "0123456789AB"})
check("judge known key + unknown key still evaluates known", ok is True, why)

# ---------- 6) run_leaf 桩测试 ----------
def _make_driver(tmp, stub):
    run_dir = Path(tmp) / "run"
    run_dir.mkdir(exist_ok=True)
    root = node_mod.new_node("root", "根", parent="", context="t", judge=None, 验收标准="", auth="")
    timed = leafrun.TimedRunner(stub)
    d = leafrun.TreeDriver(root, {"root": root}, TABLE, run_id="t-rec",
                           run_dir=str(run_dir), act_runner=timed, timed=timed)
    return d, run_dir

def _stub_leaf(payload):
    def _run(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        return json.dumps(payload, ensure_ascii=False)
    return _run

with tempfile.TemporaryDirectory() as tmp:
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(Path(tmp) / "sess")
    (Path(tmp) / "sess").mkdir(exist_ok=True)
    d, run_dir = _make_driver(tmp, _stub_leaf(
        {"reply": "据 root.c2.1 的部分证据，未发现缺口", "未决": [], "uncovered": []}))
    n = node_mod.new_node("root.fix.1", "调研", parent="root",
                          context="[修复任务] 缺 uncovered", judge={"json_keys": ["uncovered", "reply", "未决"]},
                          验收标准="x", auth="只消费输入证据",
                          overrides={"node_kind": "contract_repair", "input_evidence_ids": ["root.c2.1"]},
                          batch=2)
    d.tree["root.fix.1"] = n
    row = leafrun.run_leaf(d, n)
    check("run_leaf contract_repair zero-tool completes",
          n.get("state") == "完成" and row.get("result_type") == "complete", row)
    led = ct.read_ledger(str(run_dir))
    check("ledger has repair complete entry",
          led.get("root.fix.1", {}).get("result_type") == "complete", led)
    d2, run_dir2 = _make_driver(tmp, _stub_leaf({"reply": "内存 32GB", "未决": []}))
    n2 = node_mod.new_node("root.c2.1", "调研", parent="root",
                           context="调查本机内存实测", judge={"chars_min": 5},
                           验收标准="有数字", auth="只读", batch=2)
    d2.tree["root.c2.1"] = n2
    row2 = leafrun.run_leaf(d2, n2)
    check("run_leaf research zero-tool fails",
          n2.get("state") == "失败" and "零成功工具结果" in (row2.get("why") or ""), row2)
    d3, run_dir3 = _make_driver(tmp, _stub_leaf({"reply": "做了一些调查工作，结论如下", "未决": [],
                                                   "doc": "综合结论正文：做了一些调查工作，结论如下。"}))
    n3 = node_mod.new_node("root.c2.9", "执行", parent="root",
                           context="综合已有证据", judge="写个结论",
                           验收标准="有结论", auth="doc/x.md", batch=2)
    d3.tree["root.c2.9"] = n3
    row3 = leafrun.run_leaf(d3, n3)
    led3 = ct.read_ledger(str(run_dir3))
    check("run_leaf string judge -> partial with obligation",
          row3.get("result_type") == "partial"
          and any("未机械核验" in str(u.get("what") or "") for u in (n3.get("未决") or []))
          and led3.get("root.c2.9", {}).get("result_type") == "partial", row3)
    def _stub_timeout(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        import subprocess as _sp
        raise _sp.TimeoutExpired(cmd=["pi"], timeout=300)
    sess = Path(tmp) / "sess" / "z_tree-t-rec-root.c2.1.jsonl"
    sess.write_text(
        '{"message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "sysctl hw.memsize"}}]}}\n'
        '{"message": {"role": "toolResult", "content": "hw.memsize: 34359738368"}}\n', encoding="utf-8")
    d4, run_dir4 = _make_driver(tmp, _stub_timeout)
    n4 = node_mod.new_node("root.c2.1", "调研", parent="root",
                           context="调查本机内存实测", judge={"chars_min": 5},
                           验收标准="有数字", auth="只读", batch=2)
    d4.tree["root.c2.1"] = n4
    row4 = leafrun.run_leaf(d4, n4)
    partial = Path(run_dir4) / "partial" / "root.c2.1.json"
    check("run_leaf timeout salvages partial readable by parent",
          partial.is_file() and json.loads(partial.read_text(encoding="utf-8")).get("successes", 0) == 1
          and row4.get("result_type") == "partial"
          and any("partial/root.c2.1.json" in str(u.get("what") or "") for u in (n4.get("未决") or [])), row4)

# ---------- 7) Scheduler 脚本化全链路 ----------
with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    sess_dir = Path(tmp) / "sess"
    sess_dir.mkdir()
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(sess_dir)
    os.environ["TREE_RUNS_DIR"] = str(Path(tmp))
    calls = {"merge": 0}
    def _scripted(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        nid = str(node.get("id") or "")
        if act == "plan":
            return json.dumps({"要点": ["查本机内存", "综合成结论"]}, ensure_ascii=False)
        if act == "spawn":
            return json.dumps({"children": [
                {"role": "调研", "context": "调查本机内存实测", "judge": {"chars_min": 5},
                 "验收标准": "有数字", "auth": "只读"},
                {"role": "执行", "context": "综合已有证据成文", "judge": {"chars_min": 5},
                 "验收标准": "有结论", "auth": "doc/x.md"}]}, ensure_ascii=False)
        if act == "merge":
            calls["merge"] += 1
            if calls["merge"] == 1:
                return json.dumps({"去向": "再派", "reply": "需要下一批", "未决": []}, ensure_ascii=False)
            if calls["merge"] == 2:
                return json.dumps({"去向": "再派", "reply": "仍需下一批", "未决": []}, ensure_ascii=False)
            return json.dumps({"去向": "收束", "reply": "结论：内存 32GB，夜间窗口未决",
                               "未决": [], "uncovered": [{"what": "root.c2.2 执行叶判据为自然语言，需审计复核",
                                                          "why": "partial 未机械核验"}]}, ensure_ascii=False)
        if ".fix." in nid:
            return json.dumps({"reply": "据 root.c2.1 的证据，未发现缺口",
                               "未决": [], "uncovered": []}, ensure_ascii=False)
        if ".diag." in nid:
            return json.dumps({"reply": "根因：merge 模板缺 下一批要点 提示，已换策略",
                               "未决": []}, ensure_ascii=False)
        if str(node.get("role") or "") == "执行":
            return json.dumps({"reply": "综合结论正文", "未决": [],
                               "doc": "综合结论正文：内存 32GB。"}, ensure_ascii=False)
        return json.dumps({"reply": "内存 32GB，实测", "未决": []}, ensure_ascii=False)
    for _nid in ("root.c2.1", "root.diag.1"):
        (sess_dir / ("z_tree-t-sch-%s.jsonl" % _nid)).write_text(
            '{"message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "sysctl hw.memsize"}}]}}\n'
            '{"message": {"role": "toolResult", "content": "hw.memsize: 34359738368"}}\n', encoding="utf-8")
    sch = leafrun.Scheduler(str(run_dir), "t-sch", TABLE, "研究题：Qwen 夜间窗口",
                            max_steps=40, max_seconds=120, max_reject=2,
                            act_runner=_scripted)
    sch.build_root()
    res = sch.run()
    tree = sch.tree
    fix = tree.get("root.fix.1") or {}
    diag = tree.get("root.diag.1") or {}
    check("scheduler repair child is typed contract_repair",
          (fix.get("overrides") or {}).get("node_kind") == "contract_repair"
          and isinstance(fix.get("judge"), dict), fix.get("overrides"))
    check("scheduler repair child completed (gate no longer mis-rejects)",
          fix.get("state") == "完成", fix.get("state"))
    check("scheduler diagnosis child dispatched on repeated missing field",
          diag.get("id") == "root.diag.1" and diag.get("state") == "完成",
          diag.get("state"))
    edges = []
    _rp = Path(run_dir) / "recovery.jsonl"
    if _rp.is_file():
        edges = [json.loads(x) for x in _rp.read_text(encoding="utf-8").splitlines() if x.strip()]
    check("scheduler recovery edges logged (repair + diagnosis)",
          len([e for e in edges if e.get("failure_class") == "merge_missing_field"]) >= 2, edges)
    check("scheduler root terminal", tree.get("root", {}).get("state") in ("完成", "失败"),
          tree.get("root", {}).get("state"))
    check("scheduler root converged after repair+diagnosis",
          tree.get("root", {}).get("state") == "完成", tree.get("root", {}).get("state"))
    led = ct.read_ledger(str(run_dir))
    check("scheduler ledger covers leaves",
          led.get("root.fix.1", {}).get("result_type") == "complete", led)

# ---------- 8) 第三轮 P1④：执行叶环境类失败有界重派（上限 1 次，台账在 env_retry.jsonl） ----------
def _sess(sess_dir, run_id, nid):
    (sess_dir / ("z_tree-%s-%s.jsonl" % (run_id, nid))).write_text(
        '{"message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "sysctl hw.memsize"}}]}}\n'
        '{"message": {"role": "toolResult", "content": "hw.memsize: 34359738368"}}\n', encoding="utf-8")

def _retry_case(tmp, fail_ids):
    """fail_ids: 跑即抛 TimeoutExpired 的叶 id 精确集合。8a 只 fail 原叶；
    8b 同时 fail 原叶与重试叶（恒超时）。"""
    import subprocess as _sp
    run_dir = Path(tmp) / "run"
    run_dir.mkdir()
    sess_dir = Path(tmp) / "sess"
    sess_dir.mkdir()
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(sess_dir)
    os.environ["TREE_RUNS_DIR"] = str(Path(tmp))
    for _nid in ("root.c2.1", "root.c2.1.retry1", "root.c2.1.audit", "root.c2.1.retry1.audit"):
        _sess(sess_dir, "t-retry", _nid)
    def _scripted(node, act, knobs, prompt, session_id, run_dir, timeout=None):
        nid = str(node.get("id") or "")
        if act == "plan":
            return json.dumps({"要点": ["查本机内存"]}, ensure_ascii=False)
        if act == "spawn":
            # 执行 role（可写 doc）：chars_min 判据的叶须落盘 doc，调研 role
            # may_write=false 带不了 doc 字段，脚本桩不跑真工具写不出 doc。
            return json.dumps({"children": [
                {"role": "执行", "context": "综合已有证据成文", "judge": {"chars_min": 5},
                 "验收标准": "有结论", "auth": "doc/x.md"}]}, ensure_ascii=False)
        if act == "merge":
            return json.dumps({"去向": "收束", "reply": "结论：内存 32GB",
                               "未决": [],
                               "uncovered": [{"what": "root.c2.1 超时失败，已由重试叶 root.c2.1.retry1 补位",
                                              "why": "环境超时一次，已重试"}]}, ensure_ascii=False)
        if nid in fail_ids:
            raise _sp.TimeoutExpired(cmd=["pi"], timeout=300)
        # 成功叶须带 doc 字段：chars_min 判据的叶须落盘 doc，否则 disk-doc
        # 检查 fail-closed（真实叶也是写 doc 才算完成）。
        return json.dumps({"reply": "内存 32GB，实测", "未决": [],
                           "doc": "内存 32GB，实测结论。"}, ensure_ascii=False)
    sch = leafrun.Scheduler(str(run_dir), "t-retry", TABLE, "研究题：重试",
                            max_steps=40, max_seconds=120, max_reject=2,
                            act_runner=_scripted)
    sch.build_root()
    sch.run()
    return sch, run_dir

with tempfile.TemporaryDirectory() as tmp:
    # 8a) 原叶超时 ⇒ retry1 成功、根完成、台账 1 行、无 retry2。
    sch, run_dir = _retry_case(tmp, fail_ids={"root.c2.1"})
    tree = sch.tree
    retry = tree.get("root.c2.1.retry1") or {}
    check("env retry: 原叶超时失败",
          tree.get("root.c2.1", {}).get("state") == "失败"
          and tree.get("root.c2.1", {}).get("failure_class") == "timeout",
          tree.get("root.c2.1"))
    check("env retry: 建出重试节点 root.c2.1.retry1",
          retry.get("id") == "root.c2.1.retry1", retry.get("id"))
    check("env retry: 重试叶成功完成", retry.get("state") == "完成", retry.get("state"))
    check("env retry: 重试叶 context 含真实路径",
          os.path.realpath(str(run_dir)) in str(retry.get("context") or ""), retry.get("context"))
    check("env retry: 重试叶同 parent/batch/role/judge",
          retry.get("parent") == "root" and retry.get("batch") == 2
          and retry.get("role") == "执行" and retry.get("judge") == {"chars_min": 5},
          {k: retry.get(k) for k in ("parent", "batch", "role", "judge")})
    check("env retry: 无 retry2（上限 1 次）",
          not [k for k in tree if k.startswith("root.c2.1.retry")
               and ".audit" not in k and k != "root.c2.1.retry1"],
          list(tree))
    check("env retry: 根完成收束", tree.get("root", {}).get("state") == "完成",
          tree.get("root", {}).get("state"))
    _rp = Path(run_dir) / "env_retry.jsonl"
    _rows = [json.loads(x) for x in _rp.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if _rp.is_file() else []
    check("env retry: 台账恰 1 行",
          len(_rows) == 1 and _rows[0].get("node_id") == "root.c2.1"
          and _rows[0].get("retry_id") == "root.c2.1.retry1"
          and _rows[0].get("exit_class") == "超时"
          and _rows[0].get("failure_class") == "timeout", _rows)

with tempfile.TemporaryDirectory() as tmp:
    # 8b) 恒超时（原叶 + retry1 都超时）⇒ 仅 retry1、台账恰 1 行、不再重派。
    sch, run_dir = _retry_case(tmp, fail_ids={"root.c2.1", "root.c2.1.retry1"})
    tree = sch.tree
    check("env retry恒超时: 仅建出 retry1",
          "root.c2.1.retry1" in tree
          and not [k for k in tree if k.startswith("root.c2.1.retry")
                   and ".audit" not in k and k != "root.c2.1.retry1"],
          list(tree))
    check("env retry恒超时: retry1 亦超时失败",
          tree.get("root.c2.1.retry1", {}).get("state") == "失败"
          and tree.get("root.c2.1.retry1", {}).get("failure_class") == "timeout",
          tree.get("root.c2.1.retry1"))
    _rp = Path(run_dir) / "env_retry.jsonl"
    _rows = [json.loads(x) for x in _rp.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if _rp.is_file() else []
    check("env retry恒超时: 台账恰 1 行（重试叶失败不再重试）",
          len(_rows) == 1 and _rows[0].get("node_id") == "root.c2.1", _rows)
    check("env retry恒超时: 根仍完成收束（失败分支已进 uncovered）",
          tree.get("root", {}).get("state") == "完成", tree.get("root", {}).get("state"))

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: %s" % FAIL)
    sys.exit(1)
