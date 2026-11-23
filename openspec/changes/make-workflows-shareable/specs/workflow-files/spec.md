# Spec Delta

## Purpose

Lets a project keep a workflow, the actions it runs and the host commands it
needs in one self-contained, separately trusted file that can be shared between
projects.

## ADDED Requirements

### Requirement: Workflow file discovery and naming
sbx-tui SHALL load each regular file `.project-sandbox-tui/workflows/NAME.toml`
in the checkout that opened it as workflow `NAME`. `NAME` SHALL follow the
`[commands]` naming rules. Files without the `.toml` suffix SHALL be ignored.
A `.toml` file whose stem is not a valid name, or that is a symlink or not a
regular file, SHALL be skipped with a warning naming the path.

#### Scenario: File becomes a workflow
- **GIVEN** a trusted `.project-sandbox-tui/workflows/spec.toml`
- **WHEN** the TUI loads its configuration
- **THEN** workflow `spec` is available to `:workflow`

#### Scenario: Invalid file name
- **GIVEN** a file `.project-sandbox-tui/workflows/2nd pass.toml`
- **WHEN** the TUI loads its configuration
- **THEN** it warns that the file name is not a valid workflow name and loads no workflow from it

### Requirement: Workflow file contents
A workflow file SHALL contain one `[workflow]` table with the keys of an inline
workflow, and MAY contain `[actions.NAME]` tables, a `[commands]` table and a
`[source]` table. Command values MAY be multi-line shell scripts. Unknown
top-level tables or keys SHALL be reported as warnings and ignored, as for the
project config.

#### Scenario: Multi-line bundled command
- **GIVEN** `spec.toml` defines `[commands] test = '''` followed by two shell lines
- **WHEN** a workflow step `host:test` runs
- **THEN** both lines run with `sh -c` in the reviewed workspace, as for a `[commands]` entry in the main config

#### Scenario: Missing workflow table
- **GIVEN** `release.toml` has `[actions.check]` but no `[workflow]` table
- **WHEN** the TUI loads its configuration
- **THEN** it reports that `release.toml` has no `[workflow]` table and workflow `release` is unavailable

### Requirement: Self-contained step references
Inside a workflow file, `planning_action`, action steps and `host:NAME` steps
SHALL refer only to actions and commands defined in the same file, by their
short names. `human-review` stages SHALL behave as in inline workflows.

#### Scenario: Step refers to main config action
- **GIVEN** `spec.toml` lists step `review` and defines no `[actions.review]`
- **AND** the main config defines `[actions.review]`
- **WHEN** the TUI loads its configuration
- **THEN** it reports that workflow `spec` refers to unknown action `review` and workflow `spec` is unavailable

### Requirement: Namespaced actions and commands
An action or command `SHORT` defined in workflow file `NAME` SHALL be available
as `NAME/SHORT`. Short names SHALL follow the `[commands]` naming rules.
Namespaced names SHALL NOT collide with main config names, which cannot contain
`/`.

#### Scenario: Namespaced names
- **GIVEN** `spec.toml` defines actions `plan` and `check` and command `test`
- **WHEN** the TUI loads its configuration
- **THEN** the command line offers actions `spec/plan` and `spec/check` and command `spec/test`
- **AND** a main config action named `plan` stays available as `plan`

### Requirement: Bundled actions and commands run on their own
Namespaced actions and commands SHALL be usable wherever main config actions and
commands are: `:NAME/SHORT`, `:&NAME/SHORT` and `:*NAME/SHORT` for commands,
`:NAME/SHORT` for actions, and as `[leader]` targets. They SHALL run with the
same rules as main config entries.

#### Scenario: Continue by hand after ending a workflow
- **GIVEN** workflow `spec` was ended with `X` before its `implement` step
- **WHEN** the user runs `:spec/implement` on the bookmark
- **THEN** the action starts an unsupervised sandbox run with the prompt and persona from `spec.toml`

#### Scenario: Leader mapping to a bundled command
- **GIVEN** the main config maps leader `t` to `*spec/test`
- **WHEN** the user presses the leader key followed by `t`
- **THEN** `spec/test` runs in each target workspace

#### Scenario: Leader target from an unavailable workflow
- **GIVEN** the main config maps leader `t` to `spec/test` and `spec.toml` is not trusted
- **WHEN** the TUI loads its configuration
- **THEN** it warns that leader `t` refers to `spec/test` from unavailable workflow `spec`, the mapping is inactive, and the rest of the configuration loads

