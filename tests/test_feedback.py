from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from project_sandbox_tui import feedback, feedback_transport, initialization
from project_sandbox_tui.config import Config, ConfigError


@pytest.fixture
def reporting_dir(tmp_path: Path) -> Path:
    root = tmp_path / "runs" / "abc123"
    feedback_transport.prepare(root, "abc123", "task", {"input": 42})
    return root


def test_helper_progress_final_and_standalone_execution(
    reporting_dir: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    state.write_text('{"next":"review"}')
    helper = reporting_dir / "sbx-report"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(helper),
            "--context-dir",
            str(reporting_dir),
            "update",
            "--summary",
            "working",
            "--state-file",
            str(state),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    first = feedback.read_json(reporting_dir / "output/feedback.json")
    assert first["phase"] == "running" and first["sequence"] == 1
    assert first["state"] == {"next": "review"}
    final = feedback.report(reporting_dir, "done", "success")
    assert final["sequence"] == 2 and final["state"] == first["state"]
    with pytest.raises(feedback.FeedbackError, match="sealed"):
        feedback.report(reporting_dir, "changed", "failure")
    assert feedback.read_json(reporting_dir / "output/feedback.json") == final


def test_parallel_helper_updates_do_not_lose_sequences(reporting_dir: Path) -> None:
    def update(index: int) -> int:
        return feedback.report(reporting_dir, f"step {index}")["sequence"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(update, range(12))) == list(range(1, 13))
    assert feedback.read_json(reporting_dir / "output/feedback.json")["sequence"] == 12


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"run_id": "stale"},
        {"sequence": 0},
        {"sequence": True},
        {"sequence": 1.2},
        {"phase": "unknown"},
        {"outcome": "success"},
        {"summary": ""},
        {"state": []},
        {"extra": 1},
        {"checks": [{"name": "tests", "outcome": "maybe"}]},
        {"state": {"value": float("nan")}},
        {"state": {"value": float("inf")}},
    ],
)
def test_reject_invalid_reports(reporting_dir: Path, change: dict) -> None:
    raw = feedback.report(reporting_dir, "working")
    raw.update(change)
    with pytest.raises(feedback.FeedbackError):
        feedback.validate(raw, "abc123")


@pytest.mark.parametrize(
    "content",
    [
        b'{"key":1,"key":2}',
        b'{"key":NaN}',
        b'{"key":1e999}',
        b"\xff",
        b'{"partial":',
        b"[" * 1000 + b"]" * 1000,
        b'"' + b"x" * feedback.MAX_BYTES + b'"',
    ],
)
def test_reject_bad_json(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "bad.json"
    path.write_bytes(content)
    with pytest.raises(feedback.FeedbackError):
        feedback.read_json(path)


def test_reject_symlinks_and_fifos(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("{}")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        feedback.read_json(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(feedback.FeedbackError, match="regular"):
        feedback.read_json(fifo)


def test_sequence_and_missing_fields(reporting_dir: Path) -> None:
    first = feedback.report(reporting_dir, "first")
    assert feedback.accept(first, "abc123", first) == first
    changed = dict(first, summary="different")
    with pytest.raises(feedback.FeedbackError, match="sequence"):
        feedback.accept(changed, "abc123", first)
    del changed["phase"]
    with pytest.raises(feedback.FeedbackError, match="fields"):
        feedback.validate(changed, "abc123")


@pytest.mark.parametrize("outcome", [None, "success"])
def test_state_type_changes_require_a_new_sequence(
    reporting_dir: Path, outcome: str | None
) -> None:
    previous = feedback.report(reporting_dir, "snapshot", outcome, {"nested": [1]})
    # JSON booleans and numbers differ even though Python considers True == 1.
    changed = dict(previous, state={"nested": [True]})
    with pytest.raises(feedback.FeedbackError, match="sealed" if outcome else "sequence"):
        feedback.accept(changed, "abc123", previous)
    reordered = dict(reversed(list(previous.items())))
    assert feedback.accept(reordered, "abc123", previous) == previous


def test_init_preserves_user_guidance_and_skill_edits(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("User guidance with no final newline")
    initialization.install_feedback(tmp_path)
    first = agents.read_text()
    assert first.startswith("User guidance with no final newline")
    initialization.install_feedback(tmp_path)
    assert agents.read_text() == first
    agents.write_text(first.replace("publish progress", "custom guidance"))
    skill = tmp_path / initialization.FEEDBACK_DIRS[0] / "SKILL.md"
    skill.write_text("custom skill")
    messages = initialization.install_feedback(tmp_path)
    assert "custom guidance" in agents.read_text()
    assert skill.read_text() == "custom skill"
    assert len([message for message in messages if "preserved" in message]) == 2


def test_feedback_doctor_is_read_only_and_reports_drift(tmp_path: Path) -> None:
    from project_sandbox_tui.doctor import check_feedback

    cfg = Config()
    checks = check_feedback(cfg, tmp_path, lambda *_: (0, "--mount MOUNT"))
    assert [c.status for c in checks] == ["ok", "warn"]
    assert not (tmp_path / "AGENTS.md").exists()
    initialization.install_feedback(tmp_path)
    assert all(
        c.status == "ok" for c in check_feedback(cfg, tmp_path, lambda *_: (0, "--mount MOUNT"))
    )
    (tmp_path / initialization.FEEDBACK_DIRS[0] / "SKILL.md").write_text("customized")
    assert check_feedback(cfg, tmp_path, lambda *_: (0, "--mount MOUNT"))[-1].status == "warn"
    cfg.feedback.require_reported_success = True
    assert check_feedback(cfg, tmp_path, lambda *_: (0, "old CLI"))[0].status == "fail"


def test_feedback_config_validation() -> None:
    from project_sandbox_tui.config import from_dict

    for data in (
        {"max_steps": 0},
        {"max_attempts": 101},
        {"max_attempts": True},
        {"retry_outcomes": ["success"]},
        {"on_success": "missing"},
        {"enabled": False, "require_reported_success": True},
    ):
        with pytest.raises(ConfigError):
            from_dict({"feedback": data}).validate()
