"""Offline regression tests for pin updates and resumable publishing."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import RepoCopy

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("update_pins", ROOT / "scripts/update-pins.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    for name in ("pyproject.toml", "uv.lock", ".github/workflows/ci.yml", "docs/development.md"):
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest)
    return module


def test_release_selection_filters_young_prereleases_and_downgrades(pins: ModuleType) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=3)
    releases = [
        pins.Release("v1.9.0", old),
        pins.Release("v1.10.0", old),
        pins.Release("v1.11.0", now),
        pins.Release("v2.0.0rc1", old),
    ]
    cutoff = now - timedelta(days=1)
    assert pins.select_release(releases, "1.8", cutoff).version == "v1.10.0"
    assert pins.select_release(releases, "1.10.0", cutoff) is None
    assert pins.select_release(releases, "3.0", cutoff) is None
    assert pins.version_key("1.0") == pins.version_key("v1.0.0")


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "1e300", "oops"])
def test_invalid_age_is_rejected(pins: ModuleType, value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        pins.nonnegative_hours(value)


def test_pypi_excludes_yanked_and_uses_last_upload(
    pins: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pins,
        "request_json",
        lambda _: {
            "releases": {
                "1.0": [{"yanked": True, "upload_time_iso_8601": "2020-01-01T00:00:00Z"}],
                "2.0": [
                    {"upload_time_iso_8601": "2020-01-01T00:00:00Z"},
                    {"upload_time_iso_8601": "2020-01-02T00:00:00Z"},
                ],
            }
        },
    )
    releases = pins.pypi_releases("example")
    assert len(releases) == 1
    assert releases[0].published == datetime(2020, 1, 2, tzinfo=UTC)


def test_python_pin_rolls_back_failed_lock_and_caps_lock_updates(
    pins: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = pins.ROOT
    (root / "pyproject.toml").write_text('[project]\ndependencies = ["ruff==1.0"]\n')
    (root / "uv.lock").write_text("""[[package]]
