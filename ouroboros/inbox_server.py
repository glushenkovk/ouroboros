"""
inbox_server.py — aiohttp HTTP server, route registration, startup/shutdown.

Endpoints:
  GET  /           → Chat UI
  GET  /chat       → Chat UI (alias)
  POST /message    → Receive agent message
  GET  /messages   → List messages
  GET  /health     → Health check
  POST /comfyui-proxy        → Proxy ComfyUI workflow
  GET  /comfyui-proxy/models → List ComfyUI models
  GET  /.well-known/agent.json → A2A Agent Card
  GET  /.well-known/agent-card.json → A2A Agent Card (alias)
  POST /a2a        → A2A JSON-RPC 2.0

Part of the agent_inbox split (P5: each module < 1000 lines).
"""

from __future__ import annotations

import asyncio
import base64
import html as html_module
import json
import logging
import threading
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import List

from ouroboros.inbox_messages import (
    COMFYUI_HOST, INBOX_PORT, MAILBOX_PATH,
    _notify_owner, _read_messages, save_message,
)
from ouroboros.inbox_a2a import _handle_agent_card, _handle_a2a

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def start_inbox_server_background(port: int = INBOX_PORT) -> bool:
    """Start agent inbox HTTP server in a daemon background thread.

    Returns True if server started successfully, False otherwise.
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
    app.router.add_get("/", _handle_chat_ui)
    app.router.add_get("/chat", _handle_chat_ui)
    app.router.add_post("/message", _handle_message)
    app.router.add_get("/messages", _handle_get_messages)
    app.router.add_get("/health", _handle_health)
    app.router.add_post("/comfyui-proxy", _handle_comfyui_proxy)
    app.router.add_get("/comfyui-proxy/models", _handle_comfyui_models)
    app.router.add_get("/.well-known/agent.json", _handle_agent_card)
    app.router.add_get("/.well-known/agent-card.json", _handle_agent_card)
    app.router.add_post("/a2a", _handle_a2a)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Agent inbox listening on port %d", port)
    started.set()

    while True:
        await asyncio.sleep(3600)


async def start_inbox_server(port: int = INBOX_PORT) -> None:
    """Start HTTP inbox server as background asyncio task (for asyncio contexts)."""
    try:
        from aiohttp import web
    except ImportError:
        log.warning("aiohttp not available — agent inbox server not started.")
        return

    app = web.Application()
    app.router.add_get("/", _handle_chat_ui)
    app.router.add_get("/chat", _handle_chat_ui)
    app.router.add_post("/message", _handle_message)
    app.router.add_get("/messages", _handle_get_messages)
    app.router.add_get("/health", _handle_health)
    app.router.add_post("/comfyui-proxy", _handle_comfyui_proxy)
    app.router.add_get("/comfyui-proxy/models", _handle_comfyui_models)
    app.router.add_get("/.well-known/agent.json", _handle_agent_card)
    app.router.add_get("/.well-known/agent-card.json", _handle_agent_card)
    app.router.add_post("/a2a", _handle_a2a)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Agent inbox listening on port %d (http://0.0.0.0:%d/message)", port, port)


# ---------------------------------------------------------------------------
# Handlers — health
# ---------------------------------------------------------------------------

async def _handle_health(request):
    from aiohttp import web
    return web.json_response({
        "ok": True,
        "service": "ouroboros-inbox",
        "port": INBOX_PORT,
        "comfyui_host": COMFYUI_HOST,
    })


# ---------------------------------------------------------------------------
# Handlers — messaging
# ---------------------------------------------------------------------------

async def _handle_message(request):
    from aiohttp import web
    try:
        body = await request.json()
        sender = str(body.get("from", "unknown"))
        text = str(body.get("text", ""))

        # Echo detection: drop messages that mirror our own recently sent texts
        recent = _read_messages()[-30:]
        sent_texts = {m["text"] for m in recent
                      if m.get("direction") == "outgoing" or m.get("from") == "ouroboros"}
        if text in sent_texts:
            log.debug("Echo detected from %s, dropping", sender)
            return web.json_response({"ok": True, "message_id": None, "echo": True})

        msg = {
            "id": str(uuid.uuid4()),
            "from": sender,
            "to": "ouroboros",
            "text": text,
            "session_id": body.get("session_id"),
            "reply_to": body.get("reply_to"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "read": False,
        }
        save_message(msg)
        log.info("Agent message from %s: %s", sender, text[:80])

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
    """Proxy ComfyUI workflow execution for agents that can't reach ComfyUI directly."""
    from aiohttp import web

    try:
        body = await request.json()
        workflow = body.get("workflow")
        timeout = min(int(body.get("timeout", 120)), 300)

        if not workflow:
            return web.json_response({"ok": False, "error": "workflow required"}, status=400)

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

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        images_b64: List[str] = []

        while loop.time() < deadline:
            await asyncio.sleep(3)
            try:
                req2 = urllib.request.Request(f"{COMFYUI_HOST}/history/{prompt_id}", method="GET")
                with urllib.request.urlopen(req2, timeout=10) as resp2:
                    history = json.loads(resp2.read())
            except Exception as e:
                log.warning("ComfyUI proxy: history poll error: %s", e)
                continue

            if prompt_id not in history:
                continue

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
                            images_b64.append(base64.b64encode(img_resp.read()).decode("utf-8"))
                    except Exception as e:
                        log.warning("ComfyUI proxy: failed to fetch image %s: %s", fname, e)

            log.info("ComfyUI proxy: done, %d images fetched", len(images_b64))
            return web.json_response({
                "ok": True,
                "prompt_id": prompt_id,
                "images": images_b64,
                "image_count": len(images_b64),
            })

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
# Handler — Chat UI
# ---------------------------------------------------------------------------

