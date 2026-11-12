import json
import tempfile
import unittest
from pathlib import Path

from harness import contract as contract_mod
from harness import driver, final_audit, leafrun, node as node_mod, recover
from harness import land, schema


class _NoTiming:
    def last_for(self, _node_id):
        return None


class ManagedDocPromptTests(unittest.TestCase):
    def test_managed_doc_contract_requires_inline_text(self):
        from harness import submit
        node = node_mod.new_node(
            "root.fix.audit1.1", "执行", parent="root", batch=4, act="leaf", round=1,
            context="修复 blocks/prime-report.md", judge={"json_keys": ["reply"]},
            验收标准="报告计数为 168", auth="只改 blocks/prime-report.md")
        node["_managed_doc"] = {"target": "blocks/prime-report.md"}
        prompt = submit.assemble_prompt(
            node, {"may_write": True}, act="leaf", include_role_prompt=False)
        self.assertIn("完整正文（不是路径）", prompt)
        self.assertNotIn("你写进去的那个路径", prompt)


class ManagedDocumentTests(unittest.TestCase):
    def _fixture(self, run_dir, output_doc):
        run = Path(run_dir)
        target = run / "blocks" / "draft.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original short document", encoding="utf-8")
        session = run / "sessions" / "source.jsonl"
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(json.dumps({
            "message": {"role": "toolResult", "content": "SIEVE_COUNT=168"}
        }) + "\n", encoding="utf-8")

        parent = node_mod.new_node("root", "根", state="在跑", round=2, batch=2)
        source = node_mod.new_node(
            "root.c2.1", "执行", parent="root", state="完成",
            context="原任务：整理原始计算事实并写出完整计算说明。",
            验收标准="文档不少于 100 字",
            judge={"chars_min": 100}, reply="已完成原计算说明。")
        tree = {"root": parent, "root.c2.1": source}
        contract_mod.append_ledger(
            str(run),
            contract_mod.ledger_entry(
                source, "complete", {"calls": 1, "successes": 1, "failures": 0},
                "source-session", ["blocks/draft.md"], [], session_path="sessions/source.jsonl"))
        spec = {
            "item": "blocks/draft.md 当前长度未达到 100 字",
            "blocker_id": "[root.f2.1#1]",
            "target": "blocks/draft.md",
            "target_abs": str(target.resolve()),
            "chars_min": 100,
            "repair_of": "root.c2.1",
        }
        calls = []

        def runner(node, act, knobs, prompt, session_id, runner_dir):
            calls.append({"tools": knobs.get("tools"), "prompt": prompt,
                          "node_id": node["id"], "act": act})
            return json.dumps({"reply": "已更新绑定文档。", "未决": [],
                               "doc": output_doc}, ensure_ascii=False)

        d = driver.Driver(parent, tree, run_id="managed-doc-test",
                          run_dir=str(run), act_runner=runner)
        return d, tree, parent, spec, target, calls

    def test_repair_leaf_writes_only_bound_target_after_judge(self):
        with tempfile.TemporaryDirectory() as td:
            body = "计算过程与依据。" * 20
            d, tree, parent, spec, target, calls = self._fixture(td, body)
            ids = d._dispatch_repair_leaves(parent, [spec])
            self.assertEqual(len(ids), 1)
            leaf = tree[ids[0]]
            self.assertEqual(leaf["_managed_doc"], {"target": "blocks/draft.md"})
            self.assertIn("原任务：整理原始计算事实", leaf["context"])
            self.assertIn("original short document", leaf["context"])
            self.assertIn("SIEVE_COUNT=168", leaf["context"])
            self.assertIn("补足为完整修复后正文", leaf["context"])
            self.assertNotIn("长度至少", leaf["context"])

            row = leafrun.run_leaf(d, leaf)
            self.assertEqual(calls[0]["tools"], "")
            self.assertIn("本叶没有文件工具", calls[0]["prompt"])
            self.assertEqual(target.read_text(encoding="utf-8"), body)
            self.assertEqual(row["judge_ok"], True)
            self.assertEqual(row["managed_doc_write"]["target"], "blocks/draft.md")
            self.assertEqual(row["managed_doc_write"]["chars"], len(body))
            self.assertFalse((Path(td) / "doc" / (leaf["id"] + ".md")).exists())

    def test_short_repair_output_is_written_because_length_is_not_judged(self):
        with tempfile.TemporaryDirectory() as td:
            d, tree, parent, spec, target, calls = self._fixture(td, "too short")
            ids = d._dispatch_repair_leaves(parent, [spec])
            row = leafrun.run_leaf(d, tree[ids[0]])
            self.assertEqual(target.read_text(encoding="utf-8"), "too short")
            self.assertTrue(row["judge_ok"])
            self.assertEqual(tree[ids[0]]["state"], "完成")
            self.assertTrue((row.get("managed_doc_write") or {}).get("ok"))

    def test_pointer_output_is_rejected_and_target_kept(self):
        with tempfile.TemporaryDirectory() as td:
            d, tree, parent, spec, target, calls = self._fixture(td, "blocks/draft.md")
            original = target.read_bytes()
            ids = d._dispatch_repair_leaves(parent, [spec])
            row = leafrun.run_leaf(d, tree[ids[0]])
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse(row["judge_ok"])
            self.assertEqual(tree[ids[0]]["state"], "失败")

    def test_managed_binding_round_trips_and_write_is_run_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            d, tree, parent, spec, target, _calls = self._fixture(td, "A" * 120)
            ids = d._dispatch_repair_leaves(parent, [spec])
            checkpoint_dir = str(Path(td) / "state")
            checkpoint = recover.dump_snapshot(tree, d=checkpoint_dir)
            restored = recover.load_checkpoint(checkpoint)["nodes"]
            self.assertEqual(restored[ids[0]]["_managed_doc"],
                             {"target": "blocks/draft.md"})
            with self.assertRaises(ValueError):
                land.land_run_file(td, "../outside.md", "not allowed")
            ok, why = d.write_managed_doc(ids[0], "blocks/other.md", "wrong target")
            self.assertFalse(ok)
            self.assertIn("does not match", why)
            self.assertEqual(target.read_text(encoding="utf-8"), "original short document")

    def test_product_artifact_is_never_bound_as_a_controlled_document(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            (run / "product").mkdir()
            answer = run / "product" / "answer.txt"
            answer.write_text("168\n", encoding="utf-8")
            parent = node_mod.new_node("root", "根", state="在跑")
            tree = {"root": parent}
            d = driver.Driver(parent, tree, run_id="managed-product-test",
                              run_dir=str(run), act_runner=lambda *a: "")
            spec = {"item": "product/answer.txt 的具体内容无法核对",
                    "blocker_id": "[root.f3.1#1]",
                    "target": "product/answer.txt",
                    "target_abs": str(answer.resolve()),
                    "repair_of": ""}
            ids = d._dispatch_repair_leaves(parent, [spec])
            self.assertEqual(len(ids), 1)
            self.assertNotIn("_managed_doc", tree[ids[0]])
            self.assertEqual(answer.read_text(encoding="utf-8"), "168\n")

    def test_fact_conflict_repair_is_told_to_recompute_or_escalate(self):
        with tempfile.TemporaryDirectory() as td:
            tree, parent = self._make_tree(td)
            spec = {"item": "product/answer.txt 与 blocks/draft.md 数值不一致：168 vs 186",
                    "blocker_id": "[root.f3.1#1]",
                    "target": "blocks/draft.md",
                    "target_abs": str(Path(td) / "blocks" / "draft.md"),
                    "repair_of": ""}
            child = final_audit.build_repair_child(spec, td, tree=tree, parent=parent,
                                                   managed_doc=True)
            self.assertIn("先用工具重算或回读权威输入", child["context"])
            self.assertIn("不要改写目标文件", child["context"])
            self.assertIn("交上级裁定", child["context"])

            plain = dict(spec, item="blocks/draft.md 段落缺失")
            child2 = final_audit.build_repair_child(plain, td, tree=tree, parent=parent,
                                                    managed_doc=True)
            self.assertNotIn("交上级裁定", child2["context"])

    def test_bound_fact_block_attaches_real_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            (run / "input").mkdir()
            (run / "input" / "oracle.json").write_text('{"prime_count": 168}', encoding="utf-8")
            context = "请把 product/answer.txt 改成 186，使它与 input/oracle.json 中 count=186 一致。"
            block = driver.bound_fact_block(context, str(run))
            self.assertIn("本 run 内被引用文件的真实内容", block)
            self.assertIn('{"prime_count": 168}', block)
            self.assertIn("以上面实际内容为准", block)

            self.assertEqual(driver.bound_fact_block("没有任何文件引用", str(run)),
                             "没有任何文件引用")

    def test_readonly_output_drops_doc_and_edits(self):
        obj = {"reply": "审计结论", "未决": [], "doc": "不该出现的正文"}
        out, note = schema.normalize_readonly_output(obj, {"may_write": False})
        self.assertNotIn("doc", out)
        self.assertIn("已丢弃", note)
        obj2 = {"reply": "x", "doc": "允许的正文"}
        out2, note2 = schema.normalize_readonly_output(obj2, {"may_write": True})
        self.assertIn("doc", out2)
        self.assertEqual(note2, "")

    def test_spawn_defaults_missing_judge_to_shape_judge(self):
        obj = {"children": [{
            "role": "执行", "context": "x", "验收标准": "x", "auth": "x"}]}
        ok, why = schema.validate("spawn", obj, {})
        self.assertTrue(ok, why)
        self.assertEqual(obj["children"][0]["judge"],
                         {"json_keys": ["reply", "未决"]})

    def test_spawn_rejects_oversized_child_context(self):
        obj = {"children": [{
            "role": "执行", "context": "x" * 4001,
            "judge": {}, "验收标准": "x", "auth": "x"}]}
        ok, why = schema.validate("spawn", obj, {})
        self.assertFalse(ok)
        self.assertIn("超长", why)

    def test_parse_act_output_escapes_raw_newlines_in_strings(self):
        raw = '{"reply": "line1\nline2", "未决": []}'
        obj = driver.parse_act_output(raw, "leaf")
        self.assertEqual(obj["reply"], "line1\nline2")
        self.assertEqual(obj["未决"], [])

    def test_bound_fact_block_attaches_three_small_cited_files(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            (run / "input").mkdir()
            (run / "product").mkdir()
            (run / "blocks").mkdir()
            (run / "input" / "oracle.json").write_text(
                '{"prime_count": 168}', encoding="utf-8")
            (run / "product" / "answer.txt").write_text("168\n", encoding="utf-8")
            (run / "blocks" / "report.md").write_text(
                "The range contains 186 primes.\n", encoding="utf-8")
            context = ("读取 input/oracle.json、product/answer.txt、"
                       "blocks/report.md 后核对")
            block = driver.bound_fact_block(context, str(run))
            self.assertIn("prime_count", block)
            self.assertIn("168", block)
            self.assertIn("186", block)

    def _make_tree(self, td):
        parent = node_mod.new_node("root", "根", state="在跑", round=3, batch=2)
        tree = {"root": parent}
        target = Path(td) / "blocks" / "draft.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original", encoding="utf-8")
        return tree, parent

    def test_mechanical_dispatch_classification(self):
        with tempfile.TemporaryDirectory() as td:
            parent = node_mod.new_node("root", "根", state="在跑")
            tree = {"root": parent}
            d = leafrun.TreeDriver(parent, tree, run_dir=td, act_runner=lambda *args: "",
                                  timed=_NoTiming())
            row = {"runner": "mechanical", "hard_doc_preempt": ["blocks/draft.md"],
                   "why": "【修复派发】已建受控文档修复叶"}
            d._log_round(row)
            self.assertEqual(row["exit_class"], leafrun.EXIT_DONE)
            failed = {"runner": "mechanical", "hard_doc_preempt": ["blocks/draft.md"],
                      "why": "【明确失败】修复预算耗尽"}
            d._log_round(failed)
            self.assertEqual(failed["exit_class"], leafrun.EXIT_JUDGE)


if __name__ == "__main__":
    unittest.main()
