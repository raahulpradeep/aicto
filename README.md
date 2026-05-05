# aicto

An AI **CTO workspace**: a parent directory that owns a small fleet of "team" git repos. Each team has its own beads tracker and a tiny org chart of AI agents — a manager, a configurable number of developers, and a configurable number of reviewers — running in parallel as tmux windows. Agents can be Claude Code or Kimi Code CLI.

You sit in this directory in an AI coding assistant session and play the human CTO: file epics, approve breakdowns and plans, ask for status. The teams do the work.

## How it works

```
aicto/                     ← you are here, in the CTO session
├── bin/cto                ← the CLI you use
├── templates/             ← role prompts & per-team agent docs
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
- `claude` (Claude Code CLI) or `kimi` (Kimi Code CLI) on `PATH`
- `git`, `tmux`, `jq`, `python3`, `uv` (the last only for the MCP server)

`bd init` has already been run in this directory.

## MCP server (for the CTO session)

When you open an AI coding assistant session in this directory, an MCP server named **`cto`** is auto-loaded from `.mcp.json`. It exposes every `bin/cto` subcommand as a structured tool (`mcp__cto__team_create`, `mcp__cto__inbox`, `mcp__cto__approve`, `mcp__cto__update`, `mcp__cto__read_artifact`, etc.) so you can drive the CTO workflow as tool calls instead of shelling out.

The MCP is **scoped to this workspace only**. Team agent sessions run inside `teams/<name>/`, which is a separate project root with its own `.git`, so they do not load this MCP. Teams use `bd` directly via their role prompts; only the CTO has the higher-level cto tools.

The server lives at `mcp/server.py` and is launched by `uv run --with mcp python mcp/server.py`. No global Python deps needed — `uv` resolves `mcp` on first run.

## Quickstart

```bash
# 1. Create a team (1 manager, 2 devs, 1 reviewer by default).
bin/cto team create demo-app
#   …with options:
bin/cto team create demo-app --developers 3 --reviewers 2 --container-use
#   …using Kimi Code CLI instead of Claude Code:
bin/cto team create demo-app --agent-provider kimi
#   …from a remote git URL:
bin/cto team create demo-app --repo git@github.com:you/demo-app.git
#   …or adopt an existing local directory (with or without git):
bin/cto team adopt  demo-app ~/Work/some-existing-repo            # moves it
bin/cto team adopt  demo-app ~/Work/some-existing-repo --copy     # copies it

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
| `cto config <n> …` | edit `.cto/config.yaml` (devs / reviewers / container-use / mode / model / agent-provider) |
| `cto task <n> "<t>" --epic` | file an epic for the team |
| `cto inbox [<n>]` | open issues with `role:cto` — your approval queue |
| `cto approve <n> <id>` | close a `role:cto` issue (approval) |
| `cto reject <n> <id> --comment …` | bounce it back; reopens the upstream artifact |
| `cto start <n>` | open tmux session `cto:<n>` with one window per agent |
| `cto stop <n>` / `cto restart <n>` | graceful shutdown / cycle |
| `cto attach <n> [<window>]` | drop into the tmux session |
| `cto status [<n>]` | bd snapshot + tmux state |
| `cto update <n> [--fresh]` | latest manager status digest |
| `cto top` | unified interactive dashboard: agents, inbox, epic pipeline, activity stream, open tasks; approve/reject inline; quit with `q` |
| `cto top --legacy` | old read-only `top`-style dashboard (fallback) |
| `cto review` | same as `cto top` — opens the unified dashboard |
| `cto worktrees <n>` / `cto worktrees prune <n>` | list / GC per-task worktrees |
| `cto exec <n> -- <cmd…>` | escape hatch: run a command in the team's main worktree |

## Configuration

Per-team config at `teams/<n>/.cto/config.yaml`:

```yaml
developers: 2
reviewers: 1
manager: true
permissionMode: bypassPermissions   # or acceptEdits
containerUse: false           # true → developers use container-use MCP envs
model: sonnet
```

Edit via `cto config <n> --developers 3 --reviewers 2 --container-use on`.
After config changes, `cto restart <n>` to apply.

## Dashboard

`cto top` launches the unified dashboard. Layout:

- **Agents** (top-left) — working/idle status, elapsed time, provider, model
- **CTO Inbox** (top-right) — open `role:cto` issues across all teams; press `a` to approve, `r` to reject
- **Epic Pipeline** (middle-left) — swimlane per open epic showing Breakdown → Plan → Dev → Review → Merge → Ship
- **Activity Stream** (middle-right) — live feed of workflow events (claims, merges, commits, approvals)
- **Open Tasks** (bottom) — unclaimed work sorted by priority + age

Keyboard shortcuts:

| key | action |
|---|---|
| `q` | quit |
| `a` | approve selected inbox item |
| `r` | reject selected inbox item |
| `Enter` | drill down into selected epic/agent |

The dashboard fires desktop notifications on:
- new CTO inbox arrivals
- agent crashes
- review-loop escalations (>3 rounds)

## Health Watchdog

The reconciler now runs a health check **before** each workflow tick. It silently auto-heals:

- **Zombie issues** — auto-unclaims issues stuck `in_progress` for >15 minutes
- **Missing labels** — re-applies dropped workflow labels (`kind:epic`, `role:manager`, etc.)
- **Stuck epics** — files a `kind:status-request` if an epic has been idle for >1 hour
- **Review loops** — escalates to CTO inbox when a dev goes through >3 review rounds

## Caveats

- Agents run with `--permission-mode bypassPermissions` by default — fully autonomous file edits and shell commands. Only run this in a workspace you trust. To downgrade, set `permissionMode: acceptEdits` via `cto config <team> --permission-mode acceptEdits` (will still accept edits without prompting but ask before non-allowlisted shell commands).
- bd issue bodies are kept short by convention — agents are instructed to keep gists ≤ 5 lines / ≤ 80 words. Real artifacts live in worktrees.
- This is local-first. No remotes are wired up by default. To attach a remote, do it inside the team dir with normal git commands, or pass `--repo <url>` at `cto team create`.
