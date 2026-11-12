#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CC 接线第 4 步重测（demo/cc_remeasure.py）—— 全部用例（离线）。

自包含、可重复跑：不联网、不打 8903/8092、不起 pi、不调 CC、不依赖外部状态
（sandbox 文件用临时合成数据代替；真实 ground truth 另由报告里的 grep 核对）。
只测重测脚本的纯函数：notes 数字解析、edits 应用、cc-D 分块、pre_leaf 贴段、
cc-C hook（应用 edits + 重跑验证）、叶文本拼接、案例注册表。
真实 Sage 调用只在 demo/cc_remeasure.py 跑，不在这里。

任一用例 FAIL ⇒ 退出码 ≠ 0；全绿 ⇒ rc=0，且最后一行打印 `GATES: <pass>/<total> PASS`。
用法：cd <候选 worktree 根> && python3 tests/cases-ccremeasure.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TREE_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))

# demo/cc_remeasure.py 不是包，用 importlib 按文件加载（它只定义函数，不跑 main）。
_spec = importlib.util.spec_from_file_location(
    "cc_remeasure", str(ROOT / "demo" / "cc_remeasure.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ---------------------------------------------------------------- 数字解析
@case("parse_total_总计")
def _(_x):
    out = _mod._parse_notes_numbers(
        "data.txt 175 bytes / notes_src.txt 244 bytes，总计 419 bytes")
    expect(out == {"data.txt": 175, "notes_src.txt": 244, "total": 419},
           "总计写法应解析出 419，实际 %s" % out)


@case("parse_total_paren_triple")
def _(_x):
    # “总数（175/244/419）”括号里第一个数是 data.txt 的数，不是总数
    out = _mod._parse_notes_numbers(
        "确认 notes.md 两行数字与总数（175/244/419）与驱动器提供的文件大小一致")
    expect(out.get("total") == 419, "括号三元组应取最后一个数 419，实际 %s" % out)


@case("parse_total_TOTAL_eq")
def _(_x):
    out = _mod._parse_notes_numbers("data.txt 175\nnotes_src.txt 244\nTOTAL=419")
    expect(out.get("total") == 419, "TOTAL= 写法应解析出 419，实际 %s" % out)


@case("parse_no_total")
def _(_x):
    out = _mod._parse_notes_numbers("data.txt 175 bytes，notes_src.txt 244 bytes")
    expect("total" not in out, "无总数写法不应有 total 键，实际 %s" % out)
    expect(out.get("data.txt") == 175 and out.get("notes_src.txt") == 244,
           "两文件数仍应解析，实际 %s" % out)


# ---------------------------------------------------------------- edits 应用
def _calc_file(td: Path) -> Path:
    p = td / "calc.py"
    p.write_text("total = 0\nfor i in range(1, 10):\n    total += i\n"
                 "total = total * 2\nprint(\"TOTAL=%d\" % total)\n", encoding="utf-8")
    return p


@case("apply_edits_replace")
def _(_x):
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        _calc_file(run_dir)
        info = _mod._apply_edits(run_dir, [
            {"path": "calc.py", "section": "for i in range(1, 10):",
             "after_text": "for i in range(1, 11):"}])
        expect(info["applied"] == 1 and info["skipped"] == 0, "应应用 1 条，实际 %s" % info)
        expect("range(1, 11)" in run_dir.joinpath("calc.py").read_text(encoding="utf-8"),
               "range 行应被替换为 range(1, 11)")


@case("apply_edits_delete")
def _(_x):
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        _calc_file(run_dir)
        info = _mod._apply_edits(run_dir, [
            {"path": "calc.py", "section": "total = total * 2", "after_text": ""}])
        expect(info["applied"] == 1, "删除应应用 1 条，实际 %s" % info)
        expect("total * 2" not in run_dir.joinpath("calc.py").read_text(encoding="utf-8"),
               "total * 2 行应被删除")


@case("apply_edits_skip_bad_path")
def _(_x):
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        _calc_file(run_dir)
        info = _mod._apply_edits(run_dir, [
            {"path": "nope.py", "section": "x", "after_text": "y"}])
        expect(info["applied"] == 0 and info["skipped"] == 1,
               "未知路径应跳过，实际 %s" % info)


@case("apply_edits_skip_no_match")
def _(_x):
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        _calc_file(run_dir)
        info = _mod._apply_edits(run_dir, [
            {"path": "calc.py", "section": "不存在的某一行", "after_text": "y"}])
        expect(info["applied"] == 0 and info["skipped"] == 1,
               "定位不到 section 应跳过，实际 %s" % info)


# ---------------------------------------------------------------- cc-D 分块
@case("chunk_d_four_chunks")
def _(_x):
    with tempfile.TemporaryDirectory() as td:
        sb = Path(td) / "sandbox-d"
        sb.mkdir()
        lines = []
        for i in range(603):
            lines.append("Record %04d: the shed received copper fittings.\n" % i)
        lines[151] = "NEEDLE-ALPHA: the vault code is 4821.\n"
        lines[302] = "NEEDLE-BETA: the backup key is ORCHID-77.\n"
        lines[453] = "NEEDLE-GAMMA: the safe combination is 19-04-73.\n"
        (sb / "big.txt").write_text("".join(lines), encoding="utf-8")
        old = _mod.SB_D
        _mod.SB_D = sb
        try:
            run_dir = Path(td) / "run"
            run_dir.mkdir()
            facts = _mod._cc_d_prepare(run_dir)
        finally:
            _mod.SB_D = old
        expect(len(facts["chunks"]) == 4, "应切 4 段，实际 %d" % len(facts["chunks"]))
        expect(facts["n_lines"] == 603, "总行数 603，实际 %d" % facts["n_lines"])
        expect(facts["truth"]["copper_total"] == 600,
               "copper 总数应为 600（603 行减 3 行 NEEDLE），实际 %s"
               % facts["truth"]["copper_total"])
        expect(facts["truth"]["needles"]["NEEDLE-ALPHA"] == "4821", "ALPHA 应为 4821")
        c2 = (run_dir / "chunk-2.txt").read_text(encoding="utf-8")
        expect("NEEDLE-ALPHA" in c2, "第 152 行（ALPHA）应在 chunk-2")
        c1 = (run_dir / "chunk-1.txt").read_text(encoding="utf-8")
        expect("NEEDLE-ALPHA" not in c1, "chunk-1 不应含 NEEDLE")


@case("pre_leaf_attaches_chunk")
def _(_x):
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        (run_dir / "chunk-2.txt").write_text("line-a\nline-b\n", encoding="utf-8")
        child = {"id": "root.c2.2", "context": "你的段是 chunk-2.txt；找 NEEDLE。"}
        note = _mod._cc_d_pre_leaf(child, {}, run_dir)
        expect("chunk-2" in note and "已贴" in note, "应贴段并返回说明，实际 %r" % note)
        expect("line-a" in child["context"], "段全文应贴进子 context")
        expect(child.get("_chunk_attached") == 2, "应标记 _chunk_attached=2")
        note2 = _mod._cc_d_pre_leaf(child, {}, run_dir)
        expect(note2 == "", "重复调用不应重复贴段")


@case("pre_leaf_no_ref")
def _(_x):
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        child = {"id": "root.c2.1", "context": "汇总摘要，不提 chunk。"}
        note = _mod._cc_d_pre_leaf(child, {}, run_dir)
        expect(note == "", "无 chunk 引用时应返回空串，实际 %r" % note)
        expect(child["context"] == "汇总摘要，不提 chunk。", "context 不应被改动")


# ---------------------------------------------------------------- cc-C hook
@case("cc_c_hook_applies_edits")
def _(_x):
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        _calc_file(run_dir)
        (run_dir / "edits.jsonl").write_text(json.dumps({
            "node_id": "root.c2.1",
            "edits": [
                {"path": "calc.py", "section": "for i in range(1, 10):",
                 "after_text": "for i in range(1, 11):"},
                {"path": "calc.py", "section": "total = total * 2",
                 "after_text": ""}]}, ensure_ascii=False) + "\n", encoding="utf-8")
        node = {"id": "root.c2.1", "role": "执行",
                "reply": "两个 bug：range(1, 10) 漏 10；total * 2 多余翻倍。"}
        state = {"run_dir": run_dir, "root": {"context": "任务：修 calc.py。"},
                 "facts": {}}
        note = _mod._cc_c_on_leaf_done(node, {}, state)
        expect(state["cc_c_verify"]["total_ok"], "重跑应得 TOTAL=55，实际 %s"
               % state["cc_c_verify"])
        expect(state["cc_c_verify"]["edits_applied"]["applied"] == 2,
               "两条 edits 都应应用，实际 %s" % state["cc_c_verify"]["edits_applied"])
        expect("TOTAL=55" in note, "贴回根 context 的验证应含 TOTAL=55")
        expect("TOTAL=55" in state["root"]["context"], "根 context 应追加验证结果")


@case("cc_c_hook_fallback_known_fix")
def _(_x):
    # edits 无法应用（section 对不上），但诊断文本正确点出两个 bug → 驱动器兜底修复
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        _calc_file(run_dir)
        (run_dir / "edits.jsonl").write_text(json.dumps({
            "node_id": "root.c2.1",
            "edits": [{"path": "calc.py", "section": "对不上的行",
                       "after_text": "x"}]}, ensure_ascii=False) + "\n", encoding="utf-8")
        node = {"id": "root.c2.1", "role": "执行",
                "reply": "bug1 是 range(1, 10) 应为 range(1, 11)；bug2 是 total * 2 多余翻倍。"}
        state = {"run_dir": run_dir, "root": {"context": "任务：修 calc.py。"},
                 "facts": {}}
        _mod._cc_c_on_leaf_done(node, {}, state)
        expect(state["cc_c_verify"]["total_ok"], "兜底修复后重跑应得 TOTAL=55，实际 %s"
               % state["cc_c_verify"])
        expect("兜底" in (state["cc_c_verify"].get("fallback") or ""),
               "应记录兜底修复，实际 %s" % state["cc_c_verify"].get("fallback"))


# ---------------------------------------------------------------- 叶文本与注册表
@case("leaf_full_text")
def _(_x):
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        (run_dir / "doc").mkdir()
        (run_dir / "doc" / "root_c2_1.md").write_text("诊断全文：range 漏 10。", encoding="utf-8")
        node = {"id": "root.c2.1", "reply": "两个 bug。",
                "未决": [{"type": "缺信息", "what": "GPU", "why": "无工具"}]}
        text = _mod._leaf_full_text(node, run_dir)
        expect("两个 bug" in text and "range 漏 10" in text and "GPU" in text,
               "reply + doc + 未决都应进文本，实际 %r" % text[:120])


@case("case_registry_complete")
def _(_x):
    expect(set(_mod.CASES) == {"del-A", "del-B", "del-C", "del-D", "del-E",
                               "cc-A", "cc-Ainj", "cc-B", "cc-C", "cc-D"},
           "注册表应有 10 个案例，实际 %s" % sorted(_mod.CASES))
    for name, spec in _mod.CASES.items():
        for key in ("prepare", "context", "verify"):
            expect(callable(spec.get(key)), "%s 缺 %s" % (name, key))
    for name in ("del-A", "del-B", "del-C", "del-D", "del-E"):
        ctx = _mod.CASES[name]["context"]({})
        expect(len(ctx) > 100 and "任务" in ctx, "%s 的 context 应含任务文本" % name)


def main() -> int:
    passed = 0
    failed = []
    for name, fn in CASES:
        try:
            fn(None)
            passed += 1
            print("PASS %s" % name)
        except Exception as e:  # noqa: BLE001
            failed.append((name, e))
            print("FAIL %s: %r" % (name, e))
    print("GATES: %d/%d PASS" % (passed, len(CASES)))
    if failed:
        for name, e in failed:
            print("  FAIL %s: %r" % (name, e))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
