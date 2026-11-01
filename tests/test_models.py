from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.request
import urllib.response
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static

from project_sandbox_tui import models as m
from project_sandbox_tui.cli import main
from project_sandbox_tui.config import AGENTS, Config
from project_sandbox_tui.screens.launch import CUSTOM_MODEL, LaunchScreen
from project_sandbox_tui.widgets import CycleField

TODAY = date(2026, 10, 22)


def entry(released: str = "2026-09-22", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Model",
        "reasoning": True,
        "tool_call": True,
        "release_date": released,
        "modalities": {"input": ["text", "image"], "output": ["text"]},
        "cost": {"input": 4, "output": 20, "cache_read": 0.4},
        "limit": {"context": 1_000_000, "output": 128_000},
    }
    base.update(over)
    return base


def catalog() -> dict[str, Any]:
    return {
        "anthropic": {
            "name": "Anthropic",
            "models": {
                "claude-opus-5-5": entry(name="Claude Opus 5.5"),
                "claude-sonnet-5-5": entry("2026-09-30", name="Claude Sonnet 5.5"),
                "claude-haiku-4-5": entry("2025-11-15"),
                "claude-haiku-4-5-20251001": entry("2025-11-15"),  # dated twin
                "claude-old": entry("2024-01-01"),  # too old
                "claude-gone": entry(status="deprecated"),
                "claude-no-tools": entry(tool_call=False),
                "claude-no-thinking": entry(reasoning=False),
                "claude-embed": entry(modalities={"input": ["text"], "output": ["embedding"]}),
                "claude-no-date": entry(released=""),
                "bad id; rm -rf /": entry(),
                "claude-odd-values": entry(
                    name="x\x1b[31m" + "y" * 300,
                    cost={"input": "free", "output": -1},
                    limit={"context": True},
                ),
            },
        },
        "openai": {
            "models": {
                "gpt-5.6": entry("2026-07-09", name="GPT-5.6"),
                "gpt-5.6-2026-07-09": entry("2026-07-09"),  # dated twin
                "gpt-image-2": entry(modalities={"input": ["text"], "output": ["image"]}),
            }
        },
        "google": {"models": {"gemini-x": entry()}},
    }


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "_today", lambda: TODAY)  # catalog dates stay "recent"


def raw(data: Any) -> bytes:
    return json.dumps(data).encode()


# ---------------------------------------------------------------- parsing


def test_parse_catalog_filters_sorts_and_sanitises() -> None:
    agents = m.parse_catalog(raw(catalog()), today=TODAY)
    assert set(agents) == {"claude", "codex"}
    assert [x.id for x in agents["claude"]] == [
        "claude-sonnet-5-5",
        "claude-odd-values",
        "claude-opus-5-5",
        "claude-haiku-4-5",
    ]
    assert [x.id for x in agents["codex"]] == ["gpt-5.6"]
    opus = next(x for x in agents["claude"] if x.id == "claude-opus-5-5")
    assert (opus.name, opus.context, opus.input, opus.output) == (
        "Claude Opus 5.5",
        1_000_000,
        4.0,
        20.0,
    )
    odd = next(x for x in agents["claude"] if x.id == "claude-odd-values")
    assert "\x1b" not in odd.name and len(odd.name) == 100
    assert (odd.input, odd.output, odd.context) == (None, None, 0)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"<html>", "not valid JSON"),
        (b"[1, 2]", "not an object"),
        (raw({"google": {"models": {}}}), "no anthropic or openai"),
    ],
)
def test_parse_catalog_rejects_unexpected_input(payload: bytes, message: str) -> None:
    with pytest.raises(m.ModelsError, match=message):
        m.parse_catalog(payload, today=TODAY)


def test_parse_catalog_with_one_provider_only() -> None:
    agents = m.parse_catalog(raw({"openai": catalog()["openai"]}), today=TODAY)
    assert list(agents) == ["codex"]


