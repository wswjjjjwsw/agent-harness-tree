"""tree-13 批次① 的两件机械功课：**输出预算截断**（B1）+ **复读检测 / 生成退化**（B2）。

★ 本模块是这两件事的**单一实现处**（SSOT）：`harness/submit.py`（组装期）/ `harness/driver.py`（父轮）/
  `harness/leafrun.py`（叶子轮）一律调这里，别处不许另写一份（另写必然漂）。

计量口径（逐字钉死，task-def §2-B1；Leo 第一原则「用 shell 就能知道多少」的 Python 等价物）：
  · `chars(text) = len(text)`  —— Unicode **字符**数，与 `wc -m` 同口径；
    ★ **绝不许用字节数**（`len(text.encode("utf-8"))` / `os.path.getsize` 一律禁止）：
      中文 UTF-8 一个字 3 字节，按字节算会把上限定歪 2~3.7 倍（实测 3000 汉字 = 9000 字节）。
  · `lines(text) = text.count("\\n")` —— 与 `wc -l` 同口径（数换行符）。
  · 判据：`chars > 6000` **或** `lines > 260` ⇒ 截断（**严格大于**：恰 6000 / 恰 260 **不截**）。

两个上限 = 实测正常文档最大值取整（`runs/tree-05-e2e-20261013-224212/doc/*.md`：`wc -m` 最大 **5935**、
`wc -l` 最大 **254**）⇒ **6000 字符 / 260 行**。**不许自己发明别的数。**

★ 截断 = **截 + 打标记 + 记账**，**永不判拒**（Leo 第一原则）：本模块里没有任何
  「太长 ⇒ 拒 / ⇒ 失败 / ⇒ 该轮不算数」的分支，也**不落终态**。
★ 截断标记**末尾追加**、可 grep（`TRUNC_MARK`）；`N` = 原始字符数 − 保留字符数（可机械复算）。

★ 复读检测**必须先做转义归一化再测量**（owner 裁决 C）：pi 的正文是**内联 JSON 字符串**，换行在
  raw 里是**字面两字符 `\\n`** ⇒ 整份产出在 raw 里只有几行 ⇒ 「连续相同行」在 raw 上**恒为 1**、
  判据**恒不触发**（= 把一条永远为假的检测当成验过，最坏的一类假过）。
  归一化**只用于测量**：不改 raw 本身、不改解析输入。
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from harness import blocks, gate

# ---- 两个上限（★ 单一常量，实测最大值取整） --------------------------------
TRUNC_CHARS = 6000          # = 实测正常文档最大 5935 取整（`wc -m` 口径）
TRUNC_LINES = 260           # = 实测最大 254 取整（`wc -l` 口径）
TRUNC_MARK = "字符未显示"    # 标记的稳定子串（可 grep）
# 纯内联、无文件来源时标记里写的路径占位（逐字）
NO_PATH = "无（未落盘）"

# ---- 复读判据的三个阈值（逐字钉死；阳性侧 274 / 0.115、阴性侧最差 0.841 ⇒ 分离度极大） ----
REPEAT_MAX_RUN = 8          # 最长连续相同行 ≥ 8
REPEAT_TAIL_RATIO = 0.66    # 尾部窗口内非空行去重后占比 < 0.66
REPEAT_TAIL_WINDOW = 2000   # 尾部窗口（字符）

# ---- 记账 kind（逐字；一律走既有 gate.log_violation，gate.VIOLATION_KINDS 是禁改面） --------
TRUNC_KIND = "预算_超限_已截断"
DEGEN_KIND = "产出_生成退化"
OVER_BUDGET_KIND = "预算_超限_截断后仍超"    # 裁决 D（调用侧接住 ContextOverflow）

DEGEN_WHY = "生成退化"       # why 的起始子串（逐字）
# ★ 判退化那一轮**不许**出现的假罪名（那个措辞误导诊断好几轮了）
FALSE_BLAME = "找不到一个 JSON 对象"

_MARK_TPL = "…（还有 %d %s，全文在 %s，要看哪一段说一声）"


# ============================================================ 计量（与 wc -m / wc -l 同口径）
def chars(text) -> int:
    """`len(text)` —— Unicode **字符**数（与 `wc -m` 同口径）。★ 不许编码成字节再数。"""
    return len("" if text is None else str(text))


def lines(text) -> int:
    """`text.count("\\n")` —— 与 `wc -l` 同口径（数换行符）。"""
    return ("" if text is None else str(text)).count("\n")


# ============================================================ B1 截断
def _label(path) -> str:
    """标记里的「全文在 <路径>」：有落盘/文件来源 ⇒ 真路径；纯内联 ⇒ `无（未落盘）`。"""
    s = str(path or "").strip()
    return s if s else NO_PATH


def _mark(dropped: int, label: str) -> str:
    return _MARK_TPL % (int(dropped), TRUNC_MARK, label)


def _fit_prefix(text: str, budget_chars: int, budget_lines: int) -> str:
    """取 `text` 的前缀：字符数 ≤ budget_chars **且** 换行数 ≤ budget_lines。"""
    kept = text[:max(0, int(budget_chars))]
    if kept.count("\n") > int(budget_lines):
        idx = -1
        for _ in range(int(budget_lines) + 1):
            idx = kept.find("\n", idx + 1)
            if idx < 0:
                break
        if idx >= 0:
            kept = kept[:idx]          # 切在「第 budget_lines+1 个换行」之前 ⇒ 恰 budget_lines 个换行
    return kept


def truncate_block(text, path=None, limit_chars=TRUNC_CHARS, limit_lines=TRUNC_LINES) -> tuple:
    """单块的 6000 字符 / 260 行截断（**先到先截**）。@returns `(new_text, info)`。

    · 未超限 ⇒ `(原文, {})`（**info 非空才算真截断了**）；
    · 真截断 ⇒ 末尾追加可 grep 的标记（保留段末不以换行结尾时先补一个换行）；
      `info` 逐字含 `{"chars_before","chars_after","lines_before","lines_after",
      "dropped_chars","path"}`，其中 `dropped_chars` = 原始字符数 − **保留**字符数（= 标记里的 N）。
    ★ 截断后**重新测量**并保证 `chars ≤ limit_chars` 且 `lines ≤ limit_lines`（含标记本身：
      标记也占预算 —— 否则「截完仍超」就是自欺）。
    ★ 本函数**永不判拒**，只截 + 标。
    """
    t = "" if text is None else str(text)
    c_before, l_before = chars(t), lines(t)
    if c_before <= limit_chars and l_before <= limit_lines:
        return t, {}
    label = _label(path)
    budget_c, budget_l = int(limit_chars), int(limit_lines)
    kept, final = "", ""
    for _ in range(16):
        kept = _fit_prefix(t, budget_c, budget_l)
        sep = "" if (not kept or kept.endswith("\n")) else "\n"
        final = kept + sep + _mark(c_before - len(kept), label)
        ex_c = chars(final) - int(limit_chars)
        ex_l = lines(final) - int(limit_lines)
        if ex_c <= 0 and ex_l <= 0:
            break
        if ex_l > 0:                    # 行超了 ⇒ 少保留换行（分离出的那个换行也算行）
            budget_l = max(0, budget_l - ex_l)
            continue
        budget_c = max(1, budget_c - ex_c)
    info = {"chars_before": c_before, "chars_after": chars(final),
            "lines_before": l_before, "lines_after": lines(final),
            "dropped_chars": c_before - len(kept), "path": label}
    return final, info


def log_truncation(sink, node_id: str, info: dict) -> str:
    """把一笔截断记进 `violations.jsonl`（`kind` 逐字 `预算_超限_已截断`；**不判拒**）。

    ★ 一律走既有 `gate.log_violation`（`gate.VIOLATION_KINDS` 在禁改清单里，实测它**不校验 kind**
      ⇒ 不必改 gate.py；见报告「假设与替代方案」）。
    """
    info = info or {}
    return gate.log_violation(
        sink, kind=TRUNC_KIND, node_id=str(node_id or ""),
        detail=("预算超限已截断：来源 %s 原 %d 字符/%d 行 ⇒ %d 字符/%d 行（丢 %d 字符；"
                "标记已写在正文末尾；★ **不判拒**，该轮照常跑）"
                % (info.get("path"), int(info.get("chars_before") or 0), int(info.get("lines_before") or 0),
                   int(info.get("chars_after") or 0), int(info.get("lines_after") or 0),
                   int(info.get("dropped_chars") or 0))))


def truncate_blocks(obj, run_dir=None, sink=None, node_id="") -> tuple:
    """★ 按 `blocks.iter_block_slots(obj)` 的**同一覆盖面**就地截断 + 记账。

    覆盖面**逐字复用**那个既有函数（顶层块字段 + `children[i].<块字段>`）—— **不许另写一套遍历**
    （另写必然漂；tree-12 的教训：只遍历顶层会让 `context` 在生产路径上一次都不被处理，
    因为 `context` **只**由模型在 act=spawn 轮写进 `children[i].context`）。

    @returns `(obj, records)`；`records` 每笔 = `{"field","path","chars_before","chars_after",
             "lines_before","lines_after","dropped_chars"}`（未命中 ⇒ 空列表、零副作用）。
    ★ 命中 ⇒ 每笔记一次 `预算_超限_已截断`（kind 逐字）。**绝不判拒。**
    """
    if not isinstance(obj, dict):
        return obj, []
    records = []
    for label, container, key in blocks.iter_block_slots(obj):
        value = container.get(key)
        if not isinstance(value, str):
            continue
        seg, info = truncate_block(value, path=slot_source_path(value, run_dir))
        if not info:
            continue
        container[key] = seg
        rec = dict(info)
        rec["field"] = label
        records.append(rec)
        log_truncation(sink, node_id, info)
    return obj, records


def slot_source_path(value, run_dir=None) -> str:
    """槽位值的来源路径：引用形态（`blocks/<sha1>.md`）⇒ 真文件路径；否则 `无（未落盘）`。"""
    if blocks.is_ref(value):
        d = blocks.hint_run_dir(value, run_dir)
        if d:
            return str(blocks.blocks_dir(d) / str(value)[len(blocks.REF_PREFIX):])
    return NO_PATH


# ============================================================ B2 复读检测（生成退化）
def normalized_for_repeat(text) -> str:
    """**只用于测量**的转义归一化：字面 `\\n` → 换行、`\\t` → 制表符、`\\r` → 回车。

    ★ 不改 raw 本身、不改解析输入（解析器看到的仍是原文）。理由见本模块文件头（裁决 C）。
    """
    return ("" if text is None else str(text)).replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")


def repeat_metrics(text, tail_window=REPEAT_TAIL_WINDOW) -> tuple:
    """`(最长连续相同行, 尾部窗口内非空行去重占比, 明细)` —— **在归一化后的文本上量**。

    · 「行」= 归一化后的 `split("\\n")`；**空行也计入「相同」**（连续空行同样算复读）；
    · 尾部窗口 = 归一化文本的**最后 `tail_window` 个字符**；占比只数**非空行**（`strip()` 非空）；
      窗口内**一个非空行都没有** ⇒ 占比记 `0.0`（判退化）。
    """
    norm = normalized_for_repeat(text)
    parts = norm.split("\n")
    max_run, cur, prev = 0, 0, None
    for ln in parts:
        cur = cur + 1 if ln == prev else 1
        prev = ln
        if cur > max_run:
            max_run = cur
    tail = norm[-int(tail_window):] if tail_window else norm
    nonempty = [ln for ln in tail.split("\n") if ln.strip()]
    ratio = (len(set(nonempty)) / float(len(nonempty))) if nonempty else 0.0
    detail = {"max_run": max_run, "tail_ratio": ratio, "tail_nonempty_lines": len(nonempty),
              "tail_window": int(tail_window), "chars": chars(text), "lines": len(parts)}
    return max_run, ratio, detail


def _looks_structured(text) -> bool:
    """产出是不是「结构化」的（能取出一个 JSON 对象 / 带 ```json 围栏）？

    ★ 只做**能不能取出**的判断，**不校验契约字段**（那是 schema 的事，分工不许混）。
      取不出来 ⇒ 按散文处理（照旧用占比判据）。
    """
    s = str(text or "").strip()
    if not s:
        return False
    import json as _json
    if "```" in s:
        i = s.find("```")
        start = s.find("\n", i)
        end = s.find("```", i + 3)
        if start > 0 and end > start:
            s = s[start + 1:end]
    depth, start_i = 0, -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start_i = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start_i >= 0:
                    try:
                        _json.loads(s[start_i:i + 1])
                        return True
                    except Exception:
                        pass
    return False


