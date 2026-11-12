"""Structured Sage review output is validated once at the leaf boundary."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import leafrun, node, role


class _DriverStub:
    def __init__(self, run_dir, output):
        self.run_dir = str(run_dir)
        self.run_id = "review-test"
        self.table = role.load_roles()
        self.sink = str(Path(run_dir) / "violations.jsonl")
        self.runner_kind = "fake"
        self.tree = {}
        self.runner = lambda *_args, **_kwargs: json.dumps(output, ensure_ascii=False)

    def write_doc(self, *_args):
        raise AssertionError("read-only review must not write a doc")


class StructuredReviewTests(unittest.TestCase):
    def _node(self, temp, blocker_id="b-1"):
        review = node.new_node(
            "root.fix.review1.1", "审计", parent="root",
            judge={"json_keys": ["reply", "未决"]},
            overrides={"node_kind": "audit", "input_evidence_ids": ["evidence-1"]},
        )
        review["_structured_review"] = True
        review["_review_blocker_id"] = blocker_id
        review["_review_issue_id"] = "issue-1"
        review["_review_evidence_ids"] = ["evidence-1"]
        return review

    def _run(self, temp, output, blocker_id="b-1"):
        driver = _DriverStub(temp, output)
        driver.tree = {"root": node.new_node("root", "根")}
        review = self._node(temp, blocker_id)
        driver.tree[review["id"]] = review
        with patch("harness.evidence.evidence_gate", return_value=(True, "")):
            row = leafrun.run_leaf(driver, review)
        return review, row

    def test_incremental_audit_binds_original_input_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            driver = _DriverStub(temp, {})
            driver.log = lambda _message: None
            driver.sticky = ""
            root = node.new_node("root", "根")
            leaf = node.new_node(
                "root.c2.1", "执行", parent="root",
                overrides={"node_kind": "synthesis",
                           "input_evidence_ids": ["input/audit-data/events.psv"]})
            driver.tree = {"root": root, leaf["id"]: leaf}
            with patch("harness.contract.classify", return_value="synthesis"), \
                    patch("harness.contract.read_ledger", return_value={}):
                audit_id = leafrun.Scheduler._queue_leaf_audit(
                    driver, leaf["id"], leaf, {"result_type": "complete"})
            audit = driver.tree[audit_id]
            self.assertIn("input/audit-data/events.psv", audit["context"])
            self.assertIn("input/audit-data/events.psv",
                          audit["overrides"]["input_evidence_ids"])

    def test_valid_review_is_structured_and_keeps_unresolved_items(self):
        with tempfile.TemporaryDirectory() as temp:
            unresolved = [{"type": "缺信息", "what": "open question", "why": "source absent"}]
            review, row = self._run(temp, {
                "reply": "已按证据复核",
                "未决": unresolved,
                "verdict": "pass",
                "covers": ["b-1"],
                "evidence": [{"source": "evidence-1", "observation": "matches source"}],
                "note": "覆盖该争议",
            })

            self.assertEqual(row["state"], "完成")
            self.assertEqual(review["_review_verdict"], {
                "verdict": "pass",
                "covers": ["b-1"],
                "evidence": [{"source": "evidence-1", "observation": "matches source"}],
                "note": "覆盖该争议",
            })
            self.assertEqual(review["未决"], unresolved)

    def test_covers_allow_one_outer_display_pair_and_normalize_to_ledger_id(self):
        cases = (("[b-1]", "b-1", "[b-1]"), ("b-1", "[b-1]", "b-1"))
        for verdict in ("pass", "rework", "escalate"):
            for blocker_id, reported_cover, normalized in cases:
                with self.subTest(verdict=verdict, blocker_id=blocker_id), \
                        tempfile.TemporaryDirectory() as temp:
                    review, row = self._run(temp, {
                        "reply": "已按绑定证据复核",
                        "未决": [],
                        "verdict": verdict,
                        "covers": [reported_cover],
                        "evidence": [{"source": "evidence-1", "observation": "matches source"}],
                        "note": "说明",
                    }, blocker_id=blocker_id)
                    self.assertEqual(row["state"], "完成")
                    self.assertEqual(review["_review_verdict"]["covers"], [normalized])

    def test_covers_reject_wrong_id_or_more_than_one_display_pair(self):
        for blocker_id, reported_cover in (("[b-1]", "[[b-1]]"), ("b-1", "[[b-1]]"),
                                           ("[b-1]", "b-2")):
            with self.subTest(blocker_id=blocker_id, reported_cover=reported_cover), \
                    tempfile.TemporaryDirectory() as temp:
                review, row = self._run(temp, {
                    "reply": "复核",
                    "未决": [],
                    "verdict": "pass",
                    "covers": [reported_cover],
                    "evidence": [{"source": "evidence-1", "observation": "matches source"}],
                    "note": "说明",
                }, blocker_id=blocker_id)
                self.assertEqual(row["state"], "失败")
                self.assertNotIn("_review_verdict", review)

    def test_pass_cannot_claim_other_blockers_or_unbound_evidence(self):
        cases = [
            {"covers": ["other"], "evidence": [{"source": "evidence-1", "observation": "seen"}]},
            {"covers": ["b-1"], "evidence": [{"source": "unbound", "observation": "seen"}]},
        ]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                review, row = self._run(temp, {
                    "reply": "复核",
                    "未决": [],
                    "verdict": "pass",
                    "covers": case["covers"],
                    "evidence": case["evidence"],
                    "note": "结论",
                })
                self.assertEqual(row["state"], "失败")
                self.assertEqual(row["exit_class"], leafrun.EXIT_SHAPE)
                self.assertNotIn("_review_verdict", review)

    def test_rework_and_escalate_require_the_same_bound_blocker_evidence(self):
        for verdict in ("rework", "escalate"):
            for covers, evidence in (
                    (["other"], [{"source": "evidence-1", "observation": "seen"}]),
                    (["b-1"], [{"source": "unbound", "observation": "seen"}])):
                with self.subTest(verdict=verdict, covers=covers, evidence=evidence), \
                        tempfile.TemporaryDirectory() as temp:
                    review, row = self._run(temp, {
                        "reply": "复核",
                        "未决": [],
                        "verdict": verdict,
                        "covers": covers,
                        "evidence": evidence,
                        "note": "结论",
                    })
                    self.assertEqual(row["state"], "失败")
                    self.assertEqual(row["exit_class"], leafrun.EXIT_SHAPE)
                    self.assertNotIn("_review_verdict", review)

    def test_malformed_review_fails_without_storing_verdict(self):
        with tempfile.TemporaryDirectory() as temp:
            review, row = self._run(temp, {
                "reply": "结构化字段不完整",
                "未决": [],
                "verdict": "pass",
                "covers": ["b-1"],
                "evidence": [],
                "note": "缺少证据",
            })

            self.assertEqual(row["state"], "失败")
            self.assertEqual(row["exit_class"], leafrun.EXIT_SHAPE)
            self.assertNotIn("_review_verdict", review)
            self.assertIn("结构化复核字段不合规", row["why"])


if __name__ == "__main__":
    unittest.main()
