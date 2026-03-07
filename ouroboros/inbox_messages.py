"""
inbox_messages.py — Mailbox read/write/pop helpers.

Part of the agent_inbox split (P5: each module < 1000 lines).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

MAILBOX_PATH = Path("/home/max2/ouroboros_data/agent_mailbox.jsonl")
INBOX_PORT = 9191
COMFYUI_HOST = "http://192.168.1.130:8188"

# Optional callback — set by vm_launcher to forward messages to owner Telegram
_notify_owner: Optional[Callable[[str], None]] = None


def set_owner_notifier(fn: Callable[[str], None]) -> None:
    """Register a callback that will be called when an agent message arrives."""
    global _notify_owner
    _notify_owner = fn


def _read_messages(since: str | None = None, unread_only: bool = False) -> List[dict]:
    """Read messages from mailbox (sync, no side effects)."""
    if not MAILBOX_PATH.exists():
        return []
    messages = []
    for line in MAILBOX_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if since and msg.get("timestamp", "") < since:
                continue
            if unread_only and msg.get("read"):
                continue
            messages.append(msg)
        except json.JSONDecodeError:
            pass
    return messages


def get_unread_count() -> int:
    """Count unread messages without modifying mailbox."""
    return len(_read_messages(unread_only=True))


def pop_unread_messages() -> List[dict]:
    """Read unread messages and mark them as read. Returns list of unread messages."""
    if not MAILBOX_PATH.exists():
        return []

    lines = MAILBOX_PATH.read_text(encoding="utf-8").splitlines()
    unread = []
    updated = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if not msg.get("read"):
                unread.append(msg)
                msg["read"] = True
            updated.append(json.dumps(msg, ensure_ascii=False))
        except json.JSONDecodeError:
            updated.append(line)

    if unread:
        MAILBOX_PATH.write_text("\n".join(updated) + "\n", encoding="utf-8")

    return unread


def save_message(msg: dict) -> None:
    """Append a message to the mailbox file."""
    MAILBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MAILBOX_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
