#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CC 接线（第 3 步）—— 全部用例（离线）。

自包含、可重复跑：不联网、不打 8903/8092、不起 pi、不调 CC、不依赖外部状态。
只测：生成器（harness/cc_agents.py）、runner 的离线部分（harness/cc_run.py 的 argv/
预算/归一化/证据核验/工具调用上限）、以及 stateless prompt 的两处小改
（harness/submit.py include_role_prompt、harness/driver.py build_prompt stateless）。
真实 Sage 调用只在 demo/cc_parent_child.py 里跑，不在这里。

★ 任一用例 FAIL ⇒ 退出码 ≠ 0；全绿 ⇒ rc=0，且最后一行打印 `GATES: <pass>/<total> PASS`。
用法：cd <候选 worktree 根> && python3 tests/cases-ccwiring.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TREE_ROOT") or HERE.parent)
sys.path.insert(0, str(ROOT))

from harness import cc_agents, cc_run, driver as driver_mod, node as node_mod  # noqa: E402
from harness import role as role_mod, schema, submit  # noqa: E402

TMP = HERE / "tmp-ccwiring"
CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _table():
    return role_mod.load_roles()


def _knobs(role_name, act=None):
    return dict(role_mod.effective(role_name, act, table=_table()))


# ---------------------------------------------------------------- 生成器
@case("gen_all_six_files")
def _(x):
    with tempfile.TemporaryDirectory() as td:
        rep = cc_agents.generate_agents(_table(), str(ROOT / "role.yaml"), td)
        expect(set(rep) == {"根", "规划", "决策", "执行", "调研", "审计"},
               "应生成 6 个 role，实际 %s" % sorted(rep))
        for role, info in rep.items():
            p = Path(info["file"])
            expect(p.is_file(), "%s 的 agent 文件不存在：%s" % (role, p))
            expect(p.name == cc_agents.agent_filename(role), "文件名不对：%s" % p.name)
            text = p.read_text(encoding="utf-8")
            expect(text.startswith("---\nname: %s\n" % cc_agents.agent_name(role)),
                   "%s 的 frontmatter name 不对" % role)
            expect("model: sonnet" in text, "%s 的 frontmatter 缺 model: sonnet" % role)
            # 正文 = prompts/<role>.md 全文逐字（取第一行与最后一行核对）。
            body = text.split("---", 2)[-1]
            prompt_text = (ROOT / "prompts" / ("%s.md" % role)).read_text(encoding="utf-8")
            expect(prompt_text.strip() in body, "%s 的正文不是 prompts 原文逐字" % role)


@case("gen_no_tools_default")
def _(x):
    """(a) 缺省：全部 role 的 agent 文件 tools=[]（重测结论）。"""
    with tempfile.TemporaryDirectory() as td:
        rep = cc_agents.generate_agents(_table(), str(ROOT / "role.yaml"), td)
        for role, info in rep.items():
            expect(info["tools"] == [], "%s 缺省应无工具，实际 %r" % (role, info["tools"]))
            text = Path(info["file"]).read_text(encoding="utf-8")
            expect("tools: []" in text, "%s 的 frontmatter 应为 tools: []" % role)


@case("gen_tools_mapping_opt_in")
def _(x):
    """no_tools=False 时按 PI_TO_CC_TOOLS 映射（工具 role 可靠之后再开的路径）。"""
    with tempfile.TemporaryDirectory() as td:
        rep = cc_agents.generate_agents(_table(), str(ROOT / "role.yaml"), td,
                                        no_tools=False)
        expect(rep["执行"]["tools"] == ["Read", "Bash"],
               "执行应为 [Read, Bash]，实际 %r" % rep["执行"]["tools"])
        expect(rep["调研"]["tools"] == ["Read", "Bash"],
               "调研应为 [Read, Bash]，实际 %r" % rep["调研"]["tools"])
        for role in ("根", "规划", "决策", "审计"):
            expect(rep[role]["tools"] == [], "%s 应无工具，实际 %r"
                   % (role, rep[role]["tools"]))


@case("gen_write_not_mapped")
def _(x):
    """write 不映射（doc 统领）：no_tools=False 时执行也不应有 Write/Edit。"""
    tools = cc_agents.cc_tools_for_role(_knobs("执行"), no_tools=False)
    expect("Write" not in tools and "Edit" not in tools,
           "执行不应有 Write/Edit，实际 %r" % tools)
    expect("Read" in tools and "Bash" in tools, "执行应有 Read/Bash，实际 %r" % tools)


@case("gen_unknown_role_raises")
def _(x):
    try:
        cc_agents.agent_name("不存在")
    except KeyError:
        return
    raise AssertionError("未知 role 应响亮报错（KeyError），实际没报")


@case("gen_for_run_layout")
def _(x):
    """generate_for_run 写到 <run_dir>/.claude/agents/（随 run 隔离）。"""
    with tempfile.TemporaryDirectory() as td:
        rep = cc_agents.generate_for_run(_table(), str(ROOT / "role.yaml"), td)
        expect(Path(td, ".claude", "agents", "tree-zhixing.md").is_file(),
               "run 下应有 .claude/agents/tree-zhixing.md")
        expect(rep["执行"]["tools"] == [], "run 生成的执行 agent 应无工具")


