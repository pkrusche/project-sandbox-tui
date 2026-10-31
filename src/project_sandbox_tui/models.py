"""Model suggestions for the launch form.

Sources, merged per agent in this order (first match wins in the autocomplete):

1. ``[models]`` from the configuration (explicit pins),
2. built-in Claude Code aliases,
3. Codex's own model list (``$CODEX_HOME/models_cache.json``, written by Codex),
4. the downloaded catalog in the state directory (``models.json``).

The catalog is only ever downloaded on request (``sbx-tui --update-models`` or
the first ``sbx-tui --init``); the TUI itself never goes online. Downloads are
HTTPS-only, size-capped and validated, and only a small filtered subset is
stored, replacing the previous file atomically.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from project_sandbox_tui.config import state_home

DEFAULT_SOURCE = "https://models.dev/api.json"
DEFAULT_LICENSE = "MIT, Copyright (c) 2025 models.dev"
STORE_NAME = "models.json"
STORE_VERSION = 1

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_CACHE_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 20
MAX_AGE_DAYS = 365  # catalog models released longer ago are not suggested
STALE_AFTER_DAYS = 30  # the launch form mentions the age of an older list

PROVIDERS: dict[str, str] = {"claude": "anthropic", "codex": "openai"}  # agent -> catalog key

CLAUDE_ALIASES: tuple[str, ...] = (
    "opus",
    "sonnet",
    "haiku",
    "opusplan",
    "best",
    "fable",
    "opus[1m]",
    "sonnet[1m]",
)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATED_SUFFIX = re.compile(r"^(?P<base>.+)-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ModelsError(Exception):
    """A catalog could not be downloaded, read or understood."""


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str = ""
    released: str = ""  # YYYY-MM-DD
    context: int = 0  # tokens; 0 = unknown
    input: float | None = None  # USD per million tokens (API list price)
    output: float | None = None


@dataclass
class Store:
    source: str = ""
    license: str = ""
    fetched_at: datetime | None = None
    agents: dict[str, list[ModelInfo]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {agent: len(models) for agent, models in self.agents.items()}


def store_path() -> Path:
    return state_home() / STORE_NAME


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


# ---------------------------------------------------------------- validation


def _valid_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _text(value: Any, limit: int = 100) -> str:
    return _CONTROL.sub("", value)[:limit] if isinstance(value, str) else ""


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _context(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 < value < 1_000_000_000 else 0


def _released(value: Any) -> str:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return ""
    try:
        date.fromisoformat(value)
    except ValueError:
        return ""
    return value


def _table(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _sort_and_dedupe(models: list[ModelInfo]) -> list[ModelInfo]:
    """Newest first; drop dated snapshots whose undated id is also listed."""
    ids = {m.id for m in models}
    kept = [
        m
        for m in models
        if not ((match := _DATED_SUFFIX.match(m.id)) and match.group("base") in ids)
    ]
    kept.sort(key=lambda m: m.id)
    kept.sort(key=lambda m: m.released, reverse=True)
    return kept


# ---------------------------------------------------------------- catalog


def _today() -> date:
    return date.today()


def parse_catalog(
    raw: bytes, *, today: date | None = None, max_age_days: int = MAX_AGE_DAYS
) -> dict[str, list[ModelInfo]]:
    """Filter a models.dev-style ``api.json`` down to recent coding-capable models.

    Kept: reasoning + tool calling, text in and out, not deprecated, released
    within ``max_age_days``. Malformed entries are skipped, not trusted.
    """
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelsError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ModelsError("unexpected format: top level is not an object")
    cutoff = ((today or _today()) - timedelta(days=max_age_days)).isoformat()
    result: dict[str, list[ModelInfo]] = {}
    for agent, provider_key in PROVIDERS.items():
        provider = data.get(provider_key)
        entries = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(entries, dict):
            continue
        models: list[ModelInfo] = []
        for key, entry in entries.items():
            model_id = _valid_id(key)
            if model_id is None or not isinstance(entry, dict):
                continue
            modalities = _table(entry.get("modalities"))
            released = _released(entry.get("release_date"))
            if (
                entry.get("reasoning") is not True
                or entry.get("tool_call") is not True
                or "text" not in _strings(modalities.get("input"))
                or "text" not in _strings(modalities.get("output"))
                or entry.get("status") == "deprecated"
                or not released
                or released < cutoff
            ):
                continue
            cost, limit = _table(entry.get("cost")), _table(entry.get("limit"))
            models.append(
                ModelInfo(
                    id=model_id,
                    name=_text(entry.get("name")),
                    released=released,
                    context=_context(limit.get("context")),
                    input=_price(cost.get("input")),
                    output=_price(cost.get("output")),
                )
            )
        result[agent] = _sort_and_dedupe(models)
    if not result:
        raise ModelsError(
            "unexpected format: no " + " or ".join(sorted(PROVIDERS.values())) + " models"
        )
    return result


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ModelsError(f"refusing non-HTTPS redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(source: str, *, timeout: float = TIMEOUT_SECONDS) -> bytes:
    """Read a catalog from an ``https://`` URL or a local file (size-capped).

    HTTPS proxies come from the usual environment variables (``HTTPS_PROXY``).
    """
    if source.startswith("https://"):
        try:
            request = urllib.request.Request(source, headers={"User-Agent": "project-sandbox-tui"})
            opener = urllib.request.build_opener(_HTTPSRedirectHandler())
            with opener.open(request, timeout=timeout) as response:
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
        except (OSError, ValueError) as exc:  # URLError, HTTPError, timeouts, malformed URLs
            raise ModelsError(f"download failed: {exc}") from exc
    elif "://" in source:
        raise ModelsError(f"refusing {source.split('://')[0]}:// — use an https:// URL or a file")
    else:
        path = Path(source).expanduser()
        try:
            with path.open("rb") as fh:
                data = fh.read(MAX_DOWNLOAD_BYTES + 1)
        except OSError as exc:
            raise ModelsError(f"cannot read {path}: {exc}") from exc
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ModelsError(f"catalog larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
    return data


# ---------------------------------------------------------------- store


def _entry(model: ModelInfo) -> dict[str, Any]:
    """JSON form of a model, leaving out unknown fields."""
    return {
        k: v
        for k, v in asdict(model).items()
        if v is not None and v != "" and not (k == "context" and v == 0)
    }


def save(store: Store, path: Path | None = None) -> Path:
    """Write the store atomically: a failed write keeps the previous file."""
    path = path or store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fetched = store.fetched_at or datetime.now(UTC)
    doc = {
        "version": STORE_VERSION,
        "source": store.source,
        "license": store.license,
        "fetched_at": fetched.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agents": {agent: [_entry(m) for m in ms] for agent, ms in store.agents.items()},
    }
    fd, tmp = tempfile.mkstemp(prefix=".models-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def load_store(path: Path | None = None) -> Store | None:
    """The saved catalog, or None if missing or unreadable (never raises)."""
    path = path or store_path()
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        return None
    try:
        fetched = datetime.fromisoformat(str(data.get("fetched_at", "")).replace("Z", "+00:00"))
    except ValueError:
        fetched = None
    agents: dict[str, list[ModelInfo]] = {}
    raw_agents = data.get("agents")
    for agent, entries in raw_agents.items() if isinstance(raw_agents, dict) else ():
        if agent not in PROVIDERS or not isinstance(entries, list):
            continue
        models: list[ModelInfo] = []
        for entry in entries:
            if not isinstance(entry, dict) or (model_id := _valid_id(entry.get("id"))) is None:
                continue
            models.append(
                ModelInfo(
                    id=model_id,
                    name=_text(entry.get("name")),
                    released=_released(entry.get("released")),
                    context=_context(entry.get("context")),
                    input=_price(entry.get("input")),
                    output=_price(entry.get("output")),
                )
            )
        agents[agent] = models
    return Store(
        source=_text(data.get("source"), 300),
        license=_text(data.get("license"), 200),
        fetched_at=fetched,
        agents=agents,
    )


def update(source: str = DEFAULT_SOURCE, path: Path | None = None) -> tuple[Path, Store]:
    """Fetch, filter and save the catalog. Raises ModelsError (file left untouched)."""
    agents = parse_catalog(fetch(source))
    store = Store(
        source=source,
        license=DEFAULT_LICENSE if source == DEFAULT_SOURCE else "",
        fetched_at=datetime.now(UTC),
        agents=agents,
    )
    try:
        return save(store, path), store
    except OSError as exc:
        raise ModelsError(f"cannot write {path or store_path()}: {exc}") from exc


# ---------------------------------------------------------------- codex


def codex_cache_models(home: Path | None = None) -> list[str]:
    """Visible model slugs from Codex's own cache, in Codex's priority order."""
    path = (home or codex_home()) / "models_cache.json"
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("models") if isinstance(data, dict) else None
    ranked: list[tuple[float, str]] = []
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict) or (slug := _valid_id(entry.get("slug"))) is None:
            continue
        visibility = entry.get("visibility")
        if isinstance(visibility, str) and visibility.lower() in ("hide", "hidden"):
            continue
        priority = _price(entry.get("priority"))  # any finite non-negative number
        ranked.append((priority if priority is not None else 10_000.0, slug))
    return [slug for _, slug in sorted(ranked)]


# ---------------------------------------------------------------- launch form


def suggestions(agent: str, configured: list[str] | None, store: Store | None) -> list[str]:
    """Model names to offer for ``agent``, most preferred first, without duplicates."""
    names: list[str] = list(configured or [])
    if agent == "claude":
        names += CLAUDE_ALIASES
    if agent == "codex":
        names += codex_cache_models()
    if store is not None:
        names += [m.id for m in store.agents.get(agent, [])]
    return list(dict.fromkeys(n for n in names if n))


def store_hint(agent: str, store: Store | None, now: datetime | None = None) -> str:
    """One line for the launch form when the catalog is missing or old ("" = fine)."""
    if agent not in PROVIDERS:
        return ""
    if store is None:
        return "more models: sbx-tui --update-models"
    if store.fetched_at is None:
        return ""
    fetched = store.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    age = ((now or datetime.now(UTC)) - fetched).days
    if age <= STALE_AFTER_DAYS:
        return ""
    return f"model list is {age} days old: sbx-tui --update-models"


def describe(store: Store) -> str:
    counts = ", ".join(f"{n} {agent}" for agent, n in store.counts().items())
    when = store.fetched_at.date().isoformat() if store.fetched_at else "unknown date"
    return f"{counts or 'no models'} ({store.source}, {when})"
