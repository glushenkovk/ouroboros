"""
Ouroboros — In-process MCP bridge for claude-agent-sdk.

Wraps ToolRegistry as MCP tools so that claude-agent-sdk can call
Ouroboros tools natively via in-process MCP (no separate subprocess).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Dict

from claude_agent_sdk import tool, create_sdk_mcp_server

log = logging.getLogger(__name__)

# Two executors: general pool + dedicated single-thread for browser (Playwright thread-affinity)
_general_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
_browser_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_BROWSER_TOOLS = frozenset({"browse_page", "browser_action"})

# Tools incompatible with SDK-managed loop:
_EXCLUDED_TOOLS = frozenset({
    "switch_model",           # SDK model is fixed per session
    "compact_context",        # SDK manages its own context
    "list_available_tools",   # All tools exposed via MCP
    "enable_tools",           # All tools exposed via MCP
})


def create_mcp_bridge(registry) -> Dict[str, Any]:
    """Create McpSdkServerConfig wrapping all ToolRegistry tools.

    Returns a dict suitable for ClaudeAgentOptions.mcp_servers values.
    """
    sdk_tools = []

    for entry in registry._entries.values():
        if entry.name in _EXCLUDED_TOOLS:
            continue

        name = entry.name
        desc = entry.schema.get("description", name)
        params = entry.schema.get("parameters", {"type": "object", "properties": {}})

        # Closure — _name default captures current name
        @tool(name, desc, params)
        async def handler(args, _name=name):
            executor = _browser_executor if _name in _BROWSER_TOOLS else _general_executor
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                executor, registry.execute, _name, dict(args)
            )
            return {"content": [{"type": "text", "text": str(result)}]}

        sdk_tools.append(handler)

    server = create_sdk_mcp_server("ouroboros-tools", version="1.0.0", tools=sdk_tools)
    log.info("MCP bridge created with %d tools (excluded %d)", len(sdk_tools), len(_EXCLUDED_TOOLS))
    return server