name = "ruff"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
[[package]]
name = "textual"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
[[package]]
name = "local-project"
version = "1.0"
source = { editable = "." }
""")
    monkeypatch.setattr(
        pins, "pypi_releases", lambda _: [pins.Release("2.0", datetime(2020, 1, 1, tzinfo=UTC))]
    )
    cutoff = datetime.now(UTC)
    updates = pins.python_updates(cutoff)
    assert len(updates) == 2
    originals = {path: path.read_bytes() for path in (root / "pyproject.toml", root / "uv.lock")}

    def fail(*args: object) -> None:
        (root / "uv.lock").write_text("incomplete")
        raise RuntimeError("solve failed")

    monkeypatch.setattr(pins, "lock", fail)
    with pytest.raises(RuntimeError, match="solve failed"):
        updates[0].apply()
    assert all(path.read_bytes() == content for path, content in originals.items())
    calls = []
    monkeypatch.setattr(pins, "lock", lambda *args: calls.append(args))
    updates[1].apply()
    assert calls == [(cutoff, "textual>=1.0,<=2.0")]


def test_ci_checksums_actions_and_smoke_docs_update_together(
    pins: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2020, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(pins, "github_releases", lambda _: [pins.Release("v99.0.0", old)])
    monkeypatch.setattr(pins, "pypi_releases", lambda _: [pins.Release("99.0.0", old)])
    updates = pins.ci_updates(datetime.now(UTC))
    ci = pins.ROOT / ".github/workflows/ci.yml"
    original = ci.read_text()
    jj = next(update for update in updates if update.label == "JJ_VERSION")

    def fail(_: str) -> str:
        raise OSError("download failed")

    monkeypatch.setattr(pins, "download_sha256", fail)
    with pytest.raises(OSError, match="download failed"):
        jj.apply()
    assert ci.read_text() == original
    monkeypatch.setattr(pins, "download_sha256", lambda _: "a" * 64)
    monkeypatch.setattr(pins, "request_json", lambda _: {"sha": "b" * 40})
    for update in updates:
        update.apply()
    content = ci.read_text()
    assert "JJ_VERSION: v99.0.0" in content
    assert f"JJ_SHA256: {'a' * 64}" in content
    assert 'UV_VERSION: "99.0.0"' in content
    assert content.count(f"uses: actions/checkout@{'b' * 40} # v99.0.0") == 3
    assert content.count(f"uses: astral-sh/setup-uv@{'b' * 40} # v99.0.0") == 3
    assert "project-sandbox==99.0.0" in content
    docs = (pins.ROOT / "docs/development.md").read_text()
    assert "project-sandbox==99.0.0" in docs
    assert "/blob/v99.0.0/" in docs


def test_pin_dry_run_does_not_apply(pins: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    update = pins.Update("test", "1", "2", lambda: calls.append("applied"))
    monkeypatch.setattr(pins, "python_updates", lambda _: [update])
    monkeypatch.setattr(pins, "ci_updates", lambda _: [])
    monkeypatch.setattr(sys, "argv", ["update-pins.py", "--dry-run", "--yes"])
    assert pins.main() == 0
    assert calls == []


def test_updated_lock_accepts_normal_locked_checks(
    pins: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise uv's real resolver-option handling without registry access.
    (pins.ROOT / "pyproject.toml").write_text(
        '[project]\nname = "probe"\nversion = "1.0"\nrequires-python = ">=3.12"\n'
    )
    monkeypatch.setenv("UV_OFFLINE", "1")
    pins.lock(datetime.now(UTC) - timedelta(days=1))
    subprocess.run(["uv", "lock", "--check"], cwd=pins.ROOT, check=True, capture_output=True)
    assert "exclude-newer" not in (pins.ROOT / "uv.lock").read_text()


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout


def build_release_repo(path: Path) -> None:
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Release Test")
    git(path, "config", "user.email", "release@example.com")
    (path / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/make-release.sh", path / "scripts/make-release.sh")
    (path / "scripts/test.sh").write_text("#!/bin/sh\nexit 0\n")
    (path / "scripts/test.sh").chmod(0o755)
    (path / "scripts/e2e-test.sh").write_text(
        '#!/bin/sh\necho \'["e2e", [], null]\' >> "$RELEASE_TEST_LOG"\n'
        'exit "${RELEASE_TEST_E2E_EXIT:-0}"\n'
    )
    (path / "scripts/e2e-test.sh").chmod(0o755)
    (path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    (path / "uv.lock").write_text("lock\n")
    (path / ".gitignore").write_text(".release-status/\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")


@pytest.fixture
def release_repo(tmp_path: Path, repo_copy: RepoCopy, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = repo_copy(build_release_repo, tmp_path / "source")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-u", "origin", "main")
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "fake"
    fake.write_text(
        f"#!{sys.executable}\n"
        + """
import json
import os
import pathlib
import sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["RELEASE_TEST_LOG"], "a") as stream:
    stream.write(json.dumps([name, args, os.environ.get("GIT_DIR")]) + "\\n")
