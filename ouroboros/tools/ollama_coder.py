"""Ollama local coder tool — delegates code generation to local LLM (Qwen/etc).

Architecture:
  Claude (orchestrator) → creates plan → calls ollama_code_task
  Qwen (local LLM)     → reads files, writes code → returns structured output
  Claude (reviewer)    → reviews output, applies changes

Usage:
  1. Claude reads relevant files and creates a detailed implementation plan
  2. Claude calls ollama_code_task(prompt=plan, repo_files=[...])
  3. Qwen returns code in <file path="...">content</file> format
  4. Claude reviews and applies via repo_write_commit
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://192.168.1.130:11434")
DEFAULT_CODER_MODEL = os.environ.get("OLLAMA_CODER_MODEL", "qwen2.5-coder:32b")

SYSTEM_PROMPT = """You are an expert software engineer. You will be given a coding task with context.

Output file changes using this exact format (one block per file):
<file path="relative/path/to/file.py">
file content here
</file>

Rules:
- Output ONLY the files that need to change
- Use relative paths from the repo root
- Output the COMPLETE file content (not diffs)
- After file blocks, add a brief <summary> of what you changed and why
- If no code changes needed, just write a <summary> explaining why

Think step by step before writing code."""


def _get_ollama_client(model: str):
    """Get OpenAI-compatible client pointing at Ollama."""
    from openai import OpenAI
    host = OLLAMA_HOST
    if not host.startswith("http"):
        host = f"http://{host}"
    return OpenAI(base_url=f"{host}/v1", api_key="ollama"), model


def _parse_file_blocks(text: str) -> List[Dict[str, str]]:
    """Parse <file path="...">content</file> blocks from LLM output."""
    pattern = r'<file\s+path=["\']([^"\']+)["\']>(.*?)</file>'
    matches = re.findall(pattern, text, re.DOTALL)
    return [{"path": path.strip(), "content": content} for path, content in matches]


def _parse_summary(text: str) -> str:
    """Extract <summary> block or last paragraph."""
    m = re.search(r'<summary>(.*?)</summary>', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # fallback: last non-empty paragraph after last </file>
    after = re.split(r'</file>', text)[-1].strip()
    return after[:500] if after else "(no summary)"


def _read_repo_file(repo_dir: str, path: str) -> Optional[str]:
    """Read a file from the repo, return None if not found."""
    full = os.path.join(repo_dir, path.lstrip("/"))
    try:
        with open(full, "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return None


def _ollama_code_task(
    ctx: ToolContext,
    prompt: str,
    repo_files: Optional[List[str]] = None,
    model: Optional[str] = None,
    max_tokens: int = 8192,
) -> str:
    """Send a coding task to a local Ollama model (Qwen/etc).

    Claude should:
    1. Read relevant files first (use repo_read tool)
    2. Create a detailed implementation plan
    3. Pass plan + file contents in `prompt`
    4. Review returned <file ...> blocks and apply with repo_write_commit

    Args:
        prompt: Detailed task description. Include current file contents if relevant.
        repo_files: Optional list of repo-relative file paths to include as context.
                    These are read and appended to the prompt automatically.
        model: Ollama model name. Defaults to OLLAMA_CODER_MODEL env var or qwen2.5-coder:32b.
        max_tokens: Max tokens in response (default 8192).

    Returns:
        Structured result with file changes and summary.
    """
    chosen_model = model or DEFAULT_CODER_MODEL
    repo_dir = getattr(ctx, "repo_dir", "/home/max2/ouroboros_repo")

    # Build context: read requested files
    file_context = ""
    if repo_files:
        parts = []
        for path in repo_files:
            content = _read_repo_file(repo_dir, path)
            if content is not None:
                parts.append(f"### Current content of `{path}`:\n```\n{content}\n```")
            else:
                parts.append(f"### File `{path}`: NOT FOUND (create new)")
        if parts:
            file_context = "\n\n" + "\n\n".join(parts)

    full_prompt = prompt + file_context

    # Call Ollama
    client, model_name = _get_ollama_client(chosen_model)
    log.info("[ollama_coder] Calling %s @ %s, prompt_len=%d", model_name, OLLAMA_HOST, len(full_prompt))

    try:
        resp = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": full_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.2,  # Low temp for coding tasks
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        log.error("[ollama_coder] API error: %s", e)
        return (
            f"ERROR: Ollama call failed — {e}\n\n"
            f"Is model '{model_name}' downloaded? Run: ollama pull {model_name}\n"
            f"Fallback: use model='qwen2.5:32b' (already available)"
        )

    # Parse output
    file_blocks = _parse_file_blocks(raw)
    summary = _parse_summary(raw)

    # Format result
    lines = []
    lines.append(f"## Ollama Coder Result ({model_name})")
    lines.append(f"**Summary:** {summary}")
    lines.append(f"**Files to update:** {len(file_blocks)}")
    lines.append("")

    if file_blocks:
        lines.append("### Changes ready to apply:")
        for fb in file_blocks:
            lines.append(f"- `{fb['path']}` ({len(fb['content'])} chars)")
        lines.append("")
        lines.append("**To apply:** Use `repo_write_commit` for each file above.")
        lines.append("")
        lines.append("---")
        lines.append("### Raw file blocks:")
        for fb in file_blocks:
            lines.append(f'\n<file path="{fb["path"]}">')
            lines.append(fb["content"])
            lines.append("</file>")
    else:
        lines.append("### No file blocks found in output. Raw response:")
        lines.append(raw[:3000])

    return "\n".join(lines)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="ollama_code_task",
            schema={
                "name": "ollama_code_task",
                "description": (
                    "Delegate a coding task to a local Ollama LLM (e.g. qwen2.5-coder:32b). "
                    "Claude creates the plan/spec, Qwen writes the code. "
                    "Returns file changes in structured format ready to apply with repo_write_commit. "
                    "Use for: night evolution cycles, free coding tasks, experimenting with local models."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": (
                                "Detailed coding task. Include: what to implement, why, constraints. "
                                "The more specific, the better Qwen's output."
                            ),
                        },
                        "repo_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Repo-relative paths of files to include as context "
                                "(e.g. ['ouroboros/llm.py']). "
                                "These are read and appended to the prompt."
                            ),
                        },
                        "model": {
                            "type": "string",
                            "description": (
                                "Ollama model to use. Default: qwen2.5-coder:32b "
                                "(or OLLAMA_CODER_MODEL env). "
                                "Fallback: qwen2.5:32b (already downloaded)."
                            ),
                        },
                        "max_tokens": {
                            "type": "integer",
                            "description": "Max tokens in response (default 8192).",
                        },
                    },
                    "required": ["prompt"],
                },
            },
            handler=_ollama_code_task,
        )
    ]
