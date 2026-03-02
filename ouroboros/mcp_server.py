"""
Ouroboros — MCP (Model Context Protocol) stdio server.

Wraps the Ouroboros ToolRegistry as an MCP tool server so that Claude CLI
can call Ouroboros tools natively.  Communicates over stdin/stdout using
JSON-RPC 2.0 per the MCP spec (protocol version 2024-11-05).

Entry points:
  - serve(tools_registry, *, max_tools=None)  — run blocking on stdin
  - create_mcp_config(server_script_path, drive_root, repo_dir) -> dict
  - python3 -m ouroboros.mcp_server  (standalone, reads env vars)
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Schema conversion helpers
# ---------------------------------------------------------------------------

def _openai_to_mcp_tool(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an OpenAI function-tool schema to MCP tool format.

    OpenAI: {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    MCP:    {"name": ..., "description": ..., "inputSchema": {...}}
    """
    func = schema.get("function", schema)
    return {
        "name": func["name"],
        "description": func.get("description", ""),
        "inputSchema": func.get("parameters", {"type": "object", "properties": {}}),
    }


def _tools_list(registry, *, max_tools: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return all tool schemas from *registry* in MCP format."""
    schemas = registry.schemas(core_only=False)
    if max_tools is not None:
        schemas = schemas[:max_tools]
    return [_openai_to_mcp_tool(s) for s in schemas]


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _jsonrpc_ok(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# ---------------------------------------------------------------------------
# Request dispatcher
# ---------------------------------------------------------------------------

def _handle_request(
    msg: Dict[str, Any],
    registry,
    *,
    max_tools: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Process one JSON-RPC request and return a response (or None for notifications)."""
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params", {})

    # --- initialize ---
    if method == "initialize":
        return _jsonrpc_ok(req_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ouroboros-mcp", "version": "1.0.0"},
        })

    # --- notifications (no response expected) ---
    if method.startswith("notifications/"):
        return None

    # --- tools/list ---
    if method == "tools/list":
        tools = _tools_list(registry, max_tools=max_tools)
        return _jsonrpc_ok(req_id, {"tools": tools})

    # --- tools/call ---
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            result = registry.execute(tool_name, arguments)
            return _jsonrpc_ok(req_id, {
                "content": [{"type": "text", "text": str(result)}],
            })
        except Exception as exc:
            log.exception("Tool execution error: %s", tool_name)
            return _jsonrpc_error(req_id, -32000, f"Tool error: {exc}", str(exc))

    # --- unknown method ---
    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Stdio server loop
# ---------------------------------------------------------------------------

def serve(tools_registry, *, max_tools: Optional[int] = None) -> None:
    """Run the MCP stdio server, blocking on stdin.

    Reads newline-delimited JSON-RPC messages from stdin, dispatches them,
    and writes responses to stdout.
    """
    log.info("MCP server starting (protocol %s)", MCP_PROTOCOL_VERSION)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            # Write parse error and continue
            resp = _jsonrpc_error(None, -32700, f"Parse error: {exc}")
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        resp = _handle_request(msg, tools_registry, max_tools=max_tools)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

    log.info("MCP server exiting (stdin closed)")


# ---------------------------------------------------------------------------
# MCP config generation (for Claude CLI --mcp-config)
# ---------------------------------------------------------------------------

def create_mcp_config(
    server_script_path: str,
    drive_root: str,
    repo_dir: str,
    tools_config_json: str = "{}",
) -> Dict[str, Any]:
    """Return an MCP config dict suitable for ``claude --mcp-config <file>``.

    The config tells Claude CLI to launch the MCP server as a subprocess.
    Environment variables pass context to the ``__main__`` bootstrap.
    """
    return {
        "mcpServers": {
            "ouroboros": {
                "command": sys.executable,
                "args": ["-m", "ouroboros.mcp_server"],
                "cwd": str(pathlib.Path(server_script_path).resolve().parent.parent),
                "env": {
                    "OUROBOROS_REPO_DIR": str(repo_dir),
                    "OUROBOROS_DRIVE_ROOT": str(drive_root),
                    "OUROBOROS_TOOLS_CONFIG": tools_config_json,
                },
            }
        }
    }


# ---------------------------------------------------------------------------
# Standalone entry point: python3 -m ouroboros.mcp_server
# ---------------------------------------------------------------------------

def _bootstrap_registry():
    """Create a minimal ToolRegistry from environment variables."""
    repo_dir = pathlib.Path(os.environ.get("OUROBOROS_REPO_DIR", ".")).resolve()
    drive_root = pathlib.Path(os.environ.get("OUROBOROS_DRIVE_ROOT", repo_dir / "drive")).resolve()
    tools_config_raw = os.environ.get("OUROBOROS_TOOLS_CONFIG", "{}")

    from ouroboros.tools.registry import ToolRegistry, ToolContext

    registry = ToolRegistry(repo_dir=repo_dir, drive_root=drive_root)

    # Apply any extra context from the config JSON
    try:
        cfg = json.loads(tools_config_raw)
    except json.JSONDecodeError:
        cfg = {}

    ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)
    if cfg.get("branch_dev"):
        ctx.branch_dev = cfg["branch_dev"]
    if cfg.get("task_type"):
        ctx.current_task_type = cfg["task_type"]
    if cfg.get("task_id"):
        ctx.task_id = cfg["task_id"]
    registry.set_context(ctx)

    return registry


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,  # MCP uses stdout for protocol; logs go to stderr
    )
    registry = _bootstrap_registry()
    serve(registry)
