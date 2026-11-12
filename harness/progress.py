"""逐叶进度事件（t-f6f9d7325d93 A：进度实时化）。

职责只有两件：
  1. 每叶终结后向 <run>/progress.jsonl 追加一行结构化事件（node id / 起止 /
     耗时 / complete-partial-failed / 证据与工件路径）。证据/partial 先落盘，
     事件后发（run_leaf 里调用点在落终态之后）。
  2. 尽力把最新进度写进 bridge tasks.progress（Codex 轮询 muse_get_task 可见），
     让调用方在全树结束前看到进度。bridge 不可写时只记 progress.jsonl，
     绝不因此失败本叶；函数永不抛异常（调用方也包了 try）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

PROGRESS_NAME = "progress.jsonl"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def emit(run_dir: str, event: dict) -> dict:
    """向 <run>/progress.jsonl 追加一行。@returns: 记下的 event（含 ts）。"""
    e = dict(event or {})
    e.setdefault("ts", _now_iso())
    p = Path(run_dir) / PROGRESS_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e


def push_bridge(progress: dict) -> bool:
    """尽力更新 bridge tasks.progress + heartbeat_at。@returns: 是否写成功。"""
    task_id = os.environ.get("TREE_BRIDGE_TASK_ID") or ""
    if not task_id:
        return False
    db = os.environ.get("TREE_BRIDGE_DB") or str(
        Path.home() / "mini" / "hybrid-reasoning-v2" / "kiki_bridge.db")
    try:
        conn = sqlite3.connect(db, timeout=5.0)
        try:
            cur = conn.execute(
                "UPDATE tasks SET progress=?, heartbeat_at=datetime('now') "
                "WHERE task_id=? AND status='running'",
                (json.dumps(progress, ensure_ascii=False), task_id))
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()
    except Exception:
        return False


def emit_leaf_progress(run_dir: str, run_id: str, node: dict, row: dict, sid: str) -> dict:
    """叶终结事件：证据/partial 已落盘后调用。"""
    from harness import contract as _ct
    from harness import evidence as _ev
    from harness import session as _sess
    nid = str((node or {}).get("id") or "")
    tools = {"calls": 0, "successes": 0, "failures": 0}
    sess_path = ""
    try:
        sess_path = _sess.find_session_file(
            sid, os.environ.get("TREE_PI_AGENT_SESSION_DIR")) or ""
        if sess_path:
            tools = _ev.count_tool_results(sess_path)
    except Exception:
        pass
    artifacts = []
    try:
        _p = Path(run_dir) / "partial" / ("%s.json" % nid)
        if _p.is_file():
            artifacts.append("partial/%s.json" % nid)
        _led = _ct.read_ledger(str(run_dir)).get(nid) or {}
        if _led.get("session_path"):
            artifacts.append(str(_led.get("session_path")))
    except Exception:
        pass
    ev = {
        "event": "leaf_terminal",
        "run_id": str(run_id or ""),
        "node_id": nid,
        "kind": _ct.classify(node),
        "result_type": str(row.get("result_type") or ""),
        "state": str((node or {}).get("state") or ""),
        "start": row.get("start"),
        "end": row.get("end"),
        "seconds": row.get("seconds"),
        "tools": tools,
        "session_id": str(sid or ""),
        "session_path": sess_path,
        "artifacts": artifacts,
        "unresolved": list((node or {}).get("未决") or []),
    }
    emit(run_dir, ev)
    # bridge 进度（紧凑版，供 muse_get_task 轮询）。
    try:
        _n = 0
        _pp = Path(run_dir) / PROGRESS_NAME
        if _pp.is_file():
            _n = sum(1 for _l in _pp.read_text(encoding="utf-8").splitlines() if _l.strip())
        push_bridge({
            "phase": "dryrun" if _ct.is_dryrun() else "e2e",
            "run_id": str(run_id or ""),
            "leaves_done": _n,
            "last_leaf": {"node_id": nid, "result_type": ev["result_type"],
                          "seconds": ev["seconds"],
                          "tools_successes": tools.get("successes", 0)},
            "updated": _now_iso(),
        })
    except Exception:
        pass
    return ev
