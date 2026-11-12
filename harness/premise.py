"""关键逻辑前提台账 + 证伪传播（V2.1 P2，设计 §2.2 / GPT 补充消息 680）。

前提被证伪时：记录可追溯依赖与受影响范围，把相关结论标 suspect/invalidated，
最近有足够上下文的上级改分工/撤销错误中间分支并重新规划受影响子树。

★ 撤销 ≠ 删除证据：产物与证据留在磁盘，只改可信性标记（trust.jsonl 的
  trust_status）。兄弟分支不受影响。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

PREMISES_NAME = "premises.jsonl"

# 未决 item 里“前提被证伪”的机械标记：type=冲突 且 what 以此前缀开头。
PREMISE_FALSIFIED_MARKER = "前提被证伪"

VALID = "valid"
SUSPECT = "suspect"
INVALIDATED = "invalidated"


def record_premise(run_dir: str, premise_id: str, statement: str,
                   source_node: str, dependents: list) -> dict:
    """登记一条前提及其依赖节点（依赖 = 结论/产物直接建立在该前提上的节点 id）。"""
    e = {"premise_id": str(premise_id or ""), "statement": str(statement or "")[:500],
         "source_node": str(source_node or ""), "status": VALID,
         "dependents": [str(x) for x in (dependents or [])],
         "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    p = Path(run_dir) / PREMISES_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e


def register_premise(run_dir: str, premise_id: str, statement: str,
                     source_node: str, dependents: list) -> dict:
    """登记一条前提（幂等合并）：同一 premise_id 已存在时合并 dependents（去重保序），再追加一条最新记录。
    供 spawn 环节调用（生产者）：每轮 spawn 都调它，跨轮重派同一要点不会丢早轮的依赖。"""
    existing = read_premises(run_dir).get(str(premise_id)) or {}
    merged = [str(x) for x in (existing.get("dependents") or [])]
    for d in (dependents or []):
        if str(d) not in merged:
            merged.append(str(d))
    return record_premise(run_dir, premise_id, statement, source_node, merged)


def read_premises(run_dir: str) -> dict:
    """@returns: {premise_id: 最新一条记录}；文件不在 ⇒ {}。"""
    p = Path(run_dir) / PREMISES_NAME
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
        if isinstance(e, dict) and e.get("premise_id"):
            out[str(e["premise_id"])] = e
    return out


def find_premise_falsification(node: dict) -> dict | None:
    """从节点的未决[] 里找前提证伪标记。@returns: {premise_id, evidence} 或 None。

    标记格式：未决 item，type=冲突，what 以“前提被证伪”开头，
    后接 premise_id（形如“前提被证伪：P-xxx：<依据>”）。
    """
    for it in (node or {}).get("未决") or []:
        if not isinstance(it, dict):
            continue
        if it.get("type") != "冲突":
            continue
        what = str(it.get("what") or "").strip()
        if not what.startswith(PREMISE_FALSIFIED_MARKER):
            continue
        rest = what[len(PREMISE_FALSIFIED_MARKER):].strip("：: ").strip()
        pid = rest.split("：")[0].split(":")[0].strip() if rest else ""
        return {"premise_id": pid or "(未命名前提）",
                "evidence": str(it.get("why") or "")[:500],
                "what": what}
    return None


def falsify_premise(run_dir: str, tree: dict, premise_id: str, evidence: str,
                    source_node: str = "") -> dict:
    """证伪一条前提：依赖节点的可信性标记 → suspect/invalidated，产物保留。

    标记规则（机械，不读语义）：依赖节点中 result_type=failed 的标 invalidated，
    其余标 suspect。受影响范围 = 登记的 dependents；未登记时回落为报告叶的父节点
    同批成员（同一计划/前提下派出的子树）。
    @returns: {premise_id, status, affected:[node_id], marked:{nid:status}}。
    """
    from harness import trust as trust_mod
    premises = read_premises(run_dir)
    rec = premises.get(str(premise_id)) or {}
    dependents = list(rec.get("dependents") or [])
    # 未登记依赖：只标报告叶自身（不把父节点同批无关兄弟拉进来；要传播须先登记依赖）。
    if not dependents and source_node:
        dependents = [str(source_node)]
    trust = trust_mod.read_trust(str(run_dir))
    marked = {}
    for nid in dependents:
        e = trust.get(nid)
        if not isinstance(e, dict):
            marked[nid] = "（无 trust 记录，未标）"
            continue
        new_status = INVALIDATED if e.get("result_type") == "failed" else SUSPECT
        e["trust_status"] = new_status
        marked[nid] = new_status
    if marked:
        # 回写 trust.jsonl（整文件重写：条目数小，幂等；产物文件本身不动）。
        p = Path(run_dir) / trust_mod.TRUST_NAME
        with open(p, "w", encoding="utf-8") as fh:
            for nid in sorted(trust):
                fh.write(json.dumps(trust[nid], ensure_ascii=False) + "\n")
    # affected 只含实际改写 trust_status 的节点（无 trust 记录的只记 marked，不进 affected）。
    e = {"premise_id": str(premise_id), "statement": str(rec.get("statement") or "")[:500],
         "source_node": str(rec.get("source_node") or ""), "status": "falsified",
         "dependents": dependents, "evidence": str(evidence or "")[:500],
         "affected": sorted(n for n, s in marked.items() if s in (SUSPECT, INVALIDATED)),
         "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    pp = Path(run_dir) / PREMISES_NAME
    pp.parent.mkdir(parents=True, exist_ok=True)
    with open(pp, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return {"premise_id": str(premise_id), "status": "falsified",
            "affected": sorted(n for n, s in marked.items() if s in (SUSPECT, INVALIDATED)),
            "marked": marked}
