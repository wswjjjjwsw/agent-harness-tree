"""硬/软协议字段分类（V2.1 P2，GPT 补充消息 680 ②）。

硬字段：影响安全、权限、树去向、产物真伪的关键字段。缺失或错值 ⇒ 返回具体
  error（带 code），让对应父节点修复；绝不机械默认、绝不编造语义。
软字段：解释/展示类子字段。缺失或空 ⇒ 填机械默认值继续，默认值必须标明是
  机械填的（不许冒充模型语义）。

★ 红线：绝不把“未决缺失”默认成“无未决”——`未决` 本身是硬字段，缺失即
  hard error；显式 `未决: []` 才表示无未决。同样绝不默认填 `去向`、`uncovered`。
  软默认值只填解释类子字段（why/说明），不碰决定性内容。
"""
from __future__ import annotations

from harness import schema as schema_mod

# 无条件硬字段（每 act 的必填键；条件必填另见 _conditional_hard）。
HARD_FIELDS = {
    "merge": ["去向", "reply", "未决"],
    "leaf": ["reply", "未决"],
    "spawn": ["children"],
    "plan": ["要点"],
}

# 软子字段：(父键, 子键)。只填解释类子字段；父键本身（未决/uncovered/对审计发现）
# 的有无仍由硬规则判定。
SOFT_SUBFIELDS = [
    ("未决", "why"),
    ("uncovered", "why"),
    ("对审计发现", "说明"),
]

SOFT_DEFAULT = "（机械默认值：未提供）"


def _is_nonempty(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, list):
        return len(v) > 0
    return True


def _conditional_hard(act: str, obj: dict, knobs: dict, ack_leaf: bool = False) -> list:
    """条件必填硬字段（与 schema.CONTRACTS 同源，机械判定，不读语义）。
    ack_leaf=True（V2）：执行叶不写 doc，doc 硬规则跳过；其余规则不动。"""
    errs = []
    if act == "merge":
        go = obj.get("去向")
        if go in ("再派", "切分") and not _is_nonempty(obj.get("下一批要点")):
            errs.append({"field": "下一批要点",
                         "code": "hard_field_missing:下一批要点",
                         "message": "去向=%r ⇒ '下一批要点' 必填（硬字段，缺失）" % go})
        if go == "切分" and not _is_nonempty(obj.get("切分目标")):
            errs.append({"field": "切分目标",
                         "code": "hard_field_missing:切分目标",
                         "message": "去向=切分 ⇒ '切分目标' 必填（硬字段，缺失）"})
        if go == "收束" and "uncovered" not in obj:
            errs.append({"field": "uncovered",
                         "code": "hard_field_missing:uncovered",
                         "message": "去向=收束 ⇒ 'uncovered' 必填（硬字段，无缺口时显式给 []）"})
        if (knobs or {}).get(schema_mod.AUDIT_KEY) and not _is_nonempty(obj.get(schema_mod.AUDIT_KEY)):
            errs.append({"field": schema_mod.AUDIT_KEY,
                         "code": "hard_field_missing:对审计发现",
                         "message": "本轮有审计发现 ⇒ '对审计发现' 必填（硬字段）"})
    if act == "leaf" and not ack_leaf:
        if bool((knobs or {}).get("may_write")) and not _is_nonempty(obj.get("doc")):
            errs.append({"field": "doc",
                         "code": "hard_field_missing:doc",
                         "message": "may_write=true ⇒ 'doc' 必填（硬字段，缺失）"})
    return errs


def classify_and_fill(act: str, obj: dict, knobs: dict = None, ack_leaf: bool = False) -> tuple:
    """硬/软分类 + 软字段机械填充。

    @param act: merge/spawn/plan/leaf（与 schema.CONTRACT_BY_ACT 同口径）。
    @param obj: 模型产出 dict（原地改：只填软子字段）。
    @returns: (hard_errors, filled_paths)。
      hard_errors: [{field, code, message}]（空 = 无硬错）；
      filled_paths: ["未决[0].why", ...]（被机械默认填的软子字段路径）。
    """
    knobs = knobs or {}
    if not isinstance(obj, dict):
        return ([{"field": "(obj)", "code": "hard_field_invalid:obj",
                  "message": "产出必须是 JSON 对象"}], [])
    key = schema_mod.CONTRACT_BY_ACT.get(act, "leaf")
    hard = []
    for f in HARD_FIELDS.get(key, []):
        v = obj.get(f)
        if f == "未决":
            # 未决：缺失即硬错；显式 [] 是合法的“无未决”，不碰。
            if f not in obj:
                hard.append({"field": f, "code": "hard_field_missing:未决",
                             "message": "缺必填键 '未决'（硬字段：[] 也要显式给，缺失≠无未决）"})
            elif not isinstance(v, list):
                hard.append({"field": f, "code": "hard_field_invalid:未决",
                             "message": "'未决' 必须是数组"})
        elif f == "children":
            if not isinstance(v, list) or not v:
                hard.append({"field": f, "code": "hard_field_missing:children",
                             "message": "缺必填键 'children'（硬字段，非空数组）"})
        elif f == "要点":
            if not isinstance(v, list) or not any(str(p or "").strip() for p in v):
                hard.append({"field": f, "code": "hard_field_missing:要点",
                             "message": "缺必填键 '要点'（硬字段，非空数组）"})
        elif f == "去向":
            if not _is_nonempty(v):
                hard.append({"field": f, "code": "hard_field_missing:去向",
                             "message": "缺必填键 '去向'（硬字段，决定树去向）"})
            elif v not in schema_mod.TOWARDS:
                hard.append({"field": f, "code": "hard_field_invalid:去向",
                             "message": "'去向'=%r 不在 %s（硬字段，错值）" % (v, list(schema_mod.TOWARDS))})
        else:  # reply
            if not _is_nonempty(v):
                hard.append({"field": f, "code": "hard_field_missing:%s" % f,
                             "message": "缺必填键 %r（硬字段）" % f})
    hard += _conditional_hard(key, obj, knobs, ack_leaf=ack_leaf)
    # 软子字段：只填解释类子字段；父键缺失时不造父键（那是硬错）。
    filled = []
    for parent_key, sub_key in SOFT_SUBFIELDS:
        lst = obj.get(parent_key)
        if not isinstance(lst, list):
            continue
        for i, item in enumerate(lst):
            if not isinstance(item, dict):
                continue
            if not str(item.get(sub_key) or "").strip():
                item[sub_key] = SOFT_DEFAULT
                filled.append("%s[%d].%s" % (parent_key, i, sub_key))
    return hard, filled
