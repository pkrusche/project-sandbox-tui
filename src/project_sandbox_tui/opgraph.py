"""Operations history: a coarse map of jj history restricted to our bookmarks.

Every bookmark starts a *lineage*: a first node whose parents are the bookmarks
it started from (one for a new bookmark, several for a merge or rebase). Each
prompt on the bookmark appends a node to its lineage. Bookmarks created outside
sbx-tui are adopted with their nearest bookmarked jj ancestors as parents.

Deleting a bookmark keeps its lineage, so the bookmarks that started from it
still connect through it to its parents, and undo can restore it. ``u`` /
``ctrl+r`` walk a linear undo stack of bookmark creations and deletions.

Stored as JSON in the repository's state directory. Every change re-reads the
file under a lock so TUI instances on the same repository merge their changes.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import time
import uuid
from collections.abc import Callable, Collection, Generator, Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from project_sandbox_tui.config import private_dir
from project_sandbox_tui.jobs import JobRecord, _parse_ts
from project_sandbox_tui.vcs import Branch, VcsError

MAX_UNDO = 100

Ancestors = Callable[[str], list[list[str]]]  # JjBackend.bookmarked_ancestors


@dataclass
class Node:
    id: str
    bookmark: str
    lineage: str  # id of the lineage's first node
    kind: str  # "existing" (adopted), "create", "merge", "rebase" or "run"
    parents: list[str]  # node ids
    at: float
    commit: str = ""  # the bookmark's commit when recorded; runs: once finished
    agent: str = ""  # runs: agent label and mode
    mode: str = ""
    prompt: str = ""  # runs: the prompt's first line
    job: str = ""  # runs: the job's started_at, linking the node to its record
    status: str = ""  # runs: the final job status, once seen

    @classmethod
    def from_dict(cls, raw: Any) -> Node:
        if not isinstance(raw, dict):
            raise ValueError("invalid node")
        values: dict[str, Any] = {}
        for f in fields(cls):
            value = raw.get(f.name)
            if f.name == "parents":
                if not isinstance(value, list):
                    raise ValueError("invalid parents")
                value = [str(p) for p in value]
            elif f.name == "at":
                if type(value) not in (int, float):
                    raise ValueError("invalid time")
                value = float(value)
            elif value is None and f.name in ("id", "bookmark", "lineage", "kind"):
                raise ValueError(f"missing {f.name}")
            else:
                value = str(value or "")
            values[f.name] = value
        return cls(**values)


@dataclass
class UndoItem:
    bookmark: str
    lineage: str
    commit: str  # where the bookmark pointed when it was last removed or created


@dataclass
class UndoEntry:
    op: Literal["create", "delete"]
    items: list[UndoItem]

    @classmethod
    def from_dict(cls, raw: Any) -> UndoEntry:
        if not isinstance(raw, dict) or raw.get("op") not in ("create", "delete"):
            raise ValueError("invalid undo entry")
        items = raw.get("items")
        if not isinstance(items, list):
            raise ValueError("invalid undo items")
        return cls(
            "create" if raw["op"] == "create" else "delete",
            [
                UndoItem(str(i["bookmark"]), str(i.get("lineage") or ""), str(i["commit"]))
                for i in items
                if isinstance(i, dict)
            ],
        )

    def names(self) -> str:
        return ", ".join(item.bookmark for item in self.items)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class State:
    nodes: dict[str, Node] = field(default_factory=dict)  # in recording order
    current: dict[str, str] = field(default_factory=dict)  # bookmark → its lineage
    undo: list[UndoEntry] = field(default_factory=list)
    position: int = 0  # entries before it can be undone, from it on redone

    # ------------------------------------------------------------ storage

    @classmethod
    def from_dict(cls, raw: Any) -> State:
        state = cls()
        if not isinstance(raw, dict):
            return state
        for item in raw.get("nodes") or []:
            with contextlib.suppress(ValueError, TypeError):
                node = Node.from_dict(item)
                state.nodes[node.id] = node
        current = raw.get("current")
        if isinstance(current, dict):
            state.current = {str(k): str(v) for k, v in current.items() if str(v) in state.nodes}
        for item in raw.get("undo") or []:
            with contextlib.suppress(ValueError, TypeError, KeyError):
                state.undo.append(UndoEntry.from_dict(item))
        position = raw.get("position")
        state.position = min(max(position, 0), len(state.undo)) if type(position) is int else 0
        return state

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "nodes": [asdict(n) for n in self.nodes.values()],
            "current": dict(self.current),
            "undo": [asdict(e) for e in self.undo],
            "position": self.position,
        }

    # ------------------------------------------------------------ queries

    def head(self, bookmark: str) -> Node | None:
        """The newest node of the bookmark's lineage."""
        lineage = self.current.get(bookmark)
        if lineage is None:
            return None
        return next(
            (n for n in reversed(self.nodes.values()) if n.lineage == lineage),
            self.nodes.get(lineage),
        )

    def history(self, bookmark: str) -> list[Node]:
        """The bookmark's lineage, newest first."""
        lineage = self.current.get(bookmark)
        return [n for n in reversed(self.nodes.values()) if n.lineage == lineage]

    def activity(self) -> dict[str, float]:
        """When each bookmark's lineage last had a node added; adopting is no activity."""
        latest: dict[str, float] = {}
        for node in self.nodes.values():
            if node.kind != "existing" and self.current.get(node.bookmark) == node.lineage:
                latest[node.bookmark] = max(latest.get(node.bookmark, 0.0), node.at)
        return latest

    def bookmark_parents(self, visible: Collection[str]) -> dict[str, list[str]]:
        """Parents of each visible bookmark among the visible ones.

        Edges through hidden lineages (deleted or filtered out) collapse onto
        their nearest visible ancestors, like jj's elided revisions.
        """
        by_lineage: dict[str, list[Node]] = {}
        for node in self.nodes.values():
            by_lineage.setdefault(node.lineage, []).append(node)
        shown = {self.current[name]: name for name in visible if name in self.current}

        def direct(lineage: str) -> list[str]:
            found: list[str] = []
            for node in by_lineage.get(lineage, []):
                for parent in node.parents:
                    other = self.nodes[parent].lineage if parent in self.nodes else ""
                    if other and other != lineage and other not in found:
                        found.append(other)
            return found

        memo: dict[str, list[str]] = {}

        def collapse(lineage: str, seen: frozenset[str]) -> list[str]:
            if lineage in memo:
                return memo[lineage]
            found: list[str] = []
            for parent in direct(lineage):
                if parent in seen:
                    continue  # a cycle; recorded history never has one
                reached = [parent] if parent in shown else collapse(parent, seen | {parent})
                found += [p for p in reached if p not in found]
            memo[lineage] = found
            return found

        return {
            name: [shown[p] for p in collapse(lineage, frozenset({lineage})) if p != lineage]
            for lineage, name in shown.items()
        }

    def undo_entry(self) -> UndoEntry | None:
        return self.undo[self.position - 1] if self.position > 0 else None

    def redo_entry(self) -> UndoEntry | None:
        return self.undo[self.position] if self.position < len(self.undo) else None

    # ------------------------------------------------------------ changes

    def add_lineage(
        self,
        bookmark: str,
        kind: str,
        parents: Iterable[str],
        commit: str = "",
        at: float | None = None,
    ) -> Node:
        """Start a new lineage for ``bookmark`` from the heads of ``parents``."""
        heads = [h.id for p in parents if p != bookmark and (h := self.head(p)) is not None]
        node_id = _new_id()
        node = Node(
            node_id,
            bookmark,
            node_id,
            kind,
            list(dict.fromkeys(heads)),
            time.time() if at is None else at,
            commit,
        )
        self.nodes[node_id] = node
        self.current[bookmark] = node_id
        return node

    def add_run(self, job: JobRecord, prompt_text: str | None = None) -> Node:
        """Append a prompt's node to its bookmark's lineage."""
        head = self.head(job.branch) or self.add_lineage(job.branch, "existing", [])
        prompt = (job.spec.prompt_text if prompt_text is None else prompt_text).strip().splitlines()
        node = Node(
            _new_id(),
            job.branch,
            head.lineage,
            "run",
            [head.id],
            time.time(),
            agent=job.spec.label(),
            mode=job.mode,
            prompt=prompt[0] if prompt else "",
            job=job.started_at,
        )
        self.nodes[node.id] = node
        return node

    def push_undo(self, entry: UndoEntry) -> None:
        """Record a change, dropping what could have been redone."""
        del self.undo[self.position :]
        self.undo.append(entry)
        del self.undo[:-MAX_UNDO]
        self.position = len(self.undo)

    def infer_parents(
        self,
        bookmark: str,
        commit: str,
        branches: Iterable[Branch],
        ancestors: Ancestors,
        main: str,
    ) -> list[str]:
        """Bookmarks ``bookmark`` presumably started from: an older bookmark on
        the same commit, else those on its nearest bookmarked ancestors."""
        if not commit:
            return []

        def rank(name: str) -> tuple[bool, float, str]:
            lineage = self.nodes.get(self.current.get(name, ""))
            return (name != main, lineage.at if lineage else float("inf"), name)

        own = rank(bookmark)
        same = [
            b.name
            for b in branches
            if b.commit == commit and not b.working_copy and rank(b.name) < own
        ]
        if same:
            return [min(same, key=rank)]
        try:
            heads = ancestors(commit)
        except VcsError:
            return []
        return [min(names, key=rank) for names in heads if names]

    def adopt(self, branches: list[Branch], ancestors: Ancestors, main: str) -> None:
        """Give bookmarks created outside sbx-tui a lineage."""
        unknown = [b for b in branches if not b.working_copy and b.name not in self.current]
        now = time.time()
        for branch in sorted(unknown, key=lambda b: (b.name != main, b.timestamp, b.name)):
            self.add_lineage(branch.name, "existing", [], branch.commit, at=now)
        for branch in unknown:
            root = self.nodes[self.current[branch.name]]
            parents = self.infer_parents(branch.name, branch.commit, branches, ancestors, main)
            root.parents = [h.id for p in parents if (h := self.head(p)) is not None]

    def _run_outcome(
        self, node: Node, jobs: Mapping[str, JobRecord], listed_at: float
    ) -> str | None:
        """The final status for a pending run node, once its job has finished."""
        if node.kind != "run" or node.status:
            return None
        job = jobs.get(node.bookmark)
        if job is None:
            return None
        if job.started_at != node.job:
            # Replaced by a newer run before this instance saw the outcome.
            return "unknown" if _parse_ts(job.started_at) > _parse_ts(node.job) else None
        # The commit comes from a listing that must postdate the run's end.
        if job.running or not job.ended_at or _parse_ts(job.ended_at) > listed_at:
            return None
        return job.status

    def finish_runs(
        self, branches: list[Branch], jobs: Mapping[str, JobRecord], listed_at: float
    ) -> None:
        commits = {b.name: b.commit for b in branches}
        for node in self.nodes.values():
            status = self._run_outcome(node, jobs, listed_at)
            if status is None:
                continue
            node.status = status
            if status != "unknown" and self.current.get(node.bookmark) == node.lineage:
                node.commit = commits.get(node.bookmark, "")

    def needs_sync(
        self, branches: list[Branch], jobs: Mapping[str, JobRecord], listed_at: float
    ) -> bool:
        if any(not b.working_copy and b.name not in self.current for b in branches):
            return True
        return any(self._run_outcome(n, jobs, listed_at) for n in self.nodes.values())


