"""File-based publish/subscribe event bus.

Phase 1 uses a simple SQLite-backed queue (via StateStore) plus optional
filesystem watcher for low-latency wake-ups.  No external dependencies
(Redis, NATS) required.

Topics map to the workflow events the system cares about:
    team.{team}.{role}      — work ready for that agent type
    task.created            — new issue filed
    plan.approved           — plan passed CTO/reviewer
    code.committed          — developer pushed code
    ci.failed               — GitHub Actions failure
    review.approved         — reviewer signed off
    merge.completed         — manager merged a branch
    epic.shipped            — epic closed

Agents subscribe to their team+role topic and optionally to global
channels.  The bus supports both push (publish) and pull (poll).
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _HAS_WATCHDOG = True
except Exception:  # pragma: no cover
    _HAS_WATCHDOG = False

from state_store import StateStore


@dataclass
class Event:
    id: int
    topic: str
    payload: dict[str, Any]
    created_at: str = ""


class _WakeupSignal:
    """Thread-safe one-shot wake-up.  Multiple publishes collapse into one wake."""

    def __init__(self):
        self._cond = threading.Condition()
        self._flag = False

    def wake(self) -> None:
        with self._cond:
            self._flag = True
            self._cond.notify_all()

    def wait(self, timeout: Optional[float] = None) -> bool:
        with self._cond:
            if self._flag:
                self._flag = False
                return True
            ok = self._cond.wait(timeout)
            if ok:
                self._flag = False
            return ok

    def clear(self) -> None:
        with self._cond:
            self._flag = False


class EventBus:
    """Lightweight event bus backed by SQLite with optional file-watch acceleration.

    Each team gets its own event bus instance pointing at the team's
    `.cto/agent_state.db`.  Global events (like ci.failed) are also
    stored there for simplicity — a future Phase could centralise.
    """

    def __init__(self, store: StateStore, watch_dir: Optional[Path] = None):
        self.store = store
        self.watch_dir = watch_dir
        self._subscribers: dict[str, list[_WakeupSignal]] = {}
        self._sub_lock = threading.Lock()
        self._observer: Optional[Any] = None
        self._handler: Optional[Any] = None
        if _HAS_WATCHDOG and watch_dir and watch_dir.exists():
            self._start_watcher()

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish(self, topic: str, payload: dict[str, Any]) -> int:
        """Publish an event. Returns the event id."""
        event_id = self.store.append_event(topic, payload)
        # Also write a lightweight sentinel file for filesystem watchers.
        if self.watch_dir:
            sentinel = self.watch_dir / f".{topic}.last"
            try:
                sentinel.write_text(str(event_id))
            except OSError:
                pass
        # Wake any in-process subscribers.
        with self._sub_lock:
            for sig in self._subscribers.get(topic, []):
                sig.wake()
        return event_id

    # ------------------------------------------------------------------
    # Subscribing / Listening
    # ------------------------------------------------------------------

    def subscribe(self, topic: str) -> "Subscription":
        """Return a Subscription handle for polling events on a topic."""
        return Subscription(self, topic)

    def _register_wakeup(self, topic: str, sig: _WakeupSignal) -> None:
        with self._sub_lock:
            self._subscribers.setdefault(topic, []).append(sig)

    def _unregister_wakeup(self, topic: str, sig: _WakeupSignal) -> None:
        with self._sub_lock:
            lst = self._subscribers.get(topic, [])
            if sig in lst:
                lst.remove(sig)

    # ------------------------------------------------------------------
    # Watcher (optional, reduces latency when available)
    # ------------------------------------------------------------------

    def _start_watcher(self) -> None:
        if not _HAS_WATCHDOG or self.watch_dir is None:
            return

        bus = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:
                # Any filesystem change in the watch_dir wakes all subscribers.
                # This is coarse but safe — the subscriber will poll and only
                # process genuinely new events.
                with bus._sub_lock:
                    for sigs in bus._subscribers.values():
                        for s in sigs:
                            s.wake()

        self._handler = _Handler()
        self._observer = Observer()
        self._observer.schedule(self._handler, str(self.watch_dir), recursive=False)
        self._observer.start()

    def stop_watcher(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None


class Subscription:
    """Pull-based subscription to a topic.  Iterating yields new Events."""

    def __init__(self, bus: EventBus, topic: str):
        self.bus = bus
        self.topic = topic
        self.last_id = bus.store.latest_event_id(topic)
        self._wakeup = _WakeupSignal()
        self._closed = False
        self.bus._register_wakeup(topic, self._wakeup)

    def poll(self, timeout: Optional[float] = None, batch_size: int = 10) -> list[Event]:
        """Block up to *timeout* seconds, then return any new events."""
        if self._closed:
            return []
        # Fast path: check without sleeping.
        rows = self.bus.store.events_since(self.topic, self.last_id, limit=batch_size)
        if rows:
            events = [Event(id=r["id"], topic=self.topic, payload=r["payload"], created_at=r["created_at"]) for r in rows]
            self.last_id = events[-1].id
            return events
        # Slow path: sleep until woken or timeout.
        if timeout:
            self._wakeup.wait(timeout)
        # Re-check after wake.
        rows = self.bus.store.events_since(self.topic, self.last_id, limit=batch_size)
        if rows:
            events = [Event(id=r["id"], topic=self.topic, payload=r["payload"], created_at=r["created_at"]) for r in rows]
            self.last_id = events[-1].id
            return events
        return []

    def close(self) -> None:
        self._closed = True
        self.bus._unregister_wakeup(self.topic, self._wakeup)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __iter__(self):
        """Infinite iterator: yields events as they arrive.

        The caller must arrange graceful shutdown (e.g. signal handler
        calling sub.close()).
        """
        while not self._closed:
            events = self.poll(timeout=5.0)
            for ev in events:
                yield ev
