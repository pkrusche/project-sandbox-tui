# Codebase Review Findings

Completed review of `src/`, `tests/`, `docs/`, `skills/`, and `scripts/`. The checked entries preserve the original findings and their line references.

---

## 1. Logic Errors & Bugs

### High

- [x] **`sbx-tui --print-config` crashes with `TypeError` when any workflow file has a problem** (`src/project_sandbox_tui/cli.py:527-532`)
  - `_print_config` converts `Config` with `dataclasses.asdict(cfg)` and deletes `profile_sources` and `warnings`, but leaves `workflow_problems: list[tuple[Path, str]]` (and internal `persona_config`). When `workflow_problems` is non-empty, `json.dumps(data, indent=2)` raises `TypeError: Object of type PosixPath is not JSON serializable`.
  - **Fix:** Delete internal fields (`workflow_problems`, `persona_config`) or stringify paths before dumping.

- [x] **Raw ANSI escape sequences and control characters `0x15`–`0x1f`, `0x7f` leak through `LiveLog`** (`src/project_sandbox_tui/logview.py:27,34`)
  - `_sub_escape = re.compile("[\u0000-\u0014]").sub` stops at `0x14` (DC4), missing `0x15`–`0x1f` (including `\x1b` ESC), `0x7f` (DEL), and C1 controls (`\x80`–`\x9f`).
  - Raw ANSI sequences like `\x1b[31mred\x1b[0m` pass straight to the terminal while `Rich.Text.cell_len` counts `[31m`/`[0m` as visible cells (e.g., 12 instead of 5), corrupting terminal styling, wrapping, horizontal scroll widths, and line layout.
  - **Fix:** Strip or replace `[\x00-\x1f\x7f-\x9f]` (preserving `\t` via `expandtabs()`), or explicitly strip ANSI CSI/OSC sequences before rendering.

- [x] **Feedback successor actions with `persona` drop inherited `level`** (`src/project_sandbox_tui/queue.py:827-835`)
  - In `TaskQueue._successor_spec()`, the `action.profile` branch passes `level=level` into `Selection`, while the `action.persona` branch constructs `Selection(StepChoice("persona", persona=action.persona), parent.provider)` without `level=level`.
  - When `action.level` is unset and the parent had a user-chosen level override, `action.profile` preserves `parent.level` while `action.persona` silently drops it (and falls back to persona/project default). Also, when `parent.provider == ""` (parent was profile/custom), `Selection` passes `""`, relying on `_resolve_persona`'s `remembered_provider` fallback without availability filtering.

- [x] **Dead test in `tests/test_app.py`: `test_modifier_hints` never executes its coroutine** (`tests/test_app.py:1653-1679`)
  - `test_modifier_hints` defines `async def scenario(...)` and never calls `run_app(repo, fake_cfg(), scenario)`. The test always passes without executing a single line.
  - Worse, its assertions have already rotted (`"ctrl+s / ctrl+enter launch"` fails against the current `submit_label="run / queue"`).

### Medium

- [x] **`_refresh_once` passes post-listing timestamp as `listed_at` to `_sync_graph`** (`src/project_sandbox_tui/app.py:750-756`)
  - Line 714 properly records `listed_at = time.time()` *before* calling `backend.list_branches(...)`, matching `opgraph.py`'s invariant (`_parse_ts(job.ended_at) > listed_at` rejects commits from listings started before the run ended).
  - The second listing before queue dispatch (line 750) calls `await self._sync_graph(time.time())` *after* `list_branches` finishes. A job ending while `list_branches` runs can have its commit misrecorded as the pre-completion tip.

- [x] **`OpLogScreen.action_open_log` misclassifies older `bash` runs as Markdown + JSONL** (`src/project_sandbox_tui/screens/oplog.py:274`, `src/project_sandbox_tui/opgraph.py:345`, `src/project_sandbox_tui/screens/log.py:175,188`)
  - `opgraph.add_run` stores `node.agent = job.spec.label()` (e.g. `"bash/L3"` or `"bash/my-model/gateway/L3"`).
  - For archived runs, `OpLogScreen.action_open_log` reconstructs `LaunchSpec(node.bookmark, node.agent)` using that label as `spec.agent`. `LogScreen._markdown_file` and `_raw_log` check `source.spec.agent != "bash"`, which evaluates to `True` for `"bash/L3"`, rendering plain bash stdout as Markdown and treating plain bash logs as JSONL records.
  - Also, `action_open_log` (line 264) only checks `if not node.log:`, refusing to open runs that have `node.stdout` without `node.log`.

- [x] **Stuck background jobs and tasks cannot be forgotten via `x` on `PermissionError` or `ProcessLookupError`** (`src/project_sandbox_tui/jobs.py:1035-1042`, `src/project_sandbox_tui/tasks.py:457-470`)
  - In `JobManager._cancel`, when `os.killpg` raises `PermissionError` (e.g. PID reused by another user after a crash) or `ProcessLookupError` (group leader gone while record stays running), `self._stop_stage[branch]` is never incremented, so pressing `x` three times never reaches `_forget(branch)`. Nothing in the TUI calls `JobManager.clear(..., force=True)`.
  - Similarly, `TaskManager._signal` never advances stage or forgets a stuck running task on `PermissionError` / `ProcessLookupError`.

- [x] **`opgraph.State.adopt` ignores `b.timestamp` when inferring parents and does not deduplicate `root.parents`** (`src/project_sandbox_tui/opgraph.py:405-413`)
  - Line 409 sorts `unknown` by `(b.name != main, b.timestamp, b.name)`, then assigns every adopted bookmark the exact same `at=now`. `infer_parents` ranks bookmarks on the same commit by `(name != main, lineage.at, name)`, so `b.timestamp` is lost and ties break alphabetically instead of chronologically.
  - Line 413 sets `root.parents = [h.id for p in parents if (h := self.head(p)) is not None]` without deduplicating (`dict.fromkeys`) or guarding `p != branch.name` (unlike `add_lineage` and `add_creation`). Duplicate parents produce bogus parallel lanes/forks in `opgraph.draw()`.

- [x] **`opgraph.State.bookmark_parents` memoization can be poisoned on cycles** (`src/project_sandbox_tui/opgraph.py:257-267`)
  - `collapse(lineage, seen)` stores `memo[lineage] = found` even when parents were skipped due to `parent in seen`. If a corrupted file introduces a cycle, caching a `seen`-dependent partial result poisons subsequent lookups from other paths.

- [x] **`LiveLog.markdown_blocks` misparses inline backticks as unclosed code fences and allows 4+-space closing fences** (`src/project_sandbox_tui/logview.py:29,47-50,69-70`)
  - `_fence = re.compile(r" {0,3}(`{3,}|~{3,})")` matches lines starting with inline code such as ```` ```foo``` ```` as an opening fence. Per CommonMark, a backtick fence's info string cannot contain backticks. Once triggered, `fence` stays set and swallows the rest of the log into one unclosed code block.
  - Closing fence checks use `line.strip()`, allowing 4+ leading spaces (which are indented code content, not a closing fence).

- [x] **Empty diff on a workflow `human-review` stage auto-approves without human review** (`src/project_sandbox_tui/queue.py:972-974`)
  - `keys = backend.review_file_keys(base, target); if not keys <= SeenFiles.for_repo(jobs.repo).seen(): return`.
  - When `keys` is empty (e.g. the agent made no changes or reverted them), `set() <= seen` is vacuously `True`, so `human-review` immediately approves and runs any following `host:NAME` command without a user ever opening the review.

- [x] **`LaunchScreen.action_launch` attempts to focus hidden `AgentControls` widgets on invalid profile/persona model** (`src/project_sandbox_tui/screens/launch.py:737-741`)
  - Line 731 guards `#custom-model`/`#model` focus with `if not self._selected_profile() and not self._persona_mode():`, but lines 737–741 (`if any(ch.isspace() for ch in spec.model)`) omit that guard and focus hidden widgets when `setup` is a profile or persona.

- [x] **`WorkflowProfilesScreen` misses background capability refreshes and ignores `remembered_provider`** (`src/project_sandbox_tui/app.py:3310-3316`, `src/project_sandbox_tui/screens/workflow.py:112-116,244-247`)
  - `_refresh_capabilities()` notifies `AgentControls` and `LaunchScreen`, but never refreshes an open `WorkflowProfilesScreen`.
  - `WorkflowProfilesScreen.__init__` sets `self.default_provider` to `catalogue.ROWS[0].id` when no stage starts as a persona, ignoring `remembered_provider(cfg, last, available)` / `catalogue.default_provider`.
  - `action_start` focuses `#stage-provider-{index}` whenever `self._resolved(index).problem` is truthy, even if that stage is not in persona mode (widget has `display = False`).

- [x] **Bundled skill check fails on native (non-Git) jj repositories even when `.gitignore` already ignores the targets** (`src/project_sandbox_tui/workspace_skills.py:503-508`)
  - `_check_ignored` checks `git_dir = self.backend.git_dir()` and immediately fails all bundled targets if `git_dir is None`, without first checking whether in-tree `.gitignore` rules already ignore them.

- [x] **Leaked `runs/<run_id>` directories if `build_argv` or `save` fails during `interactive` / `record_external`** (`src/project_sandbox_tui/jobs.py:649-670,756-785`)
  - Unlike `start_background` (which cleans up `feedback_dir` on launch/save errors), `interactive` and `record_external` do not remove `feedback_dir` if `build_argv` or `self.save(job)` raises.

- [x] **Relative `sandbox.command` misses capability cache in `commit_summary.resolve_launch`** (`src/project_sandbox_tui/commit_summary.py:139`)
  - `resolve_launch` resolves relative paths in `command[0]` to absolute paths before calling `capabilities.cached(isolated.sandbox_command(), repo)`, whereas `app.py` caches under the un-resolved `cfg.sandbox_command()`, causing a cache miss and silently falling back to unrestricted `capabilities.fallback()`.

- [x] **Queue history browsing (`ctrl+n` / `down`) and `QueueScreen` error handling edge cases**
  - `PromptArea.action_history(-1)` (`src/project_sandbox_tui/widgets.py:218-238`) and `CommandScreen.action_history(-1)` (`src/project_sandbox_tui/screens/command.py:140-155`): pressing `ctrl+n`/`down` before starting a history walk populates `_browse`, computes `index = -2 < 0`, calls `replace_text(_browse_origin)` / `_set(_browse_origin)` (moving cursor to end of text instead of ringing bell or no-oping). In `CommandScreen`, when `_browse` is empty it also leaves `self._browse = []` instead of `None`.
  - `QueueScreen.action_d_prefix` (`src/project_sandbox_tui/screens/queue.py:251-258`): clears `self.marked` and `_visual_anchor` even when `self.queue.delete(...)` raises an error (e.g. attempting to delete a running task), and calls `self.render_queue()` twice (`self.change` already calls it).

---

## 2. Drawing, Layout & Off-by-One Visual Errors

- [x] **`ConfirmScreen`, `MessageScreen`, and `StaleWorkspaceScreen` clip their `.hint` row off the bottom of short terminals** (`src/project_sandbox_tui/screens/dialogs.py:195,227,260`)
  - Unlike `HelpScreen` and `CleanupScreen` (which use `fit_body` on resize), these modals hardcode `max-height: 60vh`. In a short terminal (verified at `80×10`: `hint.region.bottom == 11 > screen.region.bottom == 10`, and also at narrow widths where `.hint` wraps to 2+ lines), the bottom hint line is pushed off-screen.
  - **Fix:** Use `fit_body` in `ScrollModal.on_resize` (inherited by all scrolling modals) instead of a fixed `60vh`.

- [x] **`LiveLog` fails to highlight `cursor_row` on any non-first line of a Markdown block, and `_prune` does not adjust `cursor_row`** (`src/project_sandbox_tui/logview.py:286-315,340-360`, `src/project_sandbox_tui/screens/log.py:306-310`)
  - `LiveLog._row(y)` returns `(self._starts[block], strip)`, and `render_line` checks `self.cursor_row == line`. In Markdown mode, any `cursor_row` other than the block's first logical line never matches and gets no highlight (verified: `cursor_row=1` in a 3-line block produces no highlight).
  - `LiveLog._prune()` drops leading logical lines without updating `self.cursor_row`. `LogScreen._append()` attempts to compensate externally by pre-subtracting `max(0, len(log.lines) + endings - MAX_LINES)` before `log.write()`, which is off by 1 when `log.lines` is initially empty (since `write` first appends `""`) and only holds for plain-text exact-line pruning, not Markdown block pruning.
  - Also, `LogScreen._append` splits `\r\n` across 256 KiB chunk boundaries into two separate line endings, inserting a spurious blank line.

- [x] **`OpLogScreen` resets horizontal and vertical scroll every 1 second** (`src/project_sandbox_tui/screens/oplog.py:130-149`)
  - `_render_rows` runs every 1.0s; for any operation < 60s old, `human_age()` changes every second so `rows != self._shown`.
  - It calls `table.clear()` + `table.move_cursor(...)` without `restore_table_view()` and without resetting `column.content_width`. Every second, `scroll_x` snaps back to 0 and `scroll_y` jumps to snap the cursor row.

- [x] **`QueueScreen` resets horizontal and vertical scroll on every refresh change and visual-mode cursor move** (`src/project_sandbox_tui/screens/queue.py:176-190,226-238`)
  - `render_queue` calls `self.table.clear()` and `self.table.move_cursor(...)` without `restore_table_view()`. In visual mode (`v`), every `j`/`k` calls `render_queue()`, rebuilding the whole table and resetting `scroll_x` to 0.

- [x] **`CommitReviewScreen.action_commit` retains stale column widths and stale `priority_files` during rebuild** (`src/project_sandbox_tui/screens/review.py:337-350`)
  - `action_commit` calls `table.clear()` without resetting `column.content_width`, so a wide filename or `"Priority  "` prefix from an earlier commit leaves permanent trailing horizontal space on subsequent commits.
  - It also populates rows using the previous commit's `self.priority_files` before `self._title()` calls `refresh_assessment()`.

- [x] **`MainScreen.on_key` causes layout jump on modifier keys and does disk I/O on every keypress** (`src/project_sandbox_tui/app.py:499-525`)
  - Pressing a modifier key in an extended-key terminal replaces the multi-line `#cheatsheet` with a 1-line `KeyHints(hint)`, making the `DataTable` above jump ~10 lines down and snap back up on the next keypress.
  - On *every* non-modifier keypress (`j`, `k`, typing filter text, etc.), the `else:` branch calls `self.cheatsheet()` -> `app._host_argv(WORKING_COPY)` -> `LastUsed.load(self.repo)` (reading and parsing `last_used.json` from disk on every keypress) and re-renders both `#cheatsheet` and `#footer`, even when no modifier hint was shown.

- [x] **`OpLogScreen` keybindings hijack `j`/`k`/`^d`/`^u`/`gg`/`G` while `#oplog-details-pane` is focused** (`src/project_sandbox_tui/screens/oplog.py:49-59,174-190`)
  - Pressing `tab` focuses `#oplog-details-pane` (`VerticalScroll`), but `j`/`k`/`ctrl+d`/`ctrl+u`/`g`/`G` remain bound to `OpLogScreen`'s table cursor actions instead of scrolling the focused details pane.

- [x] **2-column horizontal misalignment between `CycleField` values and `.field` `Input`s** (`src/project_sandbox_tui/widgets.py:133-140`, `src/project_sandbox_tui/screens/agent.py:44-45`, `src/project_sandbox_tui/screens/launch.py:72-73`, `src/project_sandbox_tui/screens/profiles.py:40-41`)
  - `CycleField` renders `padding-left: 1` + 9-char label + `"‹ "` (value starts at column 12), whereas `.field-label` has `width: 10` so adjacent `Input` values start at column 10 (under `‹`).
  - Also, `CycleField.render` uses `f"{self.label:<9}"` without a guaranteed separator space; any label of 9+ chars would collide with `"‹ "` (currently the longest is `"provider"`, 8 chars).

- [x] **`DiffReviewScreen` / `CommitReviewScreen` omit `animate=False` on `move_cursor`** (`src/project_sandbox_tui/screens/review.py:121,207,348`)
  - Unlike every other `DataTable` screen (`app.py`, `oplog.py`, `tasks.py`, `queue.py`), cursor movement animates on `j`/`k`/`gg`/`G`/`[`/`]`.