# ---------------------------------------------------------------- runner：归一化与证据核验
@case("normalize_missing_fields")
def _(x):
    """叶产出缺 未决/edits ⇒ 机械补 [] 并记一笔（第 2 步 minor 项 3）。"""
    knobs = _knobs("执行")
    expect(knobs.get("may_write") is True, "执行应为 may_write=true")
    out, notes = cc_run.normalize_leaf_output({"reply": "ok", "doc": "正文"}, knobs)
    expect(out.get("未决") == [], "未决缺失应补 []，实际 %r" % out.get("未决"))
    expect(out.get("edits") == [], "edits 缺失应补 []，实际 %r" % out.get("edits"))
    expect(len(notes) == 2, "应记 2 笔，实际 %r" % notes)
    expect(out["reply"] == "ok" and out["doc"] == "正文", "已有字段不应被改")


@case("normalize_no_write_no_edits")
def _(x):
    """may_write=false 的 role：缺 edits 不补（只有 未决 补）。"""
    knobs = _knobs("调研")
    expect(knobs.get("may_write") is False, "调研应为 may_write=false")
    out, notes = cc_run.normalize_leaf_output({"reply": "ok"}, knobs)
    expect(out.get("未决") == [], "未决缺失应补 []")
    expect("edits" not in out, "may_write=false 时不应补 edits")
    expect(len(notes) == 1, "应只记 1 笔，实际 %r" % notes)


@case("normalize_invalid_input")
def _(x):
    try:
        cc_run.normalize_leaf_output(["不是 dict"], _knobs("执行"))
    except ValueError:
        return
    raise AssertionError("非 dict 输入应响亮报错（ValueError），实际没报")


@case("evidence_flags_unverified")
def _(x):
    """doc 声称 sysctl 但一次工具都没调 ⇒ 判未核验（第 2 步 minor 项 4）。"""
    res = cc_run.verify_tool_claims("实测 sysctl 得到 12 核", [])
    expect("sysctl" in res["unverified"], "应判 sysctl 未核验，实际 %r" % res)
    expect("实测" in res["unverified"], "泛指“实测”无工具调用时应判未核验")


@case("evidence_passes_with_call")
def _(x):
    """doc 声称 sysctl 且 Bash 真调了 sysctl ⇒ 判已核验。"""
    calls = [{"name": "Bash", "input": {"command": "sysctl -n hw.ncpu"}}]
    res = cc_run.verify_tool_claims("实测 sysctl 得到 12 核", calls)
    expect("sysctl" in res["verified"], "应判 sysctl 已核验，实际 %r" % res)
    expect(not res["unverified"], "不应有未核验，实际 %r" % res["unverified"])


# ---------------------------------------------------------------- runner：argv 与预算
@case("argv_no_tools")
def _(x):
    """无工具 role 的 argv：--agent tree-<role>、--model 标签；不传 --tools
    （agent 文件是工具的唯一事实来源；--tools "" 会吃掉 prompt，见 cc_run 注释）。"""
    argv = cc_run.build_cc_argv("根", [], "hello")
    expect("--agent" in argv and argv[argv.index("--agent") + 1] == "tree-gen",
           "应有 --agent tree-gen，实际 %r" % argv)
    expect("--tools" not in argv, "不应传 --tools（会吃掉 prompt），实际 %r" % argv)
    expect("--model" in argv and argv[argv.index("--model") + 1] == cc_run.CC_MODEL_LABEL,
           "应有 --model %s" % cc_run.CC_MODEL_LABEL)
    expect(argv[-1] == "hello", "prompt 应是最后一个参数")


@case("argv_with_tools")
def _(x):
    """有工具 role 的 argv：同样不传 --tools（工具由 agent 文件定）；prompt 仍是最后一位。"""
    argv = cc_run.build_cc_argv("执行", ["Read", "Bash"], "hello")
    expect("--tools" not in argv, "不应传 --tools，实际 %r" % argv)
    expect(argv[-1] == "hello", "prompt 应是最后一个参数")
    expect("" not in argv, "argv 不应含空串（空串会被 commander 当 tools 值吃掉 prompt）")


# ---------------------------------------------------------------- runner：权限 fail-closed（2026-11-07）
def _set_env(updates):
    """设 env，返回旧值（供 _restore_env 恢复）；值为 None 表示删除该变量。"""
    old = {}
    for key, val in updates.items():
        old[key] = os.environ.get(key)
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    return old


def _restore_env(old):
    for key, val in old.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _fake_cc_stdout(tool_cmds):
    """拼一条 fake CC stream-json stdout：tool_cmds 是 Bash 命令列表，result 为 {}。"""
    lines = []
    for i, cmd in enumerate(tool_cmds):
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t%d" % i, "name": "Bash",
                 "input": {"command": cmd}}]},
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }, ensure_ascii=False))
    lines.append(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 1, "duration_ms": 100, "result": "{}",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }, ensure_ascii=False))
    return "\n".join(lines)


