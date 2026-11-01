from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from project_sandbox_tui.jobs import JobRecord
from project_sandbox_tui.opgraph import (
    MAX_UNDO,
    OpGraph,
    State,
    UndoEntry,
    UndoItem,
    draw,
    toposort,
)
from project_sandbox_tui.sandbox import LaunchSpec
from project_sandbox_tui.vcs import Branch, detect
from tests.conftest import add_bookmark, jj


def render(
    keys: list[str], parents: dict[str, list[str]], recency: Mapping[str, float]
) -> list[str]:
    order = toposort(keys, parents, recency)
    lines = draw(order, parents)
    return [f"{lines[k][0]}  {k}".rstrip() for k in order]


def test_draw_forks_and_joins_on_the_node_line() -> None:
    parents = {"a": ["main"], "b": ["main"], "merge": ["a", "b"]}
    recency = {"main": 0, "a": 1, "b": 2, "merge": 3}
    assert render(["main", "a", "b", "merge"], parents, recency) == [
        "○─╮  merge",
        "○ │  a",
        "│ ○  b",
        "○─╯  main",
    ]


def test_draw_crosses_and_reuses_lanes() -> None:
    parents = {"a": ["main"], "b": ["main"], "c": ["a"], "x": [], "y": ["b", "main"]}
    recency = {"main": 0, "a": 5, "b": 2, "c": 6, "x": 4, "y": 3}
    assert render(list(parents) + ["main"], parents, recency) == [
        "○  c",  # chains stay together: c, then its parent a
        "○  a",
        "│ ○  x",  # a root ends its lane...
        "│ ○─╮  y",  # ...which the next bookmark reuses
        "│ ○ │  b",
        "○─┴─╯  main",
    ]
    order = toposort(list(parents) + ["main"], parents, recency)
    assert draw(order, parents)["y"] == ("│ ○─╮", 2)  # the node position, for restyling


def test_draw_keeps_a_new_lane_off_one_that_ends() -> None:
    # m's second parent must not reuse the lane ending at m: ╯ and ╮ in one cell.
    parents = {"m": ["a", "b"], "x": ["m"], "y": ["m"], "a": [], "b": []}
    recency = {"x": 5, "y": 4, "m": 3, "a": 2, "b": 1}
    assert render(["x", "y", "m", "a", "b"], parents, recency) == [
        "○  x",
        "│ ○  y",
        "○─┴─╮  m",
        "○   │  a",
        "    ○  b",
    ]


def test_toposort_ties_keep_order_and_cycles_do_not_hang() -> None:
    assert toposort(["b", "a", "c"], {}, {}) == ["b", "a", "c"]
    assert toposort(["a", "b"], {"a": ["b"], "b": ["a"]}, {"a": 1, "b": 2}) == ["b", "a"]
    assert draw(["b", "a"], {"a": ["b"], "b": ["a"]}) == {"b": ("○", 0), "a": ("○", 0)}


def test_parents_collapse_through_hidden_lineages() -> None:
    state = State()
    state.add_lineage("main", "existing", [])
    state.add_lineage("a", "create", ["main"])
    state.add_lineage("b", "create", ["a"])
    state.add_lineage("m", "merge", ["b", "main"])
    assert state.bookmark_parents(["main", "a", "b", "m"]) == {
        "main": [],
        "a": ["main"],
        "b": ["a"],
        "m": ["b", "main"],
    }
    # Filtered out: b's edges collapse onto a.
    assert state.bookmark_parents(["main", "a", "m"])["m"] == ["a", "main"]
    # Deleted: no longer current, yet its history still connects m to a.
    del state.current["b"]
    assert state.bookmark_parents(["main", "a", "b", "m"])["m"] == ["a", "main"]
    # A new bookmark of the same name starts a lineage of its own.
    state.add_lineage("b", "create", ["main"])
    assert state.bookmark_parents(["main", "a", "b", "m"]) == {
        "main": [],
        "a": ["main"],
        "b": ["main"],
        "m": ["a", "main"],
    }


