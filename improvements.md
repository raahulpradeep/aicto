# dashboard/top.py — Improvements

## Bug: Agents and Open-tasks panels don't refresh live timers

### Root cause

`watch_snapshot` in `TopApp` (around line 619) guards each panel re-render with a
data-equality check to avoid unnecessary work:

```python
if prev is None or prev[0] != agents or prev[4] != running:
    self.query_one("#agents", Static).update(_agent_panel(agents, running, now))
...
if prev is None or prev[2] != open_tasks:
    self.query_one("#open", Static).update(_open_panel(open_tasks, now))
```

The guards compare `prev[0]` (previous agents list) and `prev[2]` (previous open_tasks list)
against the new lists. Python does deep equality on lists of dicts, so when nothing in the
underlying issue data changes between polls — which is the common case — the comparison
returns `False` and the panel is never re-rendered.

However, both panels compute a live countdown from `now = dt.datetime.now(dt.timezone.utc)`:

- **Agents panel** — `ELAPSED` column: `_fmt_elapsed(secs)` where `secs = (now - started_at).total_seconds()`
- **Open tasks panel** — `AGE` column: `_age(row, now)` where age = `(now - created_at).total_seconds()`

Because `now` is not stored in the snapshot data, the guards prevent these panels from ever
refreshing their elapsed/age displays. The timers appear frozen.

`#inbox` and `#closed` panels do **not** use `now`, so their guards are correct and can stay.

---

## Fix

In `watch_snapshot`, remove the data-equality guard for `#agents` and `#open` so they
re-render on every snapshot tick (every `KEY_POLL_S = 0.25 s`).

### Before (lines 619–627)

```python
prev = getattr(self, "_prev_snapshot", None)
if prev is None or prev[0] != agents or prev[4] != running:
    self.query_one("#agents", Static).update(_agent_panel(agents, running, now))
if prev is None or prev[1] != inbox:
    self.query_one("#inbox", Static).update(_inbox_panel(inbox))
if prev is None or prev[2] != open_tasks:
    self.query_one("#open", Static).update(_open_panel(open_tasks, now))
if prev is None or prev[3] != closed:
    self.query_one("#closed", Static).update(_closed_panel(closed))
```

### After

```python
prev = getattr(self, "_prev_snapshot", None)
# Always re-render: ELAPSED column is computed from `now` at render time.
self.query_one("#agents", Static).update(_agent_panel(agents, running, now))
if prev is None or prev[1] != inbox:
    self.query_one("#inbox", Static).update(_inbox_panel(inbox))
# Always re-render: AGE column is computed from `now` at render time.
self.query_one("#open", Static).update(_open_panel(open_tasks, now))
if prev is None or prev[3] != closed:
    self.query_one("#closed", Static).update(_closed_panel(closed))
```

The `#inbox` and `#closed` guards are unchanged — neither panel uses `now`.

---

# Reconciler + prompt templates — Move mechanical transitions into reconciler

## What changed and why

The reconciler was only covering the "inner" workflow work (plan filing, dev/review pairs,
re-reviews, ship gate). Three inter-phase transitions were left to agents via prompts:

| Transition | Previously owned by | Now owned by |
|---|---|---|
| Breakdown closed → file `kind:approval target:breakdown` | Manager agent | Reconciler Phase 1.5 |
| Plan review approved → file `kind:approval target:plan` | Reviewer agent | Reconciler Phase 2.5 |
| Code review approved → file `kind:merge target:code` | Reviewer agent | Reconciler Phase 5.5 |

These are all pure state reads — no judgment required. Moving them to deterministic Python
eliminates the approval-blocked-by-epic bug (agents were mis-applying dep commands) and
simplifies the reviewer to a single action: **close your review with a verdict**.

---

## File changes

### `templates/reconciler.py`

**New variables** (added alongside existing per-epic declarations):
```python
plan_reviews = [c for c in children if c.kind == "review" and c.target == "plan"]
approvals_breakdown = [c for c in children if c.kind == "approval" and c.target == "breakdown"]
approvals_plan = [c for c in children if c.kind == "approval" and c.target == "plan"]
```