def test_catalog_age_boundary_and_standalone_dated_models() -> None:
    cutoff = TODAY - timedelta(days=m.MAX_AGE_DAYS)
    agents = m.parse_catalog(
        raw(
            {
                "openai": {
                    "models": {
                        "at-cutoff": entry(cutoff.isoformat()),
                        "too-old": entry((cutoff - timedelta(days=1)).isoformat()),
                        "standalone-20260922": entry(),  # no undated alias: retain this snapshot
                        "bad-date": entry("2026-02-30"),
                        "not-a-boolean": entry(reasoning=1),
                        "bad-modalities": entry(modalities={"input": "text", "output": ["text"]}),
                    }
                }
            }
        ),
        today=TODAY,
    )
    assert [model.id for model in agents["codex"]] == ["standalone-20260922", "at-cutoff"]


def test_catalog_ignores_numeric_values_too_large_for_a_float() -> None:
    agents = m.parse_catalog(
        raw({"openai": {"models": {"model": entry(cost={"input": 10**400, "output": 2})}}}),
        today=TODAY,
    )
    assert agents["codex"][0].input is None
    assert agents["codex"][0].output == 2


# ---------------------------------------------------------------- fetching


@pytest.mark.parametrize(
    "source", ["http://models.dev/api.json", "file:///etc/passwd", "ftp://x/y"]
)
def test_fetch_refuses_non_https_urls(source: str) -> None:
    with pytest.raises(m.ModelsError, match="use an https:// URL or a file"):
        m.fetch(source)


@pytest.mark.parametrize("remote", [False, True], ids=["local", "https"])
@pytest.mark.parametrize("size", [32, 33], ids=["at-limit", "over-limit"])
def test_fetch_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: bool, size: int
) -> None:
    path = tmp_path / "api.json"
    payload = b"x" * size
    path.write_bytes(payload)
    response = FakeResponse(payload)
    monkeypatch.setattr(m.urllib.request.OpenerDirector, "open", lambda *args, **kwargs: response)
    monkeypatch.setattr(m, "MAX_DOWNLOAD_BYTES", 32)
    source = "https://example.test/api.json" if remote else str(path)
    if size > 32:
        with pytest.raises(m.ModelsError, match="larger than"):
            m.fetch(source)
    else:
        assert m.fetch(source) == payload
    if remote:
        assert response.closed


def test_fetch_missing_file(tmp_path: Path) -> None:
    with pytest.raises(m.ModelsError, match="cannot read"):
        m.fetch(str(tmp_path / "missing.json"))


def test_fetch_reports_a_malformed_https_url() -> None:
    with pytest.raises(m.ModelsError, match="download failed"):
        m.fetch("https://[invalid")


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self


