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
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import requests as _requests

from claude_agent_sdk import (
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
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
# Claude usage-limit detection + automatic fallback
# ---------------------------------------------------------------------------

_LIMIT_PHRASES = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "overloaded",
    "too many requests",
    "daily limit",
    "monthly limit",
    "exceeded your",
    "claude.ai/upgrade",
)


def _is_limit_error(text: str) -> bool:
    """Return True if text indicates a Claude usage/rate limit hit."""
    low = text.lower()
    return any(p in low for p in _LIMIT_PHRASES)


def _ollama_chat_fallback(
    messages: List[Dict[str, Any]],
    reason: str,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Fallback when Claude hits limits: Ollama qwen2.5:32b → cheap OpenRouter.

    Returns (text, usage, llm_trace) in the same format as run_claude_sdk_loop.
    ENV overrides:
      OLLAMA_CHAT_HOST  — Ollama host (default: OLLAMA_HOST or 192.168.1.130:11434)
      OLLAMA_CHAT_MODEL — local chat model (default: qwen2.5:32b)
      CHEAP_FALLBACK_MODEL — OpenRouter fallback (default: deepseek/deepseek-r1-distill-qwen-7b)
    """
    ollama_host = os.environ.get(
        "OLLAMA_CHAT_HOST",
        os.environ.get("OLLAMA_HOST", "http://192.168.1.130:11434"),
    )
    ollama_model = os.environ.get("OLLAMA_CHAT_MODEL", "qwen2.5:32b")
    cheap_model = os.environ.get(
        "CHEAP_FALLBACK_MODEL", "deepseek/deepseek-r1-distill-qwen-7b"
    )

    log.warning(
        "[FALLBACK] Claude limit hit (%s) → Ollama %s @ %s",
        reason[:80], ollama_model, ollama_host,
    )

    # --- Try Ollama first ---
    try:
        from ouroboros.llm import OllamaClient
        ollama = OllamaClient(host=ollama_host)
        text_msg, ollama_usage = ollama.chat(
            messages=messages, model=ollama_model, max_tokens=4096
        )
        text = text_msg.get("content", "") if isinstance(text_msg, dict) else str(text_msg)
        if text:
            log.info("[FALLBACK] Ollama responded: %d chars", len(text))
            ollama_usage["fallback"] = f"ollama:{ollama_model}"
            trace = {
                "provider": "ollama_fallback",
                "model": ollama_model,
                "reason": reason,
                "assistant_notes": [text[:320]],
                "tool_calls": [],
            }
            return text, ollama_usage, trace
    except Exception as ollama_err:
        log.warning("[FALLBACK] Ollama failed: %s → trying cheap OpenRouter", ollama_err)

    # --- Ollama unavailable → cheap OpenRouter ---
    try:
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not openrouter_key:
            raise RuntimeError("No OPENROUTER_API_KEY")

        simple_messages = []
        for m in messages:
            role = m.get("role", "user")
            if role not in ("system", "user", "assistant"):
                continue
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            simple_messages.append({"role": role, "content": content})

        resp = _requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {openrouter_key}",
                "Content-Type": "application/json",
            },
            json={"model": cheap_model, "messages": simple_messages, "max_tokens": 4096},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        # ~$0.14/M tokens estimate for cheap distill models
        cost = data.get("usage", {}).get("total_tokens", 0) * 0.00000014
        or_usage = {"cost": cost, "fallback": f"openrouter:{cheap_model}", "provider": "openrouter"}
        trace = {
            "provider": "openrouter_cheap_fallback",
            "model": cheap_model,
            "reason": reason,
            "assistant_notes": [text[:320]],
            "tool_calls": [],
        }
        log.info("[FALLBACK] OpenRouter cheap responded: %d chars", len(text))
        return text, or_usage, trace
    except Exception as or_err:
        log.error("[FALLBACK] All fallbacks failed: %s", or_err)

    return (
        f"⚠️ Claude usage limit reached and all fallbacks failed. Reason: {reason[:120]}",
        {"cost": 0.0, "fallback": "all_failed", "provider": "none"},
        {"provider": "none", "error": reason, "assistant_notes": [], "tool_calls": []},
    )


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------

async def _make_prompt_stream(text: str):
    """Yield a single user message then block forever.

    Using AsyncIterable is REQUIRED when MCP servers are configured.

    CRITICAL: After yielding the message, we must NOT return. The SDK's
    stream_input() calls end_input() (closes stdin) when this iterator
    exhausts OR after a 60-second timeout. Closing stdin kills the MCP
    transport — all subsequent tool call responses fail with
    "ProcessTransport is not ready for writing".

    By blocking forever (via an Event that never fires), stream_input()
    stays in the async-for loop and stdin remains open for the entire
    session. The SDK cancels this task when the session ends.
    """
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }
    # Block forever — SDK will cancel this task when session ends
    await asyncio.Event().wait()


async def _run_async(
    prompt: str,
    options: ClaudeAgentOptions,
    emit_progress: Callable[[str], None],
    timeout_seconds: int = 600,
    messages: Optional[List[Dict[str, Any]]] = None,
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
    turn_count = 0
    start_time = time.monotonic()
    last_progress_ts = 0.0
    PROGRESS_MIN_INTERVAL = 15  # seconds between progress messages

    try:
        prompt_stream = _make_prompt_stream(prompt)
        async for message in query(prompt=prompt_stream, options=options):
            elapsed = time.monotonic() - start_time

            # Safety timeout — abort if session runs too long
            if elapsed > timeout_seconds:
                log.warning(
                    "Claude SDK session timeout after %.0fs (%d turns, %d tools)",
                    elapsed, turn_count, len(llm_trace["tool_calls"]),
                )
                if not final_text:
                    final_text = f"Session timed out after {int(elapsed)}s with {turn_count} turns."
                break

            if isinstance(message, AssistantMessage):
                turn_count += 1
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text = block.text or ""
                        if text:
                            final_text = text
                            # Throttle progress messages — avoid duplicate for short tasks
                            if elapsed - last_progress_ts >= PROGRESS_MIN_INTERVAL:
                                last_progress_ts = elapsed
                                try:
                                    emit_progress(text[:200])
                                except Exception:
                                    pass
                    elif isinstance(block, ToolUseBlock):
                        tool_name = block.name or "unknown"
                        llm_trace["tool_calls"].append({
                            "name": tool_name,
                            "id": block.id or "",
                        })
                        log.warning(
                            "Claude SDK turn %d: tool=%s elapsed=%.0fs",
                            turn_count, tool_name, elapsed,
                        )

            elif isinstance(message, ResultMessage):
                got_result = True
                # ResultMessage uses 'result' attribute, not 'text'
                result_text = message.result or ""
                if result_text:
                    final_text = result_text

                usage["cost"] = message.total_cost_usd or 0.0
                usage["rounds"] = message.num_turns or 0

                msg_usage = getattr(message, "usage", None)
                if msg_usage and isinstance(msg_usage, dict):
                    usage["prompt_tokens"] = msg_usage.get("input_tokens", 0)
                    usage["completion_tokens"] = msg_usage.get("output_tokens", 0)

                # Exit immediately — don't wait for SDK cleanup
                break

    except asyncio.CancelledError:
        log.warning("Claude SDK session cancelled after %.0fs", time.monotonic() - start_time)
        if not final_text:
            final_text = "Session was cancelled."
    except Exception as e:
        if _is_benign_cleanup_error(e):
            # MCP transport cleanup error -- task completed, just log it
            log.warning("Claude SDK cleanup error (benign): %s", e)
        else:
            log.error("Claude SDK query failed: %s", e, exc_info=True)
            llm_trace["error"] = str(e)
            if not final_text:
                error_text = str(e)
                if _is_limit_error(error_text) and messages is not None:
                    log.warning("[LIMIT] Limit detected in SDK exception → fallback")
                    return _ollama_chat_fallback(messages, error_text[:120])
                final_text = f"Claude SDK error: {e}"

    if final_text:
        llm_trace["assistant_notes"].append(final_text[:320])

    total_elapsed = time.monotonic() - start_time
    log.warning(
        "Claude SDK result: text_len=%d got_result=%s rounds=%d tools=%d elapsed=%.0fs",
        len(final_text), got_result, usage.get("rounds", 0),
        len(llm_trace.get("tool_calls", [])), total_elapsed,
    )

    # Detect limit message in the response text itself
    if final_text and _is_limit_error(final_text) and not usage.get("fallback") and messages is not None:
        log.warning("[LIMIT] Limit detected in response text → fallback")
        return _ollama_chat_fallback(messages, final_text[:120])

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
    max_turns = int(os.environ.get("OUROBOROS_MAX_ROUNDS", "15"))
    timeout_seconds = int(os.environ.get("OUROBOROS_SDK_TIMEOUT", "600"))

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

    log.warning(
        "Starting Claude SDK loop: task=%s model=%s max_turns=%d effort=%s timeout=%ds tools=%d",
        task_id, model, max_turns, options.effort, timeout_seconds, len(tools._entries),
    )

    # Run async loop -- use new_event_loop (not asyncio.run) for safety in forked workers
    loop = asyncio.new_event_loop()
    try:
        # Hard timeout via wait_for — fires even if query() blocks without yielding
        coro = _run_async(user_prompt, options, emit_progress, timeout_seconds, messages)
        text, usage, llm_trace = loop.run_until_complete(
            asyncio.wait_for(coro, timeout=timeout_seconds)
        )
    except asyncio.TimeoutError:
        log.warning("Claude SDK hard timeout after %ds", timeout_seconds)
        text = f"Session timed out after {timeout_seconds}s (hard limit)."
        usage = {"cost": 0, "provider": "claude_sdk", "rounds": 0}
        llm_trace = {"error": "hard_timeout", "provider": "claude_sdk"}
    except Exception as e:
        log.error("Claude SDK loop crashed: %s", e, exc_info=True)
        error_text = str(e)
        if _is_limit_error(error_text):
            log.warning("[LIMIT] Limit detected in outer loop exception → fallback")
            text, usage, llm_trace = _ollama_chat_fallback(messages, error_text[:120])
        else:
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
