from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import leafrun, role, schema


class PlanRetryFeedbackTests(unittest.TestCase):
    def test_same_malformed_receipt_gets_feedback_then_stops_at_two_attempts(self):
        prefix = '{"要点": ["'
        # The captured LLAAT attempts were both stop-finished, 188/8192 output
        # tokens, and 409 chars. The driver API receives only the response text.
        raw = prefix + ("a" * 75) + '" "' + ("b" * 319) + '"]}'
        self.assertEqual(len(raw), 409)
        with self.assertRaises(json.JSONDecodeError) as caught:
            json.loads(raw)
        self.assertEqual(caught.exception.pos, 86)

        prompts = []
        responses = []

        def fake_runner(node, act, knobs, prompt, session_id, run_dir, timeout=None):
            self.assertEqual(act, "plan")
            prompts.append(prompt)
            responses.append(raw)
            return raw

        with tempfile.TemporaryDirectory() as run_dir:
            scheduler = leafrun.Scheduler(
                run_dir, "plan-malformed-replay", role.load_roles(), "agg2 plan replay",
                max_steps=10, max_seconds=30, max_reject=2, inner_runner=fake_runner)
            root = scheduler.build_root()
            result = scheduler.run()

        self.assertEqual(responses, [raw, raw])
        self.assertEqual(len(prompts), 2)
        self.assertIn(schema.SHAPES["plan"], prompts[0])
        self.assertNotIn("【上一轮被拒】", prompts[0])
        self.assertIn("【上一轮被拒】act=plan", prompts[1])
        self.assertIn("原文 409 字", prompts[1])
        self.assertIn("找不到一个 JSON 对象", prompts[1])
        self.assertIn(schema.SHAPES["plan"], prompts[1])
        self.assertEqual(root["state"], "失败")
        self.assertEqual(result["stop_reason"], "没有任何可推进的候选（全树既非全终态、又无待处理裁决）")


if __name__ == "__main__":
    unittest.main()