def test_runs_extend_the_lineage_and_finish_with_their_commit() -> None:
    state = State()
    state.add_lineage("main", "existing", [])
    created = state.add_lineage("topic", "create", ["main"])
    job = JobRecord(
        "topic",
        LaunchSpec("topic", "codex", mode="background", prompt_text="fix it\nthoroughly"),
        123,
        "2026-01-01T00:00:00Z",
        argv=["project-sandbox", "--prompt", "fix it\nthoroughly"],
    )
    run = state.add_run(job)
    assert (run.parents, run.lineage, run.prompt, run.agent) == (
        [created.id],
        created.id,
        "fix it\nthoroughly",
        "codex",
    )
    assert run.command == "project-sandbox --prompt 'fix it\nthoroughly'"
    assert State.from_dict(state.to_dict()).nodes[run.id] == run
    legacy = state.to_dict()
    for node in legacy["nodes"]:
        node.pop("command")
    assert State.from_dict(legacy).nodes[run.id].command == ""
    assert state.head("topic") is run
    assert state.bookmark_parents(["main", "topic"])["topic"] == ["main"]
    assert state.activity()["topic"] == run.at
    branches = [Branch("topic", "abc", "", 0, commit="c1")]
    jobs = {"topic": job}
    assert not state.needs_sync(branches, jobs, listed_at=2e9)  # still running
    job.status, job.ended_at = "completed", "2026-01-01T00:10:00Z"
    # The listing must have started after the run ended to show its final commit.
    assert not state.needs_sync(branches, jobs, listed_at=1767225600.0)
    assert state.needs_sync(branches, jobs, listed_at=2e9)
    state.finish_runs(branches, jobs, listed_at=2e9)
    assert (run.status, run.commit) == ("completed", "c1")
    assert not state.needs_sync(branches, jobs, listed_at=2e9)
    # A later run replacing the record before its outcome was seen.
    second = state.add_run(job)
    jobs["topic"] = JobRecord("topic", job.spec, 124, "2026-01-02T00:00:00Z")
    state.finish_runs(branches, jobs, listed_at=2e9)
    assert second.status == "unknown" and second.commit == ""


def test_op_log_shows_each_run_and_merge() -> None:
    def run(branch: str, prompt: str) -> JobRecord:
        spec = LaunchSpec(branch, "codex", mode="background", prompt_text=prompt)
        return JobRecord(branch, spec, 1, "2026-01-01T00:00:00Z", log_path=f"/logs/{branch}.log")

    state = State()
    steps = [
        state.add_lineage("main", "existing", []),
        state.add_lineage("a", "create", ["main"]),
        state.add_lineage("b", "create", ["main"]),
        state.add_run(run("a", "do a")),
        state.add_run(run("b", "do b\nin detail")),
        state.add_lineage("m", "merge", ["a", "b"]),
        state.add_run(run("m", "resolve")),
    ]
    for i, node in enumerate(steps):
        node.at = float(i)
    steps[3].status = "completed"
    assert steps[3].log == "/logs/a.log"
    del state.current["b"]  # deleted: its runs stay in the log
    log = [f"{line}  {node.bookmark}: {state.describe(node)}" for node, (line, _) in state.op_log()]
    assert log == [
        "○  m: background codex: resolve",
        "○─╮  m: merge of a, b",  # the first input continues the line
        "○ │  a: background codex → completed: do a",
        "○ │  a: created from main",
        "│ ○  b: background codex: do b",
        "│ ○  b: created from main",
        "○─╯  main: existing bookmark",
    ]
    assert state.describe(steps[6], "running") == "background codex → running: resolve"
    assert not state.is_current(steps[4]) and state.is_current(steps[3])


def test_undo_stack_drops_redo_entries_and_old_ones() -> None:
    state = State()
    for i in range(3):
        state.push_undo(UndoEntry("create", [UndoItem(f"b{i}", "", "c")]))
    assert state.undo_entry() is state.undo[2] and state.redo_entry() is None
    state.position = 1
    assert state.redo_entry() is state.undo[1]
    state.push_undo(UndoEntry("delete", [UndoItem("x", "", "c")]))
    assert [e.names() for e in state.undo] == ["b0", "x"] and state.position == 2
    for i in range(MAX_UNDO + 5):
        state.push_undo(UndoEntry("create", [UndoItem(f"n{i}", "", "c")]))
    assert len(state.undo) == MAX_UNDO == state.position
    assert state.undo[-1].names() == f"n{MAX_UNDO + 4}"


