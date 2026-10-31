"""Exercise the artifacts users receive, including builds from a jj checkout."""

import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path


def test_release_artifacts_exclude_local_state_and_build_from_sdist(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    for name in ("src", "docs", "tests", "scripts"):
        shutil.copytree(root / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "README.md", "LICENSE", "uv.lock"):
        shutil.copy2(root / name, source / name)
    # No .gitignore: source archives and standalone jj workspaces must be safe too.
    for name in (
        ".jj/repo",
        ".git/config",
        ".env",
        ".devcontainer/credentials.json",
        ".project-sandbox/session.json",
        ".project-sandbox-tui.toml",
        "private-notes.txt",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE RELEASE SENTINEL", encoding="utf-8")

    # uv builds the wheel from the sdist by default, proving the manifest is complete.
    subprocess.run(
        ["uv", "build", "--offline", "--out-dir", str(tmp_path / "dist"), str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    with tarfile.open(tmp_path / "dist" / f"project_sandbox_tui-{version}.tar.gz") as archive:
        names = {
            Path(name).relative_to(f"project_sandbox_tui-{version}") for name in archive.getnames()
        }
        assert {name.parts[0] for name in names} <= {
            "src",
            "docs",
            "tests",
            "scripts",
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "uv.lock",
            "PKG-INFO",
        }
        assert Path("src/project_sandbox_tui/cli.py") in names
        assert Path("scripts/test.sh") in names
        assert Path("LICENSE") in names
    with zipfile.ZipFile(
        tmp_path / "dist" / f"project_sandbox_tui-{version}-py3-none-any.whl"
    ) as wheel:
        names = set(wheel.namelist())
        metadata = f"project_sandbox_tui-{version}.dist-info"
        assert "project_sandbox_tui/cli.py" in names
        assert "project_sandbox_tui/app.py" in names
        assert f"{metadata}/licenses/LICENSE" in names
        assert (
            "sbx-tui = project_sandbox_tui.cli:main"
            in wheel.read(f"{metadata}/entry_points.txt").decode()
        )
        assert all(name.startswith(("project_sandbox_tui/", f"{metadata}/")) for name in names)
