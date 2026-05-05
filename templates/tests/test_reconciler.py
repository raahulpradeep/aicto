"""Fixture-based regression tests for the workflow reconciler.

Every recurring bug we hit becomes a fixture. Pure-Python — no live bd needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconciler import (  # noqa: E402
    AddLabel,
    AutoMergeEpic,
    CloseIssue,
    FileIssue,
    FilePair,
    Issue,
    RemoveLabel,
    State,
    reconcile,
)


# ---------- Helpers ----------

def _issue(
    id: str,
    kind: str,
    *,
    role: str = "manager",
    target: str | None = None,
    status: str = "open",
    description: str = "",
    title: str | None = None,
    labels_extra: tuple[str, ...] = (),
    close_reason: str = "",
) -> Issue:
    labels = [f"kind:{kind}", f"role:{role}"]
    if target:
        labels.append(f"target:{target}")
    labels.extend(labels_extra)
    return Issue(
        id=id,
        title=title or f"{kind} {id}",
        description=description,
        status=status,
        labels=tuple(labels),
        close_reason=close_reason,
    )


def _epic(id: str = "e1", title: str = "Test epic") -> Issue:
    return Issue(
        id=id, title=title, description="", status="open",
        labels=("kind:epic", "role:manager"),
    )


def _ops_epic(id: str = "e-ops", title: str = "git pull") -> Issue:
    return Issue(
        id=id, title=title, description="", status="open",
        labels=("kind:epic", "role:manager", "class:ops"),
    )


def _state(*issues: Issue) -> State:
    return State(issues=tuple(issues))


def _all_file_issues(actions):
    """Flatten FileIssue + FilePair (upstream + downstream) into a list of
    FileIssue. Tests assert against this — both halves of a pair are
    individually issues that get filed in bd."""
    out: list[FileIssue] = []
    for a in actions:
        if isinstance(a, FileIssue):
            out.append(a)
        elif isinstance(a, FilePair):
            out.append(a.upstream)
            out.append(a.downstream)
    return out


def _file_issues(actions, *, kind: str, target: str | None = None):
    out = []
    want_kind = f"kind:{kind}"
    want_target = f"target:{target}" if target else None
    for a in _all_file_issues(actions):
        if want_kind not in a.labels:
            continue
        if want_target and want_target not in a.labels:
            continue
        out.append(a)
    return out


# ---------- Tests: epic-level FSM ----------

def test_epic_just_filed_emits_no_actions():
    """Today's aicto-0g8 bug. Epic with no children must NOT trigger ship."""
    s = _state(_epic())
    assert reconcile(s) == []


def test_epic_just_filed_does_not_file_epic_merge():
    s = _state(_epic())
    assert _file_issues(reconcile(s), kind="merge", target="epic") == []


def test_after_breakdown_merge_files_plan_and_plan_review():
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
    )
    actions = reconcile(s)
    assert len(_file_issues(actions, kind="plan")) == 1
    assert len(_file_issues(actions, kind="review", target="plan")) == 1


def test_plan_filing_idempotent():
    """If a plan with the matching idem key already exists, do nothing."""
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1\nidem: file-plan:e1"),
        _issue("rp1", "review", role="reviewer", target="plan",
               description="epic: e1\nidem: file-review-plan:e1"),
    )
    actions = reconcile(s)
    assert _file_issues(actions, kind="plan") == []
    assert _file_issues(actions, kind="review", target="plan") == []


def test_after_plan_merge_files_dev_and_code_review_when_no_chunks():
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
    )
    actions = reconcile(s, plan_chunks_for=lambda _e: [])
    assert len(_file_issues(actions, kind="dev")) == 1
    assert len(_file_issues(actions, kind="review", target="code")) == 1


def test_after_plan_merge_files_one_pair_per_chunk():
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
    )
    chunks = [("A", "alpha"), ("B", "beta"), ("C", "gamma")]
    actions = reconcile(s, plan_chunks_for=lambda _e: chunks)
    assert len(_file_issues(actions, kind="dev")) == 3
    assert len(_file_issues(actions, kind="review", target="code")) == 3


def test_epic_mid_dev_does_not_ship():
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
        _issue("d1", "dev", role="developer",
               description="epic: e1", status="open"),
        _issue("rc1", "review", role="reviewer", target="code",
               description="epic: e1\nupstream: d1", status="open"),
    )
    assert _file_issues(reconcile(s), kind="merge", target="epic") == []


def _full_done_state(extra: tuple[Issue, ...] = ()):
    base = (
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
        _issue("d1", "dev", role="developer",
               description="epic: e1", status="closed"),
        _issue("rc1", "review", role="reviewer", target="code",
               description="epic: e1\nupstream: d1", status="closed"),
        _issue("cm1", "merge", target="code",
               description="epic: e1", status="closed"),
    )
    return _state(*base, *extra)


def test_epic_all_done_files_epic_merge():
    actions = reconcile(_full_done_state())
    epic_merges = _file_issues(actions, kind="merge", target="epic")
    assert len(epic_merges) == 1
    assert "idem: file-epic-merge:e1" in epic_merges[0].description


def test_epic_merge_idempotent():
    em = _issue(
        "em1", "merge", role="cto", target="epic",
        description="epic: e1\nidem: file-epic-merge:e1", status="open",
    )
    actions = reconcile(_full_done_state(extra=(em,)))
    assert _file_issues(actions, kind="merge", target="epic") == []


# ---------- Tests: dev sub-FSM (changes-requested / re-review) ----------

def test_changes_requested_finds_upstream_via_idem_fallback():
    """Review with no `upstream:` line, no `task/<id>` reference — only its
    idem key — must still resolve to the matching dev. (Today's stuck-epic
    case: aicto-6ud's review predated FilePair.)"""
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
        _issue("d1", "dev", role="developer", status="closed",
               description="epic: e1\nidem: file-dev:e1:A"),
        _issue("rc1", "review", role="reviewer", target="code",
               description="epic: e1\nidem: file-review-code:e1:A:round-1",
               status="closed", close_reason="changes-requested"),
    )
    actions = reconcile(s)
    labels = [
        a for a in actions
        if isinstance(a, AddLabel) and a.label == "needs-re-review" and a.issue_id == "d1"
    ]
    assert len(labels) == 1


def test_changes_requested_adds_needs_re_review_label():
    rev = _issue(
        "rc1", "review", role="reviewer", target="code",
        description="epic: e1\nupstream: d1", status="closed",
        close_reason="changes-requested",
    )
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
        _issue("d1", "dev", role="developer",
               description="epic: e1", status="closed"),
        rev,
    )
    actions = reconcile(s)
    label_actions = [
        a for a in actions
        if isinstance(a, AddLabel) and a.label == "needs-re-review" and a.issue_id == "d1"
    ]
    assert len(label_actions) == 1


def test_dev_with_needs_re_review_files_round_two():
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
        _issue("d1", "dev", role="developer",
               description="epic: e1", status="closed",
               labels_extra=("needs-re-review",)),
        _issue("rc1", "review", role="reviewer", target="code",
               description="epic: e1\nupstream: d1", status="closed",
               close_reason="changes-requested"),
    )
    actions = reconcile(s)
    new_reviews = _file_issues(actions, kind="review", target="code")
    assert len(new_reviews) == 1
    assert "round-2" in new_reviews[0].description
    strips = [
        a for a in actions
        if isinstance(a, RemoveLabel) and a.label == "needs-re-review" and a.issue_id == "d1"
    ]
    assert len(strips) == 1


def test_re_review_filing_idempotent():
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
        _issue("d1", "dev", role="developer",
               description="epic: e1", status="closed",
               labels_extra=("needs-re-review",)),
        _issue("rc1", "review", role="reviewer", target="code",
               description="epic: e1\nupstream: d1", status="closed",
               close_reason="changes-requested"),
        # Already filed round-2; reconciler must not file another.
        _issue("rc2", "review", role="reviewer", target="code",
               description="epic: e1\nupstream: d1\nidem: file-review-code:e1:d1:round-2",
               status="open"),
    )
    actions = reconcile(s)
    new_reviews = _file_issues(actions, kind="review", target="code")
    assert new_reviews == []


def test_epic_blocked_by_pending_re_review():
    """All explicit children closed but one dev still has needs-re-review."""
    s = _full_done_state(extra=(
        _issue("d2", "dev", role="developer",
               description="epic: e1", status="closed",
               labels_extra=("needs-re-review",)),
        _issue("rc2", "review", role="reviewer", target="code",
               description="epic: e1\nupstream: d2", status="closed",
               close_reason="changes-requested"),
    ))
    actions = reconcile(s)
    assert _file_issues(actions, kind="merge", target="epic") == []


