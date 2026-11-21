---
name: sbx-tui-safety
description: Help a project owner choose or review sbx-tui safety settings for sandbox profiles, host commands and unattended workflows. Use during setup or when a change affects repository access, networking, mounts, credentials or automatic follow-up actions.
---

# Choose access for an sbx-tui task

Start with a concrete task and where it will run. Inspect the effective settings
with `sbx-tui config list` and compare them with `sbx-tui config list --project`.
The latter shows the file on disk. A pending trust review can make these differ.
Use the launcher's discovered capabilities in the wizard or `sbx-tui doctor`
before proposing flags.

| Scenario | Decision to make with the owner |
| --- | --- |
| Review a change | Does the reviewer need history or just source files? Where supported, use profile `extra_args = ["--vcs-access=read-only"]` for history or `--vcs-access=hidden` for source-only access. Both modes still allow source edits. Use a separate bookmark workspace if edits must stay out of the current checkout. |
| Implement and test | Identify which steps need writable VCS metadata, dependency hosts or a host directory. Keep the firewall enabled when named domains suffice. A profile can add a required host with `--extra-domain=HOST` for firewall egress. With proxy or sidecar egress, check the proxy policy. For a bind mount, agree on the host path, container target and whether writes are needed. |
| Authenticate an agent | Choose among the sources available for that agent. For a direct login inside the sandbox, use `--no-forward-credentials` where supported. If an API key is required, identify its environment variable by name. For a gateway profile, confirm the endpoint before sending a key or querying models. Keep credential values out of TOML, prompts, previews and reports. |
| Run a project command | `[commands]`, including `:*NAME`, execute shell code on the host. The setup interview and `:host` also run on the host. Use a sandbox action when the task needs container isolation. Host tasks can overlap a session in the same workspace. |
| Automate a sequence | Bind actions to named profiles so access does not depend on the last interactive choice. Review the workflow picker's choices for each step. Set finite attempt and step limits for feedback policies. Ask before enabling retries for a step with external effects that could be repeated. |
| Retain or clean workspaces | Confirm which bookmark counts as merged in `project.main_branch` and whether ignored files must survive. `project.keep_workspace` controls sandbox teardown. Later cleanup can still delete ignored files in merged workspaces. Preview candidates with `sbx-tui --cleanup-workspaces --dry-run`. |

Shared `[sandbox]` and `[network]` settings also affect other profiles.
Profile arguments follow shared arguments. Repeatable flags such as
`--extra-domain` accumulate, so putting a domain in shared settings grants it
to other firewall profiles too. For an access expansion, show the affected
profile or scope, the resource it exposes, and a narrower option that would
accomplish the task, if one exists. Resolve that choice with the owner before
applying it. An explicit choice already made in the interview is sufficient.

Queued runs retain their selected profile arguments. They use the dispatching
TUI's shared network and sandbox settings when they start. If a shared access
change affects waiting work, inspect the queue with `L` and discuss whether
those jobs should run with the new settings.

Preserve unrelated settings and review the proposed diff. For direct project
file edits, have the owner review `sbx-tui --trust-config`. Check the effective
config and the selected profile's command preview in the launch form. Escape
closes the preview. A config review requires no live launch or credential lookup.
