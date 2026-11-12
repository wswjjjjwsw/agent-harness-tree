"""四种输出契约（按 act 分型）+ 越权判定（76 §3.3，task-def §3.5）。

四种契约（76 §3.3，**键名逐字照抄，不自创字段**）：
  merge —— 决策 JSON，不写子定义：去向 / reply / 未决 + 条件必填（下一批要点 / 切分目标 /
           uncovered / 对审计发现）
  spawn —— 只写子定义，不做决策：children（每条必须带 role）
  plan  —— 只出现在根/首轮：要点
  leaf  —— 叶子的产出（没有 children）：reply / 未决（必填），doc 视 may_write 而定
           + doc（may_write=true 时）

★ 未决[] 一律必填（merge 与 leaf 都卡）：它是「够了就收」这个天然倾向的唯一机械对冲。
★ validate 只管**形状**（键在不在 / 枚举值合不合法 / children[i].role 是不是内置 role）；
  语义归父与审计，本批不实现（task-def §3.5 末条）。
★ 本模块**不含任何 role 名字的字面判断**：children[i].role 的合法集从 role.ROLE_NAMES 读。
"""
from __future__ import annotations

from harness import gate, role as role_mod

# 「去向」的取值域（76 §3.3 ①）
TOWARDS = ("再派", "切分", "收束")
# 未决[] 的 type 取值域（76 §3.3 ①）
UNDECIDED_TYPES = ("缺信息", "冲突", "超范围", "判不了", "超时")  # V2：超时是真实的未决态，V1 merge 因报“超时”被形状拒收
# 对审计发现 的「处置」取值域（76 §3.3 ★收紧四）
DISPOSALS = ("解掉", "上冒", "记未决")
# 对审计发现 的「建议」取值域（76 §3.3 ★收紧五，只有形状审查会填）
ADVICES = ("继续派", "切分", "收束")

# 契约元数据（给 assemble_prompt 拼「必填键清单」用，也是本模块的 SSOT）
# ---- 产出形状（★ 单点真值：prompt 里不再各抄一遍） --------------------------
#   为什么放这儿：形状是**契约的一部分**，与 CONTRACTS 的必填键同源；
#   放进 prompt ⇒ 4 个 role 各抄一份（实测就是这么漂的：契约与 prompt 互相打脸）。
#   只写"人看的形状"，机器校验仍只认 CONTRACTS 的必填/条件必填/禁止。
SHAPES = {
    "plan": '{"要点": ["彼此独立的分支及其产出"], "共同约束": ["已确认的共同口径；没有则 []"]}',
    "spawn": '{"children": [{"role": "执行", '
             '"context": "给子节点的完整材料正文（直接写在这里，不许引用文件路径）", '
             '"验收标准": "产出覆盖 context 里的全部要点，每条给出可复核依据", '
             '"auth": "doc/child-1.md"}]}',
    "merge": '{"去向": "再派|切分|收束", "reply": "20~200 字", '
             '"未决": [{"type": "…", "what": "…", "why": "…"}], '
             '"下一批要点": ["…"], "切分目标": ["node_id"], '
             '"uncovered": [{"what": "…", "why": "…"}], '
             '"对审计发现": [{"item": "…", "处置": "…", "说明": "…", "建议": "…"}]}',
    "leaf": '{"reply": "20~200 字", "未决": [{"type": "…", "what": "…", "why": "…"}]'
            '[, "doc": "你写进去的那个路径"]}',
}

CONTRACTS: dict = {
    "merge": {
        "act": "merge",
        "必填": ["去向", "reply", "未决"],
        "条件必填": {"下一批要点": "去向=再派/切分", "切分目标": "去向=切分",
                     "uncovered": "去向=收束", "对审计发现": "本轮有审计发现时"},
        "禁止": ["children"],
    },
    "spawn": {
        "act": "spawn",
        "必填": ["children"],
        "条件必填": {"children[i].context": "每块",
                     "children[i].验收标准": "每块", "children[i].auth": "每块",
                     "children[i].overrides": "可选", "children[i].role": "每块（收紧一）"},
        "禁止": ["去向", "未决", "uncovered", "reply"],
    },
    "plan": {
        "act": "plan",
        "必填": ["要点"],
        "条件必填": {},
        "禁止": ["children"],
    },
    "leaf": {
        "act": "leaf",
        "必填": ["reply", "未决"],
        "条件必填": {"doc": "may_write=true 时"},
        "禁止": ["children", "去向"],
    },
}
# 别名：四种契约按 act 分型，act 名与契约名一一对应（leaf 对应执行/调研）
CONTRACT_BY_ACT = {"merge": "merge", "spawn": "spawn", "plan": "plan",
                   "leaf": "leaf", None: "leaf"}