def test_phase4_ignores_historical_changes_requested_when_later_round_approved():
    """Round-1 changes-requested + round-2 approved + code-merge closed →
    reconciler must not re-tag the dev with needs-re-review every tick."""
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
        _issue("d1", "dev", role="developer", status="closed",
               description="epic: e1\nidem: file-dev:e1:A"),
        _issue("rc1", "review", role="reviewer", target="code", status="closed",
               description="epic: e1\nidem: file-review-code:e1:A:round-1",
               close_reason="changes-requested"),
        _issue("rc2", "review", role="reviewer", target="code", status="closed",
               description="epic: e1\nupstream: d1\nidem: file-review-code:e1:d1:round-2",
               close_reason="approved"),
        _issue("cm1", "merge", target="code",
               description="epic: e1", status="closed"),
    )
    actions = reconcile(s)
    label_actions = [a for a in actions if isinstance(a, AddLabel)]
    assert label_actions == []
    # And ship is now allowed.
    assert len(_file_issues(actions, kind="merge", target="epic")) == 1


def test_epic_with_historical_changes_requested_but_no_merge_does_not_ship():
    """Round-1 review closed changes-requested, dev closed, but no code
    merge ever happened. Without the merge-count guard the ship gate would
    fire (this is the aicto-6ud stuck case)."""
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
        _issue("d1", "dev", role="developer", status="closed",
               description="epic: e1\nidem: file-dev:e1:A"),
        _issue("rc1", "review", role="reviewer", target="code",
               description="epic: e1\nidem: file-review-code:e1:A:round-1",
               status="closed", close_reason="changes-requested"),
        # No kind:merge,target:code exists.
    )
    actions = reconcile(s)
    assert _file_issues(actions, kind="merge", target="epic") == []


def test_epic_blocked_by_open_code_review():
    s = _full_done_state(extra=(
        _issue("d2", "dev", role="developer",
               description="epic: e1", status="closed"),
        _issue("rc2", "review", role="reviewer", target="code",
               description="epic: e1\nupstream: d2", status="open"),
    ))
    assert _file_issues(reconcile(s), kind="merge", target="epic") == []


# ---------- Tests: idempotency under replay ----------

def test_running_reconciler_twice_emits_nothing_second_time():
    """Apply the actions from tick 1 to the state, then run tick 2 — should
    produce no new FileIssue / FilePair actions for the same bug class."""
    s1 = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
    )
    first = reconcile(s1)
    assert _file_issues(first, kind="plan")
    new_issues = list(s1.issues)
    counter = 0
    for a in _all_file_issues(first):
        counter += 1
        new_issues.append(Issue(
            id=f"new-{counter}",
            title=a.title,
            description=a.description,
            status="open",
            labels=a.labels,
        ))
    s2 = State(issues=tuple(new_issues))
    second = reconcile(s2)
    assert all(not isinstance(a, (FileIssue, FilePair)) for a in second), \
        f"unexpected re-filing on second tick: {second}"


# ---------- Tests: ops-epic shortcut FSM ----------

def test_ops_epic_files_single_dev_no_review_no_pair():
    s = _state(_ops_epic())
    actions = reconcile(s)
    # Exactly one FileIssue (the dev). No FilePair, no review.
    file_issues = [a for a in actions if isinstance(a, FileIssue)]
    pairs = [a for a in actions if isinstance(a, FilePair)]
    assert len(file_issues) == 1
    assert pairs == []
    dev = file_issues[0]
    assert "kind:dev" in dev.labels
    assert "role:developer" in dev.labels
    assert "class:ops" in dev.labels


def test_ops_epic_skips_breakdown_plan_review_merge():
    s = _state(_ops_epic())
    actions = reconcile(s)
    # No breakdown-approval, no plan, no review, no merge actions.
    for a in _all_file_issues(actions):
        kinds = {l for l in a.labels if l.startswith("kind:")}
        assert kinds & {"kind:plan", "kind:breakdown",
                        "kind:review", "kind:merge", "kind:approval"} == set(), \
            f"ops epic emitted forbidden kind: {a}"


def test_ops_epic_idempotent():
    s = _state(
        _ops_epic(),
        _issue("d1", "dev", role="developer",
               description="epic: e-ops\nidem: file-ops-dev:e-ops",
               labels_extra=("class:ops",)),
    )
    actions = reconcile(s)
    assert [a for a in actions if isinstance(a, FileIssue)] == []


