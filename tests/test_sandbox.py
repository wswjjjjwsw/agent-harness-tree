"""沙箱 profile 的路径级写限制（F2 收口：只读输入/受保护产物写不进去）。

只断言 profile 文本，不真跑 sandbox-exec（非 macOS / CI 也能跑）。
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import session

_RULE_RE = re.compile(r'\((allow|deny) file-write\* \((subpath|literal) "([^"]*)"\)\)')


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    else:
        path.write_text(str(value), encoding="utf-8")


def _rules(profile: str) -> list:
    """profile ⇒ [(action, kind, abs_path)]，按出现顺序。"""
    return [(m.group(1), m.group(2), m.group(3)) for m in _RULE_RE.finditer(profile)]


def _allow_paths(profile: str) -> list:
    return [p for action, _kind, p in _rules(profile) if action == "allow"]


class SandboxProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = (base / "run").resolve()
        self.agent = (base / "pi-agent").resolve()
        self.sess = (self.agent / "sessions").resolve()
        self.agent.mkdir(parents=True, exist_ok=True)
        self.sess.mkdir(parents=True, exist_ok=True)
        self._env = patch.dict(os.environ, {
            "TREE_PI_AGENT_DIR": str(self.agent),
            "TREE_PI_AGENT_SESSION_DIR": str(self.sess),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------ B 的策略
    def _b_policy_run(self) -> Path:
        """B 验收的策略：input/ 与 product/answer.txt 只读，只有 blocks/prime-report.md 和 doc/ 可写。"""
        self.root.mkdir(parents=True, exist_ok=True)
        _write(self.root / "authority.json", {
            "authoritative": ["input/prime-oracle.json"],
            "read_only": ["input/", "product/answer.txt"],
            "writable": ["blocks/prime-report.md", "doc/"],
        })
        _write(self.root / "input/prime-oracle.json", {"prime_count": 168})
        _write(self.root / "product/answer.txt", "186\n")
        _write(self.root / "blocks/prime-report.md", "# 报告\n")
        return self.root

    def test_denies_whole_run_then_allows_whitelist(self):
        run = self._b_policy_run()
        prof = session._sandbox_profile_text(str(run))
        rules = _rules(prof)

        # ① 整个 run 目录默认不可写（input/ 与 product/answer.txt 都在其下，被这条 deny 盖住）
        self.assertIn(("deny", "subpath", str(run)), rules)
        self.assertTrue(str(run / "input").startswith(str(run) + "/"))
        # ② 只读 input/ 与 product/answer.txt 不在任何 allow 里
        allows = _allow_paths(prof)
        for protected in (run / "input", run / "product" / "answer.txt"):
            self.assertFalse([p for p in allows if p == str(protected) or
                              p.startswith(str(protected) + "/")],
                             "受保护路径不许出现 allow：%s" % protected)
        # ③ 白名单被抬回可写：单文件 literal、目录 subpath
        self.assertIn(("allow", "literal", str(run / "blocks" / "prime-report.md")), rules)
        self.assertIn(("allow", "subpath", str(run / "doc")), rules)
        # ④ 会话/配置目录必须可写，否则 pi 写不了会话
        self.assertIn(("allow", "subpath", str(self.sess)), rules)
        self.assertIn(("allow", "subpath", str(self.agent)), rules)

    def test_order_deny_run_before_writable_allow(self):
        run = self._b_policy_run()
        prof = session._sandbox_profile_text(str(run))
        deny_line = '(deny file-write* (subpath "%s"))' % run
        self.assertIn(deny_line, prof)
        # 后匹配优先：整目录 deny 必须排在所有 run 内 allow 之前
        self.assertLess(prof.index(deny_line),
                        prof.index('(allow file-write* (literal "%s"))'
                                   % (run / "blocks" / "prime-report.md")))
        # 而外部目录 deny 仍在最前
        home = os.environ.get("HOME") or str(Path.home())
        self.assertLess(prof.index('(deny file-write* (subpath "%s/agent-harness"))' % home),
                        prof.index(deny_line))

    # ------------------------------------------------------------ 缺省策略
    def test_default_policy_input_read_only_product_writable(self):
        run = self.root
        run.mkdir(parents=True, exist_ok=True)
        _write(run / "input" / "prime-oracle.json", {"prime_count": 168})
        prof = session._sandbox_profile_text(str(run))
        rules = _rules(prof)

        self.assertIn(("deny", "subpath", str(run)), rules)
        allows = _allow_paths(prof)
        self.assertFalse([p for p in allows if p == str(run / "input")
                          or p.startswith(str(run / "input") + "/")],
                         "缺省策略下 input/ 不可写")
        for writable in ("product", "blocks", "doc"):
            self.assertIn(("allow", "subpath", str(run / writable)), rules)

    def test_missing_authority_json_falls_back_to_default(self):
        run = self.root
        run.mkdir(parents=True, exist_ok=True)
        self.assertFalse((run / "authority.json").exists())
        prof = session._sandbox_profile_text(str(run))
        self.assertIn('(allow file-write* (subpath "%s"))' % (run / "product"), prof)


if __name__ == "__main__":
    unittest.main()