def merge_response_format(audit_required: bool = False) -> dict:
    """本地 LLAAT merge 请求的 JSON Schema；条件与 validate 的 merge 分支一致。"""
    audit_item = {
        "type": "object",
        "properties": {
            "item": {"type": "string", "minLength": 1},
            "处置": {"type": "string", "enum": list(DISPOSALS)},
            "说明": {"type": "string", "minLength": 1},
            "建议": {"type": "string", "enum": list(ADVICES)},
        },
        "required": ["item", "处置", "说明"],
    }
    audit_replies = {"type": "array", "items": audit_item}
    if audit_required:
        audit_replies["minItems"] = 1

    response_schema = {
        "type": "object",
        "properties": {
            "去向": {"type": "string", "enum": list(TOWARDS)},
            "reply": {"type": "string", "minLength": 1},
            "未决": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(UNDECIDED_TYPES)},
                        "what": {"type": "string", "minLength": 1},
                        "why": {"type": "string", "minLength": 1},
                    },
                    "required": ["type", "what", "why"],
                },
            },
            "下一批要点": {
                "type": "array", "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "切分目标": {
                "type": "array", "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "uncovered": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "what": {"type": "string", "minLength": 1},
                        "why": {"type": "string", "minLength": 1},
                    },
                    "required": ["what", "why"],
                },
            },
            AUDIT_KEY: audit_replies,
        },
        "required": ["去向", "reply", "未决"] + ([AUDIT_KEY] if audit_required else []),
        "anyOf": [
            {"properties": {"去向": {"const": "再派"}},
             "required": ["去向", "下一批要点"]},
            {"properties": {"去向": {"const": "切分"}},
             "required": ["去向", "下一批要点", "切分目标"]},
            {"properties": {"去向": {"const": "收束"}},
             "required": ["去向", "uncovered"]},
        ],
    }
    return {"type": "json_schema", "json_schema": {
        "name": "harness_merge", "schema": response_schema}}


def spawn_response_format() -> dict:
    """本地 LLAAT spawn 结构约束：写入型子节点必须给可执行 judge。"""
    child_required = ["role", "context", "验收标准", "auth"]  # 2026-11-12：judge 不再必填
    child_properties = {
        # 2026-11-13：child context 是任务说明，不是文件转储。超长会把
        # spawn 输出顶到 max_tokens 后截断，整轮 JSON 不可解析（B-03 实测）。
        "context": {"type": "string", "minLength": 1, "maxLength": 4000},
        "验收标准": {"type": "string", "minLength": 1},
        "auth": {"type": "string", "minLength": 1},
    }
    branches = []
    write_roles, other_roles = [], []
    for name in role_mod.ROLE_NAMES:
        if role_mod.effective(name).get("may_write"):
            write_roles.append(name)
            # 2026-11-12：可选结构判据，字数键与 anyOf 硬约束全部取消。
            judge = {
                "type": "object",
                "description": "可选的机械结构判据；字数类判据已取消。",
                "properties": {
                    "json_keys": {
                        "type": "array", "minItems": 1,
                        "items": {
                            "type": "string",
                            "enum": list(CONTRACTS["leaf"]["必填"]) + ["doc"],
                        },
                    },
                },
                "additionalProperties": False,
            }
            branches.append({
                "type": "object",
                "properties": {
                    **child_properties,
                    "role": {"type": "string", "const": name},
                    "judge": judge,
                },
                "required": child_required,
            })
        else:
            other_roles.append(name)
    if other_roles:
        branches.append({
            "type": "object",
            "properties": {
                **child_properties,
                "role": {"type": "string", "enum": other_roles},
            },
            "required": child_required,
        })
    response_schema = {
        "type": "object",
        "properties": {"children": {
            "type": "array", "minItems": 1,
            "items": {"anyOf": branches},
        }},
        "required": ["children"],
    }
    return {"type": "json_schema", "json_schema": {
        "name": "harness_spawn", "schema": response_schema}}

