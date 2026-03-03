"""
Ouroboros — Claude CLI agent loop (MCP-based).

Drop-in replacement for ``run_llm_loop`` that delegates to ``claude`` CLI
with Ouroboros tools exposed via the MCP server (``mcp_server.py``).

Architecture:
  1. Write a temp MCP config file pointing at ``ouroboros.mcp_server``
  2. Build a prompt from the OpenAI-format messages
  3. Invoke ``claude -p … --mcp-config … --output-format stream-json``
  4. Parse stream-json output for the ``result`` event
  5. Return ``(final_text, usage_dict, llm_trace_dict)``
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ouroboros.mcp_server import create_mcp_config

log = logging.getLogger(__name__)

CLAUDE_CLI_PATH = "/home/max2/.local/bin/claude"
CLAUDE_CLI_MODEL = "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_claude_cli_available() -> bool:
    """Return True if the Claude CLI binary exists and is executable."""
    return os.path.isfile(CLAUDE_CLI_PATH) and os.access(CLAUDE_CLI_PATH, os.X_OK)


def _messages_to_prompt(messages: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Extract system prompt and conversation body from OpenAI-format messages.

    Returns (system_prompt, user_prompt).
    """
    system_parts: List[str] = []
    conversation_parts: List[str] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not content:
            continue

        # Handle list-of-blocks content (multimodal messages)
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block["text"])
            content = "\n".join(text_parts)

        if role == "system":
            system_parts.append(content)
        elif role == "user":
            conversation_parts.append(f"[User]: {content}")
        elif role == "assistant":
            conversation_parts.append(f"[Assistant]: {content}")
        # Skip tool messages — Claude CLI handles tools natively via MCP

    system_prompt = "\n\n".join(system_parts)
    user_prompt = "\n\n".join(conversation_parts)
    return system_prompt, user_prompt


def _write_mcp_config(
    task_id: str,
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    tools_config: Optional[Dict[str, Any]] = None,
) -> pathlib.Path:
    """Write a temp MCP config file and return its path."""
    mcp_script = str(pathlib.Path(__file__).resolve().parent / "mcp_server.py")
    config = create_mcp_config(
        server_script_path=mcp_script,
        drive_root=str(drive_root),
        repo_dir=str(repo_dir),
        tools_config_json=json.dumps(tools_config or {}),
    )
    config_path = pathlib.Path(f"/tmp/ouroboros_mcp_{task_id or 'default'}.json")
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path


