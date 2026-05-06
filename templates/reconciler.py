#!/usr/bin/env python3
# RECONCILER_TEMPLATE_SHA: managed by `bin/cto`. Do not edit per-team copies.
"""Health reconciler for the AI CTO orchestrator.

Stripped down from the full workflow reconciler. Agents now own all
workflow transitions (filing approvals, reviews, merges, etc.). This
module only detects and auto-heals stuck/broken/orphaned state:

- Zombie issues (in_progress >15 min with no updates) → auto-unclaim
- Review loops (>3 rounds per dev) → CTO escalation
- Missing workflow labels → auto-heal
- Stuck epics (no children, idle >1h) → status request
- Leaked epics (no children >15 min) → leak detection

The execute layer turns actions into bd CLI calls. Idempotency keys
embedded in issue descriptions prevent duplicate filings if a tick is
replayed.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from event_bus import EventBus
from state_store import StateStore

# Re-export Phase 3/4 components for callers that import them off `reconciler`.
from health_auditor import HealthAuditor, HealKind, HealAction  # noqa: F401
from action_queue import ActionQueue  # noqa: F401


# ---------- Domain types ----------

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
        for l in self.labels:
            if l.startswith("kind:"):
                return l.split(":", 1)[1]
        return None

    @property
    def target(self) -> Optional[str]:
        for l in self.labels:
            if l.startswith("target:"):
                return l.split(":", 1)[1]
        return None

    @property
    def role(self) -> Optional[str]:
        for l in self.labels:
            if l.startswith("role:"):
                return l.split(":", 1)[1]
        return None

    @property
    def is_open(self) -> bool:
        return self.status != "closed"

    def has_label(self, label: str) -> bool:
        return label in self.labels

    def linked_epic(self) -> Optional[str]:
        m = re.search(r"^epic:\s*(\S+)\s*$", self.description, re.MULTILINE)
        return m.group(1) if m else None

    def has_idem(self, key: str) -> bool:
        return f"idem: {key}" in self.description

    def changes_requested(self) -> bool:
        return (
            "changes-requested" in (self.close_reason or "")
            or "verdict:changes-requested" in self.labels
        )


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

    def has_idem(self, key: str) -> bool:
        return any(i.has_idem(key) for i in self.issues)


# ---------- Action types ----------

@dataclass(frozen=True)
class FileIssue:
    title: str
    description: str  # MUST contain a single line of the form "idem: <key>"
    labels: tuple[str, ...]
    priority: int = 2
    blocks: Optional[str] = None


@dataclass(frozen=True)
class AddLabel:
    issue_id: str
    label: str


@dataclass(frozen=True)
class RemoveLabel:
    issue_id: str
    label: str


@dataclass(frozen=True)
class ReopenIssue:
    issue_id: str
    comment: str


@dataclass(frozen=True)
class CloseIssue:
    issue_id: str
    reason: str


Action = Union[FileIssue, AddLabel, RemoveLabel, ReopenIssue, CloseIssue]


# ---------- Idempotency keys ----------

def idem(*parts: str) -> str:
    return ":".join(parts)


# ---------- Health checks ----------

def _reconcile_health(state: State) -> list[Action]:
    """Detect and auto-heal stuck/broken/orphaned state.

    Emits healing actions silently; only review-loop escalations surface
    to the CTO inbox.
    """
    actions: list[Action] = []
    now = datetime.datetime.now(datetime.timezone.utc)

    # H1: Zombie issue detection — in_progress for >15 min with no updates.
    for issue in state.issues:
        if issue.status != "in_progress":
            continue
        ts_str = issue.updated_at or issue.created_at
        if not ts_str:
            continue
        try:
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age = (now - ts).total_seconds()
        except ValueError:
            continue
        if age > 900:  # 15 minutes
            # Auto-unclaim silently.
            actions.append(AddLabel(issue.id, "stuck:zombie"))
            actions.append(
                # Reopen to clear in_progress status.
                ReopenIssue(issue.id, "auto-heal: zombie detected, resetting claim")
            )
            _emit_event(
                "stuck_detected",
                issue_id=issue.id,
                reason="zombie (>15m in_progress)",
                auto_heal_action="unclaim",
            )

    # H2: Changes-requested loop detection — >3 review rounds per dev.
    devs = [i for i in state.issues if i.kind == "dev"]
    for dev in devs:
        reviews = [
            r for r in state.issues
            if r.kind == "review" and r.target == "code"
            and _upstream_of_review(r, state) == dev.id
        ]
        if len(reviews) > 3:
            # File a CTO escalation (only once per dev).
            esc_idem = idem("escalate-review-loop", dev.id)
            if not state.has_idem(esc_idem):
                actions.append(FileIssue(
                    title=f"Escalation: {dev.title} stuck in review loop",
                    description=(
                        f"epic: {dev.linked_epic() or ''}\n"
                        f"dev: {dev.id}\n"
                        f"idem: {esc_idem}\n"
                        f"This dev has gone through {len(reviews)} review rounds. "
                        f"Consider manual intervention or merging with known issues."
                    ),
                    labels=("role:cto", "kind:escalation"),
                    priority=1,
                ))

    # H5: Missing label detection.
    for issue in state.issues:
        if issue.issue_type == "epic" and issue.kind != "epic":
            actions.append(AddLabel(issue.id, "kind:epic"))
        if issue.kind == "epic" and issue.role != "manager":
            actions.append(AddLabel(issue.id, "role:manager"))
        if issue.kind in ("dev", "plan") and not issue.role:
            actions.append(AddLabel(issue.id, "role:developer"))
        if issue.kind == "review" and not issue.role:
            actions.append(AddLabel(issue.id, "role:reviewer"))

    # H6: Stuck epic detection — no children, no activity for >1h.
    for epic in state.epics():
        if not epic.is_open:
            continue
        children = state.children_of(epic.id)
        if children:
            continue
        ts_str = epic.updated_at or epic.created_at
        if not ts_str:
            continue
        try:
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age = (now - ts).total_seconds()
        except ValueError:
            continue
        if age > 3600:  # 1 hour
            req_idem = idem("status-request-stuck", epic.id)
            if not state.has_idem(req_idem):
                actions.append(FileIssue(
                    title=f"Status request: {epic.title}",
                    description=(
                        f"epic: {epic.id}\n"
                        f"idem: {req_idem}\n"
                        f"Epic has been idle for {int(age//60)} minutes. "
                        f"Please provide a fresh status digest."
                    ),
                    labels=("role:manager", "kind:status-request"),
                    priority=2,
                ))

    return actions


def _reconcile_leaks(state: State) -> list[Action]:
    """Detect open epics that have been orphaned (no children) for too long,
    and auto-heal epics created without workflow labels."""
    actions: list[Action] = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for epic in state.epics():
        if not epic.is_open:
            continue

        # Auto-heal missing labels so the epic becomes visible to the
        # normal workflow engine on the next tick.
        if epic.issue_type == "epic" and epic.kind != "epic":
            actions.append(AddLabel(epic.id, "kind:epic"))
            if epic.role != "manager":
                actions.append(AddLabel(epic.id, "role:manager"))

        children = state.children_of(epic.id)
        if children:
            continue

        # Only alert after a 15-minute grace period for new epics.
        try:
            created = datetime.datetime.fromisoformat(
                epic.created_at.replace("Z", "+00:00")
            )
            age_seconds = (now - created).total_seconds()
        except Exception:
            age_seconds = 0

        if age_seconds < 900:
            continue

        idem_key = idem("leak-detect", epic.id)
        if state.has_idem(idem_key):
            continue

        actions.append(FileIssue(
            title=f"Leak: epic {epic.id} has no sub-tasks after {int(age_seconds/60)}m",
            description=(
                f"epic: {epic.id}\n"
                f"idem: {idem_key}\n"
                f"Epic '{epic.title}' has been open for {int(age_seconds/60)} minutes "
                f"with no breakdown, plan, or dev tasks filed. "
                f"It may have been orphaned due to missing labels or was filed "
                f"as a regular epic when it should have been ops."
            ),
            labels=("role:cto", "kind:leak-detect"),
            priority=1,
        ))
    return actions


def reconcile(state: State) -> list[Action]:
    out: list[Action] = []
    out.extend(_reconcile_health(state))
    out.extend(_reconcile_leaks(state))
    return out


# ---------- Helpers ----------

def _upstream_of_review(review: Issue, state: State) -> Optional[str]:
    """Derive the upstream dev issue id from a review's description."""
    m = re.search(r"^upstream:\s*(\S+)\s*$", review.description, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r"\btask/([A-Za-z0-9-]+)", review.description)
    if m:
        cand = m.group(1)
        if state.by_id(cand):
            return cand
    # Fallback: derive upstream via idem-key shape.
    m = re.search(r"^idem:\s*file-review-code:([^:]+):([^:]+):", review.description, re.MULTILINE)
    if m:
        epic_id, slot = m.group(1), m.group(2)
        dev_idem = f"idem: file-dev:{epic_id}:{slot}"
        for i in state.issues:
            if i.kind == "dev" and dev_idem in i.description:
                return i.id
    return None


