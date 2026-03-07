"""
Hierarchical Planning tools — Epic → Story → Task.

Feature 5 from autonomous agent best practices 2025-2026:
Hierarchical Planning — maintain a coherent multi-level plan across evolution cycles.
Epic: high-level goal (weeks), Story: capability (days), Task: concrete action (hours).

Storage: /home/max2/ouroboros_data/memory/plan.json
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

_PLAN_FILE = Path("/home/max2/ouroboros_data/memory/plan.json")
_STATUSES = ("planned", "in_progress", "done", "blocked", "cancelled")


def _load() -> dict:
    _PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _PLAN_FILE.exists():
        _PLAN_FILE.write_text(json.dumps({"epics": [], "stories": [], "tasks": []}))
    return json.loads(_PLAN_FILE.read_text())


def _save(plan: dict) -> None:
    _PLAN_FILE.write_text(json.dumps(plan, indent=2))


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ─── Epics ────────────────────────────────────────────────────────────────────

def _epic_create(ctx: ToolContext, title: str, description: str, tags: str = "") -> str:
    """Create a new Epic (high-level goal)."""
    plan = _load()
    epic = {
        "id": _short_id(),
        "title": title,
        "description": description,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "status": "planned",
        "created_at": _now(),
        "updated_at": _now(),
    }
    plan["epics"].append(epic)
    _save(plan)
    return f"🗺️ Epic created [{epic['id']}]: {title}\n📝 {description}"


def _epic_list(ctx: ToolContext, status: str = "") -> str:
    """List all epics, optionally filtered by status."""
    plan = _load()
    epics = plan["epics"]
    if status:
        epics = [e for e in epics if e["status"] == status]
    if not epics:
        return f"🗺️ No epics found{' with status: ' + status if status else ''}."

    stories = plan["stories"]
    tasks = plan["tasks"]
    lines = [f"🗺️ Epics ({len(epics)}):\n"]
    for e in epics:
        s_count = sum(1 for s in stories if s.get("epic_id") == e["id"])
        t_count = sum(1 for t in tasks if any(
            s["id"] == t.get("story_id") for s in stories if s.get("epic_id") == e["id"]
        ))
        icon = {"planned": "📋", "in_progress": "🔄", "done": "✅", "blocked": "🚫", "cancelled": "❌"}.get(e["status"], "?")
        lines.append(f"  {icon} [{e['id']}] {e['title']} — {s_count} stories, {t_count} tasks")
    return "\n".join(lines)


# ─── Stories ──────────────────────────────────────────────────────────────────

def _story_create(ctx: ToolContext, epic_id: str, title: str, description: str) -> str:
    """Create a Story under an Epic."""
    plan = _load()
    epic_ids = [e["id"] for e in plan["epics"]]
    if epic_id not in epic_ids:
        return f"❌ Epic '{epic_id}' not found. Available: {', '.join(epic_ids)}"
    story = {
        "id": _short_id(),
        "epic_id": epic_id,
        "title": title,
        "description": description,
        "status": "planned",
        "created_at": _now(),
        "updated_at": _now(),
    }
    plan["stories"].append(story)
    _save(plan)
    return f"📖 Story created [{story['id']}] under epic {epic_id}: {title}"


def _story_list(ctx: ToolContext, epic_id: str = "", status: str = "") -> str:
    """List stories, optionally filtered by epic_id or status."""
    plan = _load()
    stories = plan["stories"]
    if epic_id:
        stories = [s for s in stories if s.get("epic_id") == epic_id]
    if status:
        stories = [s for s in stories if s["status"] == status]
    if not stories:
        return f"📖 No stories found{' for epic ' + epic_id if epic_id else ''}{' with status ' + status if status else ''}."

    tasks = plan["tasks"]
    lines = [f"📖 Stories ({len(stories)}):\n"]
    for s in stories:
        t_count = sum(1 for t in tasks if t.get("story_id") == s["id"])
        t_done = sum(1 for t in tasks if t.get("story_id") == s["id"] and t["status"] == "done")
        icon = {"planned": "📋", "in_progress": "🔄", "done": "✅", "blocked": "🚫", "cancelled": "❌"}.get(s["status"], "?")
        lines.append(f"  {icon} [{s['id']}] {s['title']} — {t_done}/{t_count} tasks done (epic: {s.get('epic_id', '?')})")
    return "\n".join(lines)


# ─── Tasks ────────────────────────────────────────────────────────────────────

def _task_create(ctx: ToolContext, story_id: str, title: str, description: str, tool_hint: str = "") -> str:
    """Create a Task under a Story."""
    plan = _load()
    story_ids = [s["id"] for s in plan["stories"]]
    if story_id not in story_ids:
        return f"❌ Story '{story_id}' not found. Available: {', '.join(story_ids)}"
    task = {
        "id": _short_id(),
        "story_id": story_id,
        "title": title,
        "description": description,
        "tool_hint": tool_hint,
        "status": "planned",
        "notes": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    plan["tasks"].append(task)
    _save(plan)
    hint_str = f" [tool: {tool_hint}]" if tool_hint else ""
    return f"✅ Task created [{task['id']}] under story {story_id}: {title}{hint_str}"


def _task_list(ctx: ToolContext, story_id: str = "", status: str = "") -> str:
    """List tasks, optionally filtered by story_id or status."""
    plan = _load()
    tasks = plan["tasks"]
    if story_id:
        tasks = [t for t in tasks if t.get("story_id") == story_id]
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    if not tasks:
        return f"📋 No tasks found{' for story ' + story_id if story_id else ''}{' with status ' + status if status else ''}."

    lines = [f"📋 Tasks ({len(tasks)}):\n"]
    for t in tasks:
        icon = {"planned": "📋", "in_progress": "🔄", "done": "✅", "blocked": "🚫", "cancelled": "❌"}.get(t["status"], "?")
        hint = f" [{t['tool_hint']}]" if t.get("tool_hint") else ""
        lines.append(f"  {icon} [{t['id']}] {t['title']}{hint} (story: {t.get('story_id', '?')})")
        if t.get("notes"):
            lines.append(f"     💬 {t['notes'][:80]}")
    return "\n".join(lines)


def _task_update_status(ctx: ToolContext, task_id: str, status: str, notes: str = "") -> str:
    """Update a task's status."""
    if status not in _STATUSES:
        return f"❌ Invalid status '{status}'. Valid: {', '.join(_STATUSES)}"
    plan = _load()
    for t in plan["tasks"]:
        if t["id"] == task_id:
            t["status"] = status
            t["updated_at"] = _now()
            if notes:
                t["notes"] = notes
            _save(plan)
            icon = {"planned": "📋", "in_progress": "🔄", "done": "✅", "blocked": "🚫", "cancelled": "❌"}.get(status, "?")
            return f"{icon} Task [{task_id}] → {status}: {t['title']}"
    return f"❌ Task '{task_id}' not found."


