#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一 CLI：run / verify / demo。

本模块**不改任何既有语义**：`run` 只做 driver.sh 的前置四件事（建目录、拷夹具、
写 run.json、设 env），调度仍然交给 `harness.leafrun.main`；`verify` 只读磁盘，
按机械判据打分，不调模型；`demo` 用确定性桩把 prime-report 自愈闭环离线跑一遍。

退出码：
  run     0 = 全树终态且 root 完成；1 = 未完成；2 = 用法/环境错
  verify  0 = 全部判据 PASS；1 = 有 FAIL
  demo    0 = 离线闭环 + 验收全 PASS；1 = 失败
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_NAME = "prime-report"
DEMO_SRC = REPO_ROOT / "examples" / DEMO_NAME
DEMO_STUB = DEMO_SRC / "stub-prime.py"
COPY_DIRS = ("input", "product", "blocks", "doc")
PROTECTED_ANSWER = "product/answer.txt"


# ---------------------------------------------------------------- 小工具
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"_unparsed": line})
    return rows


def _root_state(run_dir: Path) -> str:
    path = run_dir / "state.json"
    if not path.is_file():
        return ""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return ""
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else state
    root = (nodes or {}).get("root") or {}
    return str((root or {}).get("state") or "")


def _policy(run_dir: Path) -> dict:
    from harness import authority
    return authority.load_policy(str(run_dir))


def _protected_hashes(run_dir: Path, policy: dict) -> dict:
    """read_only（含 authoritative）下所有现存文件的 sha256。"""
    out = {}
    for rel in policy.get("read_only") or []:
        target = run_dir / rel
        if target.is_file():
            out[rel] = _sha256(target)
        elif target.is_dir():
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    out[path.relative_to(run_dir).as_posix()] = _sha256(path)
    return out


def _write_manifest(run_dir: Path) -> Path:
    """把 prepare 时的受保护文件哈希与可写清单冻结在 run 目录里。

    已存在则不覆盖（resume 沿用最初基线，防止用被改过的现场当基线）。
    """
    path = run_dir / "fixture-manifest.json"
    if path.is_file():
        return path
    policy = _policy(run_dir)
    manifest = {
        "schema": "harness-fixture-manifest/v1",
        "created_at": round(time.time(), 3),
        "authority": policy,
        "protected": _protected_hashes(run_dir, policy),
    }
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def _copy_src(src: Path, run_dir: Path) -> None:
    """只拷任务现场（input/product/blocks/doc/authority.json），不拷文档与 oracle 目录。"""
    if not src or not src.is_dir():
        return
    for name in COPY_DIRS:
        source = src / name
        if source.is_dir():
            shutil.copytree(source, run_dir / name, dirs_exist_ok=True)
    authority = src / "authority.json"
    if authority.is_file():
        shutil.copy2(authority, run_dir / "authority.json")


