# Design

## Context

See proposal.md for motivation. Requirements are in `specs/model-catalogue`,
`specs/security-levels`, `specs/agent-personas` and the modified capability
deltas.

Current state:

- `sandbox.build_argv` builds every launcher command from `Config` and a
  `LaunchSpec`. It emits the `[network]` flags (`--internet-proxy`,
  `--agent-proxy`, `--ca-cert`, `--extra-domain`, `--allow-github`,
  `--no-firewall`) for every launch. It then appends `sandbox.extra_args` and
  `spec.extra_args` verbatim.
- `config.StepChoice` (`profile` / `action` / `custom`), `initial_choice` and
  `resolve_action_settings` are the single resolution path for `:NAME` action
  runs, workflow steps and the workflow dry-run preview.
  `screens/launch.LaunchScreen` resolves its own profile and custom settings,
  using `remembered_profile`.
- `capabilities.Capabilities` holds the discovered agents, VCS modes and
  runtimes. It is cached with `CACHE_VERSION = 1`. `launch_problems` refuses
  launches only on discovered capabilities. `flag_value` already reads flags
  from argument lists.
- `commit_summary.resolve_launch` builds an isolated config: a fixed image,
  resource-only profile arguments, and an empty non-repository workspace with
  patches mounted read-only. It currently passes `cfg.network` through
  unchanged.
- Config is merged from the global and project files in `config._load`. The
  raw project data is available there, so key-origin checks are possible
  without a new parser.
- The launcher `--help` (supplied 2026-11-22) offers
  `--egress {firewall,sidecar}`, `--vcs-access {full,read-only,hidden}`,
  `--build-proxy`, `--build-no-proxy` and `--build-ca-cert`. It rejects
  `--extra-domain` with `--internet-proxy` or sidecar egress, and implies
  `--allow-github` for `--agent copilot`.

## Goals / Non-Goals

**Goals:**

- One pure resolver from "selection + level" to a fully resolved `LaunchSpec`,
  shared by the launch forms, action runs, the workflow picker, dry runs and
  commit review.
- One function that turns a level into network and VCS flags, used by
  `build_argv`. Previews, doctor and conflict checks read the same output.
- Projects without `[security]` produce identical launcher commands to today.
  The one exception is commit review, whose isolation is deliberately pinned.

**Non-Goals:**

- Querying the agent gateway, Copilot or provider APIs for live model lists.
  The catalogue is static, with config overrides.
- Editing the sidecar's `egress.toml`, or managing the sidecar.
- Per-model effort validation beyond the row rules. The launcher and agent CLIs
  report invalid efforts.
- A picker for plain `:NAME` runs. A below-default level gets a confirmation
  instead.
- The policy engine, offline mode, read-only snapshots, budgets and failover
  from `docs/IMPLEMENTATION_SPEC_profiles.md`.

## Decisions

### Catalogue as a typed Python module

A new `catalogue.py` holds frozen dataclasses:

- `Vendor` tier maps for `openai`, `anthropic` and `google`.
- `ProviderRow`, with fields `id`, `label`, `vendor`, `agent` (or `"gateway"`
  for gateway rows), `gateway: bool`, `effort: bool` (False for
  `gateway-gemini`), `fast: bool` (True for `codex` and `claude-code`), and
  `models` (a per-row tier map that overrides the vendor map, for example the
  Copilot dotted IDs and the empty Copilot tiers).
- `model_for(cfg, row, tier) -> str | None`, which applies built-in rows, then
  `[catalogue.models.<vendor>]`, then `[catalogue.models.<row>]`. An empty
  string means the tier is unavailable.

Copilot IDs are listed explicitly instead of derived by replacing `-` with `.`.
The source spec warns against string substitution, and explicit entries keep
the "no automatic mapping" tests simple.

*Alternative:* shipping `sandbox-options.json` or a TOML resource. Rejected: it
needs packaging and schema code, and the JSON has known inconsistencies. Models
change with releases anyway, and config overrides cover local drift.

### Personas in the same module

`Persona(tier, effort, fast, level, internal=False)`. Built-ins come from the
agent-personas spec, and `commit-reviewer` is `internal=True`.
`[personas.NAME]` merges field by field over built-ins. New personas must set
`tier`. Persona names follow `valid_name`. An unknown `actions.X.persona` is a
config error at load, unlike profiles, because personas cannot be deleted at
runtime from the TUI.