# ─── Plan Summary ─────────────────────────────────────────────────────────────

def _plan_summary(ctx: ToolContext) -> str:
    """Return current plan as readable text: all epics → stories → tasks."""
    plan = _load()
    if not plan["epics"] and not plan["stories"] and not plan["tasks"]:
        return "📊 Plan is empty. Create an Epic to start hierarchical planning."

    epics = plan["epics"]
    stories = plan["stories"]
    tasks = plan["tasks"]

    lines = ["📊 Current Plan\n" + "=" * 40]

    for epic in epics:
        e_icon = {"planned": "📋", "in_progress": "🔄", "done": "✅", "blocked": "🚫", "cancelled": "❌"}.get(epic["status"], "?")
        lines.append(f"\n{e_icon} EPIC [{epic['id']}]: {epic['title']}")
        if epic.get("description"):
            lines.append(f"   {epic['description'][:100]}")

        epic_stories = [s for s in stories if s.get("epic_id") == epic["id"]]
        for story in epic_stories:
            s_icon = {"planned": "  📖", "in_progress": "  🔄", "done": "  ✅", "blocked": "  🚫", "cancelled": "  ❌"}.get(story["status"], "  ?")
            story_tasks = [t for t in tasks if t.get("story_id") == story["id"]]
            done = sum(1 for t in story_tasks if t["status"] == "done")
            lines.append(f"\n{s_icon} STORY [{story['id']}]: {story['title']} ({done}/{len(story_tasks)} done)")

            for task in story_tasks:
                t_icon = {"planned": "    📋", "in_progress": "    🔄", "done": "    ✅", "blocked": "    🚫", "cancelled": "    ❌"}.get(task["status"], "    ?")
                hint = f" [{task['tool_hint']}]" if task.get("tool_hint") else ""
                lines.append(f"{t_icon} [{task['id']}] {task['title']}{hint}")

    # Orphan stories (no epic)
    orphan_stories = [s for s in stories if not any(e["id"] == s.get("epic_id") for e in epics)]
    if orphan_stories:
        lines.append("\n📖 STORIES (no epic):")
        for s in orphan_stories:
            lines.append(f"  [{s['id']}] {s['title']} ({s['status']})")

    # Stats
    total_tasks = len(tasks)
    done_tasks = sum(1 for t in tasks if t["status"] == "done")
    in_prog = sum(1 for t in tasks if t["status"] == "in_progress")
    lines.append(f"\n{'─' * 40}")
    lines.append(f"📊 {len(epics)} epics | {len(stories)} stories | {total_tasks} tasks ({done_tasks} done, {in_prog} in progress)")

    return "\n".join(lines)


