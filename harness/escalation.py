"""上升原因分类与路由（v1，2026-11-13）。

旧实现按「节点状态」上升：叶失败、终审 blocker、作废上限各自一条通道，
最后都汇到修复叶 → 复核叶 → Leo。问题是：

  · 事实冲突（report 186 vs oracle 168）不需要裁决，只需要重算/回读；
  · 工具/会话故障不需要 Leo，只需要换策略；
  · 方法失效（同一失败换两种策略仍失败）应该由父节点重规划，不是问外部模型；
  · 只有「判断/取舍」和「授权/红线」才真正需要一个更高层的决策者。

本模块只做两件事：
  1. classify_*：把 blocker / failure 归到一个显式原因；
  2. route：按原因给出最便宜的处理通道，并落 escalation.jsonl。

它不改任何节点状态，也不调模型。调用方（final_audit / driver / leafrun）
拿到 reason + route 后，再走既有的修复/复核/Leo/失败路径。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# ---- 原因枚举 -------------------------------------------------------------
E_FACT = "E_FACT"          # 事实冲突：权威 vs 派生不一致；重算/回读/改派生
E_EVIDENCE = "E_EVIDENCE"  # 证据缺口：读不到、缺文件、缺约束；研究叶/重规划
E_POLICY = "E_POLICY"      # 判断/取舍：多种合法做法、质量判断；复核 → Leo
E_AUTH = "E_AUTH"          # 授权/红线：删除、发布、付款、权限；直接人工
E_BROKEN = "E_BROKEN"      # 方法失效：换过策略仍失败；父重规划 → 耗尽失败
E_BUDGET = "E_BUDGET"      # 预算耗尽：轮次/节点/token/时间；明确失败

REASONS = (E_FACT, E_EVIDENCE, E_POLICY, E_AUTH, E_BROKEN, E_BUDGET)

# ---- 路由枚举 -------------------------------------------------------------
R_MECHANICAL = "mechanical"  # 机械核对/改写/重算，不调模型
R_REWORK = "rework"          # 有界修复叶（写派生侧）
R_RESEARCH = "research"      # 研究叶/父重规划补证据
R_REVIEW = "review"          # 只读复核叶（同模型、独立上下文）
R_LEO = "leo"                # 外部裁决（持久回执）
R_USER = "user"              # 人工
R_FAIL = "fail"              # 明确失败，保留证据

ROUTES = {
    E_FACT: (R_MECHANICAL, R_REWORK, R_FAIL),
    E_EVIDENCE: (R_RESEARCH, R_FAIL),
    E_POLICY: (R_REVIEW, R_LEO, R_USER),
    E_AUTH: (R_USER,),
    E_BROKEN: (R_RESEARCH, R_FAIL),
    E_BUDGET: (R_FAIL,),
}

# ---- 词表（宁可保守：判不准时走 E_POLICY 的复核，不直接放行） -------------
AUTH_WORDS = (
    "授权", "权限", "许可", "审批", "批准", "删除", "对外发布", "发布",
    "付款", "红线", "不可逆", "越权", "approval", "permission", "auth",
)
BUDGET_WORDS = (
    "预算耗尽", "预算已耗尽", "到数", "上限", "耗尽", "超过预算",
    "max_rounds", "nodes_max", "ctx_budget", "token 上限", "步数上限",
)
STRUCTURAL_WORDS = (
    "未审批次", "批次子数不足", "缺完成态增量审计", "未终结", "缺审计",
    "批次子数",
)
MISSING_WORDS = (
    "不存在", "缺失", "未写", "未生成", "找不到", "没有", "无法读取",
    "读不到", "缺信息", "无法核查", "无法访问",
)
FACT_WORDS = (
    "不一致", "不符", "冲突", "对不上", "错误", "差异", "不等于",
    "mismatch", "inconsistent", "矛盾",
)
QUALITY_WORDS = (
    "质量", "说服力", "不够", "不完整", "不详细", "风格", "表述", "建议",
    "欠佳", "可读性", "详略", "字数", "过于", "冗余", "重复",
)

# 现有 contract.FAILURE_CLASSES → 原因。审计发现类由 blocker 文本再分类。
FAILURE_REASON = {
    "env_error": E_BROKEN,
    "timeout": E_BROKEN,
    "loop": E_BROKEN,
    "loop_breaker": E_BROKEN,
    "schema_missing_field": E_BROKEN,
    "merge_missing_field": E_BROKEN,
    "repeated_output": E_BROKEN,
    "role_invalid": E_EVIDENCE,
    "no_criteria": E_EVIDENCE,
    "unverifiable_criteria": E_POLICY,
    "bad_citation": E_EVIDENCE,
    "phantom_citation": E_EVIDENCE,
    "zero_tool_research": E_EVIDENCE,
    "audit_finding": E_POLICY,
    "unknown": E_POLICY,
}


def _row(reason: str, why: str, **extra) -> dict:
    row = {"reason": reason, "why": str(why or ""), "route": route_for(reason)}
    row.update(extra)
    return row


def _has_number(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


def _cites(text: str) -> list:
    try:
        from harness import evidence as _ev
        return list(_ev.seen_citations(text or ""))
    except Exception:                                            # noqa: BLE001
        return []


def classify_blocker(item: str, run_dir: str = None, context: dict = None) -> dict:
    """把一条终审 blocker / 复核发现归到原因。

    @param item    blocker 原文（含引用路径）
    @param run_dir run 目录；给了才会做权威事实核对
    @param context 可选：{"failure_class": ..., "budget_left": bool, "parent": id}
    @returns {"reason", "route", "why", "cites", "authority", "target",
              "canonical_item", "dedupe_key"}
    """
    text = str(item or "")
    low = text.lower()
    context = dict(context or {})
    cites = _cites(text)

    # 1) 预算耗尽：明确失败，不再派发
    if any(w in text for w in BUDGET_WORDS):
        return _row(E_BUDGET, "命中预算耗尽词", cites=cites,
                    dedupe_key=_dedupe_key(text))

    # 2) 结构性覆盖缺口：先补证据/补审计，不是判断题
    #    ★ 必须先于授权词判：「未审批次」里的「审批」不是授权，是审计覆盖。
    if any(w in text for w in STRUCTURAL_WORDS):
        return _row(E_EVIDENCE, "结构性覆盖缺口", cites=cites,
                    dedupe_key=_dedupe_key(text))

    # 3) 授权/红线：任何模型都不能替用户决定
    if any(w in text for w in AUTH_WORDS) or any(w in low for w in ("approval", "permission")):
        return _row(E_AUTH, "命中授权/红线词", cites=cites,
                    dedupe_key=_dedupe_key(text))

    # 4) 引用/证据缺失：先补证据
    if any(w in text for w in MISSING_WORDS):
        return _row(E_EVIDENCE, "引用/证据缺失", cites=cites,
                    dedupe_key=_dedupe_key(text))

    # 5) 权威事实核对：能改写就改写，不能改写再按文本判
    if run_dir:
        try:
            from harness import authority as _auth
            canon = _auth.canonical_conflict(text, str(run_dir))
        except Exception:                                        # noqa: BLE001
            canon = None
        if canon:
            return _row(E_FACT, "权威与派生不一致，已按磁盘事实改写",
                        cites=cites, authority=canon.get("authority", ""),
                        target=canon.get("target", ""),
                        canonical_item=canon.get("item", text),
                        dedupe_key=canon.get("target") or _dedupe_key(text))

    # 6) 事实冲突：有引用或数字才判 E_FACT；否则可能只是文风意见
    if any(w in text for w in FACT_WORDS) and (cites or _has_number(text)):
        return _row(E_FACT, "文本指出事实不一致", cites=cites,
                    dedupe_key=_dedupe_key(text))

    # 7) 质量/偏好：需要判断，但先复核，不直接 Leo
    if any(w in text for w in QUALITY_WORDS):
        return _row(E_POLICY, "质量/偏好类判断", cites=cites,
                    dedupe_key=_dedupe_key(text))

    # 8) 兜底：判不准的争议按判断题处理（复核 → Leo），绝不默认通过
    return _row(E_POLICY, "无法机械归类，按判断题处理", cites=cites,
                dedupe_key=_dedupe_key(text))


def classify_failure(failure_class: str, cause: str = "", context: dict = None) -> dict:
    """把叶失败类归到原因。"""
    fc = str(failure_class or "unknown")
    reason = FAILURE_REASON.get(fc, E_POLICY)
    context = dict(context or {})
    if fc in ("no_criteria", "role_invalid"):
        why = "父节点给的判据/角色不可执行，需要重规划"
    elif fc in ("bad_citation", "phantom_citation", "zero_tool_research"):
        why = "证据缺失或引用不实，需要换证据路径"
    elif fc in ("env_error", "timeout", "loop", "loop_breaker"):
        why = "环境/循环故障，先换策略重试"
    elif fc in ("schema_missing_field", "merge_missing_field", "repeated_output"):
        why = "契约/重复输出，先机械修复"
    elif fc == "unverifiable_criteria":
        why = "判据无法机械核验，需要独立复核"
    else:
        why = "未分类失败，按判断题处理"
    return _row(reason, why, failure_class=fc, cause=str(cause or ""),
                dedupe_key="fc:" + fc)


def route_for(reason: str, has_target: bool = True, budget_left: bool = True) -> str:
    """原因 → 最便宜的处理通道。"""
    reason = str(reason or E_POLICY)
    if reason == E_FACT:
        return R_REWORK if has_target else R_FAIL
    if reason == E_EVIDENCE:
        return R_RESEARCH if budget_left else R_FAIL
    if reason == E_BROKEN:
        return R_RESEARCH if budget_left else R_FAIL
    if reason == E_POLICY:
        return R_REVIEW
    if reason == E_AUTH:
        return R_USER
    if reason == E_BUDGET:
        return R_FAIL
    return R_REVIEW


def needs_external_judge(reason: str) -> bool:
    """只有判断题才允许走到 Leo；事实/证据/方法/预算都不该找外部裁决。"""
    return str(reason or "") == E_POLICY


def needs_user(reason: str) -> bool:
    return str(reason or "") in (E_AUTH,)


def _dedupe_key(text: str) -> str:
    """同一条 blocker 的稳定去重键：去掉空白/标点，取前 120 字。"""
    compact = re.sub(r"\s+", "", str(text or ""))
    compact = re.sub(r"[，。；：、,.;:!?！？（）()\[\]【】]", "", compact)
    return compact[:120]


def record(run_dir: str, row: dict) -> dict:
    """把一次路由决策写进 escalation.jsonl（append-only）。"""
    path = Path(str(run_dir or "")) / "escalation.jsonl"
    entry = {
        "ts": round(time.time(), 3),
        "reason": str((row or {}).get("reason") or ""),
        "route": str((row or {}).get("route") or ""),
        "node_id": str((row or {}).get("node_id") or ""),
        "blocker_id": str((row or {}).get("blocker_id") or ""),
        "why": str((row or {}).get("why") or "")[:500],
        "authority": str((row or {}).get("authority") or ""),
        "target": str((row or {}).get("target") or ""),
        "budget": (row or {}).get("budget") or {},
        "evidence": (row or {}).get("evidence") or [],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # 账本写不进去不能阻断树；调用方仍能用返回值继续。
        return dict(entry, recorded=False)
    return dict(entry, recorded=True)


def read_ledger(run_dir: str) -> list:
    path = Path(str(run_dir or "")) / "escalation.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows
