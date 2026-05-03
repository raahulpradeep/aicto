# {{TEAM}} — team workspace (agent instructions)

See `CLAUDE.md` for the full team operating model. Same content applies to all agents regardless of provider.

Key rules:
- bd is the source of truth. Do not use markdown TODO lists.
- Work in `.cto/worktrees/<issue-id>` (or container-use envs if `containerUse: true`); never edit the main worktree directly except for breakdowns (manager) or merges.
- bd holds gists; artifacts (`breakdowns/`, `plans/`, `docs/reviews/`, code) live as committed files.
- Never push to remotes. Never merge except via a `kind:merge` issue.
