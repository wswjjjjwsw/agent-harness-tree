"""Focused final-audit evidence binding and hard-check adjudication tests."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import contract, driver, final_audit, node, role


class AdjudicationBindingTests(unittest.TestCase):
    def test_bare_product_basenames_resolve_to_owner_execution_tool_results(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "product").mkdir()
            (run_dir / "product" / "answer.txt").write_text("168", encoding="utf-8")
            (run_dir / "blocks").mkdir()
            (run_dir / "blocks" / "primitive_count_doc.md").write_text(
                "primitive count", encoding="utf-8")
            session = run_dir / "root-c2-1.jsonl"
            session.write_text(json.dumps({
                "message": {"role": "toolResult",
                            "content": "SIEVE_COUNT:168/SIEVE2_COUNT:168"}
            }) + "\n", encoding="utf-8")
            tree = {
                "root": node.new_node("root", "根"),
                "root.c2.1": node.new_node("root.c2.1", "执行", parent="root"),
                "root.audit.1": node.new_node("root.audit.1", "审计", parent="root"),
                "other": node.new_node("other", "根"),
                "other.c1.1": node.new_node("other.c1.1", "执行", parent="other"),
            }
            for nid, citations in (
                ("root.c2.1", ["product/answer.txt", "blocks/primitive_count_doc.md"]),
                ("root.audit.1", ["product/answer.txt"]),
                ("other.c1.1", ["product/answer.txt"]),
            ):
                contract.append_ledger(str(run_dir), {
                    "node_id": nid, "kind": "synthesis", "role": "执行",
                    "result_type": "complete",
                    "tools": {"calls": 1, "successes": 1, "failures": 0},
                    "session_id": nid, "session_path": str(session),
                    "citations": citations, "unresolved": [],
                })

            for item in ("answer.txt 与 primitive_count_doc.md 中具体数值的来源可追溯性",
                         "session 日志中工具调用结果与 answer.txt 的语义对齐关系"):
                with self.subTest(item=item):
                    rows = final_audit._review_input_evidence(
                        {"id": "b-1", "item": item}, tree, str(run_dir), "root")
                    tool_rows = [row for row in rows
                                 if str(row["source_id"]).startswith("tool:")]
                    self.assertTrue(tool_rows)
                    self.assertTrue(all("root.c2.1" in row["source"] for row in tool_rows))
                    self.assertTrue(any("SIEVE_COUNT:168/SIEVE2_COUNT:168"
                                        in row["observation"] for row in tool_rows))
                    file_rows = [row for row in rows
                                 if row["source"] == "blocks/primitive_count_doc.md"]
                    if "primitive_count_doc.md" in item:
                        self.assertTrue(file_rows)
                        self.assertTrue(file_rows[0]["source_id"].startswith("file:blocks/"))
                    self.assertFalse(any("root.audit.1" in row["source"]
                                         or "other.c1.1" in row["source"]
                                         for row in tool_rows))

    def test_declared_source_input_is_bound_to_review_and_issue(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            rel = "input/audit-data/events.psv"
            source = run_dir / rel
            source.parent.mkdir(parents=True)
            source.write_text("E-101|ACTIVE|2\nE-102|REVOKED|2\n", encoding="utf-8")
            tree = {"root": node.new_node("root", "根"),
                    "root.c2.1": node.new_node("root.c2.1", "执行", parent="root"),
                    "other": node.new_node("other", "根")}
            tree["root.c2.1"]["overrides"] = {"input_evidence_ids": [rel]}
            other_rel = "input/audit-data/other.psv"
            other = run_dir / other_rel
            other.write_text("UNRELATED\n", encoding="utf-8")
            tree["other"]["overrides"] = {"input_evidence_ids": [other_rel]}
            blocker = {"id": "b-1", "item": "同秒冲突需核对 events.psv"}
            rows = final_audit._review_input_evidence(
                blocker, tree, str(run_dir), "root")
            source_rows = [row for row in rows if row["source"] == rel]
            self.assertEqual(len(source_rows), 1)
            self.assertIn("E-101|ACTIVE", source_rows[0]["observation"])
            candidate = {"去向": "收束", "reply": "done", "未决": [], "uncovered": []}
            issue = final_audit.make_adjudication_issue(
                tree["root"], candidate, [blocker], tree, str(run_dir), {}, {})
            self.assertIn(rel, issue["binding"]["artifact_snapshot"])
            self.assertNotIn(other_rel, issue["binding"]["artifact_snapshot"])
            self.assertIn(rel, issue["card"]["problem"])
            self.assertNotIn(other_rel, issue["card"]["problem"])
            self.assertEqual(
                final_audit.snapshot_product_files(str(run_dir), tree)[rel],
                issue["binding"]["artifact_snapshot"][rel])

    def test_adjudication_snapshot_checks_bound_source_and_ignores_new_files(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            source = run_dir / "input/audit-data/events.psv"
            source.parent.mkdir(parents=True)
            source.write_text("E-101|ACTIVE|2\n", encoding="utf-8")
            product = run_dir / "product"
            product.mkdir()
            (product / "answer.txt").write_text("168\n", encoding="utf-8")
            (product / "unrelated-later.txt").write_text("new", encoding="utf-8")
            parent = node.new_node("root", "根", state="待跑")
            candidate = {"去向": "收束", "reply": "done", "未决": [], "uncovered": []}
            parent["merge"] = candidate
            snapshot = {"input/audit-data/events.psv": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "product/answer.txt": hashlib.sha256((product / "answer.txt").read_bytes()).hexdigest()}
            issue = {"binding": {"candidate_sha256": final_audit._canonical_sha256(candidate),
                                  "artifact_snapshot": snapshot},
                     "blockers": []}
            d = driver.Driver(parent, tree={"root": parent}, table=role.load_roles(),
                              run_id="snapshot-check", run_dir=temp,
                              act_runner=lambda *_args: "{}")
            with patch("harness.final_audit.hard_convergence_failures", return_value=[]), \
                    patch("harness.driver.schema.validate", return_value=(True, "")), \
                    patch.object(d, "_check_partial_coverage", return_value=(True, "")):
                ok, why, hard = d._adjudication_checks(parent, issue)
                self.assertTrue(ok, (why, hard))
                source.write_text("E-101|REVOKED|2\n", encoding="utf-8")
                ok, why, _hard = d._adjudication_checks(parent, issue)
                self.assertFalse(ok)
                self.assertIn("snapshot changed", why)

    def test_accept_merge_writes_zero_blocker_ledger_row_after_review(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = node.new_node("root", "根", state="在跑", round=6)
            candidate = {"去向": "收束", "reply": "done", "未决": [], "uncovered": []}
            parent["merge"] = candidate
            parent["_merge_continuation"] = {"origin_audit_cid": "root.f6.2"}
            d = driver.Driver(parent, tree={"root": parent}, table=role.load_roles(),
                              run_id="review-ledger", run_dir=temp,
                              act_runner=lambda *_args: "{}")
            issue = {"id": "adj-review-ledger",
                     "blockers": [{"id": "b1", "item": "report 186 vs oracle 168"}],
                     "binding": {"candidate_sha256":
                                 final_audit._canonical_sha256(candidate)}}
            with patch.object(d, "_adjudication_checks", return_value=(True, "", [])):
                result = d._accept_merge_continuation(parent, issue)
            self.assertEqual(result["status"], "accepted")
            rows = [json.loads(line) for line in
                    (Path(temp) / "final_audit.jsonl").read_text(
                        encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(rows)
            self.assertEqual(rows[-1]["blocker数"], 0)
            self.assertIn("复核/裁决 pass", rows[-1].get("原因") or "")

    def test_bound_sage_pass_applies_even_when_unrelated_unresolved_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = node.new_node("root", "根", state="待跑")
            candidate = {"去向": "收束", "reply": "complete",
                         "未决": [], "uncovered": []}
            parent["merge"] = candidate
            review = node.new_node("root.fix.review1.1", "审计", parent="root",
                                   state="完成", judge_ok=True)
            review["_review_issue_id"] = "adj-sage-test"
            review["_review_blocker_id"] = "b-1"
            review["_review_verdict"] = {
                "verdict": "pass", "covers": ["b-1"],
                "evidence": [{"source": "file:product/answer.txt:sha",
                              "observation": "matched"}],
                "note": "该争议由证据解决",
            }
            review["未决"] = [{"type": "缺信息", "what": "unrelated",
                               "why": "kept as supplied"}]
            tree = {"root": parent, review["id"]: review}
            d = driver.Driver(parent, tree=tree, table=role.load_roles(),
                              run_id="adjudication-sage-test", run_dir=temp,
                              act_runner=lambda *_args: "{}")
            candidate_sha = final_audit._canonical_sha256(candidate)
            issue = {
                "id": "adj-sage-test", "candidate_sha256": candidate_sha,
                "binding": {"candidate_sha256": candidate_sha, "artifact_snapshot": {}},
                "blockers": [{"id": "b-1", "item": "BLOCKER answer.txt", "level": "blocker"}],
                "card": {"id": "adj-sage-test.leo"},
            }
            parent["_adjudication"] = {
                "issue": issue, "issue_id": issue["id"],
                "review_node_ids": [review["id"]],
            }
            with patch("harness.driver.schema.validate", return_value=(True, "")),                     patch("harness.final_audit.hard_convergence_failures", return_value=[]),                     patch.object(d, "_check_partial_coverage", return_value=(True, "")):
                result = d.resume_adjudication(parent, {
                    "source": "sage", "issue_id": issue["id"],
                    "candidate_sha256": candidate_sha,
                    "reviews": [],
                })

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(parent["state"], "完成")
            self.assertEqual(review["未决"][0]["what"], "unrelated")

    def test_sage_pass_is_not_applied_if_same_blocker_remains_unresolved(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = node.new_node("root", "根", state="待跑")
            candidate = {"去向": "收束", "reply": "complete",
                         "未决": [], "uncovered": []}
            parent["merge"] = candidate
            review = node.new_node("root.fix.review1.1", "审计", parent="root",
                                   state="完成", judge_ok=True)
            review["_review_issue_id"] = "adj-conflict-test"
            review["_review_blocker_id"] = "b-1"
            review["_review_snapshot"] = {}
            review["_review_verdict"] = {
                "verdict": "pass", "covers": ["b-1"],
                "evidence": [{"source": "evidence-1", "observation": "seen"}],
                "note": "pass",
            }
            review["未决"] = [{"type": "缺信息", "what": "b-1 remains blocked",
                               "why": "same blocker still needs evidence"}]
            tree = {"root": parent, review["id"]: review}
            d = driver.Driver(parent, tree=tree, table=role.load_roles(),
                              run_id="adjudication-conflict-test", run_dir=temp,
                              act_runner=lambda *_args: "{}")
            candidate_sha = final_audit._canonical_sha256(candidate)
            issue = {
                "id": "adj-conflict-test", "candidate_sha256": candidate_sha,
                "binding": {"candidate_sha256": candidate_sha, "artifact_snapshot": {}},
                "blockers": [{"id": "b-1", "item": "BLOCKER answer.txt", "level": "blocker"}],
                "card": {"id": "adj-conflict-test.leo"},
            }
            parent["_adjudication"] = {
                "issue": issue, "issue_id": issue["id"],
                "review_node_ids": [review["id"]],
            }
            with patch("harness.driver.schema.validate", return_value=(True, "")),                     patch("harness.final_audit.hard_convergence_failures", return_value=[]),                     patch.object(d, "_check_partial_coverage", return_value=(True, "")):
                result = d.resume_adjudication(parent, {
                    "source": "sage", "issue_id": issue["id"],
                    "candidate_sha256": candidate_sha,
                })

            self.assertEqual(result["status"], "leo")
            self.assertEqual(review["未决"][0]["what"], "b-1 remains blocked")

    def test_hard_doc_specs_are_disabled_by_the_content_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "product").mkdir()
            target = run_dir / "product" / "primitive_count_doc.md"
            target.write_text("x" * 1340, encoding="utf-8")
            parent = node.new_node("root", "根")
            leaf = node.new_node("root.c2.1", "执行", parent="root", state="失败")
            leaf["disk_doc_check"] = {
                "chars_min": 1500,
                "written": {"product/primitive_count_doc.md": 1340},
            }
            tree = {"root": parent, "root.c2.1": leaf}

            self.assertEqual(final_audit.hard_doc_specs(tree, str(run_dir), parent), [])
            target.write_text("x" * 1500, encoding="utf-8")
            self.assertEqual(final_audit.hard_doc_specs(tree, str(run_dir), parent), [])

    def test_leo_pass_cannot_waive_current_hard_mechanical_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = node.new_node("root", "根", state="待跑")
            candidate = {"去向": "收束", "reply": "complete",
                         "未决": [], "uncovered": []}
            parent["merge"] = candidate
            tree = {"root": parent}
            d = driver.Driver(parent, tree=tree, table=role.load_roles(),
                              run_id="adjudication-hard-test", run_dir=temp,
                              act_runner=lambda *_args: "{}")
            candidate_sha = final_audit._canonical_sha256(candidate)
            issue = {
                "id": "adj-test", "candidate_sha256": candidate_sha,
                "binding": {"candidate_sha256": candidate_sha, "artifact_snapshot": {}},
                "blockers": [], "card": {"id": "adj-test.leo"},
            }
            parent["_adjudication"] = {"issue": issue, "issue_id": issue["id"]}
            outcome = {
                "source": "leo", "issue_id": issue["id"],
                "candidate_sha256": candidate_sha,
                "card_sha256": final_audit._canonical_sha256(issue["card"]),
                "receipt": {"status": "done", "verdict": "pass", "blocking": False,
                            "missing": [], "requires_user_approval": False},
            }
            with patch("harness.driver.schema.validate", return_value=(True, "")), \
                    patch("harness.final_audit.hard_convergence_failures",
                          return_value=[{"kind": "disk_doc", "item": "doc too short"}]):
                result = d.resume_adjudication(parent, outcome)

            self.assertEqual(result["status"], "failed")
            self.assertIn("mechanical", result["reason"])


if __name__ == "__main__":
    unittest.main()
