import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from harness import authority, driver, escalation, final_audit, recovery


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    else:
        path.write_text(str(value), encoding="utf-8")


class UnresolvedPropagationTests(unittest.TestCase):
    def test_hard_conflict_is_not_dropped_when_merge_omits_it(self):
        previous = [{"type": "冲突", "what": "oracle 168 vs report 186",
                     "why": "数值不一致"}]
        current = [{"type": "缺信息", "what": "minor note", "why": "optional"}]
        merged = driver._merge_unresolved(previous, current)
        self.assertIn({"type": "冲突", "what": "oracle 168 vs report 186",
                       "why": "数值不一致"}, merged)

    def test_soft_items_are_not_preserved_forever(self):
        previous = [{"type": "缺信息", "what": "old note", "why": "optional"}]
        merged = driver._merge_unresolved(previous, [])
        self.assertEqual(merged, [])

    def test_duplicate_hard_item_is_deduped(self):
        item = {"type": "冲突", "what": "same", "why": "same"}
        merged = driver._merge_unresolved([item], [dict(item)])
        self.assertEqual(len(merged), 1)

    def test_resolved_conflict_is_dropped_when_run_dir_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "authority.json", {
                "authoritative": ["input/prime-oracle.json"],
                "read_only": ["input/", "product/answer.txt"],
                "writable": ["blocks/prime-report.md", "doc/"]})
            _write(root / "input/prime-oracle.json", {"prime_count": 168})
            _write(root / "product/answer.txt", "168\n")
            _write(root / "blocks/prime-report.md", "The range contains 168 primes.\n")
            previous = [{
                "type": "冲突",
                "what": ("input/prime-oracle.json 中 prime_count 仍为 168，"
                         "与 blocks/prime-report.md 不一致"),
                "why": "old conflict",
            }]
            merged = driver._merge_unresolved(previous, [], tmp)
            self.assertEqual(merged, [])


class UserChannelTests(unittest.TestCase):
    def test_auth_issue_goes_to_user_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "authority.json", {
                "read_only": ["input/"], "writable": ["blocks/"]})
            issue = final_audit.make_adjudication_issue(
                {"id": "root", "round": 1}, {"reply": "x", "去向": "收束"},
                [{"id": "b1", "item": "该操作需要授权：删除 input/x.json",
                  "级别": "blocker"}],
                {}, tmp, {}, {}, stage="user")
            self.assertEqual(issue.get("stage"), "user")
            self.assertIn("需要用户确认", str(issue.get("question") or ""))
            self.assertTrue(issue["card"]["auth_affected"])

    def test_auth_blocker_classification_is_direct_user(self):
        row = escalation.classify_blocker("该操作需要授权：删除 input/x.json")
        self.assertEqual(row["reason"], escalation.E_AUTH)
        self.assertEqual(row["route"], escalation.R_USER)


class RecoveryStrategyTests(unittest.TestCase):
    def test_same_strategy_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = recovery.RecoveryController(tmp)
            first = ctrl.choose("root", "root.c1", "phantom_citation", "bad")
            self.assertIsNotNone(first)
            second = ctrl.choose("root", "root.c1", "phantom_citation", "bad",
                                 old_strategy=first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second)
            third = ctrl.choose("root", "root.c1", "phantom_citation", "bad",
                                old_strategy=second)
            self.assertIsNone(third)

    def test_exhausted_strategy_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = recovery.RecoveryController(tmp)
            ctrl.choose("root", "root.c1", "timeout", "slow")
            self.assertIsNone(ctrl.choose("root", "root.c1", "timeout", "slow"))
            log = (Path(tmp) / "recovery.jsonl").read_text(encoding="utf-8")
            self.assertIn("exhausted", log)


