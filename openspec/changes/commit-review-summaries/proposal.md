# Proposal

## Why

The commit review shows file changes, but reviewers have to read each patch to
understand its purpose and decide which commits need closer attention. An
on-demand summary gives them a quick starting point while they browse.

## What Changes

- Add **Summarize commits** to the per-commit review. One invocation runs one
  background agent for the full range already pinned by the open review.
- Show a short summary, complexity (L/M/H), and a potentially destructive or
  harmful change flag (Y/N) above the selected commit's file table. Show a brief
  reason with Y and an unavailable state when analysis cannot be displayed.
- Give summaries a dedicated default agent profile, with a setting for selecting
  an existing named profile. Keep the reviewed checkout and bookmarks outside
  the agent's write scope.

## Capabilities

### New Capabilities

- `commit-review-summaries` covers launching agent analysis on demand and
  displaying structured feedback in commit review.

### Modified Capabilities

None.

## Impact

The change touches the per-commit review screen, pinned diff collection,
background sandbox launch and feedback, profile configuration, user
documentation, and focused tests.
