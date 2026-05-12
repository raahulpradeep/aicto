# AI CTO Architecture Redesign Plan

**Branch:** `plan/architecture-redesign`
**Author:** Joey (AI Assistant) via Rahul Pradeep
**Date:** 2026-05-05
**Status:** Draft for CTO Review

---

## Executive Summary

The current aicto system is a sophisticated orchestration layer built on bash supervisors, git worktrees, and the beads issue tracker. It successfully coordinates multiple AI agents through a Scrum-like workflow. However, the architecture carries significant operational overhead: 9+ hops per task iteration, stateless agents that waste context windows, and a human-in-the-loop design that contradicts the goal of an autonomous AI CTO.

This plan proposes a shift from **poll-driven, process-per-iteration** architecture to an **event-driven, persistent-agent** architecture with real feedback loops.

---

## Problems with Current Architecture

### 1. Execution Path is Too Long
Current flow for one agent iteration:
```
tmux supervisor → bash loop → claude/kimi CLI → worktree → git commit → bd close → reconciler polls → reconciler files next issue → supervisor polls → agent restarts
```

**Impact:** ~9 failure surfaces, slow iteration (5-15s overhead per cycle), fragile error recovery based on stderr grepping.

### 2. Agents are Stateless
Each iteration, the agent re-reads its role prompt (10K+ tokens), re-discovers the issue, re-learns codebase context. For a 200K context model, 20-30% of tokens are "orientation tax."

**Impact:** Wasted API costs, slower execution, agents lose nuanced context from previous iterations.

### 3. Human CTO is the Bottleneck
Two mandatory gates per epic: breakdown approval + plan approval. With 5+ active epics, the human is context-switching constantly.

**Impact:** Latency measured in hours instead of minutes, defeats the purpose of "autonomous" CTO.

### 4. Reviewer is the Throughput Ceiling
N developers (typically 2-3) feed into M reviewers (typically 1). Review queue grows linearly with dev output.

**Impact:** Developers idle while waiting for review, epics stall.

### 5. No Feedback from Reality
System ends at `git merge`. No CI test results, no deployment metrics, no runtime errors feeding back into planning.

**Impact:** AI CTO makes decisions in a vacuum, cannot learn from actual outcomes.

### 6. Error Recovery is Heuristic
The supervisor checks `rc != 0` and guesses failure type by grepping stderr. Git conflicts, context window crashes, API rate limits, and infinite loops all get the same "retry with back off" treatment.

**Impact:** Silent failures, zombie agents, manual intervention required for recovery.

---

## Proposed Architecture: Event-Driven Persistent Agents

### Core Principle
Replace the "spawn CLI per task" model with "long-running agent processes that wake on events, work, save state, sleep."

### System Diagram

```
┌─────────────────────────────────────────┐
│         Human CTO (You)                 │
│    Only for: strategy, exceptions,      │
│    budgets, ethics, hiring agents       │
└─────────────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────┐
│      AI CTO Agent (Orchestrator)        │
│  - Reads CI status, errors, metrics     │
│  - Decides what to build next             │
│  - Spawns sub-agents dynamically          │
│  - Owns product roadmap                   │
│  - Auto-approves with rollback policy     │
└─────────────────────────────────────────┘
                    │
    ┌───────────────┼───────────────┐
    ▼               ▼               ▼
┌────────┐    ┌────────┐    ┌────────┐
│Planner │    │Coder   │    │Tester  │
│Agent   │    │Agent   │    │Agent   │
│(1-n)   │    │(1-n)   │    │(1-n)   │
└────────┘    └────────┘    └────────┘
    │               │               │
    └───────────────┼───────────────┘
                    ▼
        ┌──────────────────┐
        │   CI/CD Pipeline │
        │ (GitHub Actions) │
        └──────────────────┘
                    │
                    ▼
        ┌──────────────────┐
        │  Observability   │
        │ (logs, metrics,  │
        │  errors, costs)  │
        └──────────────────┘
                    │
                    ▼
        ┌──────────────────┐
        │  Feedback Loop   │
        │ → CTO agent      │
        │ → Next sprint    │
        └──────────────────┘
```

---

## Key Changes

### 1. Persistent Agent Processes

**Current:** Each agent is a fresh `claude` or `kimi` CLI invocation in a tmux window.

