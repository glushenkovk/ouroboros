"""
agent_inbox.py — HTTP inbox for inter-agent messages.

AgentOS (and other agents) can POST messages here:
    POST http://192.168.1.130:9191/message
    {"from": "agentos", "text": "Hello!", "session_id": "optional"}

Messages are stored in agent_mailbox.jsonl for retrieval.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

log = logging.getLogger(__name__)

MAILBOX_PATH = Path("/home/max2/ouroboros_data/agent_mailbox.jsonl")
INBOX_PORT = 9191


async def start_inbox_server(port: int = INBOX_PORT) -> None:
    """Start HTTP inbox server as background asyncio task."""
    try:
        from aiohttp import web
    except ImportError:
        log.warning("aiohttp not available — agent inbox server not started. Install: pip install aiohttp")
        return

    app = web.Application()
    app.router.add_post("/message", _handle_message)
    app.router.add_get("/messages", _handle_get_messages)
    app.router.add_get("/health", _handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Agent inbox listening on port %d (http://0.0.0.0:%d/message)", port, port)


async def _handle_health(request):
    from aiohttp import web
    return web.json_response({"ok": True, "service": "ouroboros-inbox", "port": INBOX_PORT})


async def _handle_message(request):
    from aiohttp import web
    try:
        body = await request.json()
        msg = {
            "id": str(uuid.uuid4()),
            "from": str(body.get("from", "unknown")),
            "text": str(body.get("text", "")),
            "session_id": body.get("session_id"),
            "reply_to": body.get("reply_to"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "read": False,
        }
        MAILBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MAILBOX_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        log.info("Agent message from %s: %s", msg["from"], msg["text"][:80])
        return web.json_response({"ok": True, "message_id": msg["id"]})
    except Exception as e:
        log.error("Failed to handle incoming message: %s", e)
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def _handle_get_messages(request):
    from aiohttp import web
    try:
        since = request.rel_url.query.get("since")
        limit = min(int(request.rel_url.query.get("limit", 20)), 100)
        messages = _read_messages(since=since)
        return web.json_response({"messages": messages[-limit:], "total": len(messages)})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


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
