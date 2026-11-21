# Tasks

## 1. Capability discovery and cache

- [x] 1.1 Add `capabilities.py` with `--help` parsing for `--agent`, `--vcs-access` and `--runtime` choices and `doctor --json` runtime parsing; verify with unit tests covering the real 0.4.1 `--help`/`doctor --json` shapes, a missing `--vcs-access`, malformed choices, invalid JSON and a timed-out runner
- [x] 1.2 Add host agent availability with sources (host credentials, agent gateway, token login, login in sandbox, none needed) using `project_sandbox.config_agents.available_agents()` gated on matching launcher version; verify with tests for each source, `--no-forward-credentials`, a version mismatch and an import failure giving `unknown`
- [x] 1.3 Add the versioned `capabilities.json` cache (atomic write, size cap, validation, staleness by command, version and 24 h age); verify with tests for round-trip, corrupt and oversize files, and staleness after a command or version change
- [x] 1.4 Reuse the cached `--help` text for `feedback_transport.supported` so `--help` runs once per refresh; verify existing feedback transport tests still pass and a test counts one `--help` invocation
- [x] 1.5 Add a test that runs the parser against the locked project-sandbox's real `--help` and `doctor --json` (via `uv run project-sandbox`), skipped when the command is unavailable; verify it passes locally

## 2. Relaxed config validation and pre-launch check

- [x] 2.1 Replace agent and `sandbox.runtime` membership validation in `config.py` (defaults, profiles, actions, `[models]`) and `settings.py` with the well-formed-name check; update `AGENTS`/`RUNTIMES` fallbacks with copilot, colima and chroot; verify with config/settings tests that `agent = "copilot"` loads and `"claude code"` fails naming the key
- [x] 2.2 Refuse launches whose agent, `--vcs-access` or `--runtime` is unsupported according to a probed snapshot, never a fallback one; verify with job tests using `fake_project_sandbox.py` for refused, allowed and fallback-only cases
- [x] 2.3 Document the relaxed validation and pre-launch check in `docs/configuration.md` and `CHANGELOG.md` (marked as a config-validation change); verify the docs mention copilot, colima and chroot

## 3. Agent gateway opt-in

- [x] 3.1 Add `gateway` to `AgentProfile`, `Defaults` and `LaunchSpec` (default false), `GATEWAY_AGENTS`, and `LastUsed.gateway_by_agent`; reject `gateway = true` without a merged `network.agent_proxy` naming the key; verify with config tests, including a project profile opting in to a gateway defined globally and old job records without the field still loading
- [x] 3.2 In `sandbox.py`, add `--agent-proxy`/`--agent-proxy-key-env` only for gateway launches, always pass `--model` for them (bash included), and prefix OpenCode models with `agent-proxy/` when missing; verify with `test_sandbox.py` argv tests for claude with a gateway configured, pi from two profiles, opencode with and without the prefix, and bash
- [x] 3.3 Refuse gateway launches with an agent outside pi/opencode/bash or without a model before running the launcher; verify with job tests
- [x] 3.4 Document the opt-in, model ID handling and migration in `docs/configuration.md`, the config templates and `CHANGELOG.md` (marked breaking); verify the documented example config loads with `sbx-tui config list`

## 4. Launch form and profile editor

- [x] 4.1 Refresh capabilities in a background worker at startup when stale and push updates to open forms; verify with a Pilot test that the form opens immediately without a cache and updates after the worker finishes
- [x] 4.2 Drive the agent field from discovered agents, mark agents unavailable on the host with the reason, and keep unsupported configured or remembered agents as the shown value with a "not supported by launcher" marker; verify with Pilot tests for each case
- [x] 4.3 Hide the effort and fast fields for bash and keep fast mode limited to Claude and Codex; show the gateway on/off field only for pi/opencode/bash when a gateway is configured, remember it per agent, and require a model when it is on; verify with Pilot tests for each field
- [x] 4.4 Verify `[models]` entries for gateway agents (e.g. `pi`) appear first in suggestions with a `test_models.py` case, and update `docs/models.md` and `docs/usage.md` for the launch form changes

## 5. Doctor checks

- [x] 5.1 Add `check_capabilities` reporting supported agents with availability, VCS access modes, runtimes with status, and the `auto` selection, plus a warning when fallbacks were used; verify with `test_doctor.py` cases using the injected runner
- [x] 5.2 Add `check_agent_selections` over defaults, profiles, actions with own agent fields and the commit-review profile: failures for unsupported or unavailable agents, unsupported `--vcs-access`/`--runtime` in `extra_args` (both forms, last value wins) and an unusable effective runtime; gateway failures (agent not pi/opencode/bash, missing model, Pi/OpenCode without host credentials and without `gateway = true`) and an unused-gateway message; warnings for Claude or Codex models not in suggestions and fast mode on unsupported agents; verify with a test per scenario in `specs/agent-profile-diagnostics`
- [x] 5.3 Wire discovery into `run_checks` with time limits, write the cache, and keep the current `check_runtime` as the fallback when `doctor --json` is unavailable; verify with a test where the doctor probe times out and the other checks still report, and that `:doctor` shows the new sections
- [x] 5.4 Document the new doctor output in `docs/usage.md`; verify the documented example matches the output of `sbx-tui doctor` in this repository

## 6. Setup agent selection and gateway models

- [x] 6.1 Update the bundled `sbx-tui-config` skill: telling the setup agent apart from profile agents, how to read discovered capabilities from `sbx-tui doctor`, and the gateway model procedure (`<agent_proxy>/models`; key from `pass show agentgateway-api-key`, then the `agent_proxy_key_env` variable, never printed; plain IDs saved as `[models]` suggestions; `gateway = true` on profiles; manual fallback when unreachable); verify the doctor check that the installed skill matches the template still passes after `sbx-tui --init`
- [ ] 6.2 In the setup wizard (`add-setup-wizard-skill`, in whichever change merges second), offer profile agents from discovery with their sources instead of `config.AGENTS`, keep setup agents from `PATH`, and put the capability summary in the prompt; verify with `tests/test_setup_wizard.py` cases for combined sources, copilot, and fallback wording

## 7. Integration

- [x] 7.1 Run `scripts/test.sh`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check` and `uv build`; verify all pass
- [x] 7.2 Run `sbx-tui doctor` in this repository against the installed project-sandbox 0.4.1 and open the launch form; verify copilot is offered, runtimes match `project-sandbox doctor`, and profile problems are reported with their config keys
- [x] 7.3 With the local agentgateway running, launch one OpenCode and one Pi profile with `gateway = true` and a short prompt, plus one Claude launch with the gateway configured; verify the gateway launches finish and the Claude launch has no `--agent-proxy` in its argv
