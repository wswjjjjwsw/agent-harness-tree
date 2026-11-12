"""证据门离线回归（t-d301b69799d2 续跑）。不跑 pi、不碰模型服务，纯离线。

跑法：cd <worktree> && python3 tests/cases-evidence.py
场景覆盖：零调用叶拒收 / 成功工具调用放行 / 坏引用（绝对/相对/超行号）拒收 /
好引用放行 / 非调查叶与非调研 role 不触发 / 合法重复查询不罚 /
全失败工具结果拒收 / build_argv 分流（tools 旋钮定工具、其余 --no-tools）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness import evidence, role as role_mod, session as session_mod  # noqa: E402

WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_RUN = os.path.join(WORKTREE, "runs", "e2e-20261105-final-88c2ad4")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS %s" % name)
    else:
        FAIL += 1
        print("FAIL %s :: %s" % (name, detail))


def fake_session(path, tool_results):
    """tool_results: [(name, result_text)] → pi 形状的 JSONL（assistant toolCall + toolResult 行）。"""
    lines = [
        {"type": "session", "version": 3, "id": "x", "timestamp": "2026-11-05T00:00:00.000Z", "cwd": WORKTREE},
        {"type": "model_change", "id": "1", "parentId": None, "timestamp": "2026-11-05T00:00:00.100Z",
         "provider": "bonsai", "modelId": "/opt/models/sage-a1.5-4bit-mlx"},
        {"type": "message", "id": "2", "parentId": "1", "timestamp": "2026-11-05T00:00:00.200Z",
         "message": {"role": "system", "content": "", "sections": {}}},
        {"type": "message", "id": "3", "parentId": "2", "timestamp": "2026-11-05T00:00:00.300Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "调查本机模型"}]}},
    ]
    blocks = []
    for i, (name, _text) in enumerate(tool_results):
        blocks.append({"type": "toolCall", "id": "tc-%d" % i, "name": name, "arguments": {"path": "/tmp/x"}})
    lines.append({"type": "message", "id": "4", "parentId": "3", "timestamp": "2026-11-05T00:00:01.000Z",
                  "message": {"role": "assistant", "content": blocks}})
    for i, (_name, text) in enumerate(tool_results):
        lines.append({"type": "message", "id": "5-%d" % i, "parentId": "4",
                      "timestamp": "2026-11-05T00:00:02.000Z",
                      "message": {"role": "toolResult", "content": [{"type": "text", "text": text}]}})
    lines.append({"type": "message", "id": "6", "parentId": "5",
                  "timestamp": "2026-11-05T00:00:03.000Z",
                  "message": {"role": "assistant",
                              "content": [{"type": "text", "text": '{"reply": "ok", "未决": []}'}]}})
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return path


def main():
    tmp = tempfile.mkdtemp(prefix="evidence-gate-")
    inv_node = {"role": "调研", "id": "root.c2.1",
                "context": "调查本机已安装的本地模型（Qwen3.8-27B、Sage-A1.5）的元数据：模型大小、"
                           "内存占用、可用上下文长度、dflash 推理基准日志；确认 Qwen3.8-27B 在 "
                           "dflash 下是否真的 20t/s。"}
    reply_ok = ("已实测：内存 34359738368 来自 sysctl；旧 run 状态见 "
                "/opt/agent-harness/runs/.candidate-49dec4ba/runs/"
                "e2e-20261105-final-88c2ad4/state.json；候选实现见 harness/blocks.py。")
    reply_bad_rel = "切换成本详见 blocks/switch-cost-detail.md。"
    reply_bad_abs = "Qwen 权重见 /opt/models/qwen-27b-不存在。"
    reply_bad_line = ("证据门实现见 /opt/agent-harness/runs/.candidate-49dec4ba/"
                      "harness/evidence.py，第 999999 行有量化说明。")

    # 1. 旧 run 回放：c2.1 真实 session（6 行、零工具）+ 旧 reply ⇒ 门拒收
    old_sess = os.path.join(OLD_RUN, "pi-agent", "sessions",
                            "2026-11-05T06-17-15-655Z_tree-e2e-20261105-final-88c2ad4-root.c2.1.jsonl")
    old_reply = ("已读取本机本地模型日志与元数据。Qwen3.8-27B 在 dflash 下实测 20t/s"
                 "（来自 dflash 推理基准日志，第 42-48 行），上下文长度 131072 tokens，"
                 "显存占用约 54GB（MLX 加载日志）。")
    ok, detail = evidence.evidence_gate(inv_node, {"reply": old_reply}, old_sess,
                                        os.path.join(OLD_RUN), WORKTREE)
    check("旧run回放：零调用叶拒收", not ok and "零成功工具结果" in detail, detail)

    # 2. 成功工具调用 + 好引用 ⇒ 放行
    good_sess = fake_session(os.path.join(tmp, "good.jsonl"),
                             [("bash", "hw.memsize: 34359738368\n"),
                              ("read", "sage README line1\nline2\n")])
    ok, detail = evidence.evidence_gate(inv_node, {"reply": reply_ok}, good_sess,
                                        os.path.join(OLD_RUN), WORKTREE)
    check("成功工具+好引用放行", ok, detail)

    # 3. 幻影相对引用 ⇒ 拒收
    ok, detail = evidence.evidence_gate(inv_node, {"reply": reply_bad_rel}, good_sess,
                                        os.path.join(OLD_RUN), WORKTREE)
    check("幻影相对引用拒收", not ok and "blocks/switch-cost-detail.md" in detail, detail)

    # 4. 不存在的绝对引用 ⇒ 拒收
    ok, detail = evidence.evidence_gate(inv_node, {"reply": reply_bad_abs}, good_sess,
                                        os.path.join(OLD_RUN), WORKTREE)
    check("不存在绝对引用拒收", not ok and "qwen-27b-不存在" in detail, detail)

    # 5. 行号超出文件行数 ⇒ 拒收
    ok, detail = evidence.evidence_gate(inv_node, {"reply": reply_bad_line}, good_sess,
                                        os.path.join(OLD_RUN), WORKTREE)
    check("超行号引用拒收", not ok and "超出文件行数" in detail, detail)

    # 6. 非调查叶（无标记）零调用 ⇒ 不触发、放行
    plain_node = {"role": "调研", "id": "root.c9.9", "context": "总结替代方案的一般思路"}
    ok, detail = evidence.evidence_gate(plain_node, {"reply": old_reply}, old_sess,
                                        os.path.join(OLD_RUN), WORKTREE)
    check("非调查调研叶不触发", ok, detail)

    # 7. 非调研 role 有调查标记 ⇒ 不触发、放行
    exec_node = {"role": "执行", "id": "root.e1", "context": "调查本机日志并写报告"}
    ok, detail = evidence.evidence_gate(exec_node, {"reply": old_reply}, old_sess,
                                        os.path.join(OLD_RUN), WORKTREE)
    check("非调研role不触发", ok, detail)

    # 8. 合法重复查询/轮询（3 次相同成功调用）⇒ 放行，不罚重复
    rep_sess = fake_session(os.path.join(tmp, "rep.jsonl"),
                            [("bash", "hw.memsize: 34359738368\n")] * 3)
    ok, detail = evidence.evidence_gate(inv_node, {"reply": reply_ok}, rep_sess,
                                        os.path.join(OLD_RUN), WORKTREE)
    check("合法重复查询放行", ok, detail)

    # 9. 全失败工具结果（ENOENT）⇒ 拒收
    err_sess = fake_session(os.path.join(tmp, "err.jsonl"),
                            [("read", "ENOENT: no such file or directory, access '/etc/hostname'")])
    ok, detail = evidence.evidence_gate(inv_node, {"reply": reply_ok}, err_sess,
                                        os.path.join(OLD_RUN), WORKTREE)
    check("全失败工具结果拒收", not ok and "零成功工具结果" in detail, detail)

    # 10. 会话文件缺失 ⇒ 拒收（不静默放行）
    ok, detail = evidence.evidence_gate(inv_node, {"reply": reply_ok},
                                        os.path.join(tmp, "nope.jsonl"),
                                        os.path.join(OLD_RUN), WORKTREE)
    check("会话缺失拒收", not ok and "零成功工具结果" in detail, detail)

    # 11. build_argv 分流
    a = session_mod.build_argv({"role": "调研"}, "leaf", role_mod.effective("调研"), "P", "s")
    check("build_argv 调研叶给只读工具", "--no-tools" not in a and "--tools" in a
          and a[a.index("--tools") + 1] == "read,bash,grep,find,ls", str(a))
    b = session_mod.build_argv({"role": "执行"}, "leaf", role_mod.effective("执行"), "P", "s")
    # V2.2 根因修：执行叶是唯一 may_write 角色，prompt 要求实测+落盘，旧断言锁定的
    # --no-tools 正是 P5 tier1-2 执行叶 0 工具调用的根因。现给工具（含 write）。
    check("build_argv 执行叶给工具（含 write）", "--no-tools" not in b and "--tools" in b
          and b[b.index("--tools") + 1] == "read,bash,grep,find,ls,write", str(b))
    c = session_mod.build_argv({"role": "根"}, "merge", role_mod.effective("根"), "P", "s")
    check("build_argv merge 维持 --no-tools", "--no-tools" in c and "--tools" not in c, str(c))
    d = session_mod.build_argv({"role": "审计"}, "leaf", role_mod.effective("审计"), "P", "s")
    check("build_argv 审计叶维持 --no-tools", "--no-tools" in d and "--tools" not in d, str(d))

    # 12b. 绝对路径不得衍生幻影相对引用（.candidate-49dec4ba/… 的尾巴不是相对引用）
    bad = evidence.check_citations(
        "状态见 /opt/agent-harness/runs/.candidate-49dec4ba/runs/"
        "e2e-20261105-final-88c2ad4/state.json。",
        [os.path.join(OLD_RUN), WORKTREE])
    check("绝对路径不衍生相对引用", bad == [], str(bad))

    # 12. 旧 run 三叶全部零调用（c2.2/c2.3 同样回放）
    for leaf, fname in (("c2.2", "2026-11-05T06-17-21-180Z_tree-e2e-20261105-final-88c2ad4-root.c2.2.jsonl"),
                        ("c2.3", "2026-11-05T06-17-25-864Z_tree-e2e-20261105-final-88c2ad4-root.c2.3.jsonl")):
        p = os.path.join(OLD_RUN, "pi-agent", "sessions", fname)
        cnt = evidence.count_tool_results(p)
        check("旧run %s 零成功工具结果" % leaf, cnt["successes"] == 0 and cnt["calls"] == 0, str(cnt))

    # 13. 第四轮（integ3-q01/integ3-agg2 实测）：审计叶引用不存在文件——
    # 省略号缩写与工具名伪路径的机械抢救（确定性、只认磁盘、歧义即拒）。
    run3 = os.path.join(tmp, "run3")
    os.makedirs(os.path.join(run3, "product"), exist_ok=True)
    os.makedirs(os.path.join(run3, "pi-agent", "sessions"), exist_ok=True)
    with open(os.path.join(run3, "product", "answer.txt"), "w", encoding="utf-8") as fh:
        fh.write("168\n")
    sess3 = os.path.join(run3, "pi-agent", "sessions",
                         "2026-11-10T23-22-41-763Z_tree-integ3-q01-root.c4.1.jsonl")
    with open(sess3, "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    # 13a. 长绝对路径缩写 "/tmp/.../root.c4.1.jsonl" ⇒ 尾部唯一对应真实 session 文件
    # （文件名后缀须对到 "-" 边界：myroot.c4.1.jsonl 不算命中）
    with open(os.path.join(run3, "pi-agent", "sessions",
                           "2026-11-10T00-00-00-000Z_tree-x-myroot.c4.1.jsonl"),
              "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    rescued = evidence._rescue_ellipsis_cite(
        "/tmp/integ-accept3/.../root.c4.1.jsonl", [run3])
    check("省略号救援直测（尾部唯一）", rescued == sess3, rescued)
    bad = evidence.check_citations(
        "session 见 /tmp/integ-accept3/.../root.c4.1.jsonl，为完整日志。",
        [run3, WORKTREE])
    check("省略号缩写救援（尾部唯一）", bad == [], str(bad))
    # 13b. 尾部过短无法定位 "/tmp/.../jsonl" ⇒ 缩写不是字面断言，不计坏引用
    bad = evidence.check_citations(
        "另外，/tmp/integ-accept3/.../jsonl 路径下的原始日志未实际展读。",
        [run3, WORKTREE])
    check("省略号缩写跳过（尾部无法定位）", bad == [], str(bad))
    # 13c. 工具名伪路径 "write_file/answer.txt" ⇒ 去首段后唯一对应 product/answer.txt
    bad = evidence.check_citations(
        "证据中无 write_file/answer.txt 操作记录，仅有 17 条成功工具调用。",
        [run3, WORKTREE])
    check("工具名伪路径救援", bad == [], str(bad))
    # 13d. 真不存在的相对引用仍记坏引用（不放水）
    bad = evidence.check_citations(
        "切换成本详见 blocks/does-not-exist.md。", [run3, WORKTREE])
    check("真不存在引用仍拒收", len(bad) == 1 and "does-not-exist" in bad[0]["cite"], str(bad))
    # 13e. 去首段后缀歧义（两处同名文件）⇒ 不猜，仍记坏引用
    with open(os.path.join(run3, "product", "dup.txt"), "w", encoding="utf-8") as fh:
        fh.write("a\n")
    os.makedirs(os.path.join(run3, "blocks"), exist_ok=True)
    with open(os.path.join(run3, "blocks", "dup.txt"), "w", encoding="utf-8") as fh:
        fh.write("b\n")
    bad = evidence.check_citations("见 write_file/dup.txt。", [run3, WORKTREE])
    check("去首段后缀歧义仍拒收", len(bad) == 1, str(bad))

    # Known run-root location prose is not a file citation; arbitrary directories remain invalid.
    root_note = u'\u6587\u4ef6\u5728\u672c\u6b21run\u76ee\u5f55 %s/ \u4e0b\u3002' % run3
    bad = evidence.check_citations(root_note, [run3, WORKTREE])
    check("known run-root location accepted", bad == [], str(bad))
    other_dir = os.path.join(run3, 'product')
    bad = evidence.check_citations(u'\u6587\u4ef6\u5728 %s \u76ee\u5f55\u3002' % other_dir,
                                   [run3, WORKTREE])
    check("non-root directory citation rejected", len(bad) == 1, str(bad))
    missing = os.path.join(run3, 'not-present.md')
    bad = evidence.check_citations('file at %s' % missing, [run3, WORKTREE])
    check("missing file under run root rejected", len(bad) == 1, str(bad))
    bad = evidence.check_citations(root_note + u'\u7b2c 2 \u884c', [run3, WORKTREE])
    check("line claim on run-root directory rejected", len(bad) == 1, str(bad))
    # 14. 增量审计必须逐字使用绑定的真实证据路径；伪路径或不引用证据均拒收。
    audit_node = {"role": "审计", "id": "root.c4.1.audit", "context": "只读复核"}
    audit_kwargs = dict(node_kind="audit", input_evidence_ids=["product/answer.txt"],
                        may_write=False)
    ok, detail = evidence.evidence_gate(
        audit_node, {"reply": "已复核 product/answer.txt，内容为 168。", "未决": []},
        sess3, run3, WORKTREE, **audit_kwargs)
    check("审计引用绑定的真实证据路径放行", ok, detail)
    ok, detail = evidence.evidence_gate(
        audit_node, {"reply": "复核完成，未发现异常。", "未决": []},
        sess3, run3, WORKTREE, **audit_kwargs)
    check("审计未引用输入证据拒收", not ok and "未引用任何输入证据" in detail, detail)
    ok, detail = evidence.evidence_gate(
        audit_node, {"reply": "已复核 result/answer.txt。", "未决": []},
        sess3, run3, WORKTREE, **audit_kwargs)
    check("审计伪路径仍拒收", not ok and "result/answer.txt" in detail, detail)
    print("---- %d passed, %d failed ----" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
