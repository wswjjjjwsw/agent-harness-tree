"""可信成果台账 + 机械保底交付（V2.1 P0，t-5df0e8dd3660）。

问题（G1/G2）：证据台账只记结果类型/工具数/引用/未决，不记**产物**（doc 路径+哈希、
edits、partial 抢救文件）；最终答复只在 run 结束时生成一次，根失败时 out/ 可能为空，
已有可信产物无处可查。

设计：
  · trust.jsonl：每叶终结记一笔——node_id / kind / result_type / 执行健康
    （state + exit_class）/ products[{path, sha256, bytes, kind, source}] /
    tools / citations / unresolved。只追加，不改写；只记录已落盘文件（doc 经
    land.land_doc 原子写；partial 经 salvage；edits 经 edits.jsonl），不记录
    模型口头声称的东西。
  · out/保底交付.md：机械生成，只含已落盘可信产物 + 执行健康表 + 可信程度分级，
    **不含任何模型结论**。每叶终结重写一次（持续落盘：进程被杀、根失败时，已有
    产物仍在盘上可查）；run 结束时再写一次。无有效产物时明确写“零可信成果”，
    不虚假绿灯。
  · 执行健康 vs 成果可信程度严格分开：健康 = 叶执行状态（完成/失败/超时/熔断）；
    可信程度 = 产物是否有证据（工具成功数 + 来源可追溯）。失败叶也可以有部分可信
    产物；完成叶若判据是自然语言则只是“部分可信”，不是“已验证”。
  · 越权写不进账：record 只接受 run_dir 内的相对路径；run_dir 外的路径一律拒收。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

TRUST_NAME = "trust.jsonl"
DELIVERY_NAME = "保底交付.md"
ZERO_TRUST_LINE = "零可信成果"

# 产物相对路径白名单前缀（只认 run_dir 内的这三类落盘位置）。
_TRUSTED_PREFIXES = ("doc/", "partial/", "edits.jsonl")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel_to_run(run_dir: str, path: str) -> str | None:
    """绝对/相对路径 → run_dir 内相对路径；run_dir 外 ⇒ None（拒收）。"""
    try:
        rp = Path(path).resolve()
        base = Path(run_dir).resolve()
        rel = str(rp.relative_to(base))
    except (ValueError, OSError):
        return None
    if rel.startswith(".."):
        return None
    return rel


def _is_trusted_rel(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in _TRUSTED_PREFIXES)


def append_trust(run_dir: str, entry: dict) -> dict:
    """台账追加一笔（只追加）。@returns: 记下的 entry（含 ts）。"""
    e = dict(entry or {})
    e["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p = Path(run_dir) / TRUST_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e


def read_trust(run_dir: str) -> dict:
    """@returns: {node_id: 最新一条 entry}；文件不在 ⇒ {}（如实，不编）。"""
    p = Path(run_dir) / TRUST_NAME
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


def collect_products(run_dir: str, node_id: str, node: dict, row: dict) -> list:
    """收集该叶已落盘的产物（只认磁盘上的文件 + 节点 reply，不认模型自述）。

    @returns: [{path, sha256, bytes, kind, source}]；越权/盘外路径不进账。
    """
    products = []
    # doc：run_leaf 经 Driver.write_doc 落盘，row["doc"] 是 land 返回的绝对路径。
    doc_path = row.get("doc") or ""
    if doc_path:
        rel = _rel_to_run(run_dir, str(doc_path))
        if rel and _is_trusted_rel(rel):
            fp = Path(run_dir) / rel
            if fp.is_file():
                products.append({"path": rel, "sha256": _sha256_file(fp),
                                 "bytes": fp.stat().st_size, "kind": "doc",
                                 "source": "leaf doc（land 原子写）"})
    # partial：超时/熔断抢救文件 partial/<nid>.json。
    partial_path = Path(run_dir) / "partial" / ("%s.json" % node_id)
    if partial_path.is_file():
        products.append({"path": "partial/%s.json" % node_id,
                         "sha256": _sha256_file(partial_path),
                         "bytes": partial_path.stat().st_size, "kind": "partial",
                         "source": "salvage_partial_evidence（会话工具结果）"})
    # edits：edits.jsonl 里本叶的条目数（原样存档，不合并语义）。
    edits_path = Path(run_dir) / "edits.jsonl"
    if edits_path.is_file():
        try:
            n = 0
            for line in edits_path.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("node_id") == node_id:
                        n += 1
                except ValueError:
                    continue
            if n:
                products.append({"path": "edits.jsonl", "sha256": _sha256_file(edits_path),
                                 "bytes": edits_path.stat().st_size, "kind": "edits",
                                 "source": "leaf edits 原样存档（%d 条）" % n})
        except OSError:
            pass
    # reply：节点 reply 原文（字符数；无文件，来源为会话）。
    reply = str((node or {}).get("reply") or "")
    if reply.strip():
        products.append({"path": "", "sha256": hashlib.sha256(
            reply.encode("utf-8")).hexdigest(), "bytes": len(reply.encode("utf-8")),
                         "kind": "reply", "source": "leaf reply（节点字段，%d 字）" % len(reply)})
    return products


def trust_entry(node: dict, result_type: str, exit_class: str, products: list,
                tools: dict, session_id: str, citations: list, unresolved: list) -> dict:
    return {
        "node_id": str((node or {}).get("id") or ""),
        "kind": None,  # 由调用方填 contract.classify(node)
        "role": str((node or {}).get("role") or ""),
        "result_type": result_type,  # complete / partial / failed
        "health": {"state": str((node or {}).get("state") or ""),
                   "exit_class": str(exit_class or "")},
        "products": list(products or []),
        "tools": {"calls": int((tools or {}).get("calls") or 0),
                  "successes": int((tools or {}).get("successes") or 0),
                  "failures": int((tools or {}).get("failures") or 0)},
        "session_id": str(session_id or ""),
        "citations": list(citations or []),
        "unresolved": list(unresolved or []),
    }


def trust_level(entry: dict) -> str:
    """可信程度分级（机械规则，不读语义）：
    已验证 = complete 且有产物；部分可信 = partial 且有产物；未验证 = failed 或无产物。"""
    products = entry.get("products") or []
    if entry.get("result_type") == "complete" and products:
        return "已验证"
    if entry.get("result_type") == "partial" and products:
        return "部分可信"
    return "未验证"


def gen_guaranteed_delivery(tree: dict, run_dir, run_id: str) -> str:
    """机械生成 <run>/out/保底交付.md：只含已落盘可信产物 + 执行健康 + 可信程度。

    不含模型结论（结论见 out/最终答复.md，如已生成）；无有效产物时明确写
    “零可信成果”。可反复调用（每叶终结 + run 结束），幂等重写。
    """
    out_dir = Path(run_dir) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    trust = read_trust(str(run_dir))
    tree = tree or {}
    lines = ["# 保底交付（机械生成：只含已落盘可信产物，不含模型结论）", "",
             "run_id：%s" % run_id,
             "生成时间：%s（本地墙钟）" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "root.state：%s" % str((tree.get("root") or {}).get("state") or "（未知）"), "",
             "## 执行健康（叶执行状态，与成果可信程度分开）", ""]
    nids = sorted(trust, key=lambda x: str(x))
    if not nids:
        lines.append("（尚无叶终结记录）")
    for nid in nids:
        e = trust[nid]
        h = e.get("health") or {}
        t = e.get("tools") or {}
        lines.append("- %s：role=%s / state=%s / exit=%s / 结果类型=%s / 工具调用 %d 次 / 成功 %d / 失败 %d"
                     % (nid, e.get("role"), h.get("state"), h.get("exit_class"),
                        e.get("result_type"), t.get("calls", 0), t.get("successes", 0),
                        t.get("failures", 0)))
    lines += ["", "## 可信产物清单（路径 + sha256 + 字节数 + 来源，均来自磁盘）", ""]
    total_products = 0
    for nid in nids:
        e = trust[nid]
        prods = e.get("products") or []
        total_products += len(prods)
        lines.append("### %s（可信程度：%s）" % (nid, trust_level(e)))
        if not prods:
            lines.append("- （无产物）")
            continue
        for pr in prods:
            lines.append("- [%s] %s ｜ sha256=%s ｜ %d 字节 ｜ 来源：%s"
                         % (pr.get("kind"), pr.get("path") or "（节点 reply，无文件）",
                            (pr.get("sha256") or "")[:16], pr.get("bytes", 0),
                            pr.get("source") or ""))
    lines += ["", "## 未决（各叶 unresolved + root.merge.uncovered 逐条）", ""]
    still = []
    for nid in nids:
        for it in (trust[nid].get("unresolved") or []):
            if isinstance(it, dict):
                still.append("%s：%s ｜ %s" % (nid, it.get("what"), it.get("why")))
            else:
                still.append("%s：%s" % (nid, it))
    for u in (((tree.get("root") or {}).get("merge") or {}).get("uncovered") or []):
        if isinstance(u, dict):
            still.append("root.merge.uncovered：%s ｜ %s" % (u.get("what"), u.get("why")))
        else:
            still.append("root.merge.uncovered：%s" % u)
    if still:
        for i, s in enumerate(still, 1):
            lines.append("- %d. %s" % (i, s))
    else:
        lines.append("（无）")
    lines += ["", "## 结论状态", ""]
    if total_products == 0:
        lines.append(ZERO_TRUST_LINE + "：本 run 尚无任何已落盘可信产物（无 doc、无 partial、无 reply），"
                     "不得据此声称任何结论。")
    else:
        lines.append("可信产物共 %d 项（见上清单）。本文件不含结论；结论见 out/最终答复.md"
                     "（如已生成），未决见上节。" % total_products)
    p = out_dir / DELIVERY_NAME
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)