class _FakePopenProc:
    """Popen 替身（t-dc62df942275：run_cc_node 改走流式 Popen，mock 缝随之从
    subprocess.run 移到 subprocess.Popen；断言一字未动）。

    stdout/stderr 是真实 pipe 读端（流式读取器用 select + os.read 行切分，
    假的 str 属性过不了 fileno()）；写入端在构造时已写完 fake 输出并关闭，
    poll() 恒为已退出。kill/wait 只作接口占位（超时/熔断路径不用 fake 走）。
    """

    def __init__(self, stdout_text, returncode=0, stderr_text=""):
        out_r, out_w = os.pipe()
        os.write(out_w, stdout_text.encode("utf-8"))
        os.close(out_w)
        err_r, err_w = os.pipe()
        if stderr_text:
            os.write(err_w, stderr_text.encode("utf-8"))
        os.close(err_w)
        self.stdout = os.fdopen(out_r, "rb", closefd=True)
        self.stderr = os.fdopen(err_r, "rb", closefd=True)
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


def _make_run_dir(td):
    """在 td 下生成 run 的 agent 文件（<td>/.claude/agents/），返回 td。"""
    cc_agents.generate_for_run(_table(), str(ROOT / "role.yaml"), td)
    return td


@case("argv_default_permission_mode")
def _(x):
    """缺省 permission_mode：argv 含 --permission-mode acceptEdits，不含 bypassPermissions
    （原先硬编码 bypassPermissions 在 181 行，2026-11-07 改掉）。"""
    argv = cc_run.build_cc_argv("根", [], "hello")
    expect("--permission-mode" in argv, "应有 --permission-mode，实际 %r" % argv)
    expect(argv[argv.index("--permission-mode") + 1] == "acceptEdits",
           "缺省应为 acceptEdits，实际 %r" % argv)
    expect("bypassPermissions" not in argv, "缺省 argv 不应含 bypassPermissions，实际 %r" % argv)
    expect(cc_run.CC_PERMISSION_MODE_DEFAULT == "acceptEdits", "缺省常量应为 acceptEdits")


@case("argv_explicit_permission_modes")
def _(x):
    """显式 permission_mode=default/plan 时 argv 如实传。"""
    for mode in ("default", "plan"):
        argv = cc_run.build_cc_argv("根", [], "hello", permission_mode=mode)
        expect(argv[argv.index("--permission-mode") + 1] == mode,
               "显式 %s 应如实传，实际 %r" % (mode, argv))
        expect("bypassPermissions" not in argv, "非 bypass 不应含 bypassPermissions")


@case("argv_invalid_permission_mode")
def _(x):
    """不在 CC_PERMISSION_MODES 里的值 ⇒ ValueError（auto/dontAsk 不开放）。"""
    for bad in ("auto", "dontAsk", "manual", "bypass"):
        try:
            cc_run.build_cc_argv("根", [], "hello", permission_mode=bad)
        except ValueError:
            continue
        raise AssertionError("permission_mode=%r 应报 ValueError，实际通过了" % bad)


@case("resolve_bypass_refused_without_sandbox")
def _(x):
    """bypass 无 sandbox 条件时 build_cc_argv 与 resolve_bypass_permission 都抛错。"""
    old = _set_env({"TREE_CC_ALLOW_BYPASS": None, "TREE_CC_SANDBOX_PROFILE": None})
    try:
        expect(cc_run.resolve_bypass_permission(None) == "acceptEdits",
               "None 应解析为缺省 acceptEdits")
        expect(cc_run.resolve_bypass_permission("plan") == "plan",
               "plan 应原样返回")
        try:
            cc_run.resolve_bypass_permission("bypassPermissions")
        except RuntimeError as exc:
            expect("TREE_CC_ALLOW_BYPASS" in str(exc), "拒跑原因应写明 ALLOW_BYPASS，实际 %s" % exc)
        else:
            raise AssertionError("无 sandbox 条件时 resolve 应抛 RuntimeError，实际通过了")
        try:
            cc_run.build_cc_argv("根", [], "hello", permission_mode="bypassPermissions")
        except RuntimeError:
            pass
        else:
            raise AssertionError("无 sandbox 条件时 build_cc_argv 应抛 RuntimeError，实际通过了")
    finally:
        _restore_env(old)


@case("resolve_bypass_refused_partial_conditions")
def _(x):
    """bypass 三项条件缺一不可：逐项缺失时都拒跑。"""
    with tempfile.TemporaryDirectory() as td:
        good_profile = os.path.join(td, "tree.sb")
        Path(good_profile).write_text("(version 1)\n", encoding="utf-8")
        bad_profile = os.path.join(td, "tree.txt")
        Path(bad_profile).write_text("(version 1)\n", encoding="utf-8")
        cases = [
            ({"TREE_CC_ALLOW_BYPASS": "1", "TREE_CC_SANDBOX_PROFILE": None},
             "只给 ALLOW、缺 profile"),
            ({"TREE_CC_ALLOW_BYPASS": "1",
              "TREE_CC_SANDBOX_PROFILE": os.path.join(td, "missing.sb")},
             "profile 不存在"),
            ({"TREE_CC_ALLOW_BYPASS": "1", "TREE_CC_SANDBOX_PROFILE": bad_profile},
             "profile 不是 .sb"),
            ({"TREE_CC_ALLOW_BYPASS": None, "TREE_CC_SANDBOX_PROFILE": good_profile},
             "缺 ALLOW、只有 profile"),
            ({"TREE_CC_ALLOW_BYPASS": "0", "TREE_CC_SANDBOX_PROFILE": good_profile},
             "ALLOW 不是 1"),
        ]
        for updates, why in cases:
            old = _set_env(updates)
            try:
                cc_run.resolve_bypass_permission("bypassPermissions")
            except RuntimeError:
                pass
            else:
                raise AssertionError("bypass 应拒跑（%s），实际通过了" % why)
            finally:
                _restore_env(old)


