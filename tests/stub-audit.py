#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree-04 桩 act 脚本：`stub-audit.py <act> <node_id> <round>`，prompt 走 stdin，产出正文走 stdout。

★ 由 $TREE_ACT_CMD 指到这里 ⇒ 一个 pi 进程都不许起（tests/run-audit.sh 会采样自证）。
★ act 的两种取值（task-def §2.5）：
    "leaf"  ⇒ 检查点（抽检 / 全量终审 / 形状审查）；kind 由 prompt 里的**代码拼的格式要求**判定；
    "merge" ⇒ 父的收束轮；另有 "plan"/"spawn"（父的另两个 act）也要能答，否则驱动跑不动。
★ 行为**全由环境变量控制**（一条用例一个变量组合，互不干扰）：
    TREE_AUDIT_MODE      ∈ 正常 | 抽检失败 | 抽检无发现 | 抽检格式不符 | BLOCKER | 终审失败 |
                            形状合理 | 形状不合理收束 | 形状失败 | 形状越权 | 无回复 |
                            审计回应形状不符
    TREE_AUDIT_TRACE     <path> 每被调一次追加一行 JSON（含 prompt 原文与产出对象）
    TREE_AUDIT_CHILDREN  父 spawn 轮拉几个子（缺省 3）
    TREE_AUDIT_MERGE     父 merge 轮的「去向」（缺省 收束）
