"""role 旋钮 → LLAAT 提交参数 + prompt 拼装（76 §七，task-def §3.6）。

映射表（76 §七，逐条）：
  thinking(none/low/medium) → needs_thinking(bool)：none ⇒ False；low/medium ⇒ True
  max_tokens                → knobs["max_tokens"]（★ 池子，不是 think+out 的求和）
  kind                      —— 【2026-10-15 废止】LLAAT 时代旋钮，pi runner 不读；不再进提交体
  retry_max                 → 0（76 §6.1：temperature=0，重试是同质输出）
  judge                     → node["judge"]（2026-11-12：可选，不再补字数判据）
  ctx_budget                → **只在这里用**：拼 prompt 时按 token 估算截断 context
                              ★ 绝不写进 max_tokens（76 §七：prefill 不受 max_tokens 管）

★ 本模块**不含任何 role 名字的字面判断**：旋钮全部来自传进来的 knobs dict。
★ 本批**不写** wait()/轮询/多轮驱动 —— 属 tree-03（task-def §3.6 末条）。
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from pathlib import Path

from harness import blocks as blocks_mod
from harness import budget
from harness import role as role_mod
from harness import schema

DEFAULT_URL = os.environ.get("TREE_LLAAT_URL") or "http://127.0.0.1:8903"
# token 估算口径（自己写，注明）：按【字符】折算，1 汉字 ≈ 1 token 的保守侧。
#   ★ 与 LLAAT 自己的 est_prompt_tokens() 同一口径：ceil(len(text) / CHARS_PER_TOKEN)，
#     缺省 1.5 字/token（queue.py:224-231）。用同一口径才能跟队列侧的在飞 token 闸对上账。
CHARS_PER_TOKEN = float(os.environ.get("TREE_CHARS_PER_TOKEN") or 1.5)


def estimate_tokens(text: str) -> int:
    """token 估算：ceil(len(text) / CHARS_PER_TOKEN)。口径同 LLAAT est_prompt_tokens。"""
    n = CHARS_PER_TOKEN or 1.5
    return int(math.ceil(len(text or "") / n))


def _truncate_to_tokens(text: str, budget: int) -> str:
    """按 token 估算截断（估算函数 = estimate_tokens）。截断后附一行可 grep 的标记。"""
    if budget is None or budget <= 0:
        return text or ""
    if estimate_tokens(text) <= budget:
        return text or ""
    keep = max(0, int(budget * (CHARS_PER_TOKEN or 1.5)))
    note = "\n[已按 ctx_budget=%d token 截断，原始约 %d token]" % (budget, estimate_tokens(text))
    return (text or "")[:keep] + note


def _role_prompt_path(knobs: dict, base_dir: str = None) -> str:
    rel = str((knobs or {}).get("role_prompt") or "").strip()
    if not rel:
        raise ValueError("role 旋钮里没有 role_prompt（方法文件路径必填，76 §2.1 旋钮⑪）")
    p = Path(rel)
    if not p.is_absolute():
        root = Path(base_dir) if base_dir else role_mod.ROOT
        p = Path(root) / p
    return str(p)


def _read_role_prompt(knobs: dict, base_dir: str = None) -> str:
    path = _role_prompt_path(knobs, base_dir)
    if not os.path.isfile(path):
        # ★ 不静默拼半个 prompt：文件不在就报错（task-def §3.6）
        raise FileNotFoundError("role_prompt 文件不存在：%s（不静默拼半个 prompt）" % path)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ---- B3（tree-13 批次①）：role 的「能干什么 + 手里有什么工具」---------------------------
# ★ 为什么要自读原始 YAML（**不许**从 `role.load_roles()` 取 caps）：
#   ① `role.yaml` 的 role 行里**加不了** `tools`/`scope` —— `harness/role.py:_check_knob_row` 加载即校验，
#      未知键**直接 raise**（全树起不来）；且 `tests/cases.py:169`（冻结面）断言
#      `set(row) - set(KNOBS_LITERAL) - {"max_tokens"} == set()` ⇒ 一行都加不了。
#   ② 顶层新增段**不会被 load_roles 带出来**（实测：它只搬 knobs/driver/roles/acts 四个键）。
#   ⇒ 裁决 A：`caps` 是**顶层段**，由本模块自己读原始 YAML。
CAPS_KEY = "caps"

# ★ 裁决 B：tools 取值**只许**来自 pi 真实内置的 8 个名字（`read write edit bash find grep ls codemode`）。
#   实测 pi **没有** `fetch`、**没有** `agent`（guide §2 核过源码）；`write_block` 是 tree 自造名。
#   ⇒ `fetch` / `agent` / `browser` / `search` / `write_block` 一个字都不许出现在 caps.tools 里。
PI_TOOL_NAMES = ("read", "write", "edit", "bash", "find", "grep", "ls", "codemode")
# ★ guide §3 裁决：**统一基础集**（Leo：「限制他们能力的事情少做」）——
#   六个 role 一律给基础集；差异只保留在「写」这一件事上（`执行` 加 write/edit）。
TOOLS_BASE = ("read", "grep", "find", "ls", "bash")
TOOLS_WRITE = ("write", "edit")


def caps_table() -> dict:
    """读 `role.yaml` 的顶层 `caps:` 段（**调用期**重读 `TREE_ROLE_YAML`，与 role 表同一真值来源）。

    ★ 缺 `caps` / 读不动 / 某 role 不在 caps 里 ⇒ **返回空表（降级）**，绝不抛、绝不返回空串：
      `tests/cases-survive.py:245`（冻结面）自己 dump 一张只有 knobs/driver/roles/acts 的
      role.yaml、在**新进程**里断言新 role 出现在契约里 ⇒ 抛异常会当场砸它。
      role 名单**照常列出**（既有 6/6 行为不变），只是那一条不带 scope/tools。
    """
    path = os.environ.get("TREE_ROLE_YAML") or role_mod.DEFAULT_ROLE_YAML
    try:
        import yaml
        with open(str(path), "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        caps = raw.get(CAPS_KEY) or {}
        if not isinstance(caps, dict):
            return {}
        out = {}
        for name, row in caps.items():
            if not isinstance(row, dict):
                continue
            scope = str(row.get("scope") or "").strip()
            tools = row.get("tools")
            if isinstance(tools, (list, tuple)):
                tools = [str(t).strip() for t in tools if str(t).strip()]
            else:
                tools = []
            out[str(name)] = {"scope": scope, "tools": tools}
        return out
    except Exception:                                        # noqa: BLE001
        return {}


def caps_line(role_name: str, caps: dict = None, min_out: int = 0) -> str:
    """一个 role 的「能干什么 + 手里有什么工具 + 产出下限」一行；取不到 ⇒ 空串（降级）。

    ★ 2026-11-12：字数下限已取消，不再向 spawn 注入「产出下限」一行。
    """
    row = (caps if caps is not None else caps_table()).get(str(role_name)) or {}
    scope = str(row.get("scope") or "").strip()
    tools = list(row.get("tools") or [])
    if not scope and not tools:
        return ""
    parts = []
    if scope:
        parts.append("能干什么：%s" % scope)
    if tools:
        parts.append("手里有工具：%s" % ", ".join(tools))
    return "；".join(parts)


def role_set_note() -> str:
    """act=spawn 的「合法 role 取值域」段（R2，task-def §3 R2）。

    ★ **零 role 名字面值**：六个名字一律从 `role_mod.ROLE_NAMES`（导入期从 role.yaml 读出）拼；
      连那句「不确定就写谁」里的 role 也是按 role 表的旋钮（`may_write` = 唯一能产长文的 role）选出来的，
      不在代码里写死任何一个名字。
    ★ 开关 `TREE_ROLE_SET=0` ⇒ 返回 ""（阴性对照用）；缺省（未设/其它值）⇒ 注入。
    ★ tree-13 批次① B3：每一条 role 后面**追加**「能干什么 + 手里有工具」（来源 = `role.yaml` 顶层
      `caps:` 段，**与 role 名单同一真值来源**）。caps 缺失 ⇒ 逐条降级成既有的裸名单
      （role 名单照常列出、绝不抛、绝不返回空串）。本批**只声明、不真开**工具（真开属批次②）。
    """
    if str(os.environ.get("TREE_ROLE_SET") or "").strip() == "0":
        return ""
    names = [str(n) for n in (role_mod.ROLE_NAMES or ()) if str(n).strip()]
    if not names:
        return ""
    caps = caps_table()
    try:
        roles = (role_mod.load_roles() or {}).get("roles") or {}
    except Exception:                                        # noqa: BLE001
        roles = {}
    lines = ["## 合法 role 取值（act=spawn 的 children[i].role 只能从这 %d 个里选，不许自造）"
             % len(names)]
    for n in names:
        extra = caps_line(n, caps, min_out=(roles.get(n) or {}).get("min_out_chars") or 0)
        lines.append("- %s%s" % (n, ("：%s" % extra) if extra else ""))
    # 「派子拿不准时写谁」= 唯一 may_write=true 的 role（能产长文那个）；取不到就整句不写。
    long_writer = ""
    cands = [n for n in names if str((roles.get(n) or {}).get("may_write")) == "True"]
    if len(cands) == 1:
        long_writer = cands[0]
    if long_writer:
        lines.append("派子时若不确定，一律写「%s」（它是唯一能产长文的 role）。" % long_writer)
    return "\n".join(lines)


def enum_domain_note() -> str:
    """A2：本 act 的**枚举取值域**段 —— 四个集合一律从 `harness/schema.py` 读。

    ★ 代码里**零枚举值字面值**（`再派`/`切分`/… 一个字都不出现）：与 role_set_note 同一范式。
    ★ 为什么要有它：改前实测 merge 契约里 `解掉/上冒/记未决/继续派` 命中 **0**（§1）⇒ 模型只能猜枚举值。
    ★ ★ 标签措辞的硬约束（**实测逼出来的**）：段里的每一个字都不许含 role 名的子串。
      行内实测：标签写成 `对审计发现[].处置` 时，`审计` 撞上 `harness/role.py` 的一个 role 名 ⇒
      tree-09 电池 `cases-survive.py` 的 S2 阴性对照（`TREE_ROLE_SET=0 ⇒ 契约里不含任何一个 role 名`）
      当场翻红（`run-survive` 11/11 → 10/11）。那份用例在**禁改清单**里 ⇒ 只能改我这侧的措辞：
      标签用键路径本身（`处置` / `建议`），把「它属于哪个形状」交给既有契约的「条件必填」行去说
      （merge 契约本来就有 `对审计发现 —— 本轮有审计发现时` 那一行，一个字没动）。
    """
    sets = (
        ("去向", tuple(schema.TOWARDS or ())),
        ("未决[].type", tuple(schema.UNDECIDED_TYPES or ())),
        ("处置（父对每条发现的处置）", tuple(schema.DISPOSALS or ())),
        ("建议（父对每条发现的建议；只有形状审查会填）", tuple(schema.ADVICES or ())),
    )
    lines = ["## 枚举取值域（这些键的值只能从下面这几个里选，不许自造）"]
    for label, values in sets:
        if not values:
            continue
        lines.append("- %s：%s" % (label, " / ".join(str(v) for v in values)))
    return "\n".join(lines)


def shape_note(act: str) -> str:
    """A1：本 act 的**产出形状**（键名逐字）—— 真值 = `schema.SHAPES`（单点，prompt 里不再各抄）。"""
    key = schema._act_key(act)
    shape = (schema.SHAPES or {}).get(key)
    if not shape:
        return ""
    return "## 产出形状（键名逐字，不许自创字段）\n%s" % shape


def judge_keys_note() -> str:
    """A1b：judge 的**合法键**—— 真值 = `harness/leafrun.py` 的 `JUDGE_KEYS`（**零漂移**）。

    ★ 为什么必须从代码读：`prompts/规划.md` 曾手写 11 个键，而引擎只认其中 3 个 ⇒
      模型照 prompt 写 `one_of`/`regex`/… 必被判拒、白跑一轮（2026-10-15 体检发现）。
    """
    # 2026-11-12：不再向 spawn 注入判据键/字数约束。
    return ""


def contract_note(act: str, knobs: dict = None, ack_leaf: bool = False) -> str:
    """契约说明（该 act 的必填键清单）—— 拼进 prompt 的第三段。

    ★ 本批（tree-12 Part A）只**追加**两段，既有三段一字未动：
      ① 枚举取值域（A2，来源 = harness/schema.py 的四张表）；
      ② 块字段「填路径不填内容」+ 工具规格 write_block（A1/A3，来源 = harness/blocks.py）。
    """
    key = schema._act_key(act)
    c = schema.CONTRACTS[key]
    if ack_leaf and key == "leaf":
        # V2（ACK 叶）：执行叶只回最小 ACK，不产 doc/edits，不注入 pi 的块字段
        # 约定（blocks/ + write_block 工具规格与 CC 工具不兼容，且与“不写报告”矛盾）。
        return "\n".join([
            "## 本轮产出契约（act=leaf，ACK 叶 · V2：只做真实工作，不写报告）",
            "必填键：reply、未决",
            "不许出现：doc、edits、children、去向",
            "只输出一个 JSON 对象，不要额外解释。",
            "## ACK 形状（键名逐字，不许自创字段）",
            "{\"reply\": \"20~200 字：写了哪些文件、检查结果如何\", "
            "\"未决\": [{\"type\": \"…\", \"what\": \"…\", \"why\": \"…\"}]}",
            "## 枚举取值域（这些键的值只能从下面这几个里选，不许自造）",
            "- 未决[].type：缺信息 / 冲突 / 超范围 / 判不了 / 超时",
        ])
    lines = ["## 本轮产出契约（act=%s，键名逐字，不许自创字段）" % key,
             "必填键：%s" % "、".join(c["必填"])]
    for k, v in (c.get("条件必填") or {}).items():
        lines.append("条件必填：%s —— %s" % (k, v))
    if c.get("禁止"):
        lines.append("不许出现：%s" % "、".join(c["禁止"]))
    lines.append("只输出一个 JSON 对象，不要额外解释。")
    sn = shape_note(act)
    if sn:
        if os.environ.get("TREE_STRUCTURED") == "1":
            sn = sn.replace("你写进去的那个路径", "完整正文")
        lines.append(sn)
    if key == "spawn":
        # ★ R2：取值域进 prompt（既有三段一字不改，只**追加**这一段）。
        note = role_set_note()
        if note:
            lines.append(note)
        jn = judge_keys_note()
        if jn:
            lines.append(jn)
    # ── A2（tree-12）：枚举取值域进契约（四个集合从 harness/schema.py 读）──
    lines.append(enum_domain_note())
    # ── A1/A3（tree-12）：块字段声明 + 工具规格（从 harness/blocks.py 读，零字面字段名）──
    _may_write = None if knobs is None else knobs.get("may_write")
    if os.environ.get("TREE_STRUCTURED") == "1":
        lines.append("## 结构化输出\n本轮没有工具；字段直接填写正文，不要声称写过文件或填写虚构路径。")
    elif _may_write is False:
        # F1（t-d301b69799d2）：may_write=false 的叶不许产出 doc/edits
        # （schema.validate 硬性规定），故不注入“必须用 write 工具写文件、字段填路径”
        # 的块字段约定；否则 prompt 与校验自相矛盾，模型按 prompt 办事反被判形状不合。
        lines.append("## 只读约束（may_write=false）\n"
                     "- 不许产出 doc/edits 字段；大文本直接写进 reply，不要声称写过文件、"
                     "不要填写文件路径。")
    elif blocks_mod.declaration_on():
        lines.append(blocks_mod.block_field_note())
        lines.append(blocks_mod.tool_spec())
    return "\n".join(lines)


def assemble_prompt(node: dict, knobs: dict, act: str = None,
                    base_dir: str = None, include_role_prompt: bool = True,
                    ack_leaf: bool = False) -> str:
    """顺序：role_prompt 文件全文 + node["context"] + 契约说明（76 §3.6）。

    ★ include_role_prompt（2026-11-06，CC 接线用）：False 时不读 role_prompt 文件、
      不拼 role_prompt 头。CC runner 的 role_prompt 已在 agent 文件的 system prompt 里
      （第 1 步 §6.5：不重复注入），再拼一次是浪费 tokens 且可能自相矛盾。
      缺省 True：pi 路径与既有调用者一字不动。

    ★ ctx_budget 只在这里用：context 超出 ctx_budget 时按 token 估算截断。
    role_prompt 文件不存在 ⇒ 报错（不静默拼半个 prompt）。

    ★ 修-2（tree-12 批次① · 第 2 轮）：**引用形态的 `context` 在组装期解引用成正文**。
      只改 `context` 这一段；`context` 之外的既有行为一字未动，`_truncate_to_tokens` 的既有语义
      也不动（顺序 = **先解引用、后按 ctx_budget 截断**）。
      · 解不出来（文件不在磁盘上）⇒ **不许静默给空**，抛 `blocks.RefUnresolved`（记一笔 violations）：
        叶子侧收成 `exit_class=形状不合`（该轮不算数），父侧收成既有 `_reject`。
      · 为什么必须有这一步：修-1 让 `children[i].context` 也变引用后，子节点若只拿到
        `blocks/<sha1>.md` 那 50 个字符，树会**静默退化**成「有指针没内容」（比不做更坏）。

    ★ 修-3（tree-13 批次① B1）：**拼进 prompt 的每一块分别过 6000 字符 / 260 行的截断**
      （`harness/budget.py`，实测正常文档最大值 5935/254 取整）。与既有 `_truncate_to_tokens`
      （按 `ctx_budget` 估 token 截断）的关系：**两条都在**，既有语义一字不动，
      顺序 = **解引用 → 既有 token 截断 → 本批 6000/260 截断**。
      ★ 截断 = 截 + 打标记 + 记账（`预算_超限_已截断`），**永不判拒**、不落终态。
      ★ 哪些块一视同仁？`role_prompt` 文件全文 / `上下文` / `验收标准` / `auth` / 契约段
        （`contract_note`）—— **每一块分别截**，不是只截总量。
    """
    node = node or {}
    # include_role_prompt=False（CC）：role_prompt 已在 agent 文件的 system prompt 里，不重复读。
    head = _read_role_prompt(knobs, base_dir) if include_role_prompt else ""
    ctx_raw = node.get("context") or ""
    if not isinstance(ctx_raw, str):
        ctx_raw = json.dumps(ctx_raw, ensure_ascii=False, indent=1)
    # ── 修-2：引用形态的 context 先解引用（非引用形态原样通过，既有行为不变）──
    ctx_res, why = blocks_mod.resolve_prompt_text(ctx_raw, str(node.get("id") or ""),
                                                  run_dir=node.get("_run_dir"))
    if why:
        raise blocks_mod.RefUnresolved(why)
    ctx = _truncate_to_tokens(ctx_res, (knobs or {}).get("ctx_budget"))
    contract = contract_note(act, knobs=knobs, ack_leaf=ack_leaf)
    # ── 修-3（tree-13 B1）：每一块**分别**过 6000/260 截断（解引用与 token 截断都在它之前）──
    #   ★ 记账落点 = 该节点的 run 目录（`node["_run_dir"]`，驱动器/叶子给它）；拿不到 ⇒ 只截不记
    #     （绝不因为记账落点缺失而判拒或抛 —— 截断本身是兜底，不是判据）。
    sink = budget.sink_for_run(node.get("_run_dir") or base_dir
                               or os.environ.get(blocks_mod.RUN_DIR_ENV))
    nid = str(node.get("id") or "")
    if head:
        head, i1 = budget.truncate_block(head, path=_role_prompt_path(knobs, base_dir))
        if i1:
            budget.log_truncation(sink, nid, i1)
    ctx, i2 = budget.truncate_block(ctx, path=budget.NO_PATH)
    contract, i3 = budget.truncate_block(contract, path=budget.NO_PATH)
    for _info in (i2, i3):
        if _info:
            budget.log_truncation(sink, nid, _info)
    parts = [head.rstrip("\n")] if head else []
    parts.append("\n## 上下文（父写进来的 context）\n" + ctx)
    # B-fix (t-ad1047692e75): context JSON "id" fields are logical node IDs, not file paths.
    # Model mistook root.c2.x for read paths (see t-ac0e5a58a64f forensics). Explicit guard.
    parts.append("\n> NOTE: In the context JSON above, \"id\" fields (e.g. root.c2.1) are "
                 "logical node identifiers, NOT file paths. "
                 "Do NOT call read/ls/find on them.\n")
    for k in ("验收标准", "auth"):
        if str(node.get(k) or "").strip():
            seg, info = budget.truncate_block(str(node[k]), path=budget.NO_PATH)
            if info:
                budget.log_truncation(sink, nid, info)
            parts.append("\n## %s\n%s" % (k, seg))
    parts.append("\n" + contract)
    # Keep frozen constraints outside the old per-block context truncation.
    from harness import branch_constraints
    fixed = branch_constraints.render(node.get("_run_dir") or base_dir
                                      or os.environ.get(blocks_mod.RUN_DIR_ENV), nid)
    if fixed:
        parts.append(fixed)
    # V2.1 收尾 (B2)：叶 prompt 注入其依赖的前提 + 证伪标记格式（生产者）。
    # 只有 act=="leaf" 且该节点确有依赖前提时才注入；无前提的节点不加噪音。
    if act == "leaf":
        try:
            from harness import premise as _premise_mod
            _rd = node.get("_run_dir") or base_dir or os.environ.get(blocks_mod.RUN_DIR_ENV) or ""
            _my = []
            if _rd:
                for _pid, _rec in _premise_mod.read_premises(str(_rd)).items():
                    if str(node.get("id") or "") in [str(x) for x in (_rec.get("dependents") or [])]:
                        _my.append((_pid, str(_rec.get("statement") or "")))
            if _my:
                _lines = ["## 本轮依赖的前提（证伪即上报）"]
                for _pid, _st in _my:
                    _lines.append("- %s：%s" % (_pid, _st))
                _lines.append("执行中若发现某个前提不成立（有工具输出、文件或实测数据证明它错了），"
                              "必须在未决[] 里加一条：{\"type\": \"冲突\", \"what\": \"前提被证伪：<前提编号>：<一句话依据>\", \"why\": \"<证据>\"}。"
                              "只在正文里提一句不算，树收不到。不要编造不存在的证伪。")
                parts.append("\n".join(_lines))
        except Exception:
            pass
    return "\n".join(parts)


def map_params(node: dict, act: str = None, req_id: str = "", stage: str = "",
               knobs: dict = None, base_dir: str = None) -> dict:
    """role 旋钮 → LLAAT /submit 的 body（76 §七）。

    knobs 缺省 = role.effective(node["role"], act)：基线 ⊕ acts 覆盖。
    """
    node = node or {}
    if knobs is None:
        # ★ leaf 是【契约名】不是父的 act：只有父的三种 act 才走 acts 覆盖，
        #   其余（leaf / None）取纯基线。这里做归一，不让 effective 收到未知 act。
        knob_act = act if act in role_mod.ACTS else None
        knobs = role_mod.effective(node.get("role"), knob_act)
    # 2026-11-12：judge 可选，不再强制、不再自动补字数判据。
    judge = dict(node.get("judge") or {})
    pool = (knobs or {}).get("max_tokens")
    body = {
        "prompt": assemble_prompt(node, knobs, act, base_dir=base_dir),
        "judge": judge,
        "max_tokens": int(pool),              # ★ 池子；ctx_budget 绝不写这里
        "needs_thinking": (knobs or {}).get("thinking") != "none",
        "retry_max": 0,                       # 76 §6.1
        "tags": {"src": "tree/%s/%s" % (req_id, stage)},
    }
    return body


def submit(node: dict, act: str = None, req_id: str = "", stage: str = "",
           url: str = None, tier: str = None, timeout: float = 30.0,
           knobs: dict = None, base_dir: str = None) -> tuple:
    """真的 POST {url}/submit。@returns: (task_id, 拒收原因)。

    被 400 拒 ⇒ 把 rejected 原文带回来（不吞、不抛）。url 缺省 http://127.0.0.1:8903，
    可用参数或环境变量 TREE_LLAAT_URL 覆盖（owner 复验要打到真 8903，也能指到别的口）。
    """
    body = map_params(node, act, req_id, stage, knobs=knobs, base_dir=base_dir)
    if tier:
        body["tier"] = tier
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request((url or DEFAULT_URL).rstrip("/") + "/submit", data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        return str(payload.get("id") or ""), ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            why = json.loads(raw).get("rejected") or raw
        except Exception:
            why = raw
        return "", str(why)
    except Exception as exc:                      # 连不上 / 超时 ⇒ 如实带回
        return "", "%s: %s" % (type(exc).__name__, exc)


def get_task(task_id: str, url: str = None, timeout: float = 30.0) -> dict:
    """GET /task/<id>（本批只做只读取回，不做等待循环 —— 轮询属 tree-03）。"""
    req = urllib.request.Request((url or DEFAULT_URL).rstrip("/") + "/task/" + str(task_id),
                                 method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")
