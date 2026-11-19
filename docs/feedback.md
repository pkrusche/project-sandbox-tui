# Agent run feedback

sbx-tui collects structured progress and task outcomes separately from process
exit status. Reporting is enabled by default when the configured project-sandbox
CLI supports `--mount`. No project-sandbox modification or model-specific hook
is required. `doctor` checks transport availability and installed guidance.

## Reporting from an agent

Each launch has its own run ID and directory under the repository's state
directory, independent of the sandbox session ID and queue task ID:

```text
runs/<run_id>/
    context.json
    sbx-report
    skill/SKILL.md
    output/feedback.json
```

The sandbox receives this directory at `/run/sbx-feedback`, read-only, with a
separate writable mount for `output/`. Only that run is mounted. The skill is
also mounted at `/home/agent/.agents/skills/sbx-run-feedback` and
`/home/agent/.claude/skills/sbx-run-feedback`. These mounts apply to the main
checkout and new or reused branch workspaces. They do not rely on ignored files
being inherited by a new workspace. The helper uses only Python's standard
library; project-sandbox's generated images include Python 3.

Background agent prompts include a short reporting instruction. Bash prompts
remain executable scripts and must invoke the helper explicitly:

```sh
python3 /run/sbx-feedback/sbx-report update --summary 'Implementation complete; running tests'
python3 /run/sbx-feedback/sbx-report finish --outcome success \
  --summary 'Implemented retry handling; relevant tests passed.'
```

`finish` accepts `success`, `failure`, or `blocked`. Use `blocked` when completion
requires information, access, or an external change. `--state-file PATH` accepts
a JSON object; `--checks-file PATH` accepts an array of checks such as
`[{"name":"scripts/test.sh","outcome":"passed"}]`. Check outcomes are `passed`,
`failed`, and `skipped`. Omitted state/checks retain their previous values;
explicit `{}` / `[]` clears them. Only report checks actually performed.

The helper supplies run identity and sequence numbers, serializes concurrent
writes, and atomically replaces the report. A final report is sealed. Reporting
does not replace the agent's normal response; reporting errors should be
mentioned there.

Host launches receive `SBX_FEEDBACK_DIR` and the helper on their launch-specific
PATH. Use `python3 "$SBX_FEEDBACK_DIR/sbx-report"` or `sbx-report`. The TUI
installs the skill and conditional `AGENTS.md` guidance in the actual host
workspace, preserving customized files. Interactive agents should publish
progress between turns and finish only when the user indicates that the
session's assigned work is complete. Sessions adopted without reporting context
remain usable and have an unknown reported outcome.

## Protocol version 1

`context.json` belongs to the launcher. It contains `schema_version`, `run_id`,
`task_id` (empty outside queues), `interactive`, and `previous_state` (an object
containing only selected predecessor keys).
Interactive workflow runs also receive `planning_prompt`, the configured brief
and any topic supplied when starting the workflow.
Interactive sessions opened to resolve a blocker receive `blocked_guidance`,
which describes the issue and identifies the spec when its path is available.

Example final report:

```json
{
  "schema_version": 1,
  "run_id": "launcher-supplied-id",
  "sequence": 3,
  "phase": "finished",
  "outcome": "success",
  "summary": "Implemented retry handling; relevant tests passed.",
  "checks": [{"name": "scripts/test.sh tests/test_jobs.py", "outcome": "passed"}],
  "state": {"implementation_complete": true, "needs_review": true}
}
```

All fields except `checks` and `state` are required. Unknown report fields and
duplicate JSON keys are rejected. `schema_version` must be integer 1; `run_id`
must match the launch. `sequence` is an integer from 1 to 2^53−1. Repeated reads
of the same snapshot are permitted; changed snapshots must increase sequence.
`phase` is `running` with a null outcome or `finished` with a terminal outcome.
`summary` is nonempty text. `checks` contains at most 100 objects, each with only
`name` (nonempty text) and `outcome`. `state` is an arbitrary JSON object whose
meaning belongs to the workflow, not the core protocol.

Documents are limited to 128 KiB and nesting depth 16 (root depth zero).
Non-finite numbers, symlinks, and nonregular report files are rejected. The
helper applies the same validation as the collector. Future schema versions
require explicit reader support; they are never interpreted as version 1.

