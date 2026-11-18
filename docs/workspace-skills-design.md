# Deploying local skills to bookmark workspaces

Automatically deploy the bundled `sbx-tui-config` skill as sbx-tui opens an
idle bookmark workspace for editing or launches an agent. Users can opt in to
additional skill directories. Each bookmark gets its own copy, so its agent
can read supporting files and run scripts using the same relative paths as in
the source. A sandbox sees those files through the existing `/workspace`
mount. [project-sandbox branch mode](https://github.com/pkrusche/project-sandbox/blob/v0.4.1/docs/usage.md#branch-mode)

## Bundled configuration skill

Use the same [packaged `sbx-tui-config` resource](../src/project_sandbox_tui/skills/sbx-tui-config/SKILL.md)
as `--init`. It covers image selection, config scopes, commands, and project
config trust. Deploy it to `.agents/skills/sbx-tui-config/` and
`.claude/skills/sbx-tui-config/` on first use of new or existing bookmark
workspaces, including projects that have never run `--init`.

Use existing in-tree ignore rules where they cover the targets. Otherwise,
append the exact anchored directory rules for the bundled skill to the shared
Git backend's local `info/exclude`. Preserve existing entries and add each
rule once. This keeps the default deployment out of jj snapshots without
editing the checkout's `.gitignore`. The local excludes apply across
workspaces sharing that backend. Verify the effective ignore result in the
destination before copying, including the local excludes and any higher-priority
in-tree rules. If a target is tracked, blocked by an in-tree negation, or
cannot be safely ignored or written, preserve it, report the reason, and
continue opening the workspace or launching the agent. Preserve existing
valid local copies. [Jujutsu ignore rules](https://docs.jj-vcs.dev/latest/working-copy/#ignored-files)

Add a `[project] deploy_config_skill` setting that defaults to `true`.
Setting it to `false` stops future automatic copies. Keep existing copies and
their exclusions so disabling deployment cannot make a copy eligible for
tracking. Update the packaged skill's installation guidance when implementing
this design so it describes both `--init` and automatic bookmark deployment.

## Additional skills

Add a `workspace_skills` list to the existing global or trusted project TOML.
The default is an empty list. Add entries at the top level of the project
config and review them through `sbx-tui --trust-config`.

```toml
[[workspace_skills]]
source = "~/private-skills/release-review"
targets = [".agents/skills/release-review", ".claude/skills/release-review"]
```

Resolve `~` and absolute paths on the host and relative paths from the checkout
that opened the TUI. Accept one named skill directory per source, with a
regular `SKILL.md` and optional supporting files. The initial target roots are
`.agents/skills/` and `.claude/skills/`. Validate that each target is one direct
child of a supported root and appears only once, including the bundled targets
while automatic deployment is enabled.
See the [existing initialization and trust workflow](configuration.md).

The bookmark must already ignore each full target directory. For the example,
add these path rules to the repository's `.gitignore` and include that change in
the revisions that need the skill.

```gitignore
/.agents/skills/release-review/
/.claude/skills/release-review/
```

Check the effective in-tree ignore rules in the **destination checkout** before
copying. Git's `check-ignore --no-index` can evaluate the destination's rules
against disposable Git metadata even for a non-colocated jj workspace. Isolate
the matcher from host-only excludes so the result also applies inside a
container. Reject any tracked entry at or below a target, even when ignored.
When the sandbox sends the workspace as a build context, the effective
`.dockerignore` also needs matching exclusions to keep the copies out of the
image build. [Jujutsu ignore rules](https://docs.jj-vcs.dev/latest/working-copy/#ignored-files) · [project-sandbox build contexts](https://github.com/pkrusche/project-sandbox/blob/v0.4.1/docs/usage.md#image-builds)

## Workspace preparation

1. Resolve the effective workspace path from the bookmark and sandbox options.
   Use the same path for the busy check, skill deployment and launch. If extra
   arguments override the selected bookmark or workspace root, require the
   user to express the choice in the TUI's typed settings before deploying.
2. Under `jobs.lock`, recheck workspace ownership and busy state. For sandbox
   starts, prepare the directory before spawning `project-sandbox --branch`.
   Create new sandbox-compatible workspaces with `jj workspace add
   --no-colocate` and use the directory basename as the jj registration name
   for new workspaces, including slash-named bookmarks. project-sandbox 0.4.1
   uses that name during teardown. Preserve existing registered workspaces and
   surface a compatibility error where a legacy registration would prevent
   safe sandbox reuse or removal. [project-sandbox workspace lifecycle](https://github.com/pkrusche/project-sandbox/blob/v0.4.1/src/project_sandbox/jj_workspace.py)
3. Validate all sources and destinations. Reject symlinks in skill trees and
   destination ancestors, special files and paths that escape the intended
   roots. Stage each complete directory on the workspace filesystem outside
   the snapshotted tree. Publish it with an atomic rename, preserving executable
   bits and any existing valid local copy. A tracked target or an existing
   destination without a valid skill is a conflict. Report the bookmark and
   path on failure.
   A failed explicit deployment stops launch so the user can fix it and retry.
   Each successfully published directory is complete and retries can reuse it.
4. Use the same preparation for `I` and `:host`, sandbox `i`, and `r` or saved
   actions when the queue dispatches. An idle `o` can deploy a skill before
   opening the editor. `@` uses the checkout's own files. Previews and dry runs
   display the planned destinations without creating a workspace or copying
   data. A dispatch error uses the queue's existing pause-and-resume behavior.

`jobs.lock` coordinates sbx-tui instances. A direct project-sandbox launch
uses a separate workspace lease and can race a TUI launch before its session
record appears. The initial deployment scope is TUI-managed workspace use.
Coordinating with simultaneous shell starts calls for an upstream preparation
step under the same lease that project-sandbox holds through container teardown.
[project-sandbox workspace lease](https://github.com/pkrusche/project-sandbox/blob/v0.4.1/src/project_sandbox/workspace_lease.py)

## Updates and cleanup

Use copy-on-first-use semantics, matching `--init`. To refresh a copy, remove
that skill directory while the workspace is idle and reopen or relaunch it.
Removing a config entry stops future deployments. Workspace cleanup follows
the existing rules for ignored files, and a recreated workspace gets a fresh
copy. A preflight failure can leave a newly created workspace in place for
inspection and retry.

## Verification

- Exercise fresh and kept workspaces, including `feature/x`, a custom workspace
  root, a colocated source repo and `keep_workspace = false`.
- In an uninitialized project, verify that both bundled copies arrive by
  default and stay out of jj snapshots even without a root `.gitignore` while
  an unrelated skill remains trackable. Cover the opt-out, repeated deployment,
  in-tree negation, a tracked collision and preservation of a local edit.
- Check that a nested helper arrives with its executable bit, a local edit
  survives redeployment, and a failed copy can be retried. Cover missing ignore
  rules, tracked collisions, symlinks, busy sessions and dry runs.
- Extend the [real Bash smoke test](development.md#real-container-smoke-test) to
  read and run an ignored skill fixture from `/workspace`, then use `jj file
  list` to verify that the skill is absent from the resulting bookmark while
  the normal session change is captured.
