#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""落盘 doc 机械核验回归（第二轮 A，集成验收 t-132692485362）。

跑法：cd <候选根> && python3 tests/cases-disk-doc.py
只认磁盘，不认模型自报字数：
  1. 字符串判据叶（ALT-1b 原样）："文档字数不少于 1500 字" + 会话写出 207 字 doc ⇒ 失败；
  2. Q02 原样：doc 文件 1499 字（差 1 字）⇒ 失败；
  3. 模型谎称：inline doc 2000 字、落盘文件 207 字 ⇒ 以落盘为准，失败；
  4. 达标：落盘 1600 字 ⇒ 通过；
  5. 无字数要求 ⇒ 直通；无候选文件 ⇒ 失败（fail-closed）；
  6. 审计/调研 role 的字符串判据不抽字数 ⇒ 直通（1500 字指的是被审的 doc）；
  7. dict judge chars_min=1500 + 点名文件缺失 ⇒ 失败。
"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import leafrun

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))


def _sess_line(role, content):
    return json.dumps({"type": "message", "message": {"role": role, "content": content}},
                      ensure_ascii=False)


def _write_session(path, writes):
    """writes: [(path, content)] —— assistant write 工具调用序列。"""
    lines = [_sess_line("user", [{"type": "text", "text": "你是叶子：执行。"}])]
    for i, (p, c) in enumerate(writes):
        lines.append(_sess_line("assistant", [{"type": "toolCall", "id": "w%d" % i,
                                               "name": "write",
                                               "arguments": {"content": c, "path": p}}]))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mk_run(td, files):
    """files: {rel: text} —— 在 run_dir 落盘；返回 (run_dir, session_path)。"""
    rd = Path(td) / "run"
    for rel, text in files.items():
        fp = rd / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(text, encoding="utf-8")
    sp = rd / "pi-agent" / "sessions" / "s.jsonl"
    sp.parent.mkdir(parents=True, exist_ok=True)
    return str(rd), str(sp)


# ---------- 1) ALT-1b 原样：字符串判据 + 会话写出 207 字 doc ----------
with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {"blocks/sum_calculation_doc.md": "x" * 207,
                          "product/sum.txt": "5050\n"})
    _write_session(sp, [("blocks/sum_calculation_doc.md", "x" * 207)])
    node = {"id": "root.c2.2", "role": "执行",
            "judge": "文档字数不少于 1500 字，完整包含两种计算方法描述、交叉复核结果、"
                     "产物路径信息，且数字与 product/sum.txt 内容一致",
            "验收标准": "文档不少于 1500 字", "context": "写汇总文档到 blocks/sum_calculation_doc.md"}
    obj = {"reply": "已完成文档撰写。文档已保存到 blocks/sum_calculation_doc.md。",
           "未决": [], "doc": None}
    ok, why, info = leafrun.verify_disk_doc(node, obj, rd, str(ROOT), sp)
    check("ALT-1b 原样: 207 字 < 1500 ⇒ 失败", ok is False and "207" in why, (ok, why))
    check("ALT-1b 原样: 抽得 chars_min=1500", info["chars_min"] == 1500, info)
    check("ALT-1b 原样: 落盘文件被量到", info["written"].get("blocks/sum_calculation_doc.md") == 207, info)

# ---------- 2) Q02 原样：1499 字差 1 字 ----------
with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {"doc/root.c2.1.md": "y" * 1499})
    _write_session(sp, [("doc/root.c2.1.md", "y" * 1499)])
    node = {"id": "root.c2.1", "role": "执行",
            "judge": {"文档要求": "doc 内容不少于 1500 字，需包含计算方法、交叉复核结果、产物路径说明"},
            "验收标准": "doc 不少于 1500 字", "context": "写 doc 到 doc/root.c2.1.md"}
    obj = {"reply": "任务已完成。", "未决": [], "doc": None}
    ok, why, info = leafrun.verify_disk_doc(node, obj, rd, str(ROOT), sp)
    check("Q02 原样: 1499 字 < 1500 ⇒ 失败", ok is False and "1499" in why, (ok, why))

