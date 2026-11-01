from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from project_sandbox_tui import launchers, notify
from tests.conftest import wait_for


@pytest.mark.parametrize("launcher", ["notification", "terminal"])
def test_detached_helper_is_reaped_without_another_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: str
) -> None:
    release = tmp_path / "release"
    argv = [
        sys.executable,
        "-c",
        "import pathlib, sys, time; "
        "p = pathlib.Path(sys.argv[1]); "
        "exec('while not p.exists(): time.sleep(0.01)'); sys.exit(7)",
        str(release),
    ]
    children: list[subprocess.Popen[Any]] = []
    popen = subprocess.Popen

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        proc = popen(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", spawn)
    if launcher == "notification":
        assert notify.send("title", "body", shlex.join(argv))
    else:
        launchers.start(launchers.ExternalLaunch(argv, "", detached=True), cwd=tmp_path)
    [proc] = children
    assert proc.returncode is None  # the launch returned while the child was still running
    release.touch()
    # poll()/wait() here would hide the leak by reaping the child ourselves.
    wait_for(lambda: proc.returncode is not None)
    assert proc.returncode == 7
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)


@pytest.mark.parametrize("grandchild", [False, True])
def test_test_teardown_stops_children_even_after_an_assertion_failure(
    tmp_path: Path, grandchild: bool
) -> None:
    if grandchild and sys.platform != "linux":
        pytest.skip("Linux subreaper regression; other systems use init to reap orphans")
    test = tmp_path / "test_failure.py"
    pid_file = tmp_path / "pid"
    grandchild_file = tmp_path / "grandchild"
    script = "import time; time.sleep(60)"
    if grandchild:
        script = (
            "import subprocess, sys, time; from pathlib import Path; "
            f"child = subprocess.Popen([sys.executable, '-c', {script!r}]); "
            f"Path({str(grandchild_file)!r}).write_text(str(child.pid)); time.sleep(60)"
        )
    test.write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "from tests.conftest import child_processes, wait_for\n"
        "def test_failure():\n"
        f"    proc = subprocess.Popen([sys.executable, '-c', {script!r}], start_new_session=True)\n"
        f"    Path({str(pid_file)!r}).write_text(str(proc.pid))\n"
        + (f"    wait_for(Path({str(grandchild_file)!r}).exists)\n" if grandchild else "")
        + "    assert False, 'deliberate failure after spawning a child'\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(test)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1 and "deliberate failure" in result.stdout, result.stdout
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        if grandchild:
            with pytest.raises(ProcessLookupError):
                os.kill(int(grandchild_file.read_text()), 0)
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            if sys.platform == "linux":
                # If the regression fails, the outer test's subreaper owns any
                # descendants the inner pytest failed to collect.
                with contextlib.suppress(ChildProcessError):
                    while True:
                        os.waitpid(-int(pid_file.read_text()), 0)
