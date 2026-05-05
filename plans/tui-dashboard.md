# TUI Dashboard Plan — AI CTO

**Status:** Draft for CTO Review  
**Author:** Joey (AI Assistant) via Rahul Pradeep  
**Date:** 2026-05-05  
**Branch:** `plan/tui-dashboard`

---

## 1. Executive Summary

The current CTO dashboard (`ctodashboard.py`) is a functional Textual-based TUI that gives a read-only + basic interactive view of the AI CTO workspace. It polls every second, renders four panels, and lets the human approve/reject inbox items. It works, but it is not *world-class*.

This plan defines what a world-class CTO command center looks like:
- **Live** — sub-second updates via event bus, not polling
- **Interactive** — kill agents, spawn agents, view diffs, comment on issues from the dashboard
- **Visual** — pipeline swimlanes, queue depth charts, token cost tracking, CI status
- **Mobile** — companion web dashboard / Telegram bot for when the human is AFK
- **Fast** — virtual scrolling, lazy loading, batched renders for 1000+ event histories

The goal: the human CTO opens one terminal window and *knows everything* without switching contexts.

---

## 2. Dashboard Layout & Screens

### 2.1 Main Overview Screen (Default)

The existing layout is good. We evolve it:

```
┌─────────────────────────────────────────────────────────────────┐
│  AGENTS (6)                    │  CTO INBOX (2)                │
│  ───────────────────────────── │  ────────────────────────────   │
│  demo:dev-1  working  04:32     │  demo-1   plan-23    [A][R]   │
│  demo:dev-2  idle     —         │  demo-1   merge-7    [A][R]   │
│  demo:review working  01:15     │                                │
│  ───────────────────────────── │  ────────────────────────────   │
├─────────────────────────────────────────────────────────────────┤
│  EPIC PIPELINE                 │  ACTIVITY STREAM              │
│  ───────────────────────────── │  ────────────────────────────   │
│  health-kb  ✓ ◐ ● ● ● ●  2h    │  14:32:01  dev-1 finished     │
│  refactor   ✓ ✓ ● ● ● ●  5h    │  14:31:45  merge executed     │
│  api-v2     ✓ ✓ ◐ ● ● ●  1d    │  14:30:12  stuck detected!    │
│  ───────────────────────────── │  ────────────────────────────   │
├─────────────────────────────────────────────────────────────────┤
│  OPEN TASKS (12)                                              │
│  ─────────────────────────────────────────────────────────────  │
│  demo-1  breakdown  health-kb  —      12m                     │
│  demo-1  dev        api-v2      dev-2  45m                     │
│  ─────────────────────────────────────────────────────────────  │
└─────────────────────────────────────────────────────────────────┘
Footer: q quit · tab focus · ↑↓ navigate · enter drill · a approve · r reject · ● live · 47ms
```

**Improvements over current:**
- Real-time indicators (pulsing border on live data)
- Token count per agent (accumulated this session)
- Model badge (claude-3.5-sonnet, kimi-k2, etc.)
- Queue depth micro-badges on epic pipeline
- CI status dot (🟢/🔴) per epic

### 2.2 Per-Epic Drill-Down Screen

Triggered by pressing `Enter` on an epic in the pipeline.

