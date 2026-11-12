#!/usr/bin/env python3
"""Freeze the fourth-round G2 fixtures and independently verify clean runs.

`prepare` copies hash-pinned inputs/oracle to durable evidence storage.
`verify` is read-only. Recovery/arbitration checks are deferred until core
runtime artifacts have a stable contract; this verifier never calls a model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

EVIDENCE_DEFAULT = Path.home() / "harness-evidence/harness-takeover-20261111"
SOURCE_DEFAULT = Path("/tmp/integ-accept4")
TASKS_DEFAULT = Path.home() / ".llaat/tasks"
CONTENT_EVIDENCE_DEFAULT = Path.home() / "harness-evidence/harness-content-contract-20261112"
CONTENT_FIXTURE_DEFAULT = Path(__file__).parent / "fixtures/harness-content-contract"
CONTENT_FIXTURE_SHA256 = {
    "input/q01.md": "90d8ff31c9d95538da865db8c2953085727fa8e4852226f4d5d1680089e2e50c",
    "input/agg2.md": "e2dc58f18460d21514b9c45ff7ab44b5186aff709c750837dc81a899701cbde0",
    "input/agg-data/month-01.txt": "8fb639e709f3a9455818d6f5f741befb2f9d29837537672d862ced9471fe0808",
    "input/agg-data/month-02.txt": "a69a54b1163f224a30b18c687d6c033fa3e79b09357255a62a224afe379eee43",
    "input/agg-data/month-03.txt": "220b828fee40d5448a0d8c0f927cecde2b0e554e05b6e7e04264a80291287693",
    "input/agg-data/month-04.txt": "9b2051341637d3d9c150cec40d0e68bd81b589cbc8588cc6795615be5ac1f178",
    "input/agg-data/month-05.txt": "54e9af85a0f2a169800c42bc8f38c343155811db112d014fba58f7fd27b470b2",
    "input/agg-data/month-06.txt": "40b67321d74f806bdd47beb2471ca8baa4343267902965215116dd1b67a7caa1",
    "prereg/agg-oracle.json": "6d33c4b46b2a048ff464ffa093523daebbc7fbc06880a109eb0d9f51c435e6b7",
    "prereg/preregistration.md": "b4f24fec41e7d26defc079de0b962056c24fbadd8798c7a08e40fb0d42b29965",
}
CONTENT_MANIFEST = "prereg/content-contract-inputs.json"
FROZEN_MD5 = {
    "input/q01.md": "0ff718617362d1f3a5425378fe4f7fa8",
    "input/agg2.md": "03deec48b331dd8d4e102b05734ecf13",
    "prereg/agg-oracle.json": "647c9c3b91452aad89d40cd4584afaed",
    "prereg/preregistration.md": "ab92ed3232c5b5b00771be83cb6373b5",
    "input/agg-data/month-01.txt": "e947df0778daa06f88babbf1e072e022",
    "input/agg-data/month-02.txt": "9c1165f07fbc9e932b1e6efe9dc9cbba",
    "input/agg-data/month-03.txt": "a16997b02577f080949c950bc68c9d81",
    "input/agg-data/month-04.txt": "38ca65a97f29aa684d6e1eb35a7062a6",
    "input/agg-data/month-05.txt": "35e04e10acb37d2559be92a6e477c5ac",
    "input/agg-data/month-06.txt": "40fbd0b1ae8a7a42e86a5484f50380d2",
}


class AcceptanceError(Exception):
    pass


def md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise AcceptanceError(f"missing JSON: {path}") from e
    except json.JSONDecodeError as e:
        raise AcceptanceError(f"invalid JSON {path}: {e}") from e


def read_jsonl(path: Path, optional: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        if optional:
            return []
        raise AcceptanceError(f"missing JSONL: {path}")
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise AcceptanceError(f"invalid JSONL {path}:{n}: {e}") from e
        if not isinstance(row, dict):
            raise AcceptanceError(f"expected JSON object at {path}:{n}")
        rows.append(row)
    return rows


def prepare(args: argparse.Namespace) -> int:
    source = args.source_root.expanduser().resolve()
    evidence = args.evidence_dir.expanduser().resolve()
    data_root = source / "runs/integ4-agg2/input/agg-data"
    sources = {
        "input/q01.md": source / "input/q01.md",
        "input/agg2.md": source / "input/agg2.md",
        "prereg/agg-oracle.json": source / "prereg/agg-oracle.json",
        "prereg/preregistration.md": source / "prereg/preregistration.md",
    }
    for relative in FROZEN_MD5:
        if relative.startswith("input/agg-data/"):
            sources[relative] = data_root / Path(relative).name
    manifest_files = {}
    for relative, src in sources.items():
        expected = FROZEN_MD5[relative]
        if not src.is_file() or md5(src) != expected:
            raise AcceptanceError(f"frozen source hash mismatch: {src}")
        dst = evidence / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if not dst.is_file() or md5(dst) != expected:
                raise AcceptanceError(f"refusing to overwrite changed evidence: {dst}")
        else:
            dst.write_bytes(src.read_bytes())
        manifest_files[relative] = {"md5": expected, "sha256": hashlib.sha256(dst.read_bytes()).hexdigest()}
    manifest = {"schema_version": 1, "source_root": str(source), "files": manifest_files}
    manifest_path = evidence / "prereg/frozen-inputs.json"
    if manifest_path.exists():
        existing = read_json(manifest_path)
        if existing.get("files") != manifest_files:
            raise AcceptanceError(f"frozen manifest differs; inspect before replacing: {manifest_path}")
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (evidence / "runs").mkdir(parents=True, exist_ok=True)
    print(f"EVIDENCE_ROOT={evidence}")
    print(f"FROZEN_MANIFEST={manifest_path}")
    print(f"FILES={len(manifest_files)}; all expected MD5 values match")
    return 0


def verify_fixtures(evidence: Path) -> None:
    read_json(evidence / "prereg/frozen-inputs.json")
    for relative, expected in FROZEN_MD5.items():
        path = evidence / relative
        if not path.is_file() or md5(path) != expected:
            raise AcceptanceError(f"frozen fixture missing or changed: {path}")


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, bool(ok), detail))

    def finish(self, receipts: list[dict[str, Any]]) -> int:
        failed = False
        for name, ok, detail in self.rows:
            print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
            failed |= not ok
        print(f"LLAAT_RECEIPT_COUNT={len(receipts)}")
        print("LLAAT_TASK_IDS=" + ",".join(row["task_id"] for row in receipts))
        print("OVERALL=" + ("FAIL" if failed else "PASS"))
        return int(failed)

    def finish_content_contract(self, receipts: list[dict[str, Any]]) -> int:
        failed = False
        for name, ok, detail in self.rows:
            print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
            failed |= not ok
        print(f"LLAAT_RECEIPT_COUNT={len(receipts)}")
        print("LLAAT_TASK_IDS=" + ",".join(row["task_id"] for row in receipts))
        print("RESULT_SCOPE=automated content-contract checks only")
        print(
            "MANUAL_REVIEW_REQUIRED=Compare the run trace for the stated method, "
            "independent cross-check or line-by-line verification, per-file results, "
            "actual Bash/Python execution, and document provenance/authenticity."
        )
        print("OVERALL=" + ("FAIL" if failed else "PASS"))
        return int(failed)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_content(args: argparse.Namespace) -> int:
    fixture_dir = args.fixture_dir.expanduser().resolve()
    evidence = args.evidence_dir.expanduser().resolve()
    for relative, expected in CONTENT_FIXTURE_SHA256.items():
        source = fixture_dir / relative
        if not source.is_file() or sha256(source) != expected:
            raise AcceptanceError(f"content-contract fixture missing or changed: {source}")
        destination = evidence / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not destination.is_file() or sha256(destination) != expected:
                raise AcceptanceError(f"refusing to overwrite changed content-contract evidence: {destination}")
        else:
            destination.write_bytes(source.read_bytes())
    manifest = {
        "schema_version": 1,
        "contract": "g2-content-contract-v1",
        "files": CONTENT_FIXTURE_SHA256,
    }
    manifest_path = evidence / CONTENT_MANIFEST
    if manifest_path.exists():
        existing = read_json(manifest_path)
        if existing != manifest:
            raise AcceptanceError(f"content-contract manifest differs; inspect before replacing: {manifest_path}")
    else:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (evidence / "runs").mkdir(parents=True, exist_ok=True)
    print(f"CONTENT_EVIDENCE_ROOT={evidence}")
    print(f"CONTENT_MANIFEST={manifest_path}")
    print(f"FILES={len(CONTENT_FIXTURE_SHA256)}; all SHA-256 values match")
    return 0


def verify_content_fixtures(evidence: Path) -> None:
    manifest = read_json(evidence / CONTENT_MANIFEST)
    expected_manifest = {
        "schema_version": 1,
        "contract": "g2-content-contract-v1",
        "files": CONTENT_FIXTURE_SHA256,
    }
    if manifest != expected_manifest:
        raise AcceptanceError(f"content-contract manifest mismatch: {evidence / CONTENT_MANIFEST}")
    for relative, expected in CONTENT_FIXTURE_SHA256.items():
        path = evidence / relative
        if not path.is_file() or sha256(path) != expected:
            raise AcceptanceError(f"content-contract input missing or changed: {path}")


def content_task_receipts(report: Report, logs: list[Path], task_dir: Path) -> list[dict[str, Any]]:
    ids: set[str] = set()
    missing_logs = []
    for path in logs:
        if not path.is_file():
            missing_logs.append(str(path))
            continue
        ids.update(re.findall(r"submitted task=(tree-[0-9a-f]+)", path.read_text(encoding="utf-8", errors="replace")))
    report.add("run-isolated LLAAT logs contain task IDs", bool(logs) and not missing_logs and bool(ids), f"ids={len(ids)} missing_logs={missing_logs}")
    rows, problems = [], []
    done_all = bool(ids) and not missing_logs
    for task_id in sorted(ids):
        path = task_dir / f"{task_id}.json"
        if not path.is_file():
            done_all = False
            problems.append(f"{task_id}: missing receipt")
            continue
        try:
            record = read_json(path)
            usage = (record.get("tree_response") or {}).get("usage") or {}
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
        except (AcceptanceError, AttributeError, TypeError, ValueError) as e:
            done_all = False
            problems.append(f"{task_id}: invalid receipt ({e})")
            continue
        total = prompt + completion
        done = record.get("id") == task_id and record.get("state") == "done" and isinstance(usage, dict) and prompt + completion > 0
        done_all &= done
        if not done:
            problems.append(f"{task_id}: id/state mismatch or nonpositive usage")
        rows.append({"task_id": task_id, "path": str(path), "state": record.get("state"), "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total})
    report.add("run LLAAT task IDs have done receipts", done_all, f"count={len(ids)} problems={problems[:5]}")
    maximum = max((row["total_tokens"] for row in rows), default=0)
    report.add("single-task tokens <=32K", maximum <= 32768, f"max={maximum}")
    return rows


def aggregate_from_run(data_dir: Path) -> dict[str, Any]:
    names = sorted(Path(relative).name for relative in CONTENT_FIXTURE_SHA256 if relative.startswith("input/agg-data/"))
    files, values = {}, []
    for name in names:
        numbers = [int(line) for line in (data_dir / name).read_text(encoding="utf-8").splitlines()]
        files[name] = {"lines": len(numbers), "sum": sum(numbers)}
        values.extend(numbers)
    return {
        "files": files,
        "total_lines": len(values),
        "total_sum": sum(values),
        "max": max(values),
        "min": min(values),
    }


def content_case_outputs(report: Report, case: str, run: Path, evidence: Path) -> None:
    if case == "q01":
        answer = run / "product/answer.txt"
        answer_ok = answer.is_file() and answer.read_bytes() == b"168\n"
        report.add("q01 answer exact bytes", answer_ok, f"expected b'168\\n'; present={answer.is_file()}")
        docs = sorted((run / "doc").glob("*.md")) if (run / "doc").is_dir() else []
        valid = []
        for doc in docs:
            text = doc.read_text(encoding="utf-8")
            if text.strip() and "168" in text and "product/answer.txt" in text:
                valid.append(str(doc.relative_to(run)))
        report.add("q01 doc nonempty with key value and artifact path", bool(valid), "; ".join(valid) or "no qualifying doc/*.md")
        return

    oracle = read_json(evidence / "prereg/agg-oracle.json")
    product = run / "product/agg.json"
    data_dir = run / "input/agg-data"
    expected_names = {Path(relative).name for relative in CONTENT_FIXTURE_SHA256 if relative.startswith("input/agg-data/")}
    names = {path.name for path in data_dir.iterdir()} if data_dir.is_dir() else set()
    data_ok = names == expected_names
    data_details = []
    for relative, expected in CONTENT_FIXTURE_SHA256.items():
        if relative.startswith("input/agg-data/"):
            path = data_dir / Path(relative).name
            ok = path.is_file() and sha256(path) == expected
            data_ok &= ok
            data_details.append(f"{path.name}={'ok' if ok else 'bad/missing'}")
    report.add("agg2 run-local inputs match original frozen data", data_ok, ", ".join(data_details))

    try:
        computed = aggregate_from_run(data_dir)
        got = read_json(product)
        product_ok = all(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            == json.dumps(oracle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for value in (got, computed)
        )
        product_detail = "matches preregistered oracle and computed run-local data" if product_ok else "product, oracle, and run-local aggregate differ"
    except (AcceptanceError, OSError, ValueError, TypeError) as e:
        product_ok, product_detail = False, str(e)
        oracle = oracle if isinstance(oracle, dict) else {}
    report.add("agg2 JSON exact original oracle and raw-data verdict", product_ok, product_detail)

    doc = run / "doc/agg_report.md"
    if doc.is_file():
        text = doc.read_text(encoding="utf-8")
        required_values = [str(oracle[key]) for key in ("total_sum", "total_lines", "max", "min") if key in oracle]
        # The report need not cite itself; citing the real JSON product is what counts.
        required_paths = ("product/agg.json",)
        missing_values = [value for value in required_values if value not in text]
        missing_paths = [path for path in required_paths if path not in text]
        doc_ok = bool(text.strip()) and not missing_values and not missing_paths
        doc_detail = f"nonempty={bool(text.strip())} missing_values={missing_values} missing_paths={missing_paths}"
    else:
        doc_ok, doc_detail = False, f"missing {doc}"
    report.add("agg2 report nonempty with key values and artifact paths", doc_ok, doc_detail)


def verify_content_contract(args: argparse.Namespace) -> int:
    evidence = args.evidence_dir.expanduser().resolve()
    run = args.run_dir.expanduser().resolve()
    verify_content_fixtures(evidence)
    report = Report()
    input_path = run / "input" / f"{args.case}.md"
    expected_input = evidence / "input" / f"{args.case}.md"
    input_ok = input_path.is_file() and sha256(input_path) == sha256(expected_input)
    report.add("run input matches latest content-contract fixture", input_ok, f"{input_path}")

    state_path = run / "state.json"
    if state_path.is_file():
        state = read_json(state_path)
    else:
        state = {}
        report.add("run state exists", False, f"missing {state_path}")
    nodes = state.get("nodes") or {}
    root = nodes.get("root") or {}
    report.add("root completed", root.get("state") == "完成", f"state={root.get('state')}")
    item_id, product_path = ("Q01", "product/answer.txt") if args.case == "q01" else ("AGG1", "product/agg.json")
    # Only the root's own reply counts. The serialized node also carries the original
    # task text (context) and the fallback delivery directory does too, so searching
    # the whole object let an unaggregated root pass by accident.
    root_reply = str(root.get("reply") or "")
    summary_ok = item_id in root_reply and product_path in root_reply
    report.add("root reply includes item ID and product path", summary_ok,
               f"required {item_id}, {product_path}; reply={root_reply[:240]!r}")
    unresolved = json.dumps(root.get("未决") or [], ensure_ascii=False)
    report.add("root has no unresolved items", not root.get("未决"), unresolved[:300])

    rounds_path = run / "rounds.jsonl"
    rounds = read_jsonl(rounds_path, optional=True)
    report.add("round history exists", rounds_path.is_file(), f"rows={len(rounds)}")
    audit_path = run / "final_audit.jsonl"
    audit = read_jsonl(audit_path, optional=True)
    fallback = [row for row in audit if row.get("兜底")]
    terminal = [row for row in audit if row.get("兜底") == "明确失败" or "终审_blocker_明确失败" in str(row.get("原因") or "")]
    report.add("final audit has no fallback/explicit failure", audit_path.is_file() and not fallback and not terminal, f"present={audit_path.is_file()} fallback={len(fallback)} explicit={len(terminal)}")
    violations_path = run / "violations.jsonl"
    violations = read_jsonl(violations_path, optional=True)
    terminal_violations = [row for row in violations if "终审_blocker_明确失败" in json.dumps(row, ensure_ascii=False)]
    report.add("no terminal blocker violation", not terminal_violations, f"count={len(terminal_violations)}")

    # Per-leaf bookkeeping is not an acceptance criterion: a leaf that failed on a
    # runtime flake and was retried to completion, or a checkpoint leaf that carries
    # no mechanical judge, still leaves an exact product. Only the product, the root
    # close-out and the terminal-audit outcome decide this case.
    leaves = [nid for nid, node in nodes.items() if node.get("act") == "leaf"]
    report.add("task leaf records exist", bool(leaves), f"count={len(leaves)}")

    content_case_outputs(report, args.case, run, evidence)
    receipts = content_task_receipts(report, args.llaat_log, args.llaat_tasks.expanduser().resolve())
    return report.finish_content_contract(receipts)


def case_outputs(report: Report, case: str, run: Path, evidence: Path) -> None:
    if case == "q01":
        answer = run / "product/answer.txt"
        answer_ok = answer.is_file() and answer.read_bytes() == b"168\n"
        report.add("q01 answer exact bytes", answer_ok, f"expected b'168\\n'; present={answer.is_file()}")
        docs = sorted((run / "doc").glob("*.md")) if (run / "doc").is_dir() else []
        valid = []
        for doc in docs:
            text = doc.read_text(encoding="utf-8")
            if len(text) >= 1500 and "168" in text:
                valid.append(f"{doc.resolve().relative_to(run.resolve())} len={len(text)}")
        report.add("q01 doc Python len>=1500 and includes 168", bool(valid), "; ".join(valid) or "no qualifying doc/*.md")
        item_id, product_path = "Q01", "product/answer.txt"
    else:
        oracle = read_json(evidence / "prereg/agg-oracle.json")
        product = run / "product/agg.json"
        try:
            got = read_json(product)
            matches = got == oracle
            detail = "exact JSON equality" if matches else f"actual={got!r}; oracle={oracle!r}"
        except AcceptanceError as e:
            matches, detail = False, str(e)
        report.add("agg2 JSON exact frozen oracle", matches, detail)
        data_dir = run / "input/agg-data"
        expected_names = {Path(p).name for p in FROZEN_MD5 if p.startswith("input/agg-data/")}
        names = {p.name for p in data_dir.iterdir()} if data_dir.is_dir() else set()
        data_ok = names == expected_names
        data_detail = []
        for relative, expected in FROZEN_MD5.items():
            if relative.startswith("input/agg-data/"):
                p = data_dir / Path(relative).name
                ok = p.is_file() and md5(p) == expected
                data_ok &= ok
                data_detail.append(f"{p.name}={'ok' if ok else 'bad/missing'}")
        report.add("agg2 run-local inputs match frozen MD5", data_ok, ", ".join(data_detail))
        doc = run / "doc/agg_report.md"
        if doc.is_file():
            text = doc.read_text(encoding="utf-8")
            doc_ok = len(text) >= 1500 and "71269" in text and "148" in text
            doc_detail = f"len={len(text)} includes 71269/148"
        else:
            doc_ok, doc_detail = False, f"missing {doc}"
        report.add("agg2 report Python len>=1500 and includes 71269/148", doc_ok, doc_detail)
        item_id, product_path = "AGG1", "product/agg.json"
    root_text = json.dumps(read_json(run / "state.json")["nodes"].get("root", {}), ensure_ascii=False)
    out = run / "out"
    if out.is_dir():
        root_text += "\n" + "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in out.rglob("*.md"))
    report.add("parent summary includes item ID and product path", item_id in root_text and product_path in root_text, f"required {item_id}, {product_path}")
    root = read_json(run / "state.json")["nodes"].get("root", {})
    undecided = json.dumps(root.get("未决") or [], ensure_ascii=False)
    report.add("no unresolved root blocker", "blocker" not in undecided.lower(), undecided[:300])


def hard_doc_check(report: Report, run: Path, node_id: str, node: dict[str, Any], case: str) -> None:
    dc = node.get("disk_doc_check") or {}
    judge = node.get("judge")
    text = json.dumps(judge, ensure_ascii=False) if isinstance(judge, dict) else str(judge or "")
    mins = [v for v in [dc.get("chars_min") if isinstance(dc, dict) else None] if isinstance(v, int) and v > 0]
    match = re.search(r"(?:不少于|至少|chars_min\s*[=:])\s*(\d+)\s*(?:字|字符)?", text, re.IGNORECASE)
    if match:
        mins.append(int(match.group(1)))
    if not mins:
        return
    minimum = max(mins)
    paths = []
    if isinstance(dc, dict):
        for key in ("named", "written"):
            table = dc.get(key)
            if isinstance(table, dict):
                for raw in table:
                    if str(raw).endswith(".md"):
                        path = Path(raw)
                        if path.is_absolute():
                            try:
                                path.resolve().relative_to(run.resolve())
                            except ValueError:
                                continue
                        else:
                            path = run / path
                        paths.append(path)
    if not paths:
        for folder in ("doc", "blocks"):
            base = run / folder
            if base.is_dir():
                paths.extend(p for p in base.rglob("*.md") if node_id in p.name)
    if not paths and case == "agg2" and "agg_report" in text:
        paths = [run / "doc/agg_report.md"]
    paths = list(dict.fromkeys(paths))
    ok_all, details = bool(paths), []
    recorded = {}
    if isinstance(dc, dict):
        for key in ("named", "written"):
            if isinstance(dc.get(key), dict):
                recorded.update(dc[key])
    for path in paths:
        if not path.is_file():
            ok_all = False
            details.append(f"{path}:missing")
            continue
        chars = len(path.read_text(encoding="utf-8"))
        expected = [value for raw, value in recorded.items() if str(raw).endswith(path.name) and isinstance(value, int)]
        size_matches = not expected or all(value == chars for value in expected)
        ok = chars >= minimum and size_matches and not (isinstance(dc, dict) and dc.get("passed") is False)
        ok_all &= ok
        details.append(f"{path.resolve().relative_to(run.resolve())} len={chars} min={minimum}" + (" recorded-size-mismatch" if not size_matches else ""))
    report.add(f"hard disk doc check {node_id}", ok_all, "; ".join(details) or f"chars_min={minimum}; no file")


def task_receipts(report: Report, run: Path, logs: list[Path], task_dir: Path) -> list[dict[str, Any]]:
    ids = set()
    for path in [run / "pi-agent/llaat-adapter.log", *logs]:
        if path.is_file():
            ids.update(re.findall(r"submitted task=(tree-[0-9a-f]+)", path.read_text(encoding="utf-8", errors="replace")))
    rows, problems = [], []
    ok_all = bool(ids)
    for task_id in sorted(ids):
        path = task_dir / f"{task_id}.json"
        if not path.is_file():
            ok_all = False
            problems.append(f"{task_id}: missing task receipt")
            continue
        record = read_json(path)
        usage = (record.get("tree_response") or {}).get("usage") or {}
        prompt, completion = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        total = prompt + completion
        done = record.get("state") == "done" and bool(usage)
        ok_all &= done
        if not done:
            problems.append(f"{task_id}: state={record.get('state')} or usage missing")
        rows.append({"task_id": task_id, "path": str(path), "state": record.get("state"), "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total})
    report.add("real LLAAT task IDs have done receipts", ok_all, f"count={len(ids)} problems={problems[:5]}")
    maximum = max((row["total_tokens"] for row in rows), default=0)
    report.add("single-task tokens <=32K", maximum <= 32768, f"max={maximum}")
    return rows


def verify(args: argparse.Namespace) -> int:
    evidence, run = args.evidence_dir.expanduser().resolve(), args.run_dir.expanduser().resolve(strict=True)
    verify_fixtures(evidence)
    state = read_json(run / "state.json")
    nodes = state.get("nodes") or {}
    root = nodes.get("root") or {}
    report = Report()
    report.add("root completed", root.get("state") == "完成", f"state={root.get('state')}")
    rounds = read_jsonl(run / "rounds.jsonl")
    audit = read_jsonl(run / "final_audit.jsonl")
    fallback = [row for row in audit if row.get("兜底")]
    terminal = [row for row in audit if row.get("兜底") == "明确失败" or "终审_blocker_明确失败" in str(row.get("原因") or "")]
    report.add("no final-audit fallback/explicit failure", not fallback and not terminal, f"fallback={len(fallback)} explicit={len(terminal)}")
    violations = read_jsonl(run / "violations.jsonl", optional=True)
    terminal_violations = [row for row in violations if "终审_blocker_明确失败" in json.dumps(row, ensure_ascii=False)]
    report.add("no terminal blocker violation", not terminal_violations, f"count={len(terminal_violations)}")
    checkpoint_ids = {str(row.get("检查点id")) for row in audit if row.get("检查点id")}
    checkpoint_ids.update(nid for nid in nodes if re.search(r"\.f\d+\.\d+$", nid))
    leaves = [(nid, node) for nid, node in nodes.items() if node.get("act") == "leaf" and nid not in checkpoint_ids]
    report.add("task leaf records exist", bool(leaves), f"count={len(leaves)}")
    for nid, node in sorted(leaves):
        attempts = [row for row in rounds if row.get("node_id") == nid]
        report.add(f"leaf state {nid}", node.get("state") == "完成", f"state={node.get('state')}")
        report.add(f"leaf judge_ok {nid}", node.get("judge_ok") is True, f"judge_ok={node.get('judge_ok')}")
        all_attempts_ok = bool(attempts) and all(row.get("exit_class") == "完成" and row.get("judge_ok") is True for row in attempts)
        report.add(f"no failed leaf attempt {nid}", all_attempts_ok, f"attempts={len(attempts)}")
        hard_doc_check(report, run, nid, node, args.case)
    case_outputs(report, args.case, run, evidence)
    receipts = task_receipts(report, run, args.llaat_log, args.llaat_tasks.expanduser().resolve())
    return report.finish(receipts)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="copy the hash-pinned inputs, oracle, and preregistration")
    p.add_argument("--source-root", type=Path, default=SOURCE_DEFAULT)
    p.add_argument("--evidence-dir", type=Path, default=EVIDENCE_DEFAULT)
    p.set_defaults(func=prepare)
    p = sub.add_parser("prepare-content", help="prepare versioned inputs for the no-length G2 content contract")
    p.add_argument("--fixture-dir", type=Path, default=CONTENT_FIXTURE_DEFAULT)
    p.add_argument("--evidence-dir", type=Path, default=CONTENT_EVIDENCE_DEFAULT)
    p.set_defaults(func=prepare_content)
    p = sub.add_parser("verify", help="read-only clean G2 verification")
    p.add_argument("--case", choices=("q01", "agg2"), required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--evidence-dir", type=Path, default=EVIDENCE_DEFAULT)
    p.add_argument("--llaat-tasks", type=Path, default=TASKS_DEFAULT)
    p.add_argument("--llaat-log", type=Path, action="append", default=[], help="extra/shared adapter log; repeatable")
    p.set_defaults(func=verify)
    p = sub.add_parser("content-contract", help="verify artifacts and run outcomes against the current G2 content contract")
    p.add_argument("--case", choices=("q01", "agg2"), required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--evidence-dir", type=Path, default=CONTENT_EVIDENCE_DEFAULT)
    p.add_argument("--llaat-tasks", type=Path, default=TASKS_DEFAULT)
    p.add_argument("--llaat-log", type=Path, action="append", required=True, help="run-isolated adapter log; repeat for multiple logs")
    p.set_defaults(func=verify_content_contract)
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        return args.func(args)
    except (AcceptanceError, OSError, ValueError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