class RepairTargetBoundaryTests(unittest.TestCase):
    def test_input_is_never_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = authority.load_policy(tmp)
            self.assertFalse(authority.is_writable("input/prime-oracle.json", policy))

    def test_protected_product_is_not_writable_under_custom_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "authority.json", {
                "read_only": ["input/", "product/answer.txt"],
                "writable": ["blocks/prime-report.md", "doc/"]})
            policy = authority.load_policy(tmp)
            self.assertFalse(authority.is_writable("product/answer.txt", policy))
            self.assertTrue(authority.is_writable("blocks/prime-report.md", policy))


class AuditConfirmedConflictTests(unittest.TestCase):
    TABLE = {"roles": {"执行": {"may_write": True},
                       "审计": {"may_write": False}}}

    def test_readonly_child_conflict_forces_converge(self):
        parent = {"未决": [{"type": "冲突", "what": "186 vs 168", "why": "x"}],
                  "batch": 3}
        tree = {"root.c3.1": {"role": "审计",
                              "未决": [{"type": "冲突", "what": "186 vs 168",
                                      "why": "x"}]}}
        with unittest.mock.patch.object(driver.node_mod, "batch_members",
                                        return_value=["root.c3.1"]):
            self.assertTrue(driver._audit_confirmed_conflict(
                parent, tree, self.TABLE))

    def test_writer_child_does_not_confirm(self):
        parent = {"未决": [{"type": "冲突", "what": "x", "why": "y"}], "batch": 1}
        tree = {"root.c1.1": {"role": "执行",
                              "未决": [{"type": "冲突", "what": "x", "why": "y"}]}}
        with unittest.mock.patch.object(driver.node_mod, "batch_members",
                                        return_value=["root.c1.1"]):
            self.assertFalse(driver._audit_confirmed_conflict(
                parent, tree, self.TABLE))

    def test_no_parent_conflict_does_not_force(self):
        with unittest.mock.patch.object(driver.node_mod, "batch_members",
                                        return_value=[]):
            self.assertFalse(driver._audit_confirmed_conflict(
                {"未决": []}, {}, self.TABLE))


class ConflictBeforeTerminalAuditTests(unittest.TestCase):
    def _driver(self, tmp):
        return SimpleNamespace(
            table={"roles": {"执行": {"may_write": True},
                             "审计": {"may_write": False}}},
            run_dir=tmp)

    def test_writer_targeting_conflict_file_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "authority.json", {
                "read_only": ["input/"],
                "writable": ["blocks/prime-report.md", "doc/"]})
            parent = {"未决": [{"type": "冲突", "what": "oracle 168 vs report 186",
                                "why": "数值不一致"}]}
            targets = driver._hard_conflict_writable_targets(
                parent, "修正 blocks/prime-report.md 的计数", "执行",
                self._driver(tmp))
            self.assertEqual(targets, ["blocks/prime-report.md"])

    def test_audit_leaf_stays_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "authority.json", {
                "read_only": ["input/"],
                "writable": ["blocks/prime-report.md"]})
            parent = {"未决": [{"type": "冲突", "what": "x", "why": "y"}]}
            self.assertEqual(driver._hard_conflict_writable_targets(
                parent, "核对 blocks/prime-report.md 与 oracle", "审计",
                self._driver(tmp)), [])

    def test_no_conflict_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "authority.json", {
                "writable": ["blocks/prime-report.md"]})
            self.assertEqual(driver._hard_conflict_writable_targets(
                {"未决": []}, "修正 blocks/prime-report.md", "执行",
                self._driver(tmp)), [])


class RoutingLedgerTests(unittest.TestCase):
    def test_escalation_ledger_records_reason_and_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = escalation.classify_blocker("文档不够详细，建议补充")
            row.update({"node_id": "root", "blocker_id": "b1"})
            escalation.record(tmp, row)
            rows = escalation.read_ledger(tmp)
            self.assertEqual(rows[-1]["reason"], escalation.E_POLICY)
            self.assertEqual(rows[-1]["route"], escalation.R_REVIEW)


if __name__ == "__main__":
    unittest.main()
