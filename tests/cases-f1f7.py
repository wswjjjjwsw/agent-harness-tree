"""F1-F7 回归（离线，t-d301b69799d2）。跑法：cd <候选根> && python3 tests/cases-f1f7.py"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import session as session_mod, submit, evidence, leafrun, role as role_mod

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:200]) if extra and not cond else ""))

node_r = {"role": "调研"}
a = session_mod.build_argv(node_r, "leaf", role_mod.effective("调研"), "prompt", "sid-1")
check("F7 build_argv 调研叶含 -e loop-breaker.ts", "-e" in a and any("loop-breaker.ts" in x for x in a), a)
check("F7 build_argv 调研叶有只读工具", "--tools" in a and "read,bash,grep,find,ls" in a, a)
a2 = session_mod.build_argv({"role": "执行"}, "leaf", role_mod.effective("执行"), "prompt", "sid-2")
# V2.2 根因修：执行叶给工具（含 write），loop-breaker -e 同样生效（防重复调同一 bash）。
check("F7 build_argv 执行叶给工具（含 write）且含 -e",
      "--no-tools" not in a2 and "-e" in a2 and "--tools" in a2
      and "read,bash,grep,find,ls,write" in a2, a2)
a3 = session_mod.build_argv(node_r, "merge", role_mod.effective("调研"), "prompt", "sid-3")
check("F7 build_argv merge 维持 --no-tools", "--no-tools" in a3 and "-e" not in a3, a3)

c_ro = submit.contract_note("leaf", knobs={"may_write": False})
check("F1 may_write=false 不含块字段约定", "块字段" not in c_ro, c_ro[:300])
check("F1 may_write=false 含只读约束", "只读约束" in c_ro and "不许产出 doc/edits" in c_ro)
c_w = submit.contract_note("leaf", knobs={"may_write": True})
check("F1 may_write=true 保留块字段约定", "块字段" in c_w)

check("F7-count no such field 判失败", evidence._is_error_text("log: invalid predicate: no such field: processName"))
check("F7-count invalid predicate 判失败", evidence._is_error_text("invalid predicate: foo"))
check("F7-count 正常结果不判失败", not evidence._is_error_text("hw.memsize = 34359738368"))
check("F7-count command not found 判失败", evidence._is_error_text("zsh: command not found: foo"))

def _toolcall(name, cmd):
    return {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "name": name, "arguments": {"command": cmd}}]}}

def _toolresult(text):
    return {"type": "message", "message": {"role": "toolResult", "content": [{"text": text}]}}

def _sess(tmp, lines, name="s.jsonl"):
    p = Path(tmp) / name
    p.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", encoding="utf-8")
    return str(p)

with tempfile.TemporaryDirectory() as tmp:
    sp = _sess(tmp, [_toolcall("bash", "mkdir -p blocks; cat > blocks/x.md << 'EOF'\nabc\nEOF"),
                     _toolresult("ok")], name="s-write.jsonl")
    atts = evidence.find_write_attempts(sp)
    check("F2 写入扫描检出 mkdir/cat>", len(atts) >= 1, atts)
    sp2 = _sess(tmp, [_toolcall("bash", "sysctl hw.memsize"), _toolresult("hw.memsize = 34359738368")], name="s-read.jsonl")
    check("F2 写入扫描放行只读命令", evidence.find_write_attempts(sp2) == [])
    node = {"role": "调研", "context": "调查本机模型元数据，核查实测数据"}
    obj = {"reply": "实测完成", "未决": []}
    ok, detail = evidence.evidence_gate(node, obj, sp, tmp, str(ROOT), may_write=False)
    check("F2 证据门 may_write=false 拒收写入叶", not ok and "只读违规" in detail, detail)
    ok2, _ = evidence.evidence_gate(node, obj, sp2, tmp, str(ROOT), may_write=False)
    check("F2 证据门只读成功叶放行", ok2)
    sp3 = _sess(tmp, [], name="s-empty.jsonl")
    ok3, d3 = evidence.evidence_gate(node, obj, sp3, tmp, str(ROOT), may_write=False)
    check("F2 证据门零调用拒收", not ok3 and "零成功工具结果" in d3, d3)
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = tmp
    sid = "tree-run1-root.c2.1"
    sessf = Path(tmp) / ("20261105_%s.jsonl" % sid)
    sessf.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in [
        _toolcall("bash", "sysctl hw.memsize"), _toolresult("hw.memsize = 34359738368"),
        _toolcall("bash", "du -sh /tmp"), _toolresult("1.0G\t/tmp")]) + "\n", encoding="utf-8")
    salv = leafrun.salvage_partial_evidence(tmp, "root.c2.1", sid)
    check("F3/F5 抢救落盘 partial/root.c2.1.json", Path(tmp, "partial", "root.c2.1.json").is_file() and salv["successes"] == 2, salv)
    tree = {"root": {"state": "完成", "reply": "结论正文", "merge": {"去向": "收束", "uncovered": []}},
            "root.c2.1": {"role": "调研", "state": "完成", "reply": "实测见 /tmp/x.md 第 3 行"}}
    dp = leafrun.gen_doc_assessment(tree, tmp, "run1")
    txt = Path(dp).read_text(encoding="utf-8")
    check("F6 doc/夜间窗口评估.md 生成", Path(dp).is_file() and "run1" in txt and "夜间窗口评估" in txt)
    check("F6 doc 含 sha256 行", "sha256" in txt)

print("---- %d 通过，%d 失败 ----" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
