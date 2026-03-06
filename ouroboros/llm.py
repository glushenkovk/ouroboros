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


# Pricing from OpenRouter API (2026-02-17). Update periodically via /api/v1/models.
_MODEL_PRICING_STATIC = {
    "anthropic/claude-opus-4.6": (5.0, 0.5, 25.0),
    "anthropic/claude-opus-4": (15.0, 1.5, 75.0),
    "anthropic/claude-sonnet-4": (3.0, 0.30, 15.0),
    "anthropic/claude-sonnet-4.6": (3.0, 0.30, 15.0),
    "anthropic/claude-sonnet-4.5": (3.0, 0.30, 15.0),
    "openai/o3": (2.0, 0.50, 8.0),
    "openai/o3-pro": (20.0, 1.0, 80.0),
    "openai/o4-mini": (1.10, 0.275, 4.40),
    "openai/gpt-4.1": (2.0, 0.50, 8.0),
    "openai/gpt-5.2": (1.75, 0.175, 14.0),
    "openai/gpt-5.2-codex": (1.75, 0.175, 14.0),
    "google/gemini-2.5-pro-preview": (1.25, 0.125, 10.0),
    "google/gemini-3-pro-preview": (2.0, 0.20, 12.0),
    "x-ai/grok-3-mini": (0.30, 0.03, 0.50),
    "qwen/qwen3.5-plus-02-15": (0.40, 0.04, 2.40),
    "google/gemini-2.5-flash-preview-05-20": (0.15, 0.015, 0.60),
    "google/gemini-2.5-flash": (0.15, 0.015, 0.60),
    "google/gemini-flash-1.5": (0.075, 0.0075, 0.30),
}

_pricing_fetched = False
_cached_pricing = None
_pricing_lock = threading.Lock()

def _get_pricing() -> Dict[str, Tuple[float, float, float]]:
    """
    Lazy-load pricing. On first call, attempts to fetch from OpenRouter API.
    Falls back to static pricing if fetch fails.
    Thread-safe via module-level lock.
    """
    global _pricing_fetched, _cached_pricing

    # Fast path: already fetched (read without lock for performance)
    if _pricing_fetched:
        return _cached_pricing or _MODEL_PRICING_STATIC

    # Slow path: fetch pricing (lock required)
    with _pricing_lock:
        # Double-check after acquiring lock (another thread may have fetched)
        if _pricing_fetched:
            return _cached_pricing or _MODEL_PRICING_STATIC

        _pricing_fetched = True
        _cached_pricing = dict(_MODEL_PRICING_STATIC)

        try:
            _live = fetch_openrouter_pricing()
            if _live and len(_live) > 5:
                _cached_pricing.update(_live)
        except Exception as e:
            import logging as _log
            _log.getLogger(__name__).warning("Failed to sync pricing from OpenRouter: %s", e)
            # Reset flag so we retry next time
            _pricing_fetched = False

        return _cached_pricing

def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                   cached_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    """Estimate cost from token counts using known pricing. Returns 0 if model unknown."""
    model_pricing = _get_pricing()
    # Try exact match first
    pricing = model_pricing.get(model)
    if not pricing:
        # Try longest prefix match
        best_match = None
        best_length = 0
        for key, val in model_pricing.items():
            if model and model.startswith(key):
                if len(key) > best_length:
                    best_match = val
                    best_length = len(key)
        pricing = best_match
    if not pricing:
        return 0.0
    input_price, cached_price, output_price = pricing
    # Non-cached input tokens = prompt_tokens - cached_tokens
    regular_input = max(0, prompt_tokens - cached_tokens)
    cost = (
        regular_input * input_price / 1_000_000
        + cached_tokens * cached_price / 1_000_000
        + completion_tokens * output_price / 1_000_000
    )
    return round(cost, 6)


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
    from ouroboros.llm_cli import ClaudeCodeClient
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