def managed_doc_response_format(chars_min: int = 0) -> dict:
    """Bound repair leaf: the full replacement document must be inline."""
    # 2026-11-12：字数门禁取消，只要求 doc 非空；参数保留以兼容旧调用点。
    floor = 1
    return {"type": "json_schema", "json_schema": {
        "name": "harness_managed_doc", "schema": {
            "type": "object",
            "properties": {
                "reply": {"type": "string", "minLength": 1},
                "未决": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": list(UNDECIDED_TYPES)},
                            "what": {"type": "string", "minLength": 1},
                            "why": {"type": "string", "minLength": 1},
                        },
                        "required": ["type", "what", "why"],
                    },
                },
                "doc": {"type": "string", "minLength": floor},
            },
            "required": ["reply", "未决", "doc"],
            "additionalProperties": False,
        },
    }}


def structured_review_response_format(blocker_id: str, evidence_ids: list[str]) -> dict:
    """本地 LLAAT 只读 Sage 复核约束：绑定原 blocker 与本次允许的证据来源。"""
    response_schema = {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "minLength": 1},
            "未决": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(UNDECIDED_TYPES)},
                        "what": {"type": "string", "minLength": 1},
                        "why": {"type": "string", "minLength": 1},
                    },
                    "required": ["type", "what", "why"],
                },
            },
            "verdict": {"type": "string", "enum": ["pass", "rework", "escalate"]},
            "covers": {
                "type": "array", "minItems": 1, "maxItems": 1,
                "items": {"type": "string", "const": str(blocker_id)},
            },
            "evidence": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "enum": list(evidence_ids)},
                        "observation": {"type": "string", "minLength": 1},
                    },
                    "required": ["source", "observation"],
                },
            },
            "note": {"type": "string"},
        },
        "required": ["reply", "未决", "verdict", "covers", "evidence", "note"],
    }
    return {"type": "json_schema", "json_schema": {
        "name": "harness_structured_review", "schema": response_schema}}


# 审计发现的两个触发位（「本轮有审计发现时」怎么被机械判定，见报告 §假设）
AUDIT_KEY = "对审计发现"


def _act_key(act: str) -> str:
    if act not in CONTRACT_BY_ACT:
        raise KeyError("act 不认识：%r（只有 %s / leaf）" % (act, list(role_mod.ACTS)))
    return CONTRACT_BY_ACT[act]


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_undecided(obj: dict, errs: list) -> None:
    """未决[] 一律必填 + 每条形状 {type, what, why}（76 §3.3）。"""
    if "未决" not in obj:
        errs.append("缺必填键 '未决'（[] 也要显式给：它是「够了就收」的唯一机械对冲）")
        return
    lst = obj["未决"]
    if not isinstance(lst, list):
        errs.append("'未决' 必须是数组，实际 %r" % type(lst).__name__)
        return
    for i, item in enumerate(lst):
        if not isinstance(item, dict):
            errs.append("'未决[%d]' 必须是对象 {type,what,why}，实际 %r"
                        % (i, type(item).__name__))
            continue
        for k in ("type", "what", "why"):
            if not str(item.get(k) or "").strip():
                errs.append("'未决[%d]' 缺 %r" % (i, k))
        t = item.get("type")
        if t is not None and t not in UNDECIDED_TYPES:
            errs.append("'未决[%d].type'=%r 不在 %s" % (i, t, list(UNDECIDED_TYPES)))


def normalize_undecided(items, default="缺信息"):
    """把未决条目归一化成 validate 可接受的 [{type, what, why}]（V2.2 自愈）。

    只用于机械生成的未决（机械 merge 兜底 / 修复补全），绝不改模型产出的语义：
    - type：已是枚举成员 ⇒ 原样保留；含枚举成员作子串（如 "BLOCKER 缺信息"）
      ⇒ 取该成员；缺失或非法 ⇒ default（缺信息）。
    - what/why：缺失或空白 ⇒ 填占位（不编造事实，只保形状）。
    @returns: 新列表（不改原对象）；非 dict/str 的条目直接丢弃。
    """
    out = []
    for it in (items or []):
        if isinstance(it, dict):
            t = str(it.get("type") or "").strip()
            if t not in UNDECIDED_TYPES:
                _hit = [m for m in UNDECIDED_TYPES if m and m in t]
                t = _hit[0] if _hit else default
            what = str(it.get("what") or "").strip() or "（未注明）"
            why = str(it.get("why") or "").strip() or "叶未决"
            out.append({"type": t, "what": what, "why": why})
        elif isinstance(it, str) and it.strip():
            out.append({"type": default, "what": it.strip(), "why": "叶未决"})
    return out


