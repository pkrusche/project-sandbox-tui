#!/usr/bin/env python3
"""Version 1 feedback protocol and standalone reporting helper (stdlib only).

This file is also copied into each run's read-only sandbox mount. Keep it
independent of package imports and compatible with sandbox Python 3.9+.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

VERSION = 1
MAX_BYTES = 128 * 1024
MAX_DEPTH = 16
OUTCOMES = ("success", "failure", "blocked")
CHECK_OUTCOMES = ("passed", "failed", "skipped")
SANDBOX_ROOT = "/run/sbx-feedback"


class FeedbackError(ValueError):
    """Malformed, stale or unsupported feedback."""


class UnsupportedFeedback(FeedbackError):
    """A different protocol version requires a different reader."""


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise FeedbackError(f"duplicate key: {key}")
        result[key] = value
    return result


def _tree(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise FeedbackError(f"JSON exceeds maximum depth {MAX_DEPTH}")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise FeedbackError("JSON keys must be strings")
            _tree(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _tree(child, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise FeedbackError("non-finite numbers are not permitted")
    elif value is not None and type(value) not in (str, int, float, bool):
        raise FeedbackError("not a JSON value")


def encode(value: Any) -> str:
    _tree(value)
    text = json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise FeedbackError(f"JSON exceeds {MAX_BYTES} bytes")
    return text


def read_json(path: Path) -> Any:
    # Do not follow agent-created symlinks or block on FIFOs/devices.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise FeedbackError("feedback must be a regular file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise FeedbackError(f"JSON exceeds {MAX_BYTES} bytes")
    try:
        value = json.loads(data, object_pairs_hook=_pairs)
        _tree(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FeedbackError(str(exc)) from exc


def validate(raw: Any, run_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FeedbackError("report must be an object")
    required = {"schema_version", "run_id", "sequence", "phase", "outcome", "summary"}
    if not required <= raw.keys() or raw.keys() - required - {"checks", "state"}:
        raise FeedbackError("missing or unknown report fields")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != VERSION:
        raise UnsupportedFeedback("unsupported feedback schema_version")
    if raw["run_id"] != run_id or not run_id:
        raise FeedbackError("feedback run_id does not match this run")
    if type(raw["sequence"]) is not int or not 1 <= raw["sequence"] <= 2**53 - 1:
        raise FeedbackError("sequence must be a positive integer")
    if raw["phase"] not in ("running", "finished"):
        raise FeedbackError("phase must be running or finished")
    if (raw["phase"] == "running" and raw["outcome"] is not None) or (
        raw["phase"] == "finished" and raw["outcome"] not in OUTCOMES
    ):
        raise FeedbackError(
            "outcome must be null while running, or success/failure/blocked at finish"
        )
    if not isinstance(raw["summary"], str) or not raw["summary"].strip():
        raise FeedbackError("summary must be nonempty text")
    if not isinstance(raw.get("state", {}), dict):
        raise FeedbackError("state must be an object")
    checks = raw.get("checks", [])
    if not isinstance(checks, list) or len(checks) > 100:
        raise FeedbackError("checks must be an array of at most 100 checks")
    for check in checks:
        if (
            not isinstance(check, dict)
            or set(check) != {"name", "outcome"}
            or not isinstance(check["name"], str)
            or not check["name"].strip()
            or check["outcome"] not in CHECK_OUTCOMES
        ):
            raise FeedbackError("checks need a name and passed/failed/skipped outcome")
    encode(raw)
    return raw


def accept(raw: Any, run_id: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    report = validate(raw, run_id)
    # Python equality conflates JSON booleans and numbers (True == 1).
    # Compare serialized values while ignoring object key order.
    if previous and json.dumps(report, sort_keys=True) != json.dumps(previous, sort_keys=True):
        if previous["phase"] == "finished":
            raise FeedbackError("final report is sealed")
        if report["sequence"] <= previous["sequence"]:
            raise FeedbackError("feedback sequence did not increase")
    return report


def atomic_write(path: Path, value: Any) -> None:
    text = encode(value)
    fd, name = tempfile.mkstemp(prefix=".feedback-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


def report(
    root: Path,
    summary: str,
    outcome: str | None = None,
    state: dict[str, Any] | None = None,
    checks: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    context = read_json(root / "context.json")
    if (
        not isinstance(context, dict)
        or type(context.get("schema_version")) is not int
        or context["schema_version"] != VERSION
        or not isinstance(context.get("run_id"), str)
        or not context["run_id"]
    ):
        raise FeedbackError("invalid reporting context")
    output = root / "output"
    fd = os.open(output / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = output / "feedback.json"
        try:
            previous = validate(read_json(path), context["run_id"])
        except FileNotFoundError:
            previous = None
        raw = {
            "schema_version": VERSION,
            "run_id": context["run_id"],
            "sequence": previous["sequence"] + 1 if previous else 1,
            "phase": "finished" if outcome else "running",
            "outcome": outcome,
            "summary": summary,
            "state": state if state is not None else (previous or {}).get("state", {}),
            "checks": checks if checks is not None else (previous or {}).get("checks", []),
        }
        accept(raw, context["run_id"], previous)
        atomic_write(path, raw)
        return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report progress or finish an sbx-tui run")
    parser.add_argument(
        "--context-dir", type=Path, default=Path(os.environ.get("SBX_FEEDBACK_DIR", SANDBOX_ROOT))
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("update", "finish"):
        command = sub.add_parser(name)
        command.add_argument("--summary", required=True)
        command.add_argument("--state-file", type=Path)
        command.add_argument("--checks-file", type=Path)
        if name == "finish":
            command.add_argument("--outcome", choices=OUTCOMES, required=True)
    args = parser.parse_args(argv)
    try:
        state = read_json(args.state_file) if args.state_file else None
        checks = read_json(args.checks_file) if args.checks_file else None
        if args.state_file and not isinstance(state, dict):
            raise FeedbackError("state must be an object")
        if args.checks_file and not isinstance(checks, list):
            raise FeedbackError("checks must be an array")
        report(
            args.context_dir,
            args.summary,
            getattr(args, "outcome", None),
            state,
            checks,
        )
    except (OSError, ValueError) as exc:
        print(f"sbx-report: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
