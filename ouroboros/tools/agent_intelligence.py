"""
Agent Intelligence tools — Skill Library, Task Retrospectives, Uncertainty Flagging.

Three high-leverage patterns from autonomous agent best practices 2025-2026:
1. Skill Library — save successful task plans as reusable procedures
2. Task Retrospective — structured post-task analysis (what worked, what didn't)
3. Uncertainty Flagging — track open unknowns, retrieve before starting tasks
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

_DATA_DIR = Path("/home/max2/ouroboros_data/memory")
_SKILLS_FILE = _DATA_DIR / "skills.json"
_RETRO_FILE = _DATA_DIR / "retrospectives.jsonl"
_UNCERTAIN_FILE = _DATA_DIR / "uncertainties.json"


def _ensure_files() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _SKILLS_FILE.exists():
        _SKILLS_FILE.write_text(json.dumps({}))
    if not _UNCERTAIN_FILE.exists():
        _UNCERTAIN_FILE.write_text(json.dumps({}))


# ─── Skill Library ────────────────────────────────────────────────────────────

def _skill_save(ctx: ToolContext, name: str, description: str, steps: str, tags: str = "") -> str:
    """Save a successful task plan as a reusable skill procedure."""
    _ensure_files()
    skills = json.loads(_SKILLS_FILE.read_text())
    skills[name] = {
        "name": name,
        "description": description,
        "steps": steps,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "used_count": skills.get(name, {}).get("used_count", 0),
    }
    _SKILLS_FILE.write_text(json.dumps(skills, indent=2))
    return f"✅ Skill saved: '{name}'\n📝 {description}\n🏷️ Tags: {tags or '(none)'}"


def _skill_get(ctx: ToolContext, name: str = "", tag: str = "") -> str:
    """Retrieve a skill by name or list all skills matching a tag."""
    _ensure_files()
    skills = json.loads(_SKILLS_FILE.read_text())
    if not skills:
        return "📚 Skill library is empty. Save your first skill with skill_save."

    if name:
        s = skills.get(name)
        if not s:
            close = [k for k in skills if name.lower() in k.lower()]
            hint = f"\nDid you mean: {', '.join(close)}" if close else ""
            return f"❌ Skill '{name}' not found.{hint}"
        # Increment used_count
        s["used_count"] = s.get("used_count", 0) + 1
        _SKILLS_FILE.write_text(json.dumps(skills, indent=2))
        return (
            f"📖 Skill: {s['name']}\n"
            f"📝 {s['description']}\n\n"
            f"Steps:\n{s['steps']}\n\n"
            f"🏷️ Tags: {', '.join(s['tags']) or '(none)'} | Used: {s['used_count']}x"
        )

    # List all or filter by tag
    matched = [
        s for s in skills.values()
        if not tag or tag.lower() in [t.lower() for t in s.get("tags", [])]
    ]
    if not matched:
        return f"📚 No skills found{' with tag: ' + tag if tag else ''}."

    lines = [f"📚 Skills ({len(matched)}):\n"]
    for s in sorted(matched, key=lambda x: -x.get("used_count", 0)):
        lines.append(
            f"  📖 {s['name']} — {s['description'][:60]}{'...' if len(s['description']) > 60 else ''}"
            f" [used {s.get('used_count', 0)}x]"
        )
    return "\n".join(lines)


# ─── Task Retrospective ───────────────────────────────────────────────────────

def _retrospective_write(
    ctx: ToolContext,
    task: str,
    outcome: str,
    what_worked: str,
    what_failed: str,
    next_time: str,
    cost_usd: float = 0.0,
) -> str:
    """Record a structured post-task retrospective."""
    _ensure_files()
    entry = {
        "task": task,
        "outcome": outcome,
        "what_worked": what_worked,
        "what_failed": what_failed,
        "next_time": next_time,
        "cost_usd": cost_usd,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with _RETRO_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return (
        f"✅ Retrospective saved for: {task}\n"
        f"🎯 Outcome: {outcome}\n"
        f"✅ Worked: {what_worked[:80]}\n"
        f"❌ Failed: {what_failed[:80]}\n"
        f"💡 Next time: {next_time[:80]}"
    )


def _retrospective_read(ctx: ToolContext, keyword: str = "", last_n: int = 5) -> str:
    """Read recent retrospectives, optionally filtered by keyword."""
    _ensure_files()
    if not _RETRO_FILE.exists():
        return "📋 No retrospectives yet."

    entries = []
    for line in _RETRO_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    if keyword:
        kw = keyword.lower()
        entries = [e for e in entries if kw in e.get("task", "").lower()
                   or kw in e.get("what_worked", "").lower()
                   or kw in e.get("what_failed", "").lower()]

    if not entries:
        return f"📋 No retrospectives found{' for: ' + keyword if keyword else ''}."

    recent = entries[-last_n:]
    lines = [f"📋 Last {len(recent)} retrospective(s):\n"]
    for e in reversed(recent):
        lines.append(f"─── {e['task']} [{e['recorded_at'][:10]}]")
        lines.append(f"  🎯 {e['outcome']}")
        lines.append(f"  ✅ {e['what_worked'][:100]}")
        lines.append(f"  ❌ {e['what_failed'][:100]}")
        lines.append(f"  💡 {e['next_time'][:100]}")
        if e.get("cost_usd"):
            lines.append(f"  💰 Cost: ${e['cost_usd']:.3f}")
    return "\n".join(lines)


# ─── Uncertainty Flagging ─────────────────────────────────────────────────────

def _uncertainty_flag(ctx: ToolContext, question: str, context: str = "", domain: str = "") -> str:
    """Record an open uncertainty or knowledge gap."""
    _ensure_files()
    uncertainties: Dict[str, Any] = json.loads(_UNCERTAIN_FILE.read_text())
    uid = f"U{int(time.time())}"
    uncertainties[uid] = {
        "id": uid,
        "question": question,
        "context": context,
        "domain": domain,
        "status": "open",
        "flagged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resolved_at": None,
        "resolution": None,
    }
    _UNCERTAIN_FILE.write_text(json.dumps(uncertainties, indent=2))
    return (
        f"⚠️ Uncertainty flagged [{uid}]:\n"
        f"❓ {question}\n"
        f"🗂️ Domain: {domain or '(general)'} | Context: {context[:80] or '(none)'}"
    )


def _uncertainty_resolve(ctx: ToolContext, uid: str, resolution: str) -> str:
    """Mark an uncertainty as resolved with the answer/resolution."""
    _ensure_files()
    uncertainties = json.loads(_UNCERTAIN_FILE.read_text())
    if uid not in uncertainties:
        return f"❌ Uncertainty '{uid}' not found. List open: uncertainty_list"
    uncertainties[uid]["status"] = "resolved"
    uncertainties[uid]["resolution"] = resolution
    uncertainties[uid]["resolved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _UNCERTAIN_FILE.write_text(json.dumps(uncertainties, indent=2))
    return f"✅ Resolved [{uid}]: {resolution[:100]}"


def _uncertainty_list(ctx: ToolContext, status: str = "open", domain: str = "") -> str:
    """List uncertainties by status (open/resolved/all)."""
    _ensure_files()
    uncertainties = json.loads(_UNCERTAIN_FILE.read_text())
    if not uncertainties:
        return "💡 No uncertainties flagged yet. Clean slate."

    items = list(uncertainties.values())
    if status != "all":
        items = [u for u in items if u["status"] == status]
    if domain:
        items = [u for u in items if domain.lower() in u.get("domain", "").lower()]

    if not items:
        return f"✅ No {status} uncertainties{' in domain: ' + domain if domain else ''}."

    lines = [f"{'⚠️' if status == 'open' else '✅'} {len(items)} {status} uncertainty(-ies):\n"]
    for u in sorted(items, key=lambda x: x["flagged_at"], reverse=True):
        icon = "⚠️" if u["status"] == "open" else "✅"
        lines.append(f"  {icon} [{u['id']}] {u['question'][:80]}")
        if u.get("domain"):
            lines.append(f"     🗂️ {u['domain']}")
        if u["status"] == "resolved" and u.get("resolution"):
            lines.append(f"     → {u['resolution'][:80]}")
    return "\n".join(lines)


# ─── Tool registry ─────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="skill_save",
            schema={
                "name": "skill_save",
                "description": "Save a successful task plan as a reusable skill in the skill library. Call after completing a complex task that worked well — saves the steps so next similar task can reuse them.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short unique skill name (e.g. 'etsy-listing-flow')"},
                        "description": {"type": "string", "description": "One-line description of what this skill does"},
                        "steps": {"type": "string", "description": "Step-by-step procedure as freeform text"},
                        "tags": {"type": "string", "description": "Comma-separated tags (e.g. 'etsy,printables,pdf')"},
                    },
                    "required": ["name", "description", "steps"],
                },
            },
            handler=_skill_save,
            timeout_sec=5,
        ),
        ToolEntry(
            name="skill_get",
            schema={
                "name": "skill_get",
                "description": "Retrieve a skill by name or list skills by tag. Use BEFORE starting a complex task to check if a procedure already exists.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Exact skill name to retrieve"},
                        "tag": {"type": "string", "description": "Filter by tag instead of name"},
                    },
                    "required": [],
                },
            },
            handler=_skill_get,
            timeout_sec=5,
        ),
        ToolEntry(
            name="retrospective_write",
            schema={
                "name": "retrospective_write",
                "description": "Record a structured retrospective after completing a task. Builds institutional memory for future iterations.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "Task name/description"},
                        "outcome": {"type": "string", "description": "Success / partial / failure + one sentence why"},
                        "what_worked": {"type": "string", "description": "What went well"},
                        "what_failed": {"type": "string", "description": "What went wrong or was slow"},
                        "next_time": {"type": "string", "description": "Concrete improvement for next similar task"},
                        "cost_usd": {"type": "number", "description": "Approximate cost of the task in USD"},
                    },
                    "required": ["task", "outcome", "what_worked", "what_failed", "next_time"],
                },
            },
            handler=_retrospective_write,
            timeout_sec=5,
        ),
        ToolEntry(
            name="retrospective_read",
            schema={
                "name": "retrospective_read",
                "description": "Read recent task retrospectives to learn from past work. Use before planning similar tasks.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "Filter retrospectives by keyword"},
                        "last_n": {"type": "integer", "description": "Number of recent entries to return (default 5)"},
                    },
                    "required": [],
                },
            },
            handler=_retrospective_read,
            timeout_sec=5,
        ),
        ToolEntry(
            name="uncertainty_flag",
            schema={
                "name": "uncertainty_flag",
                "description": "Flag an open question or knowledge gap. Use when you're unsure about something important — captures it for later resolution instead of guessing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The open question or uncertainty"},
                        "context": {"type": "string", "description": "What triggered this uncertainty"},
                        "domain": {"type": "string", "description": "Domain/area (e.g. 'etsy', 'a2a', 'pricing')"},
                    },
                    "required": ["question"],
                },
            },
            handler=_uncertainty_flag,
            timeout_sec=5,
        ),
        ToolEntry(
            name="uncertainty_resolve",
            schema={
                "name": "uncertainty_resolve",
                "description": "Mark an uncertainty as resolved once you have the answer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uid": {"type": "string", "description": "Uncertainty ID (from uncertainty_flag or uncertainty_list)"},
                        "resolution": {"type": "string", "description": "The answer or resolution"},
                    },
                    "required": ["uid", "resolution"],
                },
            },
            handler=_uncertainty_resolve,
            timeout_sec=5,
        ),
        ToolEntry(
            name="uncertainty_list",
            schema={
                "name": "uncertainty_list",
                "description": "List open uncertainties. Check before starting a task — if you have flagged questions in this domain, resolve them first.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["open", "resolved", "all"], "description": "Filter by status (default: open)"},
                        "domain": {"type": "string", "description": "Filter by domain"},
                    },
                    "required": [],
                },
            },
            handler=_uncertainty_list,
            timeout_sec=5,
        ),
    ]
