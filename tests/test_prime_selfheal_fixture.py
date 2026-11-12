# -*- coding: utf-8 -*-
"""B 夹具（prime-selfheal）的公开复现测试：离线、不调模型。

断言四件事：
  ① 夹具字节与 prereg 冻结的 sha256 一致；
  ② authority 策略把 input/ 与受保护产物判为只读、只留报告与 doc/ 可写；
  ③ 权威事实改写是纯函数：冲突文本被改写成「必须把报告改为 168」，改完即判已解决；
  ④ examples/prime-report/stub-prime.py 的终审/修复产出能通过 harness 契约校验。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import authority, role, schema

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "prime-selfheal"
STUB = HERE.parent / "examples" / "prime-report" / "stub-prime.py"
PREREG = FIXTURE / "prereg" / "acceptance.json"
REPORT = "blocks/prime-report.md"
ORACLE = "input/prime-oracle.json"
ANSWER = "product/answer.txt"
UNTOUCHED = "blocks/untouched-reference.md"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _policy() -> dict:
    return json.loads(PREREG.read_text(encoding="utf-8"))


class FixtureIntegrityTests(unittest.TestCase):
    def test_fixture_hashes_match_prereg(self):
        files = _policy()["files_sha256"]
        self.assertIn("input/q01.md", files)
        for rel, want in files.items():
            with self.subTest(rel=rel):
                self.assertEqual(_sha(FIXTURE / rel), want)

    def test_prereg_has_eight_criteria(self):
        self.assertEqual(len(_policy()["acceptance"]), 8)


class AuthorityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = authority.load_policy(str(FIXTURE))

    def test_authority_is_the_oracle(self):
        self.assertEqual(self.policy["authoritative"], [ORACLE])
        self.assertTrue(authority.is_authoritative(ORACLE, self.policy))

    def test_read_only_sides_are_closed(self):
        for rel in (ORACLE, ANSWER, UNTOUCHED):
            with self.subTest(rel=rel):
                self.assertTrue(authority.is_read_only(rel, self.policy))
                self.assertFalse(authority.is_writable(rel, self.policy))

    def test_only_report_and_doc_are_writable(self):
        self.assertEqual(self.policy["writable"], [REPORT, "doc"])
        self.assertTrue(authority.is_writable(REPORT, self.policy))
        self.assertFalse(authority.is_writable("input/q01.md", self.policy))


class FactRewriteTests(unittest.TestCase):
    ITEM = ("BLOCKER %s 写 186，与权威 %s 的 prime_count=168 不一致"
            % (REPORT, ORACLE))

    def test_conflict_is_rewritten_from_disk_facts(self):
        state = authority.canonical_conflict(self.ITEM, str(FIXTURE))
        self.assertIsNotNone(state)
        self.assertEqual(state["authority"], ORACLE)
        self.assertEqual(state["target"], REPORT)
        self.assertIn("168", state["item"])
        self.assertIn("必须把可写目标改为与权威一致", state["item"])

    def test_rewrite_is_resolved_after_report_is_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(FIXTURE, tmp, dirs_exist_ok=True)
            self.assertFalse(authority.conflict_resolved(self.ITEM, tmp))
            expected = (FIXTURE / "oracle" / "report.expected.md").read_text(encoding="utf-8")
            (Path(tmp) / REPORT).write_text(expected, encoding="utf-8")
            self.assertTrue(authority.conflict_resolved(self.ITEM, tmp))

    def test_matching_report_is_not_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(FIXTURE, tmp, dirs_exist_ok=True)
            expected = (FIXTURE / "oracle" / "report.expected.md").read_text(encoding="utf-8")
            (Path(tmp) / REPORT).write_text(expected, encoding="utf-8")
            self.assertIsNone(authority.canonical_conflict(self.ITEM, tmp))


class StubContractTests(unittest.TestCase):
    def _run_stub(self, workdir: Path, prompt: str, act: str = "leaf",
                  node_id: str = "root.f4.1") -> dict:
        proc = subprocess.run(
            [sys.executable, str(STUB), act, node_id, "4"],
            input=prompt, capture_output=True, text=True, timeout=60,
            env=dict(os.environ, TREE_DEMO_WORKDIR=str(workdir)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_terminal_audit_output_is_valid_and_cites_both_files(self):
        out = self._run_stub(FIXTURE, "这是【全量终审】请核对报告与权威。")
        ok, why = schema.validate("leaf", out, role.effective("审计"))
        self.assertTrue(ok, why)
        self.assertEqual(len(out["未决"]), 1)
        item = out["未决"][0]
        self.assertEqual(item["type"], "冲突")
        self.assertIn(REPORT, item["what"])
        self.assertIn(ORACLE, item["what"])
        self.assertIsNotNone(authority.canonical_conflict(item["what"], str(FIXTURE)))

    def test_terminal_audit_is_silent_when_report_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(FIXTURE, tmp, dirs_exist_ok=True)
            expected = (FIXTURE / "oracle" / "report.expected.md").read_text(encoding="utf-8")
            (Path(tmp) / REPORT).write_text(expected, encoding="utf-8")
            out = self._run_stub(Path(tmp), "这是【全量终审】请核对报告与权威。")
            self.assertEqual(out["未决"], [])

    def test_repair_leaf_returns_the_full_fixed_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(FIXTURE, tmp, dirs_exist_ok=True)
            out = self._run_stub(Path(tmp), "", node_id="root.fix.audit2.1")
            ok, why = schema.validate("leaf", out, role.effective("执行"))
            self.assertTrue(ok, why)
            expected = (FIXTURE / "oracle" / "report.expected.md").read_text(encoding="utf-8")
            self.assertEqual(out["doc"], expected)
            self.assertNotIn("186 primes", out["doc"])

            (Path(tmp) / REPORT).write_text(out["doc"], encoding="utf-8")
            self.assertTrue(authority.conflict_resolved(
                "BLOCKER %s 写 186，与权威 %s 的 prime_count=168 不一致" % (REPORT, ORACLE), tmp))
            self.assertEqual(_sha(Path(tmp) / ANSWER), _sha(FIXTURE / ANSWER))
            self.assertEqual(_sha(Path(tmp) / UNTOUCHED), _sha(FIXTURE / UNTOUCHED))


if __name__ == "__main__":
    unittest.main()