# ---------- 3) 模型谎称：inline doc 2000 字、但点名文件只有 207 字 ----------
with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {"blocks/sum_calculation_doc.md": "x" * 207})
    _write_session(sp, [("blocks/sum_calculation_doc.md", "x" * 207)])
    node = {"id": "root.c2.2", "role": "执行",
            "judge": {"chars_min": 1500},
            "验收标准": "汇总文档 blocks/sum_calculation_doc.md 不少于 1500 字",
            "context": "写汇总文档到 blocks/sum_calculation_doc.md"}
    obj = {"reply": "完成 1500 字说明，见 blocks/sum_calculation_doc.md。",
           "未决": [], "doc": "z" * 2000}
    ok, why, info = leafrun.verify_disk_doc(node, obj, rd, str(ROOT), sp)
    check("谎称: 点名文件 207 字 ⇒ 失败（不认 inline 自报）",
          ok is False and "blocks/sum_calculation_doc.md" in why, (ok, why))

# ---------- 4) 达标：落盘 1600 字 ----------
with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {"blocks/sum_calculation_doc.md": "x" * 1600})
    _write_session(sp, [("blocks/sum_calculation_doc.md", "x" * 1600)])
    node = {"id": "root.c2.2", "role": "执行",
            "judge": "文档字数不少于 1500 字", "验收标准": "文档不少于 1500 字",
            "context": "写汇总文档"}
    obj = {"reply": "完成。", "未决": [], "doc": None}
    ok, why, info = leafrun.verify_disk_doc(node, obj, rd, str(ROOT), sp)
    check("达标: 落盘 1600 字 ⇒ 通过", ok is True and "1600" in why, (ok, why))

# ---------- 5) 无字数要求 ⇒ 直通；有要求无候选文件 ⇒ 失败 ----------
with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {})
    _write_session(sp, [])
    node = {"id": "root.c2.1", "role": "执行", "judge": "算出素数个数写入 product/answer.txt",
            "验收标准": "答案正确", "context": "算素数"}
    obj = {"reply": "完成。", "未决": [], "doc": None}
    ok, _why, info = leafrun.verify_disk_doc(node, obj, rd, str(ROOT), sp)
    check("无字数要求: 直通", ok is True and info["chars_min"] is None, info)
    node2 = {"id": "root.c2.2", "role": "执行", "judge": "文档字数不少于 1500 字",
             "验收标准": "文档不少于 1500 字", "context": "写文档"}
    ok2, why2, _info2 = leafrun.verify_disk_doc(node2, obj, rd, str(ROOT), sp)
    check("有要求无文件: fail-closed", ok2 is False and "未定位" in why2, (ok2, why2))

# ---------- 6) 审计 role 字符串判据不抽字数 ----------
with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {"blocks/sum_calculation_doc.md": "x" * 207})
    _write_session(sp, [("blocks/sum_calculation_doc.md", "x" * 207)])
    node = {"id": "root.c2.2.audit", "role": "审计",
            "judge": "被审文档不少于 1500 字", "验收标准": "复核文档长度", "context": "审计"}
    obj = {"reply": "审计完成。", "未决": [], "doc": None}
    ok, _why, info = leafrun.verify_disk_doc(node, obj, rd, str(ROOT), sp)
    check("审计 role: 不抽字数，直通", ok is True and info["chars_min"] is None, info)

# ---------- 7) dict judge + 点名文件缺失 ----------
with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {})
    _write_session(sp, [])
    node = {"id": "root.c2.1", "role": "执行", "judge": {"chars_min": 1500},
            "验收标准": "doc/root.c2.1.md 不少于 1500 字", "context": "写 doc/root.c2.1.md"}
    obj = {"reply": "完成。", "未决": [], "doc": None}
    ok, why, info = leafrun.verify_disk_doc(node, obj, rd, str(ROOT), sp)
    check("点名文件缺失: 失败", ok is False and "缺失" in why, (ok, why))
    check("点名文件缺失: named 记缺失", info["named"].get("doc/root.c2.1.md") is None, info)

# ---------- 8) session_doc_paths：只认 assistant write 调用 ----------
with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {})
    lines = [_sess_line("user", [{"type": "text", "text": "ignore me blocks/fake.md"}]),
             _sess_line("assistant", [{"type": "toolCall", "id": "t1", "name": "bash",
                                       "arguments": {"command": "cat blocks/other.md"}}]),
             _sess_line("assistant", [{"type": "toolCall", "id": "t2", "name": "write",
                                       "arguments": {"content": "x", "path": "doc/a.md"}}]),
             _sess_line("assistant", [{"type": "toolCall", "id": "t3", "name": "write",
                                       "arguments": {"content": "1", "path": "product/answer.txt"}}]),
             _sess_line("assistant", [{"type": "text", "text": "doc/b.md"}])]
    Path(sp).write_text("\n".join(lines) + "\n", encoding="utf-8")
    got = leafrun.session_doc_paths(sp)
    check("session 写路径: 只取 write 工具的 .md/.txt",
          got == ["doc/a.md", "product/answer.txt"], got)

