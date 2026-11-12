"""统一恢复控制器（t-64e27d836932 系统架构主任务）。

职责：按 failure class 选择**新策略**（不是原样重试），记录每条 recovery edge
（cause / 输入 / 旧策略 / 新策略 / 验收），防止无穷恢复循环与共享 Sage 的
不受控并发（Sage inference 本身由 harness/session.py 的 SerialLock 串行排队；
本控制器只决定“派什么”，不并发派）。

edge 记录落 <run>/recovery.jsonl，每行：
  {parent_id, node_id, failure_class, cause, old_fingerprint, new_strategy,
   new_fingerprint, outcome, ts}
outcome ∈ dispatched / exhausted / fallback / partial_delivered。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from harness import contract as contract_mod

RECOVERY_LOG = "recovery.jsonl"

# 同一（父节点，失败类）最多换 2 个不同策略；耗尽后父节点交付 partial + unresolved。
MAX_DISTINCT_STRATEGIES = 2


class RecoveryController:
    def __init__(self, run_dir: str):
        self.run_dir = str(run_dir or "")
        self.log_path = str(Path(self.run_dir) / RECOVERY_LOG) if self.run_dir else ""
        self.edges = []

    # ---- 记账 ---------------------------------------------------------------
    def record(self, parent_id: str, node_id: str, failure_class: str, cause: str,
               old_strategy: dict | None, new_strategy: dict | None,
               outcome: str) -> dict:
        edge = {
            "parent_id": str(parent_id or ""),
            "node_id": str(node_id or ""),
            "failure_class": str(failure_class or "unknown"),
            "cause": str(cause or "")[:500],
            "old_fingerprint": contract_mod.action_fingerprint(old_strategy) if old_strategy else "",
            "new_strategy": dict(new_strategy or {}),
            "new_fingerprint": contract_mod.action_fingerprint(new_strategy) if new_strategy else "",
            "outcome": str(outcome),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.edges.append(edge)
        if self.log_path:
            Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(edge, ensure_ascii=False) + "\n")
        return edge

    def attempts(self, parent_id: str, failure_class: str) -> int:
        """该（父，失败类）已派出的不同策略数。"""
        seen = set()
        for e in self.edges:
            if e.get("parent_id") == str(parent_id) and e.get("failure_class") == str(failure_class):
                if e.get("outcome") == "dispatched" and e.get("new_fingerprint"):
                    seen.add(e["new_fingerprint"])
        return len(seen)

    def used_fingerprints(self, parent_id: str, failure_class: str) -> set:
        out = set()
        for e in self.edges:
            if e.get("parent_id") == str(parent_id) and e.get("failure_class") == str(failure_class):
                if e.get("new_fingerprint"):
                    out.add(e["new_fingerprint"])
                if e.get("old_fingerprint"):
                    out.add(e["old_fingerprint"])
        return out

    # ---- 选策略 ---------------------------------------------------------------
    def choose(self, parent_id: str, node_id: str, failure_class: str,
               cause: str, old_strategy: dict | None = None) -> dict | None:
        """@returns: 新策略 dict，或 None（策略耗尽 ⇒ 父节点交付 partial + unresolved）。"""
        used = self.used_fingerprints(parent_id, failure_class)
        for st in contract_mod.strategies_for(failure_class):
            fp = contract_mod.action_fingerprint(st)
            if fp in used:
                continue
            if self.attempts(parent_id, failure_class) >= MAX_DISTINCT_STRATEGIES:
                break
            self.record(parent_id, node_id, failure_class, cause, old_strategy, st, "dispatched")
            return dict(st)
        self.record(parent_id, node_id, failure_class, cause, old_strategy, None, "exhausted")
        return None

    def mark_fallback(self, parent_id: str, node_id: str, failure_class: str,
                      cause: str, note: str) -> dict:
        """机械兜底（如 uncovered fallback）被启用时记一笔，来源可追溯。"""
        return self.record(parent_id, node_id, failure_class, cause + "｜" + note,
                           None, {"kind": "synthesis", "role": "执行",
                                  "tools": "read", "focus": "机械兜底：" + note},
                           "fallback")

    def mark_partial(self, parent_id: str, node_id: str, failure_class: str,
                     cause: str) -> dict:
        """策略耗尽、父节点交付 partial + unresolved 时记一笔。"""
        return self.record(parent_id, node_id, failure_class, cause, None, None,
                           "partial_delivered")
