"""
agent_inbox.py — HTTP inbox for inter-agent messages + ComfyUI proxy.

AgentOS (and other agents) can POST messages here:
    POST http://192.168.1.225:9191/message
    {"from": "agentos", "text": "Hello!", "session_id": "optional"}

ComfyUI proxy (for agents that can't reach 192.168.1.130:8188 directly):
    POST http://192.168.1.225:9191/comfyui-proxy
    {"workflow": {...}, "timeout": 120}
    → {"ok": true, "images": ["base64png...", ...], "prompt_id": "..."}

    GET http://192.168.1.225:9191/comfyui-proxy/models
    → {"ok": true, "checkpoints": [...]}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import urllib.request
import uuid
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
    """Register a callback that will be called when an agent message arrives.

    fn(text) — sends text to owner Telegram so they can see all agent conversations.
    """
    global _notify_owner
    _notify_owner = fn


def start_inbox_server_background(port: int = INBOX_PORT) -> bool:
    """Start agent inbox HTTP server in a daemon background thread.

    Returns True if server started successfully, False otherwise.
    This is the sync entrypoint for use from synchronous code (vm_launcher.py).
    """
    started = threading.Event()
    failed = threading.Event()

    def _run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_start_and_signal(port, started, failed))
            loop.run_forever()
        except Exception as e:
            log.error("Agent inbox server failed to start: %s", e)
            failed.set()
        finally:
            loop.close()

    t = threading.Thread(target=_run_loop, daemon=True, name="agent-inbox")
    t.start()

    # Wait up to 5 seconds for server to start
    if started.wait(timeout=5.0):
        log.info("✅ Agent inbox started on port %d", port)
        return True
    else:
        log.warning("⚠️ Agent inbox failed to start within 5s")
        return False


async def _start_and_signal(port: int, started: threading.Event, failed: threading.Event) -> None:
    """Internal: start aiohttp server and signal the waiting thread."""
    try:
        from aiohttp import web
    except ImportError:
        log.warning("aiohttp not available — install: pip install aiohttp")
        failed.set()
        return

    app = web.Application()
    app.router.add_post("/message", _handle_message)
    app.router.add_get("/messages", _handle_get_messages)
    app.router.add_get("/health", _handle_health)
    app.router.add_post("/comfyui-proxy", _handle_comfyui_proxy)
    app.router.add_get("/comfyui-proxy/models", _handle_comfyui_models)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Agent inbox listening on port %d", port)
    started.set()

    # Keep running forever
    while True:
        await asyncio.sleep(3600)


async def start_inbox_server(port: int = INBOX_PORT) -> None:
    """Start HTTP inbox server as background asyncio task (for asyncio contexts)."""
    try:
        from aiohttp import web
    except ImportError:
        log.warning("aiohttp not available — agent inbox server not started. Install: pip install aiohttp")
        return

    app = web.Application()
    app.router.add_post("/message", _handle_message)
    app.router.add_get("/messages", _handle_get_messages)
    app.router.add_get("/health", _handle_health)
    app.router.add_post("/comfyui-proxy", _handle_comfyui_proxy)
    app.router.add_get("/comfyui-proxy/models", _handle_comfyui_models)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Agent inbox listening on port %d (http://0.0.0.0:%d/message)", port, port)


# ---------------------------------------------------------------------------
# Handlers — messaging
# ---------------------------------------------------------------------------

async def _handle_health(request):
    from aiohttp import web
    return web.json_response({
        "ok": True,
        "service": "ouroboros-inbox",
        "port": INBOX_PORT,
        "comfyui_host": COMFYUI_HOST,
    })


async def _handle_message(request):
    from aiohttp import web
    try:
        body = await request.json()
        sender = str(body.get("from", "unknown"))
        text = str(body.get("text", ""))
        msg = {
            "id": str(uuid.uuid4()),
            "from": sender,
            "text": text,
            "session_id": body.get("session_id"),
            "reply_to": body.get("reply_to"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "read": False,
        }
        MAILBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MAILBOX_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        log.info("Agent message from %s: %s", sender, text[:80])

        # Notify owner so they can see agent conversations
        if _notify_owner is not None:
            try:
                _notify_owner(f"💬 [{sender}]: {text}")
            except Exception as e:
                log.debug("Failed to notify owner: %s", e)

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


# ---------------------------------------------------------------------------
# Handlers — ComfyUI proxy
# ---------------------------------------------------------------------------

async def _handle_comfyui_proxy(request):
    """Proxy ComfyUI workflow execution for agents that can't reach ComfyUI directly.

    POST /comfyui-proxy
    Body: {"workflow": {...}, "timeout": 120}

    Returns: {"ok": true, "images": ["base64png...", ...], "prompt_id": "..."}
    """
    from aiohttp import web

    try:
        body = await request.json()
        workflow = body.get("workflow")
        timeout = min(int(body.get("timeout", 120)), 300)

        if not workflow:
            return web.json_response({"ok": False, "error": "workflow required"}, status=400)

        # Submit workflow to ComfyUI
        payload = json.dumps({"prompt": workflow}).encode("utf-8")
        req = urllib.request.Request(
            f"{COMFYUI_HOST}/prompt",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
        except Exception as e:
            return web.json_response({"ok": False, "error": f"ComfyUI submit failed: {e}"}, status=502)

        prompt_id = result.get("prompt_id")
        if not prompt_id:
            return web.json_response(
                {"ok": False, "error": "No prompt_id from ComfyUI", "raw": result}, status=502
            )

        log.info("ComfyUI proxy: submitted prompt_id=%s, polling for up to %ds", prompt_id, timeout)

        # Poll history until done
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        images_b64: List[str] = []

        while loop.time() < deadline:
            await asyncio.sleep(3)
            try:
                req2 = urllib.request.Request(
                    f"{COMFYUI_HOST}/history/{prompt_id}", method="GET"
                )
                with urllib.request.urlopen(req2, timeout=10) as resp2:
                    history = json.loads(resp2.read())
            except Exception as e:
                log.warning("ComfyUI proxy: history poll error: %s", e)
                continue

            if prompt_id not in history:
                continue  # Still running

            # Job done — collect images
            outputs = history[prompt_id].get("outputs", {})
            for _node_id, node_out in outputs.items():
                for img in node_out.get("images", []):
                    fname = img.get("filename")
                    subfolder = img.get("subfolder", "")
                    img_type = img.get("type", "output")
                    if not fname:
                        continue
                    view_url = (
                        f"{COMFYUI_HOST}/view?filename={fname}"
                        f"&subfolder={subfolder}&type={img_type}"
                    )
                    try:
                        with urllib.request.urlopen(view_url, timeout=10) as img_resp:
                            img_data = img_resp.read()
                            images_b64.append(base64.b64encode(img_data).decode("utf-8"))
                    except Exception as e:
                        log.warning("ComfyUI proxy: failed to fetch image %s: %s", fname, e)

            log.info("ComfyUI proxy: done, %d images fetched", len(images_b64))
            return web.json_response(
                {
                    "ok": True,
                    "prompt_id": prompt_id,
                    "images": images_b64,
                    "image_count": len(images_b64),
                }
            )

        return web.json_response(
            {"ok": False, "error": f"Timeout after {timeout}s", "prompt_id": prompt_id},
            status=504,
        )

    except Exception as e:
        log.error("ComfyUI proxy error: %s", e)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def _handle_comfyui_models(request):
    """Return available checkpoints from ComfyUI."""
    from aiohttp import web
    try:
        with urllib.request.urlopen(f"{COMFYUI_HOST}/object_info", timeout=10) as resp:
            data = json.loads(resp.read())
        checkpoints = list(
            data.get("CheckpointLoaderSimple", {})
            .get("input", {})
            .get("required", {})
            .get("ckpt_name", [[]])[0]
        )
        return web.json_response({"ok": True, "checkpoints": checkpoints})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=502)


# ---------------------------------------------------------------------------
# Mailbox helpers
# ---------------------------------------------------------------------------

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