# ---------- 9) 第三轮 P2⑥：named 排除 product/ 下的值文件 ----------
# q01c 实测：c2.1/c4.1 的 disk_doc_check named 含 product/answer.txt: 4；
# 真正 doc 是 blocks/*.md（1420/706 字，确实未达标——失败本身是对的，归因错）。
with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {"blocks/primes_doc.md": "x" * 1420,
                          "product/answer.txt": "1234"})
    _write_session(sp, [("blocks/primes_doc.md", "x" * 1420),
                        ("product/answer.txt", "1234")])
    node = {"id": "root.c2.1", "role": "执行",
            "judge": "文档字数不少于 1500 字，数字与 product/answer.txt 内容一致",
            "验收标准": "文档不少于 1500 字", "context": "写汇总文档到 blocks/primes_doc.md"}
    obj = {"reply": "完成。", "未决": [], "doc": None}
    ok, why, info = leafrun.verify_disk_doc(node, obj, rd, str(ROOT), sp)
    check("P2-6: named 排除 product/answer.txt",
          "product/answer.txt" not in info["named"], info["named"])
    check("P2-6: named 仍认 blocks/*.md",
          info["named"].get("blocks/primes_doc.md") == 1420, info["named"])
    check("P2-6: 失败本身仍是对的（1420 < 1500）",
          ok is False and "1420" in why, (ok, why))
    check("P2-6: 失败信息点名真正 doc，不提值文件",
          "blocks/primes_doc.md" in why and "product/answer.txt" not in why, why)
    check("P2-6: written 池不动（值文件仍在池里）",
          info["written"].get("product/answer.txt") == 4, info["written"])

with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {"doc/notes.txt": "x" * 1600, "blocks/x.md": "y" * 1600})
    _write_session(sp, [("doc/notes.txt", "x" * 1600), ("blocks/x.md", "y" * 1600)])
    node = {"id": "root.c2.2", "role": "执行",
            "judge": "文档字数不少于 1500 字",
            "验收标准": "doc/notes.txt 与 blocks/x.md 均不少于 1500 字",
            "context": "写 doc/notes.txt 与 blocks/x.md"}
    obj = {"reply": "完成。", "未决": [], "doc": None}
    ok, why, info = leafrun.verify_disk_doc(node, obj, rd, str(ROOT), sp)
    check("P2-6: named 认 doc/*.txt 与 blocks/*.md",
          info["named"].get("doc/notes.txt") == 1600
          and info["named"].get("blocks/x.md") == 1600, info["named"])
    check("P2-6: 两 doc 均达标 ⇒ 通过", ok is True, (ok, why))

with tempfile.TemporaryDirectory() as td:
    rd, sp = _mk_run(td, {"product/answer.txt": "1234"})
    _write_session(sp, [("product/answer.txt", "1234")])
    node = {"id": "root.c4.1", "role": "执行",
            "judge": "文档字数不少于 1500 字，数字与 product/answer.txt 内容一致",
            "验收标准": "文档不少于 1500 字", "context": "算答案写 product/answer.txt"}
    obj = {"reply": "完成。", "未决": [], "doc": None}
    ok, why, info = leafrun.verify_disk_doc(node, obj, rd, str(ROOT), sp)
    check("P2-6: 仅 product 文件时 named 为空",
          info["named"] == {}, info["named"])
    check("P2-6: 走 pool 判定，失败点名池里最长 doc",
          ok is False and "product/answer.txt" in why and "4" in why, (ok, why))

check("P2-6: _is_doc_path 单元口径",
      leafrun._is_doc_path("product/answer.txt") is False
      and leafrun._is_doc_path("product/x.md") is False
      and leafrun._is_doc_path("blocks/x.md") is True
      and leafrun._is_doc_path("doc/notes.txt") is True
      and leafrun._is_doc_path("doc/a.md") is True
      and leafrun._is_doc_path("answer.txt") is False,
      "product/x.md 亦排除；裸 answer.txt 不认")

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: %s" % FAIL)
    sys.exit(1)