# 未决 type 的模型侧变体（"未决" 本身常被模型当成 type 值回填，见 t-b0424b0f1592
# 实测：merge 轮 "未决[2].type"="未决" 被形状拒收，整轮作废）。
# 与 normalize_undecided 的区别：那是机械生成未决的归一（缺省"缺信息"）；
# 这里是模型产出未决的归一——"未决/待定/未知"这类"判不了"的同义写法归入"判不了"
# （而不是"缺信息"：模型没说缺什么，只说没判出来），其余非法值同样归入"判不了"，
# 并返回改动说明供调用方记行；what/why 原样保留，绝不编造事实。
def normalize_readonly_output(obj, knobs):
    """may_write=false 的叶误带 doc/edits ⇒ 丢弃并返回说明。

    这不是放松判据：审计/调研 role 本来没有写权限，模型多写的字段不该让
    整条正确结论作废；字段被丢弃，结论仍然要过同一 schema。
    """
    if not isinstance(obj, dict) or bool((knobs or {}).get("may_write")):
        return obj, ""
    removed = []
    for key in ("doc", "edits"):
        if key in obj:
            obj.pop(key, None)
            removed.append(key)
    if not removed:
        return obj, ""
    return obj, "may_write=false ⇒ 已丢弃越权字段 %s" % "、".join(removed)


def normalize_model_undecided(items):
    """把模型产出的未决[] 归一成 validate 可接受的 [{type, what, why}]。

    @returns: (新列表, 改动说明或 "")。未改动时返回原列表与 ""。
    规则（确定性，不读语义）：
      - type 已是枚举成员 ⇒ 原样保留；
      - type 含枚举成员作子串 ⇒ 取该成员；
      - type 是"判不了"的同义写法（未决/待定/未知/…）或其它非法值 ⇒ "判不了"；
      - 缺 type/what/why ⇒ 补占位（不编造事实，只保形状），与 normalize_undecided 同口径；
      - 非 dict 条目：字符串 ⇒ {判不了, 原文, "叶未决"}；其他类型 ⇒ 丢弃。
    """
    if not isinstance(items, list):
        return items, ""
    out, changed = [], []
    for i, it in enumerate(items):
        if isinstance(it, dict):
            t = str(it.get("type") or "").strip()
            if t not in UNDECIDED_TYPES:
                hit = [m for m in UNDECIDED_TYPES if m and m in t]
                if hit:
                    new_t = hit[0]
                else:
                    new_t = "判不了"
                changed.append("未决[%d].type=%r→%r" % (i, t, new_t))
                t = new_t
            what = str(it.get("what") or "").strip()
            why = str(it.get("why") or "").strip()
            if not what:
                what = "（未注明）"
                changed.append("未决[%d].what 为空→占位" % i)
            if not why:
                why = "叶未决"
                changed.append("未决[%d].why 为空→占位" % i)
            out.append({"type": t, "what": what, "why": why})
        elif isinstance(it, str) and it.strip():
            out.append({"type": "判不了", "what": it.strip(), "why": "叶未决"})
            changed.append("未决[%d] 字符串→{判不了}" % i)
        else:
            changed.append("未决[%d] 非对象条目已丢弃" % i)
    return (out, "；".join(changed)) if changed else (items, "")


# merge 输出形状的模型侧变体（Run D 实测，t-132692485362 集成验收）：
# merge 轮 "uncovered 必须是 [{what,why}] 列表" 与
# "'对审计发现[0].处置'='已消费全部输入证据 IDs，…'" 不在 ['解掉','上冒','记未决'] 枚举里，
# 整轮作废。与 normalize_model_undecided 同一模式：只改机械层（归一/占位/显式失败），
# 不改模型行为、不编造事实。
_UNCOVERED_NONE_STRINGS = ("无", "无缺口", "没有", "none", "[]", "无未覆盖", "暂无")


