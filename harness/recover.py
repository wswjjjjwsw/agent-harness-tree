"""启动 reconcile（**只做一次**）：76 §6.3 + task-def §2.4。

对 `state == 在跑` 的节点各查**一次** `GET /task/<llaat_id>`（用 harness/submit.get_task，不另写一套 HTTP）：
  · 对得上 ⇒ 继续
  · 对不上 ⇒ 按 LLAAT 侧真实状态**补落树**
  · 查不到（404 / 没这条）⇒ **标失败**

★ 多轮口径（76 §6.3 末段）：父节点除 state 外还要核对 `round` 与 `act` / `batch` ——
  否则重启会把「已派过 3 轮」的父当成第 1 轮重跑（两套历史打架，且 §4.6 已证模型会脑补）。
  真值来源（两条，按优先级）：LLAAT 任务记录的 `tags`（node_id/act/round/batch）→ 任务 `prompt` 里的
  `act=… / round=…` 标记 → 都没有 ⇒ 沿用快照值并**如实记一行**（不猜）。

★ 接口可指到桩：LLAAT 基址 = `TREE_LLAAT_URL`（tree-02 已有）+ 参数 url；
★ 启动开关：`TREE_RECONCILE_ON_START` 只认显式 `1`/`true`（缺省关）；开时由
  `maybe_reconcile_on_start()` 在 leafrun.main() 入口调一次 reconcile；未设
  `TREE_LLAAT_URL` 时跳过、不默认打生产 8903（submit.DEFAULT_URL 会落到
  127.0.0.1:8903，所以这里必须显式判空）。查不到 ⇒ 标失败（见 reconcile）；
  开关是否默认开是结构性取舍，由 Leo/GPT 定，本函数不擅自改默认行为。
  树内 state 快照目录 = `TREE_STATE_DIR`（缺省 = 树内 runs/<run_id>/）。单测用本地桩 HTTP 服务，不打生产 8903。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
from pathlib import Path

from harness import role as role_mod, submit

# LLAAT 的 state → 树内 state（76 §3.2 的三终态：等的是「终结」不是「完成」）
LLAAT_DONE_OK = ("完成",)
LLAAT_FAILED = ("失败",)
LLAAT_RUNNING = ("在跑", "待跑")

# 父节点的多轮字段（额外的核对项，76 §6.3 末段）
PARENT_FIELDS = ("round", "act", "batch")

CHECKPOINT_NODE_PRIVATE_FIELDS = frozenset({
    "_audit_voids", "_audit_blockers", "_audit_cap_blockers", "_audit_cap_reason",
    "_audit_cap_cid", "_audit_repair_rounds", "_audit_review_rounds",
    "_review_of", "_review_snapshot", "_merge_reject_notes", "_merge_rounds",
    "_merge_audit_active", "_merge_mechanical_next", "_merge_fp", "_replan_request",
    "_adjudication", "_merge_continuation", "_last_reject",
    "_mech_fallback_tried", "_review_verdict", "_structured_review",
    "_review_blocker_id", "_review_issue_id", "_review_evidence_ids",
    "_managed_doc",
})
CHECKPOINT_SCHEDULER_FIELDS = (
    "version", "step_count", "rejects", "repairs", "diagnoses", "sticky", "elapsed_seconds",
)


def state_dir(run_id: str = None) -> str:
    d = os.environ.get("TREE_STATE_DIR")
    if d:
        return d
    runs = os.environ.get("TREE_RUNS_DIR") or str(Path(role_mod.ROOT) / "runs")
    return str(Path(runs) / (run_id or "default"))


def snapshot_path(run_id: str = None, d: str = None) -> str:
    return str(Path(d or state_dir(run_id)) / "state.json")


def _snapshot_scheduler(scheduler: dict) -> dict:
    out = {key: scheduler[key] for key in CHECKPOINT_SCHEDULER_FIELDS
           if key in scheduler and key not in ("repairs", "diagnoses")}
    if "repairs" in scheduler:
        out["repairs"] = [[parent, field, child]
                          for (parent, field), child in sorted(scheduler["repairs"].items())]
    if "diagnoses" in scheduler:
        out["diagnoses"] = [list(pair) for pair in sorted(scheduler["diagnoses"])]
    return out


def _restore_scheduler(scheduler: dict) -> dict:
    if scheduler is None:
        return None
    out = {key: scheduler[key] for key in CHECKPOINT_SCHEDULER_FIELDS
           if key in scheduler and key not in ("repairs", "diagnoses")}
    if "repairs" in scheduler:
        out["repairs"] = {(row[0], row[1]): row[2] for row in scheduler["repairs"]}
    if "diagnoses" in scheduler:
        out["diagnoses"] = {tuple(pair) for pair in scheduler["diagnoses"]}
    return out


def dump_snapshot(tree: dict, run_id: str = None, d: str = None,
                  scheduler: dict = None) -> str:
    """Atomically write the tree checkpoint, retaining only known private control fields."""
    p = Path(snapshot_path(run_id, d))
    p.parent.mkdir(parents=True, exist_ok=True)
    nodes = {}
    for nid, n in (tree or {}).items():
        nodes[str(nid)] = {
            k: v for k, v in (n or {}).items()
            if not str(k).startswith("_") or k in CHECKPOINT_NODE_PRIVATE_FIELDS
        }
    checkpoint = {"nodes": nodes}
    if scheduler is not None:
        checkpoint["scheduler"] = _snapshot_scheduler(scheduler)
    fd, temp_name = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(checkpoint, stream, ensure_ascii=False, indent=1)
        os.replace(temp_name, p)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return str(p)


def load_checkpoint(path: str) -> dict:
    """Load legacy or scheduler-aware snapshots as {nodes, scheduler}."""
    p = Path(path)
    if not p.exists():
        return {"nodes": {}, "scheduler": None}
    data = json.loads(p.read_text(encoding="utf-8") or "{}")
    nodes = {str(k): dict(v or {}) for k, v in (data.get("nodes") or {}).items()}
    return {"nodes": nodes, "scheduler": _restore_scheduler(data.get("scheduler"))}


def load_snapshot(path: str) -> dict:
    """Read the nodes using the legacy {node_id: node} contract."""
    return load_checkpoint(path)["nodes"]


# ---------------------------------------------------------------- 真值取用
_RE_ACT = re.compile(r"act=([A-Za-z_]+)")
_RE_ROUND = re.compile(r"round=(\d+)")


def truth_from_task(task: dict) -> dict:
    """从 LLAAT 任务记录里取真值：state + 父的多轮字段（tags 优先，prompt 标记兜底）。"""
    task = task or {}
    t = {}
    tags = task.get("tags") or {}
    if isinstance(tags, dict):
        for k in ("node_id", "act", "round", "batch"):
            if tags.get(k) not in (None, ""):
                t[k] = tags[k]
    prompt = str(task.get("prompt") or "")
    if "act" not in t:
        m = _RE_ACT.search(prompt)
        if m:
            t["act"] = m.group(1)
    if "round" not in t:
        m = _RE_ROUND.search(prompt)
        if m:
            t["round"] = int(m.group(1))
    raw = str(task.get("state") or "")
    t["_llaat_state"] = raw
    if raw in LLAAT_DONE_OK and task.get("judge_ok"):
        t["state"] = "完成"
    elif raw in LLAAT_DONE_OK and not task.get("judge_ok"):
        t["state"] = "失败"
    elif raw == "failed":
        t["state"] = "失败"
    elif raw in ("queued", "running", "pending"):
        t["state"] = "在跑"
    return t


def _fetch_one(llaat_id: str, url: str = None, timeout: float = 10.0) -> tuple:
    """查一次 GET /task/<id>。@returns: (task dict 或 None, 取任务失败原因)。"""
    try:
        return submit.get_task(str(llaat_id), url=url, timeout=timeout), ""
    except urllib.error.HTTPError as exc:
        return None, "HTTP %s（查不到这条任务）" % exc.code
    except Exception as exc:                                 # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)


# ---------------------------------------------------------------- reconcile
def reconcile(tree: dict, url: str = None, d: str = None, run_id: str = None,
              sink: str = None, timeout: float = 10.0, nodes: dict = None) -> dict:
    """对 state==在跑 的节点各查一次，逐条记账到 <state dir>/recover.jsonl。

    @param tree 就地修正的树（{node_id: node}）；也可只在 nodes 上做（纯对账，不动树）
    @returns: {"rows": [...], "queries": N, "recover_log": path, "changed": N, "failed": N}
    """
    run_id = run_id or "default"
    d = d or state_dir(run_id)
    Path(d).mkdir(parents=True, exist_ok=True)
    log = Path(sink) if sink else Path(d) / "recover.jsonl"
    work = tree if tree is not None else {}
    snap = load_snapshot(snapshot_path(run_id, d))
    for nid, n in snap.items():                              # 快照里有些、树里没有 ⇒ 补进树（补落）
        work.setdefault(nid, n)

    rows, queries = [], 0
    for nid in sorted(work):
        n = work[nid] or {}
        if n.get("state") != "在跑":
            continue
        queries += 1
        llaat_id = str(n.get("llaat_id") or "")
        if not llaat_id:
            rows.append({"node_id": nid, "对得上的字段": [], "改了哪个字段": "state",
                         "从": n.get("state"), "到": "失败", "结论": "标失败",
                         "why": "state==在跑 但没有 llaat_id，无从查（不许静默跳过）"})
            n["state"] = "失败"
            n["judge_why"] = "reconcile：无 llaat_id"
            continue
        task, err = _fetch_one(llaat_id, url, timeout)
        if task is None:
            rows.append({"node_id": nid, "llaat_id": llaat_id, "对得上的字段": [],
                         "改了哪个字段": "state", "从": n.get("state"), "到": "失败",
                         "结论": "标失败", "why": err})
            n["state"], n["judge_why"] = "失败", "reconcile：%s" % err
            continue
        t = truth_from_task(task)
        changed, matched = [], []
        # ① 状态
        if t.get("state") and t["state"] != n.get("state"):
            changed.append({"field": "state", "from": n.get("state"), "to": t["state"]})
            n["state"] = t["state"]
        else:
            matched.append("state")
        # ② LLAAT 侧判据结论（补落字段）
        if task.get("judge_ok") is not None and n.get("judge_ok") != bool(task.get("judge_ok")):
            changed.append({"field": "judge_ok", "from": n.get("judge_ok"), "to": bool(task["judge_ok"])})
            n["judge_ok"] = bool(task.get("judge_ok"))
            n["judge_why"] = str(task.get("judge_why") or "")
        elif task.get("judge_ok") is not None:
            matched.append("judge_ok")
        # ③ 父的多轮字段（round / act / batch，76 §6.3）
        if nid in snap:
            for f in PARENT_FIELDS:
                if f not in t:
                    continue
                old = n.get(f)
                new = int(t[f]) if f in ("round", "batch") and str(t[f]).isdigit() else t[f]
                if old != new:
                    changed.append({"field": f, "from": old, "to": new})
                    n[f] = new
                else:
                    matched.append(f)
        concl = "继续" if not changed else "补落"
        rows.append({"node_id": nid, "llaat_id": llaat_id, "对得上的字段": matched,
                     "改了哪个字段": ", ".join(c["field"] for c in changed) or "",
                     "从": ", ".join(str(c["from"]) for c in changed),
                     "到": ", ".join(str(c["to"]) for c in changed),
                     "变更明细": changed, "结论": concl,
                     "llaat_state": t.get("_llaat_state")})

    with open(log, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"rows": rows, "queries": queries, "recover_log": str(log),
            "changed": len([r for r in rows if r.get("变更明细")]),
            "failed": len([r for r in rows if r.get("结论") == "标失败"])}

# ---------------------------------------------------------------- 启动开关接线
def maybe_reconcile_on_start(run_id: str = None, d: str = None) -> dict:
    """启动时可选的一次 reconcile（默认关；只认 TREE_RECONCILE_ON_START=1/true）。

    开时：load snapshot → reconcile(url=TREE_LLAAT_URL) → dump snapshot 回写；
    全程记账到 <state dir>/recover.jsonl（reconcile 自带逐节点行；无 URL /
    异常时另记一行「跳过」）。关时零行为变化（不读快照、不建 recover.jsonl、
    不发任何 HTTP 请求）。

    @param run_id 缺省 "default"；@param d 状态目录（缺省 state_dir(run_id)）。
    @returns: {"enabled": bool, "skipped": "no_url"/"error"（仅跳过时）,
               "queries"/"changed"/"failed"/"recover_log"（对账时同 reconcile）}。
    注意失败语义：LLAAT 查不到的任务会被标「失败」——开关开时该语义生效，
    开关关时零行为变化。
    """
    run_id = run_id or "default"
    d = str(d or state_dir(run_id))
    if str(os.environ.get("TREE_RECONCILE_ON_START") or "").strip().lower() not in ("1", "true"):
        return {"enabled": False, "reason": "off", "queries": 0, "changed": 0, "failed": 0}
    url = str(os.environ.get("TREE_LLAAT_URL") or "").strip()
    log = Path(d) / "recover.jsonl"
    if not url:
        # 无 URL：不默认打生产（submit.DEFAULT_URL 会落到 127.0.0.1:8903），只记一行并跳过。
        Path(d).mkdir(parents=True, exist_ok=True)
        row = {"node_id": "", "结论": "跳过",
               "why": "TREE_RECONCILE_ON_START 已开但未设 TREE_LLAAT_URL：不默认打生产 8903，跳过本次启动对账"}
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return {"enabled": True, "skipped": "no_url", "reason": "未设 TREE_LLAAT_URL",
                "queries": 0, "changed": 0, "failed": 0, "recover_log": str(log)}
    try:
        checkpoint = load_checkpoint(snapshot_path(run_id, d))
        tree = checkpoint["nodes"]
        res = reconcile(tree, url=url, d=d, run_id=run_id)
        dump_snapshot(tree, d=d, scheduler=checkpoint["scheduler"])
        res = dict(res)
        res["enabled"] = True
        return res
    except Exception as exc:                                 # noqa: BLE001
        # 对账本身失败也不炸启动：记一行，启动继续。
        Path(d).mkdir(parents=True, exist_ok=True)
        row = {"node_id": "", "结论": "跳过",
               "why": "启动对账异常：%s: %s" % (type(exc).__name__, exc)}
        try:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:                                     # noqa: BLE001
            pass
        return {"enabled": True, "skipped": "error", "reason": str(exc),
                "queries": 0, "changed": 0, "failed": 0, "recover_log": str(log)}


def read_recover(path: str) -> list:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
