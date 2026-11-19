# Design

## Context

`SandboxTUI._review_commits` builds the ordered list of `ReviewCommit` objects
for `CommitReviewScreen`. This path also opens commit reviews from queued-task
history. Direct tip comparisons use `DiffReviewScreen`. Per-commit file review
already renders a diff relative to the automatic merge of the commit's parents.

The feedback transport validates run identity and final outcomes and supports
feature-specific data in `state`. The normal bookmark job path prepares a
workspace for editing, so reuse its feedback transport and process handling
where practical and give summary runs their own isolated input and launch.

## Goals / Non-Goals

Keep analysis tied to the review snapshot, keep review navigation responsive,
and keep assessment data advisory. The scope is the per-commit review and
in-memory results for the current TUI session.

## Decisions

### Stage patches from the same revisions the reviewer sees

At invocation, capture the ordered full IDs from the open screen. Collect a
plain, noninteractive patch for each ID using the same jj revision diff
semantics as file review. Stage the IDs, subjects, and patches as data in a
temporary directory. Pass the staged input to one isolated sandbox run, with
the app checkout, jj store, and bookmark workspaces outside the agent's mounts.
Treat instructions inside patches and subjects as data. The agent writes its
structured output through the existing feedback output mount. Keep preparation
and launch off the UI thread. Discard temporary input after completion or
cancellation, and on errors during preparation or launch.

One run amortizes startup over the review. Providing just the pinned patches
limits the code exposed to the agent and removes the need to give it the live
repository. The launch needs an isolation check for shared sandbox arguments,
profile extra arguments, and paths. Apply compatible resource settings and
reject launch options that would undermine isolation. In dry-run mode, preview
the chosen profile and number of pinned commits without staging patches.

### Resolve a profile specifically for summary runs

Resolve `[commit_review] profile = "NAME"` from the existing named profile
configuration. When the setting is omitted, create the built-in
`commit-summary` configuration with Codex, the agent's default model, low
effort, and fast mode off. Resolve the selection once per request and leave
the user's remembered profile alone. Show an actionable launch error if an
explicit profile was removed or if the profile cannot run the isolated
analysis. A dedicated default keeps the cost and behavior independent of the
profile last used for implementation or debugging.

### Validate structured results before rendering

Use a final feedback report with `state.commits`, an array of objects. Each
object contains `commit_id` (full ID), `summary` (brief text), `complexity`
(one of `l`, `m`, `h`), and `harmful` (a JSON boolean).
`harm_reason` is required and nonempty when `harmful` is true. Prompt for
likely data loss, removed functionality, and weakened safeguards when judging
potential harm. Prompt for a short summary and reason to fit the existing
feedback envelope.

A valid final success report and a clean process exit authorize result
collection. Validate each requested commit entry and reject duplicate IDs for
that commit. Associate valid entries by full ID. Render agent text as plain
text above the table. Show pending status during the run and unavailable
status for commits without a valid assessment. Keep previously successful
assessments if a refresh fails, with a message reporting the refresh failure.

### Keep run state in the app

Track in-flight requests by their ordered commit IDs within the repository
and cache successful assessments by full commit ID for the TUI session. A
screen reopened during a run can show the same pending status. On completion
a mounted screen refreshes from the app's results for its selected commit.
Commit navigation, screen dismissal, and file review marks remain independent
of run state.

## Risks / Trade-offs

- Large or binary patches can exceed analysis limits. Report affected commits
  as unavailable when the input cannot support a reliable assessment. Preserve
  usable entries for the rest of the range within the same request.
- Feedback has a fixed document size. Prompt for concise results and report
  a clear failure if the feedback cannot be accepted.
- The agent sees patch context only. Its harm flag reports a possible impact
  from that evidence, and a reviewer can still open each original diff.

## Migration Plan

Existing configs acquire the built-in profile when no review profile is set.
Projects can add `commit_review.profile` to select a different named profile.
Review assessments are session data and require no persisted-state migration.
