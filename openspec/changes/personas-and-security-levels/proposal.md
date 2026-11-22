# Proposal

## Why

Choosing an agent today means writing provider-specific profiles (agent, model
ID, effort, fast mode) and hand-written `extra_args` for network and VCS access.
Workflows and actions name those profiles, so moving a workflow to another
provider means editing every profile. Security-relevant flags also accumulate
from shared settings and profiles, so one stray `--extra-domain` or
`--no-firewall` quietly widens every launch. A curated model catalogue, a
linear security scale and task personas let users pick *what kind of work*,
*which provider* and *how much isolation*, and let sbx-tui produce the exact
launcher flags.

The change stays inside project-sandbox-tui. Every result compiles to flags the
current project-sandbox launcher already accepts (`--agent`, `--model`,
`--effort`, `--fast`, `--agent-proxy`, `--egress`, `--extra-domain`,
`--no-firewall`, `--vcs-access`, proxy and CA flags).

## What Changes

- **Model catalogue.** sbx-tui ships a curated catalogue in two parts. A
  *vendor tier table* maps each vendor (OpenAI, Anthropic, Google) and tier
  (fast, medium, high, frontier) to one model. *Provider rows* are the single
  route + vendor list users pick from: Codex, Claude Code, Gateway/OpenAI,
  Gateway/Anthropic, Gateway/Gemini, Copilot/OpenAI, Copilot/Anthropic and
  Copilot/Gemini. Each row fixes the agent, the launcher flags and the model-ID
  form, for example dotted Claude IDs for Copilot and `agent-proxy/<id>` for
  OpenCode. A tier that a row cannot serve is shown disabled with a reason and
  is never replaced by another tier. Projects can override tier entries. One
  project setting chooses Pi or OpenCode for the gateway rows. Existing
  `[models]` suggestions remain for custom launches.
- **Security levels.** A linear scale where a higher level means more
  isolation:
  1. `--no-firewall`
  2. `--egress=firewall` with `--extra-domain` for package sources derived from
     `project.build`, plus project-listed domains
  3. agent API only
  4. level 3 plus `--vcs-access=read-only`
  5. level 3 plus `--vcs-access=hidden`

  Levels 3 to 5 use the egress backend from user config: `security.egress =
  sidecar` (default, `--egress=sidecar`) or `firewall` (`--egress=firewall`
  with no extra domains). A sidecar that is selected but missing fails the
  launch. It is never silently replaced.
- **Project default with per-job overrides.** `[security] level` sets the
  project default. The `i` / `r` launch forms, action runs and the workflow
  picker show a level field that can be changed per job. A level below the
  project default is highlighted in red. When a level is in effect, the level is
  the only source of network and VCS flags. Conflicting flags in shared or
  profile `extra_args` are refused before launch and reported by doctor.
  Projects without `[security]` keep today's behaviour.
- **Corporate network settings stay in user config.** `--internet-proxy`,
  `--no-internet-proxy-preflight`, `--ca-cert`, and the new `--build-proxy`,
  `--build-no-proxy` and `--build-ca-cert` settings apply at every level. Level
  2 states that the proxy's policy decides reachability when an internet proxy
  is set, because the launcher rejects `--extra-domain` with a proxy.
- **Personas.** Built-in personas map a task role to tier, effort, fast mode
  and a security level: planner, checker, implementer, debugger, chore-runner
  and reviewer, plus the internal commit-reviewer. `[personas.NAME]` overrides
  or adds personas. Actions gain `persona = "..."`. A persona's level
  pre-fills the step's level field.
- **Launch selection modes.** The `i` / `r` launch forms select by
  **provider + persona**, **profile** or **custom**. Profiles and custom stay
  available for setups the catalogue does not cover.
- **Workflows become provider-independent.** Each `:workflow` step's persona
  comes from its action. The picker asks for a provider and a level per step,
  and the resolved agent, model, effort and flags are snapshotted into the
  step's launch spec as today.
- **Commit reviewer is pinned.** `s` in commit review runs the commit-reviewer
  persona at level 3 or higher on a selected provider, whatever the shared
  network settings are. `commit_review.profile` keeps working.
- **Visible caveats.** Previews and the picker show the level's effective flags
  and egress backend. The picker notes when a Copilot row implies
  `--allow-github`. Doctor lists the hosts allowed by the sidecar's
  `egress.toml`, which is global per runtime and not controlled by sbx-tui.
- **BREAKING (projects that opt in):** with `[security]` set, `extra_args`
  containing `--no-firewall`, `--extra-domain`, `--allow-github`, `--egress` or
  `--vcs-access`, and project-level proxy settings, are rejected instead of
  appended.

## Capabilities

### New Capabilities

- `model-catalogue`: vendor tier table, provider rows (agent, flags, model-ID
  form), per-row tier availability, project overrides and the gateway agent
  setting.
- `security-levels`: the 1–5 scale, its compilation to launcher flags, project
  default and per-job overrides, below-default highlighting, flag ownership and
  conflict refusal, egress backend selection, and user-level corporate network
  settings.
- `agent-personas`: built-in and configured personas, and resolution of
  provider + persona + level into a launch spec.

### Modified Capabilities

- `launch-choices`: launch forms select by provider + persona, profile or
  custom, and show a level field.
- `agent-gateway`: gateway use follows from gateway provider rows, and the
  gateway agent comes from the project setting.
- `sandbox-capabilities`: discover `--egress` choices. Levels whose flags the
  launcher lacks are unavailable with a reason.
- `agent-profile-diagnostics`: doctor checks level conflicts in `extra_args`,
  egress backend availability, sidecar allowlist hosts, unknown personas and
  catalogue entries.
- `setup-agent-selection`: setup proposes a project security level and
  default provider rows, not only raw profiles.
- `agent-actions`: actions gain `persona` and `level`.
- `project-workflows`: the step picker selects provider and level, with persona
  from the action.
- `commit-review-summaries`: the reviewer runs as the commit-reviewer persona on
  `commit_review.provider`, with API-only egress and no repository metadata.

## Impact

- Code: `config.py` (catalogue, `[security]`, `[personas]`, action `persona`,
  network build-proxy fields), new catalogue and level-compilation modules,
  `sandbox.build_argv`, `capabilities.py` (`--egress` discovery, flag-conflict
  checks), `screens/launch.py`, `screens/workflow.py`, `commit_summary.py`,
  `doctor.py`, `setup_wizard.py`, `settings.py`.
- Bundled skills: `sbx-tui-config` and `sbx-tui-safety` describe levels,
  personas and provider rows.
- Docs: `configuration.md`, `models.md`, `usage.md`, `feedback.md`, the example
  `.project-sandbox-tui.toml` (personas on actions; `spec-commits` no longer
  needs its own step because jj keeps distinct changes), and `CHANGELOG.md`.
- `docs/IMPLEMENTATION_SPEC_profiles.md` and `docs/sandbox-options.json` are
  source material only. The policy engine, offline mode, read-only snapshots,
  budgets and the cross-repository work they describe are out of scope.
- State: `LaunchSpec` gains persona, provider and level fields for display.
  Older records stay loadable.
- Assumptions: agentgateway-locally is updated to the same newest Anthropic and
  OpenAI models as the native rows, including `claude-fable-5-2`. The
  launcher's `--help` is the one supplied on 2026-11-22.