@case("bypass_build_argv_with_sandbox")
def _(x):
    """bypass 有完整 sandbox 条件时 build_cc_argv 放行：argv 含 bypassPermissions，
    但不包 sandbox-exec（包壳是 run_cc_node 的事）。sandbox-exec 不在 PATH 时 skip。"""
    if not shutil.which("sandbox-exec"):
        print("SKIP bypass_build_argv_with_sandbox：sandbox-exec 不在 PATH（macOS 上应有）")
        return
    with tempfile.TemporaryDirectory() as td:
        profile = os.path.join(td, "tree.sb")
        Path(profile).write_text("(version 1)\n", encoding="utf-8")
        old = _set_env({"TREE_CC_ALLOW_BYPASS": "1", "TREE_CC_SANDBOX_PROFILE": profile})
        try:
            argv = cc_run.build_cc_argv("根", [], "hello", permission_mode="bypassPermissions")
        finally:
            _restore_env(old)
        expect(argv[argv.index("--permission-mode") + 1] == "bypassPermissions",
               "完整条件时应放行 bypassPermissions，实际 %r" % argv)
        expect(argv[0] != "sandbox-exec", "build_cc_argv 不包 sandbox-exec（run_cc_node 才包）")


@case("run_cc_node_bypass_refused")
def _(x):
    """run_cc_node 要求 bypass 但无 sandbox 条件 ⇒ 抛错，且一次也不调 subprocess。"""
    with tempfile.TemporaryDirectory() as td:
        _make_run_dir(td)
        old = _set_env({"TREE_CC_ALLOW_BYPASS": None, "TREE_CC_SANDBOX_PROFILE": None,
                        "TREE_CC_CONFIG_DIR": os.path.join(td, "cfg")})
        try:
            with mock.patch.object(cc_run.subprocess, "Popen") as mock_popen:
                try:
                    cc_run.run_cc_node("根", "hello", td, knobs=_knobs("根"),
                                       timeout=30, session_id="t1",
                                       permission_mode="bypassPermissions")
                except RuntimeError as exc:
                    expect("bypassPermissions" in str(exc), "拒跑原因应写明 bypass，实际 %s" % exc)
                else:
                    raise AssertionError("无 sandbox 条件时 run_cc_node 应拒跑，实际通过了")
                expect(mock_popen.call_count == 0, "拒跑前不应调 subprocess，实际调了 %d 次"
                       % mock_popen.call_count)
        finally:
            _restore_env(old)


@case("run_cc_node_bypass_sandboxed")
def _(x):
    """bypass 有完整条件时 run_cc_node 把 argv 包进 sandbox-exec -f <profile>，
    meta 记 sandboxed=true 与 profile。sandbox-exec 不在 PATH 时 skip。"""
    if not shutil.which("sandbox-exec"):
        print("SKIP run_cc_node_bypass_sandboxed：sandbox-exec 不在 PATH（macOS 上应有）")
        return
    with tempfile.TemporaryDirectory() as td:
        _make_run_dir(td)
        profile = os.path.join(td, "tree.sb")
        Path(profile).write_text("(version 1)\n", encoding="utf-8")
        old = _set_env({"TREE_CC_ALLOW_BYPASS": "1", "TREE_CC_SANDBOX_PROFILE": profile,
                        "TREE_CC_CONFIG_DIR": os.path.join(td, "cfg")})
        try:
            fake = _FakePopenProc(_fake_cc_stdout([]))
            with mock.patch.object(cc_run.subprocess, "Popen",
                                  return_value=fake) as mock_popen:
                res = cc_run.run_cc_node("根", "hello", td, knobs=_knobs("根"),
                                         timeout=30, session_id="t2",
                                         permission_mode="bypassPermissions")
            expect(res["ok"] is True, "放行后应跑通（mock），实际 %r" % res)
            argv = mock_popen.call_args[0][0]
            expect(argv[0] == "sandbox-exec", "argv[0] 应为 sandbox-exec，实际 %r" % argv[:3])
            expect(argv[1] == "-f" and argv[2] == profile,
                   "应为 sandbox-exec -f <profile>，实际 %r" % argv[:3])
            expect("bypassPermissions" in argv, "argv 里应仍有 bypassPermissions")
            meta = json.loads(Path(res["meta_path"]).read_text(encoding="utf-8"))
            expect(meta.get("sandboxed") is True, "meta 应记 sandboxed=true，实际 %r" % meta)
            expect(meta.get("sandbox_profile") == profile,
                   "meta 应记 profile 路径，实际 %r" % meta.get("sandbox_profile"))
            expect(meta.get("permission_mode") == "bypassPermissions",
                   "meta 应记 permission_mode，实际 %r" % meta.get("permission_mode"))
        finally:
            _restore_env(old)


