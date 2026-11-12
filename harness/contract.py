"""类型化节点契约 + 证据台账 + failure→recovery 表（t-64e27d836932 系统架构主任务）。

背景（本案实证，runs/e2e-20261105-t97/）：
  merge 漏必填字段重试 3 次；fix.1 被证据门误拒——它是基于现有批次结果的
  合成/结构修复，任务 auth 明文禁调工具，门却按中文关键词“调查”要求工具调用。
  根因：节点没有类型化契约，证据门靠扫描中文任务词决定，修复叶与调查叶
  用同一把尺子量。

设计（落到现有 driver/state/rescue，不另造 Harness）：
  · node_kind 存放在节点 overrides["node_kind"]（20 项字段表不动；
    overrides 本就是父写给子的口袋字段；role 仍是内置 role，schema 收紧一照旧）。
  · 五种 kind：research / synthesis / contract_repair / audit / publish。
  · 结果类型 complete / partial / failed：状态机仍是 完成/失败 终态；
    complete/partial 都落 完成，区别记在证据台账（evidence_ledger.jsonl）
    + 未决[] 里；merge 收束时必须覆盖 partial/failed 分支，否则机械拒收。
  · 失败分类 + 恢复策略表：见 FAILURE_CLASSES / RECOVERY_STRATEGIES；
    恢复控制器在 harness/recovery.py。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

NODE_KINDS = ("research", "synthesis", "contract_repair", "audit", "publish")

# role → 默认 kind（显式 overrides["node_kind"] 优先；".fix." 节点恒为 repair）。
KIND_BY_ROLE = {
    "调研": "research",
    "执行": "synthesis",
    "审计": "audit",
}

# 单叶 wall time 上限（秒；None = 走 TREE_PI_TIMEOUT 兜底）。
# research 300 沿用 F3；contract_repair 是批内综合，180 足够（t97 fix.1 实跑 9.5s）。
LEAF_TIMEOUT_BY_KIND = {
    "research": 300.0,
    # 第二轮续修（integ2-q01 实测）：两修复叶都在 180.0s 上限撞停（fix.audit1.1
    # 180.055s 靠抢救完成、fix.audit2.1 180.022s 超时失败）；修复叶要读实况 +
    # 重写长 doc，与 research 同量级工作量，提至 300.0s，与 research 看齐。
    "contract_repair": 300.0,
    "synthesis": None,
    "audit": None,
    "publish": None,
}


def classify(node: dict) -> str | None:
    """节点 kind：显式声明 > .fix. 修复节点 > role 默认 > None（父 act，不适用）。"""
    node = node or {}
    ov = node.get("overrides") or {}
    if isinstance(ov, dict):
        k = str(ov.get("node_kind") or "").strip()
        if k in NODE_KINDS:
            return k
    nid = str(node.get("id") or "")
    if ".fix." in nid:
        return "contract_repair"
    return KIND_BY_ROLE.get(str(node.get("role") or ""))


# 叶超时 profile（t-f6f9d7325d93 干跑脚手架 + 工作模式分档）。
# 决议顺序：dry-run（环境 TREE_DRYRUN=1）> work_mode > kind 默认。
# 回撤范围（C 脚手架）：DRYRUN_LEAF_TIMEOUT / is_dryrun / dryrun 参数分支。
DRYRUN_LEAF_TIMEOUT = 90.0
WORK_MODE_PROFILES = {
    "light": {"research": 90.0, "contract_repair": 90.0, "synthesis": 90.0,
              "audit": 90.0, "publish": 90.0},
    "standard": {"research": 300.0, "contract_repair": 180.0, "synthesis": None,
                 "audit": None, "publish": None},
    "deep": {"research": 600.0, "contract_repair": 300.0, "synthesis": None,
             "audit": None, "publish": None},
}


def is_dryrun() -> bool:
    """干跑脚手架开关（环境变量，默认关闭）。"""
    return os.environ.get("TREE_DRYRUN") == "1"


def leaf_timeout_for(kind: str | None, work_mode: str | None = None,
                     dryrun: bool = False) -> float | None:
    if dryrun:
        return DRYRUN_LEAF_TIMEOUT
    if work_mode in WORK_MODE_PROFILES:
        return WORK_MODE_PROFILES[work_mode].get(kind or "")
    return LEAF_TIMEOUT_BY_KIND.get(kind or "")


# ---------------------------------------------------------------- 失败分类
# judge_why 文本是机械分类的唯一输入（exit_class 五值集不扩）。
FAILURE_CLASSES = (
    "zero_tool_research",   # research 叶零成功工具结果
    "bad_citation",         # 引用不存在/超行/幻影路径
    "phantom_citation",     # 幻影引用：0 成功工具结果 + 引用不存在文件（两者同时出现）
    "schema_missing_field", # 产出缺必填字段（leaf/merge）
    "merge_missing_field",  # merge 缺必填字段（与 schema_missing_field 同源，父侧计数分开）
    "loop_breaker",         # loop-breaker 熔断终止
    "loop",                 # 无进展重复调用熔断（t-dc62df942275 项 5：同一工具+等价参数连续 3 次）
    "timeout",              # 单叶超时（300s 或 TREE_PI_TIMEOUT）
    "env_error",            # 环境侧失败（act_runner 失败、pi rc!=0 等）
    "role_invalid",         # role 不在内置集（spawn/merge 拒收）
    "no_criteria",          # 节点无可执行验收判据（无 judge 或 judge 非 dict/非字符串）
    "unverifiable_criteria",# 判据为自然语言，未机械核验（→ partial，需审计复核）
    "audit_finding",        # 审计发现未回应/未处置
    "repeated_output",      # 与上次被拒 merge 输出逐字相同
    "unknown",
)


def classify_failure(exit_class: str, why: str, node: dict = None) -> str:
    """从 exit_class + judge_why 文本机械分类失败。@returns: failure class。"""
    w = str(why or "")
    # V2：exit_class 是机械事实，优先于 why 文本关键词。旧值先匹配关键词：
    # 超时叶的 why 嵌了整条 TimeoutExpired argv（含 prompt 原文里的“不存在/
    # 引用”），三叶全被误标 bad_citation（routeB-02），超时语义被吞掉。
    if str(exit_class) == "超时":
        return "timeout"
    # t-dc62df942275 项 5：无进展重复调用熔断是机械事实（why 含熔断标记文案），
    # 优先于后文的关键词匹配；exit_class 仍在五值集内（环境），不扩。
    if "无进展重复调用熔断" in w:
        return "loop"
    # V2.1 P2：组合条件优先——0 成功工具结果 + 引用不存在文件 = 幻影引用
    # （单信号仍走各自的类；两者同时出现才是模型“没查却编引用”的定型）。
    if ("零成功工具结果" in w or "零工具调用" in w) and "无法核查的引用" in w:
        return "phantom_citation"
    if "零成功工具结果" in w or "零工具调用" in w:
        return "zero_tool_research"
    if "无法核查的引用" in w or "不存在" in w and "引用" in w:
        return "bad_citation"
    if "loop_breaker 熔断" in w or "operation was aborted" in w.lower():
        return "loop_breaker"
    if "单叶上限" in w or "TimeoutExpired" in w or "超时" in w:
        return "timeout"
    if "无可执行判据" in w:
        return "no_criteria"
    if "判据为自然语言" in w:
        return "unverifiable_criteria"
    if "不是内置 role" in w or "role=" in w and "不认识" in w:
        return "role_invalid"
    if "审计发现未逐条回应" in w or "对审计发现 的形状不合规" in w:
        return "audit_finding"
    if "缺必填键" in w or "必填" in w:
        # merge 侧与 leaf 侧分开计：节点是父（may_spawn）⇒ merge 缺字段。
        try:
            from harness import role as _rm
            if node is not None and bool(_rm.effective(
                    str(node.get("role") or ""), None).get("may_spawn")):
                return "merge_missing_field"
        except Exception:                                     # noqa: BLE001
            pass
        return "schema_missing_field"
    if str(exit_class) == "环境":
        return "env_error"
    return "unknown"


# ---------------------------------------------------------------- 恢复策略表
# 每类失败 → 新策略（kind/role/工具/查询焦点）。同一失败类最多换 2 次策略；
# 耗尽后父节点交付 partial + unresolved，不死循环。
# 关键：新策略的 action fingerprint 必须与旧策略不同（见 recovery.py）。
RECOVERY_STRATEGIES = {
    "zero_tool_research": [
        {"kind": "research", "role": "调研", "tools": "read,bash,grep,find,ls",
         "focus": "换证据路径重查：改用另一组本机只读命令/文件（不得重复同一命令）",
         "max_attempts": 1},
    ],
    "bad_citation": [
        {"kind": "research", "role": "调研", "tools": "read,bash,grep,find,ls",
         "focus": "重查引用：只引用 session 工具结果里实际出现过的路径/行号",
         "max_attempts": 1},
    ],
    # V2.1 P2：幻影引用换任务方案（不是原样重派）。策略 1 = 具体命令叶（不再开放式调研）；
    # 策略 2 = 只用已有 partial 证据合成（不再派新调研）。两次都同错 ⇒ 控制器耗尽，记 uncovered。
    "phantom_citation": [
        {"kind": "research", "role": "调研", "tools": "read,bash",
         "focus": "换任务方案：不再开放式调研；只执行父节点给出的 1-2 条具体只读命令并逐字粘贴工具输出；引用路径必须来自工具输出原文；无工具输出就不写结论，记未决",
         "max_attempts": 1},
        {"kind": "synthesis", "role": "执行", "tools": "read",
         "focus": "不再派新调研：只用已抢救/已落盘的 partial 证据合成该章节结论，缺的记 uncovered；不调现场工具",
         "max_attempts": 1},
    ],
    "schema_missing_field": [
        {"kind": "contract_repair", "role": "调研", "tools": "none",
         "focus": "只消费输入证据 IDs + 目标 schema，补缺失字段的结构化值",
         "max_attempts": 1},
        {"kind": "synthesis", "role": "执行", "tools": "read",
         "focus": "父节点直接用已有证据合成缺失字段（机械兜底，须引用来源）",
         "max_attempts": 1},
    ],
    "merge_missing_field": [
        {"kind": "contract_repair", "role": "调研", "tools": "none",
         "focus": "只消费本批次证据 IDs + 目标 schema，补缺失字段的结构化值",
         "max_attempts": 1},
        {"kind": "synthesis", "role": "调研", "tools": "none",
         "focus": "诊断：综合前两次 merge 输出与修复结论，定位字段丢失环节并给出换策略建议（只消费输入证据，不调现场工具）",
         "max_attempts": 1},
        # role=调研（不是执行）：执行 role 的 may_write=true 会强制要求 doc 字段，
        # 诊断叶只出根因结论、无 doc，形状检查会拒（t-f6f9 回归实测）；调研 role
        # may_write=false，形状通过。已知耦合：role 调研在 build_argv 里会拿到工具，
        # 但 context 明确禁调现场工具、synthesis 证据门也不要求工具。
    ],
    "loop_breaker": [
        {"kind": "research", "role": "调研", "tools": "read,bash,grep,find,ls",
         "focus": "换查询策略：拆小查询、换工具（read 换 grep / bash 换 find），保留 partial",
         "max_attempts": 1},
    ],
    # t-dc62df942275 项 5：无进展重复调用熔断后不原样重派同一工具调用；
    # 只用已落盘产物/事件复核，缺口记 uncovered。同一失败类最多换 2 次策略（表头约定）。
    "loop": [
        {"kind": "research", "role": "调研", "tools": "read",
         "focus": "循环熔断后只复核已落盘产物与工具事件，不原样重派同一工具调用；缺口记 uncovered",
         "max_attempts": 1},
    ],
    "timeout": [
        {"kind": "research", "role": "调研", "tools": "read,bash,grep,find,ls",
         "focus": "缩小目标：只查 partial 里缺的那 1-2 个事实，复用已抢救证据",
         "max_attempts": 1},
    ],
    "env_error": [
        {"kind": "research", "role": "调研", "tools": "read,bash",
         "focus": "诊断环境：查服务/端口/配置是否就绪，再决定降级为未决",
         "max_attempts": 1},
    ],
    "role_invalid": [
        {"kind": "research", "role": "调研", "tools": "read",
         "focus": "角色发现：先查内置 role 表与取值域，再用合法 role 重派",
         "max_attempts": 1},
    ],
    "no_criteria": [
        {"kind": "synthesis", "role": "执行", "tools": "read",
         "focus": "父节点重派：必须带可执行验收标准（chars_min/json_keys 二选一）",
         "max_attempts": 1},
    ],
    "unverifiable_criteria": [
        {"kind": "audit", "role": "审计", "tools": "none",
         "focus": "审计复核该叶产出与证据，确认后方可视为 complete，否则留 uncovered",
         "max_attempts": 1},
    ],
    "audit_finding": [
        {"kind": "contract_repair", "role": "调研", "tools": "none",
         "focus": "按审计发现逐条处置（解掉/上冒/记未决），引用发现 id",
         "max_attempts": 1},
    ],
    "repeated_output": [
        {"kind": "research", "role": "调研", "tools": "read,bash,grep,find,ls",
         "focus": "诊断：查前两次 merge 输出逐字相同的原因，换去向/要点/分拆",
         "max_attempts": 1},
    ],
}


def strategies_for(failure_class: str) -> list:
    return [dict(s) for s in RECOVERY_STRATEGIES.get(failure_class or "", [])] or [
        {"kind": "research", "role": "调研", "tools": "read,bash,grep,find,ls",
         "focus": "未知失败：先诊断再定策略", "max_attempts": 1}]


def action_fingerprint(strategy: dict) -> str:
    """策略指纹：kind + role + tools + focus 的稳定哈希（前 12 位）。
    父节点恢复行为机械化：新策略指纹必须与旧的不同，否则不算恢复。"""
    raw = json.dumps({k: strategy.get(k) for k in ("kind", "role", "tools", "focus")},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def failure_recovery_table() -> list:
    """failure→recovery 表（报告 § 与 run 工件共用；每行：失败类 / 症状 / 新策略 / 上限 / 耗尽后）。"""
    rows = []
    for fc in FAILURE_CLASSES:
        if fc == "unknown":
            continue
        sts = strategies_for(fc)
        rows.append({
            "failure_class": fc,
            "strategies": ["%s/%s：%s" % (s["kind"], s["role"], s["focus"]) for s in sts],
            "max_distinct_strategies": len(sts),
            "on_exhausted": "父节点交付 partial + unresolved（不死循环）",
        })
    return rows


# ---------------------------------------------------------------- 证据台账
LEDGER_NAME = "evidence_ledger.jsonl"


def append_ledger(run_dir: str, entry: dict) -> dict:
    """叶子结果记一笔台账：node_id / kind / result_type / tools / session /
    citations / unresolved。@returns: 记下的 entry（含 ts）。"""
    e = dict(entry or {})
    e["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p = Path(run_dir) / LEDGER_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e


def read_ledger(run_dir: str) -> dict:
    """@returns: {node_id: 最新一条 entry}；文件不在 ⇒ {}（如实，不编）。"""
    p = Path(run_dir) / LEDGER_NAME
    out = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("node_id"):
            out[str(e["node_id"])] = e
    return out


def ledger_entry(node: dict, result_type: str, tools: dict, session_id: str,
                 citations: list, unresolved: list, session_path: str = "") -> dict:
    return {
        "node_id": str((node or {}).get("id") or ""),
        "kind": classify(node),
        "role": str((node or {}).get("role") or ""),
        "result_type": result_type,  # complete / partial / failed
        "state": str((node or {}).get("state") or ""),
        "tools": {"calls": int((tools or {}).get("calls") or 0),
                  "successes": int((tools or {}).get("successes") or 0),
                  "failures": int((tools or {}).get("failures") or 0)},
        "session_id": str(session_id or ""),
        "session_path": str(session_path or ""),
        "citations": list(citations or []),
        "unresolved": list(unresolved or []),
    }
