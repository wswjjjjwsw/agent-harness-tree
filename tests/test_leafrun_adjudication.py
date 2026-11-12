"""Scheduler pause, receipt, and original-node resume behavior."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import audit, driver, final_audit, leafrun, node, recover


class _DriverStub:
    def __init__(self, scheduler, status="accepted", specs=None):
        self.scheduler = scheduler
        self.status = status
        self.specs = specs or []
        self.outcomes = []

    def dispatch_sage_review(self, owner, issue):
        review_id = str(owner["id"]) + ".fix.review1.1"
        review = node.new_node(review_id, "审计", parent=owner["id"], state="完成")
        review["_review_verdict"] = {
            "verdict": "pass", "covers": ["b-1"],
            "evidence": [{"source": "e-1", "observation": "matches"}],
            "note": "reviewed",
        }
        self.scheduler.tree[review_id] = review
        return [review_id]

    def resume_adjudication(self, owner, outcome):
        self.outcomes.append(outcome)
        if self.status == "accepted":
            if owner["state"] == "待跑":
                node.change_state(owner, "在跑", self.scheduler.sink)
            node.change_state(owner, "完成", self.scheduler.sink)
        return {"status": self.status, "specs": self.specs}

    def _dispatch_repair_leaves(self, owner, specs, cycle=1):
        child_id = "%s.fix.audit%d.1" % (owner["id"], cycle)
        self.scheduler.tree[child_id] = node.new_node(
            child_id, "执行", parent=owner["id"], state="待跑")
        return [child_id]


class SchedulerAdjudicationTests(unittest.TestCase):
    def _scheduler(self, run_dir, max_steps=20, max_seconds=60):
        return leafrun.Scheduler(
            run_dir, "adjudication-test", {}, "", max_steps=max_steps,
            max_seconds=max_seconds, inner_runner=lambda *_args, **_kwargs: {},
        )

    def _root(self, state=node.WAIT_ADJUDICATION):
        return node.new_node("root", "根", state=state)

    def test_issue_pauses_original_then_resumes_from_structured_sage_review(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp)
            root = self._root("在跑")
            scheduler.tree["root"] = root
            driver = _DriverStub(scheduler)
            issue = {
                "id": "issue-1", "stage": "sage", "owner_id": "root",
                "candidate_sha256": "candidate-hash", "blockers": ["b-1"],
                "card": {"id": "issue-1"},
            }
            with patch.object(leafrun.Scheduler, "_driver", return_value=driver):
                scheduler._begin_adjudication(root, issue)
                self.assertEqual(root["state"], node.WAIT_ADJUDICATION)
                self.assertFalse(scheduler.candidate(root))
                changed, pending, _user = scheduler._poll_adjudications()

            self.assertTrue(changed)
            self.assertFalse(pending)
            self.assertEqual(root["state"], "完成")
            self.assertEqual(root["_adjudication"]["phase"], "applied")
            self.assertEqual(driver.outcomes[0]["source"], "sage")
            self.assertEqual(driver.outcomes[0]["issue_id"], "issue-1")
            self.assertEqual(driver.outcomes[0]["candidate_sha256"], "candidate-hash")
            self.assertNotIn("reviews", driver.outcomes[0])

    def test_rework_dispatches_existing_specs_and_waits_for_children(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp)
            root = self._root("在跑")
            scheduler.tree["root"] = root
            driver = _DriverStub(scheduler, status="rework",
                                 specs=[{"target_abs": "/tmp/doc.md", "chars_min": 10}])
            issue = {"id": "issue-1", "stage": "sage", "owner_id": "root",
                     "candidate_sha256": "candidate-hash", "blockers": ["b-1"]}
            with patch.object(leafrun.Scheduler, "_driver", return_value=driver):
                scheduler._begin_adjudication(root, issue)
                changed, pending, _user = scheduler._poll_adjudications()

            self.assertTrue(changed)
            self.assertFalse(pending)
            self.assertEqual(root["state"], node.WAIT_CHILDREN)
            self.assertEqual(root["_adjudication"]["phase"], "rework_dispatched")
            self.assertEqual(root["_audit_repair_rounds"], 1)
            self.assertIn("root.fix.audit1.1", scheduler.tree)
            self.assertEqual(driver.outcomes[0]["source"], "sage")

            driver.status = "accepted"
            scheduler.tree["root.fix.audit1.1"]["state"] = "完成"
            with patch.object(leafrun.Scheduler, "_driver", return_value=driver):
                changed, pending, _user = scheduler._poll_adjudications()

            self.assertTrue(changed)
            self.assertFalse(pending)
            self.assertEqual(root["state"], "完成")
            self.assertEqual([item["source"] for item in driver.outcomes],
                             ["sage", "repair"])
            self.assertEqual(driver.outcomes[1]["repair_node_ids"],
                             ["root.fix.audit1.1"])

    def test_missing_audit_keeps_merge_and_rechecks_before_sage(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp)
            root = self._root("在跑")
            root["merge"] = {"去向": "收束", "reply": "candidate"}
            root["_merge_continuation"] = {
                "issue_id": "issue-1", "candidate_sha256": "candidate-hash"}
            leaf = node.new_node("root.c1", "调研", parent="root",
                                 state="完成", batch=1)
            scheduler.tree.update({"root": root, "root.c1": leaf})
            driver = _DriverStub(scheduler)
            issue = {"id": "issue-1", "stage": "sage", "owner_id": "root",
                     "candidate_sha256": "candidate-hash"}
            with patch.object(leafrun.Scheduler, "_driver", return_value=driver):
                queued = scheduler._queue_audit_backfill(["root.c1"], issue=issue)
                self.assertEqual(root["state"], node.WAIT_CHILDREN)
                self.assertEqual(root["_adjudication"]["phase"], "after_audit")
                self.assertEqual(root["merge"]["reply"], "candidate")
                audit_id = queued[0]
                scheduler.tree[audit_id]["state"] = "完成"
                changed, pending, _user = scheduler._poll_adjudications()

            self.assertTrue(changed)
            self.assertFalse(pending)
            self.assertEqual(root["state"], "完成")
            self.assertEqual(driver.outcomes[0]["source"], "audit")
            self.assertEqual(driver.outcomes[0]["audit_node_ids"], [audit_id])

    def test_resume_audit_backfill_preserves_saved_issue(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp)
            root = self._root("在跑")
            root["merge"] = {"去向": "收束", "reply": "candidate"}
            root["_merge_continuation"] = {
                "issue_id": "issue-1", "candidate_sha256": "candidate-hash"}
            root["_adjudication"] = {
                "phase": "sage", "issue_id": "issue-1",
                "candidate_sha256": "candidate-hash",
                "issue": {"id": "issue-1", "blockers": [{"id": "b-1"}]},
            }
            leaf = node.new_node("root.c1", "调研", parent="root",
                                 state="完成", batch=1)
            scheduler.tree.update({"root": root, "root.c1": leaf})

            queued = scheduler._queue_audit_backfill(["root.c1"])

            self.assertEqual(root["_adjudication"]["phase"], "after_audit")
            self.assertEqual(root["_adjudication"]["issue"]["blockers"], [{"id": "b-1"}])
            self.assertEqual(root["_adjudication"]["audit_node_ids"], queued)

    def test_prepared_receipt_without_live_flag_returns_waiting_without_loop(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp, max_seconds=3600)
            scheduler.tree["root"] = self._root()
            scheduler.tree["root"]["_adjudication"] = {
                "phase": "leo", "issue_id": "issue-1",
                "candidate_sha256": "candidate-hash", "card_sha256": "card-hash",
                "issue": {"id": "issue-1", "card": {"id": "issue-1"}},
                "receipt_dir": temp,
            }
            calls = []

            def start_or_read(card, receipt_dir, live=False):
                calls.append(live)
                return {"status": "pending", "phase": "prepared"}

            with patch.dict("os.environ", {"TREE_ADJUDICATION_LIVE": ""}), \
                 patch("harness.adjudication.start_or_read", side_effect=start_or_read):
                result = scheduler.run()

            self.assertTrue(scheduler.waiting)
            self.assertIn("TREE_ADJUDICATION_LIVE=1", result["stop_reason"])
            self.assertFalse(any(calls))
            self.assertEqual(scheduler.step_count, 0)

    def test_live_submit_is_precheckpointed_and_polling_does_not_consume_steps(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp)
            root = self._root()
            scheduler.tree["root"] = root
            card = {"id": "issue-1", "question": "question"}
            meta = {"phase": "leo", "issue_id": "issue-1",
                    "candidate_sha256": "candidate-hash",
                    "issue": {"id": "issue-1", "card": card},
                    "card_sha256": "card-hash", "receipt_dir": temp}
            root["_adjudication"] = meta
            calls = []

            def start_or_read(_card, receipt_dir, live=False):
                calls.append(live)
                if live:
                    saved = recover.load_checkpoint(Path(temp) / "state.json")
                    saved_root = saved["nodes"]["root"]
                    self.assertEqual(saved_root["state"], node.WAIT_ADJUDICATION)
                    self.assertEqual(saved_root["_adjudication"]["phase"], "leo")
                    self.assertEqual(saved_root["_adjudication"]["issue"]["card"]["id"],
                                     "issue-1")
                    self.assertNotIn("card", saved_root["_adjudication"])
                    return {"status": "pending", "phase": "running", "worker_pid": 42}
                if any(calls[:-1]):
                    return {"status": "pending", "phase": "running", "worker_pid": 42}
                return {"status": "pending", "phase": "prepared"}

            with patch.dict("os.environ", {"TREE_ADJUDICATION_LIVE": "1"}), \
                 patch("harness.adjudication.start_or_read", side_effect=start_or_read):
                with patch.object(leafrun.Scheduler, "_driver", return_value=_DriverStub(scheduler)):
                    changed, pending, _user = scheduler._poll_adjudications()
                    self.assertFalse(changed)
                    self.assertTrue(pending)
                    first_steps = scheduler.step_count
                    changed, pending, _user = scheduler._poll_adjudications()

            self.assertFalse(changed)
            self.assertTrue(pending)
            self.assertEqual(scheduler.step_count, first_steps)
            self.assertEqual(calls, [False, True, False])
            kinds = [entry["kind"] for entry in scheduler.arbitrator.entries]
            self.assertIn("escalate_requested", kinds)
            self.assertIn("escalate_sent", kinds)

    def test_completed_receipt_is_applied_even_at_step_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp, max_steps=0)
            root = self._root()
            scheduler.tree["root"] = root
            root["_merge_continuation"] = {"candidate_sha256": "candidate-hash"}
            root["_adjudication"] = {
                "phase": "leo", "issue_id": "issue-1",
                "candidate_sha256": "candidate-hash", "card_sha256": "card-hash",
                "issue": {"id": "issue-1", "card": {"id": "issue-1"}},
                "receipt_dir": temp,
            }
            driver = _DriverStub(scheduler)

            with patch("harness.adjudication.start_or_read", return_value={
                    "status": "done", "verdict": "pass", "blocking": False,
                    "note": "approved", "missing": [], "requires_user_approval": False,
                    "raw_receipt": str(Path(temp) / "raw.json")}):
                with patch.object(leafrun.Scheduler, "_driver", return_value=driver):
                    result = scheduler.run()

            self.assertEqual(result["stop_reason"], "全树节点皆终态")
            self.assertEqual(root["state"], "完成")
            self.assertEqual(driver.outcomes[0]["source"], "leo")
            self.assertEqual(driver.outcomes[0]["receipt"]["verdict"], "pass")

    def test_applied_leo_pass_checkpoint_is_persisted_and_projected_without_clearing_unresolved(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp)
            parent = node.new_node("root", "根", state=node.WAIT_CHILDREN, batch=1)
            owner = node.new_node("root.c1.1", "执行", parent="root",
                                  state=node.WAIT_ADJUDICATION, batch=1)
            merge = {
                "去向": "收束",
                "reply": "原始汇总仍包含同秒冲突。",
                "未决": [{"type": "冲突", "what": "同一秒内同一账户存在 ACTIVE 与 REVOKED 两种状态"}],
            }
            owner["merge"] = json.loads(json.dumps(merge, ensure_ascii=False))
            owner["reply"] = merge["reply"]
            owner["未决"] = merge["未决"]
            issue = {
                "id": "issue-1", "candidate_sha256": "candidate-hash",
                "blockers": [{"id": "b-1", "item": "同一秒内同一账户存在 ACTIVE 与 REVOKED 两种状态", "level": "blocker"}],
            }
            owner["_adjudication"] = {
                "phase": "leo", "applied": False, "issue_id": "issue-1",
                "candidate_sha256": "candidate-hash", "card_sha256": "card-hash",
                "issue": issue,
            }
            pending = node.new_node("root.c2", "执行", parent="root",
                                    state="完成", batch=1)
            pending["_adjudication"] = {
                "phase": "user", "applied": True, "last_source": "leo",
                "last_status": "escalate", "receipt": {"verdict": "escalate", "note": "待用户决定"},
            }
            upgraded = node.new_node("root.c3", "执行", parent="root",
                                     state="完成", batch=1)
            upgraded["_adjudication"] = {
                "phase": "applied", "applied": True, "last_source": "sage",
                "last_status": "accepted",
                "receipt_checkpoint": {"source": "sage", "receipt": {"verdict": "pass", "note": "复核通过"}},
            }
            scheduler.tree.update({"root": parent, owner["id"]: owner,
                                   pending["id"]: pending, upgraded["id"]: upgraded})
            outcome = {
                "source": "leo", "issue_id": "issue-1",
                "candidate_sha256": "candidate-hash", "card_sha256": "card-hash",
                "receipt": {"verdict": "pass", "note": "已按绑定 blocker 复核"},
            }
            with patch.object(leafrun.Scheduler, "_driver", return_value=_DriverStub(scheduler)):
                result = scheduler._resume_adjudication(
                    owner, owner["_adjudication"], outcome, "leo")

            self.assertEqual(result, (True, False, False))
            self.assertEqual(owner["merge"], merge)
            self.assertEqual(owner["reply"], merge["reply"])
            self.assertEqual(owner["未决"], merge["未决"])
            saved = recover.load_checkpoint(Path(temp) / "state.json")
            saved_nodes = saved["nodes"]
            saved_owner = saved_nodes[owner["id"]]
            self.assertEqual(saved_owner["_adjudication"]["receipt_checkpoint"], outcome)

            expected = {
                "source": "leo", "verdict": "pass", "note": "已按绑定 blocker 复核",
                "binding": {
                    "issue_id": "issue-1", "candidate_sha256": "candidate-hash",
                    "card_sha256": "card-hash",
                    "blockers": [{"id": "b-1", "item": "同一秒内同一账户存在 ACTIVE 与 REVOKED 两种状态", "level": "blocker"}],
                },
            }
            batch = json.loads(driver.batch_view(saved_nodes[parent["id"]], saved_nodes))["子节点"]
            batch_rows = {row["id"]: row for row in batch}
            self.assertEqual(batch_rows[owner["id"]]["已应用裁决"], expected)
            self.assertNotIn("已应用裁决", batch_rows[pending["id"]])
            self.assertNotIn("已应用裁决", batch_rows[upgraded["id"]])

            full_tree = json.loads(audit.tree_view(saved_nodes))["全树"]
            tree_rows = {row["id"]: row for row in full_tree}
            self.assertEqual(tree_rows[owner["id"]]["已应用裁决"], expected)
            self.assertNotIn("已应用裁决", tree_rows[pending["id"]])
            self.assertNotIn("已应用裁决", tree_rows[upgraded["id"]])
            self.assertIn("相同范围且证据未变时", final_audit.PROMPT_EXTRA_FINAL_AUDIT)
            self.assertIn("不自动豁免问题", final_audit.PROMPT_EXTRA_FINAL_AUDIT)


if __name__ == "__main__":
    unittest.main()
