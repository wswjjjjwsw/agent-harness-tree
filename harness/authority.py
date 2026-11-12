"""Run-scoped authority: who is read-only, who is derived, who may be written.

The B acceptance showed the tree deciding 168-vs-186 from prose confidence:
a parent/audit leaf said "the oracle says 186", the real file says 168, and a
repair leaf then rewrote the protected product. Nothing in the runtime knew
which file was the authority.

This module makes that explicit:

  · authority.json declares authoritative / read_only / writable paths;
  · fact_sheet() renders the actual disk facts (JSON scalars, small text);
  · canonical_conflict() rewrites a conflict blocker from the real values and
    picks the writable side as the repair target.

It never writes to the run directory. It never calls a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

# 缺省策略：input/ 只读权威；product/blocks/doc 可写。
# 具体 run 可以用 authority.json 覆盖（例如把 product/answer.txt 设为受保护）。
DEFAULT_POLICY = {
    "authoritative": ["input/"],
    "read_only": ["input/"],
    "writable": ["product/", "blocks/", "doc/"],
}

POLICY_KEYS = ("authoritative", "read_only", "writable")
MAX_FACT_BYTES = 2048
MAX_FACT_FILES = 6


# ---------------------------------------------------------------- 路径与策略
def _norm_rel(value) -> str:
    """归一成 run 内相对路径；绝对路径/.. 一律拒绝（返回空串）。"""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or raw.startswith("~"):
        return ""
    while raw.startswith("./"):
        raw = raw[2:]
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return ""
    return "/".join(parts)


def _norm_list(values) -> list:
    out = []
    for value in values or []:
        rel = _norm_rel(value)
        if rel and rel not in out:
            out.append(rel)
    return out


def _policy_from(raw: dict) -> dict:
    policy = {}
    for key in POLICY_KEYS:
        values = (raw or {}).get(key)
        policy[key] = _norm_list(values if isinstance(values, list) else [])
    # 权威路径天然只读
    for rel in policy["authoritative"]:
        if rel not in policy["read_only"]:
            policy["read_only"].append(rel)
    if not policy["writable"]:
        policy["writable"] = list(DEFAULT_POLICY["writable"])
    return policy


def load_policy(run_dir: str) -> dict:
    """读 run_dir/authority.json；缺失或非法时用缺省策略（不抛异常）。"""
    path = Path(str(run_dir or "")) / "authority.json"
    if not path.is_file():
        return _policy_from(DEFAULT_POLICY)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _policy_from(DEFAULT_POLICY)
    if not isinstance(raw, dict):
        return _policy_from(DEFAULT_POLICY)
    merged = {
        "authoritative": raw.get("authoritative", DEFAULT_POLICY["authoritative"]),
        "read_only": raw.get("read_only", DEFAULT_POLICY["read_only"]),
        "writable": raw.get("writable", DEFAULT_POLICY["writable"]),
    }
    return _policy_from(merged)


def _matches(rel: str, prefix: str) -> bool:
    rel = str(rel or "").strip("/")
    prefix = str(prefix or "").strip("/")
    if not prefix:
        return False
    return rel == prefix or rel.startswith(prefix + "/")


def is_read_only(rel: str, policy: dict = None) -> bool:
    policy = policy or _policy_from(DEFAULT_POLICY)
    rel = _norm_rel(rel)
    if not rel:
        return True
    return any(_matches(rel, p) for p in policy.get("read_only") or [])


def is_writable(rel: str, policy: dict = None) -> bool:
    policy = policy or _policy_from(DEFAULT_POLICY)
    rel = _norm_rel(rel)
    if not rel or is_read_only(rel, policy):
        return False
    return any(_matches(rel, p) for p in policy.get("writable") or [])


def is_authoritative(rel: str, policy: dict = None) -> bool:
    policy = policy or _policy_from(DEFAULT_POLICY)
    rel = _norm_rel(rel)
    return bool(rel) and any(_matches(rel, p) for p in policy.get("authoritative") or [])


def writable_abs_paths(run_dir: str, policy: dict = None) -> list:
    """沙箱用：可写路径的绝对路径列表（目录或单文件）。"""
    policy = policy or load_policy(run_dir)
    root = Path(str(run_dir or "")).resolve()
    out = []
    for rel in policy.get("writable") or []:
        abs_path = (root / rel).resolve()
        try:
            abs_path.relative_to(root)
        except ValueError:
            continue
        out.append(str(abs_path))
    return out


def read_only_abs_paths(run_dir: str, policy: dict = None) -> list:
    policy = policy or load_policy(run_dir)
    root = Path(str(run_dir or "")).resolve()
    out = []
    for rel in policy.get("read_only") or []:
        abs_path = (root / rel).resolve()
        try:
            abs_path.relative_to(root)
        except ValueError:
            continue
        out.append(str(abs_path))
    return out


# ---------------------------------------------------------------- 事实读取
def _resolve(run_dir: str, cite: str) -> tuple:
    """cite ⇒ (run 相对路径, 绝对路径)；越界/不存在 ⇒ ("", "")。"""
    rel = _norm_rel(cite)
    if not rel:
        return "", ""
    root = Path(str(run_dir or "")).resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return "", ""
    if not path.is_file():
        return "", ""
    return rel, str(path)


def _flatten(value, prefix: str = "", depth: int = 0) -> dict:
    """JSON 标量拍平；最多两层，避免把大对象塞进 prompt。"""
    out = {}
    if isinstance(value, dict) and depth < 2:
        for key, item in value.items():
            key_text = str(key)
            name = (prefix + "." + key_text) if prefix else key_text
            out.update(_flatten(item, name, depth + 1))
        return out
    if isinstance(value, list):
        if all(not isinstance(x, (dict, list)) for x in value):
            out[prefix or "value"] = value
        return out
    if isinstance(value, (str, int, float, bool)) or value is None:
        out[prefix or "value"] = value
    return out


def facts_for(run_dir: str, cite: str) -> dict:
    """单个文件的机械事实：{sha256, bytes, kind, values/text}。"""
    rel, abs_path = _resolve(run_dir, cite)
    if not rel:
        return {}
    path = Path(abs_path)
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    result = {
        "path": rel,
        "sha256": hashlib.sha256(raw).hexdigest()[:16],
        "bytes": len(raw),
    }
    text = raw.decode("utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            result["kind"] = "json"
            result["values"] = _flatten(parsed)
            return result
    result["kind"] = "text"
    if len(text) <= MAX_FACT_BYTES:
        result["text"] = text
    else:
        result["text"] = text[:MAX_FACT_BYTES] + "\n…（截断，共 %d 字节）" % len(raw)
    result["numbers"] = sorted(set(re.findall(r"\d+", text)))
    return result


def _render_fact(fact: dict, policy: dict) -> str:
    if not fact:
        return ""
    rel = fact["path"]
    if is_authoritative(rel, policy):
        label = "只读权威"
    elif is_read_only(rel, policy):
        label = "只读"
    elif is_writable(rel, policy):
        label = "可写目标"
    else:
        label = "只读"
    head = "- %s（%s，sha256 %s，%d 字节）" % (
        rel, label, fact.get("sha256", ""), int(fact.get("bytes") or 0))
    if fact.get("kind") == "json":
        values = fact.get("values") or {}
        shown = []
        for key, value in values.items():
            if isinstance(value, (dict, list)):
                continue
            shown.append("%s=%s" % (key, json.dumps(value, ensure_ascii=False)))
        if shown:
            return head + "：" + "；".join(shown)
        return head + "：无可读标量"
    text = str(fact.get("text") or "").strip()
    if len(text) <= 400:
        return head + "：\n" + text
    return head + "：\n" + text[:400] + "\n…（截断）"


def render_facts(run_dir: str, cites: list, max_files: int = MAX_FACT_FILES,
                 title: str = "Harness 权威事实（程序读取，非模型陈述）") -> str:
    """把指定引用的真实内容渲染成紧凑事实表。"""
    policy = load_policy(run_dir)
    picked, seen = [], set()
    for rel in (_norm_rel(x) for x in (cites or [])):
        if not rel or rel in seen:
            continue
        seen.add(rel)
        if len(picked) >= max_files:
            break
        picked.append(rel)
    lines = [x for x in (_render_fact(facts_for(run_dir, rel), policy) for rel in picked) if x]
    if not lines:
        return ""
    return "\n".join([
        "",
        "## " + str(title or "Harness 权威事实（程序读取，非模型陈述）"),
        *lines,
        "",
        "规则：以上是磁盘实际内容；任务描述、审计结论或父节点指令里的数值若与此不一致，",
        "以此表为准。只允许修改标记为「可写目标」的文件；只读/权威文件不得修改。",
    ])


def fact_sheet(run_dir: str, cites: list = None, max_files: int = MAX_FACT_FILES) -> str:
    """把权威/被引用文件的真实内容渲染成紧凑事实表。

    @param cites 可选：只渲染这些引用；None = 策略里的 authoritative 文件。
    """
    policy = load_policy(run_dir)
    wanted = []
    if cites:
        wanted = [c for c in (_norm_rel(x) for x in cites) if c]
    else:
        root = Path(str(run_dir or ""))
        for rel in policy.get("authoritative") or []:
            abs_path = root / rel
            if abs_path.is_file():
                wanted.append(rel)
            elif abs_path.is_dir():
                for fp in sorted(abs_path.rglob("*")):
                    if fp.is_file():
                        wanted.append(fp.relative_to(root).as_posix())
    return render_facts(run_dir, wanted, max_files=max_files)


# ---------------------------------------------------------------- 冲突改写
def _numbers(text: str) -> set:
    return set(re.findall(r"\d+", str(text or "")))


def _primary_values(item: str, facts: dict) -> dict:
    """从权威事实里挑出与本条 blocker 相关的数值字段。

    · item 里点名了字段（如 prime_count）⇒ 只用那一个；
    · 只有一个数值字段 ⇒ 用它；
    · 多个数值字段且 item 没点名 ⇒ 返回空（判不准，不硬改）。
    """
    values = facts.get("values") if isinstance(facts, dict) else None
    if not values:
        return {}
    numeric = {k: v for k, v in values.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if not numeric:
        return {}
    item_text = str(item or "")
    # 精确点名优先；再允许「count」这类词匹配到「prime_count」。
    named = {k: v for k, v in numeric.items() if k and k in item_text}
    if not named:
        tokens = [t for t in re.findall(r"[A-Za-z_]+", item_text) if len(t) >= 3]
        named = {k: v for k, v in numeric.items()
                 if k and any(t in k for t in tokens)}
    if len(named) == 1:
        return named
    if len(numeric) == 1:
        return numeric
    return {}


def _text_value(facts: dict):
    if not facts:
        return ""
    text = str(facts.get("text") or "").strip()
    return text


def conflict_state(item: str, run_dir: str) -> dict | None:
    """识别「权威 vs 派生」冲突并给出当前磁盘状态（不改写任何东西）。

    @returns None（识别不了）或
             {authority, authority_facts, authority_values, target, target_abs,
              resolved, item}
    """
    text = str(item or "")
    if not text:
        return None
    try:
        from harness import evidence as _ev
        cites = list(_ev.seen_citations(text))
    except Exception:                                            # noqa: BLE001
        cites = []
    policy = load_policy(run_dir)
    resolved = []
    for cite in cites:
        rel, abs_path = _resolve(run_dir, cite)
        if rel:
            resolved.append((rel, abs_path))
    if not resolved:
        return None

    authorities = [(rel, ap) for rel, ap in resolved if is_authoritative(rel, policy)]
    if len(authorities) > 1:
        return None                                             # 多权威互斥，交 E_POLICY
    if not authorities:
        return None
    authority_rel, _ = authorities[0]
    authority_facts = facts_for(run_dir, authority_rel)
    if not authority_facts:
        return None

    # 目标：优先选 blocker 里点名的可写文件；否则选策略里唯一的可写文件
    #（排除 doc/ 这类通用草稿目录）。
    targets = [(rel, ap) for rel, ap in resolved
               if is_writable(rel, policy) and rel != authority_rel]
    if not targets:
        candidates = []
        root = Path(str(run_dir or ""))
        for rel in policy.get("writable") or []:
            if rel.startswith("doc/"):
                continue
            abs_path = root / rel
            if abs_path.is_file():
                candidates.append((rel, str(abs_path)))
            elif abs_path.is_dir():
                for fp in sorted(abs_path.rglob("*")):
                    if fp.is_file():
                        candidates.append((fp.relative_to(root).as_posix(), str(fp)))
        targets = candidates
    if len(targets) != 1:
        return None
    target_rel, target_abs = targets[0]
    target_text = Path(target_abs).read_text(encoding="utf-8", errors="replace")

    values = _primary_values(text, authority_facts)
    if values:
        authority_values = {k: str(v) for k, v in values.items()}
        expected = set(authority_values.values())
    else:
        text_value = _text_value(authority_facts)
        if not text_value:
            return None
        expected = _numbers(text_value)
        authority_values = {"text": text_value}
        if not expected:
            return None
    target_numbers = _numbers(target_text)
    resolved = not bool(expected - target_numbers)

    pairs = "；".join("%s=%s" % (k, v) for k, v in authority_values.items())
    rewritten = (
        "权威 %s 的实际值为 %s（只读，不得修改）；可写目标 %s 当前内容为 %s，"
        "两者不一致，必须把可写目标改为与权威一致。只允许修改 %s，"
        "不得修改 input/ 或任何受保护产物。"
        % (authority_rel, pairs, target_rel, json.dumps(target_text.strip(), ensure_ascii=False),
           target_rel)
    )
    return {
        "authority": authority_rel,
        "authority_facts": authority_facts,
        "authority_values": authority_values,
        "target": target_rel,
        "target_abs": target_abs,
        "resolved": resolved,
        "item": rewritten,
    }


def canonical_conflict(item: str, run_dir: str) -> dict | None:
    """把「权威 vs 派生」的未解决冲突改写成磁盘事实。

    @returns None（不是冲突 / 已经一致）或
             {authority, authority_facts, authority_values, target, target_abs,
              resolved, item}
    """
    state = conflict_state(item, run_dir)
    if not state or state.get("resolved"):
        return None
    return state


def conflict_resolved(item: str, run_dir: str) -> bool:
    """冲突是否已经被磁盘事实解决（派生侧已含权威值）。"""
    state = conflict_state(item, run_dir)
    return bool(state and state.get("resolved"))
