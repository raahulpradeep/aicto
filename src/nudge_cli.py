#!/usr/bin/env python3
"""§18: `cto nudge <team> <slot> "<message>"`.

Files a mailbox message with kind=nudge addressed to `<team>:<slot>`. The
recipient agent's persistent supervisor picks it up at the start of its
next iteration and surfaces the directive in the starter prompt. The CLI
side is independent of agent-side dispatch — the row lands in the team's
mailbox immediately.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


def _bootstrap_path() -> None:
    here = Path(__file__).resolve()
    candidates = [here.parent, here.parent / "src"]
    for c in candidates:
        if (c / "mailbox.py").is_file() and str(c) not in sys.path:
            sys.path.insert(0, str(c))


_bootstrap_path()

from event_bus import EventBus  # noqa: E402
from mailbox import Mailbox  # noqa: E402
from state_store import StateStore  # noqa: E402


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Inject a steering nudge into a running agent.")
    ap.add_argument("team_dir", help="Path to <repo>/teams/<name>")
    ap.add_argument("team_name", help="Team name (for slot prefix)")
    ap.add_argument("slot", help="Agent slot — manager, dev-1, review-2, …")
    ap.add_argument("message", help="Steering directive (free-form)")
    args = ap.parse_args(argv)

    tdir = Path(args.team_dir).resolve()
    db = tdir / ".cto" / "state" / "agent_state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(db)
    bus = EventBus(store, watch_dir=tdir / ".cto" / "state")
    cto = Mailbox(store, bus, agent_id="cto", role="cto")

    recipient = f"{args.team_name}:{args.slot}"
    msg_id = cto.ask(
        kind="nudge",
        payload={"message": args.message, "from": "cto"},
        recipient_slot=recipient,
    )
    print(json.dumps({"msg_id": msg_id, "recipient": recipient, "kind": "nudge"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
