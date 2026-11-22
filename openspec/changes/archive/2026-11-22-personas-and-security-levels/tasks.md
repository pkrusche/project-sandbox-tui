# Tasks

## 1. Catalogue and personas

- [x] 1.1 Add `catalogue.py` with the vendor tier maps, the eight provider rows (agent, gateway, effort and fast rules, explicit Copilot IDs and empty tiers) and `model_for`. Verify with new `tests/test_catalogue.py` cases for every built-in cell in the model-catalogue spec, including Copilot dotted IDs never reaching `claude-code` and missing Copilot tiers returning none.
- [x] 1.2 Add built-in personas (including internal `commit-reviewer`) with field-wise `[personas.NAME]` merging and validation of tier, effort, fast and level 1–5. Verify with `tests/test_catalogue.py` and `tests/test_config.py` cases for the agent-personas scenarios: override, a new persona without a tier, an invalid level.
- [x] 1.3 Add `[catalogue]` config (`default_provider`, `gateway_agent` limited to pi/opencode, `models.<vendor|row>` overrides with unknown-row errors), and expose the keys in `sbx-tui config set/get/list`. Verify with `tests/test_config.py` and `tests/test_settings.py`.
- [x] 1.4 Document provider rows, tiers, personas and catalogue overrides in `docs/models.md` and `docs/configuration.md`, and check the examples load with `sbx-tui config list` in a temporary config.

## 2. Security levels and argv compilation

- [x] 2.1 Add `[security]` (`level` 1–5, `egress` sidecar|firewall) and the `Network.build_proxy`, `build_no_proxy` and `build_ca_certs` fields. Make `security.egress`, and the corporate network keys when a level is in effect, global-only, with errors naming the key. Verify with `tests/test_config.py` cases for the security-levels user-config scenarios.
- [x] 2.2 Add `security.py` with `effective_level`, `level_flags` (level-2 sources per `project.build`, `network.extra_domains`/`allow_github` at level 2 only, no `--extra-domain` behind an internet proxy) and `managed_conflicts`. Verify with unit tests covering each level, each build type, and the proxy case.
- [x] 2.3 Add `persona`, `provider` and `level` to `LaunchSpec` with tolerant `from_dict`. Split the network block of `build_argv`: corporate flags plus `--build-*` always, and level flags in place of the legacy network flags when a level applies. Verify `tests/test_sandbox.py`: a project without `[security]` gives byte-identical argv to before, levels 1–5 give the specified flags, and an old queued spec runs at the project default.
- [x] 2.4 Add `security_problems` for managed flags in shared, profile and custom extra arguments, and call it wherever `launch_problems` is checked before a launch. Verify with tests that a conflicting profile is refused with its config key and the `network.extra_domains` hint, and that `--memory=16g` is kept.
- [x] 2.5 Document the levels, egress backend, global-only corporate settings and the managed-flag rule in `docs/configuration.md` (settings reference and a new security levels section). Verify that the documented TOML loads in a test config.

## 3. Launcher discovery for levels

- [x] 3.1 Parse `--egress {…}` into `Capabilities.egress_modes`, persist it in the cache, and bump `CACHE_VERSION` to 2. Verify with `tests/test_capabilities.py` cases using the supplied `--help` text, an older help without `--egress`, and a version-1 cache that is treated as stale.
- [x] 3.2 Add `level_problems` (no `--egress` blocks levels 2–5, a missing backend blocks 3–5, no `--vcs-access` blocks 4–5, fallback discovery blocks nothing) and refuse launches at unavailable levels. Verify with `tests/test_capabilities.py` and a launch-refusal test naming `security.egress`.
- [x] 3.3 Extend `tests/fake_project_sandbox.py` to accept and record `--egress`, `--build-proxy`, `--build-no-proxy` and `--build-ca-cert`, and verify that existing fake-launcher tests still pass.

## 4. Launch forms

