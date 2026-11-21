# Design

## Context

- `config.py` hardcodes `AGENTS`, `FAST_AGENTS`, `EFFORTS` and `RUNTIMES` and
  rejects other values at load time. `screens/agent.py` cycles through `AGENTS`.
  The setup wizard in `add-setup-wizard-skill` filters profile agents by
  `config.AGENTS`, so copilot can be a setup agent but not a profile agent.
- project-sandbox 0.4.1 exposes on the host, without any upstream change:
  - `--help`: argparse `{a,b,…}` choices for `--agent`, `--vcs-access` and
    `--runtime`. `--effort` uses the metavar `LEVEL`, so its values are not
    machine-readable. Fast-mode support appears only in prose.
  - `doctor --json`: `{"ok", "version", "platform", "checks": [{section, name,
    status: ok|warn|fail|info, detail, hint}]}`. The `runtimes` section has one
    entry per runtime plus `auto`. Colima appears only when installed.
  - `project_sandbox.config_agents.available_agents()` (now importable): agents
    with a host configuration directory, plus `bash`. The launcher additionally
    allows any agent with `--no-forward-credentials` or `--agent-proxy`, and
    always allows copilot.
- Measured on Linux: `--help` takes 0.1 s and `doctor --json` 0.6 s. A launcher
  run through `uvx` may download first, and runtime probes can hang on a stuck
  daemon. sbx-tui already probes `--help` for `--mount`
  (`feedback_transport.supported`).
- The existing `doctor.py` takes an injectable `Runner`. Tests use it and
  `tests/fake_project_sandbox.py`.

## Goals / Non-Goals

**Goals:**
- One place that answers: which agents, VCS access modes and runtimes the
  configured launcher supports, and which agents this host can run.
- Never block the UI on a probe. Never block a launch because of a fallback guess.

**Non-Goals:**
- No upstream change to project-sandbox, such as a capabilities JSON command.
- sbx-tui does not query agent gateways, provider APIs or agent CLIs for models.
- No new `vcs_access` or `runtime` profile fields. Profiles keep using
  `extra_args`, and `sandbox.runtime` stays global.
- Effort levels and fast-mode agents stay built-in. They cannot be discovered
  reliably from `--help`.

## Decisions

### A `capabilities` module with a parsed snapshot

`capabilities.discover(command, runner) -> Capabilities` runs `--version`,
`--help` and `doctor --json`, and returns a frozen snapshot:

```text
Capabilities(command, version, discovered_at, source: "probe" | "fallback",
             agents: tuple[str], vcs_modes: tuple[str],
             runtimes: dict[name, RuntimeStatus(status, detail, hint)], auto_selects: str)
```

`--help` is parsed with one regex per option: `--agent \{([^}]*)\}` on the
usage block. Only the first match counts, and choices must be well-formed
names. If `doctor --json` fails, the runtimes come from the `--runtime {…}`
choices with status `unknown`. Doctor `info` and `fail` both count as
unavailable. Doctor `warn` counts as usable, with the detail shown.

Alternatives: importing `project_sandbox.cli.SUPPORTED_AGENTS` or
`build_parser()`. This was rejected as the primary source because
`sandbox.command` may run a different installation (uvx, a source checkout)
from the imported package. The launcher's own output is the only source that
matches what will run. `feedback_transport.supported` moves onto the same
`--help` text, so `--help` is probed only once.

### Host availability: imported rules, version-gated

Agent availability uses `project_sandbox.config_agents.available_agents()`
only when the importable package version equals the launcher's `--version`.
On top of that it applies the launcher's own exceptions: copilot is a token
login, `network.agent_proxy` makes pi, opencode and bash available through the
gateway, and a profile's `--no-forward-credentials` means a login in the
sandbox. On a version mismatch or an import error, availability is `unknown`
and never restricts anything.

Alternative: copy the directory map into sbx-tui. Rejected because it repeats
the drift this change removes. The private-module import is covered by the
`<0.5` dependency bound and a test that calls it.

### Cache in the state directory, keyed by command and version