- [x] **`HistorySearchScreen._preview` truncates by `len()` instead of terminal cell width** (`src/project_sandbox_tui/screens/history.py:19-27`)
  - Uses `len(first) > PREVIEW_CHARS` without `.expandtabs()` or `cell_len` (unlike `widgets.table_preview`), so tabs and wide Unicode characters exceed the intended width.

---

## 3. Inconsistencies

- [x] **Contradictory `--vcs-access` advice in `capabilities.setup_summary()`** (`src/project_sandbox_tui/capabilities.py:719-721`)
  - Tells the setup agent `"VCS access modes for profile extra_args: --vcs-access=..."` right above `"Security levels the launcher supports..."`, directly contradicting `security.py` (which forbids `--vcs-access` in any `extra_args` whenever a security level is active).

- [x] **Stale `MIN_PROJECT_SANDBOX = (0, 2, 2)` in `doctor.py` vs `>=0.4.1` everywhere else** (`src/project_sandbox_tui/doctor.py:46`, `pyproject.toml:28`, `README.md:54`, `scripts/e2e-test.sh:48`)
  - `pyproject.toml` and `README.md` require `project-sandbox>=0.4.1`, and `scripts/e2e-test.sh` reads `MIN_PROJECT_SANDBOX` from `doctor.py` for preflight checks — so `doctor` and `e2e-test.sh` accept versions as old as `0.2.2` that lack `--egress=sidecar`, `--vcs-access`, and `--mount`.

- [x] **`doctor.check_agent_selections` ignores persona-based actions and persona gateway usage** (`src/project_sandbox_tui/doctor.py:575-600,670-677`)
  - `_selections()` includes `actions.*` only when `action.agent` is set (missing actions with only `action.model` or `action.persona`).
  - Line 670 reports `"network.agent_proxy is configured but no default or profile uses it"` even when `catalogue.default_provider` or `commit_review.provider` is a `gateway-*` row.

- [x] **`commands.check_name` error message omits `/`** (`src/project_sandbox_tui/commands.py:147-153`)
  - Allows one `/` (for namespaced workflow commands/actions), but the error message says `"contain only letters, digits, _ and -"`.

- [x] **Workflow host tasks display invalid `:host:<name>` command syntax in UI** (`src/project_sandbox_tui/tasks.py:77-82`, `src/project_sandbox_tui/app.py:217-226`, `src/project_sandbox_tui/screens/tasks.py:124`)
  - Workflow host steps store `line = "host:<name>"`, so `TasksScreen` renders `:host:check` and the bookmark status column renders `:host:check ✓`, even though `:host:check` is not a valid command line.

- [x] **Pruning `TaskManager` records deletes log files still referenced by `TaskQueue` history** (`src/project_sandbox_tui/tasks.py:196-199`)
  - Once `TaskQueue._acknowledge_hosts()` marks a workflow host task as `notified=True`, `TaskManager._prune()` deletes its `.task.out` file after 10 tasks on that bookmark, breaking `l`/`enter` log viewing for that completed workflow step in `QueueScreen`.

- [x] **Minor API/code inconsistencies**
  - `cli.py:36-38`: `build_parser()` epilog lists `config` and `doctor` subcommands, omitting `workflow`. `--trust-config` help mentions only `.project-sandbox-tui.toml`, omitting workflow files.
  - `app.py:1364`: `action_diff` passes `{b.name for b in self.branches}` (including `"@"`) to `main_branch(...)`, whereas `self.main_name()` (line 977) excludes `working_copy`.
  - `screens/profiles.py:32` and `setup_wizard.py:94`: annotated as `ModalScreen[ProfileEdit]` and `App[Interview]`, though both dismiss/exit with `None` on cancel (`| None` missing).
  - `setup_wizard.py:230-232`: calls `discover(repo)` (which calls `load(repo)`), then calls `load(repo)` a second time immediately after.
  - `capabilities.py:380` and `models.py:295`: use `path.parent.mkdir(parents=True, exist_ok=True)` instead of `config.private_dir(path.parent)`, inconsistent with the `0700` state-directory guarantee.
  - `initialization.py:48`: catches failures from `install_feedback` (which writes `.gitignore`, feedback skills, config skills, and `AGENTS.md`) under the message `"could not install configuration skill"`.
  - `screens/log.py:190,213-215`: compares `path == Path(source.log_path)` without guarding `bool(source.log_path)` (unlike `_markdown_file` on line 177 which checks `bool(source.stdout_path)`).
  - `screens/queue.py:117`: `selected_id()` does `str(key.value)` without checking `key.value is not None` (unlike `oplog.py` and `tasks.py`).
  - `config.py:1080`: `_merge_workflow_files` docstring states invalid workflow files are isolated in `cfg.workflow_problems`, but an inline `[workflows.<name>]` clash raises `ConfigError` outside the `try` block (aborting `load()`). Worth clarifying the docstring or isolating consistently.
  - `app.py:3129-3130`: `_reload_config` unconditionally appends `--dry-run` even if already present in `cfg.sandbox.extra_args`.

