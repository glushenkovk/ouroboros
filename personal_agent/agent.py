import asyncio
import json
import subprocess
import sys
from typing import Optional

import httpx

from memory import Memory
from tools import get_tool_definitions, execute_tool


class Agent:
    def __init__(self, memory: Memory, system_prompt: str,
                 openrouter_key: str = "", fallback_model: str = ""):
        self.memory = memory
        self.system_prompt = system_prompt
        self.openrouter_key = openrouter_key
        self.fallback_model = fallback_model
        self.tools = get_tool_definitions()
        self.max_iterations = 10

    def _build_prompt(self, messages: list) -> str:
        """Build a single prompt string for Claude CLI."""
        tools_json = json.dumps(self.tools, indent=2)

        prompt_parts = [
            self.system_prompt,
            "",
            "## Available Tools",
            "You have access to these tools. To call a tool, respond with ONLY a JSON object:",
            '{"tool_call": {"name": "tool_name", "args": {...}}}',
            "After getting the tool result, continue thinking. When done, give a plain text response.",
            "",
            "Tools:\n" + tools_json,
            "",
            "## Conversation",
        ]

        for msg in messages:
            role = msg["role"].upper()
            content = msg["content"]
            prompt_parts.append(f"{role}: {content}")

        prompt_parts.append("ASSISTANT:")
        return "\n".join(prompt_parts)

    def _call_claude_cli(self, prompt: str) -> Optional[str]:
        """Call Claude CLI and return response text."""
        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--model", "claude-opus-4-5"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            print(f"Claude CLI error: {result.stderr[:500]}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"Claude CLI exception: {e}", file=sys.stderr)
            return None

    async def _call_openrouter(self, messages: list) -> Optional[str]:
        """Fallback to OpenRouter API."""
        if not self.openrouter_key or not self.fallback_model:
            return None
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.openrouter_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.fallback_model,
                        "messages": [{"role": "system", "content": self.system_prompt}] + messages,
                    }
                )
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"OpenRouter error: {e}", file=sys.stderr)
            return None

    def _is_tool_call(self, text: str) -> Optional[dict]:
        """Check if response is a tool call JSON."""
        text = text.strip()
        if '{"tool_call":' in text:
            start = text.index('{"tool_call":')
            depth = 0
            end = start
            for i, c in enumerate(text[start:], start):
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            try:
                obj = json.loads(text[start:end])
                return obj.get("tool_call")
            except Exception:
                pass
        return None

    async def respond(self, user_message: str) -> str:
        """Process a user message and return response."""
        history = self.memory.get_chat_history(last_n=20)
        messages = history + [{"role": "user", "content": user_message}]

        for iteration in range(self.max_iterations):
            prompt = self._build_prompt(messages)

            # Try Claude CLI first (in thread to not block event loop)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, self._call_claude_cli, prompt)

            # Fallback to OpenRouter
            if response is None:
                response = await self._call_openrouter(messages)

            if response is None:
                return "Sorry, I could not get a response from the LLM. Check logs."

            # Check if it's a tool call
            tool_call = self._is_tool_call(response)
            if tool_call and isinstance(tool_call, dict):
                name = tool_call.get("name", "")
                args = tool_call.get("args", {})
                tool_result = execute_tool(name, args, self.memory)
                messages.append({"role": "assistant", "content": response})
                messages.append({
                    "role": "user",
                    "content": f"Tool result ({name}):\n{tool_result}"
                })
                continue

            # Plain text response — done
            return response

        return "(Max iterations reached)"
    async def handle_message(self, user_message: str) -> str:
        """Handle a user message: process and persist to memory."""
        self.memory.add_chat_message("user", user_message)
        response = await self.respond(user_message)
        self.memory.add_chat_message("assistant", response)
        return response