**Phase 1.5** (new, after Phase 1 early-return):
```python
closed_breakdowns = [b for b in breakdowns if not b.is_open]
if closed_breakdowns and not approvals_breakdown:
    bd_appr_idem = idem("file-approval-breakdown", epic.id)
    if not state.has_idem(bd_appr_idem):
        bd_issue = closed_breakdowns[-1]
        actions.append(FileIssue(
            title=f"Approve breakdown: {epic.title}",
            description=(
                f"epic: {epic.id}\n"
                f"branch: manager/{bd_issue.id}\n"
                f"artifact: breakdowns/{epic.id}.md @ branch manager/{bd_issue.id}\n"
                f"idem: {bd_appr_idem}\n"
                f"Read breakdowns/{epic.id}.md. Approve via `cto approve` or reject with --comment."
            ),
            labels=("role:cto", "kind:approval", "target:breakdown"),
            priority=1,
        ))
```

**Phase 2.5** (new, between Phase 2 and Phase 3):
```python
closed_approved_plan_reviews = [
    r for r in plan_reviews
    if not r.is_open and not r.changes_requested()
]
if closed_approved_plan_reviews and not approvals_plan and plans:
    pl_appr_idem = idem("file-approval-plan", epic.id)
    if not state.has_idem(pl_appr_idem):
        plan = plans[-1]
        actions.append(FileIssue(
            title=f"Approve plan: {epic.title}",
            description=(
                f"epic: {epic.id}\n"
                f"branch: task/{plan.id}\n"
                f"artifact: plans/{epic.id}.md @ task/{plan.id}\n"
                f"idem: {pl_appr_idem}\n"
                f"Read plans/{epic.id}.md. Approve via `cto approve` or reject with --comment."
            ),
            labels=("role:cto", "kind:approval", "target:plan"),
            priority=1,
        ))
```

**Phase 5.5** (new, after Phase 5, before Phase 6 ship gate):
```python
for upstream_id, revs in by_upstream.items():
    revs.sort(key=lambda r: _review_round_number(r))
    latest = revs[-1]
    if latest.changes_requested():
        continue
    merge_key = idem("file-code-merge", epic.id, upstream_id)
    if state.has_idem(merge_key):
        continue
    if any(
        f"upstream: {upstream_id}" in m.description
        or f"branch: task/{upstream_id}" in m.description
        for m in code_merges
    ):
        continue
    dev = state.by_id(upstream_id)
    if dev is None:
        continue
    actions.append(FileIssue(
        title=f"Merge: {dev.title}",
        description=(
            f"epic: {epic.id}\n"
            f"upstream: {upstream_id}\n"
            f"branch: task/{upstream_id}\n"
            f"idem: {merge_key}\n"
            f"Merge task/{upstream_id} into epic/{epic.id}, prune sub-worktree."
        ),
        labels=("role:manager", "kind:merge", "target:code"),
        priority=1,
    ))
```

---

### `templates/manager.md`

**Section 2, step 4** — removed the `bd create ... kind:approval,target:breakdown` block.
Manager now just closes the breakdown issue; reconciler fires the approval.

**Section 4** (reconciler note) — updated to list all transitions now owned by reconciler,
including the three new ones.

---

### `templates/reviewer.md`

**Intro line** — updated from "You file approval issues…" to "You close your review issue
with a verdict and the reconciler files the next step."

**Section 5A** (approved plan) — was: close review + file `kind:approval target:plan` + dep.
Now: `bd close <review-id> -r "approved; ..."` only.

**Section 5B** (approved code) — was: close review + file `kind:merge target:code` + dep.
Now: same one-liner close.

**Section 5C** (changes-requested) — unchanged.

**Hard rules** — replaced "Plans require two approvals: yours, then the CTO's" with
"Reviewers do not file `kind:approval` or `kind:merge` issues — the reconciler does that."

---

## After applying on the target machine

1. Run `cto reconciler-sync <team> --force` for every team to push the new reconciler into
   `.cto/reconciler.py`. The old copy will be replaced.
2. Run `cto start <team> --force` (or `cto restart <team>`) to regenerate prompts from the
   updated templates and restart the supervisor loop.
3. Existing in-flight epics that already have agent-filed approvals are safe — the idem key
   and `not approvals_breakdown / not approvals_plan` guards prevent duplicate filings.

---

# bin/cto + .cto/prompts/manager.md — Approval blocked by own epic

## Symptom

