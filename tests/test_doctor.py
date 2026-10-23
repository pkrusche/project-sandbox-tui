from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from project_sandbox_tui import doctor
from project_sandbox_tui.config import Config


def runner(responses: Mapping[str, tuple[int, str] | None]) -> doctor.Runner:
    """Answer by the argv's first word(s); None means "not found"."""

    def run(argv: list[str], timeout: float) -> tuple[int, str]:
        for prefix, response in responses.items():
            if " ".join(argv).startswith(prefix):
                if response is None:
                    raise FileNotFoundError(argv[0])
                return response
        raise AssertionError(f"unexpected command {argv}")

    return run


HEALTHY = {
    "jj --version": (0, "jj 0.46.0-7d382314"),
    "project-sandbox --version": (0, "project-sandbox 0.2.2"),
    "docker info": (0, "Server Version: 27"),
}


def by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


def test_healthy(repo: Path) -> None:
    # The fake runner serves Docker; don't auto-detect a runtime on the test host.
    (repo / ".project-sandbox-tui.toml").write_text(
        '[project]\nbase_image = "img"\n[sandbox]\nruntime = "docker"\n'
    )
    from project_sandbox_tui.config import ProjectTrust

    ProjectTrust.check(repo).trust(repo)
    checks = by_name(doctor.run_checks(repo, runner=runner(HEALTHY), which=lambda c: f"/bin/{c}"))
    for name in ("jj", "repository", "project-sandbox", "container runtime"):
        assert checks[name].status == "ok", checks[name]
    assert checks["project-sandbox"].detail == "0.2.2 (project-sandbox)"
    assert "image" not in checks and "project config" not in checks


@pytest.mark.parametrize(
    ("output", "status"),
    [("jj 0.46.1", "ok"), ("jj 0.38.0", "warn"), ("garbage", "fail"), (None, "fail")],
)
def test_jj_versions(output: str | None, status: str) -> None:
    check = doctor.check_jj(runner({"jj": None if output is None else (0, output)}))
    assert check.status == status


@pytest.mark.parametrize(
    ("response", "status", "text"),
    [
        ((0, "project-sandbox 0.3.0"), "ok", "0.3.0"),
        ((0, "project-sandbox 0.2.1"), "fail", "too old"),
        ((1, "Traceback\nboom"), "fail", "boom"),
        (None, "fail", "not found"),
    ],
)
def test_project_sandbox_versions(response: tuple[int, str] | None, status: str, text: str) -> None:
    cfg = Config()
    cfg.sandbox.command = "uvx project-sandbox"
    check = doctor.check_project_sandbox(cfg, runner({"uvx project-sandbox": response}))
    assert check.status == status and text in check.detail


def test_runtime_checks() -> None:
    cfg = Config()
    cfg.sandbox.runtime = "podman"
    up = runner({"podman info": (0, "ok")})
    assert doctor.check_runtime(cfg, up, which=lambda c: f"/bin/{c}").status == "ok"
    missing = doctor.check_runtime(cfg, up, which=lambda c: None)
    assert missing.status == "fail" and "podman not found" in missing.detail
    down = doctor.check_runtime(
        cfg, runner({"podman info": (125, "cannot connect")}), which=lambda c: f"/bin/{c}"
    )
    assert down.status == "fail" and "cannot connect" in down.detail
    cfg.sandbox.runtime = "apple-container"
    stopped = doctor.check_runtime(
        cfg, runner({"container system status": (1, "not running")}), which=lambda c: c
    )
    assert stopped.hint == "container system start"


def test_outside_a_repository_and_main(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "run", runner({**HEALTHY, "project-sandbox --version": None}))
    monkeypatch.setattr(doctor.shutil, "which", lambda c: f"/bin/{c}")
    monkeypatch.setattr(doctor, "runtime_cli", lambda runtime: "docker")
    assert doctor.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "! repository:" in out and "✗ project-sandbox" in out
    assert "→ uv tool install project-sandbox" in out and "1 problem found" in out
