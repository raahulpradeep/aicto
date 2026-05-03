# Review aicto-2jt: Plan — Eliminate UI jitter in top dashboard

Reviewer: aicto:review-1  
Plan: `plans/aicto-v6t.md` on branch `task/aicto-bi5`  
Epic: aicto-v6t  
Verdict: **APPROVED** (1 suggestion, no blockers)

---

## Root-cause validation

All five jitter sources were verified against `dashboard/top.py`:

- `Console().height` at line 535 — confirmed bare constructor call inside `render()`.
- Dynamic `_panel_height()` called every cycle — confirmed at lines 525–530.
- `live.update()` / `live.refresh()` split — confirmed at lines 614 and 624.
- No stale-data fallback — confirmed: `gather()` silently skips `None` teams (lines 312–314).
- No per-team wall-clock timeout — confirmed: `fut.result()` called without timeout (lines 311–312).

Root-cause analysis is accurate and grounded in the real source.

---

## Observations

### 1. Stale-row display contract is ambiguous (suggestion — top.py, Dev 1 step 4 + Dev 3 step 1)

Dev 1, step 4 says: "dim the row **or** prefix the team name with `~`."  
Dev 3, step 1 then asserts `~` prefix in the regression test.

The "or" leaves implementation choice open, but the test pins it to `~`. Pick `~` prefix explicitly in Dev 1's spec so developers and the test agree without guessing. No code change needed — just align the wording.

Severity: **suggestion**

### 2. Stale cache eviction is mentioned in risks but not in Dev 1's steps (suggestion — top.py, Dev 1)

"Evict entries whose team dir no longer exists" appears only under Risks & rollback, not as a numbered step in Dev 1. A developer reading only the implementation chunks will miss it. Add it as Dev 1 step 2b (run eviction at the top of each `gather()` cycle, before querying futures).

Severity: **suggestion**

### 3. `_layout_heights` initialization path unspecified (nit — top.py, Dev 2b)

The plan says heights are recomputed only when `console.size` changes. On the very first render the dict is empty — the initial population is left implicit. A skilled developer will handle this, but one sentence clarifying "populate on first call if empty, then only on resize" would prevent an off-by-one iteration at startup.

Severity: **nit**

---

## Overall

The three-dev breakdown is well-sequenced (gather resilience → rendering atomicity → tests/polish). The `live.console` reuse, single `live.update(refresh=True)` call, and height-stability cache are all correct Rich idioms. Risks table is thorough. No blockers found.