def normalize_model_uncovered(items):
    """把模型产出的 uncovered 归一成 validate 可接受的 [{what, why}]。

    @returns: (新值, 改动说明或 "")。新值 None ⇒ 无法归一，调用方须显式失败（不静默丢弃）。
    规则（确定性，不读语义）：
      - 已是 [{what,why}] ⇒ 原样保留；
      - dict 条目缺 what/why ⇒ 补占位（"（未注明）"/"叶未决"），与 normalize_undecided 同口径；
      - 字符串条目 ⇒ {what: 原文, why: "叶未决"}；"无/无缺口/…"类否定串 ⇒ 不计入（全是否定串 ⇒ []）；
      - 单个 dict（非列表）⇒ 包成单条列表；单个字符串同上；
      - 非 dict/非字符串条目 ⇒ 丢弃并记一笔；其它非列表类型（数字/None 等）⇒ (None, 原因)。
    """
    if isinstance(items, dict):
        items, _wrapped = [items], True
    elif isinstance(items, str):
        s = items.strip()
        if not s:
            return None, "uncovered 为空字符串，无法归一"
        if s.lower() in _UNCOVERED_NONE_STRINGS:
            return [], "uncovered=%r→[]" % s
        return [{"what": s, "why": "叶未决"}], "uncovered 字符串→单条 [{what,why}]"
    elif not isinstance(items, list):
        return None, "uncovered 类型 %s 无法归一（须为 [{what,why}] 列表）" % type(items).__name__
    out, changed = [], []
    for i, it in enumerate(items):
        if isinstance(it, dict):
            what = str(it.get("what") or "").strip()
            why = str(it.get("why") or "").strip()
            if not what:
                what = "（未注明）"
                changed.append("uncovered[%d].what 为空→占位" % i)
            if not why:
                why = "叶未决"
                changed.append("uncovered[%d].why 为空→占位" % i)
            out.append({"type": it.get("type"), "what": what, "why": why} if it.get("type") else
                       {"what": what, "why": why})
        elif isinstance(it, str) and it.strip():
            if it.strip().lower() in _UNCOVERED_NONE_STRINGS:
                changed.append("uncovered[%d] 否定串 %r→不计入" % (i, it.strip()))
                continue
            out.append({"what": it.strip(), "why": "叶未决"})
            changed.append("uncovered[%d] 字符串→{what,why}" % i)
        else:
            changed.append("uncovered[%d] 非对象条目已丢弃" % i)
    return (out, "；".join(changed)) if changed else (items, "")


# 对审计发现 的模型侧同义写法（第四轮，integ3-q01d 实测）：
# 模型把"再派"同时写进 处置 与 建议（去向也是"再派"，见该轮 merge session）——
# 它是"继续派"的同义词，不是新枚举。处置='再派' 的意图是"由再派修复叶解掉"，
# 故归入"解掉"（处置三者中唯一表示"由父处理掉"的值；上冒=交上级、记未决=先挂起）。
# 否定式（"不再派/无需再派/…"）不映射——那是否定，不是同义。
_DISPOSAL_SYNONYMS = (("再派", "解掉"), ("再派发", "解掉"), ("重派", "解掉"),
                      ("重新派", "解掉"), ("重新派发", "解掉"))
_ADVICE_SYNONYMS = (("再派", "继续派"), ("再派发", "继续派"), ("重派", "继续派"),
                    ("重新派", "继续派"), ("重新派发", "继续派"),
                    ("再次派发", "继续派"), ("继续派发", "继续派"))
_SYNONYM_NEGATIONS = ("不再派", "不重派", "无需再派", "不用再派", "不必再派",
                      "无需重派", "不用重派", "不必重派")


def _map_audit_synonym(value: str, synonyms) -> str:
    """同义映射：精确命中或作子串命中 ⇒ 目标值；否定式不映射 ⇒ ""（调用方显式失败）。"""
    v = str(value or "").strip()
    if not v:
        return ""
    if any(n in v for n in _SYNONYM_NEGATIONS):
        return ""
    for syn, target in synonyms:
        if v == syn or syn in v:
            return target
    return ""


