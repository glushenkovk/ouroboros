"""
Ouroboros — CLI-based LLM clients.

Contains clients that shell out to the `claude` CLI binary:
- ClaudeCodeClient: simple -p mode for one-shot queries
- ClaudeCliLoopClient: text-protocol loop client (legacy, pre-agent-sdk)
"""

from __future__ import annotations

import json as _json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.llm import LLMClient

log = logging.getLogger(__name__)

class ClaudeCodeClient:
    """Lightweight LLM client that shells out to the `claude` CLI binary.

    Designed for Max subscription usage — no HTTP API, no token tracking.
    Uses `claude -p` (print/pipe mode) with `--output-format json`.
    """

    def __init__(self, claude_bin: str = None):
        self._bin = claude_bin or shutil.which("claude") or "claude"

    @staticmethod
    def available() -> bool:
        """Return True if the claude binary is on PATH."""
        return shutil.which("claude") is not None

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call via claude CLI. Returns (response_message_dict, usage_dict)."""
        # Build a single prompt string from the messages list
        prompt_parts: List[str] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Multipart content — extract text parts only
                text_bits = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = "\n".join(text_bits)
            if role == "system":
                prompt_parts.append(f"[SYSTEM]\n{content}")
            elif role == "assistant":
                prompt_parts.append(f"[ASSISTANT]\n{content}")
            elif role == "tool":
                prompt_parts.append(f"[TOOL RESULT]\n{content}")
            else:
                prompt_parts.append(content)

        prompt = "\n\n".join(prompt_parts)

        cmd = [self._bin, "-p", "--output-format", "json"]
        if model:
            bare_model = model.split("/", 1)[-1] if "/" in model else model
            # Only pass --model for Claude models; ignore non-Claude model names
            # (e.g. gemini, gpt) since Claude CLI only supports Claude models
            if "claude" in bare_model.lower() or bare_model in ("sonnet", "opus", "haiku"):
                cmd.extend(["--model", bare_model])

        log.info("[CLAUDE CLI] Running: %s (prompt length=%d)", " ".join(cmd[:6]), len(prompt))

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            log.error("[CLAUDE CLI] Command timed out after 300s")
            return {"content": "[claude CLI timed out]", "role": "assistant"}, self._zero_usage()
        except FileNotFoundError:
            log.error("[CLAUDE CLI] Binary not found: %s", self._bin)
            raise RuntimeError(f"claude binary not found at {self._bin}")

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()[:500]
            log.error("[CLAUDE CLI] Exit code %d: %s", result.returncode, stderr)
            return {"content": f"[claude CLI error: {stderr}]", "role": "assistant"}, self._zero_usage()

        # Parse JSON output
        raw = result.stdout.strip()
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError:
            # Not JSON — treat raw stdout as plain text response
            return {"content": raw, "role": "assistant"}, self._zero_usage()

        # Extract text from JSON response
        # Claude CLI JSON output has a "result" field (string) or may vary
        if isinstance(data, dict):
            text = data.get("result") or data.get("content") or data.get("text") or raw
        elif isinstance(data, str):
            text = data
        else:
            text = str(data)

        return {"content": text, "role": "assistant"}, self._zero_usage()

    @staticmethod
    def _zero_usage() -> Dict[str, Any]:
        return {
            "provider": "claude_code",
            "cost": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }


class ClaudeCliLoopClient:
    """
    LLM client that uses Claude CLI (claude -p) as the backend for the main agent loop.

    This allows using the Anthropic Max subscription (free) instead of paying for
    OpenRouter for orchestration. Implements the same chat() interface as LLMClient.

    Architecture:
    - Formats tool schemas as a special system prompt section
    - Claude outputs <tool_call>{"name": "...", "arguments": {...}}</tool_call> blocks
    - We parse these and return them as structured tool_calls compatible with loop.py
    - Multi-turn: tool results are sent back via --resume session_id
    - Falls back to OpenRouter if Claude CLI is unavailable
    """

    TOOL_CALL_PATTERN = re.compile(
        r'<tool_call>\s*(\{.*?\})\s*</tool_call>',
        re.DOTALL
    )

    # System prompt section that instructs Claude how to call tools
    TOOL_CALL_INSTRUCTIONS = """