if name == "gh":
    if args[0] == "api":
        print("HTTP 404", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if name == "python":
    if args == ["-I", "-c", "import project_sandbox_tui.app"]:
        sys.exit(0)
    os.execv(sys.executable, [sys.executable, *args])
if args[0] == "version":
    print("1.0.0")
elif args[0] == "build":
    dest = pathlib.Path(args[args.index("--out-dir") + 1])
    dest.mkdir(parents=True, exist_ok=True)
    for suffix in ("-py3-none-any.whl", ".tar.gz"):
        (dest / ("project_sandbox_tui-1.0.0" + suffix)).write_text("artifact")
elif args[0] == "venv":
    dest = pathlib.Path(args[1]) / "bin"
    dest.mkdir(parents=True)
    (dest / "python").symlink_to(pathlib.Path(__file__).resolve())
    cli = dest / "sbx-tui"
    cli.write_text("#!/bin/sh\\nexit 0\\n")
    cli.chmod(0o755)
"""
    )
    fake.chmod(0o755)
    for name in ("uv", "gh"):
        (binary / name).symlink_to(fake)
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("RELEASE_TEST_LOG", str(tmp_path / "calls.jsonl"))
    return path


def run_release(path: Path, answers: str = "") -> subprocess.CompletedProcess[str]:
    # Also exercise invocation from outside the checkout.
    return subprocess.run(
        [str(path / "scripts/make-release.sh")],
        cwd=path.parent,
        input=answers,
        capture_output=True,
        text=True,
    )


def release_calls(path: Path) -> list:
    return [json.loads(line) for line in (path.parent / "calls.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("use_jj", [False, True])
def test_release_resume_targets_verified_commit_and_keeps_git_dir_scoped(
    release_repo: Path, use_jj: bool
) -> None:
    path = release_repo
    commit = git(path, "rev-parse", "HEAD").strip()
    if use_jj:
        # A separate workspace has no .git; gh needs the backing repository's GIT_DIR.
        subprocess.run(
            ["jj", "git", "init", "--colocate"], cwd=path, check=True, capture_output=True
        )
        workspace = path.parent / "workspace"
        subprocess.run(
            ["jj", "workspace", "add", str(workspace), "-r", "main"],
            cwd=path,
            check=True,
            capture_output=True,
        )
        path = workspace
    first = run_release(path, "\ny\nn\n")
    assert first.returncode == 0, first.stdout + first.stderr
    state = path / ".release-status" / f"1.0.0-{commit}"
    assert (state / "github").exists()
    assert not (state / "testpypi").exists()
    second = run_release(path, "\ny\ntest-token\ny\nprod-token\n")
    assert second.returncode == 0, second.stdout + second.stderr
    calls = release_calls(path)
    assert sum(args[0] == "build" for name, args, _ in calls if name == "uv") == 1
    assert sum(name == "e2e" for name, _, _ in calls) == 1
    create = [args for name, args, _ in calls if name == "gh" and args[0] == "release"]
    assert len(create) == 1
    assert create[0][create[0].index("--target") + 1] == commit
    assert all(git_dir is None for name, _, git_dir in calls if name == "uv")
    if use_jj:
        assert all(git_dir for name, _, git_dir in calls if name == "gh")
    publishes = [args for name, args, _ in calls if name == "uv" and args[0] == "publish"]
    assert len(publishes) == 2
    assert all(
        len([arg for arg in args if arg.endswith((".whl", ".tar.gz"))]) == 2 for args in publishes
    )
    assert (state / "pypi").exists()
    assert "test-token" not in second.stdout + second.stderr


def test_release_rejects_dirty_source_and_changed_artifacts(release_repo: Path) -> None:
    path = release_repo
    (path / "untracked").write_text("dirty")
    result = run_release(path)
    assert result.returncode != 0 and "clean" in result.stderr
    (path / "untracked").unlink()
    result = run_release(path, "\ny\nn\n")
    assert result.returncode == 0, result.stdout + result.stderr
    wheel = next((path / ".release-status").glob("*/dist/*.whl"))
    wheel.write_text("changed")
    result = run_release(path, "\n")
    assert result.returncode != 0 and "artifacts changed" in result.stderr
    assert not any(args[0] == "publish" for name, args, _ in release_calls(path) if name == "uv")


def test_release_rejects_existing_remote_tag(release_repo: Path) -> None:
    git(release_repo, "tag", "v1.0.0")
    git(release_repo, "push", "origin", "v1.0.0")
    result = run_release(release_repo, "\n")
    assert result.returncode != 0 and "already exists" in result.stderr
    assert not any(name == "gh" for name, _, _ in release_calls(release_repo))


def test_release_does_not_reuse_checks_for_a_different_commit(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = run_release(release_repo, "\nn\n")
    assert first.returncode == 0, first.stdout + first.stderr
    (release_repo / "new-source").write_text("new commit")
    git(release_repo, "add", "new-source")
    git(release_repo, "commit", "-m", "changed source")
    git(release_repo, "push", "origin", "main")
    # A failing smoke test on the new commit stops before building.
    monkeypatch.setenv("RELEASE_TEST_E2E_EXIT", "1")
    second = run_release(release_repo, "\n")
    assert second.returncode != 0 and "Container smoke test failed" in second.stderr
    calls = release_calls(release_repo)
    assert sum(args[0] == "sync" for name, args, _ in calls if name == "uv") == 2
    assert sum(name == "e2e" for name, _, _ in calls) == 2
    assert sum(args[0] == "build" for name, args, _ in calls if name == "uv") == 1