```
┌─────────────────────────────────────────────────────────────────┐
│  Epic: api-v2 — "Refactor API layer for v2"                    │
│  Team: demo  │  Age: 1d  │  Est. cost: $12.40                │
├─────────────────────────────────────────────────────────────────┤
│  PIPELINE SWIMLANE (horizontal)                                │
│  [Breakdown ✓] → [Plan ✓] → [Dev ◐] → [Review ●] → [Merge ●] → [Ship ●]  │
├─────────────────────────────────────────────────────────────────┤
│  TASKS BY STAGE                                                │
│  ─────────────────────────────────────────────────────────────  │
│  Dev (active):                                                 │
│    api-v2-34  dev-2  working  45m  23K tokens                  │
│  Review (pending):                                             │
│    api-v2-35  —      queued   —    —                           │
├─────────────────────────────────────────────────────────────────┤
│  COST ACCUMULATION                                             │
│  ─────────────────────────────────────────────────────────────  │
│  Breakdown: $1.20  │  Plan: $2.10  │  Dev: $6.80  │  Review: $2.30  │
├─────────────────────────────────────────────────────────────────┤
│  BRANCH: epic/api-v2  │  CI: 🔴 failing  │  Commits: 7         │
├─────────────────────────────────────────────────────────────────┤
│  [View Diff]  [View PR]  [Approve Plan]  [Kill Dev Agent]  [Back]  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.3 Per-Agent Live Log Screen

Triggered by pressing `Enter` on an agent row.

```
┌─────────────────────────────────────────────────────────────────┐
│  Agent: demo:dev-2                                             │
│  Status: working  │  Model: kimi-k2  │  Session tokens: 45K  │
├─────────────────────────────────────────────────────────────────┤
│  LIVE LOG (tail -f, colorized)                                  │
│  ─────────────────────────────────────────────────────────────  │
│  14:32:01  [cyan]  Starting iteration 12 on plan-34           │
│  14:32:02  [dim]   Reading artifact plans/api-v2.md           │
│  14:32:05  [green] 3 files modified                           │
│  14:32:06  [green] git commit -m "refactor: extract handler"   │
│  14:32:07  [yellow] Pushing to origin/epic/api-v2             │
│  ...                                                            │
├─────────────────────────────────────────────────────────────────┤
│  [Kill Agent]  [Restart]  [Pause]  [View Worktree]  [Back]      │
└─────────────────────────────────────────────────────────────────┘
```

**Key features:**
- Live tail of `~/.cto/logs/<agent>.log` with syntax highlighting
- ANSI color passthrough from agent output
- Kill button sends SIGTERM to the tmux pane
- Restart button runs `bin/cto restart <team> <agent>`

### 2.4 System Health / Metrics Screen

A dedicated screen for system-wide observability. Toggle with `m`.

```
┌─────────────────────────────────────────────────────────────────┐
│  SYSTEM HEALTH                                                  │
├─────────────────────────────────────────────────────────────────┤
│  AGENT FLEET                                                    │
│  ─────────────────────────────────────────────────────────────  │
│  Team    Agents  Working  Idle  Crashed  Avg Tokens/Iter       │
│  demo    3      2        1     0        12.4K                 │
│  infra   2      1        1     0        8.1K                  │
│  ─────────────────────────────────────────────────────────────  │
├─────────────────────────────────────────────────────────────────┤
│  QUEUE DEPTHS (live sparklines)                                 │
│  ─────────────────────────────────────────────────────────────  │
│  Dev:     ████████░░░░  7 waiting                               │
│  Review:  ███░░░░░░░░░  3 waiting                               │
│  Plan:    █░░░░░░░░░░░  1 waiting                               │
├─────────────────────────────────────────────────────────────────┤
│  TOKEN COST (last 24h)                                          │
│  ─────────────────────────────────────────────────────────────  │
│  Total: $45.30  │  Demos: $12.40  │  Infras: $32.90             │
│  Cost/ship: $8.20  │  Trend: ↓ 12% vs yesterday                  │
├─────────────────────────────────────────────────────────────────┤
│  CI STATUS                                                      │
│  ─────────────────────────────────────────────────────────────  │
│  epic/health-kb   🟢 passing  (last run 3m ago)                 │
│  epic/api-v2      🔴 failing  (test_auth_timeout)               │
│  epic/refactor    🟢 passing  (last run 12m ago)                │
├─────────────────────────────────────────────────────────────────┤
│  RECENT CRASHES                                                 │
│  ─────────────────────────────────────────────────────────────  │
│  14:15  demo:dev-1  context-window-exceeded  (auto-recovered)   │
│  13:42  infra:plan  API rate limit  (backoff 60s)              │
└─────────────────────────────────────────────────────────────────┘
```

### 2.5 CTO Decision Queue Screen

A focused screen for items needing human approval. Toggle with `d`.

```
┌─────────────────────────────────────────────────────────────────┐
│  CTO DECISION QUEUE — Auto-approve in 5min unless rejected      │
├─────────────────────────────────────────────────────────────────┤
│  #  Team    ID          Kind         Auto-approve    Action       │
│  ─────────────────────────────────────────────────────────────  │
│  1  demo    plan-23     plan         04:32 (+3m)   [A] [R] [E] │
│  2  demo    merge-7     epic-merge   04:35 (+6m)   [A] [R]      │
│  3  infra   breakdown-3 breakdown  MANUAL          [A] [R]      │
│  ─────────────────────────────────────────────────────────────  │
│  [E] = Edit (open in $EDITOR)                                  │
├─────────────────────────────────────────────────────────────────┤
│  PREVIEW PANEL (shows artifact content for selected item)       │
│  ─────────────────────────────────────────────────────────────  │
│  # Plan for api-v2                                              │
│  1. Extract auth handler...                                     │
│  ...                                                            │
└─────────────────────────────────────────────────────────────────┘
```

**Auto-approve mechanics:**
- `class:bypass-cto` epics: no human gate
- Regular epics: 5-minute countdown shown in dashboard
- Human can `a` to early-approve, `r` to reject, or `E` to edit artifact first
- If timer expires: system auto-approves, logs the decision, notifies

### 2.6 Diff Viewer Screen

Triggered by pressing `v` (view diff) on an epic or merge item.

```
┌─────────────────────────────────────────────────────────────────┐
│  Diff: epic/api-v2 vs main                                       │
├─────────────────────────────────────────────────────────────────┤
│  diff --git a/src/auth.py b/src/auth.py                          │
│  +++ refactored token validation                                │
│  ─────────────────────────────────────────────────────────────  │
│  [syntax highlighted diff with line numbers]                    │
│  ...                                                            │
├─────────────────────────────────────────────────────────────────┤
│  [Comment]  [Approve]  [Reject]  [Back]                         │
└─────────────────────────────────────────────────────────────────┘
```

Uses a scrollable text area with inline diff highlighting (green for additions, red for deletions).

---

## 3. Live Feed Architecture

### 3.1 The Problem with Polling

Current dashboard polls every 1s:
- Runs `bd list --status open --json` across all teams
- Runs `tmux list-windows` for each session
- Reads `activity.jsonl` for events
- This is ~3-6 subprocess calls per team per second
- With 5 teams, that's 15-30 cold-starts of `bd` (which cold-starts dolt) every second
- Result: CPU burn, 200-500ms gather latency, missed events between polls

### 3.2 Solution: Event Bus + File Watch Hybrid

We implement a lightweight event bus that existing components write to, and the dashboard subscribes to.

```
┌─────────────────────────────────────────────────────────────┐
│                     Event Bus                                │
│  (SQLite-based queue — no external deps, local-first)        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  Publishers:                                                  │
│    • supervisor.sh  → agent_iteration_start/end            │
│    • reconciler     → issue_created, issue_closed           │
│    • merge script   → merge_executed                        │
│    • CI hook        → ci_status_changed                     │
│    • token tracker  → token_usage_logged                    │
│                                                              │
│  Subscribers:                                                 │
│    • Dashboard TUI  → real-time panel updates               │
│    • Desktop notify → critical events only                 │
│    • Web dashboard  → forwarded via SSE/WebSocket            │
│    • Telegram bot   → push notifications                   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 Event Bus Implementation