★ 桩里**不许**出现任何真 pi 调用。
"""
from __future__ import annotations

import json
import os
import re
import sys

# 代码拼的格式要求里的锚串（用来判 kind；这三串只出现在 tree-04 的 prompt_extra 里）
MARK_FINAL = "这是【全量终审】"
MARK_SHAPE = "这是形状审查"
MARK_SAMPLE = "否则该条不会被认作发现"
LIST_MARK = '{"清单":'


def kind_of(act: str, prompt: str) -> str:
    if act == "merge":
        return "父merge"
    if act == "spawn":
        return "父spawn"
    if act == "plan":
        return "父plan"
    if MARK_FINAL in prompt:
        return "终审"
    if MARK_SHAPE in prompt:
        return "形状审查"
    if MARK_SAMPLE in prompt:
        return "抽检"
    return "leaf?"


def find_ids(prompt: str) -> list:
    """从 prompt 里抓发现/检查点的 id 串（`[<检查点id>#<n>]`），去重保序。"""
    out = []
    for m in re.findall(r"\[[^\[\]\"]+#\d+\]", prompt or ""):
        if m not in out:
            out.append(m)
    return out


def find_advice(prompt: str) -> str:
    """从「待回应清单」那一行读回 建议（父逐条回应时会把它带上）。"""
    for line in (prompt or "").splitlines():
        line = line.strip()
        if not line.startswith(LIST_MARK):
            continue
        try:
            obj = json.loads(line)
        except Exception:                                    # noqa: BLE001
            continue
        for row in (obj.get("清单") or []):
            adv = str((row or {}).get("建议") or "")
            if adv:
                return adv
    return ""


def children(n: int) -> list:
    out = []
    for i in range(1, n + 1):
        out.append({"role": "执行", "context": "本批第 %d 块的落点" % i,
                    "judge": {"chars_min": 120}, "验收标准": "第 %d 块写完且非空" % i,
                    "auth": "doc/s%d.md" % i})
    return out


def merge_obj(go: str, nid: str, prompt: str, mode: str) -> dict:
    rnd = 1
    obj = {"去向": go, "reply": "本轮收拢结论：%s" % go, "未决": []}
    if go == "再派":
        obj["下一批要点"] = ["再派一批的第 1 块"]
    elif go == "切分":
        obj["下一批要点"] = ["切分这一批"]
        obj["切分目标"] = ["%s.c%d.1" % (nid, max(1, rnd))]
    else:
        obj["uncovered"] = [{"what": "本层剩下的一条", "why": "本轮收束"}]
    ids = find_ids(prompt)
    if ids:
        adv = find_advice(prompt)
        rows = []
        for i in ids:
            if mode == "无回复":
                # 故意**不写** id 串：考驱动的「必须逐条回应」校验（schema 先放行，驱动再拒）
                rows.append({"item": "已回应（本条故意没写 id 串）", "处置": "解掉", "说明": "x"})
            elif mode == "审计回应形状不符":
                # ★ G1 考据用：id 串**写了**（所以「id 串逐条出现」能过），但形状不合规
                #   （处置 是枚举外的值）⇒ 只有本批补的形状自检能拦住它（schema 换键后不管这段）
                rows.append({"item": "%s 形状故意写坏" % i, "处置": "彻底瞎写", "说明": "d"})
            else:
                row = {"item": "%s 已回应：解掉" % i, "处置": "解掉", "说明": "按发现逐条处理"}
                if adv:
                    row["建议"] = adv
                rows.append(row)
        obj["对审计发现"] = rows
    return obj


def main() -> int:                                          # noqa: C901
    act = sys.argv[1] if len(sys.argv) > 1 else ""
    nid = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        rnd = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    except ValueError:
        rnd = 0
    try:
        prompt = sys.stdin.read()
    except Exception:                                        # noqa: BLE001
        prompt = ""

    mode = os.environ.get("TREE_AUDIT_MODE") or "正常"
    kind = kind_of(act, prompt)
    out = None
    raw = ""

    if act == "plan":
        out = {"要点": ["这一层要几块：第 1 块 / 第 2 块"]}
    elif act == "spawn":
        n = int(os.environ.get("TREE_AUDIT_CHILDREN") or 3)
        out = {"children": children(n)}
    elif act == "merge":
        go = os.environ.get("TREE_AUDIT_MERGE") or "收束"
        out = merge_obj(go, nid, prompt, mode)
    elif kind == "抽检":
        if mode in ("抽检失败", "失败", "审计失败"):
            raw = "桩：抽检环节这不是 JSON。"
        elif mode == "抽检无发现":
            out = {"reply": "抽检：本批三条产出形状齐备，未发现可机械复核的问题。", "未决": []}
        elif mode == "抽检格式不符":
            out = {"reply": "抽检：有条目但首词不合法。",
                   "未决": [{"type": "冲突", "what": "这条首词不是那两个大写词", "why": "考格式判定"}]}
        else:
            out = {"reply": "抽检：本批产出里有一处数字对不上。",
                   "未决": [{"type": "冲突", "what": "NOTE 兄弟产出声称的数字在本批次会计视图里核不到",
                             "why": "该批第 1 块的 reply 与 result 的计数不一致"}]}
    elif kind == "终审":
        if mode == "终审失败":
            raw = "桩：终审环节这不是 JSON。"
        elif mode == "BLOCKER":
            # 第三轮：blocker 取 integ2 实测的 doc 长度形态（点名文件 + 要求字数），
            # 使终审修复派发能走到可执行规格（P1⑤）；fixture 里无该文件时目标解不出，仍走明确失败。
            out = {"reply": "全量终审：汇总文档仅 207 字，远低于要求的 1500 字，未消除。",
                   "未决": [{"type": "冲突", "what": "BLOCKER 汇总文档 blocks/sum_calculation_doc.md 仅 207 字，远低于要求的 1500 字",
                             "why": "doc 太短"}]}
        else:
            out = {"reply": "全量终审：全量条目已逐条核过，未发现 blocker。", "未决": []}
    elif kind == "形状审查":
        if mode == "形状失败":
            raw = "桩：形状审查环节这不是 JSON。"
        elif mode == "形状不合理收束":
            out = {"判断": "不合理", "理由": "这一层的宽度已超出建议值且没有切分理由。",
                   "建议": "收束"}
        elif mode == "形状越权":
            out = {"判断": "合理", "理由": "超限有理由（同一份材料的几段）。", "建议": "继续派",
                   "越权键": "这是不该出现的键"}
        else:
            out = {"判断": "合理", "理由": "超限有理由（同一份材料的几段，不能再切）。",
                   "建议": "继续派"}
    else:
        out = {"reply": "桩：未知 act %s" % act, "未决": []}

    if raw or out is None:
        body = raw or "这不是 JSON，桩胡说了。"
    else:
        body = json.dumps(out, ensure_ascii=False)

    trace = os.environ.get("TREE_AUDIT_TRACE")
    if trace:
        with open(trace, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"act": act, "node_id": nid, "round": rnd, "kind": kind,
                                 "mode": mode, "prompt": prompt, "out": out, "raw": body},
                                ensure_ascii=False) + "\n")
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
