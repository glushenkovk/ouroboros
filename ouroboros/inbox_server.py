"""
inbox_server.py — HTTP inbox server using Starlette + uvicorn + a2a-sdk.

A2A endpoints (via a2a-sdk, standard protocol):
  GET  /.well-known/agent-card.json  — Agent Card
  GET  /.well-known/agent.json       — Agent Card (backward compat)
  POST /                             — A2A JSON-RPC 2.0

Custom endpoints:
  POST /message              — Simple agent message inbox
  GET  /messages             — List inbox messages
  GET  /health               — Health check
  POST /comfyui-proxy        — ComfyUI workflow proxy
  GET  /comfyui-proxy/models — List ComfyUI checkpoints
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
from typing import List

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ouroboros.inbox_a2a import make_a2a_app
from ouroboros.inbox_messages import (
    COMFYUI_HOST,
    INBOX_PORT,
    _notify_owner,
    _read_messages,
    save_message,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _build_app() -> Starlette:
    """Build Starlette app with A2A SDK routes + custom inbox routes."""
    a2a = make_a2a_app()
    a2a_routes = a2a.routes()  # /.well-known/agent-card.json + POST /

    custom_routes = [
        Route("/message", _handle_message, methods=["POST"]),
        Route("/messages", _handle_get_messages, methods=["GET"]),
        Route("/health", _handle_health, methods=["GET"]),
        Route("/comfyui-proxy", _handle_comfyui_proxy, methods=["POST"]),
        Route("/comfyui-proxy/models", _handle_comfyui_models, methods=["GET"]),
    ]

    return Starlette(routes=[*a2a_routes, *custom_routes])


_app: Starlette | None = None


def get_app() -> Starlette:
    """Return singleton Starlette app (built on first call)."""
    global _app
    if _app is None:
        _app = _build_app()
    return _app


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def start_inbox_server_background(port: int = INBOX_PORT) -> bool:
    """Start inbox HTTP server in a daemon background thread. Returns True on success."""
    started = threading.Event()

    def _run():
        config = uvicorn.Config(
            get_app(),
            host="0.0.0.0",
            port=port,
            log_level="warning",
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        config.setup_event_loop()
        started.set()
        asyncio.run(server.serve())

    t = threading.Thread(target=_run, daemon=True, name="agent-inbox")
    t.start()

    if started.wait(timeout=5.0):
        log.info("✅ Agent inbox started on port %d", port)
        return True
    log.warning("⚠️ Agent inbox failed to start within 5s")
    return False


async def start_inbox_server(port: int = INBOX_PORT) -> None:
    """Start inbox server as asyncio background task."""
    config = uvicorn.Config(
        get_app(),
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    log.info("Agent inbox started on port %d", port)


# ---------------------------------------------------------------------------
# Handlers — health & messaging
# ---------------------------------------------------------------------------

async def _handle_health(request: Request) -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "service": "ouroboros-inbox",
        "port": INBOX_PORT,
        "comfyui_host": COMFYUI_HOST,
    })


async def _handle_message(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        sender = str(body.get("from", "unknown"))
        text = str(body.get("text", ""))

        # Echo detection: drop messages that mirror our own outgoing texts
        recent = _read_messages()[-30:]
        sent_texts = {m["text"] for m in recent
                      if m.get("direction") == "outgoing" or m.get("from") == "ouroboros"}
        if text in sent_texts:
            log.debug("Echo from %s, dropping", sender)
            return JSONResponse({"ok": True, "message_id": None, "echo": True})

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
        log.info("Message from %s: %s", sender, text[:80])

        if _notify_owner is not None:
            try:
                _notify_owner(f"💬 [{sender}]: {text}")
            except Exception as e:
                log.debug("owner notify failed: %s", e)

        return JSONResponse({"ok": True, "message_id": msg["id"]})
    except Exception as e:
        log.error("handle_message error: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def _handle_get_messages(request: Request) -> JSONResponse:
    try:
        since = request.query_params.get("since")
        limit = min(int(request.query_params.get("limit", 20)), 100)
        messages = _read_messages(since=since)
        return JSONResponse({"messages": messages[-limit:], "total": len(messages)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ---------------------------------------------------------------------------
# Handlers — ComfyUI proxy
# ---------------------------------------------------------------------------

async def _handle_comfyui_proxy(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        workflow = body.get("workflow")
        timeout = min(int(body.get("timeout", 120)), 300)

        if not workflow:
            return JSONResponse({"ok": False, "error": "workflow required"}, status_code=400)

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
            return JSONResponse({"ok": False, "error": f"ComfyUI submit failed: {e}"}, status_code=502)

        prompt_id = result.get("prompt_id")
        if not prompt_id:
            return JSONResponse(
                {"ok": False, "error": "No prompt_id from ComfyUI", "raw": result},
                status_code=502,
            )

        log.info("ComfyUI proxy: submitted %s, polling up to %ds", prompt_id, timeout)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        images_b64: List[str] = []

        while loop.time() < deadline:
            await asyncio.sleep(3)
            try:
                with urllib.request.urlopen(
                    f"{COMFYUI_HOST}/history/{prompt_id}", timeout=10
                ) as r:
                    history = json.loads(r.read())
            except Exception as e:
                log.warning("history poll error: %s", e)
                continue

            if prompt_id not in history:
                continue

            for node_out in history[prompt_id].get("outputs", {}).values():
                for img in node_out.get("images", []):
                    fname = img.get("filename")
                    if not fname:
                        continue
                    view_url = (
                        f"{COMFYUI_HOST}/view?filename={fname}"
                        f"&subfolder={img.get('subfolder', '')}"
                        f"&type={img.get('type', 'output')}"
                    )
                    try:
                        with urllib.request.urlopen(view_url, timeout=10) as ir:
                            images_b64.append(base64.b64encode(ir.read()).decode())
                    except Exception as e:
                        log.warning("image fetch failed: %s", e)

            return JSONResponse({
                "ok": True,
                "prompt_id": prompt_id,
                "images": images_b64,
                "image_count": len(images_b64),
            })

        return JSONResponse(
            {"ok": False, "error": f"Timeout after {timeout}s", "prompt_id": prompt_id},
            status_code=504,
        )

    except Exception as e:
        log.error("comfyui_proxy error: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def _handle_comfyui_models(request: Request) -> JSONResponse:
    try:
        with urllib.request.urlopen(f"{COMFYUI_HOST}/object_info", timeout=10) as r:
            data = json.loads(r.read())
        checkpoints = list(
            data.get("CheckpointLoaderSimple", {})
            .get("input", {})
            .get("required", {})
            .get("ckpt_name", [[]])[0]
        )
        return JSONResponse({"ok": True, "checkpoints": checkpoints})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