**Phase 1: File-based (no new infrastructure)**

```python
# dashboard/bus.py
import json
import threading
import time
from pathlib import Path
from typing import Callable

class EventBus:
    """Simple file-based pub/sub using append-only JSONL.
    
    Each team has its own event stream:
        teams/<team>/.cto/events/stream.jsonl
    
    Dashboard watches all streams and merges chronologically.
    """
    
    def __init__(self, teams_dir: Path):
        self.teams_dir = teams_dir
        self._listeners: list[Callable] = []
        self._last_offsets: dict[str, int] = {}
        self._lock = threading.Lock()
        self._running = False
    
    def subscribe(self, callback: Callable[[dict], None]):
        self._listeners.append(callback)
    
    def start(self):
        self._running = True
        threading.Thread(target=self._watch, daemon=True).start()
    
    def _watch(self):
        while self._running:
            events = self._poll()
            for ev in events:
                for cb in self._listeners:
                    try:
                        cb(ev)
                    except Exception:
                        pass
            time.sleep(0.1)  # 100ms poll — file watch is cheap
    
    def _poll(self) -> list[dict]:
        # Read new lines from each team's stream.jsonl
        # Return merged, chronologically sorted events
        ...
```

**Phase 2: Upgrade to inotify/kqueue (native file watching)**

On macOS, use `fsevents` via `watchdog` library. On Linux, use `inotify`. This gives true sub-second reactivity without CPU burn.

```python
# Phase 2 — native file watching
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class EventStreamHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith("stream.jsonl"):
            self.bus._flush_for_path(event.src_path)
```

### 3.4 Event Schema

```json
{
  "ts": "2026-05-05T14:32:01+00:00",
  "topic": "agent.lifecycle",
  "team": "demo",
  "agent": "demo:dev-2",
  "event": "iteration_start",
  "issue_id": "plan-34",
  "payload": {
    "model": "kimi-k2",
    "tokens_so_far": 45000
  }
}
```

**Topics:**
- `agent.lifecycle` — start, finish, crash, kill, spawn
- `issue.state` — created, claimed, closed, reopened
- `epic.pipeline` — stage transition, blocked, unblocked
- `ci.status` — pending, running, passed, failed
- `token.usage` — per-iteration cost snapshot
- `cto.decision` — auto-approve triggered, human approved/rejected

### 3.5 Notification Rules

Not every event becomes a desktop notification. Rules:

| Event | Desktop | Telegram | Priority |
|-------|---------|----------|----------|
| New CTO inbox item | ✅ | ✅ | Normal |
| Agent crashed | ✅ | ✅ | High |
| Epic shipped | ✅ | ✅ | Normal |
| Review escalation (>30min in review) | ✅ | ✅ | High |
| CI failed on epic | ✅ | ✅ | High |
| Agent iteration complete | ❌ | ❌ | — |
| Commit pushed | ❌ | ❌ | — |
| Reconciler tick | ❌ | ❌ | — |

### 3.6 Activity Stream Design

The existing activity stream is good. Enhancements:

**Event types to add:**
- `token_usage` — "dev-2 used 12K tokens ($0.04)"
- `ci_status` — "CI failed on epic/api-v2: test_auth_timeout"
- `agent_spawned` — "Spawned demo:dev-3 (elastic scaling)"
- `auto_approve` — "Auto-approved plan-23 (confidence 0.94)"
- `cost_alert` — "Epic api-v2 exceeded $15 budget"

**Filtering:**
- Press `f` to filter by event type
- Press `/` to search within the stream
- Persist filter preferences in `~/.config/aicto/dashboard.json`

**Virtual scrolling:**
- Keep last 1000 events in memory
- Lazy-load older events from rotated JSONL on scroll
- Render only visible rows (like `DataTable` but for activity)

---

## 4. Visualization

### 4.1 Epic Pipeline Swimlane

Current implementation shows a 7-column table with status icons per epic.

**Enhancement: horizontal swimlane per epic**

```
health-kb  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
           [Breakdown]────[Plan]────[Dev]────[Review]────[Merge]────[Ship]
           ✓ DONE        ✓ DONE    ◐ ACTIVE  ● PENDING   ● PENDING  ● PENDING
           12m           8m        45m       —           —          —
```

Implementation: Rich `Columns` or custom renderable with colored blocks.

**Queue depth indicators:**
- Show a micro-badge on each stage: how many tasks are queued/waiting
- If review queue > 2, stage badge turns yellow; > 5 turns red

### 4.2 Agent Status Cards

Replace the agent table row with a richer card when the panel has focus:

```
┌─ demo:dev-2 ─────────────────┐
│  🟢 WORKING  01:15:32        │
│  Model: kimi-k2              │
│  Tokens: 45.2K this session  │
│  Issue: plan-34              │
│  Iterations: 12              │
│  Last output: 3 files modified│
└──────────────────────────────┘
```

### 4.3 Queue Depth Charts

Use Rich's bar chart or sparkline renderable:

```python
from rich.bar import Bar
from rich.columns import Columns

dev_queue = Bar(7, 10, color="yellow")  # 7 of 10 capacity
review_queue = Bar(3, 5, color="green")
```

### 4.4 Token Cost Tracking

**Per epic:**
- Accumulate from `token.usage` events
- Show in epic detail screen
- Alert if epic exceeds budget (configurable in `config.yaml`)

**Per agent:**
- Show rolling 1h / 24h token count
- Alert if an agent is in an infinite loop (tokens >> expected)

**Global:**
- Daily spend tracking
- Comparison to yesterday
- Projection for month-end

```
Token Spend (today): $45.30
├── demos      $12.40  ████████░░░░░░░░░░░░
├── infras     $32.90  █████████████████████
└── overhead   $0.00   (dashboard, reconciler)

Projection: $1,359 / $2,000 budget (68%)
```

### 4.5 CI Status Integration

**Data source:** GitHub Actions API (or webhooks)

**Display:**
- 🟢 / 🟡 / 🔴 dot next to each epic in pipeline
- Hover/Enter shows last run details: "failed 3m ago, test_auth_timeout"
- Link to GitHub Actions log (opens browser)

**Implementation:**
```python
# dashboard/ci.py
import requests

def fetch_ci_status(repo: str, branch: str, token: str) -> dict:
    """Fetch latest workflow run status for a branch."""
    url = f"https://api.github.com/repos/{repo}/actions/runs"
    params = {"branch": branch, "per_page": 1}
    headers = {"Authorization": f"token {token}"}
    r = requests.get(url, params=params, headers=headers, timeout=5)
    ...
```

---

## 5. Interactivity

### 5.1 Approve / Reject from Dashboard

Already implemented in `ctodashboard.py`. Enhancements:

- **Bulk approve:** Select multiple inbox items with `Space`, then `A` to approve all
- **Preview before approve:** Show artifact inline in a split pane (like `cmdcenter.py`)
- **Edit before approve:** Press `e` to open artifact in `$EDITOR`, save, then approve
- **Comment templates:** `a` then choose from "LGTM", "Needs tests", "See comments", or freeform

### 5.2 Kill / Restart Agent Buttons

