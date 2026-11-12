"""Fresh runs can reuse an identical pre-staged input without overwriting it."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DriverResumeShellTests(unittest.TestCase):
    def _run(self, run_root, run_id, source, frame=None, resume=False):
        env = dict(os.environ)
        env.update({
            "TREE_ROOT": str(ROOT),
            "TREE_RUNS_DIR": str(run_root),
            "TREE_PY": sys.executable,
        })
        command = ["bash", str(ROOT / "driver.sh"), "--input", str(source),
                   "--run-id", run_id, "--dry"]
        if frame is not None:
            command += ["--框架", str(frame)]
        if resume:
            command.append("--resume")
        return subprocess.run(command, env=env, text=True, capture_output=True)

    def test_identical_pre_staged_input_and_frame_are_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            run_input = runs / "stage" / "input"
            run_input.mkdir(parents=True)
            source = root / "list.jsonl"
            source.write_text('{"task":"same"}\n', encoding="utf-8")
            frame = root / "frame.md"
            frame.write_text("same frame\n", encoding="utf-8")
            (run_input / source.name).write_text(source.read_text(encoding="utf-8"),
                                                encoding="utf-8")
            (run_input / frame.name).write_text(frame.read_text(encoding="utf-8"),
                                                encoding="utf-8")

            result = self._run(runs, "stage", source, frame)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(run_input / source.name), result.stdout)
            self.assertEqual((run_input / source.name).read_text(encoding="utf-8"),
                             source.read_text(encoding="utf-8"))
            self.assertEqual((run_input / frame.name).read_text(encoding="utf-8"),
                             frame.read_text(encoding="utf-8"))

    def test_different_pre_staged_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            run_input = runs / "stage" / "input"
            run_input.mkdir(parents=True)
            source = root / "list.jsonl"
            source.write_text('{"task":"new"}\n', encoding="utf-8")
            staged = run_input / source.name
            staged.write_text('{"task":"old"}\n', encoding="utf-8")

            result = self._run(runs, "stage", source)

            self.assertEqual(result.returncode, 2)
            self.assertIn("拒绝覆盖", result.stderr)
            self.assertEqual(staged.read_text(encoding="utf-8"), '{"task":"old"}\n')

    def test_existing_state_requires_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            run_dir = runs / "stage"
            (run_dir / "input").mkdir(parents=True)
            (run_dir / "state.json").write_text("{}", encoding="utf-8")
            source = root / "list.jsonl"
            source.write_text('{"task":"same"}\n', encoding="utf-8")

            result = self._run(runs, "stage", source)

            self.assertEqual(result.returncode, 2)
            self.assertIn("请用 --resume", result.stderr)


if __name__ == "__main__":
    unittest.main()
