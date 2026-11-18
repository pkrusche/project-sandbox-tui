# Implementation plan: agent run feedback

Add a versioned, per-run protocol for agents to report progress, task outcomes,
and structured continuation state. The TUI will display these reports and can
later use them to drive explicit queue policies and bounded task loops.

Keep the agent's assessment separate from process exit status. Implemented background reporting, queue gating and bounded decisions, and host
and interactive reporting. See [agent feedback](docs/feedback.md) for configuration
and protocol details.

## 1. Define the reporting protocol

- [x] Define a version 1 JSON schema and strict parser in a new `feedback.py`
  module. Use JSON initially to match existing persistence and avoid another
  parser dependency.
- [x] Generate a unique `run_id` before launch, independently of the sandbox
  session ID and queue task ID. Each retry gets a new run ID.
- [x] Allocate a durable directory under the repository's state directory:

  ```text
  runs/<run_id>/
      context.json
      output/
          feedback.json
  ```

- [x] Make `context.json` launcher-owned. Include the protocol version, run ID,
  queue task ID when applicable, and explicitly selected previous-task state.
- [x] Define the report fields and semantics:
  - `schema_version`: integer protocol version.
  - `run_id`: identity supplied by the launcher.
  - `sequence`: increasing integer identifying each published snapshot.
  - `phase`: `running` or `finished`.
  - `outcome`: `null` while running; `success`, `failure`, or `blocked` when
    finished.
  - `summary`: human-readable explanation of progress or outcome.
  - `checks`: optional reported verification, with names and outcomes.
  - `state`: bounded JSON object for task-specific continuation information.
- [x] Specify required fields, permitted check outcomes, unknown-field behavior,
  size/depth limits, and protocol compatibility rules. Reject duplicate keys,
  invalid types/enums, non-finite numbers, and mismatched run IDs.
- [x] Keep workflow-specific interpretation of `state` outside the core protocol;
  allow future workflows to require their own state schema.
- [x] Start with one replaceable snapshot. Defer an event stream until progress
  history is needed.

Example final report:

```json
{
  "schema_version": 1,
  "run_id": "a56c...",
  "sequence": 3,
  "phase": "finished",
  "outcome": "success",
  "summary": "Implemented retry handling; relevant tests passed.",
  "checks": [
    {
      "name": "scripts/test.sh tests/test_jobs.py",
      "outcome": "passed"
    }
  ],
  "state": {
    "implementation_complete": true,
    "needs_review": true
  }
}
```

## 2. Add sandbox transport and deployment

- [x] Use project-sandbox's existing `--mount` capability to expose reporting
  context/output and provision the helper and skill before the agent starts.
  Upstream source inspection confirmed bind-mount support and Python in generated
  images; no project-sandbox changes are needed.
- [x] Expose a stable sandbox location such as `/run/sbx-feedback`, with context
  read-only and a separate writable output directory. Mount only this run's
  files, never the TUI's entire state directory.
- [x] Keep feedback outside the workspace so it survives workspace deletion and
  works for both the main checkout and branch workspaces. Do not place it under
  `.project-sandbox`, which upstream masks with an empty read-only mount.
- [x] Provision reporting for both newly created and reused workspaces. Do not
  rely on ignored files installed in the original checkout being inherited.
- [x] Extend `sandbox.py` launch construction and `doctor.py` capability/version
  checks. Define clear behavior for unsupported sandbox versions: ordinary runs
  can retain existing behavior, while policies requiring feedback must report
  the missing capability before launch.
- [x] Preserve dry-run behavior: preview deployment and paths without creating
  reporting files or launching a sandbox.