def test_fetch_https_uses_timeout_and_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_open(self, request: Any, timeout: float) -> FakeResponse:
        seen.update(url=request.full_url, timeout=timeout)
        return FakeResponse(b"{}")

    monkeypatch.setattr(m.urllib.request.OpenerDirector, "open", fake_open)
    assert m.fetch("https://example.test/api.json") == b"{}"
    assert seen == {"url": "https://example.test/api.json", "timeout": m.TIMEOUT_SECONDS}

    def failing(self, request: Any, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(m.urllib.request.OpenerDirector, "open", failing)
    with pytest.raises(m.ModelsError, match="download failed: .*403"):
        m.fetch("https://example.test/api.json")


@pytest.mark.parametrize("scheme", ["https", "http"])
def test_catalog_redirects_cannot_downgrade_https(
    monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    requested: list[str] = []

    class Response(urllib.response.addinfourl):
        msg: str

    class Transport(urllib.request.BaseHandler):
        handler_order = 100  # intercept requests before either real network transport

        def https_open(self, request):
            requested.append(request.full_url)
            headers = Message()
            status = 200
            if len(requested) == 1:
                headers["Location"] = f"{scheme}://example.test/redirected.json"
                status = 302
            response = Response(io.BytesIO(b"{}"), headers, request.full_url, status)
            response.msg = "Found" if status == 302 else "OK"
            return response

        http_open = https_open

    build_opener = urllib.request.build_opener

    def with_transport(*handlers):
        return build_opener(*handlers, Transport())

    monkeypatch.setattr(urllib.request, "build_opener", with_transport)
    if scheme == "https":
        assert m.fetch("https://example.test/api.json") == b"{}"
        assert len(requested) == 2
    else:
        with pytest.raises(m.ModelsError, match="redirect"):
            m.fetch("https://example.test/api.json")
        assert requested == ["https://example.test/api.json"]


# ---------------------------------------------------------------- store


def test_update_saves_a_small_store_that_loads_back(tmp_path: Path) -> None:
    source = tmp_path / "api.json"
    source.write_bytes(raw(catalog()))
    path, store = m.update(str(source))
    assert path == m.store_path() and path.parent == m.state_home()
    doc = json.loads(path.read_text())
    assert doc["version"] == 1 and doc["source"] == str(source) and doc["license"] == ""
    assert "cache_read" not in path.read_text()  # only the fields the TUI uses
    loaded = m.load_store()
    assert loaded is not None and loaded.agents == store.agents
    assert loaded.fetched_at is not None and loaded.fetched_at.tzinfo is not None
    assert [p.name for p in path.parent.iterdir()] == ["models.json"]  # no temp files


def test_default_source_records_the_models_dev_license(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(m, "fetch", lambda source: raw(catalog()))
    _, store = m.update()
    assert store.source == m.DEFAULT_SOURCE and "MIT" in store.license


def test_failed_update_keeps_the_previous_store(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_bytes(raw(catalog()))
    path, _ = m.update(str(good))
    before = path.read_bytes()
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    with pytest.raises(m.ModelsError):
        m.update(str(bad))
    assert path.read_bytes() == before


def test_failed_store_replace_keeps_previous_store_and_cleans_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "api.json"
    source.write_bytes(raw(catalog()))
    path, _ = m.update(str(source))
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(m.os, "replace", fail)
    with pytest.raises(m.ModelsError, match="cannot write"):
        m.update(str(source))
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        json.dumps({"version": 99, "agents": {}}),
        json.dumps(["not", "an", "object"]),
    ],
)
def test_unreadable_store_is_ignored(content: str) -> None:
    m.store_path().parent.mkdir(parents=True, exist_ok=True)
    m.store_path().write_text(content)
    assert m.load_store() is None


def test_store_entries_are_validated_on_load() -> None:
    m.store_path().parent.mkdir(parents=True, exist_ok=True)
    m.store_path().write_text(
        json.dumps(
            {
                "version": 1,
                "fetched_at": "garbage",
                "agents": {
                    "claude": [{"id": "ok-model", "input": "1"}, {"id": "$(evil)"}, "x"],
                    "unknown-agent": [{"id": "x"}],
                },
            }
        )
    )
    store = m.load_store()
    assert store is not None and store.fetched_at is None
    assert store.agents == {"claude": [m.ModelInfo(id="ok-model")]}


# ---------------------------------------------------------------- codex cache


def test_codex_cache_models_respects_visibility_and_priority(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    assert m.codex_cache_models(home) == []  # no cache yet
    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.5-codex", "priority": 2},
                    {"slug": "gpt-5.6", "priority": 1},
                    {"slug": "internal", "visibility": "hide", "priority": 0},
                    {"slug": "no-priority"},
                    {"slug": "bad slug"},
                    {"priority": 3},
                ]
            }
        )
    )
    assert m.codex_cache_models(home) == ["gpt-5.6", "gpt-5.5-codex", "no-priority"]
    (home / "models_cache.json").write_text("{oops")
    assert m.codex_cache_models(home) == []


def test_codex_home_follows_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    assert m.codex_home() == tmp_path / "elsewhere"
    monkeypatch.delenv("CODEX_HOME")
    assert m.codex_home() == Path.home() / ".codex"


# ---------------------------------------------------------------- suggestions