def test_ops_epic_closes_when_dev_closes():
    s = _state(
        _ops_epic(),
        _issue("d1", "dev", role="developer", status="closed",
               description="epic: e-ops\nidem: file-ops-dev:e-ops",
               labels_extra=("class:ops",)),
    )
    actions = reconcile(s)
    closes = [a for a in actions if isinstance(a, CloseIssue) and a.issue_id == "e-ops"]
    assert len(closes) == 1
    assert "d1" in closes[0].reason


def test_ops_epic_does_not_file_epic_merge():
    s = _state(
        _ops_epic(),
        _issue("d1", "dev", role="developer", status="closed",
               description="epic: e-ops",
               labels_extra=("class:ops",)),
    )
    actions = reconcile(s)
    epic_merges = _file_issues(actions, kind="merge", target="epic")
    assert epic_merges == []


# ---------- Tests: pair linkage (today's bug — review claimable too early) ----------

def test_plan_and_plan_review_filed_as_blocked_pair():
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
    )
    actions = reconcile(s)
    pairs = [a for a in actions if isinstance(a, FilePair)]
    assert len(pairs) == 1
    pair = pairs[0]
    assert "kind:plan" in pair.upstream.labels
    assert "kind:review" in pair.downstream.labels
    assert "target:plan" in pair.downstream.labels


def test_dev_and_code_review_filed_as_blocked_pair_per_chunk():
    s = _state(
        _epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
    )
    chunks = [("A", "alpha"), ("B", "beta")]
    actions = reconcile(s, plan_chunks_for=lambda _e: chunks)
    pairs = [a for a in actions if isinstance(a, FilePair)]
    assert len(pairs) == 2
    for p in pairs:
        assert "kind:dev" in p.upstream.labels
        assert "kind:review" in p.downstream.labels
        assert "target:code" in p.downstream.labels


# ---------- Helpers: bypass-cto ----------

def _bypass_epic(id: str = "e1", title: str = "Test epic", parent_branch: str = "main") -> Issue:
    return Issue(
        id=id, title=title,
        description=f"parent_branch: {parent_branch}",
        status="open",
        labels=("kind:epic", "role:manager", "class:bypass-cto"),
    )


# ---------- Tests: bypass-cto ----------

def test_bypass_epic_files_breakdown_merge_not_approval():
    s = _state(
        _bypass_epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
    )
    actions = reconcile(s)
    merges = _file_issues(actions, kind="merge", target="breakdown")
    approvals = _file_issues(actions, kind="approval", target="breakdown")
    assert len(merges) == 1
    assert approvals == []
    assert "file-breakdown-merge:e1:b1" in merges[0].description


def test_bypass_epic_files_plan_merge_not_approval():
    s = _state(
        _bypass_epic(),
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pr1", "review", role="reviewer", target="plan",
               description="epic: e1", status="closed", close_reason="approved"),
    )
    actions = reconcile(s)
    merges = _file_issues(actions, kind="merge", target="plan")
    approvals = _file_issues(actions, kind="approval", target="plan")
    assert len(merges) == 1
    assert approvals == []
    assert "file-plan-merge:e1:p1" in merges[0].description


def test_bypass_epic_targeting_main_files_cto_epic_merge():
    s = _full_done_state_bypass(parent_branch="main")
    actions = reconcile(s)
    epic_merges = _file_issues(actions, kind="merge", target="epic")
    auto_merges = [a for a in actions if isinstance(a, AutoMergeEpic)]
    assert len(epic_merges) == 1
    assert auto_merges == []


def test_bypass_epic_targeting_non_main_emits_auto_merge():
    s = _full_done_state_bypass(parent_branch="develop")
    actions = reconcile(s)
    epic_merges = _file_issues(actions, kind="merge", target="epic")
    auto_merges = [a for a in actions if isinstance(a, AutoMergeEpic)]
    assert epic_merges == []
    assert len(auto_merges) == 1
    assert auto_merges[0].epic_id == "e1"
    assert auto_merges[0].merge_target == "develop"


