# Plan: Support configurable parent branch per epic

**Epic:** aicto-7vi  
**Breakdown:** breakdowns/aicto-7vi.md  
**Plan author:** aicto:dev-2

---

## Goals

- Allow an optional `--parent-branch <branch>` flag when filing a new epic via `cto task` / `mcp__cto__task`.
- Store the parent branch value in the epic bd issue (via the `--design` field).
- At merge time (`cto merge-epic` / `mcp__cto__merge_epic`), read that metadata and merge `epic/<id>` into `<parent_branch>` instead of `main`.
- Surface the parent branch in `cto inbox` and `cto status` output so the CTO can see the target at a glance.
- Update manager and developer prompt templates to reference the parent branch when it differs from `main`.

## Non-goals

- Nested epic chains (parent branch is itself an epic branch): explicitly out of scope. One level only.
- Changing the repo initialisation default (`git init -b main`): teams always start life on `main`.
- Retroactively migrating existing in-flight epics: this applies only to newly filed epics.

---

## Chunk 1 — CLI + storage layer (`bin/cto` + `mcp/server.py`)

### What changes

**`bin/cto`** — `cmd_task` (lines 620–646):
- Add `--parent-branch <branch>` flag to the option parser.
- When present, append `parent_branch: <branch>` as a line in the bd issue description (design field not directly exposed via `bd create`; embed it in the body text under a `## Config` section so it is parseable by grep).
- No validation of the branch value at creation time (the branch may not exist yet on the remote; validation happens at merge time).

**`mcp/server.py`** — `task` tool (lines 165–180):
- Add `parent_branch: Optional[str] = None` parameter.
- Pass `["--parent-branch", parent_branch]` to the CLI args when set.
- Update the docstring.

### Files touched
- `bin/cto`
- `mcp/server.py`

### Storage format (embedded in epic description)

```
parent_branch: feature-x
```

Placed on its own line so it can be extracted with:
```bash
grep -oE 'parent_branch:[[:space:]]*[A-Za-z0-9._/-]+' <desc> | head -1 | awk -F: '{print $2}' | tr -d ' '
```

---

## Chunk 2 — Merge-time resolution (`bin/cto` + `mcp/server.py`)

### What changes

**`bin/cto`** — `cmd_merge_epic` (lines 715–788):
1. After resolving `tdir` and `epic_branch`, read the epic issue description and extract `parent_branch`.
2. If present, set `merge_target="$parent_branch"`. If absent, keep `merge_target="main"`.
3. Replace the hard-coded `"main"` check at line 736:
   - Old: `[[ "$head" == "main" ]] || die …`
   - New: `[[ "$head" == "$merge_target" ]] || die …`