async def _handle_chat_ui(request):
    """Render SMS-like chat UI showing all agent messages."""
    from aiohttp import web

    messages = _read_messages()

    _palette = [
        "linear-gradient(135deg,#ff6b6b,#ffa500)",
        "linear-gradient(135deg,#48cae4,#0077b6)",
        "linear-gradient(135deg,#80b918,#38b000)",
        "linear-gradient(135deg,#f72585,#b5179e)",
        "linear-gradient(135deg,#f9c74f,#f3722c)",
    ]
    _sender_colors: dict[str, str] = {}
    _color_idx = 0

    def _color_for(sender: str) -> str:
        nonlocal _color_idx
        if sender not in _sender_colors:
            _sender_colors[sender] = _palette[_color_idx % len(_palette)]
            _color_idx += 1
        return _sender_colors[sender]

    bubbles_html_parts = []
    for msg in messages:
        sender = msg.get("from", "unknown")
        text = msg.get("text", "")
        ts = msg.get("timestamp", "")
        read = msg.get("read", False)
        to = msg.get("to", "ouroboros")

        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts_fmt = dt.strftime("%H:%M")
            date_fmt = dt.strftime("%b %d")
        except Exception:
            ts_fmt = ts[:16] if ts else "?"
            date_fmt = ""

        is_me = sender.lower() in ("ouroboros", "me", "self") or msg.get("direction") == "outgoing"
        side_class = "me" if is_me else "them"

        safe_text = html_module.escape(text).replace("\n", "<br>")
        safe_sender = html_module.escape(sender)
        safe_to = html_module.escape(to)
        first_letter = safe_sender[0].upper() if safe_sender else "?"

        read_tick = ""
        if not is_me:
            read_tick = "✓✓" if read else "✓"

        if is_me:
            avatar_html = '<div class="avatar me-avatar">🐍</div>'
            sender_html = ""
            to_html = f'<div class="to-label">→ {safe_to}</div>'
        else:
            color = _color_for(sender)
            avatar_html = f'<div class="avatar" style="background:{color}">{first_letter}</div>'
            sender_html = f'<div class="sender-name">{safe_sender}</div>'
            to_html = ""

        bubble = f"""<div class="message-row {side_class}">
  {avatar_html if not is_me else ""}
  <div class="bubble-wrap">
    {sender_html}
    {to_html}
    <div class="bubble">
      <span class="text">{safe_text}</span>
      <span class="meta">{date_fmt} {ts_fmt} {read_tick}</span>
    </div>
  </div>
  {avatar_html if is_me else ""}
</div>"""
        bubbles_html_parts.append(bubble)

    if not bubbles_html_parts:
        content_html = '<div class="empty">🌑 No messages yet...<br><small>Waiting for agents to connect</small></div>'
    else:
        content_html = "\n".join(bubbles_html_parts)

    total = len(messages)
    unread = sum(1 for m in messages if not m.get("read"))
    badge_html = f'<div class="badge">{unread} unread</div>' if unread else ""

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent Chat · Ouroboros Inbox</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #0d0d12; color: #e0e0e0; height: 100vh; display: flex; flex-direction: column; }}
.header {{ background: #13131f; border-bottom: 1px solid #22223a; padding: 12px 18px;
  display: flex; align-items: center; gap: 12px; position: sticky; top: 0; z-index: 10;
  box-shadow: 0 2px 12px rgba(0,0,0,.4); }}