`state_home()/capabilities.json` (`version: 1`) stores the snapshot. It is
written atomically, like `models.json`. It is read without probing, size-capped,
and validated, and anything invalid is ignored. A snapshot is stale if
`command` differs, it is older than 24 h, or it is missing. Staleness on
version is checked by `--version` (0.1 s) during the background refresh at
startup. The launch form uses the last snapshot, or the fallback lists, as soon
as it opens. When a background refresh finishes, open forms are updated through
an app message.

### Validation moves from load time to doctor and launch

`AgentProfile.validate`, `Defaults`, `Action` and `[models]` check agent names
against `^[a-z][a-z0-9-]{0,31}$`, and `sandbox.runtime` against the same
pattern, instead of checking list membership. `EFFORTS` membership stays.
`AGENTS` and `RUNTIMES` remain as fallback lists, updated to project-sandbox
0.4.1 (adding `copilot`, `colima` and `chroot`).

Before a launch, the check (`jobs.py`, where the launch spec becomes argv)
uses only `source == "probe"` snapshots. This keeps the "fallbacks never block"
rule from the specs.

### Doctor checks are pure functions over config and a snapshot

`check_capabilities(caps)` reports the capabilities, and
`check_agent_selections(cfg, caps, model_store)` reports per-key problems. Both
take a snapshot, so tests do not spawn processes. `run_checks` calls
`capabilities.discover` with the existing runner and probe timeouts (30 s for
`--help`, 60 s for `doctor --json`, matching the current runtime probe), and
writes the cache. `:doctor` runs the same code in its worker thread. The
existing `check_runtime` becomes a fallback for when `doctor --json` is not
available.

`extra_args` are scanned for `--vcs-access`/`--runtime` in both `=` and
separate-argument forms, after `sandbox.extra_args`. The last value wins, as
the launcher's argparse does.

### Setup wizard and skill consume the same snapshot

The wizard asks `capabilities` for profile agents and their sources, and puts
a capability summary in the prompt. That summary replaces the instruction to
read `--help` (kept only when the snapshot is a fallback). The setup agent is
still chosen with `shutil.which`. The gateway-model procedure is written as
prose in the bundled `sbx-tui-config` skill: `GET <agent_proxy>/models`
(`network.agent_proxy` already ends in `/v1`), reading `data[].id`. The key is
read the way the launcher reads it: `pass show agentgateway-api-key`, then
`$<agent_proxy_key_env>` (default `AGENTGATEWAY_API_KEY`). It is passed as an
`Authorization: Bearer` header through a header file or environment expansion,
so it never appears in argv or output. The procedure ends with `sbx-tui config
set --project models.pi '[…]'` and `profiles.NAME.gateway true`.

## Risks / Trade-offs

- [Argparse help format changes upstream] → The parser accepts only `{…}`
  choices, and a parse failure falls back without blocking. A test runs the
  parser against the real `--help` of the locked project-sandbox.
- [Private `config_agents` API changes] → Version gate, import guarded by
  `try`, and `unknown` availability on any error.
- [Relaxed validation turns typos into launch-time errors] → Doctor fails
  for them, and the pre-launch check names the supported agents when
  discovery succeeded.
- [Cached runtime status is out of date (daemon stopped since)] → The
  launch form never restricts by runtime status. Only doctor reports it, and
  doctor always probes fresh.
- [Wizard change lives in another workspace] → Wizard tasks are done in
  whichever change merges second. This change exposes the API first.

## Migration Plan

No data migration. Existing configs keep loading. Configs that newly load
(copilot, colima, chroot) previously failed. Rollback is reverting the change,
and the cache file is then ignored.

## Spike: agent gateway with OpenCode (project-sandbox 0.4.1)

Read from the 0.4.1 source (`cli.py`, `agent_proxy.py`, `config_agents.py`),
then confirmed live on 2026-11-21 against the local agentgateway
(`--agent-proxy http://127.0.0.1:4000/v1`, Podman, `python:3.12-slim`, model
`gpt-5-mini`, a throwaway git project):

