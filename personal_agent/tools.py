import subprocess
import json
import os
from memory import Memory


TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Read a file from disk",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write content to a file (creates dirs if needed)",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "list_dir",
        "description": "List files and directories at a path",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list (default: '.')"}
            },
            "required": []
        }
    },
    {
        "name": "run_shell",
        "description": "Run a shell command, returns stdout+stderr",
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["cmd"]
        }
    },
    {
        "name": "web_search",
        "description": "Search the web for information",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "http_get",
        "description": "Fetch a URL, returns first 5000 chars of response",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "http_post",
        "description": "HTTP POST with JSON body",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "body": {"type": "object"}
            },
            "required": ["url", "body"]
        }
    },
    {
        "name": "read_scratchpad",
        "description": "Read working memory / scratchpad",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "update_scratchpad",
        "description": "Update/overwrite working memory",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string"}
            },
            "required": ["content"]
        }
    },
    {
        "name": "read_identity",
        "description": "Read agent identity/personality",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "update_identity",
        "description": "Update agent identity",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string"}
            },
            "required": ["content"]
        }
    },
]


def get_tool_definitions() -> list:
    return TOOL_DEFINITIONS


def execute_tool(name: str, args: dict, memory: Memory) -> str:
    try:
        if name == "read_file":
            path = args["path"]
            if not os.path.exists(path):
                return f"Error: file not found: {path}"
            with open(path) as f:
                return f.read()

        elif name == "write_file":
            path = args["path"]
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(args["content"])
            return f"Written {len(args['content'])} chars to {path}"

        elif name == "list_dir":
            path = args.get("path", ".")
            if not os.path.exists(path):
                return f"Error: path not found: {path}"
            entries = []
            for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name)):
                prefix = "[DIR] " if entry.is_dir() else "[FILE]"
                size = f" ({entry.stat().st_size} bytes)" if entry.is_file() else ""
                entries.append(f"{prefix} {entry.name}{size}")
            return "\n".join(entries) if entries else "(empty directory)"

        elif name == "run_shell":
            result = subprocess.run(
                args["cmd"], capture_output=True, text=True, timeout=30
            )
            out = result.stdout + result.stderr
            return out[:3000] if out else "(no output)"

        elif name == "web_search":
            import httpx
            query = args["query"]
            url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = httpx.get(url, headers=headers, timeout=15)
            text = resp.text
            import re
            snippets = re.findall(r'<a class="result__snippet"[^>]*>([^<]+)<', text)
            if not snippets:
                return f"No results. Raw: {text[:1000]}"
            return "\n".join(f"- {s}" for s in snippets[:8])

        elif name == "http_get":
            import httpx
            resp = httpx.get(args["url"], timeout=15, follow_redirects=True)
            return resp.text[:5000]

        elif name == "http_post":
            import httpx
            resp = httpx.post(args["url"], json=args["body"], timeout=15)
            return resp.text[:3000]

        elif name == "read_scratchpad":
            return memory.read_scratchpad() or "(empty)"

        elif name == "update_scratchpad":
            memory.write_scratchpad(args["content"])
            return "Scratchpad updated"

        elif name == "read_identity":
            return memory.read_identity() or "(empty)"

        elif name == "update_identity":
            memory.write_identity(args["content"])
            return "Identity updated"

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Tool error ({name}): {e}"