def test_storage_round_trip_and_bad_records(tmp_path: Path) -> None:
    path = tmp_path / "state" / "opgraph.json"
    graph = OpGraph(path)
    with graph.transaction() as state:
        node = state.add_lineage("main", "existing", [], "c0")
        state.push_undo(UndoEntry("create", [UndoItem("main", node.id, "c0")]))
    other = OpGraph(path)  # another instance sees the change
    assert other.state.to_dict() == graph.state.to_dict()
    assert other.state.head("main") == node

    with pytest.raises(RuntimeError), graph.transaction() as state:
        state.add_lineage("lost", "create", [])
        raise RuntimeError("jj failed")
    assert "lost" not in OpGraph(path).state.current and "lost" not in graph.state.current

    raw = json.loads(path.read_text())
    raw["nodes"].append({"id": "x"})  # incomplete
    raw["nodes"].append("junk")
    raw["current"]["ghost"] = "missing-node"
    raw["undo"].append({"op": "rename"})
    raw["position"] = 99
    path.write_text(json.dumps(raw))
    loaded = OpGraph(path).state
    assert list(loaded.nodes) == [node.id] and loaded.current == {"main": node.id}
    assert len(loaded.undo) == 1 and loaded.position == 1
    path.write_text("not json")
    assert OpGraph(path).state.nodes == {}


def test_adoption_uses_nearest_bookmarked_ancestors(repo: Path) -> None:
    add_bookmark(repo, "fix/y", "y.txt", "y\n", start="feature/x")
    add_bookmark(repo, "other", "o.txt", "o\n")
    jj(repo, "bookmark", "create", "same", "-r", "feature/x")  # a sibling on one commit
    backend = detect(repo)
    branches = backend.list_branches()
    state = State()
    state.adopt(branches, backend.bookmarked_ancestors, "main")
    parents = state.bookmark_parents([b.name for b in branches if not b.working_copy])
    # The older one wins on a shared commit, ranked by name between equals.
    assert parents == {
        "main": [],
        "feature/x": ["main"],
        "same": ["feature/x"],
        "fix/y": ["feature/x"],
        "other": ["main"],
    }
    assert all(n.kind == "existing" for n in state.nodes.values())
    assert state.activity() == {}  # adopting is no activity

    # Inferring a new bookmark's parents: an existing one on its commit wins.
    jj(repo, "bookmark", "create", "new", "-r", "fix/y")
    branches = backend.list_branches()
    commit = next(b.commit for b in branches if b.name == "new")
    assert state.infer_parents("new", commit, branches, backend.bookmarked_ancestors, "main") == [
        "fix/y"
    ]
    assert state.infer_parents("new", "", branches, backend.bookmarked_ancestors, "main") == []


