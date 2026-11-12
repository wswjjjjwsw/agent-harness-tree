"""Offline tests for the durable Leo adjudication receipt."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.adjudication import start_or_read


def _wrapper(task="node-1", rc=0, decision="pass", blocking=False, note="checked", missing=None):
    if missing is None:
        missing = []
    return {
        "rc": rc,
        "verdict": {
            "kind": "judge",
            "task": task,
            "verdict": decision,
            "defect_class": "execution",
            "priority": "P1",
            "score": 90,
            "blocking": blocking,
            "missing": missing,
            "note": note,
        },
        "backend": {
            "provider": "deepseek",
            "host": "api.deepseek.com",
            "model": "deepseek-test",
            "authentication_configured": True,
        },
        "frontend": "claude",
        "role_hashes": {},
    }


class AdjudicationTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runtime = self.base / "fake-runtime"
        self.runtime.mkdir()
        (self.runtime / "config.json").write_text(
            json.dumps({"frontend": "claude"}), encoding="utf-8")
        (self.runtime / "leo_runtime.py").write_text(
            """import json, os
from pathlib import Path

def load_selection(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def dispatch(request, role_dir, db, frontend, require_manifest=False):
    event = {
        "request": request,
        "role_dir": role_dir,
        "db": db,
        "frontend": frontend,
        "require_manifest": require_manifest,
        "request_was_saved": (Path(os.environ["FAKE_RECEIPT_DIR"]) / "request.json").is_file(),
    }
    with Path(os.environ["FAKE_CALLS"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\\n")
    return json.loads(os.environ["FAKE_LEO_WRAPPER"])
""",
            encoding="utf-8",
        )
        self.calls = self.base / "calls.jsonl"
        self.env = os.environ.copy()
        self.env.update({
            "LEO_PROFILE_RUNTIME": str(self.runtime),
            "FAKE_CALLS": str(self.calls),
            "FAKE_LEO_WRAPPER": json.dumps(_wrapper(), ensure_ascii=False),
            "PYTHONPATH": os.pathsep.join(
                [str(ROOT), self.env.get("PYTHONPATH", "")]).rstrip(os.pathsep),
        })
        self.card = {
            "id": "node-1",
            "question": "是否接受交付？",
            "problem": "按节点验收标准裁决。",
            "auth_affected": False,
            "artifact_hash": "opaque-product-hash",
        }

    def _env_for(self, receipt, wrapper=None):
        env = dict(self.env)
        env["FAKE_RECEIPT_DIR"] = str(receipt)
        if wrapper is not None:
            env["FAKE_LEO_WRAPPER"] = json.dumps(wrapper, ensure_ascii=False)
        return env

    def _calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def _finish(self, receipt):
        result = start_or_read(self.card, str(receipt), live=True)
        for _ in range(250):
            if result.get("status") != "pending":
                return result
            time.sleep(0.02)
            result = start_or_read(self.card, str(receipt), live=True)
        self.fail("Leo worker did not finish within the test deadline")

    def test_saved_request_worker_wrapper_and_duplicate_read(self):
        receipt = self.base / "receipt"
        with patch.dict(os.environ, self._env_for(receipt), clear=False):
            prepared = start_or_read(self.card, str(receipt), live=False)
            self.assertEqual(prepared["status"], "pending")
            self.assertEqual(prepared["phase"], "prepared")
            self.assertTrue((receipt / "request.json").is_file())
            self.assertEqual(self._calls(), [])

            result = self._finish(receipt)
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["verdict"], "pass")
            self.assertFalse(result["blocking"])
            self.assertEqual(result["note"], "checked")
            self.assertEqual(result["missing"], [])
            self.assertFalse(result["requires_user_approval"])
            self.assertTrue(Path(result["raw_receipt"]).is_file())

            saved = json.loads((receipt / "request.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["task"], "node-1")
            self.assertEqual(saved["card"], self.card)
            call = self._calls()[0]
            self.assertTrue(call["request_was_saved"])
            self.assertEqual(call["request"]["task"], "node-1")
            self.assertEqual(call["request"]["packet"]["card"], self.card)
            self.assertEqual(call["frontend"], "claude")
            self.assertTrue(call["require_manifest"])

            again = start_or_read(self.card, str(receipt), live=True)
            self.assertEqual(again["status"], "done")
            self.assertEqual(again["verdict"], "pass")
            self.assertEqual(len(self._calls()), 1)

    def test_result_survives_parent_process_reentry(self):
        receipt = self.base / "receipt"
        with patch.dict(os.environ, self._env_for(receipt), clear=False):
            original = self._finish(receipt)
            self.assertEqual(original["status"], "done")
            command = (
                "import json,sys; "
                "from harness.adjudication import start_or_read; "
                "print(json.dumps(start_or_read(json.loads(sys.argv[1]), sys.argv[2], live=True)))"
            )
            proc = subprocess.run(
                [sys.executable, "-c", command, json.dumps(self.card), str(receipt)],
                cwd=str(ROOT), env=self._env_for(receipt), capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            replay = json.loads(proc.stdout)
            self.assertEqual(replay["status"], "done")
            self.assertEqual(replay["raw_receipt"], original["raw_receipt"])
            self.assertEqual(len(self._calls()), 1)

    def test_rc3_or_task_mismatch_is_error_and_kept_as_raw_receipt(self):
        cases = [
            (_wrapper(rc=3), "Leo dispatch returned rc=3"),
            (_wrapper(task="other-node"), "Leo verdict kind or task does not match the card"),
            (_wrapper(rc=1, decision="pass"), "Leo rc and verdict do not match"),
        ]
        for index, (wrapper, expected_error) in enumerate(cases):
            with self.subTest(index=index):
                receipt = self.base / ("receipt-%d" % index)
                env = self._env_for(receipt, wrapper)
                with patch.dict(os.environ, env, clear=False):
                    result = self._finish(receipt)
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error"], expected_error)
                    self.assertTrue(Path(result["raw_receipt"]).is_file())
                    self.assertEqual(len(self._calls()), index + 1)
                    replay = start_or_read(self.card, str(receipt), live=True)
                    self.assertEqual(replay["status"], "error")
                    self.assertEqual(len(self._calls()), index + 1)

    def test_dead_worker_without_result_is_error_without_resend(self):
        receipt = self.base / "receipt"
        with patch.dict(os.environ, self._env_for(receipt), clear=False):
            self.assertEqual(start_or_read(self.card, str(receipt))["status"], "pending")
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            worker_pid = dead.pid
            self.assertEqual(dead.wait(timeout=5), 0)
            (receipt / "worker.json").write_text(
                json.dumps({"pid": worker_pid}), encoding="utf-8")
            (receipt / "worker.json").write_text(
                json.dumps({"state": "running", "pid": worker_pid}), encoding="utf-8")
            result = start_or_read(self.card, str(receipt), live=True)
            self.assertEqual(result["status"], "error")
            self.assertIn("not resent", result["error"])
            self.assertEqual(self._calls(), [])

    def test_auth_affected_card_can_only_return_escalate(self):
        receipt = self.base / "receipt"
        card = dict(self.card, auth_affected=True)
        with patch.dict(os.environ, self._env_for(receipt), clear=False):
            result = start_or_read(card, str(receipt), live=True)
            for _ in range(250):
                if result.get("status") != "pending":
                    break
                time.sleep(0.02)
                result = start_or_read(card, str(receipt), live=True)
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["verdict"], "escalate")
            self.assertTrue(result["blocking"])
            self.assertTrue(result["requires_user_approval"])
            self.assertIn("本人决策", result["note"])


if __name__ == "__main__":
    unittest.main()
