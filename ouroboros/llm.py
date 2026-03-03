"""
Ouroboros — LLM client.

The only module that communicates with the LLM API (OpenRouter).
Contract: chat(), default_model(), available_models(), add_usage().
"""

from __future__ import annotations

import json as _json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_LIGHT_MODEL = "google/gemini-3-pro-preview"


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


def fetch_openrouter_pricing() -> Dict[str, Tuple[float, float, float]]:
    """
    Fetch current pricing from OpenRouter API.

    Returns dict of {model_id: (input_per_1m, cached_per_1m, output_per_1m)}.
    Returns empty dict on failure.
    """
    import logging
    log = logging.getLogger("ouroboros.llm")

    try:
        import requests
    except ImportError:
        log.warning("requests not installed, cannot fetch pricing")
        return {}

    try:
        url = "https://openrouter.ai/api/v1/models"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()

        data = resp.json()
        models = data.get("data", [])

        # Prefixes we care about
        prefixes = ("anthropic/", "openai/", "google/", "meta-llama/", "x-ai/", "qwen/")

        pricing_dict = {}
        for model in models:
            model_id = model.get("id", "")
            if not model_id.startswith(prefixes):
                continue

            pricing = model.get("pricing", {})
            if not pricing or not pricing.get("prompt"):
                continue

            # OpenRouter pricing is in dollars per token (raw values)
            raw_prompt = float(pricing.get("prompt", 0))
            raw_completion = float(pricing.get("completion", 0))
            raw_cached_str = pricing.get("input_cache_read")
            raw_cached = float(raw_cached_str) if raw_cached_str else None

            # Convert to per-million tokens
            prompt_price = round(raw_prompt * 1_000_000, 4)
            completion_price = round(raw_completion * 1_000_000, 4)
            if raw_cached is not None:
                cached_price = round(raw_cached * 1_000_000, 4)
            else:
                cached_price = round(prompt_price * 0.1, 4)  # fallback: 10% of prompt

            # Sanity check: skip obviously wrong prices
            if prompt_price > 1000 or completion_price > 1000:
                log.warning(f"Skipping {model_id}: prices seem wrong (prompt={prompt_price}, completion={completion_price})")
                continue

            pricing_dict[model_id] = (prompt_price, cached_price, completion_price)

        log.info(f"Fetched pricing for {len(pricing_dict)} models from OpenRouter")
        return pricing_dict

    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning(f"Failed to fetch OpenRouter pricing: {e}")
        return {}