def _parse_stream_json(raw_output: str) -> Tuple[str, float, int, str]:
    """Parse Claude CLI stream-json output.

    Returns (final_text, total_cost_usd, num_turns, model).
    """
    final_text = ""
    total_cost = 0.0
    num_turns = 0
    model = ""

    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        if event_type == "assistant":
            num_turns += 1
            # Collect assistant text from message content
            message = event.get("message", {})
            content_blocks = message.get("content", [])
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    final_text = block["text"]

        elif event_type == "result":
            # The result event contains the final response
            result_text = event.get("result", "")
            if result_text:
                final_text = result_text
            total_cost = float(event.get("total_cost_usd", 0) or 0)
            num_turns = max(num_turns, int(event.get("num_turns", 0) or 0))
            # Extract model from modelUsage dict (first key)
            model_usage = event.get("modelUsage", {})
            if model_usage:
                model = next(iter(model_usage))

    return final_text, total_cost, num_turns, model


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_claude_loop(
    messages: List[Dict[str, Any]],
    tools: "ToolRegistry",  # noqa: F821 — avoid circular import
    emit_progress: Callable[[str], None],
    drive_logs: Optional[pathlib.Path] = None,
    incoming_messages: Optional[queue.Queue] = None,
    task_type: str = "",
    task_id: str = "",
    budget_remaining_usd: Optional[float] = None,
    event_queue: Optional[queue.Queue] = None,
    drive_root: Optional[pathlib.Path] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Run an agent loop using Claude CLI with MCP tools.

    Same return signature as ``run_llm_loop``:
        (final_text, usage_dict, llm_trace_dict)
    """
    if not is_claude_cli_available():
        return (
            "Error: Claude CLI not found or not executable at "
            f"{CLAUDE_CLI_PATH}. Install it or use run_llm_loop instead.",
            {"cost": 0, "provider": "claude_cli"},
            {"error": "claude_cli_not_found"},
        )

    # Resolve paths
    repo_dir = pathlib.Path(tools._ctx.repo_dir).resolve()
    if drive_root is None:
        drive_root = pathlib.Path(tools._ctx.drive_root).resolve()
    else:
        drive_root = pathlib.Path(drive_root).resolve()

    # Build tools config for the MCP subprocess
    tools_config = {
        "branch_dev": getattr(tools._ctx, "branch_dev", "ouroboros"),
        "task_type": task_type,
        "task_id": task_id,
    }

    # Write MCP config
    config_path = _write_mcp_config(task_id, drive_root, repo_dir, tools_config)

    # Build prompt from messages
    system_prompt, user_prompt = _messages_to_prompt(messages)

    # Combine system + user into a single prompt for -p mode
    full_prompt = ""
    if system_prompt:
        full_prompt += f"<system>\n{system_prompt}\n</system>\n\n"
    if user_prompt:
        full_prompt += user_prompt
    if not full_prompt.strip():
        full_prompt = "Continue with the current task."

    # Construct claude CLI command
    cmd = [
        CLAUDE_CLI_PATH,
        "-p", full_prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", CLAUDE_CLI_MODEL,
        "--mcp-config", str(config_path),
        "--dangerously-skip-permissions",
    ]

    # Add budget hint as max-turns if budget is tight
    if budget_remaining_usd is not None and budget_remaining_usd < 1.0:
        cmd.extend(["--max-turns", "5"])

    emit_progress(f"Starting Claude CLI loop (task={task_id or 'default'})")
    log.info("Claude CLI cmd: %s", " ".join(cmd[:6]) + " ...")

    # Run Claude CLI
    llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": [], "provider": "claude_cli"}

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_dir),
            env={**os.environ, "OUROBOROS_REPO_DIR": str(repo_dir),
                 "OUROBOROS_DRIVE_ROOT": str(drive_root)},
        )
    except FileNotFoundError:
        log.error("Claude CLI binary not found at %s", CLAUDE_CLI_PATH)
        return (
            f"Claude CLI not found at {CLAUDE_CLI_PATH}.",
            {"cost": 0, "provider": "claude_cli", "rounds": 0},
            {**llm_trace, "error": "not_found"},
        )

    # Read stdout incrementally, emitting progress every 30s
    stdout_lines: List[str] = []
    last_progress_ts = time.time()
    assistant_turns = 0
    try:
        assert proc.stdout is not None
        import select
        import fcntl
        # Set non-blocking mode
        fd = proc.stdout.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        start_time = time.time()
        while True:
            # Check if subprocess finished
            if proc.poll() is not None:
                # Read any remaining output
                try:
                    remaining = proc.stdout.read()
                    if remaining:
                        stdout_lines.extend(remaining.splitlines(keepends=True))
                except Exception:
                    pass
                break

            # Check for timeout
            elapsed = time.time() - start_time
            if elapsed > 600:
                raise subprocess.TimeoutExpired(cmd, 600)

            # Try to read stdout (non-blocking)
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                try:
                    line = proc.stdout.readline()
                    if line:
                        stdout_lines.append(line)
                        if '"type":"assistant"' in line or '"type": "assistant"' in line:
                            assistant_turns += 1
                            log.debug("Claude CLI progress: assistant turn %d", assistant_turns)
                except Exception:
                    pass

            # Emit progress every 30s
            now = time.time()
            if now - last_progress_ts >= 30:
                emit_progress(f"⏱️ Task running for {int(elapsed)}s, last progress {int(now - last_progress_ts)}s ago. Continuing.")
                last_progress_ts = now

    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        log.error("Claude CLI timed out after 600s")
        return (
            "Claude CLI timed out after 10 minutes.",
            {"cost": 0, "provider": "claude_cli", "rounds": 0},
            {**llm_trace, "error": "timeout"},
        )

    stderr_output = proc.stderr.read() if proc.stderr else ""

    # Log stderr (Claude CLI diagnostic output)
    if stderr_output:
        log.info("Claude CLI stderr: %s", stderr_output[:2000])

    # Parse output
    raw_stdout = "".join(stdout_lines)
    final_text, total_cost, num_turns, model_used = _parse_stream_json(raw_stdout)

    if not final_text and proc.returncode != 0:
        final_text = f"Claude CLI exited with code {proc.returncode}."
        if stderr_output:
            final_text += f"\nStderr: {stderr_output[:500]}"
        llm_trace["error"] = f"exit_code_{proc.returncode}"

    # Record trace
    if final_text:
        llm_trace["assistant_notes"].append(final_text[:320])

    usage: Dict[str, Any] = {
        "prompt_tokens": 0,  # not available from CLI
        "completion_tokens": 0,
        "cost": total_cost,
        "provider": "claude_cli",
        "model": model_used or CLAUDE_CLI_MODEL,
        "rounds": num_turns,
    }

    # Emit usage event
    if event_queue is not None:
        try:
            from ouroboros.utils import utc_now_iso
            event_queue.put_nowait({
                "type": "llm_usage",
                "ts": utc_now_iso(),
                "task_id": task_id,
                "model": model_used or CLAUDE_CLI_MODEL,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost": total_cost,
                "cost_estimated": False,
                "category": task_type if task_type in (
                    "evolution", "consciousness", "review", "summarize"
                ) else "task",
            })
        except Exception:
            log.debug("Failed to emit usage event", exc_info=True)

    # Cleanup temp config
    try:
        config_path.unlink(missing_ok=True)
    except Exception:
        pass

    emit_progress(f"Claude CLI loop done — {num_turns} turns, ${total_cost:.4f}")
    return final_text, usage, llm_trace