@pytest.mark.parametrize("field", ["nodes", "undo"])
@pytest.mark.parametrize("value", [1, True, "invalid", {"unexpected": "table"}])
def test_bad_history_containers_do_not_prevent_loading(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "opgraph.json"
    path.write_text(json.dumps({field: value}))
    graph = OpGraph(path)
    assert graph.state == State()
    with graph.transaction() as state:
        state.add_lineage("topic", "create", [])
    assert OpGraph(path).state.head("topic") is not None


@pytest.mark.parametrize("at", [float("nan"), float("inf"), 1e100, 10**400])
def test_unrenderable_history_times_are_ignored(at: float) -> None:
    state = State()
    state.add_lineage("topic", "create", [], at=at)
    assert State.from_dict(state.to_dict()).nodes == {}


@pytest.mark.parametrize(
    "items", [[], [None], [{"bookmark": "topic"}], [{"bookmark": "", "commit": "c"}]]
)
def test_invalid_undo_items_are_ignored(items: list[object]) -> None:
    state = State.from_dict({"undo": [{"op": "delete", "items": items}], "position": 1})
    assert state.undo_entry() is None and state.redo_entry() is None


def test_invalid_undo_entries_do_not_shift_the_undo_position() -> None:
    entry = {"op": "create", "items": [{"bookmark": "topic", "commit": "c"}]}
    state = State.from_dict({"undo": [None, entry], "position": 1})
    assert state.undo_entry() is None
    assert state.redo_entry() == UndoEntry("create", [UndoItem("topic", "", "c")])


def _graph_with_main(tmp_path: Path) -> tuple[OpGraph, Path]:
    path = tmp_path / "state" / "opgraph.json"
    graph = OpGraph(path)
    with graph.transaction() as state:
        state.add_lineage("main", "existing", [], "c0")
    return graph, path


def test_unreadable_history_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, path = _graph_with_main(tmp_path)
    saved = path.read_bytes()
    real = Path.read_bytes

    def flaky(self: Path) -> bytes:
        if self == path:
            raise OSError(24, "Too many open files")
        return real(self)

    with monkeypatch.context() as patch, pytest.raises(OSError):
        patch.setattr(Path, "read_bytes", flaky)
        with graph.transaction() as state:
            state.add_lineage("topic", "create", ["main"])
    assert path.read_bytes() == saved  # not replaced by an empty history plus topic


@pytest.mark.parametrize("content", [b"", b'{"nodes": [', b"[]", b"\xff"])
def test_corrupt_history_is_moved_aside(tmp_path: Path, content: bytes) -> None:
    graph, path = _graph_with_main(tmp_path)
    path.write_bytes(content)
    with graph.transaction() as state:
        state.add_lineage("topic", "create", [])
    [aside] = path.parent.glob("opgraph.json.corrupt-*")
    assert aside.read_bytes() == content  # kept for recovery
    assert set(OpGraph(path).state.current) == {"topic"}


def test_history_from_a_newer_version_is_left_alone(tmp_path: Path) -> None:
    graph, path = _graph_with_main(tmp_path)
    raw = json.loads(path.read_text())
    path.write_text(json.dumps({**raw, "version": 2, "future": True}))
    saved = path.read_bytes()
    with pytest.raises(OSError, match="newer sbx-tui"), graph.transaction() as state:
        state.add_lineage("topic", "create", [])
    assert path.read_bytes() == saved


def test_creation_takes_over_a_lineage_adopted_meanwhile() -> None:
    """jj creates the bookmark first; a sync may adopt it before it is recorded."""
    state = State()
    main = state.add_lineage("main", "existing", [], "c0", at=1.0)
    since = 10.0
    branches = [
        Branch("main", "m", "", 0, commit="c0"),
        Branch("topic", "t", "", 0, commit="c1"),
        Branch("child", "c", "", 0, commit="c2"),
    ]
    ancestors = {"c1": [["main"]], "c2": [["topic"]]}
    state.adopt(branches, lambda commit: ancestors[commit], "main")  # a sync in between
    adopted = state.nodes[state.current["topic"]]
    assert adopted.at >= since
    child = state.nodes[state.current["child"]]
    assert child.parents == [adopted.id]

    node = state.add_creation("topic", "create", ["main"], "c1", since)
    assert node is adopted and node.kind == "create" and node.parents == [main.id]
    assert [n for n in state.nodes.values() if n.bookmark == "topic"] == [node]
    assert state.bookmark_parents(["main", "topic", "child"])["child"] == ["topic"]

    # A lineage from before the creation started is history, not a race: keep it.
    old = state.add_creation("main", "create", [], "c0", since)
    assert old is not main and main.kind == "existing"
    # Nor one with runs on it.
    job = JobRecord("child", LaunchSpec("child", "codex"), 1, "2026-01-01T00:00:00Z")
    state.add_run(job)
    assert state.add_creation("child", "create", ["topic"], "c2", since) is not child


def test_forgotten_job_gets_its_outcome() -> None:
    state = State()
    state.add_lineage("topic", "create", [])
    job = JobRecord("topic", LaunchSpec("topic", "codex", mode="background"), 1, "t1")
    run = state.add_run(job)
    other = JobRecord("other", LaunchSpec("other", "codex"), 2, "t1")
    state.record_outcome(other, "failed")  # another bookmark's job: not this node
    assert run.status == ""
    state.record_outcome(job, "interrupted")
    assert (run.status, run.commit) == ("interrupted", "")
    state.record_outcome(job, "completed", "c9")  # recorded once
    assert run.status == "interrupted"