## Collection and display

The TUI polls reports while the process runs and reads once more when completion
is observed, before queue dispatch. Collection status is `missing`, `valid`,
`invalid`, or `unsupported`. An invalid newer file does not discard the last
accepted snapshot, but it prevents that snapshot from authorizing progression.
After completion, collection is frozen; subsequent file edits cannot change
the persisted task result.

| Process/report | Meaning |
|---|---|
| Exit 0, valid final success | Reported task success |
| Exit 0, valid final failure or blocked | Task failed or blocked despite normal exit |
| Exit 0, missing/invalid/nonfinal report | Task outcome unknown |
| Nonzero exit, timeout, interruption | Execution unsuccessful, even if success was reported |
| Final report while process is alive | Report displayed; workspace remains busy |

The queue shows process status and reported outcome separately. `P` shows the
retained report, checks, state, and collection errors. Run details and log titles
also show feedback; notifications include it when reporting is enabled.
Reports are agent assertions, not independent verification. A workflow can
require a separate host verification task when needed.

Accepted snapshots are retained in job and queue-task records, including across
workspace deletion, job-history pruning, and TUI restarts. Raw run directories,
like logs, remain in the state directory until manually removed. Removing raw
files for completed runs does not remove retained snapshots. Do not remove files
belonging to active runs.

## Queue policies

Defaults preserve existing FIFO behavior:

```toml
[feedback]
enabled = true
require_reported_success = false
pass_state_keys = []
retry_outcomes = []
max_attempts = 1
on_success = ""
on_failure = ""
on_blocked = ""
max_steps = 1
```

Set `require_reported_success = true` to pause after a predecessor without normal
process completion and a valid final success report. The reason is persisted.
Resuming with `p` explicitly overrides that pause for the next task; it does not
replay the decision for the failed task.

`pass_state_keys` selects top-level state keys to copy into the next task's
context. The first task has empty previous state. Later tasks require a valid
final predecessor report with all requested keys; otherwise dispatch pauses.
Generated successor tasks use their recorded parent. Missing/deleted parent
reports cannot silently supply state from a different task.

Optional bounded automation uses configured actions, not commands emitted by an
agent:

```toml
[feedback]
require_reported_success = true
retry_outcomes = ["failure"]
max_attempts = 2
on_success = "review"
max_steps = 2

[actions.review]
prompt = "Review {bookmark} against {main} and report the outcome."
```

Retries repeat the task with a new run/task ID and an incremented attempt.
`max_attempts` includes the first attempt. `on_success`, `on_failure`, and
`on_blocked` name existing `[actions.NAME]` entries. Successor actions inherit
the parent's launch settings except for explicit action overrides, and increment
the workflow step. `max_steps` includes the initial task. Both limits must be
between 1 and 100. Reaching a configured limit pauses the queue. Rules apply to
each completed task in the chain: in the example, a successful review reaches
the step limit instead of spawning another review.

Retries take precedence over action routing, and an explicitly configured retry
or successor takes precedence over success-only gating. They require exit code 0
and a valid final report; transport/process failures do not cause automatic
retries. Successors run before already queued tasks. Decisions use the trusted
configuration at decision time and are persisted with successor IDs before
launch, so concurrent TUIs and restarts do not create duplicate successors.
Pausing a queue also pauses decisions. Deleting a generated pending successor
does not cause it to be generated again.

Policies requiring feedback fail before launch if `--mount` is unavailable.
Ordinary runs fall back to unsupported/unknown feedback. Disable reporting with
`feedback.enabled = false`; dependent policies must also be disabled. Dry-run
previews show the intended mount paths without staging reporting files or probing
the CLI. Commas and newlines in reporting mount paths are rejected because the
upstream mount format cannot represent them.

## Initialization and diagnostics

`sbx-tui --init` installs local reporting skills and appends an idempotent,
marked block to `AGENTS.md`. Existing prose is preserved. Customized/incomplete
blocks and customized/older skill files are preserved and reported; reconcile
blocks manually or remove an installed skill and rerun `--init` to refresh it.
Launch-time sandbox skill mounts use the bundled version independently of these
local copies. `doctor` remains read-only and recommends `--init` for repairs.
