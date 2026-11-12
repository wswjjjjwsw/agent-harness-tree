"""Resume restores the durable tree and scheduler counters without rebuilding root."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import leafrun, node, recover


class LeafrunResumeTests(unittest.TestCase):
    def _scheduler(self, run_dir, t0=None):
        return leafrun.Scheduler(
            run_dir, "resume-test", {}, "", inner_runner=lambda *_args, **_kwargs: {},
            t0=t0,
        )

    def test_checkpoint_restores_scheduler_budgets_and_control_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = node.new_node("root", "根", state=node.WAIT_ADJUDICATION)
            root["_last_reject"] = {"act": "merge", "detail": "missing"}
            root["_merge_continuation"] = {"round": 3, "candidate": {"reply": "staged"}}
            before = self._scheduler(temp, t0=time.time() - 45)
            before.tree = {"root": root}
            before.step_count = 9
            before.rejects = {"root": 1}
            before.repairs = {("root", "reply"): "root.fix1"}
            before.diagnoses = {("root", "reply")}
            before.sticky = "root"
            before.heartbeat()
            heartbeat_dir = Path(temp) / "heartbeat"
            (heartbeat_dir / "round-17.json").write_text("{}", encoding="utf-8")

            checkpoint = recover.load_checkpoint(Path(temp) / "state.json")
            after = self._scheduler(temp)
            after.restore_checkpoint(checkpoint)

            self.assertEqual(after.step_count, 9)
            self.assertEqual(after.rejects, {"root": 1})
            self.assertEqual(after.repairs, {("root", "reply"): "root.fix1"})
            self.assertEqual(after.diagnoses, {("root", "reply")})
            self.assertEqual(after.sticky, "root")
            self.assertLess(after.t0, time.time() - 40)
            self.assertEqual(after.tree["root"]["_last_reject"]["detail"], "missing")
            self.assertEqual(after.tree["root"]["_merge_continuation"]["round"], 3)
            self.assertEqual(checkpoint["scheduler"]["step_count"], 9)
            after.heartbeat()
            self.assertTrue((Path(temp) / "heartbeat" / "round-18.json").is_file())
            self.assertEqual(recover.load_checkpoint(Path(temp) / "state.json")["scheduler"]["step_count"], 9)

    def test_resume_cli_does_not_build_root_or_write_delivery_while_waiting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = node.new_node("root", "根", state=node.WAIT_ADJUDICATION)
            recover.dump_snapshot(
                {"root": root}, d=temp,
                scheduler={"version": 1, "step_count": 2, "rejects": {},
                           "repairs": {}, "diagnoses": set(), "sticky": None,
                           "elapsed_seconds": 3.0},
            )

            class FakeScheduler:
                def __init__(self, *_args, **_kwargs):
                    self.tree = {}
                    self.waiting = True
                    self.restored = None

                def restore_checkpoint(self, checkpoint):
                    self.restored = checkpoint
                    self.tree = checkpoint["nodes"]

                def resume_missing_incremental_audits(self):
                    return []

                def run(self):
                    return {"steps": 2, "stop_reason": "WAITING: adjudication",
                            "nodes": 1, "seconds": 3.0}

            with patch("harness.leafrun.Scheduler", FakeScheduler), \
                 patch("harness.leafrun.role_mod.load_roles", return_value={}), \
                 patch("harness.leafrun.recover.maybe_reconcile_on_start",
                       return_value={"enabled": False}):
                rc = leafrun.main(["--resume", "--run-id", "resume-test",
                                   "--run-dir", temp])

            self.assertEqual(rc, 4)
            self.assertFalse((Path(temp) / "out" / "最终答复.md").exists())
            self.assertFalse((Path(temp) / "单价实测.json").exists())


if __name__ == "__main__":
    unittest.main()
