# {{TEAM}} — team workspace

This repository is a **team workspace** owned by an AI CTO. Multiple AI agents (a manager, developers, and reviewers) work here in parallel, coordinating exclusively through this team's beads tracker.

> If you are a human reading this: do **not** run interactive agent sessions here unless you've read `~/Work/control-room/aicto/README.md`. Use `cto …` from the parent dir.

## How this team operates

- One **manager**, {{N_DEVS}} **developers**, {{N_REVIEWERS}} **reviewers** (configured in `.cto/config.yaml`).
- Each epic gets its own long-lived feature branch `epic/<epic-id>` and worktree at `.cto/worktrees/<epic-id>/`. **All sub-branches** (`manager/<id>`, `task/<id>`) are carved off the epic branch and live in sibling sub-worktrees at `.cto/worktrees/<issue-id>/`. The team's main worktree always stays on the trunk branch. The manager merges sub-branches **into the epic worktree**; only the **CTO** merges `epic/<id>` into its parent branch.
- bd lives in the **main worktree only**. All bd commands run from `{{TEAM_DIR}}`.
- `containerUse: {{CONTAINER_USE}}` — when true, developers use the `container-use` MCP tools instead of bare git worktrees.

## Workflow stages (encoded as bd labels on `task`-typed issues)

| label | meaning |
| --- | --- |
| `kind:epic` | High-level CTO ask. Owned by manager. |
| `kind:breakdown` | Manager's written breakdown of an epic. |
| `kind:plan` | Developer-authored design doc (`plans/<epic-id>.md`). |
| `kind:dev` | Developer-authored implementation. |
| `kind:review` + `target:plan|code` | Reviewer's read of a plan or code diff. |
| `kind:approval` + `target:breakdown|plan` | CTO's final sign-off. |
| `kind:merge` + `target:breakdown|plan|code` | Manager merges a sub-branch into `epic/<epic-id>` (never the trunk branch). |
| `kind:merge` + `target:epic` + `role:cto` | **CTO-only.** Manager files this when the epic is complete; CTO runs `cto merge-epic` to merge `epic/<id>` into its parent branch. |
| `class:bypass-cto` | Epic label. Skips manual CTO `target:breakdown` and `target:plan` approvals. Reviewer checks remain mandatory. Final epic merge is auto-executed by the reconciler when `parent_branch != main`. |
| `kind:status-digest` / `kind:status-request` | Manager↔CTO status protocol. |

| label | meaning |
| --- | --- |
| `role:manager`, `role:developer`, `role:reviewer`, `role:cto` | Who claims it. |

## Gates the workflow enforces

1. The CTO must close `kind:approval target:breakdown` before any plan is filed. (Skipped for `class:bypass-cto` epics — reconciler files `kind:merge target:breakdown` directly.)
2. The reviewer must close `kind:review target:plan` (approved). The reconciler then files `kind:approval target:plan` (normal) or `kind:merge target:plan` (`class:bypass-cto`).
3. The CTO must close `kind:approval target:plan` before any `kind:dev` is filed. (Skipped for `class:bypass-cto` epics.)
4. The reviewer must close `kind:review target:code` (approved) before a `kind:merge` is filed for that dev branch.
5. The manager merges sub-branches into `epic/<epic-id>`, only via a `kind:merge target:breakdown|plan|code` issue.
6. **Only the CTO merges `epic/<id>` into its parent branch**, via `cto merge-epic` (or `cto approve` on the manager-filed `kind:merge target:epic role:cto` issue). The manager never touches the trunk branch. Exception: `class:bypass-cto` epics with `parent_branch != main` are auto-merged by the reconciler.

## Gist discipline

bd issue bodies and comments hold **gists**, not artifacts. ≤ 5 lines or ≤ 80 words. Real content lives as committed files in worktrees, referenced by path + branch.

## Quick reference

```bash
bd ready --label role:<myrole> --json     # what should I work on?
bd update <id> --claim                     # atomic claim
bd close <id> -r "<gist>"                  # finish work
bd dep <a> --blocks <b>                    # dependency
bd list --status open --label role:cto     # what's awaiting CTO?
```

<!-- Beads integration block (auto-managed) is appended below by `bd init`. -->