```python
# In AgentDetailScreen or inline in agents panel

class AgentControlScreen(ModalScreen):
    def compose(self):
        yield Static(f"Agent: {self.agent}")
        yield Button("Kill (SIGTERM)", variant="error", id="kill")
        yield Button("Kill -9", variant="error", id="kill9")
        yield Button("Restart", variant="warning", id="restart")
        yield Button("Pause", variant="primary", id="pause")
        yield Button("Resume", variant="success", id="resume")
```

**Implementation:**
```python
def kill_agent(team: str, window: str, signal: int = 15):
    session = f"cto-{team}"
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{session}:{window}", "C-c"],
        capture_output=True,
    )
    # Or more forcefully:
    # subprocess.run(["pkill", "-f", f"claude.*{team}:{window}"])

def restart_agent(team: str, window: str):
    subprocess.run([str(ROOT / "bin" / "cto"), "restart", team, window])
```

### 5.3 Spawn New Agent Button

Manual elastic scaling — press `+` in the agents panel:

```
Spawn new agent:
  Role: [developer] [reviewer] [planner]
  Model: [claude-3.5-sonnet] [kimi-k2] [gpt-4o]
  Epic: [auto-assign] [health-kb] [api-v2]
  
  [Spawn]  [Cancel]
```

**Implementation:**
```python
def spawn_agent(team: str, role: str, model: str, epic_id: str | None):
    subprocess.run([
        str(ROOT / "bin" / "cto"), "spawn", team,
        "--role", role,
        "--model", model,
        *("--epic", epic_id) if epic_id else [],
    ])
```

### 5.4 View Diff in Dashboard

When an inbox item is a merge request or contains an artifact with changes:

1. Read the artifact path from the issue description
2. If it's a plan/breakdown: show markdown diff vs previous version
3. If it's a merge: show `git diff main...branch`
4. Render in a scrollable `TextArea` with diff syntax highlighting

**Implementation:**
```python
def get_diff(team: str, epic_id: str) -> str:
    wt = TEAMS_DIR / team / ".cto" / "worktrees" / epic_id
    result = subprocess.run(
        ["git", "-C", str(wt), "diff", f"main...epic/{epic_id}"],
        capture_output=True, text=True,
    )
    return result.stdout
```

### 5.5 Comment on Issues from Dashboard

Press `c` on an inbox item or epic:

```
Add comment to issue plan-34:
┌────────────────────────────────────┐
│                                    │
│  Consider adding retry logic...    │
│                                    │
└────────────────────────────────────┘
[Submit]  [Cancel]
```

**Implementation:**
```python
def comment_on_issue(team: str, issue_id: str, comment: str):
    subprocess.run(
        ["bd", "comment", issue_id, "--message", comment],
        cwd=TEAMS_DIR / team,
    )
```

### 5.6 Filtering

Press `F` to open filter panel:

```
Filter view:
  Team:    [All] [demo] [infra]
  Epic:    [All] [health-kb] [api-v2]
  Role:    [All] [developer] [reviewer] [planner] [cto]
  Status:  [All] [working] [idle] [crashed]
  
  [Apply]  [Reset]
```

Filters apply to agents panel, inbox, open tasks, and activity stream simultaneously.

---

## 6. Performance

### 6.1 Lazy Loading for Large Histories

**Problem:** After running for days, `activity.jsonl` grows to 10MB+. Reading all lines every poll is wasteful.

**Solution:**
- Dashboard maintains a `last_offset` per file
- Only read new lines since last poll
- For tail view: keep a circular buffer of last N events in memory
- For historical search: index by timestamp in SQLite, query on demand

### 6.2 Virtual Scrolling for Activity Stream

**Problem:** 1000+ events in the stream widget = slow render.

**Solution:**
- Custom widget that only renders visible rows
- Calculate row height, viewport size, render rows in view + 2 buffer rows
- Similar to `DataTable` virtualization but for variable-height content

### 6.3 Update Batching

**Problem:** If 5 events arrive within 100ms, we don't want 5 re-renders.

**Solution:**
```python
class DashboardApp(App):
    def __init__(self):
        self._pending_updates: list[dict] = []
        self._update_timer = None
    
    def on_bus_event(self, event: dict):
        self._pending_updates.append(event)
        if self._update_timer is None:
            self._update_timer = self.set_timer(0.05, self._flush_updates)
    
    def _flush_updates(self):
        # Apply all pending updates in one batch
        self._update_timer = None
        ...
```

### 6.4 Configurable Refresh Rate

```yaml
# .cto/dashboard.yaml (new config file)
dashboard:
  poll_interval_ms: 100        # event bus poll
  render_batch_ms: 50          # update batching window
  activity_stream_limit: 100   # max events in memory
  agent_log_tail_lines: 50     # lines in agent detail screen
  stale_threshold_ms: 5000     # when data is considered stale
```