---

## 4. Documentation Issues & Overdocumentation

### Inaccuracies, Omissions & Drift

- [x] **`src/project_sandbox_tui/skills/sbx-tui-config/SKILL.md:272-277`**
  - Line 274 says *"In the third row, the launch forms select the remembered profile..."* — **off by one**: the table has 4 rows (`persona`, `profile`, `agent/model/effort`, `neither`); it refers to the **fourth** row (`neither`).
  - Lines 272–277 contradict themselves two sentences apart: line 272 says *"Prefer personas for unattended work"*, while line 275 (left over from before personas) says *"Prefer an explicit `profile` for unattended work"*.
  - Line 85 recommends `--vcs-access=read-only` in profile `extra_args`, contradicting the next section (line 117) which forbids `--vcs-access` in `extra_args` when a security level is set.
  - Frontmatter description mentions only profiles, omitting personas, providers, and security levels.

- [x] **`README.md`**
  - `## Documentation` index (lines 89–99) omits `docs/feedback.md` even though the introduction links to it.
  - Getting started (lines 81–84) only mentions `:config-profiles` and `custom`, omitting the default `persona` setup and `security.level`.

- [x] **`docs/usage.md`**
  - CLI section (lines 4–19) omits `sbx-tui workflow import` and omits `workflow` from the subcommand name-collision note (`./config`, `./doctor`).
  - Line 21 documents `project-sandbox (minimum 0.2.2)`, contradicting `README.md` (`>= 0.4.1`) and `pyproject.toml`.
  - Line 148 status glyph list omits `⏱` (timeout) and `◆` (external).
  - `Command line` table (lines 186–201) **omits `:workflow NAME [TOPIC]`**.
  - `Prompt queue` section (line 612) omits the `×` flow-column glyph for terminated workflows.

- [x] **`docs/configuration.md`**
  - Line 218 (`Agent profiles` step 3) gives `--extra-domain example.org --memory '16g'` as an example for profile CLI args, which is **refused** whenever `[security] level` is enabled.
  - Line 356 contains a changelog/migration paragraph (*"Before this change, `network.agent_proxy` was added to every launch..."*) inside evergreen reference documentation.
  - Line 595 lists built-in commands that cannot be redefined, omitting `init`, `update-models`, `reorganise`, and `reorganize`.
  - `State directory` table (lines 670–690) omits `capabilities.json`, `trusted-workflows/`, `runs/<run_id>/` (only in prose later), and `guidance.lock`.

- [x] **`docs/merging.md:13-16` & `docs/sessions.md:85,155`**
  - Both still describe launch forms as offering only "a saved agent profile or **custom**", omitting **persona** setup (provider + persona) and per-job **security level**. `docs/sessions.md:155` also omits `fast`, `gateway`, `persona`, `provider`, and `level` from what queued tasks snapshot.

- [x] **`docs/architecture.md:8-30`**
  - Modules table omits `initialization.py`, `setup_wizard.py`, `feedback.py`, `feedback_host.py`, `feedback_transport.py`, `processes.py`, and `screens/selection.py`, while inconsistently listing a few `screens/*.py` files individually and lumping the rest under `screens/`.

