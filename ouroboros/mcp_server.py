"""
Ouroboros — In-process MCP bridge for claude-agent-sdk.

Wraps ToolRegistry as MCP tools so that claude-agent-sdk can call
Ouroboros tools natively via in-process MCP (no separate subprocess).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Dict, Optional

from claude_agent_sdk import tool, create_sdk_mcp_server

log = logging.getLogger(__name__)

# Two executors: general pool + dedicated single-thread for browser (Playwright thread-affinity)
# Lazy initialization — created on first use per-process (safe after fork()).
# Module-level globals were problematic in forked worker processes: threads don't
# survive fork(), leaving pools in an inconsistent state for heavy operations.
_general_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
_browser_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
_BROWSER_TOOLS = frozenset({"browse_page", "browser_action"})

# Tools incompatible with SDK-managed loop:
_EXCLUDED_TOOLS = frozenset({
    "switch_model",           # SDK model is fixed per session
    "compact_context",        # SDK manages its own context
    "list_available_tools",   # All tools exposed via MCP
    "enable_tools",           # All tools exposed via MCP
})


def _get_executor(browser: bool = False) -> concurrent.futures.ThreadPoolExecutor:
    """Return (or lazily create) the appropriate thread pool executor.

    Lazy init ensures each forked worker process gets a fresh executor
    with live threads, avoiding post-fork deadlocks on heavy operations.
    """
    global _general_executor, _browser_executor
    if browser:
        if _browser_executor is None:
            _browser_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        return _browser_executor
    if _general_executor is None:
        _general_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    return _general_executor


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
            executor = _get_executor(browser=_name in _BROWSER_TOOLS)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                executor, registry.execute, _name, dict(args)
            )
            return {"content": [{"type": "text", "text": str(result)}]}

        sdk_tools.append(handler)

    server = create_sdk_mcp_server("ouroboros-tools", version="1.0.0", tools=sdk_tools)
    log.info("MCP bridge created with %d tools (excluded %d)", len(sdk_tools), len(_EXCLUDED_TOOLS))
    return server
