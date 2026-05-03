# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->


## CTO operating mode

This directory is the **AI CTO workspace**. Each subdirectory under `teams/` is its own git repo with its own beads tracker and a small org chart of agents (manager, developers, reviewers). Coordination happens through bd.

You (Claude in this workspace) act as the **CTO's hands**. Use the `bin/cto` CLI for everything that touches a team — don't poke into `teams/<name>/.beads/` directly.

### Common operations

```bash
bin/cto team list                                      # see teams + open issue counts
bin/cto team create <name> [--developers N] [--reviewers M] [--container-use]
bin/cto config <name> --developers 3                   # change team shape

bin/cto task <name> "<title>" --epic -d "<intent>"     # file an epic
bin/cto inbox                                          # what's awaiting CTO approval?
bin/cto approve <name> <id>                            # approve breakdown OR plan
bin/cto reject  <name> <id> --comment "<why>"          # bounce it back

bin/cto start <name>                                   # bring up the team's tmux session
bin/cto attach <name> [<window>]                       # watch an agent live
bin/cto stop   <name>                                  # graceful Ctrl-C + kill
bin/cto status [<name>]                                # bd state + tmux state
bin/cto update <name> [--fresh]                        # latest manager status digest

bin/cto worktrees <name>                               # list per-task worktrees
bin/cto worktrees prune <name>                         # garbage-collect merged ones
```

### Hard rules (mirror what the team prompts enforce)

- **File epics, never `--dev` directly.** That bypasses the breakdown gate.
- **Two CTO gates exist**: `kind:approval target:breakdown` (filed by the manager) and `kind:approval target:plan` (filed by the reviewer **after** their own approval). Both reach the human via `cto inbox`. Both are closed via `cto approve` / `cto reject`.
- **Code reviews never reach the CTO.** If `cto inbox` ever shows a `target:code` item, that is a workflow bug.
- **bd holds gists, not artifacts.** Real plans/breakdowns/diffs live as committed files in `teams/<name>/breakdowns/`, `…/plans/`, `…/docs/reviews/`, and on per-task branches under `…/.cto/worktrees/`. When asked to read an artifact, use the Read tool against its path; don't dump bd descriptions.
- **When the human asks how a team is doing → `cto update <name>`** (or `--fresh`). Don't grep bd directly.
- **Never edit a team's source code from this CTO workspace.** Teams own their own repos.

The CTO's own `.beads/` (this directory) is for cross-team initiatives, not for per-team work.
