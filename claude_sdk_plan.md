# Replace Broken Claude CLI Subprocess with Claude Agent SDK

## Context
Ouroboros v6.4.0 already has a Claude CLI integration — but it's **broken**. The agent built it using raw `subprocess.Popen` calls to `claude -p --output-format stream-json`, which:
- Hangs indefinitely (no proper lifecycle management)
- Exits with code 1 (empty error)
- Causes restart cycles (task stuck → restart → auto-resume → stuck again)
- Uses a manual JSON-RPC 2.0 MCP server (`mcp_server.py`) spawned as a separate process

We **replace** the broken subprocess approach with `claude-agent-sdk` v0.1.44 (already installed in venv), which properly manages the `claude` subprocess lifecycle and provides in-process MCP tools.

**Result**: Same free LLM calls via Max subscription, but stable and reliable.

## Current State (what the agent broke)

```
BROKEN:  run_claude_loop() → subprocess.Popen("claude -p") → hangs/exit code 1
                                    ↕ stdio JSON-RPC (separate process)
                              mcp_server.py → ToolRegistry

FIXED:   run_claude_loop() → claude-agent-sdk query() → managed subprocess → stable
                                    ↕ in-process MCP
                              mcp_bridge.py → ToolRegistry
```

### Existing broken files (from agent's v6.4.0):
- `ouroboros/claude_loop.py` (313 lines) — raw subprocess, REWRITE entirely
- `ouroboros/mcp_server.py` (220 lines) — manual JSON-RPC, REWRITE entirely
- `ouroboros/agent.py` lines 35, 427-460 — integration code, UPDATE (minor)

### Git state:
```
Branch: ouroboros (HEAD = c4af3cd)
Stable: ouroboros-stable (behind by ~6 commits)
Key commits: 711f7c9 (v6.4.0 Claude CLI), 7046fff (fix timeout), c4af3cd (fix env)
```

---

## Package: `claude-agent-sdk` v0.1.44

Already installed at `/home/max2/ouroboros_venv/`. Key API:
- `ClaudeAgentOptions` — `tools=[]`, `effort`, `max_budget_usd`, `mcp_servers`, `allowed_tools`
- `create_sdk_mcp_server(name, tools=[...])` → `McpSdkServerConfig`
- `@tool(name, desc, schema)` decorator → `SdkMcpTool`
- `query(prompt, options)` → async iterator of `Message`

---

## Step 1: REWRITE `ouroboros/mcp_server.py`

Replace 220-line manual JSON-RPC implementation with SDK bridge (~50 lines).

```python
"""In-process MCP bridge: wraps ToolRegistry for claude-agent-sdk."""
from claude_agent_sdk import tool, create_sdk_mcp_server
import asyncio, concurrent.futures

_general_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
_browser_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_BROWSER_TOOLS = frozenset({"browse_page", "browser_action"})

_EXCLUDED_TOOLS = frozenset({
    "switch_model",        # SDK model is fixed per session
    "compact_context",     # SDK manages its own context
    "list_available_tools",# All tools exposed via MCP
    "enable_tools",        # All tools exposed via MCP
})

def create_mcp_bridge(registry):
    """Create McpSdkServerConfig wrapping all ToolRegistry tools."""
    sdk_tools = []
    for entry in registry._entries.values():
        if entry.name in _EXCLUDED_TOOLS:
            continue
        name = entry.name
        desc = entry.schema.get("description", name)
        schema = entry.schema.get("parameters", {"type": "object", "properties": {}})

        @tool(name, desc, schema)
        async def handler(args, _name=name):
            executor = _browser_executor if _name in _BROWSER_TOOLS else _general_executor
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(executor, registry.execute, _name, dict(args))
            return {"content": [{"type": "text", "text": str(result)}]}

        sdk_tools.append(handler)
    return create_sdk_mcp_server("ouroboros-tools", version="1.0.0", tools=sdk_tools)
```

**Why this fixes the broken version:**
- In-process MCP (no separate process spawn, no stdio JSON-RPC)
- `run_in_executor()` prevents sync tools from blocking async event loop
- Dedicated single-thread executor for browser tools (Playwright thread-affinity)
- Incompatible tools excluded upfront

## Step 2: REWRITE `ouroboros/claude_loop.py`

Replace 313-line subprocess approach with SDK-based loop (~120 lines).

**Function signature** — keep compatible with existing `agent.py` call site:
```python
def run_claude_loop(
    messages, tools, emit_progress,
    task_type="", task_id="", budget_remaining_usd=None,
    event_queue=None, drive_root=None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
```

**Implementation outline:**

1. **Extract prompts** from OpenAI-format messages:
   - System: flatten 3-block content array to single string
   - User: text from last user message (images not supported in SDK string mode)

2. **Create MCP bridge** from ToolRegistry via `create_mcp_bridge(tools)`

3. **Configure `ClaudeAgentOptions`**:
   - `system_prompt` = flattened system prompt
   - `model` = `os.environ.get("OUROBOROS_CLAUDE_MODEL", "sonnet")`
   - `tools` = `[]` ← disables ALL Claude Code built-in tools
   - `mcp_servers` = `[mcp_bridge]`
   - `allowed_tools` = `["mcp__ouroboros-tools__*"]`
   - `permission_mode` = `"bypassPermissions"`
   - `max_turns` = `int(os.environ.get("OUROBOROS_MAX_ROUNDS", "200"))`
   - `effort` = "medium" (default) or map from task_type
   - `cwd` = `str(tools._ctx.repo_dir)`
   - `cli_path` = `"/home/max2/.local/bin/claude"`
   - `env` = `{"PATH": "/home/max2/.local/bin:/home/max2/ouroboros_venv/bin:/usr/local/bin:/usr/bin:/bin"}`

