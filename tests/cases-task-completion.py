"""任务级闭环续修回归（t-b0424b0f1592）。跑法：cd <候选根> && python3 tests/cases-task-completion.py

覆盖上一轮实验 A 实证的三处失败（均不靠 prompt 文字，纯机械机制）：
  1. 叶最终 JSON 被模型反复 Write 到 product/output.json，触发 loop-breaker 熔断，
     正文里从无 JSON ⇒ 会话抢救最终 JSON（harvest_final_json）；
  2. doc 字段是文件指针（"blocks/xxx.md"）不是正文 ⇒ doc 指针解析（resolve_doc_pointer）；
  3. root 自造中文 judge 键 ⇒ judge 归一化（normalize_judge / evaluate_judge）；
  4. merge 轮 "未决[2].type"="未决" 形状拒收 ⇒ 模型未决归一（normalize_model_undecided）；
  5. fix.1 证据门：引用面拓宽到未决/会话 assistant 侧，零引用仍拒收。
  6. merge 轮 uncovered/对审计发现 机械归一（Run D 实测形状拒收，集成验收补丁）。
"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import evidence as ev, leafrun, schema

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

# ----------  fixture：实验 A 的真实 judge（逐字抄自 smoke-a state.json） ----------
JUDGE_C42 = {"产物存在性": "product/answer.txt 必须存在且只包含结果数字",
             "文档长度": "doc 内容不少于 1500 字",
             "算法明确性": "doc 中需包含两种独立算法及结果",
             "数值一致性": "两种算法结果与产物文件数字必须一致",
             "工具调用": "Bash 和 Write 工具调用记录需完整可追溯"}
JUDGE_C21 = {"工具使用": "Bash 中需调用 python3 执行素数计数，Write 中需将结果数字单独一行写入 product/answer.txt",
             "数值一致性": "Python 第一种算法与第二种独立算法（如试除法或筛法）的结果必须相同",
             "产物要求": "product/answer.txt 必须存在且只包含结果数字（单独一行）",
             "文档要求": "doc 内容不少于 1500 字，需包含计算方法、交叉复核结果、产物路径说明"}

FINAL_JSON = {"reply": "任务已完成。1 到 1000 之间素数个数为 168，两种独立算法交叉复核结果一致。",
              "未决": [], "doc": "blocks/sushu_implementation.md"}

def _sess_line(role, content):
    return json.dumps({"type": "message", "message": {"role": role, "content": content}},
                      ensure_ascii=False)

def _write_session(path, final_json=FINAL_JSON, n_writes=5):
    """仿实验 A c2.1 会话：bash 算数 → write answer.txt → write blocks 文档 →
    5 次相同的 write product/output.json → loop-breaker 阻断 → abort。"""
    lines = [
        _sess_line("user", [{"type": "text", "text": "你是叶子：执行。"}]),
        _sess_line("assistant", [{"type": "toolCall", "id": "t1", "name": "bash",
                                  "arguments": {"command": "python3 -c 'print(168)'"}}]),
        _sess_line("toolResult", [{"type": "text", "text": "SIEVE_COUNT 168\nTrial Division 168\n"}]),
        _sess_line("assistant", [{"type": "toolCall", "id": "t2", "name": "write",
                                  "arguments": {"content": "168\n", "path": "product/answer.txt"}}]),
        _sess_line("assistant", [{"type": "toolCall", "id": "t3", "name": "write",
                                  "arguments": {"content": "# doc\n", "path": "blocks/sushu_implementation.md"}}]),
    ]
    for i in range(n_writes):
        lines.append(_sess_line("assistant", [{"type": "toolCall", "id": "w%d" % i,
                                               "name": "write",
                                               "arguments": {"content": json.dumps(final_json, ensure_ascii=False),
                                                             "path": "product/output.json"}}]))
        if i == 3:
            lines.append(_sess_line("toolResult", [{"type": "text",
                "text": "[重复阻断·loop-breaker]write 用完全相同的参数已调用 4 次"}]))
        elif i == 4:
            lines.append(_sess_line("toolResult", [{"type": "text", "text": "Operation aborted"}]))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")

# ---------- 1) judge 归一化 ----------
norm, note = leafrun.normalize_judge(dict(JUDGE_C42), role="审计")
check("judge audit: 中文键→json_keys 缺省", norm == {"json_keys": ["reply", "未决"]}, norm)
check("judge audit: 说明提到归一化", "归一化" in note, note)

norm, note = leafrun.normalize_judge(dict(JUDGE_C21), role="执行", min_out_chars=1500)
check("judge exec: 中文键值抽得 chars_min=1500", norm == {"chars_min": 1500}, norm)

norm, _ = leafrun.normalize_judge({"chars_min": 1500, "json_keys": ["reply"]}, role="执行")
check("judge: 已知键原样保留", norm == {"chars_min": 1500, "json_keys": ["reply"]}, norm)

norm, note = leafrun.normalize_judge({"chars_min": "1500"}, role="执行")
check("judge: 已知键类型非法→None（显式失败，不默认通过）", norm is None, (norm, note))

norm, _ = leafrun.normalize_judge({}, role="执行")
check("judge: 空 dict→None", norm is None, norm)
norm, _ = leafrun.normalize_judge("自然语言判据", role="执行")
check("judge: 字符串→None", norm is None, norm)

norm, _ = leafrun.normalize_judge({"工具使用": "随便写点"}, role="根")
check("judge: 无数字、无 role 缺省→None", norm is None, norm)

# ---------- 2) evaluate_judge 端到端 ----------
ok, why, _ = leafrun.evaluate_judge(
    {"id": "root.c4.2", "role": "审计", "judge": dict(JUDGE_C42)},
    {"reply": "已核验。产物存在，doc 够长，算法明确，数值一致，工具记录完整。", "未决": []})
check("evaluate: 审计叶中文 judge→通过", ok is True, why)

ok, why, _ = leafrun.evaluate_judge(
    {"id": "root.c2.1", "role": "执行", "judge": dict(JUDGE_C21)},
    {"reply": "done", "未决": [], "doc": "x" * 1600})
check("evaluate: 执行叶中文 judge + 1600 字 doc→通过", ok is True, why)

ok, why, _ = leafrun.evaluate_judge(
    {"id": "root.c2.1", "role": "执行", "judge": dict(JUDGE_C21)},
    {"reply": "done", "未决": [], "doc": "x" * 100})
check("evaluate: 执行叶 doc 仅 100 字→chars_min 照常拦截", ok is False and "1500" in why, why)

ok, why, _ = leafrun.evaluate_judge(
    {"id": "root.c2.1", "role": "执行", "judge": {"chars_min": "1500"}},
    {"reply": "done", "未决": [], "doc": "x" * 1600})
check("evaluate: 已知键类型非法→显式失败", ok is False, why)

# ---------- 3) 会话抢救最终 JSON ----------
with tempfile.TemporaryDirectory() as td:
    sp = os.path.join(td, "sess.jsonl")
    _write_session(sp)
    obj, prov = leafrun.harvest_final_json(sp)
    check("harvest: 5 次连写后抢救到最终 JSON", obj == FINAL_JSON, obj)
    check("harvest: 来源说明含 write 调用", "write" in prov and "output.json" in prov, prov)
    check("harvest: 会话文件缺失→(None, '')",
          leafrun.harvest_final_json(os.path.join(td, "nope.jsonl")) == (None, ""), "")

    # 最后一份为准（模型改主意重写时取新版）
    alt = dict(FINAL_JSON, reply="改版 reply")
    sp2 = os.path.join(td, "sess2.jsonl")
    _write_session(sp2, final_json=FINAL_JSON, n_writes=2)
    with open(sp2, "a", encoding="utf-8") as fh:
        fh.write(_sess_line("assistant", [{"type": "toolCall", "id": "w9", "name": "write",
            "arguments": {"content": json.dumps(alt, ensure_ascii=False),
                          "path": "product/output.json"}}]) + "\n")
    obj2, _ = leafrun.harvest_final_json(sp2)
    check("harvest: 取最后一份（后写覆盖前写）", obj2 == alt, obj2)

    # 正文 JSON 候选
    sp3 = os.path.join(td, "sess3.jsonl")
    Path(sp3).write_text("\n".join([
        _sess_line("assistant", [{"type": "text", "text": json.dumps(FINAL_JSON, ensure_ascii=False)}]),
    ]) + "\n", encoding="utf-8")
    obj3, prov3 = leafrun.harvest_final_json(sp3)
    check("harvest: 正文 JSON 同样可抢救", obj3 == FINAL_JSON and "正文" in prov3, prov3)

    # 无候选
    sp4 = os.path.join(td, "sess4.jsonl")
    Path(sp4).write_text(_sess_line("assistant", [{"type": "text", "text": "还在干活"}]) + "\n",
                         encoding="utf-8")
    check("harvest: 无候选→(None, '')", leafrun.harvest_final_json(sp4) == (None, ""), "")

# ---------- 4) doc 指针解析 ----------
with tempfile.TemporaryDirectory() as td:
    run = Path(td)
    (run / "blocks").mkdir()
    (run / "blocks" / "sushu_implementation.md").write_text("素数文档正文内。" * 200, encoding="utf-8")
    obj = dict(FINAL_JSON)
    ok, path = leafrun.resolve_doc_pointer(obj, str(run))
    check("doc指针: 存在的文件指针解析为正文",
          ok and path == "blocks/sushu_implementation.md" and len(obj["doc"]) == 1600, len(obj.get("doc") or ""))
    obj = dict(FINAL_JSON, doc="blocks/不存在.md")
    check("doc指针: 文件不存在→不动", leafrun.resolve_doc_pointer(obj, str(run)) == (False, ""), "")
    obj = dict(FINAL_JSON, doc="/tmp/绝对路径.md")
    check("doc指针: 绝对路径→拒绝", leafrun.resolve_doc_pointer(obj, str(run)) == (False, ""), "")
    obj = dict(FINAL_JSON, doc="../逃逸.md")
    check("doc指针: .. 逃逸→拒绝", leafrun.resolve_doc_pointer(obj, str(run)) == (False, ""), "")
    obj = dict(FINAL_JSON, doc="这是一段很长很长的正文不是指针。" * 30)
    check("doc指针: 长正文（>300 字）→不动", leafrun.resolve_doc_pointer(obj, str(run)) == (False, ""), "")
    obj = {"reply": "x", "未决": []}
    check("doc指针: 无 doc 键→不动", leafrun.resolve_doc_pointer(obj, str(run)) == (False, ""), "")

# ---------- 5) 模型未决归一 ----------
out, note = schema.normalize_model_undecided(
    [{"type": "未决", "what": "a", "why": "b"}, {"type": "缺信息", "what": "c", "why": "d"}])
check("未决归一: '未决'→'判不了'，合法值不动",
      out == [{"type": "判不了", "what": "a", "why": "b"}, {"type": "缺信息", "what": "c", "why": "d"}], out)
out, _ = schema.normalize_model_undecided([{"type": "BLOCKER 缺信息", "what": "a", "why": "b"}])
check("未决归一: 含枚举成员子串→取该成员", out[0]["type"] == "缺信息", out)
out, note = schema.normalize_model_undecided([{"type": "超时", "what": "a", "why": "b"}])
check("未决归一: 已是枚举成员→原样、无改动说明", out[0]["type"] == "超时" and note == "", note)
out, _ = schema.normalize_model_undecided([{"type": "随便写的类型", "what": "a", "why": "b"}])
check("未决归一: 非法值→'判不了'（不编造缺信息）", out[0]["type"] == "判不了", out)

# ---------- 6) 证据门：修复叶引用面 ----------
FIX_NODE = {"id": "root.fix.1", "role": "调研",
            "overrides": {"node_kind": "contract_repair", "input_evidence_ids": ["root.c6.1"]}}
with tempfile.TemporaryDirectory() as td:
    run = Path(td)
    (run / "product").mkdir()
    (run / "product" / "answer.txt").write_text("168\n", encoding="utf-8")
    # fix.1 式：reply/未决/会话三处全无证据 ID ⇒ 仍失败
    ok, detail = ev.evidence_gate(
        FIX_NODE, {"reply": "已对产物文件和日志做机械核验。product/answer.txt 存在。", "未决": []},
        os.path.join(td, "missing.jsonl"), str(run), str(run),
        may_write=False, node_kind="contract_repair", input_evidence_ids=["root.c6.1"])
    check("证据门: fix.1 式零引用→仍失败", ok is False and "root.c6.1" in detail, detail)
    # 未决里引用 ⇒ 通过
    ok, detail = ev.evidence_gate(
        FIX_NODE, {"reply": "已核验产物。",
                   "未决": [{"type": "判不了", "what": "root.c6.1 的判据问题待确认", "why": "需复核"}]},
        os.path.join(td, "missing.jsonl"), str(run), str(run),
        may_write=False, node_kind="contract_repair", input_evidence_ids=["root.c6.1"])
    check("证据门: 未决里引用证据 ID→通过", ok is True, detail)
    # 会话 assistant 工具调用参数里引用 ⇒ 通过（prompt 里出现不算）
    sp = os.path.join(td, "fix-sess.jsonl")
    Path(sp).write_text("\n".join([
        _sess_line("user", [{"type": "text", "text": "修复 root.c6.1 的问题"}]),
        _sess_line("assistant", [{"type": "toolCall", "id": "r1", "name": "read",
                                  "arguments": {"path": "partial/root.c6.1.json"}}]),
        _sess_line("assistant", [{"type": "text", "text": json.dumps(
            {"reply": "已按 root.c6.1 的失败记录复核。", "未决": []}, ensure_ascii=False)}]),
    ]) + "\n", encoding="utf-8")
    ok, detail = ev.evidence_gate(
        FIX_NODE, {"reply": "已按失败记录复核。", "未决": []},
        sp, str(run), str(run),
        may_write=False, node_kind="contract_repair", input_evidence_ids=["root.c6.1"])
    check("证据门: 会话 assistant 工具调用引用→通过", ok is True, detail)
    # 只有 prompt（user）里出现 ID、assistant 侧没有 ⇒ 仍失败
    sp2 = os.path.join(td, "fix-sess2.jsonl")
    Path(sp2).write_text("\n".join([
        _sess_line("user", [{"type": "text", "text": "修复 root.c6.1 的问题"}]),
        _sess_line("assistant", [{"type": "text", "text": json.dumps(
            {"reply": "已复核。", "未决": []}, ensure_ascii=False)}]),
    ]) + "\n", encoding="utf-8")
    ok, detail = ev.evidence_gate(
        FIX_NODE, {"reply": "已复核。", "未决": []},
        sp2, str(run), str(run),
        may_write=False, node_kind="contract_repair", input_evidence_ids=["root.c6.1"])
    check("证据门: 仅 prompt 含 ID、assistant 侧无引用→仍失败", ok is False, detail)

# ---------- 7) 全链回放（抢救→指针→判据，与 run_leaf 同顺序，离线） ----------
with tempfile.TemporaryDirectory() as td:
    run = Path(td)
    (run / "blocks").mkdir()
    (run / "blocks" / "sushu_implementation.md").write_text("素数文档正文内。" * 200, encoding="utf-8")
    sp = os.path.join(td, "c2.1.jsonl")
    _write_session(sp)
    obj, prov = leafrun.harvest_final_json(sp)
    assert obj is not None
    ok_ptr, _ = leafrun.resolve_doc_pointer(obj, str(run))
    ok_j, why_j, _ = leafrun.evaluate_judge(
        {"id": "root.c2.1", "role": "执行", "judge": dict(JUDGE_C21)}, obj)
    check("回放: c2.1 抢救 + 指针解析 + 判据全通",
          ok_ptr and ok_j and len(obj["doc"]) == 1600, (ok_ptr, ok_j, why_j))

# ---------- 8) 证据门引用解析：CJK 散文粘连抢救（task-a c2.1 实测） ----------
with tempfile.TemporaryDirectory() as td:
    run = Path(td)
    (run / "blocks").mkdir()
    (run / "blocks" / "solution_documentation.md").write_text("x" * 1600, encoding="utf-8")
    (run / "product").mkdir()
    (run / "product" / "answer.txt").write_text("168\n", encoding="utf-8")
    # task-a 原文引用：正则曾把整句当路径误拒，文件其实存在
    bad = ev.check_citations(
        "doc文档已生成至blocks/solution_documentation.md，详细说明了计算方法。",
        [str(run)])
    check("引用抢救: CJK 粘连的真实相对引用→通过", bad == [], bad)
    # 幻影引用（文件真不存在）→ 仍拒收
    bad = ev.check_citations("doc文档已生成至blocks/不存在的文件.md。", [str(run)])
    check("引用抢救: 幻影引用→仍拒收", len(bad) == 1 and "不存在" in bad[0]["reason"], bad)
    # 干净引用不受影响
    bad = ev.check_citations("见 blocks/solution_documentation.md 与 product/answer.txt。",
                             [str(run)])
    check("引用抢救: 干净引用→通过", bad == [], bad)
    # 绝对路径尾部粘连
    _abs = run / "abs.md"
    _abs.write_text("hello", encoding="utf-8")
    bad = ev.check_citations("见 %s文件。" % _abs, [str(run)])
    check("引用抢救: 绝对路径尾部粘连→通过", bad == [], bad)
    bad = ev.check_citations("见 %s文件。" % (run / "nope.md"), [str(run)])
    check("引用抢救: 不存在的绝对路径→仍拒收", len(bad) == 1, bad)
    # 全链：task-a c2.1 式 obj 走证据门（research kind 不触发时不相关；
    # 这里按 contract_repair 验引用核查本身不误拒真实引用）
    ok, detail = ev.evidence_gate(
        {"id": "root.fix.1", "role": "调研",
         "overrides": {"node_kind": "contract_repair", "input_evidence_ids": ["root.c6.1"]}},
        {"reply": "已核验 root.c6.1。doc文档已生成至blocks/solution_documentation.md。",
         "未决": []},
        os.path.join(td, "missing.jsonl"), str(run), str(run),
        may_write=False, node_kind="contract_repair", input_evidence_ids=["root.c6.1"])
    check("引用抢救: 修复叶 reply 含粘连真实引用→证据门通过", ok is True, detail)

# ---------- 9) 证据门引用解析：后缀唯一匹配抢救（run B c2.1.audit 实测） ----------
with tempfile.TemporaryDirectory() as td:
    run = Path(td)
    (run / "pi-agent" / "sessions").mkdir(parents=True)
    _sess = "2026-11-10T13-38-11-380Z_tree-task-b-llaat-root.c2.1.jsonl"
    (run / "pi-agent" / "sessions" / _sess).write_text("{}", encoding="utf-8")
    (run / "product").mkdir()
    (run / "product" / "answer.txt").write_text("168\n", encoding="utf-8")
    # run B 原文引用：模型把绝对路径缩写成 sessions/<basename>，真实文件唯一
    bad = ev.check_citations("sessions/%s（12 条工具调用结果）。" % _sess, [str(run)])
    check("后缀抢救: 缩写引用唯一匹配→通过", bad == [], bad)
    # 错目录引用（sessions/answer.txt，真实文件在 product/）→ 仍拒收
    bad = ev.check_citations("见 sessions/answer.txt。", [str(run)])
    check("后缀抢救: 错目录引用→仍拒收", len(bad) == 1, bad)
    # 歧义（两个同后缀文件）→ 仍拒收，不猜
    (run / "other" / "sessions").mkdir(parents=True)
    (run / "other" / "sessions" / _sess).write_text("{}", encoding="utf-8")
    bad = ev.check_citations("sessions/%s。" % _sess, [str(run)])
    check("后缀抢救: 歧义→仍拒收", len(bad) == 1, bad)

# ---------- 10) 抢救门：超时文案不限"单叶上限"（run C 实测） ----------
with tempfile.TemporaryDirectory() as td:
    # run C c2.1 会话的最小复现：最终 JSON 写进 final_output.json 两次，随后超时
    sp = os.path.join(td, "c2.1.jsonl")
    _fj = {"reply": "任务已完成。两种算法均为 168。", "未决": [], "doc": "blocks/sushu_implementation.md"}
    Path(sp).write_text("\n".join([
        _sess_line("assistant", [{"type": "toolCall", "id": "b1", "name": "bash",
                                  "arguments": {"command": "python3 -c 'print(168)'"}}]),
        _sess_line("toolResult", [{"type": "text", "text": "Method 1 (trial division): 168\n"}]),
        _sess_line("assistant", [{"type": "toolCall", "id": "w1", "name": "write",
                                  "arguments": {"content": json.dumps(_fj, ensure_ascii=False),
                                                "path": "final_output.json"}}]),
        _sess_line("assistant", [{"type": "toolCall", "id": "w2", "name": "write",
                                  "arguments": {"content": json.dumps(_fj, ensure_ascii=False),
                                                "path": "final_output.json"}}]),
    ]) + "\n", encoding="utf-8")
    obj, prov = leafrun.harvest_final_json(sp)
    check("抢救门: 超时前写进 final_output.json 的最终 JSON 可抢救",
          obj == _fj and "final_output.json" in prov, prov)
    # 门谓词本身（直接测 leafrun._should_harvest_after_failure）
    check("抢救门: TREE_PI_TIMEOUT 文案的超时在门内",
          leafrun._should_harvest_after_failure(
              "超时", "pi 到 TREE_PI_TIMEOUT：Command \"[\'sandbox-exec\', ...\'") is True, "")
    check("抢救门: '单叶上限'文案的超时在门内",
          leafrun._should_harvest_after_failure("超时", "pi 到单叶上限（research 300.0 秒）") is True, "")
    check("抢救门: loop-breaker 环境错在门内",
          leafrun._should_harvest_after_failure("环境", "loop_breaker 熔断终止：RuntimeError") is True, "")
    check("抢救门: 非熔断环境错不在门内",
          leafrun._should_harvest_after_failure("环境", "pi 找不到可执行文件") is False, "")
    check("抢救门: 无 why 不在门内",
          leafrun._should_harvest_after_failure("超时", "") is False, "")

# ---------- 11) merge 形状机械归一：uncovered / 对审计发现（Run D 实测，集成验收） ----------
# uncovered：缺 what/why 补占位
_u, _n = schema.normalize_model_uncovered([{"what": "筛法 doc 未写"}])
check("归一 uncovered: 缺 why 补占位", _u == [{"what": "筛法 doc 未写", "why": "叶未决"}] and _n, _n)
_u, _n = schema.normalize_model_uncovered([{"why": "模型未给"}])
check("归一 uncovered: 缺 what 补占位", _u == [{"what": "（未注明）", "why": "模型未给"}], _u)
# uncovered：已合规原样
_good = [{"what": "a", "why": "b"}]
_u, _n = schema.normalize_model_uncovered(_good)
check("归一 uncovered: 合规原样", _u == _good and _n == "", _n)
# uncovered：字符串条目 / 否定串 / 单 dict / 非列表
_u, _n = schema.normalize_model_uncovered(["筛法未覆盖"])
check("归一 uncovered: 字符串条目→单条", _u == [{"what": "筛法未覆盖", "why": "叶未决"}], _u)
_u, _n = schema.normalize_model_uncovered("无缺口")
check("归一 uncovered: 否定串→[]", _u == [], _u)
_u, _n = schema.normalize_model_uncovered({"what": "x", "why": "y"})
check("归一 uncovered: 单 dict 包成列表", _u == [{"what": "x", "why": "y"}], _u)
_u, _n = schema.normalize_model_uncovered(42)
check("归一 uncovered: 数字无法归一→None", _u is None and "无法归一" in _n, _n)
_u, _n = schema.normalize_model_uncovered([{"what": "a", "why": "b"}, 7, None])
check("归一 uncovered: 非对象条目丢弃", _u == [{"what": "a", "why": "b"}], _u)
# 对审计发现：处置子串映射 / 已合规原样 / 缺说明补占位
_a, _n = schema.normalize_model_audit_replies(
    [{"item": "root.c2.1", "处置": "已解掉该问题", "说明": "复核通过"}])
check("归一 处置: 子串映射→解掉", _a == [{"item": "root.c2.1", "处置": "解掉", "说明": "复核通过"}], _a)
_a, _n = schema.normalize_model_audit_replies(
    [{"item": "x", "处置": "上冒", "说明": "s", "建议": "建议继续派发"}])
check("归一 建议: 子串映射→继续派", _a[0]["建议"] == "继续派" and _a[0]["处置"] == "上冒", _a)
_a, _n = schema.normalize_model_audit_replies([{"item": "x", "处置": "记未决"}])
check("归一 处置: 缺说明补占位", _a == [{"item": "x", "处置": "记未决", "说明": "（未注明）"}], _a)
# 对审计发现：Run D 原文自由文本无映射→显式失败（不静默通过）
_a, _n = schema.normalize_model_audit_replies(
    [{"item": "a", "处置": "已消费全部输入证据 IDs，结论已复核", "说明": "s"}])
check("归一 处置: Run D 自由文本无映射→显式失败", _a is None and "显式失败" in _n, _n)
_a, _n = schema.normalize_model_audit_replies("不是列表")
check("归一 处置: 非列表→显式失败", _a is None, _n)
_a, _n = schema.normalize_model_audit_replies(["字符串条目"])
check("归一 处置: 非 dict 条目→显式失败（不丢弃）", _a is None, _n)
# 端到端：归一后的 merge 对象过 validate
_m = {"去向": "收束", "reply": "已完成复核。",
      "未决": [], "uncovered": [{"what": "筛法 doc 未写"}],
      "对审计发现": [{"item": "root.c2.1", "处置": "这个问题已解掉", "说明": "复核通过"}]}
_u, _un = schema.normalize_model_uncovered(_m["uncovered"])
_a, _an = schema.normalize_model_audit_replies(_m["对审计发现"])
_m["uncovered"], _m["对审计发现"] = _u, _a
_ok, _why = schema.validate("merge", _m, {})
check("归一 merge 端到端: 归一后 validate 通过", _ok is True, _why)

# ---------- 12) merge 收束覆盖机械补齐（integ-q01 实测，集成验收补丁 2） ----------
def _mk_driver_with_ledger(td):
    """最小 Driver：run_dir 下写 evidence_ledger.jsonl；树含 root + batch 4 的三叶。"""
    import json as _json
    led = [
        {"node_id": "root.c4.1", "result_type": "failed",
         "unresolved": [{"type": "缺信息", "what": "筛法 doc 未写完", "why": "叶熔断"}]},
        {"node_id": "root.c4.2", "result_type": "partial",
         "unresolved": [{"type": "超时", "what": "超时前 doc 只有 250 字", "why": "pi 超时"}]},
        {"node_id": "root.c4.3", "result_type": "complete", "unresolved": []},
    ]
    Path(td, "evidence_ledger.jsonl").write_text(
        "\n".join(_json.dumps(e, ensure_ascii=False) for e in led), encoding="utf-8")
    parent = {"id": "root", "batch": 4, "state": "等子节点", "round": 4, "act": "spawn"}
    tree = {"root": parent,
            "root.c4.1": {"id": "root.c4.1", "parent": "root", "batch": 4, "state": "失败"},
            "root.c4.2": {"id": "root.c4.2", "parent": "root", "batch": 4, "state": "部分"},
            "root.c4.3": {"id": "root.c4.3", "parent": "root", "batch": 4, "state": "完成"}}
    from harness import driver as _drv
    return _drv.Driver(parent, tree, run_dir=td), parent


with tempfile.TemporaryDirectory() as td:
    _d, _p = _mk_driver_with_ledger(td)
    # integ-q01 原样：模型给了 3 条空占位 uncovered，未点名失败分支
    _m = {"去向": "收束", "reply": "收束。", "未决": [],
          "uncovered": [{"what": "（未注明）", "why": "叶未决"}]}
    _missing = _d._uncovered_missing_coverage(_p, _m)
    check("覆盖补齐: 未覆盖分支检出 c4.1/c4.2",
          [n for n, _ in _missing] == ["root.c4.1", "root.c4.2"], _missing)
    _ok, _why = _d._check_partial_coverage(_p, _m)
    check("覆盖补齐: 补齐前终检拒收", _ok is False and "root.c4.1" in _why, _why)
    _rep = _d._repair_merge_fields(_p, _m)
    check("覆盖补齐: 机械补记两条",
          "uncovered_coverage_topup" in _rep and len(_m["uncovered"]) == 3, _rep)
    check("覆盖补齐: 补记条目含节点 id",
          "root.c4.1（failed）未被覆盖" in _m["uncovered"][1]["what"], _m["uncovered"])
    _ok, _why = _d._check_partial_coverage(_p, _m)
    check("覆盖补齐: 补齐后终检通过", _ok is True, _why)

with tempfile.TemporaryDirectory() as td:
    _d, _p = _mk_driver_with_ledger(td)
    # 已覆盖（点名 c4.1、c4.2 未决前 15 字）→ 不补
    _m = {"去向": "收束", "reply": "收束。", "未决": [],
          "uncovered": [{"what": "root.c4.1 熔断失败", "why": "见未决"},
                        {"what": "超时前 doc 只有 250 字", "why": "pi 超时"}]}
    _rep = _d._repair_merge_fields(_p, _m)
    check("覆盖补齐: 已覆盖不补", "uncovered_coverage_topup" not in _rep
          and len(_m["uncovered"]) == 2, _rep)
    _ok, _why = _d._check_partial_coverage(_p, _m)
    check("覆盖补齐: 已覆盖终检通过", _ok is True, _why)

with tempfile.TemporaryDirectory() as td:
    _d, _p = _mk_driver_with_ledger(td)
    # 非收束去向 → 不补（覆盖检查只在收束跑）
    _m = {"去向": "再派", "reply": "再派。", "未决": [],
          "下一批要点": ["root.c4.1：筛法 doc 未写完"],
          "uncovered": [{"what": "（未注明）", "why": "叶未决"}]}
    _rep = _d._repair_merge_fields(_p, _m)
    check("覆盖补齐: 非收束不补", "uncovered_coverage_topup" not in _rep, _rep)

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: %s" % FAIL)
    sys.exit(1)