# ---------- Telemetry helpers ----------

def _emit_event(event_type: str, **kwargs: Any) -> None:
    """Best-effort event emission via the team's telemetry helper."""
    helper = Path(".cto/telemetry_helper.py")
    if not helper.exists():
        return
    kvs = []
    for k, v in kwargs.items():
        kvs.append(f"--kv={k}={v}")
    subprocess.run(
        [sys.executable, str(helper), str(Path(".cto/activity.jsonl")), event_type, *kvs],
        capture_output=True,
    )


# ---------- Execute (live bd calls) ----------

def execute(actions: list[Action], dry_run: bool = False, team_dir: Optional[Path] = None) -> list[str]:
    import time
    t0 = time.monotonic()
    log: list[str] = []

    # Phase 2: initialise event bus for publishing workflow events.
    bus: Optional[EventBus] = None
    if team_dir is not None:
        state_dir = team_dir / ".cto" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        db_path = state_dir / "agent_state.db"
        store = StateStore(str(db_path))
        bus = EventBus(store, watch_dir=state_dir)

    def _publish(kind: str, **kwargs: Any) -> None:
        if bus is not None:
            try:
                bus.publish("task.lifecycle", {"kind": kind, **kwargs})
            except Exception:
                pass  # best-effort; never let event bus break bd filing

    for a in actions:
        if isinstance(a, FileIssue):
            log.append(f"file: {a.title!r} labels={list(a.labels)}")
            if not dry_run:
                new_id = _bd_file(a)
                if new_id:
                    kind = ""
                    for lbl in a.labels:
                        if lbl.startswith("kind:"):
                            kind = lbl.split(":", 1)[1]
                            break
                    _publish("task.created", task_id=new_id, kind=kind, title=a.title)
        elif isinstance(a, AddLabel):
            log.append(f"label+: {a.issue_id} +{a.label}")
            if not dry_run:
                subprocess.run(
                    ["bd", "update", a.issue_id, "--add-label", a.label],
                    check=False, capture_output=True,
                )
                if "verdict:approved" in a.label:
                    _publish("approval.granted", issue_id=a.issue_id)
        elif isinstance(a, RemoveLabel):
            log.append(f"label-: {a.issue_id} -{a.label}")
            if not dry_run:
                subprocess.run(
                    ["bd", "update", a.issue_id, "--remove-label", a.label],
                    check=False, capture_output=True,
                )
        elif isinstance(a, ReopenIssue):
            log.append(f"reopen: {a.issue_id}")
            if not dry_run:
                subprocess.run(
                    ["bd", "reopen", a.issue_id],
                    check=False, capture_output=True,
                )
        elif isinstance(a, CloseIssue):
            log.append(f"close: {a.issue_id} reason={a.reason!r}")
            if not dry_run:
                subprocess.run(
                    ["bd", "close", a.issue_id, "-r", a.reason],
                    check=False, capture_output=True,
                )
                _publish("task.closed", issue_id=a.issue_id, reason=a.reason)
    if not dry_run:
        dur_ms = int((time.monotonic() - t0) * 1000)
        _emit_event("reconciler_tick", actions=";".join(log), duration_ms=str(dur_ms))
    return log