A `kind:approval target:breakdown` (or `target:plan`) issue appears in `bd blocked` with
"Blocked by spm-2tx" — where spm-2tx is **the epic itself**.  
`cto approve` still works (it closes the issue directly without checking blockers) but
`bd ready --label role:cto` never surfaces the approval, so the CTO has to discover it via
`bd blocked` or `bd list --status open --label role:cto`.

The reconciler window looks empty because the workflow is stalled at this approval gate;
there are no automated actions left for the reconciler to take until the approval closes.

## Root cause

In `teams/spm/.cto/prompts/manager.md` (around line 78), the manager is instructed to:

```bash
bd close <new-id> -r "drafted"
bd dep <epic-id> --blocks <new-id>     # link as child of epic

bd create -t task -l role:cto,kind:approval,target:breakdown -p 1 \
  ...
```

The `bd dep <epic-id> --blocks <new-id>` line is only intended to link the **breakdown**
issue to the epic. However, the LLM manager agent sometimes applies the same dep pattern
to the **approval** issue it files immediately after, producing:

```bash
bd dep <epic-id> --blocks <approval-id>
```

This makes the approval blocked by the epic. Since the epic only closes at the very end
(via `cto merge-epic`), the approval never appears in `bd ready` and the workflow
silently stalls. The CTO inbox shows the issue, but `bd ready` does not.

The same failure mode can occur in `review-1.md` for `kind:approval target:plan` — the
reviewer prompt says `bd dep <review-id> --blocks <APPROVAL_ID>` (correct: review blocks
approval), but an LLM may additionally add `bd dep <epic-id> --blocks <APPROVAL_ID>`.

---

# Epic option: bypass manual CTO gates

## Goal

Add an explicit per-epic fast path for normal feature work that should still
go through reviewer plan/code checks, but should not wait on manual CTO
approvals during the intermediate stages.

Chosen behavior:

- Public API: `cto task ... --bypass-cto` and `mcp.server.task(..., bypass_cto=True)`
- Storage: epic label `class:bypass-cto`
- Reviewer checks remain mandatory
- Manual CTO `target:breakdown` and `target:plan` approvals are skipped
- Final epic merge remains manual when `parent_branch == main`
- Final epic merge is auto-executed by the reconciler when `parent_branch != main`

---

## File changes

### `bin/cto`

`cmd_task()` now accepts `--bypass-cto` for epics only:

- Adds `class:bypass-cto` to the epic labels
- Rejects invalid combinations:
  - `--bypass-cto` with `--dev`
  - `--bypass-cto` with `--plan`
  - `--bypass-cto` with `--ops`
- Logs the filed epic with `bypass_cto: true`

Usage text was also updated to advertise the new flag.

### `mcp/server.py`

`task(...)` now accepts:

```python
bypass_cto: bool = False
```

Validation mirrors the CLI:

- only valid for `kind="epic"`
- incompatible with `ops=True`

When true, the wrapper passes `--bypass-cto` through to `bin/cto`.

---

### `templates/reconciler.py`

Two new epic helpers were added to `Issue`:

```python
@property
def is_bypass_cto(self) -> bool:
    return "class:bypass-cto" in self.labels

@property
def parent_branch(self) -> str:
    ...
```

And a new executor action was introduced:

```python
@dataclass(frozen=True)
class AutoMergeEpic:
    epic_id: str
    merge_target: str
```

#### Phase 1.5

Previously:

- breakdown closed → file `kind:approval target:breakdown`

Now:

- normal epic → unchanged
- `class:bypass-cto` epic → file `kind:merge target:breakdown` directly

This uses an idem key:

```python
idem("file-breakdown-merge", epic.id, breakdown_id)
```

#### Phase 2.5

Previously:

- approved plan review → file `kind:approval target:plan`

Now:

- normal epic → unchanged
- `class:bypass-cto` epic → file `kind:merge target:plan` directly

This uses an idem key:

```python
idem("file-plan-merge", epic.id, plan.id)
```

#### Phase 5.5

The earlier reconciler work already moved approved code-review handling into
Python. This change preserves that path and keeps `kind:merge target:code`
filing centralized for both normal and bypassed epics.

#### Ship phase

Previously:

- ship-ready epic → always file `kind:merge target:epic role:cto`

Now:

- normal epic → unchanged
- `class:bypass-cto` + `parent_branch == main` → still file CTO epic-merge issue
- `class:bypass-cto` + `parent_branch != main` → emit `AutoMergeEpic`

