from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from project_sandbox_tui import octopus
from project_sandbox_tui.octopus import OctopusError, OctopusPlan
from project_sandbox_tui.vcs import detect, workspace_path
from tests.conftest import run


def git(repo: Path, *args: str) -> str:
    return run("git", *args, cwd=repo)


def commit_file(repo: Path, branch: str, start: str, name: str, text: str) -> None:
    git(repo, "checkout", "-q", "-b", branch, start)
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"{branch}: {name}")
    git(repo, "checkout", "-q", "main")


@pytest.fixture
def git_branches(tmp_path: Path) -> Path:
    """main + a, b (independent files) + c (conflicts with a on shared.txt)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "shared.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    commit_file(repo, "a", "main", "a.txt", "a\n")
    git(repo, "checkout", "-q", "a")
    (repo / "shared.txt").write_text("from a\n")
    git(repo, "commit", "-q", "-am", "a: shared")
    git(repo, "checkout", "-q", "main")
    commit_file(repo, "b", "main", "b.txt", "b\n")
    commit_file(repo, "c", "main", "shared.txt", "from c\n")
    return repo


def test_names_and_validation() -> None:
    assert octopus.default_result_name("merge", ["feat/x", "fix y"]) == "merge/feat-x-fix-y"
    plan = OctopusPlan("merge", ["a", "b"], "merge/a-b")
    octopus.validate(plan, {"a", "b"})
    for bad, message in [
        (OctopusPlan("merge", ["a"], "r"), "at least two"),
        (OctopusPlan("merge", ["a", "a"], "r"), "twice"),
        (OctopusPlan("merge", ["a", "z"], "r"), "no such branch"),
        (OctopusPlan("merge", ["a", "b"], "a"), "already exists"),
        (OctopusPlan("merge", ["a", "b"], "bad name"), "invalid"),
        (OctopusPlan("rebase", ["a", "b"], "r"), "needs a base"),
    ]:
        with pytest.raises(OctopusError, match=message):
            octopus.validate(bad, {"a", "b"})


def test_prompt_template_placeholders() -> None:
    plan = OctopusPlan("rebase", ["a", "b"], "rebase/a-b", base="main")
    text = octopus.render_prompt("{inputs}|{inputs_plain}|{result}|{base}|{first}|{rest}", plan)
    assert text == "`a`, `b`|a b|rebase/a-b|main|a|`b`"
    for (operation, kind), template in octopus.DEFAULT_TEMPLATES.items():
        op = cast(octopus.Operation, operation)
        rendered = octopus.render_prompt(template, OctopusPlan(op, ["a", "b"], "r", "m"))
        assert "{" not in rendered, (operation, kind)
        assert "do not push" in rendered.lower()


def test_git_octopus_merge_clean(git_branches: Path) -> None:
    plan = OctopusPlan("merge", ["main", "a", "b"], "merge/all")
    state = octopus.prepare("git", git_branches, plan)
    assert state.clean and not state.conflicts
    wt = workspace_path(git_branches, "git", "merge/all", None)
    assert state.workspace == wt and (wt / "a.txt").exists() and (wt / "b.txt").exists()
    parents = git(wt, "log", "-1", "--format=%P").split()
    assert len(parents) == 3  # a real octopus merge
    assert git(git_branches, "rev-parse", "a") == git(git_branches, "rev-parse", "a")  # untouched


def test_git_merge_stops_at_first_conflict(git_branches: Path) -> None:
    plan = OctopusPlan("merge", ["a", "b", "c"], "merge/abc")
    state = octopus.prepare("git", git_branches, plan)
    assert not state.clean
    assert state.conflicts == ["shared.txt"]
    wt = workspace_path(git_branches, "git", "merge/abc", None)
    assert (wt / "b.txt").exists()  # b merged before c conflicted
    assert (wt / ".git").exists() and "MERGE_HEAD" in git(
        wt, "rev-parse", "--git-path", "MERGE_HEAD"
    )
    assert "## Current state" in state.report() and "`shared.txt`" in state.report()


def test_git_rebase_stacks_and_stops_on_conflict(git_branches: Path) -> None:
    plan = OctopusPlan("rebase", ["b", "a", "c"], "rebase/bac", base="main")
    state = octopus.prepare("git", git_branches, plan)
    wt = workspace_path(git_branches, "git", "rebase/bac", None)
    log = git(wt, "log", "--format=%s", "main..HEAD").splitlines()
    assert log == ["a: shared", "a: a.txt", "b: b.txt"]  # b then a, linear
    assert not state.clean and state.conflicts == ["shared.txt"]  # c conflicts with a
    assert state.pending == []  # c had a single commit, now in progress


def test_git_hooks_never_run_on_the_host(git_branches: Path) -> None:
    hook = git_branches / ".git" / "hooks" / "post-merge"
    marker = git_branches / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    octopus.prepare("git", git_branches, OctopusPlan("merge", ["main", "b"], "merge/hooks"))
    assert not marker.exists()


def test_git_refuses_existing_worktree_dir(git_branches: Path) -> None:
    workspace_path(git_branches, "git", "merge/x", None).mkdir(parents=True)
    with pytest.raises(OctopusError, match="already exists"):
        octopus.prepare("git", git_branches, OctopusPlan("merge", ["a", "b"], "merge/x"))


# ---------------------------------------------------------------- jj


@pytest.fixture
def jj_branches(tmp_path: Path) -> Path:
    if shutil.which("jj") is None:
        pytest.skip("jj not installed")
    repo = tmp_path / "jj"
    repo.mkdir()

    def jj(*args: str) -> str:
        return run("jj", *args, cwd=repo)

    jj("git", "init", "--quiet")
    (repo / "shared.txt").write_text("base\n")
    jj("commit", "--quiet", "-m", "base")
    jj("bookmark", "create", "--quiet", "main", "-r", "@-")
    for name, path, text in [("a", "shared.txt", "from a\n"), ("b", "b.txt", "b\n"),
                             ("c", "shared.txt", "from c\n")]:  # fmt: skip
        jj("new", "--quiet", "main")
        (repo / path).write_text(text)
        jj("commit", "--quiet", "-m", f"{name}: {path}")
        jj("bookmark", "create", "--quiet", name, "-r", "@-")
    jj("new", "--quiet", "main")
    return repo


def jj_out(repo: Path, *args: str) -> str:
    return run("jj", "--no-pager", "--color=never", "--ignore-working-copy", *args, cwd=repo)


def test_jj_merge_records_conflicts_in_commit(jj_branches: Path) -> None:
    at_before = jj_out(jj_branches, "log", "--no-graph", "-r", "@", "-T", "commit_id")
    state = octopus.prepare("jj", jj_branches, OctopusPlan("merge", ["a", "b", "c"], "merge/abc"))
    assert not state.clean and state.conflicts == ["shared.txt"]
    parents = jj_out(jj_branches, "log", "--no-graph", "-r", '"merge/abc"', "-T",
                     'parents.map(|p| p.bookmarks()).join(",")')  # fmt: skip
    assert sorted(parents.split(",")) == ["a", "b", "c"]
    # the user's own working copy is not moved by --no-edit
    assert jj_out(jj_branches, "log", "--no-graph", "-r", "@", "-T", "commit_id") == at_before
    assert {b.name for b in detect(jj_branches).list_branches()} >= {"merge/abc", "a"}


def test_jj_merge_clean(jj_branches: Path) -> None:
    state = octopus.prepare("jj", jj_branches, OctopusPlan("merge", ["a", "b"], "merge/ab"))
    assert state.clean and state.conflicts == []


def test_jj_rebase_duplicates_without_touching_inputs(jj_branches: Path) -> None:
    a_before = jj_out(jj_branches, "log", "--no-graph", "-r", "a", "-T", "commit_id")
    plan = OctopusPlan("rebase", ["b", "a", "c"], "rebase/bac", base="main")
    state = octopus.prepare("jj", jj_branches, plan)
    stack = jj_out(jj_branches, "log", "--no-graph", "-r", 'main.."rebase/bac"', "-T",
                   'description.first_line() ++ "\\n"').splitlines()  # fmt: skip
    assert [s for s in stack if s] == ["c: shared.txt", "a: shared.txt", "b: b.txt"]
    assert not state.clean and len(state.conflicts) == 1  # c conflicts with a
    assert jj_out(jj_branches, "log", "--no-graph", "-r", "a", "-T", "commit_id") == a_before


def test_jj_rebase_unknown_base(jj_branches: Path) -> None:
    with pytest.raises(OctopusError, match="no such revision|doesn't exist|not found"):
        octopus.prepare("jj", jj_branches, OctopusPlan("rebase", ["a", "b"], "r", base="nope"))


@pytest.mark.parametrize("operation", ["merge", "rebase"])
@pytest.mark.parametrize("conflicted", [False, True])
@pytest.mark.parametrize("kind", ["git", "jj"])
def test_bash_summary(request, operation, conflicted, kind) -> None:
    repo = request.getfixturevalue(f"{kind}_branches")
    inputs = ["a", "c"] if conflicted else ["a", "b"]
    plan = OctopusPlan(operation, inputs, "summary", base="main")
    state = octopus.prepare(kind, repo, plan)
    if kind == "git":
        cwd = state.workspace
    else:
        run("jj", "new", "summary", cwd=repo)
        cwd = repo
    result = subprocess.run(
        ["bash", "-c", octopus.bash_prompt(kind, plan)],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (1 if conflicted else 0), result.stderr
    assert "shared.txt" in result.stdout or "b.txt" in result.stdout
