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

## Persistent Agent Shell (Phase 1)

The default supervisor is a bash loop (`bin/cto start`) that spawns a fresh
`claude`/`kimi`/`codex` CLI process every 5-10 seconds.  That works, but it
is **stateless** — the agent re-reads its 10K token role prompt every
iteration and loses all accumulated context.

`src/persistent_supervisor.py` is a drop-in Python replacement that:

- **Maintains state across iterations** in SQLite (`.cto/state/agent_state.db`)
- **Preserves accumulated context** — scratchpad, iteration count, last task
- **Subscribes to events** on a file-based pub/sub bus (no Redis/NATS needed)
- **Recovers from crashes** — releases stale bd claims on restart, resumes loop
- **Falls back to legacy `bd ready` polling** — backward compatible with the
  existing beads workflow

### Quick start (persistent mode)

Phase 2 adds a `--persistent` flag to `cto start`:

```bash
# Start the team with Python persistent supervisors (event-driven)
bin/cto start demo-app --persistent

# Or restart into persistent mode
bin/cto restart demo-app --persistent
```

When `--persistent` is passed, `cto start`:
1. Symlinks `src/` into `teams/<name>/.cto/src/`
2. Spawns `persistent_supervisor.py` for each agent slot instead of bash loops
3. Keeps the same tmux session layout (manager, reconciler, dev-N, review-N)

### Architecture

```
team/
└── .cto/
    ├── src/                    ← symlink to CTO_ROOT/src (Phase 2)
    ├── state/
    │   └── agent_state.db      ← SQLite persistence
    ├── logs/
    │   └── {team}:{slot}.log   ← per-agent telemetry
    ├── locks/
    │   └── {task_id}/          ← atomic claim dirs (unchanged)
    └── supervisor.sh           ← legacy bash loop (still used without --persistent)
```

### Key differences from the bash supervisor

| Bash supervisor | Persistent supervisor |
|-----------------|-----------------------|
| Fresh CLI process every iteration | Long-running Python process |
| Re-reads role prompt from scratch | Loads from state, appends scratchpad |
| 5-15s spawn overhead per cycle | Sub-second wake on events |
| Crash → manual recovery | Auto-releases claims, resumes loop |
| Polls `bd ready` constantly | Event-driven + fallback poll |

## Security Sandbox (Phase 2)

`src/security.py` implements a 6-layer sandbox inspired by Delegate's security
model.  Every agent process can be wrapped with:

```python
from security import Sandbox
sb = Sandbox.for_agent(team_dir, worktree, allowed_domains=["github.com"])
sb.apply()          # sets restrictive env vars
sb.assert_command(["git", "status"])   # validate before exec
```

### The six layers

1. **Write-path isolation** — agents can only write to their assigned worktree;
   any path outside is rejected.
2. **Disallowed git commands** — `git push --force`, `git reset --hard`, branch
   deletion, `filter-branch`, etc. are blocked before execution.
3. **OS-level sandbox** — on macOS a temporary Seatbelt profile restricts
   file writes to the worktree + `/tmp` + standard devices.  On Linux it tries
   `firejail` or `bwrap`; elsewhere it falls back to env-var warnings.
4. **Network domain allowlist** — only pre-approved domains (GitHub, PyPI,
   npm, model APIs) are reachable.  Implemented via `HTTP_PROXY` → localhost:9
   with `NO_PROXY` bypass for allowed hosts.
5. **MCP tool boundary** — agents interact only through provided MCP tools.
   Direct shell spawn (`bash -c`, `sh -c`, backticks, command substitution) is
   detected and rejected.
6. **Daemon-managed worktree lifecycle** — only the supervisor may create or
   destroy worktrees (`git worktree add/remove/prune`).  Agents receive
   pre-created worktrees.

### Tests

```bash
uv run --with pytest pytest tests/test_security.py -v
```

## Event-Driven Reconciler (Phase 2)

The reconciler (`templates/reconciler.py`) now publishes workflow events to the
file-based event bus whenever it files or transitions an issue:

- `task.created` — new bd issue filed
- `breakdown.approved` — CTO approved a breakdown
- `plan.approved` — CTO approved a plan
- `dev.assigned` — a dev task is ready for pickup
- `review.required` — a review task is ready for pickup
- `merge.ready` — a merge task is ready for a manager

Persistent agents subscribe to `team.{name}.{role}` topics and wake
immediately when relevant events arrive, skipping the old `bd ready` poll
cycle.  If no events arrive within 30 s, the agent falls back to legacy
polling — backward compatible with teams not yet on the event bus.

### Configuration

In `src/agent_process.py`:

```python
AgentConfig(
    ...
    event_priority=True,      # prefer events over bd polling
    event_poll_timeout=30.0,  # seconds before fallback to bd ready
)
```

When `event_priority=True`:
1. Agent blocks on event bus for up to 30 s.
2. If an event arrives, it processes immediately.
3. If nothing arrives, it does **one** legacy `bd ready` poll, then goes
   back to blocking on events.

## Phase 2 Summary

| Feature | Status |
|---------|--------|
| `--persistent` flag in `cto start` | ✅ |
| Python persistent supervisors in tmux | ✅ |
| Reconciler emits workflow events | ✅ |
| AgentProcess event priority + polling fallback | ✅ |
| 6-layer security sandbox (`src/security.py`) | ✅ |
| Sandbox tests (`tests/test_security.py`) | ✅ |

---

*Phase 3 (Auto-Approval with Rollback) coming next.*

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
