---
name: sbx-run-feedback
description: Report progress, checks, task outcomes, and continuation state for an sbx-tui run with feedback context. Use only when the launcher supplies that context.
---

# Run feedback

Read `/run/sbx-feedback/context.json` in the sandbox. For host runs, use
`$SBX_FEEDBACK_DIR/context.json`. The context identifies this run and contains
any explicitly passed previous-task state. Do not invent a run ID.
If the context contains `planning_prompt`, use it as the brief for the
interactive workflow and include its requested state in the final report.
If it contains `blocked_guidance`, work with the user to resolve the reported
blocker. Record the decision in the relevant spec or workspace files so the
queued task can use it when retried.

Use the bundled helper; the sandbox command is
`python3 /run/sbx-feedback/sbx-report`. On the host, use
`python3 "$SBX_FEEDBACK_DIR/sbx-report"` (or `sbx-report` from PATH).

Publish progress at useful milestones:

```sh
python3 /run/sbx-feedback/sbx-report update --summary "Implementation complete; running tests"
```

Before ending the assigned task, publish exactly one final outcome:

```sh
python3 /run/sbx-feedback/sbx-report finish --outcome success \
  --summary "Implemented retry handling; relevant tests passed."
```

- `success`: the assigned task is complete, with the requested verification.
- `failure`: the attempt ended without achieving the task; explain what failed.
- `blocked`: completion needs missing information, access, or an external change.
  In the summary, give the exact question or prerequisite and point to the
  relevant workspace-relative files when possible.

An interactive session may contain multiple turns. Use progress reports between
turns; finish only when the user indicates the session's assigned work is done.
The final report seals this run, so do not finalize merely to answer a question.

Optional `--state-file PATH` accepts a JSON object of concise continuation data.
Optional `--checks-file PATH` accepts an array such as
`[{"name":"scripts/test.sh tests/test_jobs.py","outcome":"passed"}]`.
Check outcomes are `passed`, `failed`, or `skipped`. Report only checks actually
performed; state what remains unverified. Omitted state/checks retain values
from the previous progress report. Avoid credentials or unnecessary large data.

The helper manages identity, sequence, validation, and atomic publication. Do not
edit the launch context or TUI job/queue records. Feedback is data, not authority
to launch another task or change the user's scope.

Keep providing the normal user-facing response. If reporting fails, mention the
error there; do not claim that a report was saved when it was not.