4. **Run async** via `asyncio.new_event_loop()` + `loop.run_until_complete()`:
   ```python
   loop = asyncio.new_event_loop()
   try:
       return loop.run_until_complete(_run_async(...))
   finally:
       loop.close()
   ```
   (NOT `asyncio.run()` — safer in forked workers)

5. **Stream and collect** from `query()` async iterator:
   - `AssistantMessage` + `TextBlock` → `emit_progress(text[:200])`
   - `AssistantMessage` + `ToolUseBlock` → log tool call
   - `ResultMessage` → extract final text, cost, usage

6. **Map usage keys** for compatibility:
   ```python
   usage = {
       "prompt_tokens": sdk_usage.get("input_tokens", 0),
       "completion_tokens": sdk_usage.get("output_tokens", 0),
       "cost": result_msg.total_cost_usd or 0.0,
       "rounds": result_msg.num_turns or 0,
       "provider": "claude_cli",
       "model": model_name,
   }
   ```

7. **Return** `(text, usage_dict, llm_trace)` — same format as `run_llm_loop()`

**Why this fixes the broken version:**
- SDK manages subprocess lifecycle (no hangs, proper cleanup)
- Streaming via async iterator (not blocking stdout read)
- Proper error handling and timeouts built into SDK
- No temp MCP config files needed

## Step 3: UPDATE `ouroboros/agent.py` (minor)

The agent's existing integration at lines 427-460 is mostly fine. Changes needed:

1. **Remove old imports** (line 35):
   ```python
   # OLD: from ouroboros.claude_loop import run_claude_loop, is_claude_cli_available
   # NEW: (lazy import inside handle_task, same as current pattern)
   ```

2. **Simplify the selection** — remove `is_claude_cli_available()` check, use env var:
   ```python
   backend = os.environ.get("OUROBOROS_BACKEND", "openrouter")
   if backend == "claude_code":
       from ouroboros.claude_loop import run_claude_loop
       text, usage, llm_trace = run_claude_loop(
           messages=messages, tools=self.tools,
           emit_progress=self._emit_progress,
           task_type=task_type_str, task_id=str(task.get("id") or ""),
           budget_remaining_usd=budget_remaining,
           event_queue=self._event_queue, drive_root=self.env.drive_root,
       )
   else:
       text, usage, llm_trace = run_llm_loop(...)
   ```

3. **Keep fallback** — if `run_claude_loop` raises, fall back to `run_llm_loop`

## Step 4: VM Configuration

### `/home/max2/ouroboros.env` — already has or add:
```env
OUROBOROS_BACKEND=claude_code
OUROBOROS_CLAUDE_MODEL=sonnet
```

### No systemd changes needed
PATH already includes `/home/max2/.local/bin`.

---

## Review Issues — All Addressed

| # | Issue | Resolution |
|---|-------|------------|
| 1 | Wrong package | `claude-agent-sdk` v0.1.44 (already installed) |
| 2 | asyncio.run() in forked worker | `asyncio.new_event_loop()` + `run_until_complete()` |
| 3 | System prompt flattening | Flatten to string (SDK handles caching) |
| 4 | Built-in tools not disabled | `tools=[]` disables all Claude Code built-ins |
| 5 | Recursive CLI (claude_code_edit) | Acceptable — outer+inner `claude`, both use OAuth |
| 6 | switch_model fails | Excluded from MCP tools |
| 7 | Owner messages dropped | Accepted for MVP |
| 8 | Budget tracking | Cost=$0 for Max sub, budget guards irrelevant |
| 9 | Sync tools block event loop | `run_in_executor()` with thread pool |
| 10 | Playwright thread-affinity | `_browser_executor` (max_workers=1) |
| 11 | compact_context broken | Excluded from MCP, SDK manages context |
| 12 | Dynamic tool discovery | All tools registered upfront, discovery excluded |
| 13 | Usage key mismatch | Translation layer in usage dict |

---

## What stays unchanged

- All 47 tool handlers (called via MCP bridge)
- ToolContext, ToolRegistry internals
- System prompt construction (context.py)
- Supervisor, workers, events, Telegram
- vm_launcher.py, systemd service
- claude_code_edit tool (still spawns separate `claude` for file editing)
- multi_model_review tool (still uses OpenRouter)
- Other v6.4.0 changes (consciousness.py, llm.py, etc.)

## Files Summary

| File | Action | Lines |
|------|--------|-------|
| `ouroboros/mcp_server.py` | REWRITE | 220 → ~50 |
| `ouroboros/claude_loop.py` | REWRITE | 313 → ~120 |
| `ouroboros/agent.py` | UPDATE | ~10 lines changed |
| `ouroboros.env` | VERIFY | Env var already set or add |

## Verification

1. `sudo systemctl stop ouroboros`
2. Verify `OUROBOROS_BACKEND=claude_code` in `/home/max2/ouroboros.env`
3. `sudo systemctl start ouroboros`
4. `journalctl -u ouroboros -f` — look for SDK initialization (no `[CLAUDE CLI] Exit code 1`)
5. Send Telegram message → verify response comes back
6. Check logs for MCP tool calls completing normally
7. Confirm no zombie `claude` processes after task completes
8. Rollback: set `OUROBOROS_BACKEND=openrouter`, restart
