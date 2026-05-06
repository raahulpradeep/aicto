"""Persistent Agent Process base class.

Replaces the per-iteration bash supervisor loop with a long-running
Python process that:
  1. Maintains state across iterations (SQLite persistence)
  2. Invokes claude/kimi/codex CLI but preserves accumulated context
  3. Subscribes to events on the file-based event bus
  4. Recovers automatically after crashes

The old bash supervisor spawned a fresh CLI process every 5-10 seconds,
throwing away all context.  This class keeps the process alive,
incrementally updating a scratchpad and re-hydrating the CLI prompt
with condensed state so the agent still has orientation on wake-up.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from event_bus import EventBus, Subscription
from state_store import AgentState, StateStore


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    team: str
    role: str
    slot: str
    team_dir: Path
    role_prompt_path: Path
    model: str = "sonnet"
    agent_provider: str = "claude"
    permission_mode: str = "bypassPermissions"
    # Polling backoff when no events (seconds)
    idle_sleep: float = 5.0
    # How long before a claimed bd issue is auto-released (seconds)
    claim_timeout: float = 900.0
    # Max consecutive crashes before agent halts
    max_crash_count: int = 5
    # Whether to stream CLI output to tmux pane
    stream_output: bool = True
    # Phase 2: event-driven priority. When True, skip bd polling if events arrive.
    event_priority: bool = True
    # Timeout for event poll before falling back to bd ready (seconds)
    event_poll_timeout: float = 30.0


# ---------------------------------------------------------------------------
# AgentProcess
# ---------------------------------------------------------------------------

class AgentProcess:
    """Long-running agent with SQLite state and event-driven wake-ups.

    Usage:
        proc = AgentProcess(config)
        proc.run()          # blocks until stop() called or max crashes
        proc.stop()         # signal graceful shutdown
    """

    def __init__(self, config: AgentConfig):
        self.cfg = config
        self.team_dir = Path(config.team_dir)
        self._stop_event = threading.Event()
        self._running = False

        # Ensure state dirs exist
        self.state_dir = self.team_dir / ".cto" / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = self.team_dir / ".cto" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir = self.team_dir / ".cto" / "locks"
        self.locks_dir.mkdir(parents=True, exist_ok=True)

        # Persistence
        db_path = self.state_dir / "agent_state.db"
        self.store = StateStore(db_path)
        self.bus = EventBus(self.store, watch_dir=self.state_dir)

        # Load or create state
        self.state = self.store.load_or_create(
            config.team, config.role, config.slot,
            defaults={
                "team_dir": str(config.team_dir),
                "role_prompt_path": str(config.role_prompt_path),
                "model": config.model,
                "agent_provider": config.agent_provider,
                "permission_mode": config.permission_mode,
            },
        )
        self.agent_id = self.state.agent_id
        self.agent_log = self.log_dir / f"{self.agent_id}.log"

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop.  Blocks until stop() is called or too many crashes."""
        self._running = True
        self._log("supervisor up (role=%s, provider=%s, mode=%s, model=%s)",
                   self.cfg.role, self.cfg.agent_provider,
                   self.cfg.permission_mode, self.cfg.model)

        # Crash recovery
        if self.state.status == "crashed":
            self._recover_from_crash()

        self.state.status = "running"
        self.state.last_run_at = self._now()
        self.store.save(self.state)

        # Subscribe to team+role events
        topic = f"team.{self.cfg.team}.{self.cfg.role}"
        with self.bus.subscribe(topic) as sub:
            while not self._stop_event.is_set():
                if self.state.crash_count >= self.cfg.max_crash_count:
                    self._log("HALTED after %d consecutive crashes", self.state.crash_count)
                    self.state.status = "crashed"
                    self.store.save(self.state)
                    break

                try:
                    self._iteration(sub)
                except Exception as exc:
                    self.state.crash_count += 1
                    self._log("iteration exception (crash #%d): %s", self.state.crash_count, exc)
                    self._append_scratchpad(f"CRASH {self.state.crash_count}: {exc}")
                    traceback.print_exc(file=sys.stderr)
                    self.store.save(self.state)
                    time.sleep(min(30 * self.state.crash_count, 300))

        self._running = False
        self._log("supervisor stopped")

    def stop(self) -> None:
        """Request graceful shutdown."""
        self._stop_event.set()
        self.state.status = "stopped"
        self.store.save(self.state)

    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Iteration logic
    # ------------------------------------------------------------------

    def _iteration(self, sub: Subscription) -> None:
        """One unit of work: wait for event, claim task, invoke CLI, save state.

        Phase 2 behaviour:
          - If event_priority is True, block on the event bus for up to
            event_poll_timeout seconds.  Only fall back to legacy bd polling
            if no events arrive in that window.
          - If event_priority is False (legacy), poll events briefly and
            immediately fall back to bd ready polling.
        """
        if self.cfg.event_priority:
            # Block on events first.  If something arrives, process it.
            events = sub.poll(timeout=self.cfg.event_poll_timeout, batch_size=5)
            if events:
                for ev in events:
                    self._log("event: %s %s", ev.topic, ev.payload.get("kind", "?"))
                    self._on_event(ev)
                return
            # No events after full timeout — fall back to legacy poll once.
            self._log("no events after %.0fs; falling back to bd poll", self.cfg.event_poll_timeout)
            self._legacy_poll_and_work()
            return

        # Legacy mode: brief event check, then immediate bd fallback.
        events = sub.poll(timeout=self.cfg.idle_sleep, batch_size=5)
        if events:
            for ev in events:
                self._log("event: %s %s", ev.topic, ev.payload.get("kind", "?"))
                self._on_event(ev)
            return

        # No events — fall back to legacy bd polling for compatibility.
        self._legacy_poll_and_work()

    def _on_event(self, event: Any) -> None:
        """Handle a bus event.  Subclasses may override."""
        kind = event.payload.get("kind")
        if kind == "task_ready" or kind == "task.created":
            task_id = event.payload.get("task_id")
            if task_id:
                self._do_task(task_id)
        elif kind == "dev.assigned":
            task_id = event.payload.get("task_id")
            if task_id and self.cfg.role == "developer":
                self._do_task(task_id)
        elif kind == "review.required":
            task_id = event.payload.get("task_id")
            if task_id and self.cfg.role == "reviewer":
                self._do_task(task_id)
        elif kind == "merge.ready":
            task_id = event.payload.get("task_id")
            if task_id and self.cfg.role == "manager":
                self._do_task(task_id)
        elif kind == "breakdown.approved":
            # Manager should wake to file plan + review
            if self.cfg.role == "manager":
                self._do_manager_pass()
        elif kind == "plan.approved":
            # Manager should wake to file dev tasks
            if self.cfg.role == "manager":
                self._do_manager_pass()
        elif kind == "shutdown":
            self._log("received shutdown event")
            self.stop()
        else:
            self._log("unhandled event kind: %s", kind)

    def _legacy_poll_and_work(self) -> None:
        """Backward-compatible polling via `bd ready`."""
        if self.cfg.role == "manager":
            self._do_manager_pass()
            return

        label = f"role:{self.cfg.role}"
        ready = self._bd(["ready", "--label", label, "--json"])
        if not ready:
            return
        try:
            tasks = json.loads(ready)
        except json.JSONDecodeError:
            return

        tasks = sorted(tasks, key=lambda t: t.get("priority", 2))
        for task in tasks:
            task_id = task.get("id")
            if not task_id:
                continue
            if self._atomic_claim(task_id):
                self._do_task(task_id)
                return

    def _do_manager_pass(self) -> None:
        """Manager agents don't claim single tasks; they do a digest pass."""
        self._telemetry("manager.pass_started", agent=self.agent_id, role=self.cfg.role)
        starter = (
            "Do one pass per your role manual: "
            "(1) close any open kind:status-request issues with a fresh digest; "
            "(2) decompose new kind:epic role:manager issues into a breakdown + approval:breakdown (or merge:breakdown for bypass-cto) — YOU must file the approval/merge yourself after closing the breakdown; "
            "(3) claim and complete any ready kind:merge role:manager issue; "
            "(4) check if any epic is ship-ready and file the kind:merge target:epic yourself if so; "
            "(5) update the team's open kind:status-digest issue (or create one if none). "
            "Make ONE iteration of incremental progress and exit cleanly. Do not loop yourself — "
            "I (the supervisor) will run you again."
        )
        self._invoke_cli(starter, task_id=None)
        # Manager doesn't claim bd issues the same way; it just runs.
        self.state.iteration_count += 1
        self.state.last_run_at = self._now()
        self.store.save(self.state)
        # Interruptible cooldown: wake immediately if anything happens on the
        # bus during the next 10s, so the manager doesn't sleep through new
        # work it could action right away.
        self._wake_or_sleep(10.0)

    def _do_task(self, task_id: str) -> None:
        """Claim, execute, and release a single bd issue."""
        self.state.current_task_id = task_id
        self.state.iteration_count += 1
        self.state.last_run_at = self._now()
        self.store.save(self.state)
        # Gather task metadata for the activity log.
        task_title = ""
        task_kind = ""
        issue_json = self._bd(["show", task_id, "--json"], check=False)
        if issue_json:
            try:
                issue_arr = json.loads(issue_json)
                issue_obj = issue_arr[0] if isinstance(issue_arr, list) else issue_arr
                task_title = issue_obj.get("title", "")
                for lbl in issue_obj.get("labels", []):
                    if lbl.startswith("kind:"):
                        task_kind = lbl
                        break
            except Exception:
                pass
        self._telemetry("agent.claimed", agent=self.agent_id, task_id=task_id,
                        task_title=task_title, task_kind=task_kind, role=self.cfg.role)

        # Read the issue to build context
        issue_json = self._bd(["show", task_id, "--json"])
        issue_summary = ""
        if issue_json:
            try:
                issue_arr = json.loads(issue_json)
                issue_obj = issue_arr[0] if isinstance(issue_arr, list) else issue_arr
                issue_summary = f"title: {issue_obj.get('title', '?')}\n"
                issue_summary += f"description: {issue_obj.get('description', '')[:500]}"
            except Exception:
                issue_summary = "(could not parse issue)"

        # Build the starter prompt with accumulated context
        context_header = self._build_context_header(task_id, issue_summary)

        if self.cfg.role == "developer":
            starter = (
                f"{context_header}\n"
                f"You are agent '{self.agent_id}'. "
                f"You have been assigned bd issue {task_id} (already in_progress). "
                f"Run 'bd show {task_id}' to read it, identify whether it's kind:plan or kind:dev, "
                f"set up your worktree, do the work, run any project tests/lints, commit on the task branch, "
                f"and close the bd issue with a short gist. AFTER closing, you MUST file the next review issue yourself "
                f"(plan review or code review with role:reviewer). Do exactly one bd issue this iteration and exit."
            )
        elif self.cfg.role == "reviewer":
            starter = (
                f"{context_header}\n"
                f"You are agent '{self.agent_id}'. "
                f"You have been assigned bd review issue {task_id} (already in_progress). "
                f"Run 'bd show {task_id}' to identify the upstream and what artifact to read. "
                f"Apply your principal-engineer review; if approved on a plan, file the kind:approval target:plan "
                f"(or kind:merge target:plan for bypass-cto) yourself; if approved on code, file the kind:merge target:code "
                f"for the manager yourself; if changes requested, reopen the upstream AND reset THIS review issue to "
                f"open (assignee='') with `bd dep add <review-id> <upstream-id>` so it blocks until the dev re-closes "
                f"the upstream — do NOT close the review or file a new round-N re-review. Do exactly one review this "
                f"iteration and exit."
            )
        else:
            starter = context_header + "\nDo your work and exit cleanly."

        iter_start = time.time()
        rc = -1
        try:
            rc = self._invoke_cli(starter, task_id=task_id)
        except Exception as exc:
            self._log("CLI invocation raised exception: %s", exc)
            self._append_scratchpad(f"Iteration {self.state.iteration_count} exception: {exc}")
        finally:
            iter_dur = int((time.time() - iter_start) * 1000)
            self._telemetry("agent_iteration_end",
                            agent=self.agent_id,
                            task_id=task_id,
                            exit_code=str(rc),
                            duration_ms=str(iter_dur))

            if rc != 0:
                self._log("CLI exited %d", rc)
                self.state.crash_count += 1
                self._append_scratchpad(f"Iteration {self.state.iteration_count} failed with rc={rc}")
            else:
                # Verify the agent closed the issue; if not, reset it.
                st = self._bd_show_status(task_id)
                if st == "in_progress":
                    self._log("agent did not close %s; resetting to open", task_id)
                    self._bd(["update", task_id, "--status", "open", "--assignee", ""])
                else:
                    self.state.crash_count = 0  # success resets crash streak
                    self._append_scratchpad(f"Iteration {self.state.iteration_count} ok (task={task_id})")

            self._release_claim(task_id)
            self.state.current_task_id = None
            self.state.last_run_at = self._now()
            self.store.save(self.state)

    # ------------------------------------------------------------------
    # CLI invocation
    # ------------------------------------------------------------------

    def _invoke_cli(self, starter: str, task_id: Optional[str] = None) -> int:
        """Invoke the appropriate CLI (claude/kimi/codex) and return exit code."""
        role_file = str(self.cfg.role_prompt_path)
        team_dir = str(self.cfg.team_dir)
        perm = self.cfg.permission_mode
        model = self.cfg.model
        provider = self.cfg.agent_provider

        err_fd, err_path = tempfile.mkstemp(prefix="cto-err-", suffix=".txt")
        os.close(err_fd)

        env = os.environ.copy()
        # Help the child know it's inside a persistent supervisor
        env["AICTO_PERSISTENT"] = "1"
        env["AICTO_AGENT_ID"] = self.agent_id
        if task_id:
            env["AICTO_TASK_ID"] = task_id

        self._telemetry("agent_iteration_start", agent=self.agent_id, task_id=task_id or "")

        try:
            if provider == "kimi":
                cmd = [
                    "kimi", "--print",
                    "--output-format", "stream-json",
                    "--model", model,
                    "--agent-file", role_file,
                    "--prompt", starter,
                    "--work-dir", team_dir,
                ]
                if (self.team_dir / ".mcp.json").exists():
                    cmd += ["--mcp-config-file", str(self.team_dir / ".mcp.json")]
                self._log("invoking kimi (task=%s)", task_id or "none")
                rc = self._run_cmd(cmd, err_path)

            elif provider == "codex":
                codex_args: list[str] = []
                if perm == "bypassPermissions":
                    codex_args += ["--sandbox", "workspace-write"]
                codex_args += ["--model", model]
                full_prompt = f"{Path(role_file).read_text()}\n\n{starter}"
                cmd = ["codex", "exec", "--workdir", team_dir] + codex_args + [full_prompt]
                self._log("invoking codex (task=%s)", task_id or "none")
                rc = self._run_cmd(cmd, err_path)

            else:  # claude (default)
                cmd = [
                    "claude", "--print",
                    "--output-format", "stream-json", "--verbose",
                    "--permission-mode", perm,
                    "--model", model,
                    "--append-system-prompt", Path(role_file).read_text(),
                    starter,
                ]
                self._log("invoking claude (task=%s)", task_id or "none")
                rc = self._run_cmd(cmd, err_path)

            # Capture last 20 lines of stderr for telemetry
            if os.path.getsize(err_path) > 0:
                preview = self._tail(err_path, 20)
                if preview:
                    self._telemetry("agent_output",
                                    agent=self.agent_id,
                                    task_id=task_id or "",
                                    output=preview[:2000])
        finally:
            os.unlink(err_path)

        return rc

    def _run_cmd(self, cmd: list[str], err_path: str) -> int:
        """Run a command, streaming stderr to err_path and teeing to agent log."""
        with open(err_path, "w") as err_f:
            # For tmux visibility we also tee stderr into the agent log.
            # We use a PIPE for stderr, tee manually.
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.cfg.team_dir),
                stdout=subprocess.PIPE if self.cfg.stream_output else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            # Consume stderr in a thread so we don't deadlock on large output.
            def drain_stderr() -> None:
                for line in proc.stderr or []:
                    err_f.write(line)
                    err_f.flush()
                    self._raw_log(line.rstrip())

            t = threading.Thread(target=drain_stderr, daemon=True)
            t.start()
            if self.cfg.stream_output and proc.stdout:
                for line in proc.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
            proc.wait()
            t.join(timeout=5)
            return proc.returncode

    # ------------------------------------------------------------------
    # Atomic claim (backward compatible)
    # ------------------------------------------------------------------

    def _atomic_claim(self, task_id: str) -> bool:
        """Try to atomically claim a bd issue via filesystem mkdir + bd update."""
        lock_dir = self.locks_dir / task_id
        try:
            lock_dir.mkdir(parents=False)
        except FileExistsError:
            return False
        (lock_dir / "owner").write_text(self.agent_id)
        try:
            self._bd(["update", task_id, "--status", "in_progress", "--assignee", self.agent_id])
        except subprocess.CalledProcessError:
            self._release_claim(task_id)
            return False
        self._log("claimed %s", task_id)
        return True

    def _release_claim(self, task_id: str) -> None:
        if not task_id:
            return
        self._bd(["update", task_id, "--status", "open", "--assignee", ""], check=False)
        lock_dir = self.locks_dir / task_id
        if lock_dir.exists():
            import shutil
            shutil.rmtree(lock_dir, ignore_errors=True)
        self._log("released claim on %s", task_id)
        self._telemetry("agent.released", agent=self.agent_id, task_id=task_id, role=self.cfg.role)

    def _recover_from_crash(self) -> None:
        """On startup after a crash, release any stale claims."""
        self._log("boot recovery: crash_count=%d", self.state.crash_count)
        # Reset any in_progress issues assigned to us
        ready = self._bd(["list", "--status", "in_progress", "--json"])
        if ready:
            try:
                issues = json.loads(ready)
                for issue in issues:
                    if issue.get("assignee") == self.agent_id:
                        tid = issue["id"]
                        self._log("boot recovery: resetting zombie %s", tid)
                        self._release_claim(tid)
                        self._telemetry("agent_iteration_end",
                                        agent=self.agent_id,
                                        task_id=tid,
                                        exit_code="-1",
                                        duration_ms="0")
            except Exception:
                pass
        # Clean stale lock dirs
        if self.locks_dir.exists():
            for ld in self.locks_dir.iterdir():
                if not ld.is_dir():
                    continue
                owner_file = ld / "owner"
                if owner_file.exists() and owner_file.read_text().strip() == self.agent_id:
                    import shutil
                    shutil.rmtree(ld, ignore_errors=True)
                    self._log("boot recovery: removed stale lock %s", ld.name)
                    self._telemetry("agent_iteration_end",
                                    agent=self.agent_id,
                                    task_id=ld.name,
                                    exit_code="-1",
                                    duration_ms="0")
        self.state.status = "running"
        self.store.save(self.state)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_context_header(self, task_id: Optional[str], issue_summary: str) -> str:
        """Inject accumulated orientation so the agent isn't lost every iteration."""
        lines = [
            f"# Persistent Agent Context",
            f"agent_id: {self.agent_id}",
            f"iteration: {self.state.iteration_count}",
            f"current_task: {task_id or 'none'}",
            f"model: {self.cfg.model}  provider: {self.cfg.agent_provider}",
        ]
        if self.state.scratchpad:
            lines += [
                "",
                "## Scratchpad (carried across iterations)",
                self.state.scratchpad[:2000],
            ]
        if self.state.accumulated_context:
            ctx = self.state.accumulated_context
            lines += [
                "",
                "## Accumulated Context",
                json.dumps(ctx, indent=2, default=str)[:2000],
            ]
        if issue_summary:
            lines += ["", "## Current Issue", issue_summary]
        return "\n".join(lines)

    def _append_scratchpad(self, note: str) -> None:
        ts = self._now()
        self.state.scratchpad = (self.state.scratchpad + f"\n[{ts}] {note}").strip()
        # Keep scratchpad bounded
        if len(self.state.scratchpad) > 8000:
            self.state.scratchpad = self.state.scratchpad[-8000:]

    def _wake_or_sleep(self, max_seconds: float) -> None:
        """Sleep up to max_seconds, returning early on bd state change.

        Watches the bd journal + agent_state.db mtimes; exits within ~150ms
        of any change. Prevents the manager from sleeping through new work.
        """
        if max_seconds <= 0:
            return
        watch = []
        for rel in (".beads/issues.jsonl", ".beads/issues.db",
                    ".beads/issues.db-wal", ".cto/agent_state.db",
                    ".cto/agent_state.db-wal", ".cto/activity.jsonl"):
            p = self.cfg.team_dir / rel
            if p.exists():
                watch.append(p)
        if not watch:
            time.sleep(max_seconds)
            return

        def snap() -> tuple:
            out = []
            for p in watch:
                try:
                    st = p.stat()
                    out.append((str(p), st.st_mtime_ns, st.st_size))
                except OSError:
                    out.append((str(p), 0, 0))
            return tuple(out)

        baseline = snap()
        deadline = time.monotonic() + max_seconds
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.15, remaining))
            if snap() != baseline:
                return

    def _bd(self, args: list[str], check: bool = True) -> str:
        """Run bd CLI and return stdout."""
        cmd = ["bd"] + args
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.cfg.team_dir),
                capture_output=True,
                text=True,
                check=check,
            )
            return result.stdout
        except subprocess.CalledProcessError as exc:
            if check:
                self._log("bd error: %s", exc.stderr.strip()[:200])
            return ""

    def _bd_show_status(self, task_id: str) -> str:
        out = self._bd(["show", task_id, "--json"], check=False)
        if not out:
            return ""
        try:
            arr = json.loads(out)
            obj = arr[0] if isinstance(arr, list) else arr
            return obj.get("status", "")
        except Exception:
            return ""

    def _telemetry(self, event_type: str, **kwargs: str) -> None:
        """Emit a JSONL event to the team's activity stream."""
        log_path = self.team_dir / ".cto" / "activity.jsonl"
        event = {
            "ts": self._now(),
            "event": event_type,
        }
        event.update(kwargs)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def _log(self, fmt: str, *args: Any) -> None:
        msg = fmt % args
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts} {self.agent_id}] {msg}"
        print(line, flush=True)
        self._raw_log(line)

    def _raw_log(self, line: str) -> None:
        try:
            with open(self.agent_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _tail(path: str, n: int) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-n:]).replace("\n", " ")[:2000]
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Standalone entry-point (replaces supervisor.sh for one slot)
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Persistent agent supervisor")
    parser.add_argument("team_dir", help="Path to team directory")
    parser.add_argument("role", choices=["manager", "developer", "reviewer"])
    parser.add_argument("slot", help="Agent slot name, e.g. dev-1")
    parser.add_argument("role_prompt", help="Path to role prompt file (.md or .yaml)")
    parser.add_argument("permission_mode", default="bypassPermissions")
    parser.add_argument("model", default="sonnet")
    parser.add_argument("agent_provider", default="claude")
    args = parser.parse_args(argv)

    team = Path(args.team_dir).name
    config = AgentConfig(
        team=team,
        role=args.role,
        slot=args.slot,
        team_dir=Path(args.team_dir),
        role_prompt_path=Path(args.role_prompt),
        model=args.model,
        agent_provider=args.agent_provider,
        permission_mode=args.permission_mode,
    )
    proc = AgentProcess(config)

    def _on_signal(signum: int, frame: Any) -> None:
        proc.stop()

    import signal
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    proc.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