# ---------------------------------------------------------------- run
def cmd_run(args) -> int:
    if args.stub and args.runner:
        print("用法错：--stub 与 --runner 互斥（桩模式不跑真 runner）", file=sys.stderr)
        return 2

    run_id = str(args.run_id or "").strip() or "run-" + time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.run_dir or (REPO_ROOT / "runs" / run_id)).resolve()
    runner = "pi" if args.stub else (args.runner or "pi")

    if (run_dir / "state.json").is_file() and not args.resume:
        print("run 目录已有 state.json；请用 --resume 或换 --run-id：%s" % run_dir,
              file=sys.stderr)
        return 2

    input_path = Path(args.input).resolve() if args.input else None
    if input_path and not input_path.is_file():
        print("--input 指的文件不存在：%s" % input_path, file=sys.stderr)
        return 2

    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "input").mkdir(parents=True, exist_ok=True)
    _copy_src(Path(args.fixture).resolve() if args.fixture else None, run_dir)

    if not args.resume:
        if input_path is None:
            print("缺 --input（输入任务文件）", file=sys.stderr)
            return 2
        target = run_dir / "input" / input_path.name
        if target.exists() and target.read_bytes() != input_path.read_bytes():
            print("run 内已有不同内容的输入文件；拒绝覆盖：%s" % target, file=sys.stderr)
            return 2
        if not target.exists():
            shutil.copy2(input_path, target)
        (run_dir / "run.json").write_text(
            json.dumps({"runner": runner, "input": input_path.name},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    meta_path = run_dir / "run.json"
    if meta_path.is_file():
        saved = json.loads(meta_path.read_text(encoding="utf-8")).get("input") or ""
        run_input = run_dir / "input" / saved
    else:
        print("缺 %s；新 run 请给 --input" % meta_path, file=sys.stderr)
        return 2
    if not run_input.is_file():
        print("run 内输入文件不存在：%s" % run_input, file=sys.stderr)
        return 2

    _write_manifest(run_dir)

    # ── env：与 driver.sh 同口径；唯一差别是允许 --stub 设 TREE_ACT_CMD ──
    os.environ["TREE_ROOT"] = str(REPO_ROOT)
    os.environ["TREE_RUNS_DIR"] = str(REPO_ROOT / "runs")
    os.environ["TREE_PI_AGENT_DIR"] = str(run_dir / "pi-agent")
    os.environ["TREE_PI_AGENT_SESSION_DIR"] = str(run_dir / "pi-agent" / "sessions")
    os.environ.setdefault("TREE_PI_TIMEOUT", "900")
    os.environ.setdefault("TREE_MAX_STEPS", str(args.max_steps or 40))
    if args.stub:
        stub = Path(args.stub).resolve()
        if not stub.is_file():
            print("--stub 指的脚本不存在：%s" % stub, file=sys.stderr)
            return 2
        stub.chmod(0o755)
        os.environ["TREE_ACT_CMD"] = str(stub)
        # 桩的工作区（demo 桩按它读写任务现场）；真跑路径不受影响。
        os.environ.setdefault("TREE_DEMO_WORKDIR", str(run_dir))
    else:
        os.environ.pop("TREE_ACT_CMD", None)     # 与 driver.sh 一致：真跑绝不打桩

    from harness import leafrun
    argv = ["--run-id", run_id, "--run-dir", str(run_dir),
            "--input", str(run_input), "--runner", runner]
    if args.resume:
        argv.append("--resume")
    if args.max_steps:
        argv += ["--max-steps", str(args.max_steps)]
    if args.max_seconds:
        argv += ["--max-seconds", str(args.max_seconds)]
    return int(leafrun.main(argv) or 0)


# ---------------------------------------------------------------- verify
class _Report:
    def __init__(self, case: str, run_dir: Path):
        self.case = case
        self.run_dir = run_dir
        self.rows = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        return bool(ok)

    def emit(self) -> int:
        failed = [row for row in self.rows if not row[1]]
        print("VERIFY case=%s run=%s" % (self.case, self.run_dir))
        for name, ok, detail in self.rows:
            print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                                ("  -- " + detail) if detail and not ok else ""))
        if failed:
            print("VERIFY FAIL: %d/%d" % (len(failed), len(self.rows)))
            return 1
        print("VERIFY PASS: %d/%d" % (len(self.rows), len(self.rows)))
        return 0


def _manifest(run_dir: Path) -> dict:
    path = run_dir / "fixture-manifest.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _verify_content(report: _Report, run_dir: Path) -> None:
    answer = run_dir / PROTECTED_ANSWER
    body = answer.read_bytes() if answer.is_file() else b""
    report.check("product/answer.txt == b'168\\n'", body == b"168\n", repr(body))
    report.check("root 完成", _root_state(run_dir) == "完成", _root_state(run_dir))
    rows = _read_jsonl(run_dir / "final_audit.jsonl")
    last = rows[-1] if rows else {}
    report.check("final_audit 末行 blocker 数 = 0",
                 bool(rows) and int(last.get("blocker数") or 0) == 0,
                 json.dumps(last, ensure_ascii=False))


def _verify_selfheal(report: _Report, run_dir: Path) -> None:
    rows = _read_jsonl(run_dir / "final_audit.jsonl")
    blocker_rows = [row for row in rows if int(row.get("blocker数") or 0) > 0]
    report.check("1 终审记录过 blocker（收束被拦）", bool(blocker_rows),
                 "final_audit.jsonl 无 blocker 行")

    report_path = run_dir / "blocks" / "prime-report.md"
    text = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
    report.check("2 报告改为 168 且不再含 '186 primes'",
                 "168 primes" in text and "186 primes" not in text,
                 text.replace("\n", "\\n")[:200])

    manifest = _manifest(run_dir)
    protected = manifest.get("protected") or {}
    report.check("3 冻结清单存在（准备阶段的受保护哈希）", bool(protected),
                 "缺 fixture-manifest.json；请用 `agent-harness-tree run` 准备 run")

    drifted = []
    for rel, sha in protected.items():
        path = run_dir / rel
        if not path.is_file():
            drifted.append(rel + "（缺失）")
        elif _sha256(path) != sha:
            drifted.append(rel + "（sha 变了）")
    report.check("4 受保护文件哈希不变", not drifted, "、".join(drifted))

    rounds = _read_jsonl(run_dir / "rounds.jsonl")
    reruns = [row for row in rounds
              if str(row.get("node_id") or "") == "root.c1.1" and row.get("act") == "leaf"]
    report.check("5 预置叶 root.c1.1 未重跑", len(reruns) <= 1, "出现 %d 次" % len(reruns))

    report.check("6 同一 run 内 root 完成", _root_state(run_dir) == "完成", _root_state(run_dir))

    last = rows[-1] if rows else {}
    report.check("7 末条 final_audit blocker 数 = 0",
                 bool(rows) and int(last.get("blocker数") or 0) == 0,
                 json.dumps(last, ensure_ascii=False))

    policy = _policy(run_dir)
    want = (manifest.get("authority") or {}).get("writable") or []
    report.check("8 authority 可写清单未被改", policy.get("writable") == want,
                 "%s != %s" % (policy.get("writable"), want))