### Requirement: Persona-only agent selection in workflow files
An action in a workflow file MAY set `prompt`, `persona`, `timeout` and `level`.
Setting `profile`, `agent`, `model` or `effort` SHALL make the file invalid.
An action without `persona` SHALL resolve its agent as a main config action
without agent settings does.

#### Scenario: Profile in a workflow file
- **GIVEN** `spec.toml` has `[actions.implement] profile = "deep"`
- **WHEN** the TUI loads its configuration
- **THEN** it reports that workflow file actions select agents with `persona` only, and workflow `spec` is unavailable

#### Scenario: Persona configured only locally
- **GIVEN** `spec.toml` sets `persona = "house-style"` and `[personas.house-style]` exists only in the main config
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports a warning that workflow `spec` depends on locally configured persona `house-style`

### Requirement: Invalid workflow file is isolated
A trusted workflow file that cannot be parsed or validated SHALL make only that
workflow, and its namespaced actions and commands, unavailable. The TUI SHALL
report the file and the reason, and the rest of the configuration SHALL load.

#### Scenario: One broken file
- **GIVEN** trusted `spec.toml` refers to an unknown persona and trusted `release.toml` is valid
- **WHEN** the TUI loads its configuration
- **THEN** it reports the problem in `spec.toml` and workflow `release` is available

### Requirement: Unique workflow names across sources
A workflow name defined both inline as `[workflows.NAME]` in the effective
config and as a workflow file SHALL be a load error naming both definitions.

#### Scenario: Inline and file workflow with the same name
- **GIVEN** the project config has `[workflows.spec]` and the checkout has a trusted `spec.toml`
- **WHEN** the TUI loads its configuration
- **THEN** loading fails with an error naming `[workflows.spec]` and `.project-sandbox-tui/workflows/spec.toml`

### Requirement: Per-file trust with trusted copies
Each workflow file SHALL be trusted on its own. sbx-tui SHALL keep the trusted
text per workflow name in the repository state directory. A never-trusted file
SHALL be ignored with a warning. A changed file SHALL leave the trusted copy in
effect, with a warning, until the change is reviewed.

#### Scenario: New file is not used until trusted
- **GIVEN** `release.toml` was added and never trusted
- **WHEN** `sbx-tui doctor` runs
- **THEN** it reports that `release.toml` is not trusted and is ignored, and workflow `release` is unavailable

#### Scenario: Changed file keeps the trusted copy
- **GIVEN** trusted `spec.toml` whose `check` prompt was edited since it was trusted
- **WHEN** the TUI loads its configuration without a review
- **THEN** `spec/check` runs with the trusted prompt and the TUI warns that `spec.toml` changed since it was trusted

#### Scenario: Deleted file
- **GIVEN** `spec.toml` was trusted and has since been deleted
- **WHEN** the TUI loads its configuration
- **THEN** workflow `spec` is unavailable

### Requirement: Reviewing workflow files
`sbx-tui --trust-config` and the TUI start-up review SHALL present the project
config and each new or changed workflow file separately, each with its diff
against its trusted copy, and SHALL trust exactly the reviewed text of each file
the user accepts. Declining one file SHALL NOT affect the others.

#### Scenario: Accept one, decline another
- **GIVEN** `spec.toml` and `release.toml` both changed since they were trusted
- **WHEN** the user runs `sbx-tui --trust-config`, accepts `spec.toml` and declines `release.toml`
- **THEN** the new `spec.toml` is in effect and `release` keeps its trusted copy

#### Scenario: File changes during review
- **GIVEN** the user is reviewing the diff of `spec.toml`
- **WHEN** the file changes again before the user accepts
- **THEN** the text that was shown is trusted, and the next load reports `spec.toml` as changed

### Requirement: Started workflows are unaffected by file changes
A workflow started from a workflow file SHALL keep its snapshotted steps when the
file is edited, re-trusted, untrusted or deleted, including after a TUI restart.

#### Scenario: File removed mid-workflow
- **GIVEN** workflow `spec` started and its `implement` step is pending
- **WHEN** `spec.toml` is deleted and the TUI restarts
- **THEN** the `implement` step runs with the settings and prompt captured at start