def _full_done_state_bypass(parent_branch: str = "main", extra: tuple[Issue, ...] = ()):
    epic = _bypass_epic(parent_branch=parent_branch)
    base = (
        epic,
        _issue("b1", "breakdown", description="epic: e1", status="closed"),
        _issue("bm1", "merge", target="breakdown",
               description="epic: e1", status="closed"),
        _issue("p1", "plan", role="developer",
               description="epic: e1", status="closed"),
        _issue("pm1", "merge", target="plan",
               description="epic: e1", status="closed"),
        _issue("d1", "dev", role="developer",
               description="epic: e1", status="closed"),
        _issue("rc1", "review", role="reviewer", target="code",
               description="epic: e1\nupstream: d1", status="closed"),
        _issue("cm1", "merge", target="code",
               description="epic: e1", status="closed"),
    )
    return _state(*base, *extra)


# ---------- Health watchdog tests ----------

import datetime as dt


def _issue_with_ts(
    id: str,
    kind: str,
    *,
    role: str = "manager",
    target: str | None = None,
    status: str = "open",
    description: str = "",
    title: str | None = None,
    labels_extra: tuple[str, ...] = (),
    close_reason: str = "",
    updated_at: str = "",
    assignee: str = "",
) -> Issue:
    labels = [f"kind:{kind}", f"role:{role}"]
    if target:
        labels.append(f"target:{target}")
    labels.extend(labels_extra)
    return Issue(
        id=id,
        title=title or f"{kind} {id}",
        description=description,
        status=status,
        labels=tuple(labels),
        close_reason=close_reason,
        updated_at=updated_at,
        assignee=assignee,
    )


def test_zombie_issue_gets_unclaimed():
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)).isoformat()
    epic = _epic()
    zombie = _issue_with_ts("z1", "dev", role="developer", status="in_progress", updated_at=old, assignee="demo:dev-1")
    s = _state(epic, zombie)
    actions = reconcile(s)
    labels = [a for a in actions if isinstance(a, AddLabel) and a.issue_id == "z1"]
    reopens = [a for a in actions if isinstance(a, __import__("reconciler", fromlist=["ReopenIssue"]).ReopenIssue) and a.issue_id == "z1"]
    assert len(labels) == 1
    assert labels[0].label == "stuck:zombie"
    assert len(reopens) == 1


def test_fresh_in_progress_not_flagged():
    recent = dt.datetime.now(dt.timezone.utc).isoformat()
    epic = _epic()
    fresh = _issue_with_ts("f1", "dev", role="developer", status="in_progress", updated_at=recent, assignee="demo:dev-1")
    s = _state(epic, fresh)
    actions = reconcile(s)
    labels = [a for a in actions if isinstance(a, AddLabel) and a.issue_id == "f1"]
    assert labels == []


def test_review_loop_escalation():
    epic = _epic()
    dev = _issue("d1", "dev", role="developer", description="epic: e1", status="closed")
    reviews = [
        _issue(f"r{i}", "review", role="reviewer", target="code",
               description=f"epic: e1\nupstream: d1", status="closed",
               close_reason="changes-requested")
        for i in range(1, 5)
    ]
    s = _state(epic, dev, *reviews)
    actions = reconcile(s)
    esc = [a for a in actions if isinstance(a, FileIssue) and a.title.startswith("Escalation:")]
    assert len(esc) == 1
    assert "4 review rounds" in esc[0].description


def test_stuck_epic_status_request():
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
    epic = Issue(
        id="e1", title="Stuck epic", description="", status="open",
        labels=("kind:epic", "role:manager"),
        updated_at=old,
    )
    s = _state(epic)
    actions = reconcile(s)
    reqs = [a for a in actions if isinstance(a, FileIssue) and a.title.startswith("Status request:")]
    assert len(reqs) == 1
    assert "kind:status-request" in reqs[0].labels


def test_missing_kind_label_auto_healed():
    epic = Issue(
        id="e1", title="Orphan", description="", status="open",
        labels=("role:manager",), issue_type="epic",
    )
    s = _state(epic)
    actions = reconcile(s)
    labels = [a for a in actions if isinstance(a, AddLabel)]
    assert any(a.issue_id == "e1" and a.label == "kind:epic" for a in labels)


def test_missing_role_label_auto_healed():
    epic = Issue(
        id="e1", title="Orphan", description="", status="open",
        labels=("kind:epic",), issue_type="epic",
    )
    s = _state(epic)
    actions = reconcile(s)
    labels = [a for a in actions if isinstance(a, AddLabel)]
    assert any(a.issue_id == "e1" and a.label == "role:manager" for a in labels)
