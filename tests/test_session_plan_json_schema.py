from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import schema, session, submit

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "plan-json-schema.ts"


class PiJsonSchemaTests(unittest.TestCase):
    def _argv(self, act: str, knobs: dict = None, node: dict = None) -> list[str]:
        return session.build_argv(node or {"id": "root", "role": "根"}, act, knobs or {},
                                  "mock prompt", "mock-session")

    def test_schema_extension_is_limited_to_local_llaat_structured_acts(self):
        with patch.dict(os.environ, {
            "TREE_USE_LLAAT": "1",
            "TREE_LLAAT_ADAPTER_URL": "http://127.0.0.1:18905/v1",
        }, clear=False):
            self.assertEqual(self._argv("plan")[2:4], ["-e", str(EXTENSION)])
            self.assertEqual(self._argv("merge")[2:4], ["-e", str(EXTENSION)])
            self.assertEqual(self._argv("spawn")[2:4], ["-e", str(EXTENSION)])
            review = {"id": "root.fix.review1.1", "role": "审计",
                      "_structured_review": True}
            self.assertEqual(self._argv("leaf", node=review)[2:4],
                             ["-e", str(EXTENSION)])
            self.assertNotIn("-e", self._argv("leaf")[2:])
        with patch.dict(os.environ, {
            "TREE_USE_LLAAT": "1",
            "TREE_LLAAT_ADAPTER_URL": "https://models.example/v1",
        }, clear=False):
            self.assertNotIn("-e", self._argv("plan"))
            self.assertNotIn("-e", self._argv("merge"))
            self.assertNotIn("-e", self._argv("spawn"))
            self.assertNotIn("-e", self._argv("leaf", node=review))
        with patch.dict(os.environ, {
            "TREE_USE_LLAAT": "0",
            "TREE_LLAAT_ADAPTER_URL": "http://127.0.0.1:18905/v1",
        }, clear=False):
            self.assertNotIn("-e", self._argv("plan"))
            self.assertNotIn("-e", self._argv("merge"))
            self.assertNotIn("-e", self._argv("spawn"))
            self.assertNotIn("-e", self._argv("leaf", node=review))

    def test_structured_review_schema_binds_blocker_and_allowed_sources(self):
        blocker_id = "[root.c1.1.f2.2#1]"
        evidence_ids = ["file:doc/a.md:abc123", "tool:root.c1.1:1:def456"]
        generated = schema.structured_review_response_format(
            blocker_id, evidence_ids)["json_schema"]["schema"]
        self.assertEqual(generated["required"],
                         ["reply", "未决", "verdict", "covers", "evidence", "note"])
        self.assertEqual(generated["properties"]["verdict"]["enum"],
                         ["pass", "rework", "escalate"])
        covers = generated["properties"]["covers"]
        self.assertEqual((covers["minItems"], covers["maxItems"]), (1, 1))
        self.assertEqual(covers["items"]["const"], blocker_id)
        evidence = generated["properties"]["evidence"]
        self.assertEqual(evidence["minItems"], 1)
        self.assertEqual(evidence["items"]["properties"]["source"]["enum"], evidence_ids)
        self.assertEqual(evidence["items"]["required"], ["source", "observation"])
        self.assertEqual(
            generated["properties"]["未决"]["items"]["properties"]["type"]["enum"],
            list(schema.UNDECIDED_TYPES))

    def test_merge_response_schema_tracks_validator_conditions(self):
        close = {"去向": "收束", "reply": "summary", "未决": [], "uncovered": []}
        split = {"去向": "切分", "reply": "split", "未决": [],
                 "下一批要点": ["part"], "切分目标": ["root.c1"]}
        dispatch = {"去向": "再派", "reply": "continue", "未决": [],
                    "下一批要点": ["remaining"]}
        for value in (close, split, dispatch):
            self.assertTrue(schema.validate("merge", value)[0])
        self.assertFalse(schema.validate("merge", {k: v for k, v in close.items()
                                                     if k != "uncovered"})[0])
        self.assertFalse(schema.validate("merge", {k: v for k, v in dispatch.items()
                                                     if k != "下一批要点"})[0])

        required = schema.merge_response_format(audit_required=True)["json_schema"]["schema"]
        self.assertIn(schema.AUDIT_KEY, required["required"])
        self.assertEqual(required["properties"][schema.AUDIT_KEY]["minItems"], 1)
        self.assertEqual([branch["required"] for branch in required["anyOf"]], [
            ["去向", "下一批要点"],
            ["去向", "下一批要点", "切分目标"],
            ["去向", "uncovered"],
        ])
        findings = [{"item": "AUD-7 repeated timestamp", "处置": "上冒", "说明": "needs review"}]
        audited = dict(close, **{schema.AUDIT_KEY: findings})
        self.assertTrue(schema.validate("merge", audited, {schema.AUDIT_KEY: findings})[0])
        self.assertFalse(schema.validate("merge", close, {schema.AUDIT_KEY: findings})[0])
        self.assertEqual(required["properties"][schema.AUDIT_KEY]["items"]["properties"]["处置"]["enum"],
                         list(schema.DISPOSALS))

    def test_spawn_schema_does_not_require_judge(self):
        generated = schema.spawn_response_format()["json_schema"]["schema"]
        branches = generated["properties"]["children"]["items"]["anyOf"]
        write_roles = {name for name in schema.role_mod.ROLE_NAMES
                       if schema.role_mod.effective(name).get("may_write")}
        self.assertEqual(write_roles, {"执行"})
        write_branch = next(branch for branch in branches
                            if branch["properties"]["role"].get("const") in write_roles)
        judge = write_branch["properties"]["judge"]
        self.assertEqual(set(judge["properties"]), {"json_keys"})
        self.assertEqual(judge["properties"]["json_keys"]["minItems"], 1)
        self.assertEqual(judge["properties"]["json_keys"]["items"]["enum"],
                         ["reply", "未决", "doc"])
        self.assertFalse(judge["additionalProperties"])
        self.assertNotIn("judge", write_branch["required"])
        other = next(branch for branch in branches
                     if "const" not in branch["properties"]["role"])
        self.assertEqual(set(other["properties"]["role"]["enum"]),
                         set(schema.role_mod.ROLE_NAMES) - write_roles)
        self.assertNotIn("judge", other["properties"])
        self.assertNotIn("judge", other["required"])

    def test_managed_doc_schema_requires_inline_document(self):
        generated = schema.managed_doc_response_format(1500)["json_schema"]["schema"]
        self.assertEqual(generated["required"], ["reply", "未决", "doc"])
        self.assertEqual(generated["properties"]["doc"]["minLength"], 1)
        self.assertFalse(generated["additionalProperties"])

    def test_managed_doc_leaf_uses_response_schema_extension(self):
        node = {"id": "root.fix.audit1.1", "role": "执行",
                "_managed_doc": {"target": "blocks/supplement.md", "chars_min": 1500}}
        with patch.dict(os.environ, {
            "TREE_USE_LLAAT": "1",
            "TREE_LLAAT_ADAPTER_URL": "http://127.0.0.1:18905/v1",
        }, clear=False):
            argv = self._argv("leaf", knobs={"tools": ""}, node=node)
            self.assertEqual(argv[2:4], ["-e", str(EXTENSION)])
            self.assertIn("--no-tools", argv)
        with patch.dict(os.environ, {
            "TREE_USE_LLAAT": "1",
            "TREE_LLAAT_ADAPTER_URL": "https://models.example/v1",
        }, clear=False):
            self.assertNotIn("-e", self._argv("leaf", knobs={"tools": ""}, node=node))

    def test_pi_hook_adds_plan_merge_and_spawn_schemas_to_mock_payload(self):
        node = Path.home() / "node" / "bin" / "node"
        if not node.is_file():
            found = shutil.which("node")
            if not found:
                self.skipTest("Node is required to exercise the installed Pi extension hook")
            node = Path(found)
        jiti = (Path.home() / "node" / "lib" / "node_modules" / "@earendil-works"
                / "pi-coding-agent" / "node_modules" / "jiti" / "lib" / "jiti.mjs")
        if not jiti.is_file():
            self.skipTest("Installed Pi jiti loader is unavailable")

        merge_format = schema.merge_response_format(audit_required=True)
        spawn_format = schema.spawn_response_format()
        review_format = schema.structured_review_response_format(
            "[root.c1.1.f2.2#1]", ["file:doc/a.md:abc123", "tool:root.c1.1:1:def456"])
        script = f"""
import {{ createJiti }} from {json.dumps(jiti.as_uri())};
const jiti = createJiti(import.meta.url);
const extension = jiti({json.dumps(str(EXTENSION))});
delete process.env.TREE_PI_RESPONSE_FORMAT;
let planHook;
extension.default({{ on(name, callback) {{ if (name === "before_provider_request") planHook = callback; }} }});
const planInput = {{ model: "sage-test", messages: [{{ role: "user", content: "mock plan" }}] }};
const planResult = await planHook({{ type: "before_provider_request", payload: planInput }});
process.env.TREE_PI_RESPONSE_FORMAT = JSON.stringify({json.dumps(merge_format, ensure_ascii=False)});
let mergeHook;
extension.default({{ on(name, callback) {{ if (name === "before_provider_request") mergeHook = callback; }} }});
const mergeInput = {{ model: "sage-test", messages: [{{ role: "user", content: "mock merge" }}] }};
const mergeResult = await mergeHook({{ type: "before_provider_request", payload: mergeInput }});
process.env.TREE_PI_RESPONSE_FORMAT = JSON.stringify({json.dumps(spawn_format, ensure_ascii=False)});
let spawnHook;
extension.default({{ on(name, callback) {{ if (name === "before_provider_request") spawnHook = callback; }} }});
const spawnInput = {{ model: "sage-test", messages: [{{ role: "user", content: "mock spawn" }}] }};
const spawnResult = await spawnHook({{ type: "before_provider_request", payload: spawnInput }});
process.env.TREE_PI_RESPONSE_FORMAT = JSON.stringify({json.dumps(review_format, ensure_ascii=False)});
let reviewHook;
extension.default({{ on(name, callback) {{ if (name === "before_provider_request") reviewHook = callback; }} }});
const reviewInput = {{ model: "sage-test", messages: [{{ role: "user", content: "mock review" }}] }};
const reviewResult = await reviewHook({{ type: "before_provider_request", payload: reviewInput }});
console.log(JSON.stringify({{ planResult, mergeResult, spawnResult, reviewResult }}));
"""
        completed = subprocess.run(
            [str(node), "--input-type=module", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        observed = json.loads(completed.stdout)
        plan_schema = observed["planResult"]["response_format"]["json_schema"]["schema"]
        self.assertEqual(plan_schema["required"], ["要点"])
        self.assertFalse(plan_schema["additionalProperties"])
        self.assertEqual(observed["mergeResult"]["model"], "sage-test")
        self.assertEqual(observed["mergeResult"]["messages"],
                         [{"role": "user", "content": "mock merge"}])
        self.assertEqual(observed["mergeResult"]["response_format"], merge_format)
        self.assertEqual(observed["spawnResult"]["response_format"], spawn_format)
        self.assertEqual(observed["reviewResult"]["response_format"], review_format)

    def test_run_act_passes_spawn_schema_only_to_local_llaat_pi(self):
        captures = []

        def fake_run(argv, env, **kwargs):
            captures.append({"argv": argv, "env": dict(env)})
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        common_patches = [
            patch.object(session, "history_tokens", return_value=0),
            patch.object(submit, "estimate_tokens", return_value=0),
            patch.object(session, "ensure_adapter"),
            patch.object(session, "pi_env", side_effect=lambda *args: dict(os.environ)),
            patch.object(session, "_sandbox_wrap", side_effect=lambda argv, run_dir: argv),
            patch.object(session.subprocess, "run", side_effect=fake_run),
        ]
        for url in ("http://127.0.0.1:18905/v1", "https://models.example/v1"):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "TREE_USE_LLAAT": "1",
                "TREE_LLAAT_ADAPTER_URL": url,
                "TREE_PI_RESPONSE_FORMAT": "stale value",
            }, clear=False), common_patches[0], common_patches[1], common_patches[2], \
                    common_patches[3], common_patches[4], common_patches[5]:
                session.run_act({"id": "root", "role": "根"}, "spawn",
                                {"ctx_budget": 1, "max_tokens": 1}, "mock spawn",
                                session_id="mock-session", run_dir=tmp)

        local, remote = captures
        self.assertEqual(local["argv"][2:4], ["-e", str(EXTENSION)])
        self.assertEqual(json.loads(local["env"]["TREE_PI_RESPONSE_FORMAT"]),
                         schema.spawn_response_format())
        self.assertNotIn("-e", remote["argv"])
        self.assertNotIn("TREE_PI_RESPONSE_FORMAT", remote["env"])

    def test_run_act_passes_structured_review_schema_only_to_local_llaat_pi(self):
        captures = []

        def fake_run(argv, env, **kwargs):
            captures.append({"argv": argv, "env": dict(env)})
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        node = {"id": "root.fix.review1.1", "role": "审计",
                "_structured_review": True,
                "_review_blocker_id": "[root.c1.1.f2.2#1]",
                "_review_evidence_ids": ["file:doc/a.md:abc123", "tool:root.c1.1:1:def456"]}
        patches = [
            patch.object(session, "history_tokens", return_value=0),
            patch.object(submit, "estimate_tokens", return_value=0),
            patch.object(session, "ensure_adapter"),
            patch.object(session, "pi_env", side_effect=lambda *args: dict(os.environ)),
            patch.object(session, "_sandbox_wrap", side_effect=lambda argv, run_dir: argv),
            patch.object(session.subprocess, "run", side_effect=fake_run),
        ]
        for url in ("http://127.0.0.1:18905/v1", "https://models.example/v1"):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "TREE_USE_LLAAT": "1",
                "TREE_LLAAT_ADAPTER_URL": url,
                "TREE_PI_RESPONSE_FORMAT": "stale value",
            }, clear=False), patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                session.run_act(node, "leaf", {"ctx_budget": 1, "max_tokens": 1},
                                "mock review", session_id="mock-session", run_dir=tmp)

        local, remote = captures
        expected = schema.structured_review_response_format(
            node["_review_blocker_id"], node["_review_evidence_ids"])
        self.assertEqual(local["argv"][2:4], ["-e", str(EXTENSION)])
        self.assertEqual(json.loads(local["env"]["TREE_PI_RESPONSE_FORMAT"]), expected)
        self.assertNotIn("-e", remote["argv"])
        self.assertNotIn("TREE_PI_RESPONSE_FORMAT", remote["env"])

    def test_run_act_passes_audit_aware_merge_schema_to_pi_child(self):
        captured = {}

        def fake_run(argv, env, **kwargs):
            captured["argv"] = argv
            captured["env"] = dict(env)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        audit_findings = [{"id": "AUD-7", "item": "repeated timestamp"}]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "TREE_USE_LLAAT": "1",
            "TREE_LLAAT_ADAPTER_URL": "http://127.0.0.1:18905/v1",
            "TREE_PI_RESPONSE_FORMAT": "stale value",
        }, clear=False), patch.object(session, "history_tokens", return_value=0), \
                patch.object(submit, "estimate_tokens", return_value=0), \
                patch.object(session, "ensure_adapter"), \
                patch.object(session, "pi_env", side_effect=lambda *args: dict(os.environ)), \
                patch.object(session, "_sandbox_wrap", side_effect=lambda argv, run_dir: argv), \
                patch.object(session.subprocess, "run", side_effect=fake_run):
            session.run_act(
                {"id": "root", "role": "根"}, "merge",
                {"ctx_budget": 1, "max_tokens": 1, schema.AUDIT_KEY: audit_findings},
                "mock merge", session_id="mock-session", run_dir=tmp,
            )

        self.assertEqual(captured["argv"][2:4], ["-e", str(EXTENSION)])
        response_format = json.loads(captured["env"]["TREE_PI_RESPONSE_FORMAT"])
        self.assertIn(schema.AUDIT_KEY, response_format["json_schema"]["schema"]["required"])
        self.assertEqual(response_format, schema.merge_response_format(audit_required=True))


if __name__ == "__main__":
    unittest.main()