def normalize_model_audit_replies(items):
    """把模型产出的 对审计发现[] 归一成 validate 可接受的 [{item,处置,说明(,建议)}]。

    @returns: (新列表, 改动说明或 "")；新列表 None ⇒ 无法归一，调用方须显式失败。
    规则（确定性，不读语义）：
      - 非列表 ⇒ (None, 原因)；
      - 非 dict 条目 ⇒ (None, 原因)——审计发现的回应不许静默丢弃；
      - item/说明 缺失或空白 ⇒ 补占位 "（未注明）"；
      - 处置：已是枚举成员 ⇒ 原样保留；含枚举成员作子串 ⇒ 取该成员；
        无映射 ⇒ (None, "…无映射，显式失败")；
      - 建议（可选）：缺失 ⇒ 不补；已是枚举 ⇒ 保留；含枚举成员作子串 ⇒ 取该成员；
        自由文本无映射 ⇒ 保留到说明和归一日志后省略，不猜成可执行动作。
    """
    if not isinstance(items, list):
        return None, "对审计发现 非列表（%s），无法归一" % type(items).__name__
    out, changed = [], []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            return None, "对审计发现[%d] 非对象（%s），无法归一" % (i, type(it).__name__)
        item = str(it.get("item") or "").strip()
        expl = str(it.get("说明") or "").strip()
        if not item:
            item = "（未注明）"
            changed.append("对审计发现[%d].item 为空→占位" % i)
        if not expl:
            expl = "（未注明）"
            changed.append("对审计发现[%d].说明 为空→占位" % i)
        d = str(it.get("处置") or "").strip()
        if d not in DISPOSALS:
            hit = [m for m in DISPOSALS if m and m in d]
            if hit:
                changed.append("对审计发现[%d].处置=%r→%r" % (i, d, hit[0]))
                d = hit[0]
            else:
                _syn = _map_audit_synonym(d, _DISPOSAL_SYNONYMS)
                if _syn:
                    changed.append("对审计发现[%d].处置=%r→%r（同义：再派即派修复叶解掉）" % (i, d, _syn))
                    d = _syn
                else:
                    return None, "对审计发现[%d].处置=%r 无枚举映射，显式失败" % (i, d)
        new = {"item": item, "处置": d, "说明": expl}
        adv = it.get("建议")
        if adv is not None:
            adv = str(adv).strip()
            if adv not in ADVICES:
                hit = [m for m in ADVICES if m and m in adv]
                if hit:
                    changed.append("对审计发现[%d].建议=%r→%r" % (i, adv, hit[0]))
                    adv = hit[0]
                else:
                    _syn = _map_audit_synonym(adv, _ADVICE_SYNONYMS)
                    if _syn:
                        changed.append("对审计发现[%d].建议=%r→%r（同义）" % (i, adv, _syn))
                        adv = _syn
                    else:
                        raw = adv or "（空）"
                        note = "未采纳自由文本建议（不映射为动作）：%s" % raw
                        expl = (expl + "；" + note) if expl else note
                        new["说明"] = expl
                        changed.append("对审计发现[%d].建议=%r 无枚举映射，已省略并保留于说明" % (i, adv))
                        adv = None
            if adv is not None:
                new["建议"] = adv
        out.append(new)
    return (out, "；".join(changed)) if changed else (items, "")


def _check_audit_replies(obj: dict, errs: list) -> None:
    """对审计发现：每条含 item/处置/说明，`建议` 可选（76 §3.3 收紧四/五）。"""
    lst = obj[AUDIT_KEY]
    if not isinstance(lst, list):
        errs.append("'%s' 必须是数组" % AUDIT_KEY)
        return
    for i, item in enumerate(lst):
        if not isinstance(item, dict):
            errs.append("'%s[%d]' 必须是对象 {item,处置,说明}" % (AUDIT_KEY, i))
            continue
        for k in ("item", "处置", "说明"):
            if not str(item.get(k) or "").strip():
                errs.append("'%s[%d]' 缺 %r" % (AUDIT_KEY, i, k))
        d = item.get("处置")
        if d is not None and d not in DISPOSALS:
            errs.append("'%s[%d].处置'=%r 不在 %s" % (AUDIT_KEY, i, d, list(DISPOSALS)))
        adv = item.get("建议")
        if adv is not None and adv not in ADVICES:
            errs.append("'%s[%d].建议'=%r 不在 %s" % (AUDIT_KEY, i, adv, list(ADVICES)))