class LLMClient:
    """OpenRouter API wrapper. All LLM calls go through this class."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1",
    ):
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._base_url = base_url
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
                default_headers={
                    "HTTP-Referer": "https://colab.research.google.com/",
                    "X-Title": "Ouroboros",
                },
            )
        return self._client

    def _fetch_generation_cost(self, generation_id: str) -> Optional[float]:
        """Fetch cost from OpenRouter Generation API as fallback."""
        try:
            import requests
            url = f"{self._base_url.rstrip('/')}/generation?id={generation_id}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
            # Generation might not be ready yet — retry once after short delay
            time.sleep(0.5)
            resp = requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
        except Exception:
            log.debug("Failed to fetch generation cost from OpenRouter", exc_info=True)
            pass
        return None

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call. Returns: (response_message_dict, usage_dict with cost)."""
        client = self._get_client()
        effort = normalize_reasoning_effort(reasoning_effort)

        extra_body: Dict[str, Any] = {
            "reasoning": {"effort": effort, "exclude": True},
        }

        # Pin Anthropic models to Anthropic provider for prompt caching
        if model.startswith("anthropic/"):
            extra_body["provider"] = {
                "order": ["Anthropic"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "extra_body": extra_body,
        }
        if tools:
            # Add cache_control to last tool for Anthropic prompt caching
            # This caches all tool schemas (they never change between calls)
            tools_with_cache = [t for t in tools]  # shallow copy
            if tools_with_cache:
                last_tool = {**tools_with_cache[-1]}  # copy last tool
                last_tool["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                tools_with_cache[-1] = last_tool
            kwargs["tools"] = tools_with_cache
            kwargs["tool_choice"] = tool_choice

        resp = client.chat.completions.create(**kwargs)
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        # Extract cached_tokens from prompt_tokens_details if available
        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        # Extract cache_write_tokens from prompt_tokens_details if available
        # OpenRouter: "cache_write_tokens"
        # Native Anthropic: "cache_creation_tokens" or "cache_creation_input_tokens"
        if not usage.get("cache_write_tokens"):
            prompt_details_for_write = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details_for_write, dict):
                cache_write = (prompt_details_for_write.get("cache_write_tokens")
                              or prompt_details_for_write.get("cache_creation_tokens")
                              or prompt_details_for_write.get("cache_creation_input_tokens"))
                if cache_write:
                    usage["cache_write_tokens"] = int(cache_write)

        # Ensure cost is present in usage (OpenRouter includes it, but fallback if missing)
        if not usage.get("cost"):
            gen_id = resp_dict.get("id") or ""
            if gen_id:
                cost = self._fetch_generation_cost(gen_id)
                if cost is not None:
                    usage["cost"] = cost

        # Tag provider for per-provider budget tracking
        if "api.openai.com" in self._base_url:
            usage["provider"] = "openai"
        else:
            usage["provider"] = "openrouter"

        return msg, usage

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "anthropic/claude-sonnet-4.6",
        max_tokens: int = 1024,
        reasoning_effort: str = "low",
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Send a vision query to an LLM. Lightweight — no tools, no loop.

        Args:
            prompt: Text instruction for the model
            images: List of image dicts. Each dict must have either:
                - {"url": "https://..."} — for URL images
                - {"base64": "<b64>", "mime": "image/png"} — for base64 images
            model: VLM-capable model ID
            max_tokens: Max response tokens
            reasoning_effort: Effort level

        Returns:
            (text_response, usage_dict)
        """
        # Build multipart content
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img["url"]},
                })
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                })
            else:
                log.warning("vision_query: skipping image with unknown format: %s", list(img.keys()))

        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        """Return the single default model from env. LLM switches via tool if needed."""
        return os.environ.get("OUROBOROS_MODEL", "anthropic/claude-sonnet-4.6")

    def available_models(self) -> List[str]:
        """Return list of available models from env (for switch_model tool schema)."""
        main = os.environ.get("OUROBOROS_MODEL", "anthropic/claude-sonnet-4.6")
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models = [main]
        if code and code != main:
            models.append(code)
        if light and light != main and light != code:
            models.append(light)
        return models


# ---------------------------------------------------------------------------
# Ollama fallback client
# ---------------------------------------------------------------------------

class OllamaClient:
    """Wraps Ollama's OpenAI-compatible API for local LLM inference."""

    DEFAULT_HOST = "192.168.1.130:11434"

    def __init__(self, host: str = None):
        self._host = host or os.environ.get("OLLAMA_HOST", self.DEFAULT_HOST)
        # Ensure host has scheme
        if not self._host.startswith("http"):
            self._host = f"http://{self._host}"
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=f"{self._host}/v1",
                api_key="ollama",  # Ollama doesn't need a real key
            )
        return self._client

    @staticmethod
    def _map_model(model: str) -> str:
        """Map cloud model names to local Ollama models."""
        if model.startswith("anthropic/"):
            return "qwen2.5:32b"
        if model.startswith("google/") or model.startswith("openai/"):
            return "qwen2.5:14b"
        return model

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call via Ollama. Returns (response_message_dict, usage_dict)."""
        client = self._get_client()
        mapped_model = self._map_model(model)
        log.info("[LOCAL LLM] Using Ollama at %s with model %s", self._host, mapped_model)

        kwargs: Dict[str, Any] = {
            "model": mapped_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            # Strip cache_control from tools — Ollama doesn't support it
            clean_tools = []
            for t in tools:
                t_copy = {k: v for k, v in t.items() if k != "cache_control"}
                clean_tools.append(t_copy)
            kwargs["tools"] = clean_tools
            try:
                kwargs["tool_choice"] = tool_choice
            except Exception:
                pass  # Ollama may not support tool_choice

        resp = client.chat.completions.create(**kwargs)
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        # Cost is always 0 for local inference
        usage["cost"] = 0.0

        return msg, usage


class OpenAIClient:
    """Native OpenAI API client (direct, not via OpenRouter)."""

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = "https://api.openai.com/v1"
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
            )
        return self._client

    @staticmethod
    def _map_model(model: str) -> str:
        """Strip 'openai/' prefix for native API; pass non-OpenAI models as-is."""
        if model.startswith("openai/"):
            return model[len("openai/"):]
        return model

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call via native OpenAI API."""
        client = self._get_client()
        mapped_model = self._map_model(model)
        log.info("[OPENAI] Using native OpenAI API with model %s", mapped_model)

        kwargs: Dict[str, Any] = {
            "model": mapped_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            # Strip cache_control from tools — OpenAI native doesn't use it
            clean_tools = []
            for t in tools:
                t_copy = {k: v for k, v in t.items() if k != "cache_control"}
                clean_tools.append(t_copy)
            kwargs["tools"] = clean_tools
            kwargs["tool_choice"] = tool_choice

        resp = client.chat.completions.create(**kwargs)
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        # Extract cached_tokens from prompt_tokens_details if available
        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        # Tag provider for per-provider budget tracking (spent_usd_openai)
        usage["provider"] = "openai"

        return msg, usage


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


class FallbackLLMClient(LLMClient):
    """Wraps LLMClient with automatic fallback chain.

    Fallback order: primary (OpenRouter) → paid fallbacks (OpenAI) → free fallback (Ollama).
    On budget exhaustion, skips directly to the free fallback.
    """

    def __init__(
        self,
        primary: LLMClient,
        fallbacks: List[Any],
        budget_remaining_fn: callable = None,
    ):
        # We don't call super().__init__() — we delegate to the wrapped primary
        self._primary = primary
        self._fallbacks = fallbacks  # Ordered list: [OpenAIClient, OllamaClient, ...]
        self._budget_remaining_fn = budget_remaining_fn
        # Identify free fallback (Ollama) for budget-exhaustion shortcut
        self._free_fallback = None
        for fb in fallbacks:
            if isinstance(fb, OllamaClient):
                self._free_fallback = fb
                break

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        call_kwargs = dict(
            messages=messages, model=model, tools=tools,
            reasoning_effort=reasoning_effort, max_tokens=max_tokens,
            tool_choice=tool_choice,
        )

        # Budget exhausted → skip to free fallback (Ollama)
        if self._budget_remaining_fn is not None:
            remaining = self._budget_remaining_fn()
            if remaining <= 0 and self._free_fallback is not None:
                log.warning("[FALLBACK] Budget exhausted (remaining=%s), using Ollama", remaining)
                return self._free_fallback.chat(**call_kwargs)

        # Try primary (OpenRouter)
        try:
            return self._primary.chat(**call_kwargs)
        except Exception as exc:
            exc_str = str(exc).lower()
            if "402" in exc_str or "insufficient_funds" in exc_str or "credits" in exc_str:
                reason = "insufficient credits (HTTP 402)"
            else:
                reason = f"{type(exc).__name__}: {exc}"
            log.warning("[FALLBACK] Primary (OpenRouter) failed: %s", reason)

        # Try fallbacks in order (OpenAI → Ollama)
        for fb in self._fallbacks:
            try:
                return fb.chat(**call_kwargs)
            except Exception as fb_exc:
                log.warning("[FALLBACK] %s failed: %s", type(fb).__name__, fb_exc)
                continue

        raise RuntimeError("All LLM providers failed (OpenRouter + fallbacks)")

    # Delegate non-chat methods to the primary LLMClient
    def vision_query(self, *args, **kwargs):
        return self._primary.vision_query(*args, **kwargs)

    def default_model(self) -> str:
        return self._primary.default_model()

    def available_models(self) -> List[str]:
        return self._primary.available_models()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _probe_claude_cli() -> bool:
    """Check if the claude CLI binary is available on PATH."""
    return ClaudeCodeClient.available()


def _probe_ollama() -> bool:
    """Check if Ollama is reachable. Returns True if available."""
    ollama_host = os.environ.get("OLLAMA_HOST", OllamaClient.DEFAULT_HOST)
    if not ollama_host.startswith("http"):
        ollama_url = f"http://{ollama_host}"
    else:
        ollama_url = ollama_host

    if os.environ.get("OLLAMA_HOST"):
        log.info("Ollama host configured via OLLAMA_HOST=%s", ollama_host)
        return True

    try:
        import requests
        resp = requests.head(f"{ollama_url}/", timeout=1)
        available = resp.status_code < 500
        log.info("Ollama probe at %s: status %s", ollama_url, resp.status_code)
        return available
    except Exception:
        log.debug("Ollama not reachable at %s", ollama_url)
        return False


def create_llm_client(budget_remaining_fn: callable = None) -> LLMClient:
    """Create an LLMClient with fallback chain: OpenRouter → OpenAI → Ollama."""
    primary = LLMClient()  # OpenRouter

    fallbacks = []

    # OpenAI native (if API key is set)
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        fallbacks.append(OpenAIClient(api_key=openai_key))
        log.info("OpenAI native client added to fallback chain")

    # Ollama (local, free)
    if _probe_ollama():
        ollama_host = os.environ.get("OLLAMA_HOST", OllamaClient.DEFAULT_HOST)
        fallbacks.append(OllamaClient(host=ollama_host))
        log.info("Ollama added to fallback chain (%s)", ollama_host)

    if fallbacks:
        names = [type(fb).__name__ for fb in fallbacks]
        log.info("LLM client created with fallback chain: OpenRouter → %s", " → ".join(names))
        return FallbackLLMClient(primary, fallbacks, budget_remaining_fn)

    log.info("LLM client created without fallbacks")
    return primary