# ─── Tool registry ─────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="epic_create",
            schema={
                "name": "epic_create",
                "description": "Create a high-level Epic (goal spanning multiple evolution cycles). E.g. 'Launch Etsy shop', 'A2A network expansion'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short epic title"},
                        "description": {"type": "string", "description": "What success looks like for this epic"},
                        "tags": {"type": "string", "description": "Comma-separated tags"},
                    },
                    "required": ["title", "description"],
                },
            },
            handler=_epic_create,
            timeout_sec=5,
        ),
        ToolEntry(
            name="epic_list",
            schema={
                "name": "epic_list",
                "description": "List all epics with story/task counts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "Filter by status (planned/in_progress/done/blocked/cancelled)"},
                    },
                    "required": [],
                },
            },
            handler=_epic_list,
            timeout_sec=5,
        ),
        ToolEntry(
            name="story_create",
            schema={
                "name": "story_create",
                "description": "Create a Story under an Epic. A story is a capability or feature (typically 1-3 evolution cycles).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "epic_id": {"type": "string", "description": "Parent epic ID"},
                        "title": {"type": "string", "description": "Story title"},
                        "description": {"type": "string", "description": "What this story delivers"},
                    },
                    "required": ["epic_id", "title", "description"],
                },
            },
            handler=_story_create,
            timeout_sec=5,
        ),
        ToolEntry(
            name="story_list",
            schema={
                "name": "story_list",
                "description": "List stories with task progress.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "epic_id": {"type": "string", "description": "Filter by parent epic ID"},
                        "status": {"type": "string", "description": "Filter by status"},
                    },
                    "required": [],
                },
            },
            handler=_story_list,
            timeout_sec=5,
        ),
        ToolEntry(
            name="task_create",
            schema={
                "name": "task_create",
                "description": "Create a concrete Task under a Story. Tasks are the unit of one evolution cycle.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "story_id": {"type": "string", "description": "Parent story ID"},
                        "title": {"type": "string", "description": "Task title"},
                        "description": {"type": "string", "description": "What to do concretely"},
                        "tool_hint": {"type": "string", "description": "Primary tool/method to use (e.g. 'claude_code_task', 'generate_printables_batch')"},
                    },
                    "required": ["story_id", "title", "description"],
                },
            },
            handler=_task_create,
            timeout_sec=5,
        ),
        ToolEntry(
            name="task_list",
            schema={
                "name": "task_list",
                "description": "List tasks with status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "story_id": {"type": "string", "description": "Filter by parent story ID"},
                        "status": {"type": "string", "description": "Filter by status"},
                    },
                    "required": [],
                },
            },
            handler=_task_list,
            timeout_sec=5,
        ),
        ToolEntry(
            name="task_update_status",
            schema={
                "name": "task_update_status",
                "description": "Update task status. Use throughout evolution cycles to track progress.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "Task ID"},
                        "status": {"type": "string", "enum": list(_STATUSES), "description": "New status"},
                        "notes": {"type": "string", "description": "Optional notes about the status change"},
                    },
                    "required": ["task_id", "status"],
                },
            },
            handler=_task_update_status,
            timeout_sec=5,
        ),
        ToolEntry(
            name="plan_summary",
            schema={
                "name": "plan_summary",
                "description": "Show full hierarchical plan: all epics → stories → tasks with statuses. Use at the start of evolution cycles to see what's planned.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_plan_summary,
            timeout_sec=5,
        ),
    ]