### Selection and resolution

`StepChoice` gains a `persona` kind (`StepChoice("persona", persona="reviewer")`).
A new `Selection(choice, provider, level)` adds the per-job provider (persona
kind only) and level (`None` = use the starting level).

`resolve_selection(cfg, last, selection, action=None) -> ResolvedSetup` extends
`resolve_action_settings`:

| Choice | agent / model / effort / fast / gateway | extra_args | starting level |
|---|---|---|---|
| persona + provider | row agent (`catalogue.gateway_agent` for gateway rows), `model_for(row, tier)`, persona effort unless `row.effort` is False, persona fast only if `row.fast` | `[]` | `action.level`, else persona level |
| profile | as today | profile's | `action.level`, else project default |
| action settings | as today | `[]` | `action.level`, else project default |
| custom | as today | `last.extra_args` | project default |

A missing tier returns a `ResolvedSetup` with `problem` set. Callers show it
and refuse to launch. There is no fallback.

`initial_choice` order becomes persona, then profile, then action settings,
then the remembered selection. The provider for persona steps starts at
`last.provider` if available, else `catalogue.default_provider`, else the first
available row.

*Alternative:* turning personas into synthetic profiles. Rejected: profiles
carry `extra_args`, which persona launches must not add, and synthetic profiles
would show up in `:config-profiles`.

### Level compilation owns network and VCS flags

A new `security.py` module provides:

- `effective_level(cfg, spec) -> int | None`: `spec.level`, else
  `cfg.security.level`, else `None` (legacy).
- `level_flags(cfg, level, agent) -> list[str]`, following the security-levels
  spec. It reads `project.build` for the level-2 sources and
  `network.extra_domains` / `allow_github` at level 2 only. It drops
  `--extra-domain` when `network.internet_proxy` is set.
- `MANAGED_FLAGS`: `--no-firewall`, `--egress`, `--extra-domain`,
  `--allow-github`, `--vcs-access`, `--pi-ollama`, `--internet-proxy`,
  `--no-internet-proxy-preflight`, `--ca-cert`, `--build-proxy`,
  `--build-no-proxy` and `--build-ca-cert`. `managed_conflicts(args)` finds
  them in either `--flag=value` or `--flag value` form.

In `build_argv`, the network block splits in two. Corporate settings
(`--internet-proxy`, `--ca-cert`, the new `--build-*` fields, and
`--agent-proxy` for gateway launches) are always emitted. When
`effective_level` is not `None`, the legacy `extra_domains` / `allow_github` /
`no_firewall` emission is replaced by `level_flags`. When it is `None`, the
legacy code runs unchanged. `LaunchSpec` gains `persona`, `provider` and
`level`. `from_dict` defaults them, so queued records from older versions load
and pick up the project default at dispatch.

Conflict checks run in a new non-discovery `security_problems(cfg, spec)`.
`launch_problems` callers already gather problems before a launch, so it runs
in the same place. It applies to `sandbox.extra_args` plus `spec.extra_args`.

*Alternative:* letting level flags win by ordering. Rejected: the launcher
accumulates repeatable flags and has no negation for `--no-firewall`.

### Global-only corporate settings

`_load` checks the raw project data. When the effective `security.level` is
set, any of `network.internet_proxy`, `network.ca_certs`, `network.build_proxy`,
`network.build_no_proxy`, `network.build_ca_certs` or `security.egress` in the
project file raises `ConfigError` with a hint to set it globally.
`security.egress` is global-only in every project, since it describes the
machine's runtime. New `Network` fields are `build_proxy: str`,
`build_no_proxy: list[str]` and `build_ca_certs: list[str]`, and `build_argv`
emits them for every launch.

### Discovery of egress modes and level availability

`Capabilities.egress_modes` comes from `parse_choices(help, "--egress")`, and
`CACHE_VERSION` becomes 2, so older caches refresh.
`level_problems(caps, cfg, level)` returns reasons only for discovered
capabilities:

- no `--egress` blocks levels 2 to 5;
- a backend missing from the modes blocks levels 3 to 5;
- no `--vcs-access` blocks levels 4 and 5.

Forms use it to mark choices, and the pre-launch check refuses them.

### Launch form