def validate(act: str, obj: dict, knobs: dict = None, ack_leaf: bool = False) -> tuple:
    """形状校验（不查语义）。@returns: (是否合规, 理由)。"""
    knobs = knobs or {}
    if not isinstance(obj, dict):
        return False, "产出必须是 JSON 对象，实际 %r" % (type(obj).__name__,)
    key = _act_key(act)
    errs = []
    if key == "plan" and "共同约束" in obj:
        from harness import branch_constraints
        try:
            branch_constraints.validate(obj["共同约束"])
        except ValueError as exc:
            errs.append(str(exc))

    if key == "merge":
        for k in ("去向", "reply"):
            if not str(obj.get(k) or "").strip():
                errs.append("缺必填键 %r" % k)
        _check_undecided(obj, errs)
        go = obj.get("去向")
        if go is not None and go not in TOWARDS:
            errs.append("'去向'=%r 不在 %s" % (go, list(TOWARDS)))
        if go in ("再派", "切分") and not obj.get("下一批要点"):
            errs.append("'去向'=%r ⇒ '下一批要点' 必填" % go)
        if go == "切分" and not obj.get("切分目标"):
            errs.append("'去向'=切分 ⇒ '切分目标' 必填")
        if go == "收束" and "uncovered" not in obj:
            errs.append("'去向'=收束 ⇒ 'uncovered' 必填（无缺口时显式给[]）")
        if go == "收束" and "uncovered" in obj:
            unc = obj["uncovered"]
            if not isinstance(unc, list) or not all(
                    isinstance(u, dict) and str(u.get("what") or "").strip()
                    and str(u.get("why") or "").strip() for u in unc):
                errs.append("'uncovered' 必须是 [{what,why}] 列表")
        if (knobs or {}).get(AUDIT_KEY) and not obj.get(AUDIT_KEY):
            errs.append("本轮有审计发现 ⇒ '%s' 必填（每条含 item/处置/说明）" % AUDIT_KEY)
        if AUDIT_KEY in obj:
            _check_audit_replies(obj, errs)
        if "children" in obj:
            errs.append("merge 轮**不写子定义**（不出现 children）：⑵spawn 轮的活")

    elif key == "spawn":
        ch = obj.get("children")
        if not isinstance(ch, list) or not ch:
            errs.append("缺必填键 'children'（非空数组）")
        else:
            for i, c in enumerate(ch):
                if not isinstance(c, dict):
                    errs.append("children[%d] 必须是对象" % i)
                    continue
                r = c.get("role")
                if not str(r or "").strip():
                    errs.append("children[%d] 缺 'role'（收紧一：必须从内置 role 里选）" % i)
                elif r not in role_mod.ROLE_NAMES:
                    errs.append("children[%d].role=%r 不是内置 role（只有 %s）"
                                % (i, r, list(role_mod.ROLE_NAMES)))
                # 2026-11-13：judge 与 spawn_response_format 对齐为可选；
                # 缺省补形状判据（reply/未决），不因模型漏写一个键判死整轮。
                if not str(c.get("judge") or "").strip():
                    c["judge"] = {"json_keys": ["reply", "未决"]}
                for k in ("context", "验收标准", "auth"):
                    if not str(c.get(k) or "").strip():
                        errs.append("children[%d] 缺 %r（父写子 = 选 role + context/验收标准/auth）"
                                    % (i, k))
                _ctx = str(c.get("context") or "")
                if len(_ctx) > 4000:
                    errs.append(
                        "children[%d].context 超长（%d 字 > 4000）：只写任务、目标文件路径、"
                        "验收标准；不要粘贴文件正文，执行叶有 read 工具自己读"
                        % (i, len(_ctx)))
        for k in ("去向", "未决", "uncovered"):
            if k in obj:
                errs.append("act=spawn 的轮次不该出现 %r（角色串了，收紧三）" % k)

    elif key == "plan":
        pts = obj.get("要点")
        if not pts or not isinstance(pts, list) or all(
                not str(p or "").strip() for p in pts):
            errs.append("缺必填键 '要点'（非空数组：这一层要哪几块 + 每块一句话）")
        if "children" in obj:
            errs.append("plan 轮只给要点，不写子定义（不出现 children）")

    else:  # leaf
        if not str(obj.get("reply") or "").strip():
            errs.append("缺必填键 'reply'")
        _check_undecided(obj, errs)
        if "children" in obj:
            errs.append("leaf 里不许出现 'children'（执行/调研 may_spawn=false，"
                        "那条路径永远不可能合法）")
        if "去向" in obj:
            errs.append("leaf 里不许出现 '去向'（决策字段属 merge 轮）")
        may_write = bool((knobs or {}).get("may_write"))
        # V2（ACK 叶）：执行叶的交付是磁盘文件（Harness 机械扫描验收），doc 不再
        # 是必填项；ack_leaf 只在 CC 路径由 run_leaf 显式传入，pi 路径一字不动。
        if may_write and not ack_leaf and not str(obj.get("doc") or "").strip():
            errs.append("role 的 may_write=true ⇒ 'doc' 必填（模型出内容，驱动器落盘，76 §3.4）")
        if not may_write and (obj.get("doc") or obj.get("edits")):
            errs.append("role 的 may_write=false ⇒ 不许产出 'doc'/'edits'")
        edits = obj.get("edits")
        if edits is not None:
            if not isinstance(edits, list):
                errs.append("'edits' 必须是数组")
            else:
                for i, e in enumerate(edits):
                    if not isinstance(e, dict) or not str(e.get("path") or "").strip():
                        errs.append("edits[%d] 必须是对象且带 'path'" % i)
        # 修复叶结构化输出（t-d301b69799d2）：修复子节点可返回 uncovered 结构化数组；
        # 若提供，必须是 [{what,why}] 列表（与 merge 契约同款校验）。自然语言 reply 不能替代。
        if "uncovered" in obj:
            unc = obj["uncovered"]
            if not isinstance(unc, list) or not all(
                    isinstance(u, dict) and str(u.get("what") or "").strip()
                    and str(u.get("why") or "").strip() for u in unc):
                errs.append("'uncovered' 必须是 [{what,why}] 列表")

    return (not errs), ("；".join(errs) if errs else "")


