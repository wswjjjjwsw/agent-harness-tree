import tempfile
import unittest
from pathlib import Path

from harness import escalation


class EscalationClassificationTests(unittest.TestCase):
    def test_auth_words_go_to_user(self):
        row = escalation.classify_blocker("该操作需要授权：删除 product/answer.txt")
        self.assertEqual(row["reason"], escalation.E_AUTH)
        self.assertEqual(row["route"], escalation.R_USER)

    def test_budget_words_go_to_explicit_failure(self):
        row = escalation.classify_blocker("终审预算耗尽，不再派发")
        self.assertEqual(row["reason"], escalation.E_BUDGET)
        self.assertEqual(row["route"], escalation.R_FAIL)

    def test_structural_gap_is_evidence_not_judgement(self):
        row = escalation.classify_blocker("未审批次 root#2 批次子数不足阈值")
        self.assertEqual(row["reason"], escalation.E_EVIDENCE)
        self.assertEqual(row["route"], escalation.R_RESEARCH)

    def test_missing_file_is_evidence(self):
        row = escalation.classify_blocker("input/prime-oracle.json 不存在")
        self.assertEqual(row["reason"], escalation.E_EVIDENCE)

    def test_numeric_mismatch_is_fact_not_policy(self):
        row = escalation.classify_blocker(
            "input/prime-oracle.json 中 prime_count 仍为 168，与修正后的 answer.txt 不一致")
        self.assertEqual(row["reason"], escalation.E_FACT)
        self.assertIn("input/prime-oracle.json", row["cites"])

    def test_quality_opinion_is_policy(self):
        row = escalation.classify_blocker("文档不够详细，建议补充背景与误差说明")
        self.assertEqual(row["reason"], escalation.E_POLICY)
        self.assertEqual(row["route"], escalation.R_REVIEW)

    def test_unknown_falls_back_to_policy_not_pass(self):
        row = escalation.classify_blocker("这个结果我不太确定")
        self.assertEqual(row["reason"], escalation.E_POLICY)
        self.assertEqual(row["route"], escalation.R_REVIEW)

    def test_failure_classes_map_to_local_recovery(self):
        self.assertEqual(escalation.classify_failure("timeout")["reason"],
                         escalation.E_BROKEN)
        self.assertEqual(escalation.classify_failure("bad_citation")["reason"],
                         escalation.E_EVIDENCE)
        self.assertEqual(escalation.classify_failure("schema_missing_field")["reason"],
                         escalation.E_BROKEN)
        self.assertEqual(escalation.classify_failure("unverifiable_criteria")["reason"],
                         escalation.E_POLICY)

    def test_only_policy_reaches_external_judge(self):
        for reason in escalation.REASONS:
            expected = reason == escalation.E_POLICY
            self.assertEqual(escalation.needs_external_judge(reason), expected, reason)
        self.assertTrue(escalation.needs_user(escalation.E_AUTH))
        self.assertFalse(escalation.needs_user(escalation.E_FACT))

    def test_fact_without_writable_target_fails(self):
        self.assertEqual(escalation.route_for(escalation.E_FACT, has_target=False),
                         escalation.R_FAIL)
        self.assertEqual(escalation.route_for(escalation.E_FACT, has_target=True),
                         escalation.R_REWORK)

    def test_broken_without_budget_fails(self):
        self.assertEqual(escalation.route_for(escalation.E_BROKEN, budget_left=False),
                         escalation.R_FAIL)
        self.assertEqual(escalation.route_for(escalation.E_BROKEN, budget_left=True),
                         escalation.R_RESEARCH)

    def test_ledger_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = escalation.classify_blocker("文档不够详细，建议补充")
            row.update({"node_id": "root", "blocker_id": "b1"})
            written = escalation.record(tmp, row)
            self.assertTrue(written["recorded"])
            rows = escalation.read_ledger(tmp)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reason"], escalation.E_POLICY)
            self.assertEqual(rows[0]["node_id"], "root")
            self.assertEqual(rows[0]["blocker_id"], "b1")


if __name__ == "__main__":
    unittest.main()
