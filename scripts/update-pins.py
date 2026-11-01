#!/usr/bin/env python3
"""Review and update Python and CI pins (stable releases at least 24 hours old)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r'(?P<quote>["\'])(?P<name>[\w.-]+)==(?P<version>[^"\']+)(?P=quote)')
ACTION = re.compile(r"uses: (?P<repo>[\w.-]+/[\w.-]+)@(?P<sha>[a-f0-9]{40}) # (?P<tag>v[\d.]+)")


@dataclass(frozen=True)
class Release:
    version: str
    published: datetime


@dataclass(frozen=True)
class Update:
    label: str
    current: str
    target: str
    apply: Callable[[], None]


def version_key(version: str) -> tuple[tuple[int, ...], int] | None:
    """Compare stable numeric versions; deliberately exclude prereleases."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:\.post(\d+))?", version)
    if not match:
        return None
    parts = list(map(int, match[1].split(".")))
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts), int(match[2] or 0)


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def request_json(url: str):
    headers = {"User-Agent": "project-sandbox-tui-update-pins"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def pypi_releases(name: str) -> list[Release]:
    print(f"Checking PyPI: {name}", flush=True)
    data = request_json(f"https://pypi.org/pypi/{name}/json")
    result = []
    for version, files in data["releases"].items():
        dates = [timestamp(f["upload_time_iso_8601"]) for f in files if not f.get("yanked")]
        if dates:
            result.append(Release(version, max(dates)))
    return result


def github_releases(repo: str) -> list[Release]:
    print(f"Checking GitHub: {repo}", flush=True)
    result = []
    for page in range(1, 101):
        data = request_json(
            f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
        )
        if not isinstance(data, list):
            raise RuntimeError(f"Invalid releases response for {repo}")
        result.extend(
            Release(item["tag_name"], timestamp(item["published_at"]))
            for item in data
            if not item.get("draft") and not item.get("prerelease")
        )
        if len(data) < 100:
            return result
    raise RuntimeError(f"Too many release pages for {repo}")


def select_release(releases: list[Release], current: str, cutoff: datetime) -> Release | None:
    current_key = version_key(current)
    if current_key is None:
        raise RuntimeError(f"Cannot safely compare current version: {current}")
    eligible = [
        release
        for release in releases
        if (key := version_key(release.version)) is not None
        and key > current_key
        and release.published <= cutoff
    ]
    return max(eligible, key=lambda r: version_key(r.version) or ((), 0), default=None)


def replace(path: Path, replacements: list[tuple[str, str]]) -> None:
    text = path.read_text()
    for old, new in replacements:
        if old not in text:
            raise RuntimeError(f"Expected pin missing from {path}: {old}")
        text = text.replace(old, new)
    path.write_text(text)


def lock(cutoff: datetime, *packages: str) -> None:
    command = ["uv", "lock", "--exclude-newer", cutoff.isoformat()]
    for package in packages:
        command.extend(["--upgrade-package", package])
    path = ROOT / "uv.lock"
    original = path.read_bytes()
    try:
        subprocess.run(command, cwd=ROOT, check=True)
        # uv records --exclude-newer in the lock's resolver options. Drop that
        # temporary option while retaining the solved pins, so CI's ordinary
        # `uv sync --locked` does not reject the resulting lockfile.
        subprocess.run(["uv", "lock"], cwd=ROOT, check=True)
    except Exception:
        path.write_bytes(original)
        raise


def python_updates(cutoff: datetime) -> list[Update]:
    project = ROOT / "pyproject.toml"
    lockfile = ROOT / "uv.lock"
    updates = []
    direct = set()
    for match in PIN.finditer(project.read_text()):
        name, current = match["name"], match["version"]
        direct.add(name.lower().replace("_", "-"))
        release = select_release(pypi_releases(name), current, cutoff)
        if release is None:
            continue
        old, new = match[0], f"{match['quote']}{name}=={release.version}{match['quote']}"

        def apply(old=old, new=new):
            # A failed solve must not leave pyproject.toml and uv.lock inconsistent.
            originals = {path: path.read_bytes() for path in (project, lockfile)}
            try:
                replace(project, [(old, new)])
                lock(cutoff)
            except Exception:
                for path, content in originals.items():
                    path.write_bytes(content)
                raise

        updates.append(Update(f"Python {name}", current, release.version, apply))

    for package in tomllib.loads(lockfile.read_text())["package"]:
        name, current = package["name"], package["version"]
        if name in direct or "registry" not in package.get("source", {}):
            continue
        release = select_release(pypi_releases(name), current, cutoff)
        if release:
            # uv retains the project's compatibility bounds. The cap prevents the
            # solver selecting something newer than the release we just reviewed.
            constraint = f"{name}>={current},<={release.version}"
            updates.append(
                Update(
                    f"uv.lock {name} (within project bounds)",
                    current,
                    release.version,
                    lambda constraint=constraint: lock(cutoff, constraint),
                )
            )
    return updates


def download_sha256(url: str) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as response:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def ci_updates(cutoff: datetime) -> list[Update]:
    path = ROOT / ".github/workflows/ci.yml"
    text = path.read_text()
    updates = []
    for name, repo in (("UV_VERSION", "astral-sh/uv"), ("JJ_VERSION", "jj-vcs/jj")):
        match = re.search(rf'(?m)^(\s*{name}: )("?)(v?[\d.]+)\2', text)
        if not match:
            raise RuntimeError(f"Missing {name} in {path}")
        current = match[3]
        release = select_release(github_releases(repo), current, cutoff)
        if release is None:
            continue
        latest = release.version.removeprefix("v")
        if name == "JJ_VERSION":
            latest = f"v{latest}"
        old, new = match[0], f"{match[1]}{match[2]}{latest}{match[2]}"

        def apply(name=name, latest=latest, old=old, new=new):
            replacements = [(old, new)]
            if name == "JJ_VERSION":
                checksum = re.search(r"JJ_SHA256: ([a-f0-9]{64})", path.read_text())
                if not checksum:
                    raise RuntimeError("Missing JJ_SHA256")
                digest = download_sha256(
                    f"https://github.com/jj-vcs/jj/releases/download/{latest}/"
                    f"jj-{latest}-x86_64-unknown-linux-musl.tar.gz"
                )
                replacements.append((checksum[0], f"JJ_SHA256: {digest}"))
            # Both jj pins are written only after the download succeeds.
            replace(path, replacements)

        updates.append(Update(name, current, latest, apply))

    seen = set()
    for match in ACTION.finditer(text):
        repo, current = match["repo"], match["tag"]
        if repo in seen:
            continue
        seen.add(repo)
        release = select_release(github_releases(repo), current, cutoff)
        if release is None:
            continue
        old, latest = match[0], release.version

        def apply(repo=repo, latest=latest, old=old):
            # The commits endpoint peels annotated tags to a commit, too.
            sha = request_json(f"https://api.github.com/repos/{repo}/commits/{latest}")["sha"]
            if not re.fullmatch(r"[a-f0-9]{40}", sha):
                raise RuntimeError(f"Invalid commit SHA for {repo}@{latest}")
            replace(path, [(old, f"uses: {repo}@{sha} # {latest}")])

        updates.append(Update(f"Action {repo}", current, latest, apply))

    match = re.search(r"project-sandbox==([\d.]+)", text)
    if not match:
        raise RuntimeError("Missing project-sandbox smoke-test pin")
    current = match[1]
    release = select_release(pypi_releases("project-sandbox"), current, cutoff)
    if release:
        latest = release.version

        def apply():
            docs = ROOT / "docs/development.md"
            originals = {file: file.read_bytes() for file in (path, docs)}
            try:
                for file in (path, docs):
                    replace(file, [(f"project-sandbox=={current}", f"project-sandbox=={latest}")])
                # Keep the upstream workflow link aligned with the tested version.
                content = docs.read_text().replace(f"/blob/v{current}/", f"/blob/v{latest}/")
                docs.write_text(content)
            except Exception:
                for file, content in originals.items():
                    file.write_bytes(content)
                raise

        updates.append(Update("project-sandbox smoke test", current, latest, apply))
    return updates


def nonnegative_hours(value: str) -> float:
    try:
        hours = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected hours as a number") from exc
    if not math.isfinite(hours) or hours < 0 or hours > 876000:
        raise argparse.ArgumentTypeError("hours must be finite and between 0 and 876000")
    return hours


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-y", "--yes", action="store_true", help="accept all updates")
    parser.add_argument("--dry-run", action="store_true", help="show updates without writing files")
    parser.add_argument("--min-age-hours", type=nonnegative_hours, default=24.0)
    args = parser.parse_args()
    cutoff = datetime.now(UTC) - timedelta(hours=args.min_age_hours)
    updates = python_updates(cutoff) + ci_updates(cutoff)
    for update in updates:
        message = f"{update.label}: {update.current} -> {update.target}"
        print(message)
        if not args.dry_run and (
            args.yes or input("Apply? [y/N] ").lower().strip() in {"y", "yes"}
        ):
            update.apply()
    print("Review the diff and run the development checks." if updates else "Pins are current.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
