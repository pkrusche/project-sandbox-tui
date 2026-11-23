# Spec Delta

## Purpose

Lets a user copy a single workflow file from another repository, by HTTPS URL
or local path, into the current project, reviewed and trusted, with a record of
where it came from.

## ADDED Requirements

### Requirement: Import sources
`sbx-tui workflow import SOURCE [--name NAME]` SHALL accept an HTTPS URL or a
local file path. A `https://github.com/OWNER/REPO/blob/REF/PATH` URL SHALL be
fetched from its raw form. Other URL schemes SHALL be rejected. The fetched
content SHALL be one workflow file; the command SHALL NOT clone or read other
files from the source.

#### Scenario: GitHub blob URL
- **WHEN** the user runs `sbx-tui workflow import https://github.com/org/tools/blob/main/.project-sandbox-tui/workflows/spec.toml`
- **THEN** sbx-tui fetches `https://raw.githubusercontent.com/org/tools/main/.project-sandbox-tui/workflows/spec.toml`

#### Scenario: Plain HTTP refused
- **WHEN** the user runs `sbx-tui workflow import http://example.com/spec.toml`
- **THEN** the command fails without fetching, stating that only HTTPS URLs and local files are accepted

#### Scenario: Local file
- **WHEN** the user runs `sbx-tui workflow import ../other-repo/.project-sandbox-tui/workflows/spec.toml`
- **THEN** sbx-tui reads that file and proposes workflow `spec`

### Requirement: Import name
The imported workflow's name SHALL be `--name NAME` when given, otherwise the
source file's stem. A name that does not follow the `[commands]` naming rules
SHALL be rejected before anything is written.

#### Scenario: Renamed on import
- **WHEN** the user runs `sbx-tui workflow import ./spec.toml --name ext-spec`
- **THEN** the file is written as `.project-sandbox-tui/workflows/ext-spec.toml` and its actions load as `ext-spec/...`

### Requirement: Import validation
Before showing or writing anything, the import SHALL validate the content as a
workflow file under the imported name, including the persona-only rule and the
self-contained step references. An invalid file SHALL be rejected with the
reason and nothing SHALL be written.

#### Scenario: Import of a file with a profile
- **GIVEN** the source file has an action with `profile = "deep"`
- **WHEN** the user imports it
- **THEN** the import fails with the persona-only reason and no file is written

### Requirement: Review and confirmation
The import SHALL generate the final workflow text, including the complete
`[source]` table with its timestamp and checksum, before asking for
confirmation. It SHALL show that full text for a new file or the diff from the
current file to that text for an overwrite. On confirmation it SHALL use the
same reviewed text for the workflow file and its trusted copy. On refusal it
SHALL change nothing.

#### Scenario: Confirmed import
- **WHEN** the user imports a valid `spec.toml` and confirms
- **THEN** `.project-sandbox-tui/workflows/spec.toml` and its trusted copy both contain exactly the shown text, including `[source]`

#### Scenario: Declined import
- **WHEN** the user imports a valid file and declines
- **THEN** no workflow file or trusted copy is created or changed

### Requirement: Name conflicts
When the target name is already used by a workflow file or an inline
`[workflows.NAME]`, the import SHALL say which, and SHALL offer to overwrite (file
only) or to choose another name. Overwriting an inline workflow SHALL NOT be
offered. In a non-interactive run, an existing name SHALL fail the import unless
`--name` picks a free name.

#### Scenario: Overwrite existing file
- **GIVEN** a trusted `spec.toml` exists
- **WHEN** the user imports another `spec.toml` and chooses overwrite
- **THEN** sbx-tui shows the diff from the existing file to the final text, including any changes to `[source]`, and on confirmation writes the reviewed result to the workflow file and its trusted copy

#### Scenario: Conflict with inline workflow
- **GIVEN** the project config has `[workflows.spec]`
- **WHEN** the user imports `spec.toml`
- **THEN** sbx-tui reports the inline workflow `spec` and asks for another name instead of offering to overwrite

### Requirement: Provenance record
Before review, the import SHALL generate a `[source]` table with the URL or
the absolute local path, the import time in UTC ISO-8601, and the SHA-256 of the
workflow body serialized by `tomlkit` after removing any `[source]` table from
the fetched content. `sbx-tui doctor` SHALL report, for each imported workflow,
whether its content still matches the recorded checksum.

#### Scenario: Provenance written
- **WHEN** the user imports `https://example.com/spec.toml` and confirms
- **THEN** the preview shows `[source]` with `url = "https://example.com/spec.toml"`, the import time and the `sha256` of the serialized workflow body, and the same values are present in the written file and trusted copy

#### Scenario: Local edits detected
- **GIVEN** an imported `spec.toml` whose `check` prompt was edited afterwards
- **WHEN** the user runs `sbx-tui doctor`
- **THEN** doctor reports that workflow `spec` differs from its imported source

### Requirement: Re-import updates a workflow
Importing again from the URL or path recorded in a workflow's `[source]` SHALL
follow the overwrite flow, showing the diff against the current file.

#### Scenario: Update from source
- **GIVEN** `spec.toml` was imported from `https://example.com/spec.toml`
- **WHEN** the user imports the same URL again and chooses overwrite
- **THEN** sbx-tui shows the diff between the current file and the final text with regenerated provenance before asking to write it