def is_degenerate(text, max_run=REPEAT_MAX_RUN, tail_ratio=REPEAT_TAIL_RATIO,
                  tail_window=REPEAT_TAIL_WINDOW) -> tuple:
    """写飞了没有？`(是否退化, why)` —— 两条判据**或**关系，任一命中即「生成退化」。

    · 最长连续相同行 ≥ 8；· 尾部 2000 字符内非空行不重复占比 < 0.66。
    阈值**不许改**（阳性侧实测 274 / 0.115 vs 阴性侧最差 0.841 —— 分离度极大，改阈值只会引入误伤）。

    `why` 逐字含 `生成退化` + **四个数**（两个实测值 + 两个阈值）；★ 不含假罪名
    `找不到一个 JSON 对象`、**不贴原文**（既有解析器会贴 `raw[:200]`，本路径必须不贴）。
    """
    # tree-16 ①：空产出**不判**生成退化（空产出是另一回事，由 driver 的正常路径处置）。
    #   实测过的假罪名：raw 0 字符 ⇒ 占比算出 0.000 ⇒ 被写成「生成退化」，把排查带偏。
    if not str(text or "").strip():
        return False, "产出为空（不是生成退化 ⇒ 由「产出为空」的既有路径处置）"

    mr, ratio, _det = repeat_metrics(text, tail_window=tail_window)
    structured = _looks_structured(text)
    hit_run = mr >= int(max_run)
    # tree-16 ②：**结构化产出不用「唯一行占比」判据**。原阴性侧是 9 篇散文（最差 0.841）、
    #   阳性侧是那份空表格行复读（0.115），中间没样本。而 spawn 契约 JSON 每个子节点的
    #   字段名天然同构 ⇒ 实测占比 0.636，落在中间 ⇒ 健康 JSON 被误杀（tree-16-qwen-f 实证：
    #   模型吐了 1917 token 的合法 spawn JSON，仅因 0.636 < 0.66 判退化 ⇒ 整轮作废）。
    #   ⇒ 结构化时**只看连续相同行**：真复读那一份最长连续相同行 274（阈值 8），这一条照样抓得住。
    hit_tail = (not structured) and ratio < float(tail_ratio)
    why = ("%s：最长连续相同行 %d（阈值 %d）%s；尾部 %d 字符内非空行不重复占比 %.3f（阈值 %.2f）%s"
           " ⇒ %s（判据先做转义归一化再测量；只记一笔，不贴原文）"
           % (DEGEN_WHY, mr, int(max_run), "≥" if hit_run else "<", int(tail_window), ratio,
              float(tail_ratio), "<" if hit_tail else "≥",
              "判退化" if (hit_run or hit_tail) else "不判退化"))
    return bool(hit_run or hit_tail), why