@case("bash_allowlist_allows")
def _(x):
    """白名单命令全过：每条前缀的裸命令与带参形式都放行；非 Bash 工具不计入。"""
    for prefix in cc_run.CC_BASH_ALLOWLIST:
        for cmd in (prefix, prefix + " --flag value"):
            res = cc_run.verify_bash_allowlist([{"name": "Bash", "input": {"command": cmd}}])
            expect(not res["violations"], "%r 应放行，实际 %r" % (cmd, res["violations"]))
            expect(res["allowed"] == [cmd], "%r 应进 allowed，实际 %r" % (cmd, res))
    extra = [
        {"name": "Bash", "input": {"command": "ls -la"}},
        {"name": "Bash", "input": {"command": "git status --short"}},
        {"name": "Bash", "input": {"command": "sed -n '1,10p' f.txt"}},
        {"name": "Bash", "input": {"command": "python3 -m pytest tests/ -q"}},
        {"name": "Read", "input": {"file_path": "rm -rf /"}},
    ]
    res = cc_run.verify_bash_allowlist(extra)
    expect(not res["violations"], "白名单样例应全放行，实际 %r" % res["violations"])
    expect(len(res["allowed"]) == 4, "Read 不应计入 allowed，实际 %r" % res["allowed"])
    expect(cc_run.verify_bash_allowlist([]) == {"allowed": [], "violations": []},
           "空调用列表应返回空结果")


@case("bash_allowlist_rejects")
def _(x):
    """白名单外与含元字符的命令全被拒，并逐条写明原因。"""
    bad_cmds = [
        "rm -rf /", "curl https://example.com", "ls; rm -rf /",
        "cat x | grep y", 'python3 -c "print(1)"', "echo $(whoami)",
        "echo ${HOME}", "ls > out.txt", "cat < x", "echo `whoami`",
        "ls\nrm", "ls && ls", "ls || ls", "lsof", "git push origin main",
        "", "ls & rm -rf /", "sudo ls", "/bin/rm -rf /",
    ]
    calls = [{"name": "Bash", "input": {"command": c}} for c in bad_cmds]
    res = cc_run.verify_bash_allowlist(calls)
    expect(res["allowed"] == [], "违规命令不应有放行的，实际 %r" % res["allowed"])
    expect(len(res["violations"]) == len(bad_cmds),
           "应逐条违规 %d 条，实际 %d 条：%r" % (len(bad_cmds), len(res["violations"]), res))
    seen = {v["command"]: v["reason"] for v in res["violations"]}
    for cmd in bad_cmds:
        expect(cmd in seen, "%r 应在 violations 里" % cmd)
        expect(seen[cmd], "%r 的拒绝原因不应为空" % cmd)
    expect("元字符" in seen["ls; rm -rf /"], "分号命令原因应写明元字符，实际 %r" % seen["ls; rm -rf /"])
    expect("元字符" in seen["echo $(whoami)"], "$() 原因应写明元字符，实际 %r" % seen["echo $(whoami)"])
    expect("元字符" in seen["cat x | grep y"], "管道原因应写明元字符，实际 %r" % seen["cat x | grep y"])
    expect("白名单" in seen["rm -rf /"], "rm 原因应写明白名单，实际 %r" % seen["rm -rf /"])
    expect("白名单" in seen["lsof"], "lsof 不应沾 ls 前缀的光，原因应写明白名单，实际 %r" % seen["lsof"])


@case("safe_settings_merge")
def _(x):
    """ensure_safe_settings：写临时目录验证合并语义（保留原有键、allow 含白名单、
    deny 有具体危险命令、无 Bash(*) 兜底、重复调用不重复写规则）。"""
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "cfg")
        os.makedirs(target)
        Path(target, "settings.json").write_text(json.dumps(
            {"keep": 1, "permissions": {"allow": ["Bash(npm run *)"],
                                        "deny": ["Bash(git push *)"]}},
            ensure_ascii=False), encoding="utf-8")
        got = cc_run.ensure_safe_settings(target)
        expect(got == target, "应返回传入的目录，实际 %r" % got)
        data = json.loads(Path(target, "settings.json").read_text(encoding="utf-8"))
        expect(data.get("keep") == 1, "原有键 keep 应保留，实际 %r" % data)
        allow = data["permissions"]["allow"]
        deny = data["permissions"]["deny"]
        expect("Bash(npm run *)" in allow and allow[0] == "Bash(npm run *)",
               "原有 allow 规则应保留在前，实际 %r" % allow)
        expect("Bash(ls *)" in allow, "allow 应含 Bash(ls *)，实际 %r" % allow)
        expect("Bash(git status *)" in allow, "allow 应含 Bash(git status *)，实际 %r" % allow)
        expect("Bash(python3 -m pytest *)" in allow,
               "allow 应含 Bash(python3 -m pytest *)，实际 %r" % allow)
        for tool in ("Read", "Edit", "Write"):
            expect(tool in allow, "allow 应含 %s，实际 %r" % (tool, allow))
        expect("Bash(git push *)" in deny, "原有 deny 规则应保留，实际 %r" % deny)
        for cmd in ("rm", "curl", "wget", "sudo"):
            expect("Bash(%s *)" % cmd in deny, "deny 应含 Bash(%s *)，实际 %r" % (cmd, deny))
        expect("Bash(*)" not in deny, "deny 绝不能有 Bash(*)（会摘掉整个 Bash 工具）")
        expect("Bash(*)" not in allow, "allow 不应有 Bash(*)")
        expect("Bash" not in deny, "deny 不应有裸 Bash（会摘掉整个 Bash 工具）")
        # 幂等：再调一次不重复写规则。
        cc_run.ensure_safe_settings(target)
        data2 = json.loads(Path(target, "settings.json").read_text(encoding="utf-8"))
        expect(data2["permissions"]["allow"] == allow, "重复调用 allow 不应变长")
        expect(data2["permissions"]["deny"] == deny, "重复调用 deny 不应变长")


