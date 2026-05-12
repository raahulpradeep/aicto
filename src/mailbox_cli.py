#!/usr/bin/env python3
"""Agent-facing CLI for the §12 mailbox.

Copied at team-start into `<tdir>/.cto/mailbox.py` so prompts can call
`python3 .cto/mailbox.py …`. Identity comes from env vars exported by the
persistent supervisor: AICTO_AGENT_ID, AICTO_ROLE, AICTO_TEAM_DIR.

Subcommands:
    ask     --recipient-role <r> | --recipient-slot <s> --kind <k> --payload <json>
    listen
    reply   <msg_id> --payload <json>
    show    <msg_id>
    expire  [--age-hours N]

All output is JSON on stdout for scripted consumption. Never touches `bd` or
`dolt` — strictly in-process via the team's StateStore.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional


def _bootstrap_path() -> None:
    """Make src/ importable when this file lives at teams/<name>/.cto/mailbox.py."""
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent.parent / "src",  # teams/<name>/.cto/mailbox.py
        here.parent / "src",
        here.parent,
    ]
    for c in candidates:
        if (c / "mailbox.py").is_file() and str(c) not in sys.path:
            sys.path.insert(0, str(c))


_bootstrap_path()

from event_bus import EventBus  # noqa: E402
from mailbox import Mailbox  # noqa: E402
from state_store import StateStore  # noqa: E402


def _identity() -> tuple[str, str, Path]:
    agent_id = os.environ.get("AICTO_AGENT_ID")
    role = os.environ.get("AICTO_ROLE")
    tdir = os.environ.get("AICTO_TEAM_DIR")
    if not (agent_id and role and tdir):
        print(json.dumps({"error": "AICTO_AGENT_ID/AICTO_ROLE/AICTO_TEAM_DIR must be set"}),
              file=sys.stderr)
        sys.exit(2)
    return agent_id, role, Path(tdir).resolve()


def _open_mailbox() -> Mailbox:
    agent_id, role, tdir = _identity()
    db = tdir / ".cto" / "state" / "agent_state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(db)
    bus = EventBus(store, watch_dir=tdir / ".cto" / "state")
    return Mailbox(store, bus, agent_id, role)


def _parse_payload(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"--payload is not valid JSON: {e}"}), file=sys.stderr)
        sys.exit(2)
    if not isinstance(d, dict):
        print(json.dumps({"error": "--payload must decode to a JSON object"}), file=sys.stderr)
        sys.exit(2)
    return d


def cmd_ask(args: argparse.Namespace) -> int:
    mb = _open_mailbox()
    msg_id = mb.ask(
        kind=args.kind,
        payload=_parse_payload(args.payload),
        recipient_slot=args.recipient_slot,
        recipient_role=args.recipient_role,
    )
    print(json.dumps({"msg_id": msg_id}))
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    mb = _open_mailbox()
    msg = mb.listen()
    if msg is None:
        print(json.dumps({"msg": None}))
        return 0
    print(json.dumps({
        "msg_id": msg.id,
        "sender": msg.sender,
        "kind": msg.kind,
        "payload": msg.payload,
        "created_at": msg.created_at,
    }))
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    mb = _open_mailbox()
    mb.reply(args.msg_id, _parse_payload(args.payload))
    print(json.dumps({"ok": True, "msg_id": args.msg_id}))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    mb = _open_mailbox()
    msg = mb._get(args.msg_id)
    if msg is None:
        print(json.dumps({"error": f"msg {args.msg_id} not found"}), file=sys.stderr)
        return 1
    print(json.dumps({
        "msg_id": msg.id,
        "sender": msg.sender,
        "recipient_slot": msg.recipient_slot,
        "recipient_role": msg.recipient_role,
        "kind": msg.kind,
        "payload": msg.payload,
        "status": msg.status,
        "reply_payload": msg.reply_payload,
        "created_at": msg.created_at,
        "claimed_at": msg.claimed_at,
        "replied_at": msg.replied_at,
    }))
    return 0


def cmd_expire(args: argparse.Namespace) -> int:
    mb = _open_mailbox()
    n = mb.expire_old(age_hours=args.age_hours)
    print(json.dumps({"expired": n}))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Agent mailbox CLI (§12).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask")
    a.add_argument("--recipient-slot")
    a.add_argument("--recipient-role")
    a.add_argument("--kind", required=True)
    a.add_argument("--payload", help="JSON object string")
    a.set_defaults(fn=cmd_ask)

    sub.add_parser("listen").set_defaults(fn=cmd_listen)

    r = sub.add_parser("reply")
    r.add_argument("msg_id", type=int)
    r.add_argument("--payload", required=True)
    r.set_defaults(fn=cmd_reply)

    s = sub.add_parser("show")
    s.add_argument("msg_id", type=int)
    s.set_defaults(fn=cmd_show)

    e = sub.add_parser("expire")
    e.add_argument("--age-hours", type=int, default=24)
    e.set_defaults(fn=cmd_expire)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
