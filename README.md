# aicto

An AI **CTO workspace**: a parent directory that owns a small fleet of "team" git repos. Each team has its own beads tracker and a tiny org chart of Claude Code agents — a manager, a configurable number of developers, and a configurable number of reviewers — running in parallel as tmux windows.

You sit in this directory in a Claude Code session and play the human CTO: file epics, approve breakdowns and plans, ask for status. The teams do the work.

## How it works

```
aicto/                     ← you are here, in a claude session
├── bin/cto                ← the CLI you use
├── templates/             ← role prompts & per-team CLAUDE.md
└── teams/
    └── <team-name>/       ← one repo per team
        ├── .beads/        ← team's own bd tracker
        ├── breakdowns/    ← manager-authored .md artifacts
        ├── plans/         ← developer-authored .md artifacts
        ├── docs/reviews/  ← reviewer-authored notes
        └── .cto/
            ├── config.yaml
            ├── prompts/   ← rendered role prompts
            └── worktrees/<id>/   ← per-task worktrees
```

A team's agents communicate **only through bd**. Real artifacts (breakdowns, plans, code diffs) live as committed files in worktrees; bd issues hold short gists pointing at them.

## The workflow

For every CTO-filed epic, the team walks this state machine:

1. **Manager** writes a breakdown into a `manager/<epic-id>` worktree, files a `kind:approval target:breakdown` for the **CTO**.
2. **CTO** reads `breakdowns/<epic>.md` and runs `cto approve <team> <id>` (or rejects).
3. Reviewer/CTO files a `kind:merge`. **Manager** merges the breakdown into `main`.
4. **Manager** files `kind:plan` + `kind:review target:plan`.
5. **Developer** claims the plan, writes `plans/<epic>.md` in a `task/<plan-id>` worktree, closes.
6. **Reviewer** claims the plan review. If approved, files a `kind:approval target:plan` for the **CTO**. If not, reopens the plan with change requests.
7. **CTO** approves the plan via `cto inbox` + `cto approve …`.
8. Reviewer/CTO files merge for the plan branch. **Manager** merges.
9. **Manager** files `kind:dev` + `kind:review target:code` pairs per the merged plan.
10. **Developers** claim devs in parallel, work in their worktrees, **run tests**, commit, close.
11. **Reviewer** reviews each diff, approves → files merge. **Manager** merges.
12. When everything's merged, **manager** closes the epic.

Two CTO gates total per epic: breakdown approval, then plan approval. Code reviews are reviewer-only.

## Setup

This repo expects:

- `bd` (beads CLI) on `PATH`
- `claude` (Claude Code CLI) on `PATH`
- `git`, `tmux`, `jq`, `python3`

`bd init` has already been run in this directory.

## Quickstart

```bash
# 1. Create a team (1 manager, 2 devs, 1 reviewer by default).
bin/cto team create demo-app
#   …or with options:
bin/cto team create demo-app --developers 3 --reviewers 2 --container-use

# 2. File the team's first epic.
bin/cto task demo-app "Build a CLI that greets the user by name" \
  --epic -d "--name flag, fall back to \$USER, prints greeting."

# 3. Bring the team online (tmux session with one window per agent).
bin/cto start demo-app

# 4. Watch an agent live (Ctrl-b d to detach).
bin/cto attach demo-app manager

# 5. The manager will produce a breakdown and file an approval for you.
bin/cto inbox                              # see what's waiting on you
ls teams/demo-app/breakdowns/              # read the artifact
bin/cto approve demo-app <breakdown-approval-id>

# 6. After breakdown merges, plan is produced and reviewed; reviewer files
#    a plan approval for you.
bin/cto inbox
ls teams/demo-app/plans/
bin/cto approve demo-app <plan-approval-id>

# 7. Devs implement; reviewer reviews; manager merges. Ask for updates.
bin/cto update demo-app                    # latest digest
bin/cto update demo-app --fresh            # force a new digest

# 8. Inspect a worktree if you want to look directly.
bin/cto worktrees demo-app

# 9. When done, stop the team and prune merged worktrees.
bin/cto stop demo-app
bin/cto worktrees prune demo-app
```

## CLI reference

Run `bin/cto help` for a full list. Common ones:

| command | what it does |
| --- | --- |
| `cto team create <n> …` | scaffold a new team (git init + bd init + role prompts) |
| `cto team list` | one line per team, with bd & tmux state |
| `cto team remove <n>` | delete a team (confirms) |
| `cto config <n> …` | edit `.cto/config.yaml` (devs / reviewers / container-use / mode / model) |
| `cto task <n> "<t>" --epic` | file an epic for the team |
| `cto inbox [<n>]` | open issues with `role:cto` — your approval queue |
| `cto approve <n> <id>` | close a `role:cto` issue (approval) |
| `cto reject <n> <id> --comment …` | bounce it back; reopens the upstream artifact |
| `cto start <n>` | open tmux session `cto:<n>` with one window per agent |
| `cto stop <n>` / `cto restart <n>` | graceful shutdown / cycle |
| `cto attach <n> [<window>]` | drop into the tmux session |
| `cto status [<n>]` | bd snapshot + tmux state |
| `cto update <n> [--fresh]` | latest manager status digest |
| `cto worktrees <n>` / `cto worktrees prune <n>` | list / GC per-task worktrees |
| `cto exec <n> -- <cmd…>` | escape hatch: run a command in the team's main worktree |

## Configuration

Per-team config at `teams/<n>/.cto/config.yaml`:

```yaml
developers: 2
reviewers: 1
manager: true
permissionMode: acceptEdits   # or bypassPermissions
containerUse: false           # true → developers use container-use MCP envs
model: sonnet
```

Edit via `cto config <n> --developers 3 --reviewers 2 --container-use on`.
After config changes, `cto restart <n>` to apply.

## Caveats

- Agents run with `--permission-mode acceptEdits` by default. They will accept file edits without prompting and prompt before shell commands not on their allowlist. Use `cto start --dangerous` for `bypassPermissions` (fully autonomous; only when you trust the workspace).
- bd issue bodies are kept short by convention — agents are instructed to keep gists ≤ 5 lines / ≤ 80 words. Real artifacts live in worktrees.
- This is local-first. No remotes are wired up by default. To attach a remote, do it inside the team dir with normal git commands, or pass `--repo <url>` at `cto team create`.
