"""
Ouroboros — Claude Agent SDK loop.

Drop-in replacement for run_llm_loop() that uses claude-agent-sdk
to run an agent loop via the claude CLI with Max subscription ($0).

All Ouroboros tools are exposed via in-process MCP bridge.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import queue
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from claude_agent_sdk import (
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    query,
)
from ouroboros.mcp_server import create_mcp_bridge

log = logging.getLogger(__name__)

CLAUDE_CLI_PATH = "/home/max2/.local/bin/claude"


def is_claude_cli_available() -> bool:
    """Return True if the Claude CLI binary exists and is executable."""
    return os.path.isfile(CLAUDE_CLI_PATH) and os.access(CLAUDE_CLI_PATH, os.X_OK)


# ---------------------------------------------------------------------------
# Prompt extraction from OpenAI-format messages
# ---------------------------------------------------------------------------

def _extract_system_prompt(messages: List[Dict[str, Any]]) -> str:
    """Flatten system messages (may have list-of-blocks content) to string."""
    parts = []
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.append("\n\n".join(
                b["text"] for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ))
    return "\n\n".join(parts)


def _extract_user_prompt(messages: List[Dict[str, Any]]) -> str:
    """Extract text from the last user message."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                b["text"] for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
    return ""


def _map_effort(task_type: str) -> str:
    """Map task type to SDK effort level."""
    if task_type in ("evolution", "review"):
        return "high"
    return "medium"


def _is_benign_cleanup_error(exc: BaseException) -> bool:
    """Check if an exception is a benign MCP cleanup error.

    The claude-agent-sdk raises ExceptionGroup during session teardown when
    pending MCP control request handlers try to write to a closing transport.
    This is expected and harmless -- the task has already completed.
    """
    if isinstance(exc, ExceptionGroup):
        return all(_is_benign_cleanup_error(e) for e in exc.exceptions)
    exc_type = type(exc).__name__
    exc_str = str(exc).lower()
    return (
        "CLIConnectionError" in exc_type
        or "not ready for writing" in exc_str
        or ("transport" in exc_str and "closed" in exc_str)
    )


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------

async def _make_prompt_stream(text: str):
    """Yield a single user message as AsyncIterable.

    Using AsyncIterable instead of string prompt is REQUIRED when MCP servers
    are configured. With string prompts, the SDK closes stdin immediately after
    sending the user message, which races with pending MCP control request
    handlers (e.g. notifications/initialized). AsyncIterable mode keeps stdin
    open until the first result is received.
    """
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }


async def _run_async(
    prompt: str,
    options: ClaudeAgentOptions,
    emit_progress: Callable[[str], None],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Run claude-agent-sdk query and collect results."""
    final_text = ""
    llm_trace: Dict[str, Any] = {
        "assistant_notes": [],
        "tool_calls": [],
        "provider": "claude_sdk",
    }
    usage: Dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost": 0.0,
        "provider": "claude_sdk",
        "model": options.model or "sonnet",
        "rounds": 0,
    }
    got_result = False

    try:
        prompt_stream = _make_prompt_stream(prompt)
        async for message in query(prompt=prompt_stream, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    block_type = getattr(block, "type", "")
                    if block_type == "text":
                        text = getattr(block, "text", "")
                        if text:
                            final_text = text
                            try:
                                emit_progress(text[:200])
                            except Exception:
                                pass
                    elif block_type == "tool_use":
                        tool_name = getattr(block, "name", "unknown")
                        llm_trace["tool_calls"].append({
                            "name": tool_name,
                            "id": getattr(block, "id", ""),
                        })

            elif isinstance(message, ResultMessage):
                got_result = True
                result_text = getattr(message, "text", "") or ""
                if result_text:
                    final_text = result_text

                cost = getattr(message, "total_cost_usd", 0) or 0.0
                num_turns = getattr(message, "num_turns", 0) or 0

                usage["cost"] = cost
                usage["rounds"] = num_turns

                msg_usage = getattr(message, "usage", None)
                if msg_usage and isinstance(msg_usage, dict):
                    usage["prompt_tokens"] = msg_usage.get("input_tokens", 0)
                    usage["completion_tokens"] = msg_usage.get("output_tokens", 0)

    except Exception as e:
        if _is_benign_cleanup_error(e):
            # MCP transport cleanup error -- task completed, just log it
            log.warning("Claude SDK cleanup error (benign): %s", e)
        else:
            log.error("Claude SDK query failed: %s", e, exc_info=True)
            llm_trace["error"] = str(e)
            if not final_text:
                final_text = f"Claude SDK error: {e}"

    if final_text:
        llm_trace["assistant_notes"].append(final_text[:320])

    log.info(
        "Claude SDK result: text_len=%d got_result=%s rounds=%d tools=%d",
        len(final_text), got_result, usage.get("rounds", 0),
        len(llm_trace.get("tool_calls", [])),
    )

    return final_text, usage, llm_trace


# ---------------------------------------------------------------------------
# Main entry point (sync wrapper)
# ---------------------------------------------------------------------------

def run_claude_loop(
    messages: List[Dict[str, Any]],
    tools: Any,  # ToolRegistry
    emit_progress: Callable[[str], None],
    task_type: str = "",
    task_id: str = "",
    budget_remaining_usd: Optional[float] = None,
    event_queue: Optional[queue.Queue] = None,
    drive_root: Optional[pathlib.Path] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Run an agent loop using claude-agent-sdk with MCP tools.

    Same return signature as run_llm_loop:
        (final_text, usage_dict, llm_trace_dict)
    """
    if not is_claude_cli_available():
        return (
            f"Error: Claude CLI not found at {CLAUDE_CLI_PATH}.",
            {"cost": 0, "provider": "claude_sdk"},
            {"error": "claude_cli_not_found"},
        )

    # Build prompts
    system_prompt = _extract_system_prompt(messages)
    user_prompt = _extract_user_prompt(messages)
    if not user_prompt:
        user_prompt = "Continue with the current task."

    # Create MCP bridge from ToolRegistry
    mcp_bridge = create_mcp_bridge(tools)

    # Resolve paths
    repo_dir = pathlib.Path(tools._ctx.repo_dir).resolve()

    # Build SDK options
    model = os.environ.get("OUROBOROS_CLAUDE_MODEL", "sonnet")
    max_turns = int(os.environ.get("OUROBOROS_MAX_ROUNDS", "200"))

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        tools=[],  # Disable ALL Claude Code built-in tools
        mcp_servers={"ouroboros-tools": mcp_bridge},
        allowed_tools=["mcp__ouroboros-tools__*"],
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        effort=_map_effort(task_type),
        cwd=str(repo_dir),
        cli_path=CLAUDE_CLI_PATH,
        env={
            "PATH": "/home/max2/.local/bin:/home/max2/ouroboros_venv/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": "/home/max2",
        },
    )

    log.info(
        "Starting Claude SDK loop: task=%s model=%s max_turns=%d effort=%s tools=%d",
        task_id, model, max_turns, options.effort, len(tools._entries),
    )

    # Run async loop -- use new_event_loop (not asyncio.run) for safety in forked workers
    loop = asyncio.new_event_loop()
    try:
        text, usage, llm_trace = loop.run_until_complete(
            _run_async(user_prompt, options, emit_progress)
        )
    except Exception as e:
        log.error("Claude SDK loop crashed: %s", e, exc_info=True)
        text = f"Claude SDK loop error: {e}"
        usage = {"cost": 0, "provider": "claude_sdk", "rounds": 0}
        llm_trace = {"error": str(e), "provider": "claude_sdk"}
    finally:
        loop.close()

    # Emit usage event
    if event_queue is not None:
        try:
            from ouroboros.utils import utc_now_iso
            event_queue.put_nowait({
                "type": "llm_usage",
                "ts": utc_now_iso(),
                "task_id": task_id,
                "model": usage.get("model", model),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "cost": usage.get("cost", 0),
                "category": task_type if task_type in (
                    "evolution", "consciousness", "review", "summarize"
                ) else "task",
            })
        except Exception:
            log.debug("Failed to emit llm_usage event", exc_info=True)

    return text, usage, llm_trace