@case("safe_settings_refuses_live_config")
def _(x):
    """ensure_safe_settings 指到 ~/.claude 时抛错拒写（参数与 env 两种形式）。"""
    live = str(Path.home() / ".claude")
    for bad in (live, "~/.claude", live + "/"):
        try:
            cc_run.ensure_safe_settings(bad)
        except RuntimeError as exc:
            expect(".claude" in str(exc), "拒写原因应写明 .claude，实际 %s" % exc)
        else:
            raise AssertionError("config_dir=%r 应拒写，实际通过了" % bad)
    old = _set_env({"TREE_CC_CONFIG_DIR": live})
    try:
        cc_run.ensure_safe_settings()
    except RuntimeError:
        pass
    else:
        raise AssertionError("TREE_CC_CONFIG_DIR 指到 live 配置时应拒写，实际通过了")
    finally:
        _restore_env(old)


@case("cc_env_safe_settings_rules")
def _(x):
    """_cc_env 钉死配置后写安全规则：隔离目录的 settings.json 含白名单 allow 与危险 deny。"""
    with tempfile.TemporaryDirectory() as td:
        old = _set_env({"TREE_CC_CONFIG_DIR": td})
        try:
            cc_run._cc_env()
        finally:
            _restore_env(old)
        data = json.loads(Path(td, "settings.json").read_text(encoding="utf-8"))
        allow = data["permissions"]["allow"]
        deny = data["permissions"]["deny"]
        expect("Bash(ls *)" in allow, "_cc_env 后 allow 应含 Bash(ls *)，实际 %r" % allow)
        expect("Read" in allow, "_cc_env 后 allow 应含 Read，实际 %r" % allow)
        expect("Bash(rm *)" in deny, "_cc_env 后 deny 应含 Bash(rm *)，实际 %r" % deny)
        expect("Bash(*)" not in deny, "_cc_env 后 deny 不应有 Bash(*)")


@case("run_cc_node_bash_violation")
def _(x):
    """run_cc_node：模型调了 rm ⇒ 判失败（RuntimeError），meta 已落盘且记 violations。"""
    with tempfile.TemporaryDirectory() as td:
        _make_run_dir(td)
        cfg = os.path.join(td, "cfg")
        old = _set_env({"TREE_CC_CONFIG_DIR": cfg})
        try:
            fake = _FakePopenProc(_fake_cc_stdout(["ls -la", "rm -rf /"]))
            with mock.patch.object(cc_run.subprocess, "Popen", return_value=fake):
                try:
                    cc_run.run_cc_node("根", "hello", td, knobs=_knobs("根"),
                                       timeout=30, session_id="t3")
                except RuntimeError as exc:
                    expect("白名单" in str(exc), "失败原因应写明白名单，实际 %s" % exc)
                else:
                    raise AssertionError("Bash 违规时 run_cc_node 应判失败，实际通过了")
            meta_path = Path(td, "cc-t3.meta.json")
            expect(meta_path.is_file(), "判失败前 meta 应已落盘：%s" % meta_path)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            viols = meta.get("bash_allowlist", {}).get("violations", [])
            expect(any(v.get("command") == "rm -rf /" for v in viols),
                   "meta 应记 rm 违规，实际 %r" % viols)
            expect(meta.get("bash_allowlist", {}).get("allowed") == ["ls -la"],
                   "meta 应记放行的 ls，实际 %r" % meta.get("bash_allowlist"))
            expect(meta.get("permission_mode") == "acceptEdits",
                   "meta 应记缺省 acceptEdits，实际 %r" % meta.get("permission_mode"))
            expect(meta.get("sandboxed") is False, "非 bypass 时 sandboxed 应为 false")
        finally:
            _restore_env(old)


@case("run_cc_node_clean_bash_ok")
def _(x):
    """run_cc_node：模型只调白名单 Bash ⇒ 通过，meta 记 allowed、无 violations。"""
    with tempfile.TemporaryDirectory() as td:
        _make_run_dir(td)
        cfg = os.path.join(td, "cfg")
        old = _set_env({"TREE_CC_CONFIG_DIR": cfg})
        try:
            fake = _FakePopenProc(_fake_cc_stdout(["ls -la", "git status"]))
            with mock.patch.object(cc_run.subprocess, "Popen", return_value=fake):
                res = cc_run.run_cc_node("根", "hello", td, knobs=_knobs("根"),
                                         timeout=30, session_id="t4")
            expect(res["ok"] is True, "白名单 Bash 应通过，实际 %r" % res)
            expect(res["text"] == "{}", "应取到 result 正文")
            meta = json.loads(Path(res["meta_path"]).read_text(encoding="utf-8"))
            expect(meta["bash_allowlist"]["allowed"] == ["ls -la", "git status"],
                   "meta 应记 allowed，实际 %r" % meta["bash_allowlist"])
            expect(meta["bash_allowlist"]["violations"] == [],
                   "meta violations 应为空，实际 %r" % meta["bash_allowlist"])
        finally:
            _restore_env(old)


