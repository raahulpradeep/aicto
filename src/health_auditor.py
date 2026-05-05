"""Health auditor — detects workflow violations and auto-heals or escalates.

Replaces the bulk of the old reconciler's per-epic logic.  The auditor
looks at the *derived* state (from workflow.compute_state) and decides
whether anything is stuck, orphaned, or inconsistent.

Design doc: plans/reconciler-redesign.md
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from workflow import EpicState, Issue, State, compute_state


# ---------------------------------------------------------------------------
# Heal action types
# ---------------------------------------------------------------------------

class HealKind(Enum):
    FILE_REVIEW = auto()
    FILE_MERGE = auto()
    FILE_PLAN_REVIEW_PAIR = auto()
    FILE_BREAKDOWN_MERGE = auto()
    FILE_PLAN_MERGE = auto()
    FILE_CODE_MERGE = auto()
    FILE_EPIC_MERGE = auto()
    ADD_LABEL = auto()
    REMOVE_LABEL = auto()
    REOPEN = auto()
    CLOSE = auto()
    ESCALATE_CTO = auto()
    RESET_CLAIM = auto()
    AUTO_MERGE_EPIC = auto()


@dataclass(frozen=True)
class HealAction:
    kind: HealKind
    issue_id: str = ""
    target_id: str = ""
    label: str = ""
    reason: str = ""
    epic_id: str = ""
    merge_target: str = ""

    @property
    def is_auto_safe(self) -> bool:
        """True if the action can be executed without human confirmation."""
        return self.kind in {
            HealKind.ADD_LABEL,
            HealKind.REMOVE_LABEL,
            HealKind.RESET_CLAIM,
            HealKind.FILE_REVIEW,
            HealKind.FILE_MERGE,
            HealKind.FILE_PLAN_REVIEW_PAIR,
            HealKind.FILE_BREAKDOWN_MERGE,
            HealKind.FILE_PLAN_MERGE,
            HealKind.FILE_CODE_MERGE,
            HealKind.AUTO_MERGE_EPIC,
        }


# ---------------------------------------------------------------------------
# Health Auditor
# ---------------------------------------------------------------------------

ZOMBIE_THRESHOLD_SECONDS = 900   # 15 minutes
ORPHAN_THRESHOLD_SECONDS = 900   # 15 minutes
STUCK_EPIC_THRESHOLD_SECONDS = 3600  # 1 hour
REVIEW_LOOP_THRESHOLD = 3


class HealthAuditor:
    """Detects workflow violations and produces HealActions.

    The auditor is intentionally conservative:
    * auto-safe actions are emitted silently
    * escalations surface to the CTO inbox
    """

    def audit(self, state: State) -> list[HealAction]:
        """Audit entire state and return ordered list of heal actions."""
        actions: list[HealAction] = []
        now = datetime.datetime.now(datetime.timezone.utc)

        actions.extend(self._audit_health(state, now))
        for epic in state.epics():
            if not epic.is_open:
                continue
            actions.extend(self._audit_epic(epic, state, now))
        actions.extend(self._audit_leaks(state, now))
        return actions

    # ------------------------------------------------------------------
    # Per-epic audit
    # ------------------------------------------------------------------

    def _audit_epic(self, epic: Issue, state: State, now: datetime.datetime) -> list[HealAction]:
        actions: list[HealAction] = []
        children = state.children_of(epic.id)
        epic_state = compute_state(epic, children, state)

        # V1: CREATED but no breakdown after 15min
        age = self._age_seconds(epic, now)
        if epic_state == EpicState.CREATED and age is not None and age > ORPHAN_THRESHOLD_SECONDS:
            actions.append(HealAction(
                kind=HealKind.ESCALATE_CTO,
                issue_id=epic.id,
                reason="No breakdown filed after 15 minutes",
            ))

        # V2: BREAKDOWN_DONE but no plan+plan-review pair filed
        # (This catches the case where the manager closed breakdown but didn't file next)
        if epic_state == EpicState.BREAKDOWN_DONE:
            # Check if we actually have any plans filed; if not, escalate
            plans = [c for c in children if c.kind == "plan"]
            if not plans:
                actions.append(HealAction(
                    kind=HealKind.ESCALATE_CTO,
                    issue_id=epic.id,
                    reason="Breakdown merged but no plan filed",
                ))

        # V3: PLAN_DONE but no devs filed
        if epic_state == EpicState.PLAN_DONE:
            devs = [c for c in children if c.kind == "dev"]
            if not devs:
                actions.append(HealAction(
                    kind=HealKind.ESCALATE_CTO,
                    issue_id=epic.id,
                    reason="Plan merged but no dev tasks filed",
                ))

        # V4: DEV_DONE but no review filed (agent forgot to file next step)
        devs = [c for c in children if c.kind == "dev"]
        for dev in devs:
            if not dev.is_open:
                has_review = any(
                    r.kind == "review" and r.target == "code"
                    and self._upstream_matches(r, dev.id, state)
                    for r in state.issues
                )
                if not has_review:
                    actions.append(HealAction(
                        kind=HealKind.FILE_REVIEW,
                        target_id=dev.id,
                        epic_id=epic.id,
                        reason=f"Dev {dev.id} closed but no review filed",
                    ))

        # V5: Review approved but no merge filed (reviewer forgot to file next step)
        code_reviews = [c for c in children if c.kind == "review" and c.target == "code"]
        for rev in code_reviews:
            if not rev.is_open and not rev.changes_requested():
                upstream_id = self._upstream_of_review(rev, state)
                if upstream_id:
                    has_merge = any(
                        m.kind == "merge" and m.target == "code"
                        and upstream_id in (m.description or "")
                        for m in state.issues
                    )
                    if not has_merge:
                        actions.append(HealAction(
                            kind=HealKind.FILE_MERGE,
                            target_id=upstream_id,
                            epic_id=epic.id,
                            reason=f"Review {rev.id} approved but no code merge filed",
                        ))

        # V6: Changes-requested loop detection (>3 review rounds per dev)
        # MOVED to _audit_health so it runs globally (not just per-epic children).

        return actions

    # ------------------------------------------------------------------
    # Global health checks
    # ------------------------------------------------------------------

    def _audit_health(self, state: State, now: datetime.datetime) -> list[HealAction]:
        actions: list[HealAction] = []

        # H1: Zombie issue detection — in_progress for >15 min with no updates.
        for issue in state.issues:
            if issue.status != "in_progress":
                continue
            age = self._age_seconds(issue, now)
            if age is None:
                continue
            if age > ZOMBIE_THRESHOLD_SECONDS:
                actions.append(HealAction(
                    kind=HealKind.RESET_CLAIM,
                    issue_id=issue.id,
                    reason=f"Zombie detected (>15m in_progress)",
                ))

        # H5: Missing label detection (skip closed issues).
        for issue in state.issues:
            if not issue.is_open:
                continue
            if issue.issue_type == "epic" and issue.kind != "epic":
                actions.append(HealAction(
                    kind=HealKind.ADD_LABEL,
                    issue_id=issue.id,
                    label="kind:epic",
                    reason="Missing kind:epic label",
                ))
            if issue.kind == "epic" and issue.role != "manager":
                actions.append(HealAction(
                    kind=HealKind.ADD_LABEL,
                    issue_id=issue.id,
                    label="role:manager",
                    reason="Missing role:manager label",
                ))
            if issue.kind in ("dev", "plan") and not issue.role:
                actions.append(HealAction(
                    kind=HealKind.ADD_LABEL,
                    issue_id=issue.id,
                    label="role:developer",
                    reason="Missing role:developer label",
                ))
            if issue.kind == "review" and not issue.role:
                actions.append(HealAction(
                    kind=HealKind.ADD_LABEL,
                    issue_id=issue.id,
                    label="role:reviewer",
                    reason="Missing role:reviewer label",
                ))

        # H2: Changes-requested loop detection — >3 review rounds per dev.
        devs = [i for i in state.issues if i.kind == "dev"]
        for dev in devs:
            reviews = [
                r for r in state.issues
                if r.kind == "review" and r.target == "code"
                and self._upstream_matches(r, dev.id, state)
            ]
            if len(reviews) > REVIEW_LOOP_THRESHOLD:
                actions.append(HealAction(
                    kind=HealKind.ESCALATE_CTO,
                    issue_id=dev.id,
                    epic_id=dev.linked_epic() or "",
                    reason=f"Dev {dev.id} stuck in review loop ({len(reviews)} rounds)",
                ))

        return actions

    # ------------------------------------------------------------------
    # Leak detection (orphaned epics)
    # ------------------------------------------------------------------

    def _audit_leaks(self, state: State, now: datetime.datetime) -> list[HealAction]:
        actions: list[HealAction] = []
        for epic in state.epics():
            if not epic.is_open:
                continue
            children = state.children_of(epic.id)
            if children:
                continue
            age = self._age_seconds(epic, now)
            if age is None or age < ORPHAN_THRESHOLD_SECONDS:
                continue
            actions.append(HealAction(
                kind=HealKind.ESCALATE_CTO,
                issue_id=epic.id,
                reason=f"Epic has no sub-tasks after {int(age//60)}m",
            ))
        return actions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _age_seconds(issue: Issue, now: datetime.datetime) -> Optional[int]:
        ts_str = issue.updated_at or issue.created_at
        if not ts_str:
            return None
        try:
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return int((now - ts).total_seconds())
        except ValueError:
            return None

    @staticmethod
    def _upstream_of_review(review: Issue, state: State) -> Optional[str]:
        import re
        m = re.search(r"^upstream:\s*(\S+)\s*$", review.description, re.MULTILINE)
        if m:
            return m.group(1)
        m = re.search(r"\btask/([A-Za-z0-9-]+)", review.description)
        if m:
            cand = m.group(1)
            if state.by_id(cand):
                return cand
        return None

    @staticmethod
    def _upstream_matches(review: Issue, dev_id: str, state: State) -> bool:
        uid = HealthAuditor._upstream_of_review(review, state)
        return uid == dev_id
