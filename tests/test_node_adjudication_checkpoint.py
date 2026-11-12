"""Tests for the adjudication wait state and durable tree checkpoints."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import node, recover


class NodeAdjudicationCheckpointTests(unittest.TestCase):
    def test_wait_state_can_resume_or_fail_and_terminals_stay_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            sink = str(Path(temp) / "violations.jsonl")
            resumed = node.new_node("resume", "worker")
            self.assertTrue(node.change_state(resumed, "在跑", sink)[0])
            self.assertTrue(node.change_state(resumed, node.WAIT_ADJUDICATION, sink)[0])
            self.assertFalse(node.is_terminal(resumed))
            self.assertTrue(node.change_state(resumed, "待跑", sink)[0])

            failed = node.new_node("fail", "worker")
            self.assertTrue(node.change_state(failed, "在跑", sink)[0])
            self.assertTrue(node.change_state(failed, node.WAIT_ADJUDICATION, sink)[0])
            self.assertTrue(node.change_state(failed, "失败", sink)[0])
            self.assertTrue(node.is_terminal(failed))
            self.assertFalse(node.change_state(failed, "待跑", sink)[0])


    def test_start_reconcile_round_trip_preserves_scheduler_meta(self):
        with tempfile.TemporaryDirectory() as temp:
            scheduler = {
                "version": 1,
                "step_count": 4,
                "rejects": {},
                "repairs": {},
                "diagnoses": set(),
                "sticky": None,
                "elapsed_seconds": 12.0,
            }
            recover.dump_snapshot({"root": node.new_node("root", "root")}, d=temp,
                                  scheduler=scheduler)
            with patch.dict(os.environ, {
                    "TREE_RECONCILE_ON_START": "true",
                    "TREE_LLAAT_URL": "http://stub",
            }, clear=False), patch("harness.recover.reconcile", return_value={
                    "queries": 0, "changed": 0, "failed": 0,
            }) as reconcile:
                result = recover.maybe_reconcile_on_start(d=temp)
            self.assertTrue(result["enabled"])
            reconcile.assert_called_once()
            self.assertEqual(recover.load_checkpoint(Path(temp) / "state.json")["scheduler"],
                             scheduler)

    def test_legacy_and_scheduler_checkpoint_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            tree = {"root": node.new_node("root", "root")}
            tree["root"]["state"] = node.WAIT_ADJUDICATION
            tree["root"]["_audit_repair_rounds"] = 2
            tree["root"]["_review_of"] = ["merge-gap"]
            tree["root"]["_adjudication"] = {"phase": "pending", "receipt": "node-review"}
            tree["root"]["_review_verdict"] = {"verdict": "rework", "evidence": ["s1"], "note": "gap"}
            tree["root"]["_mech_fallback_tried"] = True
            tree["root"]["_unknown_transient"] = "drop"

            legacy_path = Path(recover.dump_snapshot(tree, d=temp))
            legacy_data = json.loads(legacy_path.read_text(encoding="utf-8"))
            self.assertEqual(set(legacy_data), {"nodes"})
            self.assertEqual(legacy_data["nodes"]["root"]["state"], node.WAIT_ADJUDICATION)
            self.assertEqual(legacy_data["nodes"]["root"]["_adjudication"]["phase"], "pending")
            self.assertEqual(legacy_data["nodes"]["root"]["_review_verdict"]["verdict"], "rework")
            self.assertTrue(legacy_data["nodes"]["root"]["_mech_fallback_tried"])
            self.assertNotIn("_unknown_transient", legacy_data["nodes"]["root"])
            self.assertEqual(recover.load_checkpoint(legacy_path)["scheduler"], None)
            self.assertEqual(recover.load_snapshot(legacy_path)["root"]["_audit_repair_rounds"], 2)

            legacy_file = Path(temp) / "old-state.json"
            old_nodes = {"root": {"id": "root", "state": "待跑", "round": 3}}
            legacy_file.write_text(json.dumps({"nodes": old_nodes}), encoding="utf-8")
            self.assertEqual(recover.load_checkpoint(legacy_file),
                             {"nodes": old_nodes, "scheduler": None})
            self.assertEqual(recover.load_snapshot(legacy_file), old_nodes)

            scheduler = {
                "version": 1,
                "step_count": 11,
                "rejects": {"root": 2},
                "repairs": {("root", "merge"): "root.repair"},
                "diagnoses": {("root", "merge")},
                "sticky": "root",
                "elapsed_seconds": 123.5,
                "unrecognized": "drop",
            }
            checkpoint_path = Path(recover.dump_snapshot(tree, d=temp, scheduler=scheduler))
            wire = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(wire["scheduler"]["repairs"], [["root", "merge", "root.repair"]])
            self.assertEqual(wire["scheduler"]["diagnoses"], [["root", "merge"]])
            self.assertNotIn("unrecognized", wire["scheduler"])
            self.assertNotIn("_unknown_transient", wire["nodes"]["root"])

            checkpoint = recover.load_checkpoint(checkpoint_path)
            self.assertEqual(checkpoint["nodes"]["root"]["state"], node.WAIT_ADJUDICATION)
            self.assertEqual(checkpoint["nodes"]["root"]["_adjudication"]["receipt"], "node-review")
            self.assertEqual(checkpoint["nodes"]["root"]["_review_verdict"]["evidence"], ["s1"])
            self.assertTrue(checkpoint["nodes"]["root"]["_mech_fallback_tried"])
            self.assertEqual(checkpoint["scheduler"],
                             {key: value for key, value in scheduler.items() if key != "unrecognized"})
            self.assertEqual(recover.load_snapshot(checkpoint_path), checkpoint["nodes"])


if __name__ == "__main__":
    unittest.main()