@case("budget_accepts_small")
def _(x):
    """小 prompt：估算 + 安全余量 < ctx_budget ⇒ 通过。"""
    knobs = _knobs("根")
    res = cc_run.check_budget("小任务", "agent 正文", [], knobs)
    expect(res["estimated_input_tokens"] > cc_run.CC_OVERHEAD_TOKENS,
           "估算应含安全余量")
    expect(res["estimated_input_tokens"] < int(knobs["ctx_budget"]),
           "小 prompt 应通过预算，实际 %r" % res)


@case("budget_no_large_overhead")
def _(x):
    """隔离最小配置后无大开销：60000 字符 prompt（约 15000 tokens + 1000 余量）
    应通过（旧 20000 开销下会被拒发；ccswitch 调查已证实开销来自 live 配置 ECC 清单，
    隔离配置后正式请求回到约 4.7k tokens）。"""
    knobs = _knobs("根")
    res = cc_run.check_budget("x" * 60000, "agent 正文", [], knobs)
    expect(res["estimated_input_tokens"] < int(knobs["ctx_budget"]),
           "60000 字符 prompt 应通过（开销已降到 1000），实际 %r" % res)


@case("budget_refuses_over_ctx")
def _(x):
    """真超 ctx_budget 才拒发：150000 字符 prompt（约 37500 tokens + 余量）> 32768。"""
    knobs = _knobs("根")
    try:
        cc_run.check_budget("x" * 150000, "agent 正文", [], knobs)
    except RuntimeError as exc:
        expect("ctx_budget" in str(exc), "拒发原因应写明 ctx_budget，实际 %s" % exc)
        return
    raise AssertionError("超 ctx_budget 应拒发，实际通过了")


@case("minimal_config_created")
def _(x):
    """隔离最小配置：ensure_minimal_config 建目录 + 空 settings.json（离线）。"""
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "cc-minimal")
        got = cc_run.ensure_minimal_config(target)
        expect(got == target, "应返回传入的目录，实际 %r" % got)
        expect(Path(target, "settings.json").is_file(), "应建 settings.json")
        expect(Path(target, "settings.json").read_text(encoding="utf-8") == "{}",
               "settings.json 应为空对象 {}")
        # 已存在时不动它（不覆盖）。
        Path(target, "settings.json").write_text("{\"a\": 1}", encoding="utf-8")
        cc_run.ensure_minimal_config(target)
        expect(Path(target, "settings.json").read_text(encoding="utf-8") == "{\"a\": 1}",
               "已存在的 settings.json 不应被覆盖")


@case("cc_env_isolated_config")
def _(x):
    """_cc_env 指到隔离最小配置（不指 Leo 的 live ~/.claude/），并传 15721 与 token。"""
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("TREE_CC_CONFIG_DIR")
        os.environ["TREE_CC_CONFIG_DIR"] = td
        try:
            env = cc_run._cc_env()
        finally:
            if old is None:
                os.environ.pop("TREE_CC_CONFIG_DIR", None)
            else:
                os.environ["TREE_CC_CONFIG_DIR"] = old
        expect(env.get("CLAUDE_CONFIG_DIR") == td,
               "CLAUDE_CONFIG_DIR 应指到隔离配置，实际 %r" % env.get("CLAUDE_CONFIG_DIR"))
        expect(env.get("ANTHROPIC_BASE_URL") == cc_run.CC_BASE_URL,
               "应钉死 15721，实际 %r" % env.get("ANTHROPIC_BASE_URL"))
        expect(env.get("ANTHROPIC_AUTH_TOKEN") == cc_run.CC_AUTH_TOKEN,
               "应传 PROXY_MANAGED，实际 %r" % env.get("ANTHROPIC_AUTH_TOKEN"))
        expect(str(Path.home()) not in str(env.get("CLAUDE_CONFIG_DIR") or ""),
               "隔离配置不应指到用户主目录下的 live 配置")


@case("budget_refuses_server_cap")
def _(x):
    """估算 + max_tokens > 163840（服务端上限）⇒ 拒发。"""
    knobs = dict(_knobs("根"))
    # 估算约 1003 tokens（小 prompt + 1000 余量），max_tokens=163000 时
    # 合计约 164003 > 163840 应拒发。
    knobs["max_tokens"] = 163000
    try:
        cc_run.check_budget("小任务", "agent 正文", [], knobs)
    except RuntimeError:
        return
    raise AssertionError("估算 + max_tokens 超服务端上限应拒发，实际通过了")


@case("tool_call_cap")
def _(x):
    """工具调用次数硬上限：6 次 > MAX_TOOL_CALLS(5) ⇒ run_cc_node 应判失败。"""
    lines = []
    for i in range(6):
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t%d" % i, "name": "Bash",
                 "input": {"command": "echo %d" % i}}]},
        }, ensure_ascii=False))
    lines.append(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 7, "duration_ms": 1000, "result": "{}",
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }, ensure_ascii=False))
    parsed = cc_run._parse_stream_json("\n".join(lines))
    expect(len(parsed["tool_calls"]) == 6, "应解析出 6 次工具调用，实际 %d"
           % len(parsed["tool_calls"]))
    expect(len(parsed["tool_calls"]) > cc_run.MAX_TOOL_CALLS,
           "6 次应超上限 %d" % cc_run.MAX_TOOL_CALLS)


