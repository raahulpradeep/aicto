"""Explicit workflow state machine for the AI CTO orchestrator.

Replaces the scattered ad-hoc conditionals in the old reconciler with a
pure, testable state graph.  Each epic's state is derived from its
children (breakdowns, plans, devs, reviews, merges) — no idempotency
hacks needed.

Design doc: plans/reconciler-redesign.md
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Optional


class EpicState(Enum):
    """Canonical states in the epic lifecycle."""

    CREATED = auto()
    BREAKDOWN_OPEN = auto()
    BREAKDOWN_DONE = auto()
    PLAN_OPEN = auto()
    PLAN_DONE = auto()
    DEV_IN_PROGRESS = auto()
    DEV_DONE = auto()
    REVIEW_IN_PROGRESS = auto()
    CHANGES_REQUESTED = auto()
    MERGE_READY = auto()
    SHIP_READY = auto()
    SHIPPED = auto()

    # Sentinel used when we can't determine state (should never happen)
    UNKNOWN = auto()


# ---------------------------------------------------------------------------
# Transition graph (documentation + validation)
# ---------------------------------------------------------------------------

TRANSITIONS: dict[EpicState, list[EpicState]] = {
    EpicState.CREATED: [EpicState.BREAKDOWN_OPEN],
    EpicState.BREAKDOWN_OPEN: [EpicState.BREAKDOWN_DONE],
    EpicState.BREAKDOWN_DONE: [EpicState.PLAN_OPEN],
    EpicState.PLAN_OPEN: [EpicState.PLAN_DONE],
    EpicState.PLAN_DONE: [EpicState.DEV_IN_PROGRESS],
    EpicState.DEV_IN_PROGRESS: [EpicState.DEV_DONE, EpicState.CHANGES_REQUESTED],
    EpicState.DEV_DONE: [EpicState.REVIEW_IN_PROGRESS],
    EpicState.REVIEW_IN_PROGRESS: [EpicState.MERGE_READY, EpicState.CHANGES_REQUESTED],
    EpicState.CHANGES_REQUESTED: [EpicState.DEV_IN_PROGRESS],
    EpicState.MERGE_READY: [EpicState.SHIP_READY],
    EpicState.SHIP_READY: [EpicState.SHIPPED],
    EpicState.SHIPPED: [],
    EpicState.UNKNOWN: [],
}


def allowed_next_states(state: EpicState) -> list[EpicState]:
    """Return the states that *may* follow *state* according to the graph."""
    return list(TRANSITIONS.get(state, []))


def is_transition_allowed(from_state: EpicState, to_state: EpicState) -> bool:
    return to_state in allowed_next_states(from_state)


# ---------------------------------------------------------------------------
# Domain types (mirror reconciler types for interoperability)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Issue:
    id: str
    title: str
    description: str
    status: str  # "open" | "in_progress" | "closed"
    labels: tuple[str, ...]
    close_reason: str = ""
    issue_type: str = ""
    created_at: str = ""
    updated_at: str = ""
    assignee: str = ""

    @property
    def kind(self) -> Optional[str]:
        for lbl in self.labels:
            if lbl.startswith("kind:"):
                return lbl.split(":", 1)[1]
        return None

    @property
    def target(self) -> Optional[str]:
        for lbl in self.labels:
            if lbl.startswith("target:"):
                return lbl.split(":", 1)[1]
        return None

    @property
    def role(self) -> Optional[str]:
        for lbl in self.labels:
            if lbl.startswith("role:"):
                return lbl.split(":", 1)[1]
        return None

    @property
    def is_open(self) -> bool:
        return self.status != "closed"

    def has_label(self, label: str) -> bool:
        return label in self.labels

    def linked_epic(self) -> Optional[str]:
        m = re.search(r"^epic:\s*(\S+)\s*$", self.description, re.MULTILINE)
        return m.group(1) if m else None

    def changes_requested(self) -> bool:
        return (
            "changes-requested" in (self.close_reason or "")
            or "verdict:changes-requested" in self.labels
        )

    @property
    def is_ops(self) -> bool:
        return "class:ops" in self.labels

    @property
    def is_bypass_cto(self) -> bool:
        return "class:bypass-cto" in self.labels

    @property
    def parent_branch(self) -> str:
        m = re.search(
            r"^parent_branch:\s*(\S+)\s*$", self.description, re.MULTILINE
        )
        return m.group(1) if m else "main"


@dataclass(frozen=True)
class State:
    issues: tuple[Issue, ...]

    def by_id(self, id: str) -> Optional[Issue]:
        for i in self.issues:
            if i.id == id:
                return i
        return None

    def epics(self) -> list[Issue]:
        return [
            i for i in self.issues
            if i.kind == "epic" or i.issue_type == "epic"
        ]

    def children_of(self, epic_id: str) -> list[Issue]:
        return [
            i for i in self.issues
            if i.linked_epic() == epic_id and i.id != epic_id
        ]


# ---------------------------------------------------------------------------
# Pure state computation
# ---------------------------------------------------------------------------

def _upstream_of_review(review: Issue, state: State) -> Optional[str]:
    """Derive the upstream dev/plan id a review covers."""
    m = re.search(r"^upstream:\s*(\S+)\s*$", review.description, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r"\btask/([A-Za-z0-9-]+)", review.description)
    if m:
        cand = m.group(1)
        if state.by_id(cand):
            return cand
    m = re.search(
        r"^idem:\s*file-review-code:([^:]+):([^:]+):",
        review.description,
        re.MULTILINE,
    )
    if m:
        epic_id, slot = m.group(1), m.group(2)
        dev_idem = f"idem: file-dev:{epic_id}:{slot}"
        for i in state.issues:
            if i.kind == "dev" and dev_idem in i.description:
                return i.id
    return None


def compute_state(epic: Issue, children: list[Issue], state: State) -> EpicState:
    """Derive the canonical EpicState from an epic and its children.

    This is a **pure function** — no side effects, no I/O, no bd calls.
    Perfect for unit testing.
    """
    # ---- Ops shortcut ----
    if epic.is_ops:
        devs = [c for c in children if c.kind == "dev"]
        if not devs:
            return EpicState.DEV_IN_PROGRESS
        if all(not d.is_open for d in devs):
            return EpicState.SHIPPED
        return EpicState.DEV_IN_PROGRESS

    breakdowns = [c for c in children if c.kind == "breakdown"]
    breakdown_merges = [
        c for c in children if c.kind == "merge" and c.target == "breakdown"
    ]
    breakdown_merges_closed = [c for c in breakdown_merges if not c.is_open]

    plans = [c for c in children if c.kind == "plan"]
    plan_merges = [
        c for c in children if c.kind == "merge" and c.target == "plan"
    ]
    plan_merges_closed = [c for c in plan_merges if not c.is_open]

    devs = [c for c in children if c.kind == "dev"]
    code_reviews = [
        c for c in children if c.kind == "review" and c.target == "code"
    ]
    code_merges = [
        c for c in children if c.kind == "merge" and c.target == "code"
    ]
    epic_merges = [
        c for c in children if c.kind == "merge" and c.target == "epic"
    ]

    # ---- Phase 1: no breakdown yet ----
    if not breakdowns:
        return EpicState.CREATED

    # ---- Phase 1.5: breakdown filed but not merged ----
    if not breakdown_merges_closed:
        return EpicState.BREAKDOWN_OPEN

    # ---- Phase 2: after breakdown merge, plan not yet filed ----
    if not plans:
        return EpicState.BREAKDOWN_DONE

    # ---- Phase 2.5: plan filed but not merged ----
    if not plan_merges_closed:
        return EpicState.PLAN_OPEN

    # ---- Phase 3: after plan merge, dev not yet filed ----
    if not devs:
        return EpicState.PLAN_DONE

    # ---- Phase 4: devs filed; are they all closed? ----
    any_dev_open = any(d.is_open for d in devs)
    any_dev_needs_re_review = any(d.has_label("needs-re-review") for d in devs)

    if any_dev_open or any_dev_needs_re_review:
        return EpicState.DEV_IN_PROGRESS

    # ---- Phase 5: all devs closed; reviews in flight? ----
    any_review_open = any(r.is_open for r in code_reviews)

    # Check for changes-requested on the latest review per upstream
    by_upstream: dict[str, list[Issue]] = {}
    for rev in code_reviews:
        if rev.is_open:
            continue
        u = _upstream_of_review(rev, state)
        if u:
            by_upstream.setdefault(u, []).append(rev)

    latest_changes_requested = False
    for upstream_id, revs in by_upstream.items():
        revs.sort(key=lambda r: _review_round_number(r))
        latest = revs[-1]
        if latest.changes_requested():
            latest_changes_requested = True
            break

    # Also detect any review with changes-requested that lacks an upstream link
    if not latest_changes_requested:
        for rev in code_reviews:
            if rev.is_open:
                continue
            if rev.changes_requested():
                latest_changes_requested = True
                break

    if any_review_open:
        return EpicState.REVIEW_IN_PROGRESS

    if latest_changes_requested:
        return EpicState.CHANGES_REQUESTED

    # If no reviews exist yet but all devs closed → DEV_DONE (waiting for review filing)
    if not code_reviews:
        return EpicState.DEV_DONE

    # ---- Phase 5.5: all reviews closed & approved; code merges? ----
    any_code_merge_open = any(m.is_open for m in code_merges)
    if any_code_merge_open:
        return EpicState.MERGE_READY

    # Need at least as many code merges as devs to be fully merged
    code_merges_closed = [m for m in code_merges if not m.is_open]
    if len(code_merges_closed) < len(devs):
        return EpicState.MERGE_READY

    # ---- Phase 6: ship gate ----
    any_epic_merge_open = any(m.is_open for m in epic_merges)
    if any_epic_merge_open:
        return EpicState.SHIP_READY

    # Everything closed — epic is ready to ship (or already shipped)
    if epic.status == "closed":
        return EpicState.SHIPPED

    return EpicState.SHIP_READY


def _review_round_number(review: Issue) -> int:
    m = re.search(r"round-(\d+)", review.description)
    return int(m.group(1)) if m else 1


# ---------------------------------------------------------------------------
# Plan chunk helper (moved here from reconciler for reuse)
# ---------------------------------------------------------------------------

def parse_plan_chunks(
    epic_id: str, root: Optional[str] = None
) -> list[tuple[str, str]]:
    """Find chunk markers in a merged plan."""
    from pathlib import Path

    root_path = Path(root or ".")
    candidates = [
        root_path / ".cto" / "worktrees" / epic_id / "plans" / f"{epic_id}.md",
        root_path / "plans" / f"{epic_id}.md",
    ]
    for p in candidates:
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            return [
                (m.group(1), m.group(2).strip())
                for m in re.finditer(
                    r"^#{2,3}\s+(?:Dev|Chunk)\s+([A-Z])\s*[:—\-]\s*(.+)$",
                    text,
                    re.MULTILINE,
                )
            ]
    return []


# ---------------------------------------------------------------------------
# Backward-compatible shim
# ---------------------------------------------------------------------------

def compute_state_compat(
    epic: Issue,
    state: State,
    plan_chunks_for: Optional[Callable[[str], list[tuple[str, str]]]] = None,
) -> EpicState:
    """Wrapper that matches the old reconcile_epic signature for gradual migration."""
    children = state.children_of(epic.id)
    return compute_state(epic, children, state)
