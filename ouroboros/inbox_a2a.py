"""
inbox_a2a.py — A2A protocol handler (Google A2A spec v1.0).

Handles:
  GET  /.well-known/agent.json  — Agent Card for discovery
  POST /a2a                     — JSON-RPC 2.0 task endpoint

Methods: message/send, tasks/get, tools/list, tools/call

Part of the agent_inbox split (P5: each module < 1000 lines).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from ouroboros.inbox_messages import (
    COMFYUI_HOST, MAILBOX_PATH, _notify_owner, save_message
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# A2A task storage (in-memory, expires after 1h)
# ---------------------------------------------------------------------------
_tasks: dict[str, dict] = {}


def _cleanup_tasks() -> None:
    """Remove tasks older than 1 hour."""
    cutoff = datetime.now(timezone.utc).timestamp() - 3600
    stale = [tid for tid, t in _tasks.items() if t.get("_created", 0) < cutoff]
    for tid in stale:
        del _tasks[tid]


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------

async def _handle_agent_card(request):
    """Serve A2A Agent Card per spec v1.0."""
    from aiohttp import web
    try:
        version = open("/home/max2/ouroboros_repo/VERSION").read().strip()
    except Exception:
        version = "6.10.0"
    card = {
        "name": "Ouroboros",
        "description": (
            "Autonomous digital agent — self-creating, self-improving. "
            "Specializes in code generation, image generation (ComfyUI/Flux), "
            "inter-agent coordination, and business automation."
        ),
        "version": version,
        "url": "http://192.168.1.225:9191",
        "supportedInterfaces": [
            {
                "url": "http://192.168.1.225:9191/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "message",
                "name": "Message",
                "description": "Send a text message to Ouroboros. Routed to owner via Telegram.",
                "tags": ["messaging", "coordination"],
                "inputModes": ["text/plain"],
                "outputModes": ["application/json"],
            },
            {
                "id": "comfyui-generate",
                "name": "ComfyUI Image Generation",
                "description": "Run a ComfyUI workflow via proxy. Returns base64 PNG images.",
                "tags": ["image-generation", "comfyui", "flux"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "tools-list",
                "name": "List Tools",
                "description": "List tools available for remote call via tools/call.",
                "tags": ["tools", "discovery"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "tools-call",
                "name": "Call Tool",
                "description": "Execute a named tool: comfyui.generate, comfyui.models, web.search.",
                "tags": ["tools", "execution"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
        ],
    }
    return web.json_response(card)


# ---------------------------------------------------------------------------
# A2A JSON-RPC 2.0 endpoint
# ---------------------------------------------------------------------------

async def _handle_a2a(request):
    """A2A JSON-RPC 2.0 endpoint — handles message/send, tasks/get, tools/list, tools/call."""
    from aiohttp import web

    _cleanup_tasks()

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status=400,
        )

    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    def _ok(result):
        return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _err(code, message):
        return web.json_response(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
        )

    if method == "message/send":
        message = params.get("message", {})
        text = ""
        parts = message.get("parts", [])
        if parts:
            text = parts[0].get("text", "") if isinstance(parts[0], dict) else str(parts[0])
        if not text:
            text = message.get("content", "") or message.get("text", "")

        meta = message.get("metadata", {}) or {}
        sender = meta.get("from") or message.get("role") or "a2a-agent"

        msg_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())

        msg = {
            "id": msg_id,
            "from": sender,
            "to": "ouroboros",
            "text": text,
            "session_id": (params.get("configuration") or {}).get("sessionId"),
            "reply_to": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "read": False,
            "a2a_task_id": task_id,
        }
        save_message(msg)
        log.info("A2A message/send from %s: %s", sender, text[:80])

        if _notify_owner is not None:
            try:
                _notify_owner(f"\U0001f91d [A2A/{sender}]: {text}")
            except Exception as e:
                log.debug("Failed to notify owner: %s", e)

        task = {
            "id": task_id,
            "status": {
                "state": "completed",
                "message": {
                    "role": "agent",
                    "parts": [{"type": "text", "text": "Message received and queued."}],
                },
            },
            "result": {"message_id": msg_id},
            "_created": datetime.now(timezone.utc).timestamp(),
        }
        _tasks[task_id] = task

        return _ok({"task": {k: v for k, v in task.items() if not k.startswith("_")}})

    elif method == "tasks/send":
        # Legacy method alias
        task_id = params.get("id", str(uuid.uuid4()))
        message = params.get("message", {})
        parts = message.get("parts", [])
        text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
        sender = (params.get("metadata") or {}).get("sender", "a2a-agent")

        msg = {
            "id": task_id,
            "from": sender,
            "to": "ouroboros",
            "text": text,
            "session_id": params.get("sessionId"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "read": False,
            "a2a": True,
        }
        save_message(msg)
        log.info("A2A tasks/send from %s: %s", sender, text[:80])

        if _notify_owner is not None:
            try:
                _notify_owner(f"\U0001f91d [A2A/{sender}]: {text}")
            except Exception:
                pass

        return _ok({
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"type": "text", "text": "Message received by Ouroboros."}]}],
        })

    elif method == "tasks/get":
        task_id = params.get("id", "")
        if task_id in _tasks:
            task = _tasks[task_id]
            return _ok({"task": {k: v for k, v in task.items() if not k.startswith("_")}})
        return _ok({"id": task_id, "status": {"state": "completed"}})

    elif method == "tools/list":
        return _ok({"tools": _get_exposed_tools()})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_params = params.get("params", {})
        result = await _execute_a2a_tool(tool_name, tool_params)
        return _ok({"result": result})

    else:
        return _err(-32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Exposed tools registry
# ---------------------------------------------------------------------------

def _get_exposed_tools() -> list:
    """Return list of tools Ouroboros exposes to other agents via A2A."""
    return [
        {
            "name": "comfyui.generate",
            "description": "Generate images using ComfyUI + Flux on RTX 3090.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workflow": {"type": "object", "description": "ComfyUI API-format workflow"},
                    "timeout": {"type": "integer", "description": "Timeout seconds (default 120, max 300)"}
                },
                "required": ["workflow"]
            }
        },
        {
            "name": "comfyui.models",
            "description": "List available ComfyUI checkpoint models on RTX 3090.",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "web.search",
            "description": "Search the web via DuckDuckGo.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        },
    ]


async def _execute_a2a_tool(name: str, params: dict) -> dict:
    """Execute a tool call from another agent via A2A tools/call."""
    if name == "comfyui.generate":
        workflow = params.get("workflow")
        if not workflow:
            return {"ok": False, "error": "workflow required"}
        timeout = min(int(params.get("timeout", 120)), 300)
        payload = json.dumps({"prompt": workflow}).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"{COMFYUI_HOST}/prompt",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
        except Exception as e:
            return {"ok": False, "error": f"ComfyUI submit failed: {e}"}
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            return {"ok": False, "error": "No prompt_id from ComfyUI"}
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        images_b64 = []
        while loop.time() < deadline:
            await asyncio.sleep(3)
            try:
                with urllib.request.urlopen(f"{COMFYUI_HOST}/history/{prompt_id}", timeout=10) as r:
                    history = json.loads(r.read())
            except Exception:
                continue
            if prompt_id not in history:
                continue
            for node_out in history[prompt_id].get("outputs", {}).values():
                for img in node_out.get("images", []):
                    fname = img.get("filename")
                    if not fname:
                        continue
                    url = f"{COMFYUI_HOST}/view?filename={fname}&type=output"
                    try:
                        with urllib.request.urlopen(url, timeout=10) as ir:
                            images_b64.append(base64.b64encode(ir.read()).decode())
                    except Exception:
                        pass
            return {"ok": True, "prompt_id": prompt_id, "images": images_b64,
                    "image_count": len(images_b64)}
        return {"ok": False, "error": f"Timeout after {timeout}s", "prompt_id": prompt_id}

    elif name == "comfyui.models":
        try:
            with urllib.request.urlopen(f"{COMFYUI_HOST}/object_info", timeout=10) as r:
                data = json.loads(r.read())
            checkpoints = list(
                data.get("CheckpointLoaderSimple", {})
                .get("input", {}).get("required", {})
                .get("ckpt_name", [[]])[0]
            )
            return {"ok": True, "checkpoints": checkpoints}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif name == "web.search":
        import urllib.parse
        query = params.get("query", "")
        if not query:
            return {"ok": False, "error": "query required"}
        encoded = urllib.parse.quote(query)
        try:
            url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_redirect=1&no_html=1"
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            abstract = data.get("AbstractText", "") or data.get("Answer", "")
            source = data.get("AbstractURL", "")
            related = [item.get("Text", "") for item in data.get("RelatedTopics", [])[:3]
                       if isinstance(item, dict)]
            return {"ok": True, "query": query, "abstract": abstract,
                    "source": source, "related": related}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    else:
        available = [t["name"] for t in _get_exposed_tools()]
        return {"ok": False, "error": f"Unknown tool: {name}. Available: {available}"}
