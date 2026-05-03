# {{TEAM}} — team workspace

This repository is a **team workspace** owned by an AI CTO. Multiple Claude Code agents (a manager, developers, and reviewers) work here in parallel, coordinating exclusively through this team's beads tracker.

> If you are a human reading this: do **not** run interactive `claude` here unless you've read `~/Work/control-room/aicto/README.md`. Use `cto …` from the parent dir.

## How this team operates

- One **manager**, {{N_DEVS}} **developers**, {{N_REVIEWERS}} **reviewers** (configured in `.cto/config.yaml`).
- All work-in-progress lives in **per-task worktrees** at `.cto/worktrees/<issue-id>` on branches `manager/<id>` (manager) or `task/<id>` (developers).
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
| `kind:merge` | Manager merges a branch into `main`. |
| `kind:status-digest` / `kind:status-request` | Manager↔CTO status protocol. |

| label | meaning |
| --- | --- |
| `role:manager`, `role:developer`, `role:reviewer`, `role:cto` | Who claims it. |

## Gates the workflow enforces

1. The CTO must close `kind:approval target:breakdown` before any plan is filed.
2. The reviewer must close `kind:review target:plan` (approved) before they file `kind:approval target:plan`.
3. The CTO must close `kind:approval target:plan` before any `kind:dev` is filed.
4. The reviewer must close `kind:review target:code` (approved) before a `kind:merge` is filed for that dev branch.
5. Only the manager merges, only via a `kind:merge` issue.

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
