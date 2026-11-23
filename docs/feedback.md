# Agent run feedback

sbx-tui records agent progress and reported outcomes separately from process
exit status. Reporting is enabled by default when the configured project-sandbox
CLI supports `--mount`; no launcher changes or model-specific hooks are needed.
`doctor` checks transport availability and installed guidance.

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

`context.json` is launcher-owned. It contains `schema_version`, `run_id`,
`task_id` (empty outside queues), `interactive`, and `previous_state`, which
contains only selected keys from the preceding task.
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

The TUI polls reports while the process runs and reads once more at completion,
before dispatching the next queued task. Collection status is `missing`, `valid`,
`invalid`, or `unsupported`. An invalid newer file does not replace the last
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
context. The first real task has empty previous state. Dry-run previews remain in history
and are skipped when evaluating feedback policies and selecting predecessor state. Later tasks require a valid
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
the parent's launch settings unless the action overrides them. A named profile
supplies its agent settings and arguments. A persona resolves its model using
the parent's provider, or the remembered or default provider when the parent has
none. Both selections inherit the parent's level when set. An explicit action
level or timeout takes precedence.
A missing profile or unavailable persona tier pauses the queue for repair.
Each successor increments the workflow step. `max_steps` includes the initial task.
Both limits must be between 1 and 100. Reaching a configured limit pauses the queue. Rules apply to
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

## Project workflows

