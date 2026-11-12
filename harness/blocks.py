"""块字段（天生的大文本）的落盘 / 引用 / 机械接管 / 引用完整性（task-def §2 A1/A3/A4/A5/A6）。

★ 本模块是「哪些字段天生是大文本」的**唯一来源**（SSOT）：契约文案与接管判定都从 `BLOCK_FIELDS` 读，
  本模块之外（`submit.py` / `leafrun.py` / `driver.py`）**不写字面字段名**。
★ 两条口径**不矛盾**，一条都不许丢（task-def §3 硬约束 1 / §2 A1 的 5 与 6）：
  ① **产物合法、只是长得不合规 ⇒ 绝不判拒**：落盘 + 字段值换成引用 + 记一笔（`takeover`）；
     哪怕模型没走工具通道（裸长字段直吐）也照落盘、照替换、照记账，**该轮不被拒**。
  ② **引用指向的文件不在磁盘上 ⇒ 判拒**（`check_refs`）：残缺必须硬 —— 放行就是喂假数据。
★ `BIG_TEXT_THRESHOLD_CHARS` **只用于声明侧归类与接管触发**，**运行侧零判拒分支**
  （它不出现在任何「太长 ⇒ 拒」的条件里）。
★ 落盘是原子的（tempfile + os.fsync + os.replace）；文件名 = 内容 sha1 ⇒ 同一正文落两次是同一份（幂等）。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from harness import gate

# ---- SSOT：块字段清单（字段名 + 谁写） --------------------------------------
# ★ 声明侧契约文案（submit.contract_note）与接管判定都从它读 ⇒ 表改了、文案跟着改（无漂移）。
BLOCK_FIELDS = (("doc", "leaf"), ("reply", "leaf"), ("context", "父"))

# ---- 单一阈值常量：**只用于声明侧归类**与接管触发（运行侧零判拒分支） --------
BIG_TEXT_THRESHOLD_CHARS = 200

# ---- 引用形态：REF_PREFIX + <文件名> + ".md"（字符串，可直接当相对路径解引用） ----
#   ★ 2026-10-15 放宽（修-3）：原正则要求 `blocks/<40 位 hex>.md`，于是"像路径但不是引用形态"的串
#     （如模型自造的 `blocks/plan.md`，仅 14 字）从两道闸中间穿过去 ——
#     `check_refs` 只查引用形态 ⇒ 跳过它；`takeover` 只接管 ≥200 字 ⇒ 也跳过它
#     ⇒ 它被当成"合法的短正文"原样放行，最终喂进子节点的 prompt，子在 900s 里反复 ls/find 找不到。
#     放宽后一律当引用 ⇒ 一律走磁盘存在性校验（tree-17-validator 现场的那个洞）。
REF_PREFIX = "blocks/"
BLOCK_SUFFIX = ".md"

# ---- 记账（复用 gate.log_violation；本模块是 kind 字面值的唯一落点） ----------
TAKEOVER_KIND = "接管_未走工具通道"
NOTE_NOT_VIA_TOOL = "本次未走工具通道"
# ★ 装订期解引用失败的 kind（修-2 的记账；gate.VIOLATION_KINDS 在禁改清单里，log_violation 不校验 kind）
REF_UNRESOLVED_KIND = "引用_组装期不可解"

# 「声明侧开关」（阴性对照用；缺省/其它值 ⇒ 注入）——照既有 role_set_note 的 TREE_ROLE_SET 范式
DECL_OFF = "0"

_REF_RE = re.compile("^" + re.escape(REF_PREFIX) + r"[^/]+" + re.escape(BLOCK_SUFFIX) + r"$")

# ---- children[] 里的块字段（修-1）------------------------------------------
#   ★ 为什么必须覆盖它：`context` **只**由模型在 act=spawn 轮写进 `children[i].context`
#     （`driver.spawn_children` 落成子节点字段）⇒ 只遍历顶层 = 生产路径上 `context` 一次也不会被
#     接管/校验（现场：`root` 2758 字、最大 5739 字那条正是它）。
CHILDREN_KEY = "children"
# 路径标签：顶层 = 字段名本身；子 = `children[i].<字段名>`（**字段名一律从表读**，零字面值）
CHILD_LABEL = "children[%d].%s"

# ---- 组装期「文件在哪」（修-2）---------------------------------------------
#   ★ 为什么需要一个进程侧的提示：冻结面 `harness/{audit,final_audit,shape_review}.py` 调
#     `assemble_prompt(node, knobs, act="leaf")` 时**拿不到 run_dir**（本批不许改它们）
#     ⇒ 只能由建 Driver 的人把 run_dir 放进 env；再加一份「本进程落过盘的 ref → run 目录」兜底
#     （同一进程里 land 完接着组装 prompt 的场合，例如复验探针）。
#   ★ 它只管**找路径**：文件在不在仍然真查磁盘（`deref` 不看这个提示）。
#   ★ 「按 ref 记目录」为什么不会喂错内容：文件名 = 正文 sha1 ⇒ **同一文件名在任何目录里内容
#     都逐字节相同**（所以命中哪个目录都拿到同一份正文）；从没落过盘的 ref 不在表里 ⇒ 判不可解。
RUN_DIR_ENV = "TREE_BLOCK_RUN_DIR"
_LANDED = {}          # ref 串 → [它真落过盘的 run 目录…]（最近在前）
_MAX_LANDED = 8


def declaration_on() -> bool:
    """声明侧开关：`TREE_BLOCKS_DECL=0` ⇒ 不注入块字段约定与工具规格（阴性对照用）。"""
    return str(os.environ.get("TREE_BLOCKS_DECL") or "").strip() != DECL_OFF


def block_field_names() -> tuple:
    """块字段名（**调用期**从 `BLOCK_FIELDS` 读 ⇒ 换表即跟着变，证明文案不是硬编码）。"""
    return tuple(str(name) for name, _who in BLOCK_FIELDS)


# ---- 覆盖面（修-1）：顶层字段 + children[i].<块字段> ------------------------
def iter_block_slots(obj) -> list:
    """遍历 obj 里**所有**块字段槽位（可写）：@returns [(label, container, key)]。

    · 顶层块字段：label = 字段名本身（`"context"`）；
    · `children[i]` 的块字段：label = `children[i].<字段名>`（如 `"children[0].context"`）；
      `children` 不是数组 / 某子不是对象 ⇒ 跳过（形状问题归 schema.validate 管）。
    ★ 字段名一律从 `block_field_names()` 读（**零字面字段名**）⇒ 表改了、覆盖面跟着改。

    ★ 为什么必须含 children：`context` **只**由模型在 act=spawn 轮写进 `children[i].context`
      （`driver.spawn_children` 落成子节点字段）⇒ 只遍历顶层 = 生产路径上 `context` 一次也不会
      被接管/校验（现场：`root` 2758 字、最大 5739 字那条正是它）。
    """
    out = []
    if not isinstance(obj, dict):
        return out
    for name in block_field_names():
        if name in obj:
            out.append((name, obj, name))
    kids = obj.get(CHILDREN_KEY)
    if isinstance(kids, list):
        for i, child in enumerate(kids):
            if not isinstance(child, dict):
                continue
            for name in block_field_names():
                if name in child:
                    out.append((CHILD_LABEL % (i, name), child, name))
    return out


# ---- 组装期解引用（修-2）---------------------------------------------------
def note_run_dir(run_dir, ref=None) -> str:
    """登记「这个 ref 真落过盘的 run 目录」（`land_block` 自动调用；驱动器建 Driver 时也调一次）。

    ★ 存在的理由：冻结面 `harness/{audit,final_audit,shape_review}.py` 调 `assemble_prompt`
      时拿不到 run_dir（本批不许改它们）⇒ 组装期靠这份登记 + env 找块文件。
    @returns: 归一后的路径字符串（run_dir 为空 ⇒ ""）
    """
    if not run_dir:
        return ""
    p = str(Path(run_dir))
    if ref:
        lst = _LANDED.setdefault(str(ref), [])
        if p in lst:
            lst.remove(p)
        lst.insert(0, p)
        del lst[_MAX_LANDED:]
    return p


def candidate_dirs(value=None, run_dir=None) -> list:
    """找块文件时要查的 run 目录（按可信度排序，去重）：

    ① 显式传进来的 `run_dir` → ② env `TREE_BLOCK_RUN_DIR`（冻结面走这条路）
    → ③ 本进程**真把这个 ref 落过盘**的目录（最近在前）。
    """
    out = []
    for d in [run_dir, os.environ.get(RUN_DIR_ENV)] + list(_LANDED.get(str(value) or "") or []):
        s = str(d or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def hint_run_dir(value=None, run_dir=None) -> str:
    """第一个候选目录（没有 ⇒ ""）。★ 只是「往哪找」的提示，文件在不在仍真查磁盘。"""
    c = candidate_dirs(value, run_dir)
    return c[0] if c else ""


def read_block_at(run_dir, value) -> str:
    """按**指定** run 目录把引用读成原文（非引用 ⇒ 原样返回；文件不在 ⇒ ""）。"""
    if not is_ref(value):
        return "" if value is None else str(value)
    p = _path_of(run_dir, value)
    try:
        _is_file = p.is_file()
    except OSError:
        return ""
    if not _is_file:
        return ""
    return p.read_text(encoding="utf-8")


def resolve_prompt_text(value, node_id: str = "", run_dir=None) -> tuple:
    """★ 修-2：组装 prompt 时把「引用形态的块字段」解引用成**正文**。

    @returns: (text, why)。why 非空 ⇒ **解不出来**（引用指向空气）：
              · 已走 `gate.log_violation` **记一笔**（响亮，不静默）；
              · 调用方必须走既有响亮路径（该轮不算数 / 叶子形状不合），**不许静默给空**。

    ★ 非引用形态（普通正文）原样返回、(text, "")：**既有行为一字不动**。
    """
    if not is_ref(value):
        return ("" if value is None else str(value)), ""
    dirs = candidate_dirs(value, run_dir)
    for d in dirs:
        p = _path_of(d, value)
        if p.is_file():
            return p.read_text(encoding="utf-8"), ""
    why = ("引用不可解：%s 不存在（文件不在磁盘上；查过 %s）"
           % (value, dirs if dirs else "无（TREE_BLOCK_RUN_DIR 未设、本进程也没落过它）"))
    gate.log_violation(_hint_sink(dirs), kind=REF_UNRESOLVED_KIND, node_id=str(node_id or ""),
                       detail="组装期解引用失败：%s ⇒ 该轮不算数（不许静默给空）" % why)
    # 结构化模式（无工具）：模型写的路径不是真引用 ⇒ 原样当正文用，不判死
    import os as _os
    if _os.environ.get("TREE_STRUCTURED") == "1":
        return str(value), ""
    return "", why


def _hint_sink(dirs) -> str:
    """记账落点：知道 run 目录 ⇒ 落它的 violations.jsonl；否则走 gate 的缺省落点。"""
    return str(Path(dirs[0]) / "violations.jsonl") if dirs else ""


class RefUnresolved(ValueError):
    """组装期引用不可解（修-2）。★ 继承 ValueError ⇒ 既有 `except ValueError` 也接得住。"""


# ---- 落点与命名 ------------------------------------------------------------
def blocks_dir(run_dir) -> Path:
    """块文件落点：<run_dir>/blocks（父目录由 land_block 自建）。"""
    return Path(run_dir) / REF_PREFIX.rstrip("/")


def block_name(text) -> str:
    """内容 hash 文件名：sha1(utf-8 原文).hexdigest() + '.md'。"""
    body = "" if text is None else str(text)
    return hashlib.sha1(body.encode("utf-8")).hexdigest() + BLOCK_SUFFIX


def land_block(run_dir, text) -> str:
    """原子落盘（tempfile + os.replace，父目录自建）。空/空白 ⇒ raise（不落一个空块）。

    ★ 文件名 = 内容 hash ⇒ 同一正文落两次是**同一路径**（覆盖同一份，不产生第二份）。
    @returns: 引用串（`blocks/<sha1>.md`）
    """
    if text is None or not str(text).strip():
        raise ValueError("块正文为空/空白 ⇒ 拒绝落盘（不许落一个空的块文件）")
    body = str(text)
    d = blocks_dir(run_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = block_name(body)
    ref = make_ref(name)
    # ★ 修-2：登记「这个 ref 落在哪个 run 目录」（组装期解引用靠它找路径；只记路径、不缓存内容）
    note_run_dir(run_dir, ref)
    target = d / name
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix="." + name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return make_ref(name)


def make_ref(rel_name) -> str:
    """块文件名 → 引用串。"""
    return REF_PREFIX + str(rel_name)


def is_ref(value) -> bool:
    """值是不是引用形态（REF_PREFIX 开头 + 40 位 hex + '.md'）。"""
    return isinstance(value, str) and bool(_REF_RE.match(value))


def _path_of(run_dir, value) -> Path:
    return blocks_dir(run_dir) / str(value)[len(REF_PREFIX):]


def deref(run_dir, value) -> tuple:
    """解引用判定（**只查文件在不在，不读内容**）。

    非引用形态 ⇒ (True, "")（没什么可解的）。文件不在磁盘上 ⇒
    (False, "引用不可解：<路径> 不存在（文件不在磁盘上）")。
    """
    if not is_ref(value):
        return True, ""
    if _path_of(run_dir, value).is_file():
        return True, ""
    return False, "引用不可解：%s 不存在（文件不在磁盘上）" % value


def read_block(run_dir, value) -> str:
    """把引用读成**原文**（落 doc/ 之类要正文的场合用）。文件不在 ⇒ ""（调用方应先过 check_refs）。"""
    if not is_ref(value):
        return "" if value is None else str(value)
    p = _path_of(run_dir, value)
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8")


def check_refs(obj, run_dir) -> tuple:
    """★ A5：**求值之前**跑。obj 里任一块字段（**含 `children[i]` 的**）是引用形态而文件缺失
    ⇒ (False, why)。

    这是本批**唯一该拒**的情形（拒因必含子串 `引用不可解`）。命中这条的损失只是几十 token 的骨架
    —— 这正是「事前约定」的价值；而「产物合法但字段太长」走 takeover，**绝不判拒**。
    ★ 覆盖面（修-1）：顶层块字段 + `children[i].<块字段>` 两条都查 —— 只查顶层会让
      `context`（**只**由模型在 spawn 轮写进 children）在生产路径上永远不被校验。
    """
    if not isinstance(obj, dict):
        return True, ""
    # 结构化模式（无工具）：模型不可能落文件，context 里的路径不是引用 ⇒ 不校验
    import os as _os
    if _os.environ.get("TREE_STRUCTURED") == "1":
        return True, ""
    for label, container, key in iter_block_slots(obj):
        value = container.get(key)
        if is_ref(value):
            ok, why = deref(run_dir, value)
            if not ok:
                return False, ("%s（字段 %s）" % (why, label))
    return True, ""


# ---- A4 机械接管 ------------------------------------------------------------
def takeover(obj, run_dir, node_id, went_through_tool=False) -> tuple:
    # 结构化模式（无工具）：doc 是正文、由 land_doc 直接落盘，不走「转块引用」
    import os as _os
    if _os.environ.get("TREE_STRUCTURED") == "1":
        return obj, []
    """★ A4 机械接管：把 ≥ 阈值的块字段**原地**换成引用 + 落盘 + 记一笔。

    触发条件（逐条）：`isinstance(value, str)` 且 `len(value) >= BIG_TEXT_THRESHOLD_CHARS`
    且**不是**引用形态。命中 ⇒ land_block 落盘 → 字段值 = make_ref(...) → 记一笔 record。

    ★ **接管 = 绝不判拒**（Leo 第一原则）：模型没走工具通道、直接吐了裸长字段时，
      也照落盘、照替换、照记账，**该轮不被拒、不算「该轮不算数」**。

    `went_through_tool` 口径（task-def §2 A4 ①-b）：进入本函数时该 obj 的某个块字段**已是引用形态**
    ⇒ 说明本轮走了工具通道（或已被接管过）⇒ True；否则 = 模型直接吐了裸长字段 ⇒ False（record 里
    带 `note="本次未走工具通道"`，并落 violations）。

    ★ 覆盖面（修-1）：顶层块字段 + `children[i].<块字段>`；命中就改**那一层的那个键**。
    ★ **绝不判拒**（Leo 第一原则）：裸长字段照落盘、照替换、照记账，该轮照常推进。

    @returns: (obj, records)；obj 是**就地改过**的同一个 dict；records 每笔 =
             {"node_id","field","chars","ref","went_through_tool"[, "note"]}
             —— `field` 用**路径标签**（子节点上是 `children[0].context` 这种），便于事后定位。
    """
    if not isinstance(obj, dict):
        return obj, []
    via_tool = bool(went_through_tool) or any(
        is_ref(container.get(key)) for _label, container, key in iter_block_slots(obj))
    records = []
    for label, container, key in iter_block_slots(obj):
        value = container.get(key)
        if not isinstance(value, str) or is_ref(value):
            continue
        if len(value) < BIG_TEXT_THRESHOLD_CHARS:
            continue
        ref = land_block(run_dir, value)
        container[key] = ref
        rec = {"node_id": str(node_id), "field": label, "chars": len(value), "ref": ref,
               "went_through_tool": via_tool}
        if not via_tool:
            rec["note"] = NOTE_NOT_VIA_TOOL
        records.append(rec)
    return obj, records


def log_takeover_records(sink, records) -> list:
    """把接管记录落进 violations.jsonl（`kind=接管_未走工具通道`）。

    ★ kind 逐字只有一处落点（本模块）⇒ leafrun / driver 两个调用点共用同一条记账通道。
    ★ `gate.log_violation` 实测不校验 kind（gate.py:49 直接写 JSONL）⇒ 不必改禁改清单里的 gate.py。
    @returns: 实际写到的路径列表
    """
    out = []
    for rec in (records or ()):
        if rec.get("went_through_tool"):
            continue
        out.append(gate.log_violation(
            sink, kind=TAKEOVER_KIND, node_id=str(rec.get("node_id") or ""),
            detail="%s：字段 %s %d 字 ⇒ 已落盘 %s（只记一笔，本次不判拒）"
                   % (NOTE_NOT_VIA_TOOL, rec.get("field"), int(rec.get("chars") or 0), rec.get("ref"))))
    return out


# ---- A1/A2/A3 声明侧文案（生成之前就约定好） --------------------------------
BLOCK_FIELD_MARK = "## 块字段（天生的大文本）：正文写进文件，字段里填你真实写进去的路径"
TOOL_SPEC_MARK = "## 可用工具（生成之前就约定好，不是你现场发明的）"


def block_field_note() -> str:
    """A1/A2：块字段的「填路径不填内容」声明 —— 字段名与阈值**一律从本模块的 SSOT 读**。

    ★ 覆盖面也写进声明（修-1）：**顶层字段 + `children[i].` 下的同名字段**都要按这个约定做，
      否则模型只对顶层字段守约、而 `context` 恰恰只出现在 `children[i]` 里。
    """
    names = "、".join(block_field_names())
    return "\n".join([
        BLOCK_FIELD_MARK,
        "- 这些字段：%s" % names,
        "- 覆盖面：顶层这几个字段**以及** `%s[i].` 下的同名字段（父写给子时同样按这个约定办）"
        % CHILDREN_KEY,
        "- 约定：单个字段 ≥ %d 字 ⇒ **必须**用 write 工具写进文件，字段里填你真实写进去的那个**路径**"
        "（不要把正文贴进 JSON）" % BIG_TEXT_THRESHOLD_CHARS,
        "- 路径形态：%s<文件名>.md（相对本次 run 目录，可直接当相对路径解引用）" % REF_PREFIX,
        "- ★ 硬性：字段里填的路径必须**真实存在** —— 驱动器会在派单前真的去磁盘上找；"
        "指不到文件 ⇒ 该轮当场判拒（理由「引用不可解」），这一轮的活白干",
        "- ★ 反过来：**父给你的 `context` 若是打不开的路径** ⇒ 立刻把它写进 "
        "`未决[].type=缺信息` 收工（说清你缺哪份材料）。**不要自己到处 ls / find 找料**："
        "找不到的东西不会因为多找几次出现，只会把这一轮耗到超时。",
        "- 为什么：正文贴进 JSON 会把整份产出撑爆/解析不出来 ⇒ **整份丢弃**（净亏）",
    ])


def tool_spec() -> str:
    """A3：工具规格（名字 / 参数 / 落点 / **收场**）—— 拼进契约段，这就是「生成之前就约定好」。

    ★ 2026-10-15 重写：原版只讲"怎么写文件"，没讲"写完之后干什么"。实测（bonsai 单节点探针）
      模型把同一个 write 逐字重复 12 次、始终不交 JSON ⇒ 撞超时。原因不是它写错，是
      **工具调用本身被当成了终点**，而循环里没有任何"该收尾了"的信号。故在此补一条硬性收场。
    """
    return "\n".join([
        TOOL_SPEC_MARK,
        "- 工具名：write（★ pi 真实内置工具；`write_block` 是 tree 自造名，pi 里没有，调不出来）",
        "- 参数：{path: str, content: str}（落盘路径 + 正文原文，两个都要给）",
        "- 落点：必须写进本次 run 目录下的 %s 目录（路径形态 %s<文件名>.md）" % (
            REF_PREFIX.rstrip("/"), REF_PREFIX),
        "- 用法：大文本一律先 write 落盘，产出 JSON 里该字段填你**写进去的那个真实路径**",
        "- ★★ **工具调用不是终点，那个 JSON 对象才是本次交付**：工具用完必须收尾交 JSON。"
        " 反复调同一个工具、或写完文件不回填路径，这一轮都算白干（驱动器只会按你交的 JSON 收单）。",
        # tree-16：实测模型「文件写好了（6225 字节）、JSON 里却漏了 doc」⇒ 被 may_write 的必填
        # 契约当场判拒。它不是不会写，是**收尾漏回填**。这里把要求写成硬性的一步。
        "- ★ 收尾（硬性，不许省）：先 write 落盘，**再把你刚写进去的那个路径回填进 JSON 的对应字段**。"
        "  may_write=true 的 role ⇒ `doc` **必填**，值就是那个路径。"
        "  **只写文件、不回填 ⇒ 整份产出作废**（这一轮的活白干，判拒后你会被整份丢掉）",
    ])