def degenerate_detail(text, max_run=REPEAT_MAX_RUN, tail_ratio=REPEAT_TAIL_RATIO,
                      tail_window=REPEAT_TAIL_WINDOW) -> str:
    """`violations` 的 detail（`kind` 逐字 `产出_生成退化`）：**四个数**都在（两个实测值 + 两个阈值）。"""
    mr, ratio, det = repeat_metrics(text, tail_window=tail_window)
    return ("%s：最长连续相同行 %d（实测）/ 阈值 %d；尾部不重复占比 %.3f（实测）/ 阈值 %.2f"
            "（尾部窗口 %d 字符、非空行 %d 条、归一化后 %d 行；转义归一化只用于测量）"
            % (DEGEN_WHY, mr, int(max_run), ratio, float(tail_ratio), int(det["tail_window"]),
               int(det["tail_nonempty_lines"]), int(det["lines"])))


# ============================================================ 裁决 D：超预算不再终止整树
def relax_knobs(knobs, hist, est) -> tuple:
    """按**实际需要**放宽本次生效的预算。@returns `(放宽后的 knobs 副本, 放宽后的 ctx_budget)`。

    · `ctx_budget ← max(budget, hist + est) + 小余量`（向上取整 + 2% 余量，至少 1）；
    · ★ 必须传**副本**（`dict(knobs)`）—— **不许就地改**调用方的 knobs（会污染后续轮）；
    · ★ 依据（写进报告）：tree 侧 `ctx_budget` 是**注意力预算，不是模型窗口**
      （服务端 `--dflash-max-ctx 114688`，历史 8527 远低于窗口）⇒ 继续跑在物理上是安全的，
      超限只作**记账与告警**，不再是死刑。
    """
    out = dict(knobs or {})
    base = int(out.get("ctx_budget") or 0)
    need = int(hist) + int(est)
    margin = max(1, int(math.ceil(need * 0.02)))
    widened = max(base, need) + margin
    out["ctx_budget"] = widened
    return out, widened


