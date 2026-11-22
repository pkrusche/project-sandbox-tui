# Project Sandbox — Task-to-Agent Selection Specification

**Version:** 1.2 draft · **Date:** 2026-11-22  
**Companion:** [`sandbox-options.json`](sandbox-options.json)  
**Targets:** `project-sandbox`, `project-sandbox-tui`, `agentgateway-locally`

## 1. Objective and scope

Implement a small task-first wizard that selects (1) a model *route/provider* and capability tier, (2) a compatible harness and reasoning effort, and (3) network, VCS, credential, and runtime isolation. The wizard should yield a validated **LaunchPlan**, not directly construct arbitrary shell commands. Advanced mode exposes all controls; basic mode asks at most task, sensitivity, and whether downloads are required, then shows a review screen.

The companion JSON is an **advisory seed catalogue**, not a promise of provider availability. `model_candidates[].model_id_verified=false` deliberately requires live confirmation of exact model IDs and supported switches. A name displayed in GitHub Copilot may not be a valid direct API ID; never infer one from the other. Similarly, do not assume every listed model supports every effort setting. Some candidate names derive from the prior research report and must be reconciled against actual model discovery before production use.

## 2. Existing integration points

`project-sandbox` documents `--agent {claude,codex,opencode,pi,bash}`, `--model`, `--effort {low,medium,high,xhigh,max}`, `--runtime {auto,apple-container,docker,podman}`, `--branch`, `--keep-workspace`, `--timeout`, `--pi-tools`, and `--pi-ollama`. The current entrypoint maps model/effort differently per harness: Claude `--model`/`--effort`; Codex `--model` and `-c model_reasoning_effort=...`; OpenCode `--model`/`--variant`; Pi `--model`/`--thinking`. Bash ignores them. Treat **Copilot CLI as a proposed adapter pending a capability probe** rather than assuming the CLI already accepts `--agent copilot`.

`project-sandbox-tui` already has a model discovery/update mechanism (`:update-models` and `docs/models.md`). Reuse it as a source and enrich with route-specific runtime discovery. `agentgateway-locally` supplies an authenticated model proxy; route through it without injecting provider secrets into the sandbox. Confirm exact port/host and reachable model list from deployed configuration, not hardcoded assumptions.