def _bd_file(action: FileIssue) -> Optional[str]:
    cmd = [
        "bd", "create",
        "-t", "task",
        "-l", ",".join(action.labels),
        "-p", str(action.priority),
        "-d", action.description,
        action.title,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    new_id: Optional[str] = None
    for tok in (r.stdout or "").split():
        if "-" in tok and tok.replace("-", "").isalnum():
            new_id = tok
            break
    if new_id:
        epic_id = ""
        m = re.search(r"^epic:\s*(\S+)\s*$", action.description, re.MULTILINE)
        if m:
            epic_id = m.group(1)
        kind = ""
        for lbl in action.labels:
            if lbl.startswith("kind:"):
                kind = lbl.split(":", 1)[1]
                break
        _emit_event(
            "issue_created",
            issue_id=new_id,
            kind=kind,
            title=action.title,
            labels=",".join(action.labels),
            epic_id=epic_id,
        )
        if action.blocks:
            subprocess.run(
                ["bd", "dep", new_id, "--blocks", action.blocks],
                check=False, capture_output=True,
            )
    return new_id


# ---------- Load state from bd ----------

def load_state() -> State:
    seen: dict[str, Issue] = {}
    for status_args in (
        ["--status", "open"],
        ["--status", "in_progress"],
        ["--status", "closed", "--limit", "200"],
    ):
        r = subprocess.run(
            ["bd", "list", *status_args, "--json"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            continue
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        for raw in data:
            id_ = raw.get("id")
            if not id_:
                continue
            seen[id_] = Issue(
                id=id_,
                title=raw.get("title", ""),
                description=raw.get("description", ""),
                status=raw.get("status", "open"),
                labels=tuple(raw.get("labels", [])),
                close_reason=raw.get("close_reason", "")
                              or raw.get("closeReason", ""),
                issue_type=raw.get("issue_type", "")
                           or raw.get("issueType", ""),
                created_at=raw.get("created_at", "")
                           or raw.get("createdAt", ""),
                updated_at=raw.get("updated_at", "")
                           or raw.get("updatedAt", ""),
                assignee=raw.get("assignee", ""),
            )
    return State(issues=tuple(seen.values()))


# ---------- Entry ----------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--role", default="manager",
                   help="Only role=manager runs health checks.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--preview", action="store_true",
                   help="Shadow mode: run health auditor and print proposed "
                        "heal actions without executing anything.")
    p.add_argument("--team-dir", type=Path, default=Path("."),
                   help="Path to team directory (for event bus init).")
    args = p.parse_args(argv)

    if args.role != "manager":
        return 0

    state = load_state()

    if args.preview:
        from workflow import State as WState, Issue as WIssue
        wstate = WState(issues=tuple(
            WIssue(
                id=i.id, title=i.title, description=i.description,
                status=i.status, labels=i.labels,
                close_reason=i.close_reason, issue_type=i.issue_type,
                created_at=i.created_at, updated_at=i.updated_at,
                assignee=i.assignee,
            )
            for i in state.issues
        ))
        auditor = HealthAuditor()
        for heal in auditor.audit(wstate):
            tag = "auto" if heal.is_auto_safe else "escalate"
            print(f"heal[{tag}] {heal.kind.name} "
                  f"issue={heal.issue_id or heal.target_id} "
                  f"epic={heal.epic_id} reason={heal.reason!r}")
        return 0

    actions = reconcile(state)
    for line in execute(actions, dry_run=args.dry_run, team_dir=args.team_dir):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