def test_suggestions_merge_sources_in_order(tmp_path: Path) -> None:
    store = m.Store(
        agents={
            "claude": [m.ModelInfo("claude-opus-5-5"), m.ModelInfo("opus")],
            "codex": [m.ModelInfo("gpt-5.6"), m.ModelInfo("gpt-5.5")],
        }
    )
    claude = m.suggestions("claude", ["claude-pinned", "opus"], store)
    assert claude[:2] == ["claude-pinned", "opus"]
    assert claude[2 : len(m.CLAUDE_ALIASES) + 1] == list(m.CLAUDE_ALIASES[1:])
    assert claude[-1] == "claude-opus-5-5" and len(claude) == len(set(claude))

    codex_home = m.codex_home()
    codex_home.mkdir(parents=True)
    (codex_home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-5.5"}]}))
    assert m.suggestions("codex", ["mine"], store) == ["mine", "gpt-5.5", "gpt-5.6"]
    assert m.suggestions("pi", ["x"], store) == ["x"]
    assert m.suggestions("codex", None, None) == ["gpt-5.5"]


@pytest.mark.parametrize("naive", [False, True], ids=["aware", "legacy-naive"])
def test_store_hint(naive: bool) -> None:
    now = datetime(2026, 10, 22, tzinfo=UTC)
    fetched = now.replace(tzinfo=None) if naive else now
    fresh = m.Store(fetched_at=fetched - timedelta(days=30))
    stale = m.Store(fetched_at=fetched - timedelta(days=31))
    assert m.store_hint("claude", None, now) == "more models: sbx-tui --update-models"
    assert m.store_hint("codex", fresh, now) == ""
    assert "31 days old" in m.store_hint("claude", stale, now)
    assert m.store_hint("bash", None, now) == ""  # agent without a catalog


# ---------------------------------------------------------------- CLI


def test_cli_update_models_needs_no_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "api.json"
    source.write_bytes(raw(catalog()))
    monkeypatch.chdir(tmp_path)  # not a repository
    assert main(["--update-models", str(source)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("models: ") and "claude" in out and str(m.store_path()) in out
    assert m.load_store() is not None


def test_cli_update_models_defaults_to_models_dev(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    def fake_fetch(source: str) -> bytes:
        seen.append(source)
        return raw(catalog())

    monkeypatch.setattr(m, "fetch", fake_fetch)
    assert main(["--update-models"]) == 0
    assert seen == [m.DEFAULT_SOURCE]


def test_cli_update_models_failure_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--update-models", "http://insecure.test/api.json"]) == 2
    assert "sbx-tui: models: refusing http://" in capsys.readouterr().err
    assert not m.store_path().exists()


def test_cli_init_downloads_models_once_and_tolerates_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def offline(source: str) -> bytes:
        calls.append(source)
        raise m.ModelsError("download failed: offline")

    monkeypatch.setattr(m, "fetch", offline)
    assert main([str(repo), "--init"]) == 0  # config written despite the failure
    captured = capsys.readouterr()
    assert "wrote:" in captured.out and "run `sbx-tui --update-models` later" in captured.err
    assert calls == [m.DEFAULT_SOURCE]

    monkeypatch.setattr(m, "fetch", lambda source: raw(catalog()))
    assert main([str(repo), "--init"]) == 0
    assert "models: " in capsys.readouterr().out and m.load_store() is not None

    monkeypatch.setattr(m, "fetch", offline)
    assert main([str(repo), "--init"]) == 0  # a list exists: no download
    assert calls == [m.DEFAULT_SOURCE]


# ---------------------------------------------------------------- launch form


def run_form(repo: Path, cfg: Config, check: Any, size: tuple[int, int] = (120, 30)) -> None:
    from project_sandbox_tui.app import SandboxTUI
    from project_sandbox_tui.vcs import detect

    cfg.ui.bell = False
    cfg.ui.notify_command = "true"
    app = SandboxTUI(repo, detect(repo), cfg)

    async def scenario() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.press("j", "i")  # feature/x: sessions need a change bookmark
            form = app.screen
            assert isinstance(form, LaunchScreen)
            await check(form, pilot)
            await pilot.press("escape")

    asyncio.run(scenario())


def test_launch_form_suggests_downloaded_models(repo: Path, fake_cfg: Any, tmp_path: Path) -> None:
    source = tmp_path / "api.json"
    source.write_bytes(raw(catalog()))
    m.update(str(source))

    async def check(form: LaunchScreen, pilot: Any) -> None:
        model = form.query_one("#model", CycleField)
        assert model.value == ""
        assert "claude-sonnet-5-5" in model.options
        assert model.options.index("sonnet") < model.options.index("claude-sonnet-5-5")
        model.focus()
        await pilot.press("right")
        assert form.current_spec().model == model.options[1]
        assert model.value in str(form.query_one("#preview", Static).render())
        assert not form.query_one("#model-hint", Static).display  # list is fresh
        form.query_one("#agent", CycleField).value = "codex"
        await pilot.pause()
        assert "gpt-5.6" in model.options
        assert "sonnet" not in model.options
        assert model.value == ""
        assert form.current_spec().model == ""

    run_form(repo, fake_cfg(), check)


def test_launch_form_hints_at_update_models_without_a_list(repo: Path, fake_cfg: Any) -> None:
    async def check(form: LaunchScreen, pilot: Any) -> None:
        hint = form.query_one("#model-hint", Static)
        assert hint.display and "--update-models" in str(hint.render())
        form.query_one("#agent", CycleField).value = "bash"
        await pilot.pause()
        assert not hint.display
        form.query_one("#agent", CycleField).value = "claude"
        await pilot.pause()
        assert hint.display
        model = form.query_one("#model", CycleField)
        assert "opusplan" in model.options

    run_form(repo, fake_cfg(), check)


def test_launch_form_preserves_custom_model(repo: Path, fake_cfg: Any) -> None:
    cfg = fake_cfg()
    cfg.defaults.model = "custom-model"

    async def check(form: LaunchScreen, pilot: Any) -> None:
        model = form.query_one("#model", CycleField)
        assert model.value == "custom-model"
        assert "" in model.options
        form.query_one("#agent", CycleField).value = "bash"
        await pilot.pause()
        assert model.options == [""]
        assert form.current_spec().model == ""
        form.query_one("#agent", CycleField).value = cfg.defaults.agent
        await pilot.pause()
        assert model.value == "custom-model"

    run_form(repo, cfg, check)


@pytest.mark.parametrize("agent", [agent for agent in AGENTS if agent != "bash"])
def test_launch_form_accepts_free_text_model(repo: Path, fake_cfg: Any, agent: str) -> None:
    cfg = fake_cfg()
    cfg.defaults.agent = agent

    async def check(form: LaunchScreen, pilot: Any) -> None:
        model = form.query_one("#model", CycleField)
        row = form.query_one("#custom-model-row")
        name = form.query_one("#custom-model", Input)
        assert not row.display
        model.focus()
        await pilot.press("left", "tab")  # default wraps to custom; Tab edits its name
        assert model.value == CUSTOM_MODEL
        assert row.display and form.focused is name
        await pilot.press(*" provider/my-model-v2 ")
        assert form.current_spec().model == "provider/my-model-v2"
        assert "--model=provider/my-model-v2" in str(form.query_one("#preview", Static).render())

        await pilot.press("shift+tab", "right")  # back to agent default
        assert not row.display
        assert form.current_spec().model == ""
        await pilot.press("left", "tab")
        assert name.value == " provider/my-model-v2 "
        await pilot.press("ctrl+u")  # blank custom name uses the agent default
        assert form.current_spec().model == ""
        assert "--model" not in str(form.query_one("#preview", Static).render())

        name.value = "provider/my-model-v2"
        form.query_one("#agent", CycleField).value = "bash"
        await pilot.pause()
        assert not row.display
        assert model.options == [""]
        assert form.current_spec().model == ""
        assert "--model" not in str(form.query_one("#preview", Static).render())

    run_form(repo, cfg, check)