Sources: [sandbox usage](https://github.com/pkrusche/project-sandbox/blob/main/docs/usage.md), [security](https://github.com/pkrusche/project-sandbox/blob/main/docs/security.md), [TUI models](https://github.com/pkrusche/project-sandbox-tui/blob/main/docs/models.md), [gateway](https://github.com/pkrusche/agentgateway-locally), [Copilot models](https://docs.github.com/en/copilot/reference/ai-models/supported-models).

## 3. Data model

Split the system into:

- **Static catalogue:** `sandbox-options.json` provides tiers, suggested model families, candidate IDs, presets, switches, policies, and source references. Ship versioned with the TUI.
- **Discovered capabilities:** short-lived records keyed by `(route, provider, model ID, harness version)` with model availability, actual accepted effort values, context window, tool support, entitlement, and last probe timestamp. Cache with TTL and expose stale state.
- **Policy:** organization and repository security ceilings/floors. Policies are immutable by ordinary TUI overrides. A user can request *more* isolation without authorization, never less.
- **LaunchPlan:** normalized, fully resolved values and validation evidence; include selected route, exact route-specific model ID, harness adapter, translated effort arguments, egress policy, mount plan, VCS plan, runtime, approval policy, timeouts, budget, and audit metadata.

Use separate `model_key` (stable catalogue identity), `model_id` (provider API ID), and `copilot_model_id` (CLI selection ID). A model's **tier is an advisory classification**, not an objective benchmark. Avoid storing API keys or authentication tokens in any of these files.

### Example resolved plan

```json
{
  "schema_version": "1.0.0",
  "task_preset": "routine_code",
  "provider": "anthropic",
  "route": "gateway",
  "model_key": "anthropic:claude-sonnet-5",
  "resolved_model_id": "<model-id-from-live-discovery>",
  "tier": "medium",
  "harness": "claude",
  "effort": {"ui": "medium", "native": "<validated-adapter-value>"},
  "network": {"mode": "api_only", "allowed_endpoints": ["<gateway-host>:<port>"]},
  "vcs": {"mode": "isolated_worktree", "branch": "agent/task-123"},
  "runtime": "auto",
  "credentials": "gateway_only",
  "approval": "interactive",
  "validation": {"status": "pending"}
}
```

## 4. TUI interaction

**Basic mode:**

1. **Task:** Quick question / Routine code / Dependency update / Complex refactor / Security review / Sensitive project / Air-gapped / Frontier investigation.
2. **Safety:** Normal / Confidential / Restricted, plus *Needs internet downloads?* (No / Trusted registries / Broad internet).
3. **Review:** `Medium · Sonnet · Claude · Medium thinking · API-only · Git worktree · Apple container`, with an explanation and a `Change` affordance for each field. Launch only if all constraints pass.

**Advanced mode:** Provider route → capability tier → discovered model → harness → effort → network mode + allowlist → VCS isolation → engine → credentials → approval/tools → timeout/budget/logging. Disable unsupported options with explicit reasons rather than silently rewriting them.

Offer `Auto` for model, effort, and engine. `Auto` means a deterministic policy-driven choice, not random selection. Display estimated cost only when current price metadata is verified; otherwise `cost unknown`.

## 5. Resolution algorithm

```text
function resolve(request, platform, capabilities, policies):
    plan = defaults()
    plan = merge(plan, preset(request.task))
    plan = merge(plan, user_preferences())
    plan = merge(plan, request.explicit_overrides)
    plan = enforce_immutable_security_constraints(plan, policies)

    if request.sensitivity in {confidential, restricted}:
        require(plan.vcs in {isolated_worktree_review, read_only_snapshot})
        require(plan.credentials in {gateway_only, none})
        require(plan.network in {api_only, offline})

    if plan.network == offline:
        require(plan.route == local and local_model_reachable())

    engines = probe_engines(platform)
    plan.runtime = choose_compatible_engine(plan.runtime, engines, policies)
    candidates = discover_models(plan.route, plan.provider)
    candidates = filter(candidates, plan.tier, plan.harness, policies)
    plan.model = select_deterministically(candidates, user_preference, quality, cost)
    require(plan.model is not null)
    plan.effort = translate_effort(plan.model, plan.harness, plan.effort)
    require(plan.effort.validated)
    plan.egress = compile_and_probe_egress(plan.network, plan.route)
    plan.mounts = compile_mount_policy(plan.vcs, plan.credentials)
    validate_all(plan)
    return plan
```

**Selection priority:** repository/organization policy > user safety overrides > preset > convenience preferences. `trusted` is not automatically permissible for a sensitive task just because dependencies are needed: show a blocked action with an explanation and require a separately approved policy exception. `auto` fallback must stay within the same security boundary; provider failover is **off by default**, because changing providers may change data handling.

## 6. Effort and harness translation

The TUI presents `Auto / Low / Medium / High / Extra high` plus any model-specific extensions. The adapter translates to native controls only after probing support. Do not pass generic `max` to every model. `ultra`/multi-agent orchestration is a separate *execution strategy*, not a universal reasoning value. For Pi, preserve `provider/model[:thinking]` semantics and resolve any conflicts between an inline thinking suffix and an explicit effort selection. For OpenCode, validate that the requested variant exists. For Codex, validate `model_reasoning_effort` support. For Claude, validate the CLI's accepted effort values. Copilot must be probed for installed version, model list, entitlement, and reasoning controls before enabling its adapter.

Prefer explicit compatibility records:

```text
Compatibility(route, provider, model_id, harness, harness_version)
  available: bool
  native_model_selector: string
  supported_effort: [string]
  supported_tools: [string]
  checked_at: timestamp
  error: optional string
```

Never convert a failed model probe into a claim of unavailability forever; retry and allow manual refresh.

## 7. Isolation and security semantics

**Network:**

| Mode | Intended egress | Enforcement |
|---|---|---|
| Unrestricted | Any outbound destination | Explicit confirmation; only if policy allows |
| Trusted | Explicit FQDN/port allowlist | Firewall + DNS controls; reject unsupported rules |
| API-only | Selected gateway or specific model API endpoints | Deny all other outbound; no generic web browsing |
| Offline | No outbound access | Local inference only; reject remote model routes |

The existing sandbox security documentation describes a default-drop egress allowlist and DNS restrictions. Preserve its fail-closed behavior. **Allowlisting a domain does not prevent exfiltration to that domain**; the gateway may itself forward confidential content. API-only is not the same as local-only or confidential-computing. Consider gateway-side provider restrictions, logging retention, and DLP as separate controls.

**VCS:** `current_checkout` is lowest isolation; `isolated_worktree` uses existing `--branch`; `isolated_worktree_review` adds a mandatory human merge/review gate; `read_only_snapshot` is **proposed** and needs an actual read-only mount or copy mechanism, not just a UI label. Git/jj metadata mounts are still potential mutation surfaces: protect parent metadata and ensure workspace path checks, branch uniqueness, and cleanup logic. Review mode must not auto-merge. Concurrent runs must use unique branches/bookmarks.

**Credentials:** default to a scoped gateway credential only. Do not expose host provider keys, SSH agent, cloud credentials, or unrestricted host mounts to untrusted runs. Use existing credential staging mechanisms, and document which credential is visible to the process. Explicitly test egress and mount boundaries *inside* the launched container.

**Runtime:** macOS preference: Apple `container`, then Docker, then Podman if installed/healthy and policy-compliant. Linux: Docker, then Podman, subject to policy and rootless/privilege checks. Windows: WSL2 Linux only as an explicitly supported deployment target; do not imply native support. Never downgrade to a weaker engine to make a launch succeed silently.

## 8. CLI compilation

The TUI should call a typed backend interface rather than interpolating command strings. Map a validated LaunchPlan to existing CLI flags:

```text
--agent <claude|codex|opencode|pi|bash>
--model <resolved_model_id>
--effort <validated_supported_effort>
--runtime <auto|apple-container|docker|podman>
--branch <unique_branch>
--keep-workspace            # optional
--timeout <seconds>
--pi-tools <tools>          # Pi only
--pi-ollama                # Pi local route only
```

Network modes and review policies that lack a direct CLI flag **require backend implementation before exposing them as enforced features**. A review gate may be implemented in TUI workflow, but enforcement for headless/CLI callers must live in the core or shared policy layer. Ensure CLI launch does not accept contradictory values after validation.

## 9. Validation, telemetry, and tests

**Preflight:** verify gateway reachability/auth without exposing secrets, provider model discovery, harness installation, accepted effort, runtime health, firewall capability, network route, workspace and branch safety, and credential scope. If anything security-critical cannot be verified, block launch. Print a redacted launch plan and the *effective* sandbox policy.

**Unit tests:** all model/tier mappings; invalid effort rejection; Copilot ID vs provider ID; missing model; missing runtime; offline+remote model conflict; sensitivity override rejection; unique worktree names; no automatic cross-provider fallback; policy precedence; gateway failure.

**Integration tests:** every installed harness adapter; network attempts to permitted and forbidden endpoints; DNS tunneling attempts; host mount isolation; Git and jj review workflows; macOS Apple container and Linux Docker/Podman; crash cleanup; concurrent sessions; logs without credentials.

**Acceptance criteria:** 100% of launches produce an auditable validated plan; all blocked combinations explain why; unsupported options are disabled; no sensitive-task run can bypass policy via direct CLI; no unexpected network egress; no silent provider/engine downgrade; existing CLI behavior remains compatible for legacy users.

## 10. Implementation phases

1. **Catalog + capability layer:** ship JSON, parser, versioning, model discovery cache, normalized provider/route IDs, capability probe API. No security behavior changes.
2. **TUI wizard:** task presets, safety questions, advanced picker, review screen, disabled-option explanations, launch plan preview.
3. **Shared policy compiler:** enforce network modes, credential scope, VCS review requirements, read-only snapshot, runtime checks in core; do not expose unfinished modes as working.
4. **Harness adapters:** harden Claude/Codex/OpenCode/Pi effort translation, then add Copilot CLI only after supported command surface is verified.
5. **End-to-end hardening:** security integration tests, cost metadata refresh, provider failover opt-in, migration tooling, audit log and documentation.

## 11. Backward compatibility and migration

Continue accepting existing `--agent`, `--model`, `--effort`, `--runtime`, and `--branch` flags. Convert legacy TUI selections into a LaunchPlan at launch time. Preserve raw user-specified model IDs but mark them `custom/unverified` until validated; do not rewrite old IDs to a newer family by string substitution. Keep repository policy separate from local user preferences. Version both the catalogue and the resolved-plan contract; reject unknown major schema versions and provide a migration command.

## 12. Open implementation decisions

- Which network modes can be expressed through existing project-sandbox firewall/config switches without core changes? Inspect and test exact CLI/config behavior.
- How does the current TUI represent agent/harness vs provider route, and what discovery cache is already available?
- Is Copilot CLI currently integrated in any branch beyond the documented CLI agent enumeration?
- Which gateway model-list endpoint and provider auth modes are enabled in the user's deployed service?
- Should restricted projects forbid all remote inference, or permit approved gateway providers? Make this an organization policy, not an implicit assumption.
- How should budget enforcement work when token/cost metadata is missing? Default to warning rather than claiming a hard cap.

**Security note:** This document describes a proposed selection architecture. The JSON includes switches that are *not yet implemented* as enforceable CLI features; these must be gated behind capability checks until completed.


## 14. November 2026 route-specific model refresh (v1.1)

The model selection contract is now **route-first**, not provider-first. The same upstream model can be accessed through different billing, authentication and feature surfaces. Never combine them without explicit route selection. The JSON `route_catalogues` is the normative seed for the UI; `model_candidates` is the union of known and discovery-only candidates.

| Route | Harnesses | Preferred catalogue | Discovery |
| --- | --- | --- | --- |
| Codex | `codex` | GPT-6 Luna/Sol/Astra, GPT-6.1 Terra/Sol, GPT-6.2 Sol/Astra | Codex CLI and account |
| Claude Code | `claude` | Haiku 5.5, Sonnet 5.5, Opus 5.5, Fable 5.2 | Claude Code `/model` and account |
| GitHub Copilot | `copilot`, `opencode` | GitHub Copilot CLI model IDs, plus models supported by plan docs where client discovery confirms | CLI model selection, Copilot account and organization policy |
| agentgateway-locally | `pi`, `opencode` | **Exact names from `config/config.yaml` only** | Parse tracked YAML and optionally authenticated `/v1/models` |

**Gateway sync limitation:** the GitHub blob and raw URL were not retrievable in this execution environment. The updated JSON therefore deliberately records `gateway_config_status=unverified_fetch_blocked`, `gateway_config_model_ids=null` and `models_from_config=null`. It is **not** a claimed transcription of the latest YAML. Do not replace these with guesses or automatically fill from public provider lists. The repository README independently confirms an OpenAI/Anthropic local proxy, but not the actual model list. A local checkout or user-supplied YAML is needed to complete this source-specific synchronization.

### 14.1 Gateway configuration sync

At build time, read the actual YAML from `agentgateway-locally/config/config.yaml` (via a pinned checkout, vendored file, or GitHub contents API). Parse `llm.models` according to the actual config structure; preserve `name`, `provider`, `params.model`, aliases and transformation rules exactly. Distinguish exposed names from upstream names. Generate `route_catalogues.agentgateway_local.models_from_config` and `catalogue_sync.gateway_config_model_ids`; attach source commit SHA and SHA-256. Do not infer model names from upstream providers. A gateway instance's authenticated `/v1/models` can further filter the list; failure to query it must not be interpreted as no models configured.

### 14.2 Copilot CLI vs plan catalogue

GitHub's CLI reference documents `--model=MODEL`, `COPILOT_MODEL` and `auto`. The canonical CLI IDs currently include `claude-sonnet-5.5`, `claude-opus-5.5`, `claude-haiku-5.5`, `gpt-6-luna`, `gpt-6.1-terra`, `gpt-6.1-sol`, `gpt-6.2-sol`, `gpt-6.2-astra`, `gpt-6-astra`, `gemini-3.8-flash`, and `gemini-4-hydrogen`. These **differ from direct Anthropic IDs** (dots vs hyphens). The wider Copilot plan model page additionally lists Fable 5.2 and Gemini 4 Argon/Carbon/Neon; do not assert CLI support from the plan table alone. For OpenCode, verify that its Copilot provider adapter exposes the specific model. The TUI should show models unavailable on the selected subscription/client as disabled with a reason, not silently substitute a gateway API model.

### 14.3 Effort translation

`effort` is a semantic request, not a universal literal flag. Codex maps supported values to `model_reasoning_effort`, with model-specific constraints (notably GPT-6.2 rejects `none`). Claude Code uses its own effort setting and adaptive thinking on 5.5/Fable 5.2. OpenCode maps to a model/provider-specific variant. Pi maps to `--thinking` only when its provider and selected model support it. Copilot CLI must be probed for its own model-specific controls. Unsupported combinations must be rejected or explicitly reset after user confirmation; never silently discard a requested thinking level.

### 14.4 TUI resolution algorithm

```text
route = choose_route(codex | claude_code | copilot | agentgateway_local)
harness = choose_compatible_harness(route)
models = static_seed(route) intersect discovered_models(route, harness, account)
if route == agentgateway_local:
    models = exact_config_names intersect live_gateway_names_if_available
model = choose_model(models, tier, task)
effort = supported_effort(route, harness, model, requested_effort)
policy = enforce_security_floor(task, repository, network, vcs, runtime)
validate_all_or_block(route, harness, model, effort, policy)
launch(plan)
```

### 14.5 Required tests

- Copilot CLI dotted Claude IDs are not accidentally passed to Claude Code, and vice versa.
- Codex rejects legacy GPT-5 candidates from the primary selector.
- Claude Code primary selector only shows 5.5 generation and Fable 5.2.
- Pi never offers subscription-only Copilot models.
- OpenCode distinguishes gateway routing from Copilot subscription routing.
- Gateway model aliases remain byte-for-byte identical to configured names.
- Unavailable gateway config produces `unknown`, not a fabricated model list.
- Security isolation remains enforced regardless of selected model or route.

### 14.6 Sources and verification status

- [Gateway configuration (exact source, **not fetched**)](https://github.com/pkrusche/agentgateway-locally/blob/main/config/config.yaml)
- [Gateway repository README](https://github.com/pkrusche/agentgateway-locally)
- [GitHub Copilot supported models](https://docs.github.com/en/copilot/reference/ai-models/supported-models)
- [GitHub Copilot CLI reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
- [Claude Code model configuration](https://support.claude.com/en/articles/11940350-claude-code-model-configuration)
- [Codex model guidance](https://learn.chatgpt.com/docs/models?surface=cli)

## 12. Gateway configuration sync (v1.2, 2026-11-22)

**Source of truth:** the user-provided `agentgateway-locally/config/config.yaml` in this conversation. The static gateway catalogue is now fully populated with the **18 configured entries**, and provider references and upstream IDs are preserved verbatim. This establishes *configuration*, not live endpoint health, API-key entitlement, or successful inference. Do not interpret `model_id_verified=true` as upstream availability; it means the ID is verified **in the supplied YAML**.

| Provider | Configured model IDs | Count |
|---|---|---:|
| OpenAI | `gpt-5-mini`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna` | 7 |
| Anthropic | `claude-opus-5`, `claude-opus-5-5`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-haiku-4-5`, `claude-fable-5`, `claude-fable-5-1` | 7 |
| Gemini | `gemini-4-argon`, `gemini-4-carbon`, `gemini-4-hydrogen`, `gemini-4-neon` | 4 |

**Harness boundary:** all these gateway models are candidates for **Pi** and **OpenCode** only, subject to adapter compatibility. Do not automatically offer them through Codex or Claude Code. Codex retains its GPT-6+ native catalogue; Claude Code retains the Claude 5.5 / Fable 5.2 native catalogue; Copilot CLI and OpenCode via Copilot use a separate Copilot-discovered catalogue. A model appearing in two catalogues is two independently validated routes, not one entitlement.

### Gemini tier correction

| ID | Tier | Role |
|---|---|---|
| `gemini-4-hydrogen` | **fast** | Low-latency routine implementation, tests, maintenance |
| `gemini-4-argon` | **medium** | General reasoning and nontrivial coding |
| `gemini-4-neon` | **high** | Strong, efficient complex coding and reasoning |
| `gemini-4-carbon` | **frontier** | Extended, difficult engineering and security work |

The tiers optimize **task selection**, not a strict intelligence leaderboard. Neon is frontier-capable and can beat Carbon on some evaluations; Carbon is the explicit long-horizon specialist. Do not force a numeric quality ordering across tasks. References: [Google Carbon model card](https://storage.googleapis.com/deepmind-media/Model-Cards/Gemini-4-Carbon-Model-Card.pdf), [Google Neon model card](https://storage.googleapis.com/deepmind-media/Model-Cards/Gemini-4-Neon-Model-Card.pdf).

### Gateway defaults and effort handling

- OpenAI: `gpt-5-mini` defaults to `reasoning.effort=medium`; all six other OpenAI entries default to `reasoning.effort=high`.
- Anthropic: all seven entries default to `thinking.type=adaptive` and `output_config.effort=high`.
- Gemini: the YAML supplies **no default thinking/effort parameters**. The TUI should offer `auto` (omit override) until the Pi/OpenCode adapter and gateway/provider combination has been tested. Do not assume the Antigravity CLI's thinking suffixes apply to gateway API model IDs.
- Defaults are not proof of a supported range. For effort overrides, probe or use a validated per-route capability manifest. Reject unsupported combinations rather than silently substituting a different effort.

### Discovery and validation

1. Load the 18 configured entries from the static catalogue, retaining exact model names and upstream mappings.
2. Read the gateway base URL from deployment/runtime configuration. The supplied YAML defines MCP port `3000` and UI port `15000`, **not** the LLM gateway listening port. Do not hardcode `4000`.
3. Authenticate to the gateway using its strict gateway key, not an upstream provider key. Query `/v1/models` if exposed by this deployment and compare with the static catalogue; absence of the endpoint must be reported as `unverified`, not as model removal.
4. Test an actual short completion per selected model/harness/effort before marking it `working`. Separate `configured`, `listed`, `authenticated`, and `inference_verified` states.
5. On config drift, warn and offer refresh; never rewrite the user-managed gateway YAML automatically. Store last check and error reason; redact credentials from logs.
6. Network `api_only` should allow the sandbox to reach the configured gateway endpoint while the gateway handles upstream access. Do not confuse the gateway's CORS `localhost:15000` origin with the sandbox's egress allowlist.

### Required tests

- The configured gateway model set is **exactly 18**: 7 OpenAI, 7 Anthropic, 4 Gemini; no missing or extra gateway entries.
- Every configured entry preserves `name`, `provider.reference`, and `params.model` from YAML.
- Pi and OpenCode show the gateway models; Codex and Claude Code do not inherit the gateway catalogue.
- Gemini Hydrogen/Argon/Neon/Carbon resolve to fast/medium/high/frontier respectively.
- OpenAI and Anthropic default request parameters are preserved; Gemini defaults remain absent.
- Missing gateway port, key, model list, or inference capability fails with an actionable message and never silently broadens network access.

**Migration:** v1.1 consumers can continue reading the top-level `model_candidates`; v1.2 adds `gateway_configured`, `gateway_default_parameters`, `gateway_model_configuration`, and `gemini_tiering`. Prefer `route_catalogues.agentgateway_local.models_by_provider` for exact gateway filtering. A future schema should distinguish `configured_model_id_verified` from `upstream_model_id_verified` explicitly.