| Launch | Result |
| --- | --- |
| `--agent opencode --model gpt-5-mini` | exit 1, "OpenCode proxy models must use --model agent-proxy/<model-id>" |
| `--agent opencode --model agent-proxy/gpt-5-mini` | exit 0, replied as asked |
| `--agent pi --model gpt-5-mini` (no prefix) | exit 0, replied as asked |
| `--agent bash --model gpt-5-mini` | exit 0; `OPENAI_BASE_URL=http://host.docker.internal:4000/v1` and `OPENAI_MODEL` set; `/v1/responses` works |
| `--agent bash` (no model) | exit 1, "--agent-proxy requires the regular --model option" |
| `--agent claude --model claude-haiku-4-5` | exit 1, "--agent-proxy supports only --agent pi, --agent opencode, or --agent bash" |
| `--agent opencode --model agent-proxy/not-a-model` | exit 1, "Selected model 'not-a-model' is unavailable from the agent proxy" |

All rejections happen before an image build or container start.
`GET /v1/models` returned 14 IDs, all plain (for example `gpt-5-mini`,
`claude-sonnet-5-5`). For OpenAI models the gateway rejects
`/v1/chat/completions` (an unknown `reasoning` parameter). That is
gateway-side configuration, which OpenCode and Pi avoid by using the Responses
API, and it does not affect sbx-tui.

- `--agent-proxy` is accepted only with `--agent pi`, `opencode` or `bash`.
  Any other agent exits with "--agent-proxy supports only …".
- `--agent-proxy` requires `--model`. The ID, with any `agent-proxy/` prefix
  removed, must be in the gateway's `GET <agent_proxy>/models` list (`data[].id`).
  Otherwise the launcher exits "Selected model … is unavailable from the agent
  proxy". The launcher queries this list itself, before every launch.
- Model ID forms: **OpenCode** must be `agent-proxy/<id>` ("OpenCode proxy
  models must use --model agent-proxy/<model-id>"). **Pi** accepts both forms
  and adds the prefix itself. **bash** receives the plain ID as `OPENAI_MODEL`.
  This resolves the earlier open question.
- For OpenCode the launcher generates a provider named `agent-proxy`
  (`@ai-sdk/openai-compatible`, with each model routed through `@ai-sdk/openai`,
  which uses the Responses API) and mounts it read-only.
- Key lookup order: `pass show agentgateway-api-key`, then `$<agent-proxy-key-env>`
  (default `AGENTGATEWAY_API_KEY`), then `--agent-proxy-key`. The skill's
  gateway query uses the same order.

Problems in current sbx-tui (`sandbox.py`):

1. `network.agent_proxy` adds `--agent-proxy` to **every** launch, so with a
   gateway configured, claude and codex launches always fail. Today, combining
   agents from different sources is impossible.
2. sbx-tui drops `--model` for bash, but `--agent-proxy` requires it, so a
   bash launch through the gateway always fails.

### Decision: per-launch gateway opt-in

Chosen by the user over two alternatives: using the gateway automatically for
every pi, opencode and bash launch (which leaves no way to run Pi with host
credentials while a gateway is configured), and per-profile `--agent-proxy` in
`extra_args` (which repeats the URL and key settings).

- New `gateway: bool = False` on `AgentProfile`, `Defaults` and `LaunchSpec`,
  plus `LastUsed.gateway_by_agent`, which mirrors `fast_by_agent` and is limited
  to `GATEWAY_AGENTS = ("pi", "opencode", "bash")`. Actions get the gateway
  setting through their profile only. No new action key.
- Load-time validation stays structural: `gateway = true` needs a merged
  `network.agent_proxy`. Whether the agent can use the gateway is a doctor and
  pre-launch check (agent-gateway and agent-profile-diagnostics specs). This
  matches the relaxed agent validation above. The agent set is fixed by the
  launcher, so it is a built-in constant.
- `sandbox.py`: add the `--agent-proxy` flags only when `spec.gateway`. For a
  gateway launch, always pass `--model` (bash included). For opencode, add the
  `agent-proxy/` prefix when missing. Model suggestions keep plain IDs for
  every agent.
- Migration: configs that set `network.agent_proxy` stop using it until a
  profile or `defaults.gateway` opts in. Doctor's "gateway unused" message and
  the changelog explain this. No stored state needs migrating.