def over_budget_detail(hist, est, budget, widened) -> str:
    """`预算_超限_截断后仍超` 的 detail（**带 hist / est / budget 三个数**，逐字可核）。"""
    return ("预算_超限_截断后仍超：hist=%d est=%d budget=%d（hist+est=%d > budget）⇒ 在调用侧接住 "
            "ContextOverflow：把**本次生效**的 ctx_budget 放宽到 %d（以实际需要 hist+est 为准 + 小余量）"
            "重调一次并继续跑（不落终态、不判死；ctx_budget 是注意力预算不是模型窗口，"
            "session.py 的真正 raise 一字未动）" % (int(hist), int(est), int(budget),
                                                   int(hist) + int(est), int(widened)))


# ============================================================ V2.1 P2: budget rungs
BUDGET_RUNGS = (8192, 16384, 32768, 65536)  # rung caps; top 65536, never jump to unmeasured 131072
OVERFLOW_NAME = "overflow.jsonl"


def next_budget_rung(current: int) -> int | None:
    """当前预算的下一档；已到顶 ⇒ None（不再扩，改走 Checkpoint + 响亮失败）。"""
    cur = int(current or 0)
    for r in BUDGET_RUNGS:
        if r > cur:
            return r
    return None


def record_overflow(run_dir: str, record: dict) -> dict:
    """ContextOverflow 机器可读记录 → <run>/overflow.jsonl（一行一条）。"""
    import json as _json
    import time as _time
    rec = dict(record or {})
    rec["ts"] = _time.strftime("%Y-%m-%d %H:%M:%S")
    if not str(run_dir or "").strip():
        return rec  # run_dir 为空时不写盘（不写到 cwd）
    p = Path(run_dir) / OVERFLOW_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(_json.dumps(rec, ensure_ascii=False) + chr(10))
    return rec


def sink_for_run(run_dir) -> str:
    """`<run_dir>/violations.jsonl`（拿不到 run_dir ⇒ gate 的缺省落点）。"""
    d = str(run_dir or "").strip()
    if d:
        return str(Path(d) / "violations.jsonl")
    if os.environ.get("TREE_BLOCK_RUN_DIR"):
        return str(Path(os.environ["TREE_BLOCK_RUN_DIR"]) / "violations.jsonl")
    return gate.default_sink()
