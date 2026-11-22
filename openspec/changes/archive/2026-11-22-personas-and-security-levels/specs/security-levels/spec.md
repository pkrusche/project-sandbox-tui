# Spec Delta

## Purpose

Lets a project choose sandbox isolation on a linear 1–5 scale that sbx-tui
compiles to existing project-sandbox network and VCS flags, with a project
default and visible per-job overrides.

## ADDED Requirements

### Requirement: Levels compile to launcher flags
A launch with a security level SHALL emit exactly these network and VCS flags:
level 1 `--no-firewall`; level 2 `--egress=firewall` plus level-2 sources;
level 3 the egress backend flag; level 4 the level 3 flag plus
`--vcs-access=read-only`; level 5 the level 3 flag plus `--vcs-access=hidden`.

#### Scenario: Level 4 with the sidecar backend
- **GIVEN** the egress backend is sidecar
- **WHEN** a launch runs at level 4
- **THEN** the launcher arguments contain `--egress=sidecar` and `--vcs-access=read-only`, and no `--no-firewall`, `--extra-domain` or `--allow-github`

#### Scenario: Level 1
- **WHEN** a launch runs at level 1
- **THEN** the launcher arguments contain `--no-firewall` and no `--egress` or `--vcs-access`

### Requirement: Level 2 sources
At level 2, sbx-tui SHALL add `--extra-domain` for the package sources of
`project.build`, then for each `network.extra_domains` entry, and
`--allow-github` when `network.allow_github` is true. Built-in sources SHALL be
`pypi.org` and `files.pythonhosted.org` for python-uv, `crates.io`,
`index.crates.io` and `static.crates.io` for rust-cargo, and
`registry.npmjs.org` for node-npm.

#### Scenario: Python project at level 2
- **GIVEN** `project.build = "python-uv"` and `network.extra_domains = ["example.org"]`
- **WHEN** a launch runs at level 2
- **THEN** the launcher arguments contain `--egress=firewall` and `--extra-domain` for `pypi.org`, `files.pythonhosted.org` and `example.org`

#### Scenario: Dockerfile project at level 2
- **GIVEN** `project.dockerfile` is set and `network.extra_domains` is empty
- **WHEN** a launch runs at level 2
- **THEN** the launcher arguments contain `--egress=firewall` and no `--extra-domain`

#### Scenario: Shared domains outside level 2
- **GIVEN** `network.extra_domains = ["example.org"]`
- **WHEN** a launch runs at level 3
- **THEN** the launcher arguments contain no `--extra-domain`

### Requirement: Level 2 behind an internet proxy
When `network.internet_proxy` is set, a level 2 launch SHALL NOT add
`--extra-domain` and SHALL state in its preview that the proxy's policy decides
which hosts are reachable.

#### Scenario: Corporate proxy
- **GIVEN** `network.internet_proxy` is set in the global config
- **WHEN** the user previews a level 2 launch
- **THEN** the arguments contain `--internet-proxy` and no `--extra-domain`, and the preview says the proxy policy applies

### Requirement: Egress backend for levels 3 to 5
`security.egress` SHALL choose the backend for levels 3 to 5: `sidecar`
(default) emits `--egress=sidecar`, and `firewall` emits `--egress=firewall`
with no extra domains. sbx-tui SHALL NOT switch backends when the selected one
is unavailable.

#### Scenario: Firewall backend
- **GIVEN** the global config sets `security.egress = "firewall"`
- **WHEN** a launch runs at level 3
- **THEN** the launcher arguments contain `--egress=firewall` and no `--extra-domain`

#### Scenario: Sidecar not offered by the launcher
- **GIVEN** the egress backend is sidecar and the discovered `--egress` choices do not include `sidecar`
- **WHEN** the user starts a level 3 launch
- **THEN** sbx-tui refuses the launch naming `security.egress` and the supported egress modes

### Requirement: Project default level
`security.level` SHALL set the default level for every launch of the project.
Without a configured level, launches SHALL keep the network and VCS behaviour
they had before this change.

