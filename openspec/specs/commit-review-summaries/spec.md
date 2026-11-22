# commit-review-summaries Specification

## Purpose
Give reviewers a quick agent-generated assessment of each commit alongside its
changed files in the TUI.

## Requirements

### Requirement: On-demand batch from commit review

The per-commit review SHALL provide a **Summarize commits** action. Invoking
it SHALL start one background analysis for the full set of pinned commits
without interrupting review navigation. Opening a review alone SHALL leave the
agent idle.

#### Scenario: Review a bookmark's commits

- **GIVEN** the current review contains three pinned commits
- **WHEN** the user invokes **Summarize commits** from the second commit
- **THEN** one agent run receives all three pinned commits in review order
- **AND** the user can continue moving between commits and opening file diffs

#### Scenario: Analysis is pending

- **WHEN** the user invokes the action again while analysis of the same range is pending
- **THEN** the existing run remains in progress and no duplicate run starts

#### Scenario: Compare two revision tips

- **WHEN** the user opens the direct comparison of two revision tips
- **THEN** the comparison has no **Summarize commits** action

#### Scenario: Dry-run mode

- **WHEN** the user invokes **Summarize commits** in dry-run mode
- **THEN** the TUI previews the pinned range and chosen profile without starting a run

### Requirement: Dedicated agent profile

Commit summary runs SHALL use a dedicated default profile. A
`commit_review.profile` setting SHALL select an existing named agent profile.
Starting a summary SHALL preserve the remembered profile for other launches.

#### Scenario: Default profile

- **GIVEN** no `commit_review.profile` is configured
- **WHEN** the user summarizes commits
- **THEN** the run uses the built-in `commit-summary` profile with Codex and low effort
- **AND** the remembered launch profile is unchanged

#### Scenario: Explicit profile

- **GIVEN** `commit_review.profile` names a configured agent profile
- **WHEN** the user summarizes commits
- **THEN** the run uses that profile's agent settings, including its model, effort, and fast mode where supported

#### Scenario: Unknown or incompatible profile

- **WHEN** the user invokes a summary with a profile that is missing or incompatible with isolated analysis
- **THEN** the TUI identifies the problem and leaves the review usable without starting a run

### Requirement: Isolated analysis of pinned patches

The agent SHALL receive the commit identity and patch corresponding to each
pinned revision in the review. The run SHALL have no write access to the
reviewed checkout, repository metadata, or bookmarks.

#### Scenario: Bookmark moves during analysis

- **GIVEN** a review pinned a commit before its bookmark moved
- **WHEN** the user starts or finishes analysis of that review
- **THEN** the patch and result still correspond to the pinned commit
- **AND** the result is never attached to the replacement commit

#### Scenario: Commit with multiple parents

- **WHEN** the agent receives a merge commit's patch
- **THEN** its patch uses the same automatically merged parent baseline as the file review

### Requirement: Per-commit assessment

The review SHALL display the selected commit's summary, complexity (L/M/H),
and potentially destructive or harmful flag (Y/N) above its changed files.
Complexity SHALL reflect the context required to assess the change. A Y flag
SHALL include a short reason describing the potential impact.

#### Scenario: Browse completed results

- **GIVEN** the run returned valid results for two commits
- **WHEN** the user moves between those commits
- **THEN** the assessment follows the selected full commit ID
- **AND** the file table and its review controls remain usable

#### Scenario: Reopen the review

- **GIVEN** an assessment succeeded for a commit during this TUI session
- **WHEN** the user opens a review containing that same full commit ID
- **THEN** the prior assessment is available without another invocation
- **AND** a rewritten commit with a different ID has no inherited assessment

### Requirement: Analysis status and recoverability

The review SHALL identify pending or unavailable assessments. It SHALL
associate only valid results from a successful final run with requested
commit IDs. An unavailable assessment SHALL be eligible for another attempt.

#### Scenario: Partial or malformed result

- **WHEN** a successful run returns a missing entry or entries with duplicate IDs or invalid fields
- **THEN** affected requested commits show **Unavailable** and carry no Y/N result
- **AND** independently valid entries for requested commits can still be displayed
- **AND** entries for unrequested IDs are ignored

#### Scenario: Run fails after reporting

- **WHEN** a run times out, exits unsuccessfully, or lacks valid final success feedback
- **THEN** the TUI explains that this run yielded no new assessments
- **AND** the user can retry without leaving the review