Upstream reference:
[workspace masking](https://github.com/pkrusche/project-sandbox/blob/v0.3.0/docs/runtime.md#workspace-masking).

## 3. Provide a reporting helper and bundled skill

- [x] Bundle a small executable, provisionally `sbx-report`, inside the sandbox.
  Ensure its runtime dependencies are installed; do not assume the TUI package
  itself is available there.
- [x] Support progress and final reports, for example:

  ```sh
  sbx-report update --summary "Implementation complete; running tests"
  sbx-report finish --outcome success \
    --summary "Implemented retry handling; relevant tests passed." \
    --state-file /tmp/task-state.json
  ```

- [x] Have the helper read launcher context, supply identity and sequence
  numbers, validate input, and publish through a temporary file followed by
  atomic replacement. Serialize concurrent helper writes. Reject further
  helper updates after a final report.
- [x] Package `skills/sbx-run-feedback/SKILL.md` alongside the existing
  configuration skill. Explain progress reporting, final outcomes, checks,
  continuation state, and how to use the helper.
- [x] Instruct agents to report only checks actually performed and to mention
  reporting failures in their normal final response.
- [x] Deploy the skill at launch using the sandbox integration. Make it
  discoverable by supported agents and explicitly readable by other agents.
- [x] Append a short instruction to background prompts pointing to the deployed
  skill and reporting context. Skill discovery alone must not be the only cue.
- [x] Package and verify helper/skill resources in both the wheel and sdist.

## 4. Collect reports and display outcomes

- [x] Extend `JobRecord` with run identity, feedback path, accepted snapshot, and
  collection status (`missing`, `valid`, `invalid`, or `unsupported`). Preserve
  backward compatibility when loading older records.
- [x] Preserve existing process status and exit-code handling. Use these rules:

  | Observation | Interpretation |
  |---|---|
  | Exit 0 and final success report | Reported task success |
  | Exit 0 and final failure report | Task failed despite normal process exit |
  | Exit 0 and final blocked report | Task blocked |
  | Exit 0 without a valid final report | Task outcome unknown |
  | Timeout, interruption, or nonzero exit | Execution unsuccessful; retain report for diagnosis |
  | Final report while process remains alive | Display report; keep workspace busy |

- [x] Poll feedback in the existing refresh cycle, validating identity and
  sequence. Distinguish the last accepted snapshot from errors reading a newer
  candidate; malformed feedback must not silently authorize queue progression.
- [x] Read feedback once more before finalizing completion and dispatching the
  next task. Freeze the accepted final result after collection; later file edits
  must not retroactively change a completed task's outcome.
- [x] Persist accepted feedback in job/task history, including across restart,
  job replacement, and workspace cleanup. Align raw-file retention with existing
  cleanup behavior without losing retained task results.
- [x] Show progress, reported outcome, summary, checks, state, and collection
  errors in task details. Include relevant outcome information in completion
  notifications.
- [x] Preserve existing locking order and completion-notification claiming when
  multiple TUI instances observe the same run.
- [x] Treat feedback as an agent assertion. Workflows that need independent
  verification can additionally require a host-run verification task.

## 5. Add explicit queue policies and state handoff

- [x] Initially retain existing FIFO dispatch behavior; collecting feedback must
  not silently change which tasks run.
- [x] Add an opt-in policy such as `require_reported_success`: advance only after
  normal process completion and a valid final success report. Otherwise pause
  with a reason, including missing, malformed, unsupported, failed, or blocked
  reports.
- [x] Pass only explicitly selected, validated previous-task state into the next
  run's context. Define behavior when the expected state is absent or invalid.
- [x] Add configured branching/loop rules separately from the reporting
  protocol. Reports supply data, not executable commands or permission to enqueue
  arbitrary tasks.
- [x] Persist decisions and successor task IDs through the existing queue
  transaction mechanism so restarts or multiple TUI instances cannot dispatch a
  successor twice.
- [x] Require explicit retry/iteration limits and preserve the relationship
  between logical tasks and individual run attempts.

## 6. Integrate initialization and diagnostics

- [x] Extend `--init` to install the reporting guidance and add an idempotent,
  marked block to `AGENTS.md`. Preserve all content outside the managed block.
- [x] Scope the guidance to runs with reporting context, for example: "When
  launched with sbx feedback context, follow the deployed reporting skill and
  publish a final result."
- [x] Keep `doctor` diagnostic: check protocol support, skill availability, and
  the guidance block, then recommend `--init` for repairs. An explicit
  `doctor --fix` can be added separately.
- [x] Define skill/block upgrade behavior that preserves user edits and reports
  conflicts instead of overwriting customized content.
- [x] Document the protocol, deployment, status semantics, state storage, and
  queue policy in the existing architecture, configuration, and session docs.

## 7. Validate and deliver in stages

- [x] Stage 1: protocol, upstream transport, helper/skill deployment, background
  collection, TUI display, and init/doctor support.
- [x] Stage 2: opt-in queue gating, validated state handoff, and bounded task
  decisions.
- [x] Stage 3: host and interactive reporting. Define the task/session completion
  boundary explicitly; completing a conversational turn must not accidentally
  finalize an entire interactive session. Define behavior for adopted sessions
  that were launched without reporting context.
- [x] Add isolated protocol/helper tests covering malformed and oversized input,
  schema versions, duplicate keys, stale run IDs, sequence ordering, atomic
  updates, and sealed final reports.
- [x] Extend fake-sandbox job/queue tests for process/report disagreement,
  missing reports, timeout/interruption, restart adoption, retained history,
  concurrent TUI instances, and exactly-once successor dispatch.
- [x] Extend the opt-in real-container smoke test using the Bash agent/helper to
  verify mount permissions, new and reused workspaces, main-checkout runs, and
  feedback survival after workspace deletion. No model API is needed.
- [x] Test repeated initialization and preservation of existing `AGENTS.md`
  content and customized skills.
- [x] Validate under `docs/development.md`: the full suite passed (1,109 tests,
  3 skips), followed by the updated feedback suite (49 tests, including the TUI
  successor-dispatch regression). Ruff lint/format, ty, and the sdist/wheel build
  pass. Test state is isolated in `tmp_path`; tests run through `scripts/test.sh`.
- [ ] Execute the extended real-container smoke test on a runtime-equipped host.
  The test is implemented, but this workspace has neither project-sandbox nor a
  Docker/Podman/Apple container runtime. This remains an integration validation
  step, not an upstream implementation dependency.
