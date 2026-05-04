#!/usr/bin/env python3
# RECONCILER_TEMPLATE_SHA: managed by `bin/cto`. Do not edit per-team copies.
"""Workflow reconciler for the AI CTO orchestrator.

Pure-function core: reconcile(state) -> [Action]. The execute layer turns
actions into bd CLI calls. Idempotency keys embedded in issue descriptions
prevent duplicate filings if a tick is replayed.

Replaces the bash hooks that previously lived inline in `supervisor.sh`
(hooks 1 through 5 plus the epic-ship gate). The reconciler is the only
thing in the system that may file or relabel workflow-control issues
(approvals, plan filings, dev/review pairs, epic merges, re-reviews).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


# ---------- Domain types ----------

@dataclass(frozen=True)
class Issue:
    id: str
    title: str
    description: str
    status: str  # "open" | "in_progress" | "closed"
    labels: tuple[str, ...]
    close_reason: str = ""

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

    @property
    def is_ops(self) -> bool:
        """Lightweight epic that bypasses breakdown/plan/review/merge.
        Use for one-shot ops tasks (git pull, run a sync script, etc.)
        where there is no diff to review."""
        return "class:ops" in self.labels


@dataclass(frozen=True)
class State:
    issues: tuple[Issue, ...]

    def by_id(self, id: str) -> Optional[Issue]:
        for i in self.issues:
            if i.id == id:
                return i
        return None

    def epics(self) -> list[Issue]:
        return [i for i in self.issues if i.kind == "epic"]

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
class FilePair:
    """File two issues atomically and link them with a `blocks` dependency.

    `upstream` is filed first; its bd-assigned id is then used as the
    `blocks` target for `downstream`, so `downstream` does not appear in
    `bd ready` until `upstream` closes. Used for plan↔review-plan,
    dev↔review-code, and dev↔re-review pairs — without this, reviewers
    claim review issues before the upstream artifact exists.
    """
    upstream: FileIssue
    downstream: FileIssue


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


Action = Union[FileIssue, FilePair, AddLabel, RemoveLabel, ReopenIssue, CloseIssue]


# ---------- Idempotency keys ----------

def idem(*parts: str) -> str:
    return ":".join(parts)


# ---------- Per-epic reconcile ----------

def _reconcile_ops_epic(epic: Issue, state: State) -> list[Action]:
    """Lightweight FSM for `class:ops` epics: file a single dev, close the
    epic when it closes. No breakdown, plan, review, merge, or worktree.
    Intended for one-shot tasks that don't produce a diff (git pull, run a
    sync script, restart something)."""
    actions: list[Action] = []
    children = state.children_of(epic.id)
    devs = [c for c in children if c.kind == "dev"]

    if not devs:
        dev_idem = idem("file-ops-dev", epic.id)
        if not state.has_idem(dev_idem):
            actions.append(FileIssue(
                title=f"Ops: {epic.title}",
                description=(
                    f"epic: {epic.id}\n"
                    f"idem: {dev_idem}\n"
                    f"Ops task — work directly in the team's main worktree. "
                    f"No branch, no diff expected. Close when done."
                ),
                labels=("role:developer", "kind:dev", "class:ops"),
                priority=2,
            ))
        return actions

    # Single dev exists. Close the epic when it closes.
    if all(not d.is_open for d in devs):
        actions.append(CloseIssue(
            issue_id=epic.id,
            reason=f"ops complete via {devs[0].id}",
        ))
    return actions


def reconcile_epic(
    epic: Issue,
    state: State,
    plan_chunks_for: "Optional[callable]" = None,
) -> list[Action]:
    """Compute actions for a single epic.

    `plan_chunks_for(epic_id) -> list[(letter, desc)]` reads the merged plan
    from disk to determine dev-task chunks. Tests inject a stub.
    """
    if epic.is_ops:
        return _reconcile_ops_epic(epic, state)

    children = state.children_of(epic.id)
    actions: list[Action] = []

    breakdowns = [c for c in children if c.kind == "breakdown"]
    breakdown_merges = [c for c in children if c.kind == "merge" and c.target == "breakdown"]
    breakdown_merges_closed = [c for c in breakdown_merges if not c.is_open]

    plans = [c for c in children if c.kind == "plan"]
    plan_merges = [c for c in children if c.kind == "merge" and c.target == "plan"]
    plan_merges_closed = [c for c in plan_merges if not c.is_open]

    devs = [c for c in children if c.kind == "dev"]
    code_reviews = [c for c in children if c.kind == "review" and c.target == "code"]
    code_merges = [c for c in children if c.kind == "merge" and c.target == "code"]
    epic_merges = [c for c in children if c.kind == "merge" and c.target == "epic"]

    # ---- Phase 1: no breakdown yet → manager fills this in. Reconciler waits.
    if not breakdowns:
        return actions

    # ---- Phase 2: after breakdown merge, ensure plan + plan-review filed.
    if breakdown_merges_closed and not plans:
        plan_idem = idem("file-plan", epic.id)
        review_idem = idem("file-review-plan", epic.id)
        if not state.has_idem(plan_idem) and not state.has_idem(review_idem):
            actions.append(FilePair(
                upstream=FileIssue(
                    title=f"Plan: {epic.title}",
                    description=(
                        f"epic: {epic.id}\n"
                        f"idem: {plan_idem}\n"
                        f"Author plans/{epic.id}.md on a task branch off epic/{epic.id}."
                    ),
                    labels=("role:developer", "kind:plan"),
                    priority=2,
                ),
                downstream=FileIssue(
                    title=f"Review plan: {epic.title}",
                    description=(
                        f"epic: {epic.id}\n"
                        f"idem: {review_idem}\n"
                        f"Review plans/{epic.id}.md on the plan branch."
                    ),
                    labels=("role:reviewer", "kind:review", "target:plan"),
                    priority=2,
                ),
            ))

    # ---- Phase 3: after plan merge, ensure dev + review:code per chunk.
    if plan_merges_closed and not devs:
        chunks: list[tuple[str, str]] = []
        if plan_chunks_for is not None:
            chunks = list(plan_chunks_for(epic.id) or [])
        if not chunks:
            chunks = [("", epic.title)]
        for letter, desc in chunks:
            slot = letter or "full"
            dev_key = idem("file-dev", epic.id, slot)
            rev_key = idem("file-review-code", epic.id, slot, "round-1")
            if state.has_idem(dev_key) or state.has_idem(rev_key):
                continue
            if letter:
                dev_title = f"Implement {desc}"
                dev_body = (
                    f"epic: {epic.id}\n"
                    f"idem: {dev_key}\n"
                    f"Per plans/{epic.id}.md §{letter} — {desc}.\n"
                    f"Worktree: .cto/worktrees/<dev-id> off epic/{epic.id}."
                )
                rev_title = f"Review: {desc}"
            else:
                dev_title = f"Implement {epic.title}"
                dev_body = (
                    f"epic: {epic.id}\n"
                    f"idem: {dev_key}\n"
                    f"Implement deliverables from the merged plan for epic {epic.id}.\n"
                    f"Worktree: .cto/worktrees/<dev-id> off epic/{epic.id}."
                )
                rev_title = f"Review: {epic.title}"
            actions.append(FilePair(
                upstream=FileIssue(
                    title=dev_title,
                    description=dev_body,
                    labels=("role:developer", "kind:dev"),
                    priority=2,
                ),
                downstream=FileIssue(
                    title=rev_title,
                    description=(
                        f"epic: {epic.id}\n"
                        f"idem: {rev_key}\n"
                        f"Review diff on the dev branch against epic/{epic.id}."
                    ),
                    labels=("role:reviewer", "kind:review", "target:code"),
                    priority=2,
                ),
            ))

    # ---- Phase 4: changes-requested handling (was supervisor Hook 1).
    for rev in code_reviews:
        if rev.is_open or not rev.changes_requested():
            continue
        upstream_id = _upstream_of_review(rev, state)
        if not upstream_id:
            continue
        upstream = state.by_id(upstream_id)
        if upstream is None or upstream.has_label("needs-re-review"):
            continue
        actions.append(AddLabel(issue_id=upstream_id, label="needs-re-review"))

    # ---- Phase 5: re-review filing (was supervisor Hook 2).
    for d in devs:
        if d.is_open or not d.has_label("needs-re-review"):
            continue
        # If an open review already covers this dev, the re-review is already
        # in flight — emit nothing and just strip the stale label.
        open_review_covers_dev = any(
            r.is_open and _upstream_of_review(r, state) == d.id
            for r in code_reviews
        )
        if open_review_covers_dev:
            actions.append(RemoveLabel(issue_id=d.id, label="needs-re-review"))
            continue
        round_n = _next_review_round(d, state)
        rev_key = idem("file-review-code", epic.id, d.id, f"round-{round_n}")
        if state.has_idem(rev_key):
            continue
        actions.append(FileIssue(
            title=f"Re-review (round {round_n}): {d.title}",
            description=(
                f"epic: {epic.id}\n"
                f"upstream: {d.id}\n"
                f"idem: {rev_key}\n"
                f"Re-review after dev addressed prior changes-requested."
            ),
            labels=("role:reviewer", "kind:review", "target:code"),
            priority=2,
        ))
        actions.append(RemoveLabel(issue_id=d.id, label="needs-re-review"))

    # ---- Phase 6: ship gate (was supervisor Hook 3, with all guards).
    # We don't gate on "no review ever closed changes-requested" — that's
    # historical state and can be cleared by an approved round-2. The
    # active gate is: every dev closed, no dev still carries
    # `needs-re-review`, no review or code-merge open, AND there are at
    # least as many code-merges as devs (so every dev's branch was merged
    # — without this, an epic with a changes-requested review and no
    # follow-up merge ships prematurely).
    ship_ready = (
        bool(breakdowns)
        and bool(breakdown_merges_closed)
        and bool(plans)
        and bool(plan_merges_closed)
        and bool(devs)
        and all(not d.is_open for d in devs)
        and not any(d.has_label("needs-re-review") for d in devs)
        and all(not r.is_open for r in code_reviews)
        and all(not m.is_open for m in code_merges)
        and len(code_merges) >= len(devs)
        and not any(em.is_open for em in epic_merges)
    )
    if ship_ready:
        ship_key = idem("file-epic-merge", epic.id)
        if not state.has_idem(ship_key):
            actions.append(FileIssue(
                title=f"Merge epic: {epic.title}",
                description=(
                    f"epic: {epic.id}\n"
                    f"epic-branch: epic/{epic.id}\n"
                    f"idem: {ship_key}"
                ),
                labels=("role:cto", "kind:merge", "target:epic"),
                priority=1,
            ))

    return actions


def reconcile(state: State, plan_chunks_for=None) -> list[Action]:
    out: list[Action] = []
    for epic in state.epics():
        if not epic.is_open:
            continue
        out.extend(reconcile_epic(epic, state, plan_chunks_for=plan_chunks_for))
    return out


# ---------- Helpers ----------

def _upstream_of_review(review: Issue, state: State) -> Optional[str]:
    m = re.search(r"^upstream:\s*(\S+)\s*$", review.description, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r"\btask/([A-Za-z0-9-]+)", review.description)
    if m:
        cand = m.group(1)
        if state.by_id(cand):
            return cand
    # Fallback: derive upstream via idem-key shape. Reviews filed by the
    # reconciler carry idem `file-review-code:<epic>:<slot>:round-N`; their
    # paired dev carries `file-dev:<epic>:<slot>`. Used for code reviews
    # that predate FilePair (no description-level link to the dev).
    m = re.search(r"^idem:\s*file-review-code:([^:]+):([^:]+):", review.description, re.MULTILINE)
    if m:
        epic_id, slot = m.group(1), m.group(2)
        dev_idem = f"idem: file-dev:{epic_id}:{slot}"
        for i in state.issues:
            if i.kind == "dev" and dev_idem in i.description:
                return i.id
    return None


def _next_review_round(dev: Issue, state: State) -> int:
    n = 1
    for i in state.issues:
        if i.kind != "review" or i.target != "code":
            continue
        if _upstream_of_review(i, state) == dev.id:
            n += 1
    return n


def parse_plan_chunks(epic_id: str, root: Optional[Path] = None) -> list[tuple[str, str]]:
    """Find chunk markers in a merged plan. Looks for headings of the form
    `## Dev A: <desc>` or `### Chunk B — <desc>`."""
    root = root or Path(".")
    candidates = [
        root / ".cto" / "worktrees" / epic_id / "plans" / f"{epic_id}.md",
        root / "plans" / f"{epic_id}.md",
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


# ---------- Execute (live bd calls) ----------

def execute(actions: list[Action], dry_run: bool = False) -> list[str]:
    log: list[str] = []
    for a in actions:
        if isinstance(a, FileIssue):
            log.append(f"file: {a.title!r} labels={list(a.labels)}")
            if not dry_run:
                _bd_file(a)
        elif isinstance(a, FilePair):
            log.append(
                f"pair: {a.upstream.title!r} → {a.downstream.title!r} "
                f"(downstream blocked by upstream)"
            )
            if not dry_run:
                up_id = _bd_file(a.upstream)
                if up_id:
                    down_action = FileIssue(
                        title=a.downstream.title,
                        description=a.downstream.description,
                        labels=a.downstream.labels,
                        priority=a.downstream.priority,
                        blocks=None,
                    )
                    down_id = _bd_file(down_action)
                    if down_id:
                        # Upstream blocks downstream: downstream stays out of
                        # `bd ready` until upstream closes.
                        subprocess.run(
                            ["bd", "dep", up_id, "--blocks", down_id],
                            check=False, capture_output=True,
                        )
        elif isinstance(a, AddLabel):
            log.append(f"label+: {a.issue_id} +{a.label}")
            if not dry_run:
                subprocess.run(
                    ["bd", "update", a.issue_id, "--add-label", a.label],
                    check=False, capture_output=True,
                )
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
    if new_id and action.blocks:
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
            )
    return State(issues=tuple(seen.values()))


# ---------- Entry ----------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--role", default="manager",
                   help="Only role=manager performs reconciliation.")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if args.role != "manager":
        return 0

    state = load_state()
    actions = reconcile(state, plan_chunks_for=parse_plan_chunks)
    for line in execute(actions, dry_run=args.dry_run):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
