#!/usr/bin/env -S uv run --quiet --with rich python
"""Live `top`-style dashboard for the AI CTO workspace.

Four panels:
  1. Agents       — one row per tmux window across every running team.
  2. CTO inbox    — open issues with role:cto across all teams.
  3. Open tasks   — open work nobody's holding (excludes role:cto + status meta).
  4. Recently closed — last 5 closures across all teams.

Refreshes ~1s. Quit with `q`, `Q`, `Esc`, or Ctrl-C. Read-only.

Known limitation: `bd list --status closed --json` doesn't include the
`assignee` or `started_at` fields, so:
  * "CLOSED-BY" is best-effort: derived from labels (verdict:approved →
    "CTO", or the `role:*` label).
  * "RESOLVE-TIME" is full lifecycle (`closed_at - created_at`), not
    claim-to-close.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import select
import subprocess
import sys
import termios
import time
import tty
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
TEAMS_DIR = ROOT / "teams"
KEY_POLL_S = 0.25  # how often we wake to check stdin for q
RECENT_CLOSED_LIMIT = 5
RECENT_CLOSED_SCAN_PER_TEAM = 30  # bd -n; we sort+trim across teams afterward
# Module-level executor reused across refreshes — bd subprocesses are
# expensive (each invocation cold-starts dolt), so we run the per-team
# queries in parallel.
_POOL = ThreadPoolExecutor(max_workers=12)


# ---- shell helpers --------------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 4.0) -> str:
    try:
        r = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def tmux_alive(session: str) -> bool:
    return (
        subprocess.call(
            ["tmux", "has-session", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        == 0
    )


def tmux_windows(session: str) -> list[str]:
    out = _run(["tmux", "list-windows", "-t", session, "-F", "#W"])
    return [line.strip() for line in out.splitlines() if line.strip()]


# ---- bd query -------------------------------------------------------------


def _bd_json(args: list[str], cwd: Path) -> list:
    try:
        return json.loads(_run(["bd", *args, "--json"], cwd=cwd) or "[]")
    except json.JSONDecodeError:
        return []


# ---- data shapers ---------------------------------------------------------


META_LABELS = {"kind:status-digest", "kind:status-request"}


def _kind(labels: list[str]) -> str:
    for lbl in labels or []:
        if lbl.startswith("kind:"):
            return lbl[len("kind:") :]
    return "—"


def _is_meta(labels: list[str]) -> bool:
    return any(lbl in META_LABELS for lbl in labels or [])


def _classify_closed(issue: dict) -> str:
    """Best-effort closer identification for a closed bd issue.

    bd doesn't track an explicit closer; we fall back to label hints.
    """
    labels = issue.get("labels") or []
    if "verdict:approved" in labels or "verdict:rejected" in labels:
        return "CTO"
    for lbl in labels:
        if lbl.startswith("role:") and lbl != "role:cto":
            return lbl[len("role:") :]
    return "—"


def _human_duration(seconds: int) -> str:
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s" if s and m < 10 else f"{m}m"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(seconds, 86400)
    h, _ = divmod(rem, 3600)
    return f"{d}d {h}h" if h else f"{d}d"


def _parse_iso(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _resolve_time(issue: dict) -> str:
    end = _parse_iso(issue.get("closed_at") or "")
    start = _parse_iso(issue.get("started_at") or "") or _parse_iso(issue.get("created_at") or "")
    if not (end and start):
        return "—"
    return _human_duration(int((end - start).total_seconds()))


def _age(issue: dict, now: dt.datetime) -> str:
    created = _parse_iso(issue.get("created_at") or "")
    if not created:
        return "—"
    return _human_duration(int((now - created).total_seconds()))


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_elapsed(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# ---- per-team gather ------------------------------------------------------


def _read_team_config(tdir: Path) -> dict:
    """Parse .cto/config.yaml for agentProvider and model."""
    cfg = {}
    cfg_path = tdir / ".cto" / "config.yaml"
    try:
        with open(cfg_path) as f:
            for line in f:
                line = line.split("#", 1)[0].rstrip()
                if not line or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip().strip('"\'')
                cfg[k] = v
    except OSError:
        pass
    return cfg


def _gather_team(team: str, tdir: Path):
    """Return (agent_rows, inbox_rows, open_rows, closed_rows) or None.

    Three bd queries fired in parallel; the broader `--status open`
    query covers both the CTO inbox and the Open Tasks panels.
    """
    sess = f"cto-{team}"
    if not tmux_alive(sess):
        return None

    windows = tmux_windows(sess)
    cfg = _read_team_config(tdir)
    provider = cfg.get("agentProvider", "claude")
    model = cfg.get("model", "—")

    f_ip = _POOL.submit(_bd_json, ["list", "--status", "in_progress"], tdir)
    f_open = _POOL.submit(_bd_json, ["list", "--status", "open"], tdir)
    f_closed = _POOL.submit(
        _bd_json,
        ["list", "--status", "closed", "-n", str(RECENT_CLOSED_SCAN_PER_TEAM)],
        tdir,
    )
    ip = f_ip.result()
    op = f_open.result()
    cl = f_closed.result()

    ip_by_assignee = {i["assignee"]: i for i in ip if i.get("assignee")}
    agent_rows = [
        {
            "agent": f"{team}:{w}",
            "team": team,
            "window": w,
            "issue": ip_by_assignee.get(f"{team}:{w}"),
            "provider": provider,
            "model": model,
        }
        for w in windows
    ]

    inbox_rows = [
        {"team": team, **i} for i in op if "role:cto" in (i.get("labels") or [])
    ]
    open_rows = [
        {"team": team, **i}
        for i in op
        if "role:cto" not in (i.get("labels") or []) and not _is_meta(i.get("labels") or [])
    ]
    closed_rows = [{"team": team, **i} for i in cl]
    return agent_rows, inbox_rows, open_rows, closed_rows


def gather():
    """Return (agents, inbox, open_tasks, closed_recent, running)."""
    agents: list[dict] = []
    inbox: list[dict] = []
    open_tasks: list[dict] = []
    closed: list[dict] = []
    running: list[str] = []

    if not TEAMS_DIR.is_dir():
        return agents, inbox, open_tasks, closed, running

    teams = [
        p for p in sorted(TEAMS_DIR.iterdir()) if p.is_dir() and (p / ".cto").is_dir()
    ]
    futures = {team.name: _POOL.submit(_gather_team, team.name, team) for team in teams}
    for team_name, fut in futures.items():
        result = fut.result()
        if result is None:
            continue
        running.append(team_name)
        a_rows, i_rows, o_rows, c_rows = result
        agents.extend(a_rows)
        inbox.extend(i_rows)
        open_tasks.extend(o_rows)
        closed.extend(c_rows)

    # sort closed across teams by closed_at desc, take top 5
    closed.sort(key=lambda r: r.get("closed_at") or "", reverse=True)
    closed_recent = closed[:RECENT_CLOSED_LIMIT]

    return agents, inbox, open_tasks, closed_recent, running


# ---- panel builders -------------------------------------------------------


def _agent_panel(agents: list[dict], running: list[str], now: dt.datetime) -> Panel:
    if not running:
        return Panel(
            Text(
                "no running teams — start one with `bin/cto start <team>`",
                style="dim",
                justify="center",
            ),
            title="Agents (0)",
            border_style="dim",
        )

    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("AGENT", overflow="fold")
    t.add_column("TEAM", overflow="fold")
    t.add_column("WINDOW", overflow="fold")
    t.add_column("PROVIDER", overflow="fold")
    t.add_column("MODEL", overflow="fold")
    t.add_column("STATUS", overflow="fold")
    t.add_column("ISSUE", overflow="fold", ratio=2)
    t.add_column("ELAPSED", justify="right", overflow="fold")

    for row in agents:
        agent = row["agent"]
        team = row["team"]
        w = row["window"]
        provider = row.get("provider", "claude")
        model = row.get("model", "—")
        issue = row["issue"]
        if issue:
            started = _parse_iso(issue.get("started_at") or issue.get("updated_at") or "")
            secs = int((now - started).total_seconds()) if started else 0
            elapsed = _fmt_elapsed(max(0, secs)) if started else "—"
            title = _truncate(issue.get("title", ""), 50)
            t.add_row(
                agent,
                team,
                w,
                provider,
                model,
                Text("working", style="green"),
                Text(f"{issue['id']}  {title}"),
                Text(elapsed, style="green"),
            )
        elif w == "manager":
            t.add_row(
                agent, team, w, provider, model, Text("active", style="yellow"), Text("—", style="dim"), "—"
            )
        else:
            t.add_row(
                agent, team, w, provider, model, Text("idle", style="dim"), Text("—", style="dim"), "—"
            )
    return Panel(t, title=f"Agents ({len(agents)})", border_style="cyan")


def _inbox_panel(inbox: list[dict]) -> Panel:
    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("TEAM", overflow="fold")
    t.add_column("ID", overflow="fold")
    t.add_column("TITLE", overflow="fold", ratio=3)
    t.add_column("LABELS", overflow="fold", ratio=1)
    if inbox:
        for row in inbox:
            labels = ",".join(
                lbl for lbl in row.get("labels", []) if not lbl.startswith("role:cto")
            )
            t.add_row(row["team"], row["id"], row.get("title", ""), Text(labels, style="dim"))
    else:
        t.add_row(
            Text("—", style="dim"),
            Text("—", style="dim"),
            Text("(nothing waiting on the CTO)", style="dim"),
            Text("—", style="dim"),
        )
    return Panel(
        t,
        title=f"CTO inbox ({len(inbox)})",
        border_style="magenta" if inbox else "dim",
    )


def _open_panel(open_tasks: list[dict], now: dt.datetime) -> Panel:
    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("TEAM", overflow="fold")
    t.add_column("ID", overflow="fold")
    t.add_column("KIND", overflow="fold")
    t.add_column("TITLE", overflow="fold", ratio=3)
    t.add_column("ASSIGNEE", overflow="fold")
    t.add_column("AGE", overflow="fold", justify="right")
    if open_tasks:
        # Stable sort: priority asc (lower=more urgent), then created_at asc.
        rows = sorted(
            open_tasks,
            key=lambda r: (r.get("priority") or 99, r.get("created_at") or ""),
        )
        for row in rows:
            assignee = row.get("assignee") or ""
            assignee_disp = Text(assignee, style="green") if assignee else Text("—", style="dim")
            t.add_row(
                row["team"],
                row.get("id", ""),
                _kind(row.get("labels") or []),
                _truncate(row.get("title", ""), 60),
                assignee_disp,
                _age(row, now),
            )
    else:
        t.add_row(
            Text("—", style="dim"),
            Text("—", style="dim"),
            Text("—", style="dim"),
            Text("(no open tasks)", style="dim"),
            Text("—", style="dim"),
            Text("—", style="dim"),
        )
    return Panel(
        t,
        title=f"Open tasks ({len(open_tasks)})",
        border_style="blue" if open_tasks else "dim",
    )


def _closed_panel(closed: list[dict]) -> Panel:
    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("TEAM", overflow="fold")
    t.add_column("ID", overflow="fold")
    t.add_column("KIND", overflow="fold")
    t.add_column("TITLE", overflow="fold", ratio=3)
    t.add_column("CLOSED-BY", overflow="fold")
    t.add_column("RESOLVE", overflow="fold", justify="right")
    t.add_column("AT", overflow="fold")
    if closed:
        for row in closed:
            closed_at = _parse_iso(row.get("closed_at") or "")
            at_str = closed_at.astimezone().strftime("%H:%M:%S") if closed_at else "—"
            closer = _classify_closed(row)
            closer_style = "green" if closer == "CTO" else "yellow"
            t.add_row(
                row["team"],
                row.get("id", ""),
                _kind(row.get("labels") or []),
                _truncate(row.get("title", ""), 50),
                Text(closer, style=closer_style),
                _resolve_time(row),
                at_str,
            )
    else:
        t.add_row(
            *[Text("—", style="dim")] * 6,
            Text("(no closures yet)", style="dim"),
        )
    return Panel(
        t,
        title=f"Recently closed ({len(closed)})",
        border_style="green" if closed else "dim",
    )


# ---- render ---------------------------------------------------------------


def render(
    agents: list[dict],
    inbox: list[dict],
    open_tasks: list[dict],
    closed: list[dict],
    running: list[str],
    gather_ms: int = 0,
) -> Layout:
    now = dt.datetime.now(dt.timezone.utc)

    agent_panel = _agent_panel(agents, running, now)
    inbox_panel = _inbox_panel(inbox)
    open_panel = _open_panel(open_tasks, now)
    closed_panel = _closed_panel(closed)

    ts = dt.datetime.now().strftime("%H:%M:%S")
    teams_summary = ", ".join(running) if running else "—"
    footer = Text(
        f"q quit · gather {gather_ms}ms · teams: {teams_summary} · {ts}",
        style="dim",
        justify="center",
    )

    # Top row: agents + inbox side by side, sized to fit agents + a bit
    # of breathing room. Closed has a fixed 8-row footprint. Open gets
    # whatever's left. Footer is one row.
    top_h = max(7, len(agents) + 4)
    layout = Layout()
    layout.split_column(
        Layout(name="top", size=top_h),
        Layout(name="open"),
        Layout(name="closed", size=8),
        Layout(footer, name="footer", size=1),
    )
    layout["top"].split_row(
        Layout(agent_panel, name="agents", ratio=3),
        Layout(inbox_panel, name="inbox", ratio=2),
    )
    layout["open"].update(open_panel)
    layout["closed"].update(closed_panel)
    return layout


# ---- input loop -----------------------------------------------------------


@contextmanager
def cbreak_stdin():
    """Put stdin into cbreak mode so we can poll for keypresses without Enter."""
    if not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def stdin_quit_pressed(timeout: float) -> bool:
    """Block up to `timeout` seconds; return True if the user pressed q/Q/Esc."""
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return False
    rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    if not rlist:
        return False
    try:
        ch = os.read(sys.stdin.fileno(), 1)
    except OSError:
        return False
    return ch in (b"q", b"Q", b"\x1b")  # q, Q, Esc


# ---- main -----------------------------------------------------------------


def main() -> int:
    with cbreak_stdin():
        with Live(refresh_per_second=4, screen=True) as live:
            try:
                while True:
                    t0 = time.monotonic()
                    agents, inbox, open_tasks, closed, running = gather()
                    gather_ms = int((time.monotonic() - t0) * 1000)
                    live.update(
                        render(
                            agents,
                            inbox,
                            open_tasks,
                            closed,
                            running,
                            gather_ms=gather_ms,
                        )
                    )
                    if stdin_quit_pressed(KEY_POLL_S):
                        break
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