## Tool Calling Protocol

You have access to tools. To call a tool, output a JSON block in this exact format:

<tool_call>{"name": "tool_name", "id": "call_1", "arguments": {"param": "value"}}</tool_call>

Rules:
- Output ONLY ONE tool call at a time, then STOP and wait for the result
- After a tool call, do NOT continue until you see the result
- Tool results will be provided as: <tool_result id="call_1">result</tool_result>
- After receiving a result, you may call another tool or give your final answer
- Use incrementing IDs: call_1, call_2, call_3, etc.
- Arguments must be valid JSON matching the tool's parameter schema
- If you have no more tool calls to make, give your final text response

Available tools:
"""

    def __init__(self, claude_bin: str = None, fallback: 'LLMClient' = None):
        self._claude_bin = claude_bin or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        self._fallback = fallback
        self._call_counter: Dict[str, int] = {}  # session_id -> call count
        self._session_ids: Dict[str, str] = {}  # conversation_key -> session_id

    @staticmethod
    def available() -> bool:
        """Check if Claude CLI is available."""
        bin_path = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        return os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)

    def _format_system_with_tools(self, messages: List[Dict], tools: Optional[List[Dict]]) -> Tuple[str, List[Dict]]:
        """
        Extract system message and format tool schemas into system prompt.
        Returns (system_prompt, user_messages_only).
        """
        system_parts = []
        user_messages = []

        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
                system_parts.append(content)
            else:
                user_messages.append(msg)

        system_prompt = "\n\n".join(system_parts)

        if tools:
            tool_schemas_text = []
            for tool in tools:
                # Handle both OpenAI format ({type, function: {name, description, parameters}})
                # and direct format ({name, description, parameters/input_schema})
                if "function" in tool:
                    fn = tool["function"]
                    name = fn.get("name", "")
                    desc = fn.get("description", "")
                    params = fn.get("parameters", {})
                else:
                    name = tool.get("name", "")
                    desc = tool.get("description", "")
                    params = tool.get("parameters") or tool.get("input_schema", {})

                tool_schemas_text.append(
                    f"### {name}\n{desc}\nParameters: {_json.dumps(params, indent=2)}"
                )

            system_prompt += self.TOOL_CALL_INSTRUCTIONS + "\n".join(tool_schemas_text)

        return system_prompt, user_messages

    def _messages_to_text(self, messages: List[Dict]) -> str:
        """
        Convert message history to a single text prompt for Claude CLI.
        Handles tool calls and tool results.
        """
        parts = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in content
                )

            if role == "user":
                parts.append(f"Human: {content}")
            elif role == "assistant":
                text = content or ""
                tool_calls = msg.get("tool_calls", [])
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    call_id = tc.get("id", "call_x")
                    text += f'\n<tool_call>{{"name": "{fn.get("name", "")}", "id": "{call_id}", "arguments": {fn.get("arguments", "{}")}}}</tool_call>'
                parts.append(f"Assistant: {text}")
            elif role == "tool":
                tool_call_id = msg.get("tool_call_id", "call_x")
                parts.append(f'<tool_result id="{tool_call_id}">{content}</tool_result>')

        return "\n\n".join(parts)

    def _run_claude(
        self,
        prompt: str,
        system_prompt: str = None,
        session_id: str = None,
        timeout: int = 300,
    ) -> Tuple[str, str, int]:
        """
        Run claude -p and return (text_output, session_id, duration_ms).
        """
        cmd = [
            self._claude_bin, "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--tools", "",  # Disable native tools — we use text protocol
        ]
        if session_id:
            cmd += ["--resume", session_id]
        if system_prompt and not session_id:
            cmd += ["--system-prompt", system_prompt]

        start = time.time()
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return "", session_id or "", int((time.time() - start) * 1000)

        duration_ms = int((time.time() - start) * 1000)

        # Parse stream-json output
        new_session_id = session_id or ""
        output_text = ""

        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = _json.loads(line)
                etype = event.get("type", "")

                if etype == "system" and event.get("session_id"):
                    new_session_id = event["session_id"]
                elif etype == "result":
                    if event.get("session_id"):
                        new_session_id = event["session_id"]
                    if event.get("subtype") == "success":
                        output_text = event.get("result", "")
                elif etype == "assistant" and not output_text:
                    # Extract text from assistant message content
                    msg_content = event.get("message", {}).get("content", [])
                    for block in msg_content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            output_text = block.get("text", "")
            except (_json.JSONDecodeError, KeyError):
                continue

        return output_text, new_session_id, duration_ms

    def _parse_tool_calls(self, text: str) -> List[Dict]:
        """
        Parse <tool_call>...</tool_call> blocks from text.
        Returns list of tool_call dicts in OpenAI format.
        """
        tool_calls = []
        for i, match in enumerate(self.TOOL_CALL_PATTERN.finditer(text)):
            try:
                call_data = _json.loads(match.group(1))
                name = call_data.get("name", "")
                call_id = call_data.get("id") or f"call_{i+1}"
                args = call_data.get("arguments", {})

                # Normalize args to string (OpenAI format)
                if isinstance(args, dict):
                    args_str = _json.dumps(args)
                elif isinstance(args, str):
                    args_str = args
                else:
                    args_str = _json.dumps(args)

                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": args_str,
                    }
                })
            except (_json.JSONDecodeError, KeyError) as e:
                log.warning("Failed to parse tool_call block: %s", e)
                continue
        return tool_calls

    def _strip_tool_calls(self, text: str) -> str:
        """Remove <tool_call> blocks from text, keeping only human-readable content."""
        return self.TOOL_CALL_PATTERN.sub("", text).strip()

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str = None,
        tools: Optional[List[Dict]] = None,
        reasoning_effort: str = "medium",
        **kwargs,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Chat with Claude CLI. Compatible with LLMClient.chat() interface.

        Returns:
            (message_dict, usage_dict) where message_dict may contain tool_calls
        """
        if not self.available():
            if self._fallback:
                return self._fallback.chat(messages=messages, model=model, tools=tools,
                                           reasoning_effort=reasoning_effort, **kwargs)
            raise RuntimeError("Claude CLI not available and no fallback configured")

        system_prompt, user_messages = self._format_system_with_tools(messages, tools)
        prompt_text = self._messages_to_text(user_messages)

        if not prompt_text.strip():
            prompt_text = "[No message]"

        output_text, session_id, duration_ms = self._run_claude(
            prompt=prompt_text,
            system_prompt=system_prompt,
            timeout=300,
        )

        if not output_text:
            # Empty response — try fallback
            if self._fallback:
                log.warning("ClaudeCliLoopClient: empty response, falling back to %s",
                           self._fallback.default_model())
                return self._fallback.chat(messages=messages, model=model, tools=tools,
                                           reasoning_effort=reasoning_effort, **kwargs)
            return {"role": "assistant", "content": ""}, self._zero_usage(duration_ms)

        # Parse tool calls from output
        tool_calls = self._parse_tool_calls(output_text) if tools else []
        content = self._strip_tool_calls(output_text) if tool_calls else output_text

        message = {
            "role": "assistant",
            "content": content,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        # Estimate token usage (Claude CLI doesn't report tokens)
        prompt_tokens = len(prompt_text) // 4  # rough estimate
        completion_tokens = len(output_text) // 4
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "cost": 0.0,  # Max subscription = free
            "duration_ms": duration_ms,
            "model": "claude-cli/max-subscription",
        }

        return message, usage

    @staticmethod
    def _zero_usage(duration_ms: int = 0) -> Dict[str, Any]:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "cost": 0.0,
            "duration_ms": duration_ms,
            "model": "claude-cli/max-subscription",
        }

    @property
    def default_model(self) -> str:
        return "claude-cli/max-subscription"

    def available_models(self) -> List[str]:
        return ["claude-cli/max-subscription"]


