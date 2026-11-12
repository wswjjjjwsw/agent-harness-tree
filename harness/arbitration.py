"""95 v0 薄本地规则仲裁器 + 上升闸 + 外部顾问接线（V2.1 P3）。

设计来源：~/mini/rag-corpus/claude/本地LLM体系设计/80-逻辑树实施/95-仲裁器与
上升闸-实现设计.md（doc 95）。96（v1：reject/expand、卡片自包含校验）是后续单，
本模块不实现。一句话：把 routine 决策挡在本地，把
「价值观/红线/不可逆/新先例」上升给外部顾问（DeepSeek）。

路由（doc 95 §4 求值顺序，逐字照搬）：
  结构缺料 → defer；硬上升 → escalate；规则命中 → decide；
  new_precedent 或（无规则命中 且 无唯一安全选项）→ escalate；
  唯一安全选项 → decide；兜底 → escalate（默认安全）。

普通工具/协议故障不得无脑送云端：恢复表能处理的本地故障由规则
（routine-tool-fault）直接 decide，绝不上升。

外部顾问只作外部顾问，不切成树内推理后端（树后端明确拒绝 DeepSeek，
harness/agentrun.py:41-44）：
  - 技术方向困境 → DeepSeek + 纯方向指导 role（原子化独立调用，不加载 leo 人格）
  - 目标/偏好/高代价/授权取舍 → 已有 leo-profile DeepSeek judge（leo_runtime.py）
  - 接线走 leo_runtime.dispatch（transport 可注入；测试用假 transport）。
  - tree_arbitration.dispatch 只用于已停树的最终裁决（要求全树终态），
    不用于飞行中叶失败的升级——飞行中升级走 leo_runtime.dispatch。
    本模块提供 TreeArbitrationTransport 供已停树路径复用，不在飞行中调用它。

红线（本任务无“可扣费 DeepSeek 真调用”的明确许可）：
  - 默认 live=False：escalate 只记台账 + 返回本地兜底，绝不发真实调用。
  - live=True 须显式开启且必须传入 transport（真实调用路径）；未传入
    transport 时 live=True 也只返回本地兜底，不会悄悄发调用。
  - 云端不可用/输出无效/预算耗尽 ⇒ 本地恢复或保底，绝不堵住输出（不抛异常）。
  - 顾问建议不能越过 auth；涉及 auth 的建议一律 requires_user_approval=True，
    未经用户批准不得应用到 auth 字段。本模块从不自己应用任何建议
    （apply_advisor_result 只校验，applied 恒为 False）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

LEDGER_NAME = "arbitration-ledger.jsonl"

# doc 95 §4 默认红线词 + 树侧 auth 词（权限/授权/auth 碰到即上升，见任务要求）。
RED_LINE_WORDS = (
    "账本一致性", "主控项", "职责分离", "数据删除", "对外发布", "付款",
    "权限", "授权", "auth",
)
CONFIDENCE_THRESHOLD = 0.8   # doc 95：confidence < 0.8 ⇒ 上升
SCORE_GAP = 0.1              # doc 95：前两名 option 评分差 < 0.1（打平）⇒ 上升
CARD_MAX_CHARS = 1200        # doc 95：上升卡 ≤1200 字
DEFAULT_MAX_ESCALATIONS = 3  # 每 run 最多上升次数（预算门；超了走本地兜底）

# 普通工具/协议故障（恢复表能处理）——本地 decide，绝不上升。
ROUTINE_FAILURE_CLASSES = (
    "env_error", "timeout", "loop_breaker", "zero_tool_research",
    "bad_citation", "phantom_citation", "schema_missing_field",
    "merge_missing_field", "role_invalid", "no_criteria", "repeated_output",
)

DEFAULT_RULES = [
    {"id": "routine-tool-fault",
     "when": {"failure_class": list(ROUTINE_FAILURE_CLASSES)},
     "choose": "A",
     "why": "普通工具/协议故障按恢复表换策略，不上升"},
    {"id": "within-acceptance",
     "when": {"question_contains": ["验收"]},
     "choose": "按 acceptance.md 机械判定",
     "why": "验收有客观标准，不需要人"},
    {"id": "reversible-min-change",
     "when": {"principles_in_play": ["最小构建"]},
     "choose": "最小改动且可逆的那个",
     "why": "规则库已有优先级"},
]


def _opt_label(opt) -> str:
    return str((opt or {}).get("label") or "")


def _rule_matches(rule: dict, decision: dict) -> bool:
    """when 全满足才命中；每个 when 键内任一元素命中即算该键满足。"""
    when = (rule or {}).get("when") or {}
    if not when:
        return True
    for key, wanted in when.items():
        wanted = [str(x) for x in (wanted or [])]
        if key == "question_contains":
            q = str(decision.get("question") or "")
            if not any(w in q for w in wanted):
                return False
        elif key == "principles_in_play":
            have = [str(x) for x in (decision.get("principles_in_play") or [])]
            if not any(w in have for w in wanted):
                return False
        elif key == "failure_class":
            if str(decision.get("failure_class") or "") not in wanted:
                return False
        else:
            return False
    return True


def _safe_options(decision: dict) -> list:
    """唯一安全选项口径（doc 95）：可逆 且 cost != high。
    红线已在硬上升步排掉，到这一步时 decision 无红线。"""
    out = []
    for opt in (decision.get("options") or []):
        if bool((opt or {}).get("reversible")) and str((opt or {}).get("cost") or "") != "high":
            out.append(opt)
    return out


def _hard_escalate_reason(decision: dict) -> str:
    """硬上升（doc 95 §4 的 1/2/4/5/6；#3 new_precedent 不在这一步，
    按原文排在规则命中之后）。命中返回原因，否则返回 ""。"""
    red = list(decision.get("red_lines") or [])
    principles = [str(x) for x in (decision.get("principles_in_play") or [])]
    if red or any(w in p for p in principles for w in RED_LINE_WORDS):
        return "红线命中：%s" % ("、".join(red) if red else "原则命中红线词")
    if bool(decision.get("irreversible")):
        return "不可逆"
    for opt in (decision.get("options") or []):
        if str((opt or {}).get("cost") or "") == "high":
            return "高代价选项：%s" % _opt_label(opt)
    if bool(decision.get("role_conflict")):
        return "角色冲突"
    if bool(decision.get("leo_reserved")):
        return "Leo 保留裁决"
    conf = decision.get("confidence")
    conf = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.0
    if conf < CONFIDENCE_THRESHOLD:
        return "置信度 %.2f < %.2f" % (conf, CONFIDENCE_THRESHOLD)
    scored = sorted(
        (float(o["score"]) for o in (decision.get("options") or [])
         if isinstance((o or {}).get("score"), (int, float))),
        reverse=True)
    if len(scored) >= 2 and (scored[0] - scored[1]) < SCORE_GAP:
        return "前两名评分打平（差 %.3f < %.2f）" % (scored[0] - scored[1], SCORE_GAP)
    return ""


def gate(decision: dict, rules=None) -> dict:
    """95/96 薄本地规则门。@returns: doc 95 §5 输出契约
    {id, action, option, reason, rule_hits, confidence, card, missing}。
    action ∈ decide / defer / escalate。纯规则，不调任何模型。"""
    d = dict(decision or {})
    did = str(d.get("id") or "")
    conf = d.get("confidence")
    conf = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.0
    options = list(d.get("options") or [])
    out = {"id": did, "action": "", "option": "", "reason": "",
           "rule_hits": [], "confidence": conf, "card": "", "missing": []}
    # 1) 结构缺料 → defer（doc 95：options < 2，或任一 option 缺 pros/cons，
    #    或 question/context 为空）。
    missing = []
    if len(options) < 2:
        missing.append("options 少于 2 项")
    for opt in options:
        if not isinstance(opt, dict) or "pros" not in opt or "cons" not in opt:
            missing.append("option %s 缺 pros/cons" % (_opt_label(opt) or "?"))
            break
    if not str(d.get("question") or "").strip():
        missing.append("question 为空")
    if not str(d.get("context") or "").strip():
        missing.append("context 为空")
    if missing:
        out.update(action="defer", reason="信息不足，先补材料", missing=missing)
        return out
    # 2) 硬上升 1/2/4/5/6 → escalate。
    why = _hard_escalate_reason(d)
    if why:
        out.update(action="escalate", reason="硬上升：" + why)
        out["card"] = render_card_text(build_card(d))
        return out
    # 3) 规则命中 → decide。
    for rule in (rules if rules is not None else DEFAULT_RULES):
        if _rule_matches(rule, d):
            choose = str(rule.get("choose") or "")
            labels = [_opt_label(o) for o in options]
            option = choose if choose in labels else ""
            if not option:
                safe = _safe_options(d)
                option = _opt_label(safe[0]) if len(safe) == 1 else ""
            out.update(action="decide", option=option,
                       reason="规则 %s：%s" % (rule.get("id"), rule.get("why") or choose),
                       rule_hits=[str(rule.get("id") or "")])
            return out
    # 4) new_precedent，或（无规则命中 且 无唯一安全选项）→ escalate。
    #    到这一步已无规则命中（第 3 步已处理），故等价于 new_precedent 或无唯一安全选项。
    safe = _safe_options(d)
    if bool(d.get("new_precedent")) or len(safe) != 1:
        out.update(action="escalate",
                   reason="新先例" if bool(d.get("new_precedent")) else "无唯一安全选项")
        out["card"] = render_card_text(build_card(d))
        return out
    # 5) 唯一安全选项 → decide。
    out.update(action="decide", option=_opt_label(safe[0]),
               reason="唯一安全选项：无红线、可逆、cost != high")
    return out


# ---------------------------------------------------------------- 仲裁卡
def build_card(decision: dict, advisor: str = None) -> dict:
    """仲裁卡（任务要求字段）：问题/已尝试恢复/受影响子树/证据/已有成果/建议选项。
    外加 advisor、auth 标记、未上升时的默认兜底、内容哈希（幂等键）。"""
    d = dict(decision or {})
    adv = advisor or advisor_for(d)
    card = {
        "id": str(d.get("id") or ""),
        "question": str(d.get("question") or ""),
        "problem": (str(d.get("question") or "") + "\n"
                    + str(d.get("context") or "")).strip(),
        "attempted_recovery": list(d.get("attempted_recovery") or []),
        "affected_subtree": [str(x) for x in (d.get("affected_subtree") or [])],
        "evidence": list(d.get("evidence") or []),
        "existing_results": list(d.get("existing_results") or []),
        "options": [dict(o) for o in (d.get("options") or [])],
        "principles_in_play": list(d.get("principles_in_play") or []),
        "advisor": adv,
        "auth_affected": bool(d.get("auth_affected")),
        "default_if_not_escalated": str(d.get("default_if_not_escalated") or "交付 partial + unresolved"),
        "confidence": float(d.get("confidence") or 0.0)
        if isinstance(d.get("confidence"), (int, float)) else 0.0,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    raw = json.dumps({k: card[k] for k in sorted(card) if k != "ts"},
                     ensure_ascii=False, sort_keys=True)
    card["content_hash"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return card


def render_card_text(card: dict) -> str:
    """上升卡文本（doc 95 §5 格式，≤1200 字）。"""
    lines = ["【上升】%s" % str(card.get("question") or ""), "选项："]
    for opt in (card.get("options") or []):
        lines.append("  %s pros=%s / cons=%s（可逆=%s，代价=%s）" % (
            _opt_label(opt), "；".join(opt.get("pros") or []),
            "；".join(opt.get("cons") or []),
            "是" if opt.get("reversible") else "否",
            str(opt.get("cost") or "")))
    lines.append("碰的原则：%s" % ("、".join(card.get("principles_in_play") or []) or "(无)"))
    lines.append("已尝试恢复：%s" % ("；".join(card.get("attempted_recovery") or []) or "(无)"))
    lines.append("受影响子树：%s" % ("、".join(card.get("affected_subtree") or []) or "(无)"))
    lines.append("已有成果：%s" % ("；".join(card.get("existing_results") or []) or "(无)"))
    lines.append("不上升的默认：%s" % str(card.get("default_if_not_escalated") or ""))
    lines.append("证据：%s" % ("；".join(str(x) for x in (card.get("evidence") or [])) or "(无)"))
    if card.get("auth_affected"):
        lines.append("注意：涉及 auth/授权，建议不得越过 auth，重大授权须用户批准。")
    text = "\n".join(lines)
    if len(text) > CARD_MAX_CHARS:
        text = text[:CARD_MAX_CHARS - 3] + "..."
    return text


def advisor_for(decision: dict) -> str:
    """顾问分流（任务要求）：目标/偏好/高代价/授权取舍 → judge（leo-profile）；
    其余技术方向困境 → direction（DeepSeek + 纯方向指导 role，不加载 leo 人格）。"""
    d = decision or {}
    if bool(d.get("auth_affected")) or bool(d.get("leo_reserved")):
        return "judge"
    for opt in (d.get("options") or []):
        if str((opt or {}).get("cost") or "") == "high":
            return "judge"
    return "direction"


def decision_from_failure(failure_class: str, cause: str, parent_id: str = "",
                          node_id: str = "", attempted_recovery=None,
                          affected_subtree=None, evidence=None,
                          existing_results=None, auth_affected: bool = False,
                          new_precedent: bool = False, confidence: float = None,
                          leo_reserved: bool = False) -> dict:
    """从一次叶失败建仲裁决策。options 恒 ≥2（A 本地恢复 / B 上升外部顾问 /
    C 交付 partial），保证 gate 不因“缺料”而 defer。
    confidence 缺省：普通机械类 0.9（分类确定，可本地 decide）；前提证伪/
    审计发现/未知类 0.6（低于 0.8 阈值，硬上升）。"""
    fc = str(failure_class or "unknown")
    if confidence is None:
        confidence = 0.6 if fc in ("unknown", "audit_finding", "premise_falsified") else 0.9
    return {
        "id": "arb-%s-%s" % (fc, str(node_id or "root")),
        "question": "叶 %s 失败（%s）：%s" % (node_id or "?", fc, (cause or "")[:200]),
        "context": "父 %s；失败类 %s；原因：%s" % (parent_id or "?", fc, cause or ""),
        "options": [
            {"label": "A", "pros": ["不依赖云端", "已有恢复机制"],
             "cons": ["可能再次失败"], "cost": "low", "reversible": True,
             "evidence": ["恢复表：%s" % fc]},
            {"label": "B", "pros": ["外部方向指导"], "cons": ["需云端", "有延迟"],
             "cost": "medium", "reversible": True, "evidence": []},
            {"label": "C", "pros": ["立刻有结果"], "cons": ["留未决"],
             "cost": "low", "reversible": True, "evidence": []},
        ],
        "principles_in_play": [],
        "red_lines": ["权限"] if auth_affected else [],
        "irreversible": False,
        "new_precedent": bool(new_precedent),
        "role_conflict": False,
        "leo_reserved": bool(leo_reserved),
        "confidence": float(confidence),
        "failure_class": fc,
        "attempted_recovery": list(attempted_recovery or []),
        "affected_subtree": list(affected_subtree or [node_id, parent_id]),
        "evidence": list(evidence or []),
        "existing_results": list(existing_results or []),
        "auth_affected": bool(auth_affected),
        "default_if_not_escalated": "交付 partial + unresolved",
    }


# ---------------------------------------------------------------- 顾问校验
def apply_advisor_result(card: dict, raw) -> dict:
    """校验顾问原始输出。@returns: {ok, suggestion, requires_user_approval,
    reason, applied}。本模块从不自己应用任何建议（applied 恒 False）；
    涉及 auth 的建议一律 requires_user_approval=True。
    接受的形状：{"option": "<label>", "note": "...", "confidence": 0..1}
    或 leo_runtime judge 形状 {"verdict": {"note": ..., "verdict": ...}}。"""
    card = card or {}
    labels = [_opt_label(o) for o in (card.get("options") or [])]
    if not isinstance(raw, dict):
        return {"ok": False, "suggestion": None,
                "requires_user_approval": bool(card.get("auth_affected")),
                "reason": "顾问输出无效：非 JSON 对象", "applied": False}
    opt = str(raw.get("option") or "")
    note = ""
    verdict = raw.get("verdict")
    if isinstance(verdict, dict):
        note = str(verdict.get("note") or "")
        opt = opt or str(verdict.get("option") or "")
    note = note or str(raw.get("note") or raw.get("suggestion") or "")
    if opt and opt not in labels:
        return {"ok": False, "suggestion": None,
                "requires_user_approval": bool(card.get("auth_affected")),
                "reason": "顾问输出无效：option %r 不在卡片选项里" % opt, "applied": False}
    if not opt and not note:
        return {"ok": False, "suggestion": None,
                "requires_user_approval": bool(card.get("auth_affected")),
                "reason": "顾问输出无效：无可用建议", "applied": False}
    return {"ok": True,
            "suggestion": {"option": opt, "note": note,
                           "confidence": raw.get("confidence")},
            "requires_user_approval": bool(card.get("auth_affected")),
            "reason": "顾问建议已校验（仅作建议，未应用）", "applied": False}


# ---------------------------------------------------------------- 台账
def _ledger_path(run_dir: str) -> str:
    return str(Path(run_dir) / LEDGER_NAME) if run_dir else ""


def append_ledger(run_dir: str, entry: dict) -> dict:
    """仲裁台账一笔（gate 裁决 + 上升尝试/结果），供幂等与审计回放。"""
    e = dict(entry or {})
    e["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p = _ledger_path(run_dir)
    if p:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e


def read_ledger(run_dir: str) -> list:
    """读回台账（缺文件 ⇒ []）。"""
    p = _ledger_path(run_dir)
    if not p or not Path(p).is_file():
        return []
    out = []
    for line in Path(p).read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------- 顾问传输
def _find_runtime_dir() -> str:
    """定位 leo-profile-runtime（含 leo_runtime.py / tree_arbitration.py）（V2.2 自愈）。

    P5/探针真 bug：原代码用 `Path(__file__).parent.parent.parent / "leo-profile-runtime"`，
    只在生产树布局（~/agent-harness/harness/）下正确；候选 worktree 多一层
    （~/agent-harness/runs/.candidate-*/harness/）时算出不存在的 ~/agent-harness/runs/leo-profile-runtime，
    import 直接 ModuleNotFoundError，live 顾问永远 consult 不上。
    本函数按优先级试：① 环境变量 LEO_PROFILE_RUNTIME；② 从 __file__ 向上逐层找
    （最多 6 层）含 leo_runtime.py 的 leo-profile-runtime；③ ~/agent-harness/leo-profile-runtime；
    ④ 原固定算法（生产布局兜底）。返回首个存在的目录；全不存在时返回 ③（调用方 import
    报错即为响亮失败，不静默）。
    """
    _env = str(os.environ.get("LEO_PROFILE_RUNTIME") or "").strip()
    if _env and Path(_env, "leo_runtime.py").is_file():
        return _env
    _here = Path(__file__).resolve()
    for _p in [_here] + list(_here.parents)[:6]:
        _cand = _p / "leo-profile-runtime"
        if (_cand / "leo_runtime.py").is_file():
            return str(_cand)
    _home = str(Path.home() / "agent-harness" / "leo-profile-runtime")
    if Path(_home, "leo_runtime.py").is_file():
        return _home
    return str(_here.parent.parent.parent / "leo-profile-runtime")


class LeoRuntimeTransport:
    """leo_runtime.dispatch 的薄包装（真实调用路径）。
    仅当调用方显式 live=True 且传入本 transport 时才发调用；本模块其余
    路径一律不构造它。direction 用纯方向指导 role（不加载 leo 人格）；
    judge 用已有 leo-profile role。role_dir/db/frontend 由调用方传入。"""

    def __init__(self, role_dir: str, db: str, frontend: str,
                 direction_role_dir: str = None, require_manifest: bool = False):
        self.role_dir = str(role_dir or "")
        self.db = str(db or "")
        self.frontend = str(frontend or "")
        self.direction_role_dir = str(direction_role_dir or self.role_dir)
        self.require_manifest = bool(require_manifest)

    def __call__(self, card: dict, advisor: str) -> dict:
        import sys
        runtime_dir = _find_runtime_dir()
        # leo_runtime 在树外（~/agent-harness/leo-profile-runtime/）；懒加载，import
        # lib.arbitration 本身绝不碰它。
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        import leo_runtime
        # direction 目前复用 judge 模式：leo_runtime.prepare() 只接受
        # ('judge', 'health', 'opportunity')，没有 "direction" 模式；
        # direction role dir 是首次启用 live 上升前才交付的独立材料。
        mode = "judge"
        role_dir = self.role_dir if advisor == "judge" else self.direction_role_dir
        request = {"mode": mode, "task": str(card.get("id") or ""),
                   "packet": {"title": str(card.get("question") or ""),
                              "original_requirement": str(card.get("problem") or ""),
                              "card": card}}
        return leo_runtime.dispatch(request, role_dir, self.db, self.frontend,
                                    require_manifest=self.require_manifest)


class TreeArbitrationTransport:
    """tree_arbitration.dispatch 的薄包装，仅用于已停树的最终裁决。
    飞行中叶失败不得用它（它要求全树终态，否则直接抛错）。"""

    def __call__(self, card: dict, advisor: str) -> dict:
        import sys
        runtime_dir = _find_runtime_dir()
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        import tree_arbitration
        return tree_arbitration.dispatch(
            {"task_id": str(card.get("id") or ""), "receipt": None})


# ---------------------------------------------------------------- 仲裁器
class Arbitrator:
    """薄本地规则门 + 上升接线（每个 run 一个实例，台账落 run_dir）。"""

    def __init__(self, run_dir: str = "", rules=None, max_escalations: int = DEFAULT_MAX_ESCALATIONS):
        self.run_dir = str(run_dir or "")
        self.rules = list(rules) if rules is not None else list(DEFAULT_RULES)
        self.max_escalations = int(max_escalations)
        self.entries = read_ledger(self.run_dir)

    # ---- 台账 ----
    def _record(self, entry: dict) -> dict:
        e = append_ledger(self.run_dir, entry)
        self.entries.append(e)
        return e

    def escalations_sent(self) -> int:
        return sum(1 for e in self.entries if e.get("kind") == "escalate_sent")

    def already_sent(self, content_hash: str) -> bool:
        return any(e.get("kind") == "escalate_sent"
                   and e.get("content_hash") == content_hash for e in self.entries)

    def sent_outcome(self, content_hash: str):
        for e in reversed(self.entries):
            if e.get("kind") == "escalate_sent" and e.get("content_hash") == content_hash:
                return e.get("outcome")
        return None

    # ---- 门 ----
    def gate(self, decision: dict) -> dict:
        verdict = gate(decision, rules=self.rules)
        self._record({"kind": "gate", "decision_id": verdict.get("id"),
                      "action": verdict.get("action"), "option": verdict.get("option"),
                      "reason": verdict.get("reason"),
                      "rule_hits": verdict.get("rule_hits"),
                      "confidence": verdict.get("confidence")})
        return verdict

    # ---- 上升 ----
    def escalate(self, card: dict, advisor: str = None, transport=None,
                 live: bool = False) -> dict:
        """上升外部顾问。默认 live=False：不发任何真实调用，只记台账 + 本地兜底。
        live=True 时必须传入 transport（否则同样只记台账 + 本地兜底）。
        云端不可用/输出无效/预算耗尽 ⇒ 本地兜底，绝不抛异常、绝不堵住输出。"""
        card = dict(card or {})
        adv = advisor or str(card.get("advisor") or "direction")
        base = {"status": "not_sent", "live": bool(live), "advisor": adv,
                "card_id": str(card.get("id") or ""),
                "content_hash": str(card.get("content_hash") or ""),
                "fallback": "local",
                "requires_user_approval": bool(card.get("auth_affected"))}
        if not live or transport is None:
            base["reason"] = "live 未开启或未传 transport：仅记台账，不发真实调用"
            self._record({"kind": "escalate_not_sent", "card_id": base["card_id"],
                          "content_hash": base["content_hash"], "advisor": adv,
                          "reason": base["reason"]})
            return base
        prior = self.sent_outcome(base["content_hash"])
        if prior is not None:
            out = dict(prior)
            out["status"] = "replayed"
            self._record({"kind": "escalate_replayed", "card_id": base["card_id"],
                          "content_hash": base["content_hash"], "advisor": adv})
            return out
        if self.escalations_sent() >= self.max_escalations:
            base["reason"] = "预算耗尽：本 run 上升已达 %d 次上限" % self.max_escalations
            self._record({"kind": "escalate_budget", "card_id": base["card_id"],
                          "content_hash": base["content_hash"], "advisor": adv,
                          "reason": base["reason"]})
            return base
        try:
            raw = transport(card, adv)
        except Exception as exc:  # noqa: BLE001 - 云端不可用 ⇒ 本地兜底，不抛
            base["reason"] = "顾问不可用（%s: %s），本地兜底" % (type(exc).__name__, exc)
            self._record({"kind": "escalate_failed", "card_id": base["card_id"],
                          "content_hash": base["content_hash"], "advisor": adv,
                          "reason": base["reason"]})
            return base
        checked = apply_advisor_result(card, raw)
        if not checked.get("ok"):
            base["reason"] = "顾问输出无效（%s），本地兜底" % checked.get("reason")
            self._record({"kind": "escalate_invalid", "card_id": base["card_id"],
                          "content_hash": base["content_hash"], "advisor": adv,
                          "reason": base["reason"]})
            return base
        out = {"status": "sent", "live": True, "advisor": adv,
               "card_id": base["card_id"], "content_hash": base["content_hash"],
               "suggestion": checked.get("suggestion"),
               "requires_user_approval": checked.get("requires_user_approval"),
               "applied": False, "fallback": None,
               "reason": "顾问建议已校验（仅作建议，未应用）"}
        self._record({"kind": "escalate_sent", "card_id": base["card_id"],
                      "content_hash": base["content_hash"], "advisor": adv,
                      "outcome": out})
        return out