---

## 7. Mobile Companion

### 7.1 The Problem

The CTO dashboard is a terminal app. When the human is AFK (commuting, at dinner, in bed), they can't see:
- New inbox items that need approval
- Agents that have crashed
- Epics that have shipped or failed CI

### 7.2 Solution: Dual Companion

**Option A: Simple Web Dashboard (priority)**

A minimal FastAPI/Flask app that serves a mobile-optimized web UI:

```
GET /api/snapshot      → current dashboard state JSON
GET /api/events?since= → SSE stream of new events
POST /api/approve      → approve an inbox item
POST /api/reject       → reject an inbox item
GET /                  → mobile HTML dashboard
```

**Mobile UI (simplified):**
```
┌─────────────────────────┐
│  AI CTO Dashboard       │
│  3 agents · 2 inbox     │
├─────────────────────────┤
│  📥 INBOX (2)           │
│  ─────────────────────  │
│  plan-23  demo  [A][R]  │
│  merge-7  demo  [A][R]  │
├─────────────────────────┤
│  🤖 AGENTS              │
│  ─────────────────────  │
│  🟢 dev-1  working 4m   │
│  🟡 dev-2  idle         │
│  🟢 review working 1m   │
├─────────────────────────┤
│  🚢 EPICS               │
│  ─────────────────────  │
│  health-kb  ◐ dev       │
│  api-v2     ● review    │
└─────────────────────────┘
```

**Technology:**
- Backend: FastAPI (async, lightweight)
- Frontend: Vanilla JS + SSE for push updates (no React needed for this simplicity)
- Auth: Simple API key in header (behind Tailscale or localhost only)
- Host: Same machine as the TUI dashboard, different port (e.g., `:8080`)

**Option B: Telegram Bot (already have Telegram integration)**

Leverage the existing Telegram bot for push notifications:

```python
# On critical event:
message = (
    "🔴 Agent crashed\n"
    f"Agent: {agent}\n"
    f"Reason: {reason}\n"
    f"Epic: {epic_id}\n"
    "[View Dashboard] [Kill Agent] [Restart]"
)
```

**Inline keyboard for actions:**
- Approve / Reject buttons on inbox notifications
- Kill / Restart buttons on crash notifications
- Mark as read on non-critical updates

### 7.3 Push Notifications

