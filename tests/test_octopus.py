from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from project_sandbox_tui import octopus
from project_sandbox_tui.octopus import OctopusError, OctopusPlan
from project_sandbox_tui.vcs import detect
from tests.conftest import run


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
    for operation, template in octopus.DEFAULT_TEMPLATES.items():
        op = cast(octopus.Operation, operation)
        rendered = octopus.render_prompt(template, OctopusPlan(op, ["a", "b"], "r", "m"))
        assert "{" not in rendered, operation
        assert "do not push" in rendered.lower()


@pytest.fixture
def jj_branches(tmp_path: Path) -> Path:
    """main + a (conflicts with c on shared.txt), b (independent file), c."""
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
    state = octopus.prepare(jj_branches, OctopusPlan("merge", ["a", "b", "c"], "merge/abc"))
    assert not state.clean and state.conflicts == ["shared.txt"]
    parents = jj_out(jj_branches, "log", "--no-graph", "-r", '"merge/abc"', "-T",
                     'parents.map(|p| p.bookmarks()).join(",")')  # fmt: skip
    assert sorted(parents.split(",")) == ["a", "b", "c"]
    # the user's own working copy is not moved by --no-edit
    assert jj_out(jj_branches, "log", "--no-graph", "-r", "@", "-T", "commit_id") == at_before
    assert {b.name for b in detect(jj_branches).list_branches()} >= {"merge/abc", "a"}


def test_jj_merge_clean(jj_branches: Path) -> None:
    state = octopus.prepare(jj_branches, OctopusPlan("merge", ["a", "b"], "merge/ab"))
    assert state.clean and state.conflicts == []


def test_jj_rebase_duplicates_without_touching_inputs(jj_branches: Path) -> None:
    a_before = jj_out(jj_branches, "log", "--no-graph", "-r", "a", "-T", "commit_id")
    plan = OctopusPlan("rebase", ["b", "a", "c"], "rebase/bac", base="main")
    state = octopus.prepare(jj_branches, plan)
    stack = jj_out(jj_branches, "log", "--no-graph", "-r", 'main.."rebase/bac"', "-T",
                   'description.first_line() ++ "\\n"').splitlines()  # fmt: skip
    assert [s for s in stack if s] == ["c: shared.txt", "a: shared.txt", "b: b.txt"]
    assert not state.clean and len(state.conflicts) == 1  # c conflicts with a
    assert jj_out(jj_branches, "log", "--no-graph", "-r", "a", "-T", "commit_id") == a_before


def test_jj_rebase_unknown_base(jj_branches: Path) -> None:
    with pytest.raises(OctopusError, match="no such revision|doesn't exist|not found"):
        octopus.prepare(jj_branches, OctopusPlan("rebase", ["a", "b"], "r", base="nope"))


@pytest.mark.parametrize("operation", ["merge", "rebase"])
@pytest.mark.parametrize("conflicted", [False, True])
def test_bash_summary(jj_branches: Path, operation, conflicted) -> None:
    inputs = ["a", "c"] if conflicted else ["a", "b"]
    plan = OctopusPlan(operation, inputs, "summary", base="main")
    octopus.prepare(jj_branches, plan)
    run("jj", "new", "summary", cwd=jj_branches)
    result = subprocess.run(
        ["bash", "-c", octopus.bash_prompt(plan, {"main", "a", "b", "c"})],
        cwd=jj_branches,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (1 if conflicted else 0), result.stderr
    assert "shared.txt" in result.stdout or "b.txt" in result.stdout


@pytest.mark.parametrize("base", ["@-", "heads(::a & ::b)", "main"])
def test_jj_rebase_onto_revset_base(jj_branches: Path, base: str) -> None:
    """The default base can be a revset (``@-`` without main/master/trunk)."""
    state = octopus.prepare(jj_branches, OctopusPlan("rebase", ["a", "b"], "r", base=base))
    assert state.clean
    stack = jj_out(jj_branches, "log", "--no-graph", "-r", 'main.."r"', "-T",
                   'description.first_line() ++ "\\n"').splitlines()  # fmt: skip
    assert [s for s in stack if s] == ["b: b.txt", "a: shared.txt"]


def test_jj_rebase_base_must_be_one_revision(jj_branches: Path) -> None:
    with pytest.raises(OctopusError, match="single"):
        octopus.prepare(jj_branches, OctopusPlan("rebase", ["a", "b"], "r", base="a | b"))


def test_bash_summary_revset_base() -> None:
    plan = OctopusPlan("rebase", ["a", "b"], "r", base="trunk()")
    assert "--from 'trunk()'" in octopus.bash_prompt(plan, {"a", "b"})
    plan.base = "fix-"
    assert """--from '"fix-"'""" in octopus.bash_prompt(plan, {"a", "b", "fix-"})
