"""role 表加载 + 覆盖边界（76 §2.1 / §2.2 / §2.4 / §3.1）。

★ 本模块**不出现任何 role 名字的字面判断**：一切从 role.yaml 读（task-def 硬约束 6）。
   role 表是数据，新增 role 不改代码（76 §2.4 末句）。

四个入口（task-def §3.3）：
   KNOBS / ROLE_NAMES  —— 导入期从 role.yaml 读出（不是硬编码）
   load_roles(path)    —— 读 + 校验，不合法 raise
   effective(role, act)—— 基线 ⊕ acts[role][act] 覆盖
   apply_overrides()   —— 只许放宽 out_budget / ctx_budget，其余拒 + 记一笔
   validate(table)     —— 13 旋钮齐全 / 类型 / 池子不变式 / acts 只出现在父 role
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

# ---- 路径口径 -------------------------------------------------------------
# 缺省根 = harness/ 的上一级（= ~/agent-harness/）；可用 env 覆盖，便于测试与别的批次复用。
ROOT = Path(os.environ.get("TREE_ROOT") or Path(__file__).resolve().parent.parent)
DEFAULT_ROLE_YAML = os.environ.get("TREE_ROLE_YAML") or str(ROOT / "role.yaml")
DEFAULT_RUNS_DIR = os.environ.get("TREE_RUNS_DIR") or str(ROOT / "runs")
DEFAULT_DOC_DIR = os.environ.get("TREE_DOC_DIR") or str(ROOT / "doc")

# 三个 act（父的一生，76 §4.5.3）
ACTS = ("plan", "spawn", "merge")
# acts 覆盖只允许碰这 5 个键（task-def §3.3 effective 签名；76 §2.2「thinking/ctx/产出三项改由 act 决定」）
ACT_KEYS = ("thinking", "ctx_budget", "think_budget", "out_budget", "max_tokens")

# ---- 旋钮类型表（13 个；这里是【类型口径】，不是 role 取值） ----------------
ENUMS = {"thinking": ("none", "low", "medium"), "kind": ("text", "agent")}
INT_KNOBS = ("ctx_budget", "think_budget", "out_budget", "min_ctx_tokens",
             "min_out_chars", "max_children_per_round", "max_rounds")
BOOL_KNOBS = ("may_spawn", "may_write")
STR_KNOBS = ("kind", "role_prompt", "tools")

# pi 工具 allowlist（本树实测出现过的工具名；role.yaml tools 旋钮只许填这些）。
# 新 pi 工具要进 role 表，先加到这里。
TOOLS_ALLOWED = ("read", "bash", "grep", "find", "ls", "write")

POOL_FIELD = "max_tokens"      # ★ 池子（不是第 13 个旋钮，owner 裁决 task-def §3.2）
POOL_INVARIANT = "think_budget + out_budget <= max_tokens"


def _read_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError("role.yaml 顶层必须是映射：%s" % path)
    return raw


# 导入期读一次缺省表 —— 只为了拿到旋钮名与 role 名（真正用哪张表由调用方传 path/table 决定）
_BOOT = _read_yaml(DEFAULT_ROLE_YAML)
KNOBS: tuple = tuple(_BOOT.get("knobs") or ())
ROLE_NAMES: tuple = tuple((_BOOT.get("roles") or {}).keys())


# ---- 校验 -----------------------------------------------------------------
def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_knob_row(where: str, row: dict, errs: list) -> None:
    """一行 13 旋钮：齐全 + 类型 + 枚举。"""
    if not isinstance(row, dict):
        errs.append("%s 必须是映射，实际 %r" % (where, type(row).__name__))
        return
    for k in KNOBS:
        if k not in row:
            errs.append("%s 缺旋钮 %r" % (where, k))
    for k in row:
        if k not in KNOBS and k != POOL_FIELD:
            errs.append("%s 出现未知键 %r（旋钮只有 %d 个 + 池子 %s）"
                        % (where, k, len(KNOBS), POOL_FIELD))
    for k in ENUMS:
        if k in row and row[k] not in ENUMS[k]:
            errs.append("%s 的 %s=%r 不在取值域 %s" % (where, k, row[k], list(ENUMS[k])))
    for k in BOOL_KNOBS:
        if k in row and not isinstance(row[k], bool):
            errs.append("%s 的 %s 必须是 bool，实际 %r" % (where, k, row[k]))
    for k in INT_KNOBS:
        if k in row and not _is_int(row[k]):
            errs.append("%s 的 %s 必须是非负整数，实际 %r" % (where, k, row[k]))
        elif k in row and row[k] < 0:
            errs.append("%s 的 %s 不能为负：%r" % (where, k, row[k]))
    if "role_prompt" in row and not isinstance(row["role_prompt"], str):
        errs.append("%s 的 role_prompt 必须是字符串" % where)
    if "tools" in row and not isinstance(row["tools"], str):
        errs.append("%s 的 tools 必须是字符串（逗号分隔的 allowlist，空串 = 无工具）" % where)
    if "tools" in row and isinstance(row["tools"], str) and row["tools"] != "":
        _seen = set()
        for _t in row["tools"].split(","):
            if not _t:
                errs.append("%s 的 tools 有空段（逗号分隔，段不能为空）：%r" % (where, row["tools"]))
            elif _t not in TOOLS_ALLOWED:
                errs.append("%s 的 tools 有非法工具名 %r（合法：%s）" % (where, _t, list(TOOLS_ALLOWED)))
            elif _t in _seen:
                errs.append("%s 的 tools 重复 %r" % (where, _t))
            else:
                _seen.add(_t)


def _check_pool(where: str, row: dict, errs: list) -> None:
    if POOL_FIELD not in row:
        errs.append("%s 缺池子字段 %s（owner 裁决：max_tokens 是一等字段）" % (where, POOL_FIELD))
        return
    if not _is_int(row[POOL_FIELD]):
        errs.append("%s 的 %s 必须是整数，实际 %r" % (where, POOL_FIELD, row[POOL_FIELD]))
        return
    tb, ob = row.get("think_budget"), row.get("out_budget")
    if not (_is_int(tb) and _is_int(ob)):
        return
    if tb + ob > row[POOL_FIELD]:
        errs.append("%s 违反池子不变式 %s：%d + %d = %d > %d"
                    % (where, POOL_INVARIANT, tb, ob, tb + ob, row[POOL_FIELD]))


def _split_table(table: dict) -> tuple:
    """容错：既收「整张表」也收「裸的 roles 映射」。"""
    if isinstance(table, dict) and isinstance(table.get("roles"), dict) and (
            "acts" in table or "knobs" in table or "driver" in table):
        return table.get("roles") or {}, table.get("acts") or {}, table.get("driver") or {}
    return (table or {}), {}, {}


def validate(table: dict) -> list:
    """@returns: 错误列表（空列表 = 合法）。**加载即校验**由 load_roles 负责 raise。"""
    roles, acts, driver = _split_table(table)
    errs = []
    if not roles:
        errs.append("role 表为空：一个 role 都没有")
    if not KNOBS:
        errs.append("缺 knobs 段（13 个旋钮名）")
    for name, row in sorted(roles.items()):
        where = "role %s" % name
        _check_knob_row(where, row, errs)
        _check_pool(where, row, errs)
        # 父 role = may_spawn=true（能力由 role 定死，76 §2.4）
        is_parent = bool(row.get("may_spawn")) if isinstance(row, dict) else False
        per_role = (acts or {}).get(name) or {}
        if per_role and not is_parent:
            errs.append("%s 不是父 role（may_spawn=false）却带 acts：acts 只出现在父 role"
                        % where)
        for act, ov in (per_role or {}).items():
            if act not in ACTS:
                errs.append("%s 的 act %r 不合法（只有 %s）" % (where, act, list(ACTS)))
                continue
            if not isinstance(ov, dict):
                errs.append("%s.%s 的覆盖必须是映射" % (where, act))
                continue
            for k in ov:
                if k not in ACT_KEYS:
                    errs.append("%s.%s 覆盖了不许覆盖的键 %r（只许 %s）"
                                % (where, act, k, list(ACT_KEYS)))
            merged = dict(row or {})
            merged.update(ov)
            _check_pool("%s.%s" % (where, act), merged, errs)
    for name in (acts or {}):
        if name not in roles:
            errs.append("acts 里的 %r 不是已定义的 role" % name)
    if driver:
        nm = driver.get("nodes_max")
        if not _is_int(nm) or nm <= 0:
            errs.append("driver.nodes_max 必须是正整数，实际 %r" % (nm,))
    return errs


def load_roles(path: str = None) -> dict:
    """读 role.yaml（缺省 ~/agent-harness/role.yaml）并**立刻校验**；不合法 raise ValueError。

    @returns: {"knobs": [...], "driver": {...}, "roles": {name: row}, "acts": {...}}
    """
    path = path or DEFAULT_ROLE_YAML
    raw = _read_yaml(path)
    table = {"knobs": list(raw.get("knobs") or ()),
             "driver": dict(raw.get("driver") or {}),
             "roles": dict(raw.get("roles") or {}),
             "acts": copy.deepcopy(raw.get("acts") or {})}
    errs = validate(table) if table["knobs"] else _validate_with_boot_knobs(table)
    if errs:
        raise ValueError("role 表不合法（%s）：\n - %s" % (path, "\n - ".join(errs)))
    return table


def _validate_with_boot_knobs(table: dict) -> list:
    """表里带了自己的 knobs 段才算数，否则用导入期那份（避免自证式校验）。"""
    return validate(table)


def effective(role: str, act: str = None, table: dict = None) -> dict:
    """基线 ⊕ acts[role][act]。act=None ⇒ 纯基线。

    只覆盖 ACT_KEYS（thinking/ctx_budget/think_budget/out_budget/max_tokens）。
    @returns: 13 旋钮 + max_tokens 的**新** dict（不改表本体）。
    """
    table = table or load_roles()
    roles = table["roles"]
    if role not in roles:
        raise KeyError("没有这个 role：%r（表里只有 %s）" % (role, list(roles)))
    row = dict(roles[role])
    if act:
        if act not in ACTS:
            raise KeyError("act 不合法：%r（只有 %s）" % (act, list(ACTS)))
        ov = ((table.get("acts") or {}).get(role) or {}).get(act) or {}
        row.update({k: v for k, v in ov.items() if k in ACT_KEYS})
    return row


def apply_overrides(knobs: dict, overrides: dict, sink: str = None,
                    node_id: str = "") -> tuple:
    """父拉子时的 overrides（76 §2.4）：**只能放宽约束（out_budget / ctx_budget）、不能放宽能力**。

    规则：数值变大（等于也放行）⇒ 放行；传小值 ⇒ 拒（「只许放宽，不许收紧」）；
          其它键（尤其 thinking / may_spawn）一律拒（「能力由 role 定死，不可覆盖」）。
    每拒一条 ⇒ log_violation(kind="越权_overrides")。
    @returns: (生效旋钮 dict, 被拒理由列表)
    """
    out = dict(knobs or {})
    rejected = []
    if not isinstance(overrides, dict):
        return out, ["overrides 必须是映射，实际 %r" % (type(overrides).__name__,)]
    for key, val in overrides.items():
        base = out.get(key)
        if key not in ("out_budget", "ctx_budget"):
            why = ("%s：能力由 role 定死，不可覆盖（76 §2.4，只许放宽 out_budget / ctx_budget）"
                   % key)
        elif not _is_int(val):
            why = "%s=%r 不是整数：只许放宽（数值变大），没法判定" % (key, val)
        elif _is_int(base) and val < base:
            why = "只许放宽，不许收紧（%s：%d < 基线 %d）" % (key, val, base)
        else:
            out[key] = val
            continue
        rejected.append(why)
        _log_overrides(sink, node_id, why)
    return out, rejected


def _log_overrides(sink: str, node_id: str, why: str) -> None:
    from harness import gate  # 局部导入，避免 import 环
    gate.log_violation(sink, kind="越权_overrides", node_id=node_id, detail=why)