#### Scenario: Default applies to a profile launch
- **GIVEN** `security.level = 3`
- **WHEN** the user launches a named profile with an unchanged level field
- **THEN** the launch runs at level 3

#### Scenario: No security section
- **GIVEN** no `security.level` is configured and `network.no_firewall = true`
- **WHEN** the user launches any profile
- **THEN** the launcher arguments contain `--no-firewall` as before

### Requirement: Per-job level overrides
When a security level is in effect, the `i` / `r` launch forms and the workflow
picker SHALL show a level field for each job. A level below the project default
SHALL be highlighted in red. The chosen level SHALL apply to that job only.

#### Scenario: Lower level highlighted
- **GIVEN** `security.level = 3`
- **WHEN** the user changes a launch form's level to 2
- **THEN** the level field is shown in red and the launch runs at level 2

#### Scenario: Level not remembered
- **GIVEN** the user launched at level 1 in a project with default 3
- **WHEN** the user opens the next launch form with the same selection
- **THEN** the level field starts at the project or persona level, not 1

### Requirement: Levels own network and VCS flags
When a launch has a security level, sbx-tui SHALL refuse it before running the
launcher if the effective shared, profile or custom extra arguments contain
`--no-firewall`, `--egress`, `--extra-domain`, `--allow-github`, `--vcs-access`,
`--pi-ollama`, or a proxy or CA flag. The error SHALL name the setting.

#### Scenario: Profile argument conflicts with the level
- **GIVEN** `security.level = 3` and `[profiles.web] extra_args = ["--extra-domain=example.org"]`
- **WHEN** the user launches profile `web`
- **THEN** sbx-tui refuses the launch naming `profiles.web.extra_args` and suggests `network.extra_domains` with level 2

#### Scenario: Unrelated argument is kept
- **GIVEN** `security.level = 3` and profile extra arguments `--memory=16g`
- **WHEN** the user launches that profile
- **THEN** the launcher arguments contain `--memory=16g`

### Requirement: Corporate network settings come from the user config
When a security level is in effect, `network.internet_proxy`, `network.ca_certs`,
`network.build_proxy`, `network.build_no_proxy`, `network.build_ca_certs` and
`security.egress` SHALL be read only from the global config. A value for them in
the project config SHALL be a configuration error.

#### Scenario: Proxy in project config
- **GIVEN** `security.level = 3` and the project config sets `network.internet_proxy`
- **WHEN** sbx-tui loads the configuration
- **THEN** loading fails with an error naming `network.internet_proxy` and saying to set it in the global config

#### Scenario: Build proxy in user config
- **GIVEN** the global config sets `network.build_proxy = "http://proxy:3128"` and `network.build_ca_certs = ["corp.pem"]`
- **WHEN** a launch runs at any level
- **THEN** the launcher arguments contain `--build-proxy=http://proxy:3128` and `--build-ca-cert=<path of corp.pem>`

### Requirement: Unavailable levels are shown with a reason
A level whose flags the discovered launcher does not support SHALL be shown as
unavailable with the reason, and a launch at that level SHALL be refused. When
discovery failed, levels SHALL NOT be restricted.

#### Scenario: Launcher without VCS access modes
- **GIVEN** the discovered launcher has no `--vcs-access` option
- **WHEN** the user cycles the level field
- **THEN** levels 4 and 5 are marked unavailable because the launcher has no `--vcs-access` option

### Requirement: Previews show the effective isolation
The launch form preview, the workflow picker and dry-run previews SHALL show
each job's level, its egress backend and the network and VCS flags it compiles
to. A Copilot provider SHALL be shown as also allowing GitHub hosts.

#### Scenario: Copilot at level 3
- **WHEN** the user selects a Copilot provider at level 3
- **THEN** the level hint says that Copilot also allows GitHub hosts

### Requirement: Queued jobs without a level
A queued job saved without a level SHALL run at the project default level when
it starts in a project that has a security level, and with the earlier
behaviour otherwise.

#### Scenario: Task queued before the project opted in
- **GIVEN** a task was queued before `security.level = 3` was added
- **WHEN** the task starts
- **THEN** it runs at level 3