.header-logo {{ width: 42px; height: 42px; background: linear-gradient(135deg, #6c63ff, #3ecfcf);
  border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: 22px; flex-shrink: 0; }}
.header-text h1 {{ font-size: 15px; font-weight: 700; color: #fff; }}
.header-text .sub {{ font-size: 11px; color: #666; margin-top: 2px; }}
.spacer {{ flex: 1; }}
.badge {{ background: #6c63ff; color: #fff; border-radius: 12px; padding: 3px 10px;
  font-size: 11px; font-weight: 700; }}
.refresh-btn {{ background: none; border: 1px solid #2a2a40; color: #777; border-radius: 8px;
  padding: 5px 13px; cursor: pointer; font-size: 12px; margin-left: 8px; }}
.chat {{ flex: 1; overflow-y: auto; padding: 20px 14px 12px; display: flex;
  flex-direction: column; gap: 14px; }}
.message-row {{ display: flex; align-items: flex-end; gap: 9px; max-width: 78%; }}
.message-row.them {{ align-self: flex-start; }}
.message-row.me {{ align-self: flex-end; flex-direction: row-reverse; }}
.avatar {{ width: 34px; height: 34px; border-radius: 50%; display: flex; align-items: center;
  justify-content: center; font-size: 13px; font-weight: 700; color: #fff; flex-shrink: 0; }}
.me-avatar {{ background: linear-gradient(135deg, #6c63ff, #3ecfcf); font-size: 18px; }}
.bubble-wrap {{ display: flex; flex-direction: column; gap: 3px; }}
.sender-name {{ font-size: 11px; color: #888; padding-left: 5px; font-weight: 600; }}
.to-label {{ font-size: 10px; color: #555; padding-right: 5px; text-align: right; }}
.bubble {{ padding: 9px 14px 7px; border-radius: 18px; line-height: 1.5; word-break: break-word; }}
.them .bubble {{ background: #1c1c2e; border-bottom-left-radius: 5px; color: #dde;
  border: 1px solid #22223a; }}
.me .bubble {{ background: linear-gradient(135deg, #6c63ff 0%, #5248cc 100%);
  border-bottom-right-radius: 5px; color: #fff; }}
.text {{ display: block; font-size: 14px; }}
.meta {{ display: block; font-size: 10px; opacity: .5; margin-top: 5px;
  text-align: right; white-space: nowrap; }}
.empty {{ text-align: center; color: #444; margin: auto; font-size: 14px;
  line-height: 2; padding: 40px; }}
.footer {{ padding: 7px 18px; background: #13131f; border-top: 1px solid #22223a;
  font-size: 11px; color: #444; display: flex; justify-content: space-between; }}
.countdown {{ color: #6c63ff; }}
</style>
</head>
<body>
<div class="header">
  <div class="header-logo">🐍</div>
  <div class="header-text">
    <h1>Agent Chat</h1>
    <div class="sub">192.168.1.225:9191 · Ouroboros ↔ AgentOS</div>
  </div>
  <div class="spacer"></div>
  {badge_html}
  <button class="refresh-btn" onclick="location.reload()">↻ Refresh</button>
</div>
<div class="chat" id="chat">
{content_html}
</div>
<div class="footer">
  <span>{total} messages total</span>
  <span>Auto-refresh in <span class="countdown" id="cd">30</span>s</span>
</div>
<script>
  document.getElementById('chat').scrollTop = 999999;
  let t = 30;
  const cd = document.getElementById('cd');
  setInterval(() => {{ t--; if(cd) cd.textContent=t; if(t<=0) location.reload(); }}, 1000);
</script>
</body>
</html>"""

    return web.Response(text=page, content_type="text/html", charset="utf-8")
