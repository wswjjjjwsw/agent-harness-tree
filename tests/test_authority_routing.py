import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness import authority, driver, escalation, evidence, final_audit, node as node_mod, role


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    else:
        path.write_text(str(value), encoding="utf-8")


def _fixture(tmp):
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


B02_ITEM = ("input/prime-oracle.json 中 prime_count 仍为 168，"
            "与修正后的 answer.txt 不一致")
B03_ITEM = "input/prime-oracle.json 中 count=186"


class AuthorityRoutingTests(unittest.TestCase):
    def test_b02_blocker_is_canonicalised_to_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            d = SimpleNamespace(run_dir=tmp, tree={})
            kept, dropped = final_audit.reverify_blockers(
                [{"id": "b1", "item": B02_ITEM, "级别": "blocker"}], d, "root")
            self.assertEqual(dropped, [])
            self.assertEqual(len(kept), 1)
            blocker = kept[0]
            self.assertEqual(blocker.get("reason"), escalation.E_FACT)
            self.assertEqual(blocker["authority_direction"]["authority"],
                             "input/prime-oracle.json")
            self.assertEqual(blocker["authority_direction"]["target"],
                             "blocks/prime-report.md")
            self.assertIn("blocks/prime-report.md", blocker["item"])
            self.assertIn("168", blocker["item"])

    def test_repair_spec_uses_the_canonical_writable_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            d = SimpleNamespace(run_dir=tmp, tree={})
            kept, _ = final_audit.reverify_blockers(
                [{"id": "b1", "item": B02_ITEM, "级别": "blocker"}], d, "root")
            specs = final_audit.repair_specs(kept, {}, tmp)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0]["target"], "blocks/prime-report.md")
            self.assertTrue(specs[0]["target_abs"].endswith("blocks/prime-report.md"))

    def test_repair_child_sees_real_authority_value_and_write_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            d = SimpleNamespace(run_dir=tmp, tree={})
            kept, _ = final_audit.reverify_blockers(
                [{"id": "b1", "item": B02_ITEM, "级别": "blocker"}], d, "root")
            specs = final_audit.repair_specs(kept, {}, tmp)
            child = final_audit.build_repair_child(specs[0], tmp)
            context = str(child.get("context") or "")
            self.assertIn("权威事实", context)
            self.assertIn("prime_count=168", context)
            self.assertIn("blocks/prime-report.md", context)
            self.assertIn("写入边界", context)
            self.assertIn("不得修改", context)

    def test_b03_false_claim_is_classified_as_fact_with_real_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            row = escalation.classify_blocker(B03_ITEM, tmp)
            self.assertEqual(row["reason"], escalation.E_FACT)
            self.assertEqual(row["target"], "blocks/prime-report.md")
            self.assertIn("prime_count=168", row["canonical_item"])

    def test_reason_helpers_route_auth_and_structural(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            rows = driver._blocker_reasons([
                {"id": "auth", "item": "该操作需要授权：删除 blocks/prime-report.md"},
                {"id": "struct", "item": "未审批次 root#2 批次子数不足阈值"},
                {"id": "fact", "item": B02_ITEM},
            ], tmp)
            self.assertEqual([r["reason"] for r in rows],
                             [escalation.E_AUTH, escalation.E_EVIDENCE,
                              escalation.E_FACT])

    def test_fact_blocker_is_covered_by_its_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            d = SimpleNamespace(run_dir=tmp, tree={})
            kept, _ = final_audit.reverify_blockers(
                [{"id": "b1", "item": B02_ITEM, "级别": "blocker"}], d, "root")
            specs = final_audit.repair_specs(kept, {}, tmp)
            self.assertTrue(driver._blocker_covered_by_specs(kept[0], specs))

    def test_cap_outcome_dispatches_fact_repair_to_writable_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            parent = node_mod.new_node("root", "根", state="在跑", round=3, batch=3)
            candidate = {"去向": "收束", "reply": "done", "未决": [], "uncovered": []}
            parent["merge"] = candidate
            parent["_audit_cap_blockers"] = [
                {"id": "b1", "item": B02_ITEM, "级别": "blocker"}]
            parent["_audit_cap_reason"] = "次数上限"
            parent["_audit_cap_cid"] = "root.f6.1"
            tree = {"root": parent}
            d = driver.Driver(parent, tree=tree, table=role.load_roles(),
                              run_id="cap-route", run_dir=tmp,
                              act_runner=lambda *_args: "{}")
            result = d._audit_cap_outcome(parent, candidate, {})
            self.assertIn("修复派发", result)
            child_id = "root.fix.audit1.1"
            self.assertIn(child_id, d.tree)
            child = d.tree[child_id]
            self.assertEqual((child.get("_managed_doc") or {}).get("target"),
                             "blocks/prime-report.md")
            self.assertIn("prime_count=168", child.get("context") or "")

    def test_audit_gate_accepts_shorthand_when_one_real_citation_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            node = {"id": "root.c3.1", "role": "审计", "context": "x",
                    "judge": {"json_keys": ["reply", "未决"]},
                    "验收标准": "x", "auth": "只读"}
            ok, detail = evidence.evidence_gate(
                node, {"reply": "blocks/prime-report.md 与 oracle/answer.txt 不一致",
                       "未决": []},
                "", tmp, tmp, may_write=False, node_kind="audit")
            self.assertTrue(ok, detail)
            ok_bad, _ = evidence.evidence_gate(
                node, {"reply": "oracle/answer.txt 不一致", "未决": []},
                "", tmp, tmp, may_write=False, node_kind="audit")
            self.assertFalse(ok_bad)

    def test_unparseable_spawn_falls_back_to_mechanical_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            parent = node_mod.new_node("root", "根", state="在跑", round=3)
            parent["merge"] = {
                "去向": "再派", "reply": "修复 blocks/prime-report.md 为 168",
                "下一批要点": ["修复 blocks/prime-report.md 的 186 为 168"],
            }
            d = driver.Driver(parent, tree={"root": parent},
                              table=role.load_roles(), run_id="mech-spawn",
                              run_dir=tmp, act_runner=lambda *_args: "{}")
            cid = d._mechanical_repair_from_merge(parent, "truncated")
            self.assertTrue(cid)
            self.assertIn(".fix.audit", cid)
            child = d.tree[cid]
            self.assertEqual((child.get("_managed_doc") or {}).get("target"),
                             "blocks/prime-report.md")
            self.assertEqual(parent.get("state"), node_mod.WAIT_CHILDREN)

    def test_policy_blocker_is_not_covered_by_fact_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            d = SimpleNamespace(run_dir=tmp, tree={})
            kept, _ = final_audit.reverify_blockers(
                [{"id": "b1", "item": B02_ITEM, "级别": "blocker"}], d, "root")
            specs = final_audit.repair_specs(kept, {}, tmp)
            self.assertFalse(driver._blocker_covered_by_specs(
                {"id": "other", "item": "文档不够详细，建议补充背景"}, specs))


if __name__ == "__main__":
    unittest.main()