**Proposed:** Each agent is a long-running Python process with:
- In-memory state (current task, accumulated context, scratchpad)
- SQLite persistence for crash recovery
- Event subscriber (Redis, NATS, or simple file-watch)
- Rehydration on restart: loads state, resumes where it left off

**Benefits:**
- Context windows preserved across iterations
- Agent remembers "this codebase uses tabs, not spaces"
- Sub-second response to new events instead of 5-15s spawn overhead

**Implementation:**
```python
class AgentProcess:
    def __init__(self, role: str, team: str):
        self.state = StateStore.load(role, team)
        self.llm = LLMClient(model=self.state.model)
        self.event_bus = EventBus.subscribe(f"team.{team}.{role}")
    
    def run(self):
        for event in self.event_bus:
            self.state = self.llm.execute(event, context=self.state)
            StateStore.save(self.state)
```

### 2. Event Bus Instead of Polling

**Current:** Supervisor bash loop runs `bd ready --label role:manager --json` every 5 seconds.

**Proposed:** Lightweight event bus (Redis pub/sub, or even SQLite-based queue for local-first):
- `task.created` → Planner agent wakes
- `plan.approved` → Coder agents wake
- `code.committed` → Reviewer agent wakes
- `ci.failed` → CTO agent wakes, files incident
- `review.approved` → Merge agent wakes

**Benefits:**
- No wasted polling cycles
- Immediate response to state changes
- Multiple agents can subscribe to same topic (parallel reviewers)

**Implementation:**
```python
# Simple file-based event bus (no external deps)
class EventBus:
    def publish(self, topic: str, payload: dict):
        EventStore.append(topic, json.dumps(payload))
    
    def subscribe(self, topic: str):
        return EventStore.listen(topic, since=self.last_seen)
```

### 3. Auto-Approval with Rollback

**Current:** Human must `cto approve` breakdown and plan for every epic.

**Proposed:** AI CTO auto-approves if confidence > threshold, with automatic rollback policy:
- Breakdown confidence > 0.9 → auto-approve
- Plan confidence > 0.8 → auto-approve
- Code passes CI + no behavior regression → auto-merge
- Any metric degrades post-deploy → automatic `git revert` + incident filing

**Human override:**
- `class:bypass-cto` epics auto-execute entirely
- Non-bypass epics show up in dashboard with "approve in 5min unless rejected"
- Human can always `--freeze` an epic for manual review

**Benefits:**
- Latency drops from hours to minutes
- Human only sees exceptions and edge cases
- System learns from rollback outcomes

### 4. Elastic Agent Scaling

**Current:** Fixed team size (2 devs, 1 reviewer) defined in `config.yaml`.

**Proposed:** Dynamic scaling based on queue depth:
- `dev_queue_depth > 3` → spawn additional Coder agent
- `review_queue_depth > 2` → spawn additional Reviewer agent
- `epic_count > 10` → spawn additional Planner agent
- Idle agents auto-terminate after 15min of no work

**Benefits:**
- No reviewer bottleneck
- Cost scales with actual work, not fixed overhead
- Parallel execution for independent sub-tasks

### 5. Executable Review Instead of Human-Style Review

**Current:** Reviewer agent reads code and writes prose feedback.

**Proposed:** Review is a set of automated checks:
1. **Compilation check** — does it build?
2. **Test check** — do all tests pass?
3. **Behavior diff** — does output differ from `main` in expected ways?
4. **Lint/format** — style compliance
5. **Security scan** — basic pattern matching for SQL injection, hardcoded secrets
6. **Only then** — LLM review for architecture/logic (high-value human-like review)

**Benefits:**
- Reviews complete in seconds, not minutes
- No human-style nitpicking about formatting
- LLM focus on actual design decisions

### 6. CI/CD Feedback Loop

**Current:** No deployment or testing after merge.

**Proposed:** Every merged epic auto-deploys to staging:
```
main branch → GitHub Action → staging deploy → metric observation (5min)
    ↓ metrics good → auto-promote to prod
    ↓ metrics bad → auto-revert + file incident + notify CTO
```

**Metrics observed:**
- Error rate (should not increase)
- Latency p99 (should not regress)
- Test pass rate (should be 100%)
- Cost per request (should not spike)

**Benefits:**
- AI CTO gets real feedback on its decisions
- Safety net prevents bad deploys
- System learns what works