The existing `#profile` `CycleField` becomes the setup selector:
`["persona", *profiles, "custom"]`, with the label **setup**. Persona mode
mounts provider and persona `CycleField`s. Unavailable rows are labelled with
their reason, following the `AgentControls.agent_status` pattern. A level
`CycleField` (1–5, with labels from the security-levels spec) appears in all
modes when `effective_level` would not be `None`. Below the project default,
it gets a `below-default` CSS class rendered red. The hint line shows
`ResolvedSetup.label()` plus the level's flags, and the Copilot GitHub caveat.

`LastUsed` gains `selection: "persona" | None`, `provider` and `persona`.
`remembered_selection` replaces `remembered_profile` for the forms.
`remembered_profile` stays for actions without a persona. Levels are never
remembered.

### Workflow picker and action runs

`WorkflowProfilesScreen` rows gain a provider field (persona choices only) and
a level field. `enter` refuses while any row has a `problem`. The dry-run
preview adds persona, provider, level and flags.

`_run_action` resolves once. When the resolved level is below the project
default, it pushes the existing confirmation dialog with both levels before
launching or queueing.

### Commit review

`resolve_launch` builds the commit-reviewer persona on `commit_review.provider`
(default `codex`), unless `commit_review.profile` is set. The spec it builds
always has `level = 3`, so `level_flags` emits only the egress backend flag.
The workspace is an empty directory with no `.git` or `.jj`, so repository
metadata is absent by construction, and no `--vcs-access` is passed. That
avoids depending on launcher behaviour for non-repository workspaces. The
existing `_resource_args` check already rejects widening profile arguments.

### Doctor and setup

`check_config` gains three checks:

- level conflicts per config key, from `managed_conflicts`;
- the egress backend: support, whether `egress.toml` exists, and a best-effort
  host list;
- the provider list, with failures for an unavailable `default_provider` or
  `commit_review.provider`.

`setup_summary` appends providers with availability, supported levels and
personas. The `sbx-tui-config` and `sbx-tui-safety` skills get sections on
levels, personas and providers.

## Risks / Trade-offs

- **Commit review now needs the sidecar by default.** Users without one get a
  failing summary after upgrading. → Doctor reports it with both hints
  (`project-sandbox-egress` or `security.egress = "firewall"`), and the
  CHANGELOG calls it out as breaking.
- **Level 3 can be as open as egress.toml allows.** The sidecar policy is
  global per runtime. → Doctor lists the allowed hosts and says the policy is
  shared. The docs describe level 3 as "launcher minimum", not as a guarantee.
- **Levels 4 and 5 keep source files writable.** → The level hints say "VCS
  read-only" / "no VCS", not "read-only workspace". The docs point Pi reviewers
  to `--pi-tools` through a profile, for which `--pi-tools` is not a managed
  flag.
- **Catalogue model IDs go stale.** → `[catalogue.models.*]` overrides, and the
  launcher's gateway `/models` check fails early for wrong gateway IDs.
- **`--agent-proxy` together with `--egress=sidecar` is untested here.** → The
  real-container smoke test gains a gateway-at-level-3 case. If the launcher
  rejects the pair, `level_problems` will mark gateway rows unavailable at
  levels 3 to 5.
- **A red level is only a warning.** → Action runs below the default need a
  confirmation, and all previews show the level.
- **Old profiles with `--extra-domain` block launches once a project opts in.**
  → Doctor names each key. The error suggests `network.extra_domains` with
  level 2.

## Migration Plan

- No state migration is needed. `LaunchSpec` and `LastUsed` gain optional
  fields that default when absent, and the capabilities cache is rebuilt
  because of the version bump.
- Projects opt in by adding `[security] level`. Until then, launcher commands
  are unchanged, except for commit review's pinned egress.
- The example `.project-sandbox-tui.toml` assigns personas to its actions. It
  drops the `spec-commits` step and its `commits` result key, because jj
  already keeps work in distinct changes.
- Rollback is a plain revert. Configs using `[security]`, `[catalogue]`,
  `[personas]`, `actions.*.persona`/`level` or `commit_review.provider` would
  then fail to load as unknown keys until those keys are removed.

## Open Questions

- What is the structure of `egress.toml`? Doctor's host listing is best-effort
  until it is confirmed, and falls back to reporting only that the file exists.
- Does `--agent-proxy` work with `--egress=sidecar`? The real-container smoke
  test answers this. A negative result only adds an availability rule.
