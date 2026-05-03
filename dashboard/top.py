#!/usr/bin/env -S uv run --quiet --with rich python
"""Live `top`-style dashboard for the AI CTO workspace.

Shows two panels:
  1. Per-agent state across every running team (one row per tmux window).
  2. Open issues with role:cto across all teams (the human's inbox).

Refreshes ~1s. Quit with `q` or Ctrl-C. Read-only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import select
import subprocess
import sys
import termios
import tty
from contextlib import contextmanager
from pathlib import Path

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
TEAMS_DIR = ROOT / "teams"
REFRESH_S = 1.0


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


# ---- data gather ----------------------------------------------------------


def gather() -> tuple[list[dict], list[dict], list[str]]:
    """Return (agent_rows, inbox_rows, running_team_names)."""
    agents: list[dict] = []
    inbox: list[dict] = []
    running: list[str] = []

    if not TEAMS_DIR.is_dir():
        return agents, inbox, running

    for tdir in sorted(p for p in TEAMS_DIR.iterdir() if p.is_dir()):
        team = tdir.name
        if not (tdir / ".cto").is_dir():
            continue
        sess = f"cto-{team}"
        if not tmux_alive(sess):
            continue
        running.append(team)

        windows = tmux_windows(sess)

        try:
            ip = json.loads(
                _run(["bd", "list", "--status", "in_progress", "--json"], cwd=tdir) or "[]"
            )
        except json.JSONDecodeError:
            ip = []
        ip_by_assignee = {i.get("assignee"): i for i in ip if i.get("assignee")}

        try:
            cto_issues = json.loads(
                _run(["bd", "list", "--status", "open", "-l", "role:cto", "--json"], cwd=tdir)
                or "[]"
            )
        except json.JSONDecodeError:
            cto_issues = []

        for w in windows:
            agent = f"{team}:{w}"
            agents.append(
                {
                    "agent": agent,
                    "team": team,
                    "window": w,
                    "issue": ip_by_assignee.get(agent),
                }
            )

        for i in cto_issues:
            inbox.append(
                {
                    "team": team,
                    "id": i.get("id", ""),
                    "title": i.get("title", ""),
                    "labels": i.get("labels", []),
                }
            )

    return agents, inbox, running


# ---- render ---------------------------------------------------------------


def _fmt_elapsed(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def render(agents: list[dict], inbox: list[dict], running: list[str]) -> Layout:
    now = dt.datetime.now(dt.timezone.utc)

    # ---- agents table ----
    if not running:
        agent_panel = Panel(
            Text(
                "no running teams — start one with `bin/cto start <team>`",
                style="dim",
                justify="center",
            ),
            title="Agents (0)",
            border_style="dim",
        )
    else:
        t = Table(expand=True, show_lines=False, header_style="bold")
        t.add_column("AGENT", overflow="fold", no_wrap=False)
        t.add_column("TEAM", overflow="fold")
        t.add_column("WINDOW", overflow="fold")
        t.add_column("STATUS", overflow="fold")
        t.add_column("ISSUE", overflow="fold", ratio=2)
        t.add_column("ELAPSED", justify="right", overflow="fold")

        for row in agents:
            agent = row["agent"]
            team = row["team"]
            w = row["window"]
            issue = row["issue"]
            if issue:
                started_iso = (issue.get("started_at") or issue.get("updated_at") or "").replace(
                    "Z", "+00:00"
                )
                try:
                    started = dt.datetime.fromisoformat(started_iso)
                    secs = max(0, int((now - started).total_seconds()))
                    elapsed = _fmt_elapsed(secs)
                except ValueError:
                    elapsed = "—"
                title = _truncate(issue.get("title", ""), 60)
                t.add_row(
                    agent,
                    team,
                    w,
                    Text("working", style="green"),
                    Text(f"{issue['id']}  {title}"),
                    Text(elapsed, style="green"),
                )
            elif w == "manager":
                t.add_row(
                    agent, team, w, Text("active", style="yellow"), Text("—", style="dim"), "—"
                )
            else:
                t.add_row(
                    agent, team, w, Text("idle", style="dim"), Text("—", style="dim"), "—"
                )
        agent_panel = Panel(t, title=f"Agents ({len(agents)})", border_style="cyan")

    # ---- inbox table ----
    inbox_t = Table(expand=True, show_lines=False, header_style="bold")
    inbox_t.add_column("TEAM", overflow="fold")
    inbox_t.add_column("ID", overflow="fold")
    inbox_t.add_column("TITLE", overflow="fold", ratio=3)
    inbox_t.add_column("LABELS", overflow="fold", ratio=1)
    if inbox:
        for row in inbox:
            labels = ",".join(
                lbl for lbl in row.get("labels", []) if not lbl.startswith("role:cto")
            )
            inbox_t.add_row(row["team"], row["id"], row["title"], Text(labels, style="dim"))
    else:
        inbox_t.add_row(
            "[dim]—[/]",
            "[dim]—[/]",
            Text("(nothing waiting on the CTO)", style="dim"),
            "[dim]—[/]",
        )
    inbox_panel = Panel(
        inbox_t,
        title=f"CTO inbox ({len(inbox)})",
        border_style="magenta" if inbox else "dim",
    )

    # ---- footer ----
    ts = dt.datetime.now().strftime("%H:%M:%S")
    teams_summary = ", ".join(running) if running else "—"
    footer = Text(
        f"q quit · {REFRESH_S:g}s refresh · teams: {teams_summary} · {ts}",
        style="dim",
        justify="center",
    )

    layout = Layout()
    layout.split_column(
        Layout(agent_panel, name="agents"),
        Layout(inbox_panel, name="inbox", size=max(6, min(14, len(inbox) + 4))),
        Layout(footer, name="footer", size=1),
    )
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
        # Not a tty (e.g. piped) — just sleep and never quit via key.
        import time

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
                    agents, inbox, running = gather()
                    live.update(render(agents, inbox, running))
                    if stdin_quit_pressed(REFRESH_S):
                        break
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