### 7. Retrospective Agent

**New component:** After every epic ships or rolls back, a Retrospective agent:
- Reads the epic history (breakdown → plan → dev → review → deploy)
- Identifies what went wrong (plan too vague? tests missing? review missed bug?)
- Updates role prompts with lessons learned
- Files `improvement:` issues for systemic fixes

**Benefits:**
- System gets better over time
- Prompts evolve based on actual outcomes
- Not just a workflow engine — a learning organization

---

## Implementation Phases

### Phase 1: Persistent Agent Shell (1-2 weeks)
- [ ] Build `AgentProcess` base class with state persistence
- [ ] Replace `supervisor.sh` bash loops with Python agent runners
- [ ] Keep existing worktree + git workflow intact
- [ ] Agents still invoke `claude`/`kimi` CLI but maintain state across iterations

### Phase 2: Event Bus (1 week)
- [ ] Implement file-based event queue (no external deps)
- [ ] Replace `bd ready` polling with event subscriptions
- [ ] Reconciler publishes events instead of filing bd issues
- [ ] Backward compatible: old bd workflow still works

### Phase 3: Auto-Approval (1 week)
- [ ] Add confidence scoring to breakdown/plan quality
- [ ] Implement `class:bypass-cto` epic auto-workflow
- [ ] Dashboard shows "auto-approve in N minutes" with reject button
- [ ] Human can still manual-approve non-bypass epics

### Phase 4: Elastic Scaling (1 week)
- [ ] Monitor queue depths per role
- [ ] Auto-spawn/terminate agent processes
- [ ] Dashboard shows active agent count

### Phase 5: Executable Review + CI (2 weeks)
- [ ] Integrate GitHub Actions for auto-test on PR
- [ ] Build behavior diff tool (compare output of `main` vs branch)
- [ ] Replace prose review with automated checks + LLM architecture review
- [ ] Auto-merge on green CI

### Phase 6: Feedback Loop + Retrospectives (2 weeks)
- [ ] Deploy-to-staging pipeline
- [ ] Metric observation window
- [ ] Auto-revert on regression
- [ ] Retrospective agent that updates prompts

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Auto-approval ships bugs | Medium | CI gate must be strict; rollback is automatic; human can freeze |
| Persistent agents consume too much memory | Low | Agents sleep when idle; SQLite state is small; auto-terminate after idle |
| Event bus adds complexity | Low | Start with file-based queue (no new infra); upgrade to Redis later |
| Agents lose state on crash | Medium | SQLite persistence + rehydration; agent resumes from last event |
| Human feels out of control | Medium | Dashboard shows everything; freeze button; all decisions logged |
| Token costs increase with persistent agents | Low | Less "orientation tax" actually reduces tokens; idle agents don't call LLM |

---

## What to Keep from Current System

1. **Git worktrees** — still the best isolation mechanism. No change needed.
2. **Role prompt templates** — manager/planner, developer/coder, reviewer/tester personas are correct.
3. **Beads (bd) issue tracker** — keep for audit trail and human-readable history. Event bus supplements, doesn't replace.
4. **Dashboard concept** — upgrade to show real-time metrics, agent states, and auto-approval queue.
5. **Reconciler concept** — evolve into event publisher instead of poller.

---

## Success Metrics

| Metric | Current | Target (Phase 6) |
|--------|---------|------------------|
| Epic cycle time (filed → shipped) | Hours (human gates) | Minutes (auto-approve) |
| Agent iteration overhead | 5-15s | <1s |
| Context window waste | 20-30% | <5% |
| Reviewer bottleneck | 1 reviewer, N devs | Elastic reviewers |
| Post-merge feedback | None | CI + staging metrics |
| System learning | None | Prompt updates per epic |
| Human CTO time per epic | 10-15min (2 approvals) | 2-3min (exception review only) |

---

## Next Steps

1. **CTO Review:** Rahul reviews this plan, approves/rejects/modifies
2. **Phase 1 Spike:** Build a proof-of-concept persistent agent for one role (e.g., Developer)
3. **Parallel Track:** Set up GitHub Actions CI pipeline for the aicto repo itself (dogfooding)
4. **Decision Point:** After Phase 1, evaluate if the complexity trade-off is worth it

---

*End of plan. Ready for CTO approval.*
