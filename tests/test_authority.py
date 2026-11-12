import json
import tempfile
import unittest
from pathlib import Path

from harness import authority


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    else:
        path.write_text(str(value), encoding="utf-8")


class AuthorityPolicyTests(unittest.TestCase):
    def test_default_policy_makes_input_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = authority.load_policy(tmp)
            self.assertFalse(authority.is_writable("input/prime-oracle.json", policy))
            self.assertTrue(authority.is_writable("product/answer.txt", policy))
            self.assertTrue(authority.is_writable("blocks/prime-report.md", policy))

    def test_custom_policy_protects_product_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "authority.json", {
                "authoritative": ["input/prime-oracle.json"],
                "read_only": ["input/", "product/answer.txt"],
                "writable": ["blocks/prime-report.md", "doc/"],
            })
            policy = authority.load_policy(tmp)
            self.assertTrue(authority.is_writable("blocks/prime-report.md", policy))
            self.assertFalse(authority.is_writable("product/answer.txt", policy))
            self.assertFalse(authority.is_writable("input/prime-oracle.json", policy))

    def test_rejects_absolute_and_parent_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "authority.json", {
                "read_only": ["../etc", "/etc/passwd", "input/"],
                "writable": ["blocks/"],
            })
            policy = authority.load_policy(tmp)
            self.assertEqual(policy["read_only"], ["input"])


class AuthorityFactTests(unittest.TestCase):
    def _fixture(self, tmp):
        root = Path(tmp)
        _write(root / "authority.json", {
            "authoritative": ["input/prime-oracle.json"],
            "read_only": ["input/", "product/answer.txt", "blocks/untouched-reference.md"],
            "writable": ["blocks/prime-report.md", "doc/"],
        })
        _write(root / "input/prime-oracle.json", {
            "range_start": 1,
            "range_end": 1000,
            "inclusive": True,
            "prime_count": 168,
            "method": "independent trial division",
        })
        _write(root / "product/answer.txt", "168\n")
        _write(root / "blocks/prime-report.md",
               "# Prime count report\n\nThe range contains 186 primes.\n")
        _write(root / "blocks/untouched-reference.md", "protected\n")
        return root

    def test_fact_sheet_contains_real_authority_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp)
            sheet = authority.fact_sheet(tmp)
            self.assertIn("只读权威", sheet)
            self.assertIn("prime_count=168", sheet)
            self.assertIn("不得修改", sheet)

    def test_canonical_conflict_targets_report_not_oracle(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp)
            item = ("input/prime-oracle.json 中 prime_count 仍为 168，"
                    "与修正后的 answer.txt 不一致")
            result = authority.canonical_conflict(item, tmp)
            self.assertIsNotNone(result)
            self.assertEqual(result["authority"], "input/prime-oracle.json")
            self.assertEqual(result["target"], "blocks/prime-report.md")
            self.assertIn("168", result["item"])
            self.assertNotIn("target_abs", result["item"])

    def test_false_claim_about_authority_is_rewritten_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp)
            result = authority.canonical_conflict(
                "input/prime-oracle.json 中 count=186", tmp)
            self.assertIsNotNone(result)
            self.assertEqual(result["target"], "blocks/prime-report.md")
            self.assertIn("prime_count=168", result["item"])
            self.assertIn("186", result["item"])

    def test_conflict_resolved_detects_consistent_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp)
            item = ("input/prime-oracle.json 中 prime_count 仍为 168，"
                    "与 blocks/prime-report.md 不一致")
            self.assertFalse(authority.conflict_resolved(item, tmp))
            _write(Path(tmp) / "blocks/prime-report.md",
                   "# Prime count report\n\nThe range contains 168 primes.\n")
            self.assertTrue(authority.conflict_resolved(item, tmp))

    def test_already_consistent_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp)
            _write(Path(tmp) / "blocks/prime-report.md",
                   "# Prime count report\n\nThe range contains 168 primes.\n")
            self.assertIsNone(authority.canonical_conflict(
                "input/prime-oracle.json 中 prime_count 与报告不一致", tmp))

    def test_two_authorities_are_not_auto_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp)
            _write(Path(tmp) / "input/second.json", {"prime_count": 200})
            _write(Path(tmp) / "authority.json", {
                "authoritative": ["input/prime-oracle.json", "input/second.json"],
                "read_only": ["input/"],
                "writable": ["blocks/prime-report.md"],
            })
            self.assertIsNone(authority.canonical_conflict(
                "input/prime-oracle.json 与 input/second.json 不一致", tmp))

    def test_writable_paths_are_run_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            paths = authority.writable_abs_paths(tmp)
            self.assertTrue(paths)
            for path in paths:
                self.assertTrue(path.startswith(str(root.resolve())))


if __name__ == "__main__":
    unittest.main()