@case("parse_stream_json_result")
def _(x):
    """stream-json 解析：result 事件的正文、assistant 消息的 usage、工具调用都能取出。
    （实测：result 事件的 usage 全是 0，真实 tokens 在 assistant 消息的 usage 里。）"""
    lines = [
        json.dumps({"type": "system", "subtype": "init", "tools": ["Read"]},
                   ensure_ascii=False),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "思考中"}],
            "usage": {"input_tokens": 15000, "output_tokens": 20}}}, ensure_ascii=False),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "num_turns": 1, "duration_ms": 1234, "result": "{\"要点\": [\"a\"]}",
                    "usage": {"input_tokens": 0, "output_tokens": 0}},
                   ensure_ascii=False),
    ]
    parsed = cc_run._parse_stream_json("\n".join(lines))
    expect(parsed["result_text"] == "{\"要点\": [\"a\"]}", "应取到 result 正文")
    expect(parsed["usage"]["input_tokens"] == 15000, "应从 assistant 消息取 input_tokens")
    expect(parsed["usage"]["output_tokens"] == 20, "应从 assistant 消息取 output_tokens")
    expect(parsed["num_turns"] == 1 and parsed["duration_ms"] == 1234, "应取到轮次/耗时")
    expect(parsed["tool_calls"] == [], "无工具调用时应为空列表")


# ---------------------------------------------------------------- 契约与 prompt
@case("spawn_json_valid")
def _(x):
    """有效的 spawn JSON 能过 schema.validate（复用既有契约）。"""
    knobs = _knobs("根", "spawn")
    obj = {"children": [{"role": "执行", "context": "写硬件基线文档",
                         "judge": {"chars_min": 1500},
                         "验收标准": "doc ≥1500 字", "auth": "doc/root.c1.1.md"}]}
    ok, why = schema.validate("spawn", obj, knobs)
    expect(ok, "有效 spawn 应通过校验，实际 %s" % why)


@case("spawn_json_invalid_role")
def _(x):
    """spawn 里 role 自造 ⇒ 校验应拒。"""
    knobs = _knobs("根", "spawn")
    obj = {"children": [{"role": "不存在的角色", "context": "写文档",
                         "judge": {"chars_min": 1500},
                         "验收标准": "doc ≥1500 字", "auth": "doc/root.c1.1.md"}]}
    ok, why = schema.validate("spawn", obj, knobs)
    expect(not ok, "自造 role 的 spawn 应被拒，实际通过了")


@case("assemble_prompt_no_role_prompt")
def _(x):
    """include_role_prompt=False 时 prompt 里不应有 role_prompt 原文。"""
    knobs = _knobs("根")
    node = {"id": "root", "context": "测试任务", "验收标准": "", "auth": ""}
    full = submit.assemble_prompt(node, knobs, act="plan")
    expect("你是整棵推敲树的" in full, "缺省应含 role_prompt 原文")
    slim = submit.assemble_prompt(node, knobs, act="plan", include_role_prompt=False)
    expect("你是整棵推敲树的" not in slim, "include_role_prompt=False 时不应含 role_prompt 原文")
    expect("测试任务" in slim, "context 不应被去掉")
    expect("本轮产出契约" in slim, "契约段不应被去掉")


@case("build_prompt_stateless_merge")
def _(x):
    """stateless merge：base（含契约）+ 增量（batch_view 子结果）两者都要有。"""
    table = _table()
    knobs = _knobs("根", "merge")
    parent = node_mod.new_node("root", "根", parent="", context="测试任务",
                               judge=None, 验收标准="", auth="",
                               round=2, batch=1)
    child = node_mod.new_node("root.c1.1", "执行", parent="root",
                              context="写文档", judge={"chars_min": 1500},
                              验收标准="doc ≥1500 字", auth="",
                              round=1, batch=1)
    child["state"] = "完成"
    child["reply"] = "子结果：硬件基线已写好"
    child["未决"] = []
    tree = {"root": parent, "root.c1.1": child}
    parent["_tree"] = tree
    parent["_run_dir"] = ""
    stateless = driver_mod.build_prompt(parent, "merge", knobs, table,
                                        first_round=False, stateless=True)
    expect("子结果：硬件基线已写好" in stateless,
           "stateless merge 应含 batch_view 子结果")
    expect("本轮产出契约" in stateless,
           "stateless merge 应含 base 契约段（无会话时契约不能丢）")
    stateful = driver_mod.build_prompt(parent, "merge", knobs, table,
                                       first_round=False, stateless=False)
    expect("子结果：硬件基线已写好" in stateful, "pi 路径 merge 也应含子结果")
    expect("本轮产出契约" not in stateful,
           "pi 路径非首轮不应重复注入 base 契约（会话里已有）")


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    passed, failed = 0, []
    print("cc-wiring gates · root=%s · python=%s" % (ROOT, sys.version.split()[0]))
    for name, fn in CASES:
        try:
            fn(None)
            print("CASE %-32s PASS" % name)
            passed += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            print("CASE %-32s FAIL: %s" % (name, exc))
            if os.environ.get("TREE_GATES_TRACE"):
                import traceback
                traceback.print_exc()
    total = len(CASES)
    print("-" * 72)
    if failed:
        print("FAILED: %s" % ", ".join(failed))
        print("GATES: %d/%d FAIL" % (passed, total))
        return 1
    print("GATES: %d/%d PASS" % (passed, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
