# Tasks

## 1. Profile and isolated input

- [x] 1.1 Add `commit_review.profile` and the built-in `commit-summary` profile.
  Document the setting and resolved default in `docs/configuration.md`.
  Use config tests for the default, named override, and missing or incompatible
  selection.
- [x] 1.2 Stage patches for the pinned IDs in review order. Use VCS tests to
  compare the staged patches to file review for ordinary, rewritten, empty,
  and merge commits and to verify that a moved bookmark cannot alter the input.
- [x] 1.3 Launch one sandbox against staged input with structured feedback
  enabled. Use fake-launch tests to verify profile settings, isolation of the
  reviewed checkout and store, incompatible mount options, and cleanup on
  preparation failure, cancellation, and completion.

## 2. Feedback and review display

- [x] 2.1 Validate final run status and per-commit entries. Use feedback tests
  for absent and malformed entries, duplicate and unexpected IDs, invalid
  complexity and boolean values, missing harm reasons, and valid feedback
  followed by a failed exit.
- [x] 2.2 Add the action, status, and assessment display to per-commit review.
  Cover a nonblocking batch launch, duplicate suppression, navigation,
  plain-text rendering, reopening, rewritten commits, partial results, and
  retry after failure with Textual tests and a fake agent. Verify direct
  comparisons and seen marks, and document the interaction in `docs/usage.md`.
- [x] 2.3 Add a dry-run preview with the selected profile and pinned range.
  Test that it creates no inputs and starts no process. Verify that starting
  a summary leaves the remembered launch profile unchanged.

## 3. Integration verification

- [x] 3.1 Exercise the launch-to-display path with the fake sandbox, including
  moving the bookmark while the run is pending. Run the relevant tests through
  `scripts/test.sh`, then run the standard checks in `docs/development.md`.
- [x] 3.2 Verify the mount boundary in a real container smoke run without
  model credentials. Use a disposable repository and prove the review
  harness can read staged patches and publish feedback while the live checkout
  and jj store are inaccessible. Record the runtime and result.

  Result (2026-11-20, Linux host): passed on Docker 29.9.3 with
  project-sandbox 0.4.1 and jj 0.46.0, credentials not forwarded.
  `scripts/test.sh -n 0 -v -s tests/test_container_smoke.py --container-smoke
  --container-runtime docker -k commit_summary` ran
  `test_real_commit_summary_mount_boundary` in 10.2 s.