def check_permission(act: str, obj: dict, knobs: dict = None, sink: str = None) -> tuple:
    """越权判定（76 §3.3 收紧二/三）：**丢弃 + 记一笔**，不是抛异常中断。

    ① knobs["may_spawn"] == False 却返回 children ⇒ 直接丢弃该字段 + kind="越权_children"；
    ② act == "spawn" 的轮次返回 去向 / 未决 / uncovered 任一 ⇒ 丢弃 + kind="越权_act"。
    ★ 就地改 obj（返回值是 (是否干净, 理由)；调用方拿到的就是被清洗过的 obj —— 见报告 §假设）。
    @returns: (是否未被越权, 理由)
    """
    knobs = knobs or {}
    if not isinstance(obj, dict):
        return False, "产出必须是 JSON 对象"
    why_out = []
    node_id = str(obj.get("id") or "")

    if not knobs.get("may_spawn", False) and "children" in obj:
        dropped = obj.pop("children")
        why = ("may_spawn=false 的 role 返回了 children（%d 条）⇒ 丢弃该字段（收紧二）"
               % (len(dropped) if isinstance(dropped, list) else 1))
        gate.log_violation(sink, kind="越权_children", node_id=node_id, detail=why)
        why_out.append(why)

    if act == "spawn":
        for k in ("去向", "未决", "uncovered"):
            if k in obj:
                obj.pop(k, None)
                why = "act=spawn 的轮次返回了 %r ⇒ 按越权丢弃（角色串了，收紧三）" % k
                gate.log_violation(sink, kind="越权_act", node_id=node_id, detail=why)
                why_out.append(why)

    return (not why_out), ("；".join(why_out))


def required_keys(act: str) -> list:
    """该 act 的必填键清单（给 assemble_prompt 拼契约说明用；也是「不许自创字段名」的落点）。"""
    return list(CONTRACTS[_act_key(act)]["必填"])