#### Auto-merge executor

`_auto_merge_epic(...)` performs the actual merge for the non-`main` bypass
case inside the reconciler execute layer:

- verifies `epic/<id>` exists
- verifies the target branch exists locally or on `origin`
- refuses to proceed unless the team main worktree is already on the target branch
- stashes/pops `.beads` if needed
- runs `git merge --no-ff epic/<id>`
- closes any stale open `kind:merge target:epic` issue for that epic
- closes the epic itself
- prunes `.cto/worktrees/<epic-id>` and deletes `epic/<id>`

This keeps the fast path entirely deterministic and reconciler-owned.

---

### Prompt + docs updates

Updated:

- `CLAUDE.md`
- `templates/team-CLAUDE.md`
- `templates/manager.md`
- `templates/reviewer.md`

Key wording changes:

- reviewer prompts still require review, but they now mention that approved
  plan reviews may flow to either CTO approval or direct plan merge depending
  on `class:bypass-cto`
- manager prompt now states that breakdown and plan transitions may go straight
  to manager merges for bypassed epics
- team docs now describe the conditional auto-ship behavior for non-`main`
  parent branches

---

## Tests

### `templates/tests/test_reconciler.py`

Added coverage for:

1. bypass epic skips breakdown approval and files breakdown merge
2. bypass epic skips plan approval and files plan merge
3. bypass epic targeting `main` still files CTO epic-merge issue
4. bypass epic targeting non-`main` emits `AutoMergeEpic`

### `tests/test_mcp_server.py`

New wrapper test verifies:

```python
task(team="demo", title="Test epic", kind="epic", bypass_cto=True)
```

passes exactly:

```python
["task", "demo", "Test epic", "-p", "2", "--epic", "--bypass-cto"]
```

to `_run(...)`.

## Fix 1 — Make manager prompt explicit (primary fix)

In `templates/` and `teams/spm/.cto/prompts/manager.md`, add a warning immediately after
the dep command so the pattern isn't applied to the approval:

**Before (around line 78):**
```bash
   bd close <new-id> -r "drafted"
   bd dep <epic-id> --blocks <new-id>     # link as child of epic

   bd create -t task -l role:cto,kind:approval,target:breakdown -p 1 \
```

**After:**
```bash
   bd close <new-id> -r "drafted"
   bd dep <epic-id> --blocks <new-id>     # link as child of epic — breakdown only, NOT approval

   # Do NOT run bd dep for the approval issue.
   # The approval must be immediately available to the CTO; adding any dep would block it.
   bd create -t task -l role:cto,kind:approval,target:breakdown -p 1 \
```

Apply the same note in `review-1.md` around the approval filing for `target:plan`.

## Fix 2 — cto approve: auto-remove stale epic blocker (defensive fix)

In `bin/cto` `cmd_approve()` (around line 992), after reading `issue_json`, detect and
remove any dep where the epic itself is a blocker of this approval:

```bash
  # Remove any dep that makes the epic block this approval — a common
  # mis-filing by the manager agent that would otherwise hide the approval
  # from `bd ready` forever.
  if [[ -n "$desc" ]]; then
    local epic_id_from_desc
    epic_id_from_desc=$(grep -oE 'epic:[[:space:]]*[A-Za-z0-9._-]+' <<<"$desc" \
                        | head -1 | awk -F: '{print $2}' | tr -d '[:space:]' || true)
    if [[ -n "$epic_id_from_desc" ]]; then
      (cd "$tdir" && bd dep remove "$epic_id_from_desc" --blocks "$id" 2>/dev/null || true)
    fi
  fi
```

Place this block just before the `( cd "$tdir" && bd comment ... )` approval-close block
(around line 1013). This is idempotent — `bd dep remove` is a no-op if the dep doesn't exist.

Note: verify the exact `bd dep remove` command syntax for your bd version; it may be
`bd dep del` or `bd dep <id> --unblocks <other>`.

## Fix 3 — reconciler empty window is expected when workflow is stalled

The reconciler window shows output only when the reconciler files or labels issues.
When the workflow is blocked at an approval gate (nothing for the reconciler to act on),
the window is blank. **This is normal**, not a crash. You can confirm by running:

```bash
tmux send-keys -t cto-spm:reconciler '' Enter
```

If the cursor is at a shell prompt and the loop is still running, the reconciler is healthy.
