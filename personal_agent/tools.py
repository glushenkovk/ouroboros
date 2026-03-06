import subprocess
import json
import os
import asyncio
import threading
import uuid
from datetime import datetime, timezone
from memory import Memory

try:
    import config as _config
    _DATA_DIR = _config.DATA_DIR
except Exception:
    _DATA_DIR = "/home/max2/agent_data"

_tasks_lock = threading.Lock()
_outgoing_queue = None


def set_message_queue(q):
    global _outgoing_queue
    _outgoing_queue = q


def _tasks_path():
    return os.path.join(_DATA_DIR, "tasks.json")


def _load_tasks() -> dict:
    path = _tasks_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_tasks(tasks: dict):
    path = _tasks_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(tasks, f, indent=2)


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
    {
        "name": "task_board",
        "description": (
            "Persistent task tracker (INBOX -> PLANNED -> IN_PROGRESS -> DONE). "
            "Actions: create (title, description, priority), "
            "list (optional status filter: inbox/planned/in_progress/done/all, default=active), "
            "update (task_id, status), note (task_id, note), delete (task_id)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "update", "note", "delete"],
                    "description": "Operation to perform"
                },
                "title": {"type": "string", "description": "Task title (for create)"},
                "description": {"type": "string", "description": "Task description (for create)"},
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Priority level (for create, default medium)"
                },
                "task_id": {"type": "string", "description": "Task ID (for update/note/delete)"},
                "status": {
                    "type": "string",
                    "enum": ["inbox", "planned", "in_progress", "done", "all"],
                    "description": "Status filter (list) or target status (update)"
                },
                "note": {"type": "string", "description": "Note text to append (for note action)"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "generate_image",
        "description": (
            "Generate an image via ComfyUI (through Ouroboros proxy). "
            "For printables: coloring pages, dot-marker sheets, tracing pages, color-by-number. "
            "Returns proxy response (job status or image info)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image description / subject"},
                "style": {
                    "type": "string",
                    "description": "Visual style: coloring_page, dot_marker, tracing, color_by_number (default: coloring_page)"
                },
                "width": {"type": "integer", "description": "Width in pixels (default 1024)"},
                "height": {"type": "integer", "description": "Height in pixels (default 768)"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "send_message",
        "description": "Send a Telegram message to Kostya. Use proactively when you have something important to say.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text to send"}
            },
            "required": ["text"]
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
            import re
            query = args["query"]
            url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = httpx.get(url, headers=headers, timeout=15)
            snippets = re.findall(r'<a class="result__snippet"[^>]*>([^<]+)<', resp.text)
            if not snippets:
                return f"No results. Raw: {resp.text[:1000]}"
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

        elif name == "task_board":
            action = args["action"]
            with _tasks_lock:
                tasks = _load_tasks()

                if action == "create":
                    task_id = str(uuid.uuid4())[:8]
                    now = datetime.now(timezone.utc).isoformat()
                    tasks[task_id] = {
                        "id": task_id,
                        "title": args.get("title", "Untitled"),
                        "description": args.get("description", ""),
                        "priority": args.get("priority", "medium"),
                        "status": "inbox",
                        "created_at": now,
                        "updated_at": now,
                        "notes": []
                    }
                    _save_tasks(tasks)
                    return f"Created task {task_id}: {args.get('title', 'Untitled')}"

                elif action == "list":
                    status_filter = args.get("status", None)
                    result_tasks = []
                    for t in tasks.values():
                        if status_filter == "all":
                            result_tasks.append(t)
                        elif status_filter is None:
                            # default: show non-done tasks
                            if t["status"] != "done":
                                result_tasks.append(t)
                        elif t["status"] == status_filter:
                            result_tasks.append(t)

                    if not result_tasks:
                        return "No tasks found."

                    prio_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
                    status_emoji = {"inbox": "📥", "planned": "📋", "in_progress": "⚡", "done": "✅"}
                    lines = []
                    for t in sorted(result_tasks, key=lambda x: x["created_at"]):
                        pe = prio_emoji.get(t["priority"], "⚪")
                        se = status_emoji.get(t["status"], "?")
                        lines.append(f"{pe}{se} [{t['id']}] {t['title']} ({t['status']})")
                        if t.get("description"):
                            lines.append(f"   {t['description'][:100]}")
                        if t.get("notes"):
                            lines.append(f"   Notes: {len(t['notes'])} entries")
                    return "\n".join(lines)

                elif action == "update":
                    task_id = args.get("task_id", "")
                    if task_id not in tasks:
                        return f"Task {task_id} not found"
                    new_status = args.get("status", tasks[task_id]["status"])
                    tasks[task_id]["status"] = new_status
                    tasks[task_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
                    _save_tasks(tasks)
                    return f"Task {task_id} status → {new_status}"

                elif action == "note":
                    task_id = args.get("task_id", "")
                    if task_id not in tasks:
                        return f"Task {task_id} not found"
                    note = args.get("note", "")
                    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                    tasks[task_id]["notes"].append(f"[{now_str}] {note}")
                    tasks[task_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
                    _save_tasks(tasks)
                    return f"Note added to task {task_id}"

                elif action == "delete":
                    task_id = args.get("task_id", "")
                    if task_id not in tasks:
                        return f"Task {task_id} not found"
                    title = tasks[task_id]["title"]
                    del tasks[task_id]
                    _save_tasks(tasks)
                    return f"Deleted task {task_id}: {title}"

                else:
                    return f"Unknown task_board action: {action}"

        elif name == "generate_image":
            import httpx
            prompt = args["prompt"]
            style = args.get("style", "coloring_page")
            width = args.get("width", 1024)
            height = args.get("height", 768)
            payload = {
                "prompt": prompt,
                "style": style,
                "size": f"{width}x{height}"
            }
            try:
                resp = httpx.post(
                    "http://192.168.1.225:9191/comfyui-proxy",
                    json=payload,
                    timeout=120
                )
                return resp.text[:2000]
            except Exception as e:
                return f"ComfyUI proxy error: {e}"

        elif name == "send_message":
            text = args.get("text", "")
            if _outgoing_queue is not None:
                try:
                    loop = asyncio.get_event_loop()
                    loop.call_soon_threadsafe(_outgoing_queue.put_nowait, text)
                    return "Message queued for delivery to Kostya"
                except Exception:
                    pass
            # Fallback: write to pending file
            pending = os.path.join(_DATA_DIR, "pending_messages.txt")
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(pending, "a") as f:
                now_str = datetime.now(timezone.utc).isoformat()
                f.write(f"[{now_str}] {text}\n")
            return "Message saved to pending file (queue not available)"

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Tool error ({name}): {e}"
