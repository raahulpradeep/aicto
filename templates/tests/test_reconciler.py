"""Fixture-based regression tests for the workflow reconciler.

Every recurring bug we hit becomes a fixture. Pure-Python — no live bd needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconciler import (  # noqa: E402
    AddLabel,
    FileIssue,
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


def _state(*issues: Issue) -> State:
    return State(issues=tuple(issues))


def _file_issues(actions, *, kind: str, target: str | None = None):
    out = []
    want_kind = f"kind:{kind}"
    want_target = f"target:{target}" if target else None
    for a in actions:
        if not isinstance(a, FileIssue):
            continue
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
    produce no new FileIssue actions for the same bug class."""
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
    for a in first:
        if isinstance(a, FileIssue):
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
    assert all(not isinstance(a, FileIssue) for a in second), \
        f"unexpected re-filing on second tick: {second}"