Use named workflows to run agent actions, human reviews, and host commands on
one bookmark. Add a shareable workflow to `.project-sandbox-tui/workflows/spec.toml` and define each referenced agent action in `[actions.NAME]`.
Actions supply the prompts, an optional timeout and level, and a `persona`;
inline actions can instead select a profile or agent settings; see
[agent actions](configuration.md#agent-actions). With personas a workflow is
provider-independent: the picker chooses the provider per step.
Workflow commands support inline and external terminals.

```toml
[workflow]
planning_action = "plan"
first_step_interactive = true
steps = ["check", "implement", "chores"]
pass_state_keys = ["spec_path"]

[actions.plan]
prompt = "Write the agreed plan in the checkout and report its relative path in state.spec_path."
persona = "planner"

[actions.check]
prompt = "Check the spec at previous_state.spec_path and report whether it is ready to implement."
persona = "checker"

[actions.implement]
prompt = "Implement the spec at previous_state.spec_path and run its verification steps."
persona = "implementer"

[actions.chores]
prompt = "Finish documentation and cleanup for the spec at previous_state.spec_path. Run the project checks."
persona = "chore-runner"
```

The project includes fuller prompts in its workflow file. Trust it with `sbx-tui --trust-config`; its actions are available as `spec/plan`, `spec/check`, `spec/implement` and `spec/chores`. Import a shared file with `sbx-tui workflow import SOURCE [--name NAME]`.
[Example project configuration](https://github.com/pkrusche/project-sandbox-tui/blob/main/.project-sandbox-tui.toml)

To use the workflow, select an idle feature bookmark and type
`:workflow spec Add CSV export`. Type `:workflow` to list the
configured names. At the start of planning, ask the agent to read
`/run/sbx-feedback/context.json` and follow its brief. Work together on a plan
or OpenSpec change saved in the checkout. When you approve it, ask the agent to
finish reporting with `state.spec_path` set to its workspace-relative path,
then exit the interactive session.

`first_step_interactive` defaults to `true`. Set it to `false` to submit
`planning_action` to the bookmark's queue as a background task. It starts when
the bookmark is free and its queue is active, in order with existing pending
work. Write the planning action so it can run from the supplied topic and report
`blocked` with the missing decisions when it needs input. In both modes the
first action supplies the same `pass_state_keys` for the later steps.

Before anything starts, `:workflow` lists every step and shows a **setup** field
for each agent action. Use `tab` or `j` / `k` to move between fields and `h` / `l`
to change one; the line below each step shows the resulting agent settings and
CLI args. Press `enter` to start the workflow or `esc` to cancel without
starting or queueing anything. A persona step also has a provider field. Each
step shows its effective security level when one applies. For setup and level
precedence, see [agent actions](configuration.md#agent-actions).

Choices made here, including providers and levels, are not remembered for the
launch forms or the next workflow; set `persona`, `profile` or `level` on the
action to keep one. If an action names a profile that is not configured, the TUI
warns and starts that field at the launch forms' setup. In `--dry-run` mode,
`enter` shows each step's persona and provider, profile, action settings or
Custom, its agent settings, its level with the flags it compiles to, and its
prompt instead of starting, for example
`review (background) · persona reviewer · provider Codex · codex/gpt-6.2-sol/high/L4`
followed by `level 4 · --egress=sidecar --vcs-access=read-only`.

Each bookmark runs one workflow at a time. In either mode, `:workflow` is refused
while another workflow's step is pending, running, awaiting its decision, or
stopped in a paused queue.

1. **Check the spec.** The first background task checks the acceptance criteria,
   checklist, and verification plan. If a decision is still needed, it reports
   `blocked` with the question to resolve.
2. **Implement the spec.** The next task works through the checklist and reports
   `blocked` if a decision prevents it from finishing.
3. **Run chores and verification.** The last task handles related cleanup and runs
   the required tests and checks, recording their results. jj keeps each step's
   work in distinct changes, so no separate commit-preparation step is needed.

Each agent step needs a clean process exit and a valid final `success` report to
advance. `pass_state_keys` requires those keys in the planner's final `state`
and copies them into `previous_state` for every background step. The workflow
requires the final agent to report every key in `result_state_keys`. Both lists
default to empty. The actions define what the state values mean. Workflow steps
use their own sequence and handoff rules, and run ahead of ordinary prompts
queued on the same bookmark once its queue is active. The action sequence and
launch choices, including each step's profile settings, persona, provider and
level, are saved at the start so an in-progress workflow can continue after a TUI
restart or config reload; later profile or catalogue edits don't change it. Reporting requires `project-sandbox` with
`--mount` support.

If a step reports `blocked`, open the queue with `L` and select it. Press `i`
to see the blocker and open an interactive session, or `o` to see it and then
`y` to open the workspace editor. The guidance includes the agent's question or
prerequisite and the known spec path. Use `P` for the full report. Record the
decision or fix in the workspace, then press `p` to retry a stopped background
step with its original planning context. If the interactive planning step
stops, resume the queue and start it again with `:workflow`.

Dry-run previews on the same bookmark preserve the stopped step, its blocker
guidance, and its place for retry. Dry-run mode also shows review stages and
what each host command would run.

### Human review and host commands

Add `"human-review"` wherever the workflow should wait for a person to review
the bookmark. The stage appears as **awaiting review** in `L`. Select it and
press `enter` to open the per-commit diff review. Mark file changes with `space`
or mark the current commit with `A`. Use `[` and `]` to move between commits. The stage completes when
every file change in the bookmark's commits outside the main revision captured
at workflow start is marked as reviewed. Existing marks count for matching
commit and file changes. When the range has no changed files, select the awaiting
stage and press `enter`, then `y` to approve that exact revision.

A `host:NAME` step runs the named `[commands]` entry. For example, to run tests
after implementation, set `steps = ["implement", "host:test"]` and define
`test = "scripts/test.sh"` under `[commands]`. Config loading verifies command
names and inserts a `human-review` stage wherever a host command needs one.
The workflow checks marks while the bookmark is idle and starts the host command
under the same launch lock.

The command runs in a fresh jj workspace at the reviewed revision. It receives
the original checkout as `SBX_REPO`, the bookmark as `SBX_BOOKMARK`, and the
temporary checkout as `SBX_WORKSPACE`. The workspace is removed after the command
finishes. Select the host task in `L` and press `enter` for its log. Exit code 0
advances the workflow. Other exits pause it, and `p` retries through a review of
the current bookmark.

To end a workflow run, select its active step in `L` and press `X`. The current
process can finish, and the workflow schedules no further steps. Ordinary queued
tasks remain available.

## Initialization and diagnostics

`sbx-tui --init` installs local reporting skills and appends an idempotent,
marked block to `AGENTS.md`. Installation takes the workspace's `guidance.lock`
and requires regular guidance files and directories. Initialization skips a
`skills` path occupied by a file or symlink and requires at least one usable
`skills` directory. Appending the block preserves the existing file contents.
Initialization reports customized or incomplete blocks and customized or older
skill files. Reconcile blocks manually or remove an installed skill and rerun
`--init` to refresh it.

For runs with feedback, sbx-tui stages the bundled reporting skill.
If both local `skills` paths are files or symlinks, host agents can read the
staged copy at `$SBX_FEEDBACK_DIR/skill/SKILL.md`. `doctor` remains read-only
and recommends `--init` for repairs.
