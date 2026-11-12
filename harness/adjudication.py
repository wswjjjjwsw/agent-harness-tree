"""Durable node-level Leo adjudication receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def _atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("receipt JSON must be an object")
    return value


def _identity(card: dict) -> tuple:
    if not isinstance(card, dict):
        raise ValueError("card must be an object")
    task = str(card.get("id") or "").strip()
    if not task:
        raise ValueError("card.id is required")
    encoded = json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return task, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _error(message: str, raw_path: Path = None, worker_pid: int = None) -> dict:
    result = {"status": "error", "error": str(message)}
    if raw_path is not None:
        result["raw_receipt"] = str(Path(raw_path))
    if worker_pid is not None:
        result["worker_pid"] = worker_pid
    return result


def _parse_dispatch(card: dict, raw: dict, raw_path: Path) -> dict:
    if not isinstance(raw, dict):
        return _error("Leo dispatch wrapper must be an object", raw_path)
    rc = raw.get("rc")
    if not isinstance(rc, int) or isinstance(rc, bool):
        return _error("Leo dispatch rc must be an integer", raw_path)
    if rc == 3:
        return _error("Leo dispatch returned rc=3", raw_path)
    if rc not in (0, 1, 2):
        return _error("Leo dispatch returned an unsupported rc", raw_path)

    verdict = raw.get("verdict")
    if not isinstance(verdict, dict):
        return _error("Leo dispatch verdict must be an object", raw_path)
    task, _ = _identity(card)
    if verdict.get("kind") != "judge" or str(verdict.get("task") or "") != task:
        return _error("Leo verdict kind or task does not match the card", raw_path)
    decision = verdict.get("verdict")
    rc_by_verdict = {"pass": 0, "rework": 1, "escalate": 2}
    if decision not in rc_by_verdict or rc_by_verdict[decision] != rc:
        return _error("Leo rc and verdict do not match", raw_path)

    blocking = verdict.get("blocking")
    note = verdict.get("note", "")
    missing = verdict.get("missing", [])
    if not isinstance(blocking, bool):
        return _error("Leo verdict blocking must be boolean", raw_path)
    if not isinstance(note, str):
        return _error("Leo verdict note must be a string", raw_path)
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        return _error("Leo verdict missing must be a list of strings", raw_path)
    if decision == "pass" and blocking:
        return _error("blocking Leo verdict cannot pass", raw_path)

    requires_user_approval = bool(card.get("auth_affected"))
    if requires_user_approval:
        if decision != "escalate":
            note = "授权相关事项需本人决策；Leo原裁决为%s：%s" % (decision, note)
        decision, blocking = "escalate", True
    return {
        "status": "done",
        "verdict": decision,
        "blocking": blocking,
        "note": note,
        "missing": missing,
        "requires_user_approval": requires_user_approval,
        "raw_receipt": str(Path(raw_path)),
    }


def _write_worker(root: Path, state: str, pid: int = None, error: str = None) -> None:
    record = {"state": state, "updated_at": time.time()}
    if pid is not None:
        record["pid"] = pid
    if error:
        record["error"] = error
    _atomic_json(root / "worker.json", record)


def _save_judgement(root: Path, task: str, card_hash: str,
                    raw, outcome: dict, pid: int = None) -> None:
    _atomic_json(root / "judgement.json", {
        "task": task,
        "card_sha256": card_hash,
        "raw": raw,
        "outcome": outcome,
        "worker_pid": pid,
        "saved_at": time.time(),
    })


def _saved_outcome(root: Path, task: str, card_hash: str) -> dict:
    path = root / "judgement.json"
    if not path.is_file():
        return None
    try:
        saved = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return _error("stored Leo judgement is invalid", path)
    if saved.get("task") != task or saved.get("card_sha256") != card_hash:
        return _error("stored Leo judgement identity does not match the card", path)
    outcome = saved.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("status") not in ("done", "error"):
        return _error("stored Leo judgement has no validated outcome", path)
    _reap_child(saved.get("worker_pid"))
    return outcome


def _reap_child(pid) -> None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pending(root: Path, phase: str, pid: int = None) -> dict:
    result = {"status": "pending", "phase": phase, "checkpoint": str(root / "worker.json")}
    if pid is not None:
        result["worker_pid"] = pid
    return result


def _transport():
    from harness.arbitration import LeoRuntimeTransport, _find_runtime_dir

    runtime_dir = Path(_find_runtime_dir())
    runtime_text = str(runtime_dir)
    if runtime_text not in sys.path:
        sys.path.insert(0, runtime_text)
    import leo_runtime

    selection = leo_runtime.load_selection(runtime_dir / "config.json")
    return LeoRuntimeTransport(
        role_dir=str(runtime_dir / "role"),
        db=str(Path.home() / ".cc-switch" / "cc-switch.db"),
        frontend=str(selection.get("frontend") or ""),
        require_manifest=True,
    )


def _worker(receipt_dir: str) -> int:
    root = Path(receipt_dir).resolve()
    pid = os.getpid()
    try:
        saved = _read_json(root / "request.json")
        card = saved.get("card")
        task, card_hash = _identity(card)
        if saved.get("task") != task or saved.get("card_sha256") != card_hash:
            raise ValueError("saved request identity mismatch")
        _write_worker(root, "running", pid)
        raw = _transport()(card, "judge")
        raw_path = root / "judgement.json"
        outcome = _parse_dispatch(card, raw, raw_path)
        _save_judgement(root, task, card_hash, raw, outcome, pid)
        _write_worker(root, outcome["status"], pid,
                      outcome.get("error") if outcome["status"] == "error" else None)
        return 0 if outcome["status"] == "done" else 1
    except Exception as exc:
        message = "Leo worker failed: %s" % type(exc).__name__
        task = ""
        card_hash = ""
        try:
            saved = _read_json(root / "request.json")
            task, card_hash = saved.get("task") or "", saved.get("card_sha256") or ""
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        raw_path = root / "judgement.json"
        outcome = _error(message, raw_path, pid)
        _save_judgement(root, task, card_hash, None, outcome, pid)
        _write_worker(root, "error", pid, message)
        return 1


def start_or_read(card: dict, receipt_dir: str, live: bool = False) -> dict:
    try:
        task, card_hash = _identity(card)
        root = Path(receipt_dir).expanduser().resolve()
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        request_path = root / "request.json"
        leo_request = {
            "mode": "judge",
            "task": task,
            "packet": {
                "title": str(card.get("question") or ""),
                "original_requirement": str(card.get("problem") or ""),
                "card": card,
            },
        }
        request = {
            "task": task,
            "card_sha256": card_hash,
            "card": card,
            "leo_request": leo_request,
        }
        if request_path.is_file():
            saved = _read_json(request_path)
            if saved.get("task") != task or saved.get("card_sha256") != card_hash:
                return _error("receipt directory belongs to a different card")
        else:
            if any((root / name).exists() for name in ("worker.json", "judgement.json")):
                return _error("receipt artifacts exist without a saved request")
            _atomic_json(request_path, request)
            _write_worker(root, "prepared")

        outcome = _saved_outcome(root, task, card_hash)
        if outcome is not None:
            return outcome

        worker_path = root / "worker.json"
        worker = _read_json(worker_path) if worker_path.is_file() else {"state": "prepared"}
        state, pid = worker.get("state"), worker.get("pid")
        if state in ("running", "starting"):
            if _pid_alive(pid):
                return _pending(root, "running", pid)
            message = "Leo worker exited without a judgement; request was not resent"
            _save_judgement(root, task, card_hash, None, _error(message, root / "judgement.json", pid), pid)
            _write_worker(root, "error", pid, message)
            return _saved_outcome(root, task, card_hash)
        if state in ("done", "error"):
            return _error("Leo worker finished without a saved judgement",
                          worker_pid=pid if isinstance(pid, int) else None)
        if state != "prepared":
            return _error("Leo worker state is uncertain; request was not resent")

        if not live:
            return _pending(root, "prepared")
        _write_worker(root, "starting")
        env = os.environ.copy()
        env.setdefault("LEO_TIMEOUT_S", "1200")
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        argv = [sys.executable, "-m", "harness.adjudication", "--worker", str(root)]
        null = os.devnull
        actions = (
            (os.POSIX_SPAWN_OPEN, 0, null, os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, null, os.O_WRONLY, 0),
            (os.POSIX_SPAWN_OPEN, 2, null, os.O_WRONLY, 0),
        )
        try:
            pid = os.posix_spawn(sys.executable, argv, env, file_actions=actions, setsid=True)
        except OSError:
            message = "could not start Leo worker"
            _save_judgement(root, task, card_hash, None, _error(message, root / "judgement.json"), None)
            _write_worker(root, "error", error=message)
            return _saved_outcome(root, task, card_hash)
        _write_worker(root, "running", pid)
        outcome = _saved_outcome(root, task, card_hash)
        if outcome is not None:
            return outcome
        return _pending(root, "running", pid)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _error("invalid adjudication receipt: %s" % type(exc).__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True)
    args = parser.parse_args(argv)
    return _worker(args.worker)


if __name__ == "__main__":
    raise SystemExit(main())