4. Replace the merge command at line 750 to use `"$merge_target"` instead of the implicit HEAD.
5. Replace the close-reason at line 775: `"epic merged into $merge_target by CTO"`.
6. **Validate target branch existence** before merging (check both local and remote refs so a branch that exists on origin but hasn't been fetched locally is still accepted):
   ```bash
   git -C "$tdir" show-ref --verify --quiet "refs/heads/$merge_target" \
     || git -C "$tdir" show-ref --verify --quiet "refs/remotes/origin/$merge_target" \
     || die "merge-epic: target branch '$merge_target' not found locally or on origin"
   ```
7. Update worktree-prune ancestor check (lines 1365–1370): use `"$merge_target"` instead of `"main"` when pruning the epic's own sub-branches. This requires a helper `epic_parent_branch()` that reads the epic's description once.

**`mcp/server.py`** — `merge_epic` docstring (line 207):
- Update to mention that the merge target is determined by the epic's `parent_branch` setting (defaulting to `main`).

### Files touched
- `bin/cto`
- `mcp/server.py`

### Helper function (new, in `bin/cto`)

```bash
# Read the parent_branch value stored in an epic's description.
# Returns "main" if not set.
# team_root must be the main worktree root — bd only works from there.
epic_parent_branch() {
  local team_root="$1" epic_id="$2"
  local desc
  desc=$(cd "$team_root" && bd show "$epic_id" --json 2>/dev/null \
         | jq -r 'if type=="array" then .[0].description // "" else .description // "" end')
  local pb
  pb=$(grep -oE 'parent_branch:[[:space:]]*[A-Za-z0-9._/-]+' <<<"$desc" \
       | head -1 | sed 's/parent_branch:[[:space:]]*//')
  echo "${pb:-main}"
}
```

The call site in `cmd_merge_epic` must supply `team_root` (resolved via `git rev-parse --show-toplevel` on the main worktree, not `tdir`), not the epic worktree path.

---

## Chunk 3 — Visibility + prompt awareness

### What changes

**`bin/cto`** — `cmd_inbox` (lines 820–838):
- For each `role:cto` issue in the output, if the issue is `kind:epic` or `kind:merge target:epic`, extract `parent_branch` from the description and append `→ <parent_branch>` to the display line (only when it differs from `main`).

**`bin/cto`** — `cmd_status` (lines 1276–1299):
- When listing open epics, extract and display `parent_branch` (e.g. `[target: feature-x]`) after the epic title if set.

**`templates/manager.md`**:
- Line 51 currently hardcodes `git branch epic/<epic-id> main`. Update the instruction to:
  > Create the epic branch from `main` (or from `parent_branch` if set in the epic description):
  > ```bash
  > PARENT=$(bd show <epic-id> ... | grep parent_branch | awk '{print $2}')
  > PARENT=${PARENT:-main}
  > git branch epic/<epic-id> $PARENT
  > ```
- Update any `merge epic/<id> into main` references in the template to say `into <parent_branch> (default: main)`.

**`templates/team-CLAUDE.md`**:
- Update the sentence "Only CTO merges epic into `main`" to "Only CTO merges epic into the target branch (default `main`; configurable via `parent_branch` on the epic issue)."

**`templates/developer.md`** — no changes required (devs always branch off `epic/<id>`, never from `main` directly).

### Files touched
- `bin/cto`
- `templates/manager.md`
- `templates/team-CLAUDE.md`

---

## Test strategy

No automated test suite exists. Manual verification steps (to be run by the developer before closing the dev issue):

1. **Flag parsing** — `cto task <team> "test epic" --parent-branch feature-x` and verify the resulting bd issue description contains `parent_branch: feature-x`.
2. **Merge target resolution** — mock a team with a `feature-x` branch checked out at the main worktree, call `cmd_merge_epic`, and confirm it targets `feature-x` not `main`.
3. **Missing branch guard** — call `cmd_merge_epic` when `feature-x` doesn't exist and confirm it dies with a helpful error.
4. **Default fallback** — file an epic without `--parent-branch` and confirm merge-epic still targets `main`.
5. **Inbox display** — confirm `cto inbox` shows `→ feature-x` only when parent branch differs from `main`.
6. **MCP tool** — call `mcp__cto__task` with `parent_branch="feature-x"` and verify the CLI is called with `--parent-branch feature-x`.

---

## Risks and rollback

| Risk | Mitigation |
|------|------------|
| Target branch doesn't exist at merge time | Hard error with clear message; CTO must create the branch first |
| Nested epics (epic branch as parent) | Explicitly rejected at planning stage; one level only |
| `cmd_worktrees prune` uses wrong ancestor | Helper `epic_parent_branch()` is called once per epic at prune time |
| Templates are regenerated by `cto config --scale` | Template changes flow through to existing teams only after next `render_prompts` call; document in changelog |
| In-flight epics without `parent_branch` | `epic_parent_branch()` defaults to `"main"` — backward compatible |

**Rollback:** All changes are backward-compatible (default is `main`). A simple revert of `bin/cto` and `mcp/server.py` restores prior behaviour with no data migration.

---

## Dev task breakdown

| Dev task | Chunk | Estimated effort |
|----------|-------|-----------------|
| Dev 1: CLI flag + storage | Chunk 1 | ~30 lines in `bin/cto` + ~5 lines in `mcp/server.py` |
| Dev 2: Merge-time resolution | Chunk 2 | ~40 lines in `bin/cto` + docstring update |
| Dev 3: Visibility + templates | Chunk 3 | ~20 lines in `bin/cto` + template text edits |