- [x] 4.1 Add `resolve_selection` and the `persona` `StepChoice` kind, with the provider-row tier `problem` for missing tiers, reusing `resolve_action_settings` for the other kinds. Verify with unit tests for the persona resolution table in design.md, including gateway rows using `catalogue.gateway_agent`, Gemini omitting effort, and fast mode only on Codex and Claude Code.
- [x] 4.2 Add `LastUsed.selection`, `provider` and `persona` with tolerant loading, plus `remembered_selection` (persona when no profiles exist, implementer, default provider). Verify that older `last_used.json` files load, and that persona launches leave custom settings and per-agent gateway choices unchanged.
- [x] 4.3 Turn the launch form's profile field into the **setup** selector (persona / profiles / custom). Add provider and persona fields with availability reasons, and a level field in every mode that is red below the project default, not remembered, and marks unavailable levels. Verify with `tests/test_launch_choices.py` and `tests/test_profiles.py` Textual tests for the launch-choices and security-levels scenarios, including the Copilot GitHub caveat in the hint.
- [x] 4.4 Add catalogue models to model suggestions after `[models]` and before the existing sources, so doctor's unknown-model warning accepts them. Verify with `tests/test_models.py` and the launch-choices "Catalogue models in custom mode" scenario.
- [x] 4.5 Update `docs/usage.md` (launch forms) and `docs/models.md` for the setup selector and level field, and verify the documented key bindings match the form.

## 5. Actions and workflows

- [x] 5.1 Add `Action.persona` and `Action.level` with validation (persona excludes profile/agent/model/effort; unknown persona is a load error; level 1–5) and `config set` support. Verify with `tests/test_config.py` and `tests/test_settings.py` cases from the agent-actions delta.
- [x] 5.2 Update `initial_choice` (persona first) and `_run_action` to resolve persona actions on the remembered or default provider. Ask for confirmation when the resolved level is below the project default. Verify with action-run tests for the reviewer example and the declined `:e2e` confirmation.
- [x] 5.3 Add provider and level fields to `WorkflowProfilesScreen`, refuse acceptance while a row has a tier problem, snapshot provider, persona and level into step specs, and keep choices unremembered. Verify with `tests/test_workflow.py` cases for the project-workflows delta, including restart after snapshot.
- [x] 5.4 Extend the workflow dry-run preview with persona, provider, level and compiled flags. Verify with the persona dry-run scenario test.
- [x] 5.5 Update `docs/feedback.md` (project workflows) and `docs/configuration.md` (agent actions) for personas, providers and levels, and verify that the examples load.

## 6. Commit review

- [x] 6.1 Add `commit_review.provider` (exclusive with `profile`). Resolve commit review through the internal commit-reviewer persona (default Codex), with a pinned level-3 spec that drops shared network widening and keeps corporate proxy and CA settings. Verify with `tests/test_commit_summary.py` cases for every commit-review-summaries delta scenario, including a project without `[security]`.
- [x] 6.2 Update the commit review section of `docs/configuration.md` for provider selection, pinned isolation and the sidecar requirement, and verify that the example config loads.

## 7. Doctor, setup and skills

- [x] 7.1 Add doctor checks for level conflicts per config key, the ignored `network.no_firewall`, the egress backend (`egress.toml` presence and a best-effort host list, never modified), and provider availability with failures for `default_provider` and `commit_review.provider`. Verify with `tests/test_doctor.py` cases for the agent-profile-diagnostics delta.
- [x] 7.2 Extend `setup_summary` with providers, supported levels and personas, and the firewall-backend hint when sidecar is missing. Verify with `tests/test_setup_wizard.py`.
- [x] 7.3 Update the bundled `sbx-tui-config` and `sbx-tui-safety` skills for levels, personas, providers and global-only corporate settings, and refresh the installed copies under `.agents/skills/` and `.claude/skills/`. Verify that `tests/test_packaging.py` and the skill install tests pass.

## 8. Project config, changelog and integration

- [x] 8.1 Update `.project-sandbox-tui.toml`: assign personas to the review, check, e2e and spec actions, and drop the `spec-commits` step and its `commits` result key. Verify that `sbx-tui doctor` in this repository reports no config failures.
- [x] 8.2 Add the CHANGELOG entry, including the breaking changes for opted-in projects and commit review's default sidecar egress, and check the entry against the proposal.
- [x] 8.3 Add a gateway-at-level-3 case to the opt-in real-container smoke test, and verify it is skipped cleanly without a runtime.
- [x] 8.4 Run the full checks from `docs/development.md` (`scripts/test.sh`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv build`) and verify that all pass.

## Workflow follow-up

- Run the real-container smoke test on a host with project-sandbox, a container runtime and the egress sidecar, and record whether `--agent-proxy` works with `--egress=sidecar`.
- Archive the change after review.