- [x] **Code docstrings & templates**
  - `src/project_sandbox_tui/config.py:1908` (`PROJECT_TEMPLATE`): comment still says `# launch forms offer profiles and custom` (omits personas).
  - `src/project_sandbox_tui/models.py:3-10` module docstring: lists only 4 suggestion sources, omitting catalogue models of provider rows (which `docs/models.md` lists as source #2).

### Overdocumentation & Structural Bloat

- [x] **Heavy cross-document duplication (~3,800 lines of docs + ~380 lines of skills):**
  - **Security levels (1–5), all 12 managed flags, and global-only corporate network rules** are documented in full in *three* separate files (`docs/configuration.md`, `skills/sbx-tui-config/SKILL.md`, `skills/sbx-tui-safety/SKILL.md`) and summarized again in `docs/usage.md` and `docs/feedback.md`.
  - **Personas table and provider rows** are duplicated across `docs/models.md`, `docs/configuration.md`, and `skills/sbx-tui-config/SKILL.md`.
  - **Launch setup selection & precedence rules** (persona vs profile vs custom vs remembered choices) are re-explained in full in `docs/configuration.md` (3 separate sections!), `docs/usage.md`, `docs/feedback.md`, and `skills/sbx-tui-config/SKILL.md`.
  - **Project image detection table** is duplicated verbatim in `docs/configuration.md` and `skills/sbx-tui-config/SKILL.md`.
  - **Shell command modes (`:`, `:&`, `:*`, env vars, marks)** are repeated across 4 subsections of `docs/usage.md`, `docs/configuration.md`, and the 124-line walkthrough in `docs/command-workflow.md`.
  - **`docs/architecture.md`** includes a 21-row per-keybinding lock table and prose detailing internal method names (`_history_lock`, `JobManager._busy_reason`, `queue-transaction.json`) that duplicates `docs/configuration.md#state-directory` and `docs/feedback.md` and creates high maintenance drift risk.
  - **`docs/development.md`** spends ~85 lines enumerating every internal assertion of `test_container_smoke.py` and ~40 lines narrating every step of `scripts/make-release.sh` (which already documents itself via `--help`).
  - **Recommendation:** Pick a single canonical home for each reference table/policy (e.g., `docs/models.md` for catalogue/personas, `docs/configuration.md` for config/levels/state, `docs/usage.md` for keys/commands) and link instead of restating. Keep `architecture.md` focused on high-level invariants (lock hierarchy, not a per-key matrix) and `development.md` on how to run checks/releases rather than line-by-line script narration.

---

## 5. Test Suite Issues: Dead, Shallow & Redundant Tests

### Dead / Broken Assertions

- [x] **`tests/test_app.py:1653` (`test_modifier_hints`)**: Defines `async def scenario` and never calls `run_app(...)`. Completely dead test with stale expectations (`"ctrl+s / ctrl+enter launch"` instead of `"run / queue"`).
- [x] **`tests/test_setup_wizard.py:164` (`test_summary_lists_providers_levels_and_personas`)**: Asserts `assert "need `security.egress" not in text`, which has a stray backtick — the actual message is `'levels 3 to 5 need security.egress = "firewall"'` (no backtick), so this negative assertion is vacuously true even if the warning appears.

### Shallow Tests

- [x] **`tests/test_keys.py:49-85` (`test_docs_list_every_key`)**:
  - Extracts every backticked token from the entire 660-line `docs/usage.md` rather than each screen's key table, so almost any single-letter binding passes by accident if that letter appears in backticks anywhere in `usage.md`.
  - Omits `DiffReviewScreen`, `CommitReviewScreen`, `LaunchScreen`, `ProfilesScreen`, `WorkflowProfilesScreen`, `CommandScreen`, and `HistorySearchScreen`, and never checks `commands.BUILTINS` against `docs/usage.md` (which allowed `:workflow` to go undocumented).
- [x] **`tests/test_catalogue.py:28-94,135-158`**:
  - `test_provider_rows_in_order`, `test_every_built_in_cell`, `test_row_rules`, and `test_built_in_personas` copy the static `ROWS`, `VENDOR_TIERS`, and `BUILTIN_PERSONAS` tables verbatim into test assertions — pure change-detector tests that test constant declarations rather than behavior.
- [x] **`tests/test_security.py:331-351` (`test_fake_launcher_records_isolation_flags`)**:
  - Primarily tests the test fake (`tests/fake_project_sandbox.py`) rather than production code (`build_argv` flag compilation is already covered directly by `test_levels_compile_to_exactly_their_flags` and `test_build_proxy_from_user_config_at_any_level`).
- [x] **`tests/test_setup_wizard.py:175-186` (`test_interview_prompt_carries_the_summary`)**:
  - Asserts exact static boilerplate strings inside an f-string (including an embedded newline `"propose a project\nsecurity.level"`), making it brittle without verifying behavior.
- [x] **`tests/test_docs_examples.py:79-85` (`test_settings_reference_example`)**:
  - Pops `data["security"]["egress"]` with comment `# global-only`, even though `from_dict()` doesn't check global-only restrictions (only `merge_config` does); meanwhile several TOML blocks in `docs/configuration.md` (`Agent profiles`, `Agent gateway`, `Feedback settings`) and `skills/sbx-tui-config/SKILL.md` are not tested at all.

### Redundant Tests

- [x] **`tests/test_catalogue.py` internal overlap**:
  - `test_copilot_dotted_ids_never_reach_claude_code` (line 72) duplicates `test_every_built_in_cell` + `test_copilot_uses_its_own_ids`.
  - `test_fast_mode_only_on_codex_and_claude_code` (line 286) duplicates `test_row_rules` + `test_pi_gateway_row` + `test_checker_on_claude_code`.
  - Ten separate single-case resolution tests (`test_reviewer_on_codex` through `test_planner_preview_on_gateway_anthropic`, lines 208–285) can be collapsed into 1–2 parametrized tests.
- [x] **`tests/test_security.py` internal overlap**:
  - `test_python_project_at_level_two` (line 158) overlaps `test_levels_compile_to_exactly_their_flags` + `test_level_two_sources_per_build`.
  - `test_older_help_without_egress` (line 272) is a strict subset of `test_level_problems` (line 301).
- [x] **Cross-file duplication**:
  - `LaunchSpec.from_dict` legacy defaults and `to_dict` round-trips are re-tested across 4 files: `test_sandbox.py` (`test_old_launch_spec_defaults_fast_mode_to_off`, `test_old_records_without_gateway_load`), `test_security.py` (`test_launch_spec_round_trips_selection_fields`), and `test_profiles.py` (`test_profile_argv_is_a_snapshot`).
  - `test_sandbox.py::test_claude_with_gateway_configured_gets_no_gateway_flags` duplicates `test_sandbox.py::test_interactive_argv` and `test_catalogue.py::test_agent_follows_from_the_row`.
  - `test_launch_choices.py::test_persona_mode_without_profiles` re-asserts the full persona/provider constant lists from `test_catalogue.py`; `test_planner_hint_on_gateway_anthropic` duplicates `test_catalogue.py` inside a full Textual app; `test_codex_without_host_configuration_is_unavailable` overlaps `test_unavailable_agent_is_marked_with_reason`.
  - `test_setup_wizard.py::test_fallback_wording_when_launcher_cannot_be_probed` duplicates `test_capabilities.py::test_setup_summary_says_when_it_is_a_fallback` while booting a full `SetupDialog` app.

### Notable Test Coverage Gaps (for bugs above)

- [x] `sbx-tui --print-config` when `cfg.workflow_problems` is non-empty.
- [x] `LiveLog` handling of ANSI escape sequences (`\x1b[...]`), `cursor_row` inside multi-line Markdown blocks, `cursor_row` across `_prune()`, and inline triple-backtick code in `markdown_blocks`.
- [x] `ConfirmScreen` / `MessageScreen` / `StaleWorkspaceScreen` layout in short terminals (`80×10`, `40×14`).
- [x] `OpLogScreen` and `QueueScreen` scroll preservation across periodic refresh / visual-mode navigation, and `OpLogScreen.action_open_log` on archived `bash/L3` runs.
- [x] `TaskQueue._successor_spec` level propagation for persona actions, and `human-review` behavior on empty diffs.
- [x] `JobManager._cancel` / `TaskManager._signal` recovery when `os.killpg` raises `PermissionError` or `ProcessLookupError`.