**Critical events that trigger push:**
1. New CTO inbox item (when human hasn't approved in 5min)
2. Agent crashed and auto-recovery failed
3. Review escalation (reviewer stuck > 30min)
4. Epic shipped successfully
5. CI failed on epic branch
6. Token budget alert (>80% of daily limit)

**Delivery channels:**
- Desktop: native notification (existing `notify.py`)
- Mobile: Telegram push notification (phone buzzes)
- Web: Browser push notification (if web dashboard is open)

---

## 8. Implementation Phases

### Phase A: Live Activity Stream + Agent Status (Weeks 1-2)

**Goal:** Replace polling with event-driven updates for the two most important panels.

**Tasks:**
- [ ] Implement `dashboard/bus.py` — file-based event bus
- [ ] Emit events from supervisor, reconciler, merge scripts
- [ ] Refactor `ActivityStream` widget to be event-driven
- [ ] Refactor `EpicPipeline` widget to be event-driven
- [ ] Add agent status cards with token counts
- [ ] Add system health screen (basic)
- [ ] Add kill/restart agent buttons
- [ ] Add desktop notifications for agent crashes

**Deliverable:** Dashboard feels "live" — events appear within 100ms of occurrence.

### Phase B: Epic Pipeline + Interactivity (Weeks 3-4)

**Goal:** Rich epic drill-down, inline approvals, diff viewing.

**Tasks:**
- [ ] Horizontal swimlane pipeline renderable
- [ ] Per-epic detail screen with task breakdown
- [ ] Inline approve/reject with artifact preview
- [ ] Diff viewer screen (`v` key)
- [ ] Comment on issues from dashboard (`c` key)
- [ ] Spawn new agent button (`+` key)
- [ ] Filter panel (`F` key)
- [ ] Bulk approve workflow

**Deliverable:** Human can do 90% of CTO work without leaving the dashboard.

### Phase C: Metrics + Cost Tracking + CI Integration (Weeks 5-6)

**Goal:** Full observability — know what's costing money and what's broken.

**Tasks:**
- [ ] Token cost accumulator per epic / per agent / global
- [ ] Cost alert system (budget thresholds)
- [ ] CI status fetcher (GitHub Actions API)
- [ ] CI status display in pipeline and epic detail
- [ ] Queue depth charts with sparklines
- [ ] Agent crash history and recovery tracking
- [ ] System health screen (full)
- [ ] Performance optimizations (virtual scrolling, batching)

**Deliverable:** Dashboard shows money, health, and pipeline in one view.

### Phase D: Mobile Companion (Week 7)

**Goal:** CTO can monitor and act from phone.

**Tasks:**
- [ ] FastAPI backend for snapshot + events API
- [ ] SSE endpoint for real-time push to web
- [ ] Mobile-optimized HTML dashboard
- [ ] Telegram bot integration for push notifications
- [ ] Web dashboard hosted at `:8080`
- [ ] Auth/API key protection
- [ ] Browser push notifications

**Deliverable:** Phone buzzes when CTO attention is needed. Approve from bed.

---

## 9. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           AI CTO Workspace                               │
│                                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │ Supervisor   │  │ Reconciler   │  │ Merge Script │  │ CI Webhook  │ │
│  │ (tmux)       │  │ (periodic)   │  │ (git)        │  │ (GitHub)    │ │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬──────┘ │
│         │                 │                 │                 │        │
│         └─────────────────┴─────────────────┴─────────────────┘        │
│                                   │                                      │
│                                   ▼                                      │
│                    ┌──────────────────────────────┐                      │
│                    │      Event Bus               │                      │
│                    │  (SQLite queue / file watch) │                      │
│                    │                              │                      │
│                    │  topics:                     │                      │
│                    │    • agent.lifecycle          │                      │
│                    │    • issue.state              │                      │
│                    │    • epic.pipeline            │                      │
│                    │    • ci.status                │                      │
│                    │    • token.usage              │                      │
│                    └─────────────┬────────────────┘                      │
│                                  │                                      │
│         ┌────────────────────────┼────────────────────────┐              │
│         │                        │                        │              │
│         ▼                        ▼                        ▼              │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐         │
│  │  TUI Dashboard│      │ Web Dashboard │      │ Telegram Bot │         │
│  │  (Textual)    │      │ (FastAPI+SSE) │      │ (Push notify)│         │
│  │               │      │               │      │              │         │
│  │  ┌─────────┐  │      │  ┌─────────┐  │      │  ┌─────────┐  │         │
│  │  │Overview │  │      │  │Overview │  │      │  │Snapshot │  │         │
│  │  │Epic     │  │      │  │Inbox    │  │      │  │Alerts   │  │         │
│  │  │Agent    │  │      │  │Agents   │  │      │  │Actions  │  │         │
│  │  │Health   │  │      │  │Epics    │  │      │  │         │  │         │
│  │  │Decision │  │      │  │         │  │      │  │         │  │         │
│  │  └─────────┘  │      │  └─────────┘  │      │  └─────────┘  │         │
│  └──────────────┘      └──────────────┘      └──────────────┘         │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 10. Technology Recommendations

### 10.1 TUI Framework: Stay with Textual

**Why Textual (not Rich TUI or other):**
- Already using it — migration cost is zero
- Mature reactive framework (`reactive` descriptors)
- Built-in async worker support (`run_worker(thread=True)`)
- CSS-like layout system
- Modal screens, focus management, key bindings all solved
- Can mix Rich renderables inside Textual widgets

**What to add:**
- `watchdog` for native file watching (optional, Phase 2)
- Custom virtual-scrolled widget for activity stream (build on Textual's `DataTable` pattern)

### 10.2 Web Framework: FastAPI

**Why FastAPI:**
- Native async — perfect for SSE streaming
- Minimal boilerplate
- Auto-generated API docs (`/docs`)
- Already in Python ecosystem (no new language)

**Routes:**
```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

@app.get("/api/snapshot")
async def snapshot() -> DashboardState:
    ...

@app.get("/api/events")
async def events() -> StreamingResponse:
    # SSE stream
    ...

@app.post("/api/approve")
async def approve(req: ApproveRequest) -> ActionResult:
    ...
```

### 10.3 Event Bus: Start Simple

**Phase 1:** File-based JSONL with 100ms poll. Zero dependencies.
**Phase 2:** `watchdog` for native file watching. One dependency.
**Phase 3 (future):** If multi-machine deployment needed, upgrade to Redis pub/sub or NATS.

### 10.4 Data Storage

| Data | Store | Why |
|------|-------|-----|
| Activity events | JSONL (per-team) | Append-only, human-readable, log-structured |
| Event bus stream | SQLite (`events.db`) | Queryable, transactional, no new process |
| Token costs | SQLite (`costs.db`) | Aggregation, time-series queries |
| Dashboard config | YAML (`~/.config/aicto/dashboard.yaml`) | Human-editable |
| CI status | In-memory cache (5min TTL) | Ephemeral, fast refresh |

---

## 11. Cost Estimates

### 11.1 Dashboard Infrastructure Cost

The dashboard itself consumes negligible resources:

| Component | CPU | Memory | Notes |
|-----------|-----|--------|-------|
| TUI Dashboard | <1% | ~20MB | Python process, mostly idle |
| Event bus watcher | <1% | ~10MB | File I/O bound |
| FastAPI web backend | <1% | ~30MB | Only when mobile active |
| SQLite | 0% (in-process) | ~5MB | Embedded |
| **Total** | **<5%** | **~65MB** | **Negligible** |

### 11.2 Token Cost for Dashboard-Related LLM Calls

The dashboard does NOT call LLMs directly. It is a pure observer/controller.

However, the *system* it monitors has costs:

| Scenario | Current ( polling) | New (event-driven) | Savings |
|----------|-------------------|-------------------|---------|
| Agent iteration overhead | 5-15s spawn + context re-read | <1s wake from sleep | **~80% faster** |
| Context window waste | 20-30% orientation tax | <5% (persistent state) | **~75% less waste** |
| Dashboard polling cost | 0 tokens | 0 tokens | — |
| Human context switching | ~10min per epic (2 approvals) | ~2min per epic (exceptions only) | **~80% less human time** |

**Estimated monthly savings (if running 5 teams, 10 epics):**
- Token savings from persistent agents: ~$200-400/month
- Human time savings: 8-10 hours/month
- Faster ship times: priceless

### 11.3 CI Integration Cost

GitHub Actions API calls: 60 requests/hour × $0.002 = ~$0.12/month (free tier covers this).

---

## 12. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Event bus adds complexity | Low | Medium | Start with file-based; upgrade only if needed |
| Textual performance at scale | Low | Medium | Virtual scrolling + batching; tested patterns |
| Web dashboard security | Medium | High | API key auth, localhost-only by default, Tailscale for remote |
| Mobile companion abandoned | Low | Low | Telegram bot is simpler fallback; web is optional |
| Over-notification fatigue | Medium | Medium | Strict notification rules; human can mute per epic |
| Breaking existing workflow | Low | High | Backward compatible; old `top.py` and `cmdcenter.py` kept until ready |

---

## 13. Success Metrics

| Metric | Current | Phase A | Phase B | Phase C | Phase D |
|--------|---------|---------|---------|---------|---------|
| Event latency (detect → display) | 1-2s (poll) | <100ms | <100ms | <100ms | <100ms |
| Dashboard CPU usage | ~15% (bd polling) | <5% | <5% | <5% | <5% |
| Inbox approval time | ~10min (context switch) | ~5min | ~2min | ~2min | ~1min (mobile) |
| Human actions per epic | 2 (breakdown + plan) | 2 | 1-2 | 0-1 (auto-approve) | 0-1 |
| Crash detection time | Manual | <5s | <5s | <5s | <5s (push) |
| Token cost visibility | None | Per epic | Per epic + agent | Full | Full |

---

## 14. Appendix: File Structure

```
dashboard/
├── ctodashboard.py              # Main TUI app (evolved)
├── top.py                       # Legacy read-only (keep until migration done)
├── cmdcenter.py                 # Legacy interactive (keep until migration done)
├── __init__.py
│
├── bus.py                       # NEW: Event bus (file-based → watchdog)
├── ci.py                        # NEW: GitHub Actions integration
├── costs.py                     # NEW: Token cost tracking
├── notify.py                    # EXISTING: Desktop notifications
├── web.py                       # NEW: FastAPI backend for mobile
├── telegram_bot.py              # NEW: Telegram push integration
│
├── widgets/
│   ├── __init__.py
│   ├── activity_stream.py         # EVOLVED: Event-driven + virtual scroll
│   ├── epic_pipeline.py           # EVOLVED: Swimlane + queue depth
│   ├── agent_card.py              # NEW: Rich agent status card
│   ├── queue_chart.py             # NEW: Sparkline bar chart
│   ├── diff_viewer.py             # NEW: Scrollable diff with syntax highlight
│   ├── cost_panel.py              # NEW: Token cost display
│   ├── ci_badge.py                # NEW: CI status indicator
│   └── filter_bar.py              # NEW: Filter controls
│
└── templates/
    └── mobile_dashboard.html      # NEW: Mobile web UI
```

---

## 15. Next Steps

1. **CTO Review:** Rahul reviews this plan, approves/rejects/modifies
2. **Phase A Spike:** Build `bus.py` and wire it to one panel (activity stream)
3. **Parallel Track:** Set up `dashboard/web.py` scaffold with FastAPI
4. **Dogfooding:** Use the new dashboard for actual aicto work to find friction
5. **Iterate:** Adjust phases based on what feels most painful in practice

---

*End of plan. Ready for CTO approval.*