def _verify_recovery(report: _Report, run_dir: Path) -> None:
    state = _root_state(run_dir)
    report.check("root 处于终态", state in ("完成", "失败"), state)
    rounds = _read_jsonl(run_dir / "rounds.jsonl")
    seen, dups = set(), []
    for row in rounds:
        if row.get("act") != "leaf" or row.get("role") != "执行":
            continue
        key = str(row.get("node_id") or "")
        if key in seen:
            dups.append(key)
        seen.add(key)
    report.check("执行叶没有第二次 start", not dups, "、".join(sorted(set(dups))))
    bad = [row for row in _read_jsonl(run_dir / "recovery.jsonl") if "_unparsed" in row]
    report.check("recovery.jsonl 可解析（不存在视为通过）", not bad,
                 "存在非法行 %d 条" % len(bad))


CASES = {
    "content": _verify_content,
    "selfheal": _verify_selfheal,
    "recovery": _verify_recovery,
}


def cmd_verify(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        print("--run-dir 不存在：%s" % run_dir, file=sys.stderr)
        return 2
    report = _Report(args.case, run_dir)
    CASES[args.case](report, run_dir)
    return report.emit()


# ---------------------------------------------------------------- demo
def cmd_demo(args) -> int:
    if args.what != DEMO_NAME:
        print("未知 demo：%s（可选：%s）" % (args.what, DEMO_NAME), file=sys.stderr)
        return 2
    if not DEMO_STUB.is_file():
        print("缺演示桩：%s" % DEMO_STUB, file=sys.stderr)
        return 2

    cleanup = None
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
        if run_dir.exists() and (run_dir / "state.json").is_file():
            print("--run-dir 已有 run；请换目录或先删掉：%s" % run_dir, file=sys.stderr)
            return 2
    else:
        cleanup = tempfile.mkdtemp(prefix="agent-harness-demo-")
        run_dir = Path(cleanup) / "prime-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TREE_DEMO_WORKDIR"] = str(run_dir)

    rc = cmd_run(argparse.Namespace(
        input=str(DEMO_SRC / "task.md"), fixture=str(DEMO_SRC), run_id="prime-demo",
        run_dir=str(run_dir), runner=None, stub=str(DEMO_STUB),
        max_steps=60, max_seconds=300, resume=False))
    report = _Report("selfheal", run_dir)
    _verify_selfheal(report, run_dir)
    verify_rc = report.emit()

    if rc == 0 and verify_rc == 0:
        print("DEMO PASS: blocks/prime-report.md 186 -> 168, protected files unchanged")
        result = 0
    else:
        print("DEMO FAIL: run rc=%d verify rc=%d" % (rc, verify_rc))
        result = 1
    if cleanup and not args.keep:
        shutil.rmtree(cleanup, ignore_errors=True)
    elif cleanup:
        print("run 目录保留：%s" % run_dir)
    return result


# ---------------------------------------------------------------- 入口
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-harness-tree",
        description="可验收逻辑树：跑一个 run、机械验收、离线演示自愈闭环")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="跑一棵树（复用 harness.leafrun）")
    run.add_argument("--input", help="任务输入文件")
    run.add_argument("--fixture", help="任务现场目录（拷 input/product/blocks/doc/authority.json）")
    run.add_argument("--run-id", default="")
    run.add_argument("--run-dir", default="")
    run.add_argument("--runner", choices=("pi", "cc"), default=None)
    run.add_argument("--stub", default="", help="确定性桩脚本（与 --runner 互斥）")
    run.add_argument("--max-steps", type=int, default=None)
    run.add_argument("--max-seconds", type=float, default=None)
    run.add_argument("--resume", action="store_true")
    run.set_defaults(func=cmd_run)

    verify = sub.add_parser("verify", help="按机械判据验收一个 run 目录")
    verify.add_argument("--run-dir", required=True)
    verify.add_argument("--case", choices=sorted(CASES), default="content")
    verify.set_defaults(func=cmd_verify)

    demo = sub.add_parser("demo", help="离线演示（确定性桩，不需要模型）")
    demo.add_argument("what", choices=[DEMO_NAME])
    demo.add_argument("--run-dir", default="")
    demo.add_argument("--keep", action="store_true", help="保留临时 run 目录")
    demo.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