class OpGraph:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state = State.from_dict(self._read_raw())

    def _read_raw(self) -> Any:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def reload(self) -> None:
        """Pick up changes made by other instances; readers keep the old state."""
        self.state = State.from_dict(self._read_raw())

    @contextlib.contextmanager
    def transaction(self) -> Generator[State, None, None]:
        """Change a fresh copy of the stored state; saved unless the block raises.

        Readers of ``self.state`` (the UI thread) see the old or the new state,
        never one in between.
        """
        private_dir(self.path.parent)
        with self.path.with_name(self.path.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                raw = self._read_raw()
                state = State.from_dict(raw)
                yield state
                new = state.to_dict()
                if new != raw:
                    tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
                    try:
                        tmp.write_text(json.dumps(new, indent=1), encoding="utf-8")
                        tmp.replace(self.path)
                    finally:
                        with contextlib.suppress(OSError):
                            tmp.unlink(missing_ok=True)
                self.state = state
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def sync(
        self,
        branches: list[Branch],
        jobs: Mapping[str, JobRecord],
        listed_at: float,
        ancestors: Ancestors,
        main: str,
    ) -> None:
        """Adopt new bookmarks and record finished runs, as of a bookmark listing
        that started at ``listed_at``."""
        self.reload()
        if not self.state.needs_sync(branches, jobs, listed_at):
            return
        with self.transaction() as state:
            state.adopt(branches, ancestors, main)
            state.finish_runs(branches, jobs, listed_at)


# ---------------------------------------------------------------- layout


def toposort(
    keys: list[str], parents: Mapping[str, list[str]], recency: Mapping[str, float]
) -> list[str]:
    """Children before parents, most recent first, keeping chains together
    (like ``git log --topo-order``). Ties keep the order of ``keys``."""
    index = {k: i for i, k in enumerate(keys)}
    pending = dict.fromkeys(keys, 0)  # children not yet emitted
    for key in keys:
        for parent in parents.get(key, []):
            if parent in pending:
                pending[parent] += 1

    def priority(key: str) -> tuple[float, int]:
        return recency.get(key, 0.0), -index[key]

    ready = sorted((k for k in keys if not pending[k]), key=priority)  # a stack: top is next
    order: list[str] = []
    done: set[str] = set()
    while len(order) < len(keys):
        if not ready:  # only a cycle is left: break it at its most recent bookmark
            ready.append(max((k for k in keys if k not in done), key=priority))
        key = ready.pop()
        if key in done:
            continue
        done.add(key)
        order.append(key)
        freed = []
        for parent in parents.get(key, []):
            if parent in pending and parent not in done:
                pending[parent] -= 1
                if not pending[parent]:
                    freed.append(parent)
        ready.extend(reversed(freed))  # the first parent continues the chain
    return order


NODE = "○"


def draw(order: list[str], parents: Mapping[str, list[str]]) -> dict[str, tuple[str, int]]:
    """One line per key, jj-style: ``(line, node_index)``.

    Lines come down from children to parents. ``○─╯`` joins lines that end at
    the node; ``○─╮`` starts one for a further parent. The node is drawn as
    ``NODE`` at ``node_index`` so callers can restyle it.
    """
    index = {k: i for i, k in enumerate(order)}
    lanes: list[str | None] = []  # the key each line is heading for
    out: dict[str, tuple[str, int]] = {}
    for key in order:
        waiting = [i for i, target in enumerate(lanes) if target == key]
        if waiting:
            col = waiting[0]
        else:
            col = next((i for i, target in enumerate(lanes) if target is None), len(lanes))
            if col == len(lanes):
                lanes.append(None)
        before = list(lanes)
        joins = set(waiting[1:])
        for i in waiting:
            lanes[i] = None
        below = [p for p in parents.get(key, []) if index.get(p, -1) > index[key]]
        forks: set[int] = set()
        if below:
            lanes[col] = below[0]
            for parent in below[1:]:
                # Not a lane ending here: ╯ and ╮ in one cell would be ambiguous.
                slot = next(
                    (i for i in range(col + 1, len(lanes)) if before[i] is None and i not in forks),
                    None,
                )
                if slot is None:
                    lanes.append(parent)
                    before.append(None)
                    slot = len(lanes) - 1
                else:
                    lanes[slot] = parent
                forks.add(slot)
        right = max([col, *joins, *forks])
        cells: list[str] = []
        for i in range(len(before)):
            through = before[i] is not None and before[i] != key
            if i == col:
                cell = NODE
            elif i in joins:
                cell = "╯" if i == right else "┴"
            elif i in forks:
                cell = "╮" if i == right else "┬"
            elif col < i < right:
                cell = "┼" if through else "─"
            else:
                cell = "│" if through else " "
            cells.append(cell + ("─" if col <= i < right else " "))
        out[key] = ("".join(cells).rstrip(), 2 * col)
        while lanes and lanes[-1] is None:
            lanes.pop()
    return out
