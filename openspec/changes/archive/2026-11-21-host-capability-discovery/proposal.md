# Proposal

## Why

sbx-tui hardcodes the agents and runtimes that project-sandbox supports, and the
lists have drifted: project-sandbox 0.4.1 also supports the `copilot` agent and
the `colima` and `chroot` runtimes, so a profile with `agent = "copilot"` is a
config error today. Profiles can also name agents whose host credentials are
missing or runtimes that are not running. These only fail at launch, after an
image build. The current project-sandbox already reports what it supports
(`--help`, `doctor --json`), so sbx-tui can discover this on the host without an
upstream change.

## What Changes

- Discover launcher capabilities from the configured `sandbox.command`:
  - supported agents from the `--agent {…}` choices in `--help`;
  - VCS access modes from the `--vcs-access {…}` choices in `--help`, with no
    modes when the flag is absent;
  - isolation runtimes and whether each is usable from `doctor --json`, falling
    back to the `--runtime {…}` choices with unknown status.
  Results are cached per command and launcher version and refreshed by `doctor`.
  Without a usable probe, the built-in lists apply.
- Determine which supported agents can run on this host: forwarded host
  credentials, the configured agent gateway (`network.agent_proxy`), or a login
  inside the sandbox (`--no-forward-credentials`).
- **BREAKING (config validation):** loading the config no longer rejects agent
  names or `sandbox.runtime` values missing from the built-in lists. Names are
  checked for syntax only. Support is checked by `doctor` and before a launch,
  so newer launcher agents and runtimes work without an sbx-tui release.
- Restrict the launch form and profile editor to supported agents. Show agents
  that are unavailable on this host, with the reason, instead of hiding them.
  Model suggestions keep their current sources: `[models]`, Claude aliases,
  Codex's cache, and the models.dev catalog.
- `doctor` (CLI and `:doctor`) checks every agent selection: `defaults`, each
  `[profiles.NAME]`, actions with their own agent fields, and the commit-review
  profile. It fails for settings the launcher would reject: an unsupported or
  unavailable agent, an unsupported `--vcs-access` mode or `--runtime`, or an
  unusable runtime. It warns for settings that are probably wrong: a Claude or
  Codex model not in the model list, or fast mode on an agent without it.
- Separate the two agent choices in setup. The **setup agent** runs the
  interview on the host and is found on `PATH`. **Profile agents** run in the
  sandbox and come from launcher discovery and host availability. Profiles may
  combine agents from different sources, for example Claude with forwarded
  credentials and Pi through the gateway.
- **BREAKING (gateway):** the agent gateway becomes a per-launch opt-in.
  `network.agent_proxy` only defines the gateway. A profile, the launch form, or
  `defaults.gateway` sets `gateway = true` for pi, opencode or bash. Today
  sbx-tui adds `--agent-proxy` to every launch, so claude and codex launches
  fail whenever a gateway is configured, and bash fails because sbx-tui drops
  `--model`. Gateway launches always pass `--model`, and OpenCode models get the
  `agent-proxy/` prefix the launcher requires.
- When the user wants the agent gateway, the `sbx-tui-config` skill tells the
  setup agent to list the gateway's models. The setup agent then records them
  as `[models]` suggestions and sets `gateway = true` on the profiles that use
  it. sbx-tui does not query the gateway itself.

## Capabilities

### New Capabilities

- `sandbox-capabilities`: discovering the configured launcher's agents, VCS
  access modes and isolation runtimes, host agent availability, caching and
  fallbacks, and the relaxed config validation that depends on it.
- `launch-choices`: which agents, models and options the launch form and profile
  editor offer, and how unavailable choices are shown.
- `agent-profile-diagnostics`: `doctor` checks of configured agent selections
  against discovered capabilities and model lists.
- `agent-gateway`: per-launch gateway opt-in, its validation, launcher
  arguments for gateway launches, and the launch form's gateway field.
- `setup-agent-selection`: the distinction between the host setup agent and
  sandbox profile agents, the setup prompt's capability summary, and gateway
  model instructions for the configuration agent.

### Modified Capabilities

None. The project has no archived specs. The in-flight `action-profiles` change
adds an unknown-profile warning to `doctor`. This change adds separate checks
alongside it.

## Impact

- New `src/project_sandbox_tui/capabilities.py`: probe, parse, cache and query.
- `config.py`: replace membership validation of `agent` and `sandbox.runtime`
  with syntax checks; keep `AGENTS`/`RUNTIMES` as fallback lists, adding
  `copilot`, `colima` and `chroot`.
- `screens/agent.py` (launch form and profile editor), `app.py` (cache refresh
  at startup and in `:doctor`), `jobs.py`/`sandbox.py` (pre-launch check).
- Gateway opt-in: `gateway` on `AgentProfile`, `Defaults`, `LaunchSpec` (new
  field with a default, so stored job records still load) and
  `LastUsed.gateway_by_agent`. `sandbox.py` argv for `--agent-proxy` and
  `--model`.
- `doctor.py`: capability and agent-profile checks.
- `settings.py`: `[models]` keys accept any well-formed agent name.
- Bundled `sbx-tui-config` skill: capability discovery and agent gateway models.
- Setup wizard (`setup_wizard.py`, in the unmerged `add-setup-wizard-skill`
  change): profile agents from discovery instead of `config.AGENTS`.
- Docs: `docs/configuration.md`, `docs/models.md`, `docs/usage.md` (doctor),
  `CHANGELOG.md`. Tests for the parser, cache, config, doctor and launch form.
- No new dependencies. This uses the `project-sandbox` package dependency
  already added.
