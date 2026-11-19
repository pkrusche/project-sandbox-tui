#!/usr/bin/env python3
"""Stand-in for the project-sandbox CLI used by the tests.

Understands just enough of the real CLI: the positional repo, --branch,
--worktree-dir, --agent, --prompt-text, --log, --json-summary. Behaviour is
driven by the prompt text: ``sleep:<seconds>``, ``exit:<code>`` and
``ignore-signals`` (ignore SIGINT/SIGTERM) tokens.
Writes a session record the same way project-sandbox does, so the TUI's
session matching is exercised too.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def path_for(repo: Path, branch: str, root: str | None) -> Path:
    base = Path(root) if root else repo.parent / f"{repo.name}-workspaces"
    safe = branch.replace("/", "-")
    if "/" in branch:
        safe = f"{safe}-{hashlib.sha256(branch.encode()).hexdigest()[:6]}"
    return base / safe


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("project")
    p.add_argument("base_image", nargs="?")
    p.add_argument("--mount", action="append", default=[])
    p.add_argument("--branch")
    p.add_argument("--worktree-dir")
    p.add_argument("--agent")
    p.add_argument("--prompt-text")
    p.add_argument("--log")
    p.add_argument("--json-summary")
    args, _unknown = p.parse_known_args()

    repo = Path(args.project)
    workspace = path_for(repo, args.branch, args.worktree_dir) if args.branch else repo
    feedback_root = None
    review_input = None
    for mount in args.mount:
        parts = dict(item.split("=", 1) for item in mount.split(",") if "=" in item)
        if parts.get("target") == "/run/sbx-feedback":
            feedback_root = Path(parts["source"])
        if parts.get("target") == "/run/sbx-review":
            review_input = Path(parts["source"])
    if review_input and feedback_root:
        control = Path(os.environ["SBX_FAKE_REVIEW_CONTROL"])
        request = json.loads((control / "request.json").read_text())
        entries = json.loads((review_input / "commits.json").read_text())
        state = feedback_root / "output" / "fake-state.json"
        state.write_text(json.dumps(request["state"]))
        subprocess.run(
            [
                sys.executable,
                str(feedback_root / "sbx-report"),
                "--context-dir",
                str(feedback_root),
                "finish",
                "--outcome",
                "success",
                "--summary",
                "Fake review",
                "--state-file",
                str(state),
            ],
            check=True,
        )
        ready = {
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "entries": entries,
            "patches": [(review_input / entry["patch"]).read_text() for entry in entries],
        }
        (control / "ready.tmp").write_text(json.dumps(ready))
        (control / "ready.tmp").replace(control / "ready.json")
        while not (control / "release").exists():
            time.sleep(0.025)
        return int(request.get("exit", 0))
    context = feedback_root / "context.json" if feedback_root else None
    planning = (
        json.loads(context.read_text()).get("planning_prompt", "")
        if context and context.is_file()
        else ""
    )
    prompt = args.prompt_text or planning
    sleep = float(m.group(1)) if (m := re.search(r"sleep:([\d.]+)", prompt)) else 0.0
    code = int(m.group(1)) if (m := re.search(r"exit:(\d+)", prompt)) else 0
    if "ignore-signals" in prompt:  # simulate a process that won't stop politely
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    sessions = Path(state) / "project-sandbox" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    sid = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(6).hex()
    record_path = sessions / f"{sid}.json"
    record = {
        "session_id": sid,
        "container_name": f"project-sandbox-{sid}",
        "project_path": str(repo),
        "workspace_path": str(workspace),
        "agent": args.agent,
        "runtime": "fake",
        "status": "running",
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "ended_at": None,
        "exit_code": None,
    }
    record_path.write_text(json.dumps(record))
    print(f"fake project-sandbox: {' '.join(sys.argv[1:])}", flush=True)
    if feedback_root and (match := re.search(r"report:(success|failure|blocked)", prompt)):
        state_args: list[str] = []
        if "report-state:" in prompt:
            state, _ = json.JSONDecoder().raw_decode(prompt.split("report-state:", 1)[1].lstrip())
            state_path = feedback_root / "output" / "fake-state.json"
            state_path.write_text(json.dumps(state))
            state_args = ["--state-file", str(state_path)]
        subprocess.run(
            [
                sys.executable,
                str(feedback_root / "sbx-report"),
                "--context-dir",
                str(feedback_root),
                "finish",
                "--outcome",
                match[1],
                "--summary",
                "Fake task outcome",
                *state_args,
            ],
            check=True,
        )
    try:
        time.sleep(sleep)
    except KeyboardInterrupt:
        code = 130
    if args.log:
        Path(args.log).write_text(f"log for {args.branch}\n")
        Path(args.log).with_suffix(".md").write_text(f"# transcript\n\n{prompt}\n")
    record.update(status="completed", exit_code=code, ended_at=datetime.now(UTC).isoformat())
    record_path.write_text(json.dumps(record))
    if args.json_summary:
        Path(args.json_summary).write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "container_name": record["container_name"],
                    "workspace_path": str(workspace),
                    "agent": args.agent,
                    "bookmark": args.branch,
                    "exit_code": code,
                }
            )
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
