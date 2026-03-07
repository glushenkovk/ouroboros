"""
Agent Intelligence Tools — Skill Library + Error Patterns + Task Outcomes.

Three high-leverage self-improvement patterns (2025-2026 best practices):
1. Skill Library: save/reuse successful task plans
2. Error Pattern Base: track failures + fixes to avoid repeating mistakes
3. Task Outcome Tracking: success rate stats for self-calibration

Usage:
  Before complex task → skill_search(query) + outcome_stats(task_type)
  After success      → skill_save(name, steps) + outcome_record(success=True)
  After error        → error_record(tool, error_type, fix) + outcome_record(success=False)
"""
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_DATA = Path("/home/max2/ouroboros_data/memory")
_SKILLS_DIR = _DATA / "skills"
_ERRORS_FILE = _DATA / "error_patterns.jsonl"
_OUTCOMES_FILE = _DATA / "task_outcomes.jsonl"


# ── Skill Library ──────────────────────────────────────────────────────────────

def _save_skill(name: str, description: str, steps: list, tags: list = None) -> str:
    _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    skill = {
        "name": name,
        "description": description,
        "steps": steps,
        "tags": tags or [],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "use_count": 0,
    }
    path = _SKILLS_DIR / f"{name.replace(' ', '_').lower()}.json"
    # Preserve use_count if skill exists
    if path.exists():
        existing = json.loads(path.read_text())
        skill["use_count"] = existing.get("use_count", 0)
    path.write_text(json.dumps(skill, ensure_ascii=False, indent=2))
    return str(path)


def _get_skill(name: str):
    path = _SKILLS_DIR / f"{name.replace(' ', '_').lower()}.json"
    if not path.exists():
        return None
    skill = json.loads(path.read_text())
    skill["use_count"] = skill.get("use_count", 0) + 1
    path.write_text(json.dumps(skill, ensure_ascii=False, indent=2))
    return skill


def _search_skills(query: str) -> list:
    if not _SKILLS_DIR.exists():
        return []
    q = query.lower()
    results = []
    for f in _SKILLS_DIR.glob("*.json"):
        try:
            s = json.loads(f.read_text())
            if (q in s.get("name", "").lower()
                    or q in s.get("description", "").lower()
                    or any(q in t.lower() for t in s.get("tags", []))):
                results.append(s)
        except Exception:
            pass
    return sorted(results, key=lambda x: x.get("use_count", 0), reverse=True)


def _list_skills() -> list:
    if not _SKILLS_DIR.exists():
        return []
    skills = []
    for f in sorted(_SKILLS_DIR.glob("*.json")):
        try:
            s = json.loads(f.read_text())
            skills.append({
                "name": s["name"],
                "description": s.get("description", ""),
                "use_count": s.get("use_count", 0),
                "tags": s.get("tags", []),
            })
        except Exception:
            pass
    return skills


# ── Error Patterns ─────────────────────────────────────────────────────────────

def _record_error(tool: str, error_type: str, context: str, fix: str = None, pattern: str = None):
    _ERRORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "tool": tool,
        "error_type": error_type,
        "context": context,
        "fix": fix,
        "pattern": pattern,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resolved": fix is not None,
    }
    with _ERRORS_FILE.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _lookup_errors(tool: str = None, error_type: str = None) -> list:
    if not _ERRORS_FILE.exists():
        return []
    records = []
    for line in _ERRORS_FILE.read_text().splitlines():
        try:
            r = json.loads(line)
            if tool and r.get("tool") != tool:
                continue
            if error_type and r.get("error_type") != error_type:
                continue
            records.append(r)
        except Exception:
            pass
    # Resolved errors first, then by recency
    return sorted(records, key=lambda x: (not x.get("resolved"), x.get("timestamp", "")), reverse=False)[-20:]


# ── Task Outcomes ──────────────────────────────────────────────────────────────

def _record_outcome(task_type: str, approach: str, tools_used: list,
                    success: bool, duration_s: float = None, notes: str = None):
    _OUTCOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "task_type": task_type,
        "approach": approach,
        "tools_used": tools_used,
        "success": success,
        "duration_s": duration_s,
        "notes": notes,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with _OUTCOMES_FILE.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _get_stats(task_type: str = None) -> dict:
    if not _OUTCOMES_FILE.exists():
        return {}
    buckets = defaultdict(lambda: {"total": 0, "success": 0, "approaches": defaultdict(int)})
    for line in _OUTCOMES_FILE.read_text().splitlines():
        try:
            r = json.loads(line)
            tt = r.get("task_type", "unknown")
            if task_type and tt != task_type:
                continue
            buckets[tt]["total"] += 1
            if r.get("success"):
                buckets[tt]["success"] += 1
            buckets[tt]["approaches"][r.get("approach", "unknown")] += 1
        except Exception:
            pass
    result = {}
    for tt, data in buckets.items():
        total = data["total"]
        result[tt] = {
            "total": total,
            "success": data["success"],
            "success_rate": round(data["success"] / total, 2) if total else 0,
            "top_approaches": dict(sorted(data["approaches"].items(), key=lambda x: x[1], reverse=True)[:3]),
        }
    return result


def _get_calibration_hint(task_type: str) -> str:
    stats = _get_stats(task_type)
    if not stats or task_type not in stats:
        return "No data yet — record outcomes to build calibration."
    s = stats[task_type]
    rate = s["success_rate"]
    top = list(s["top_approaches"].keys())
    if rate >= 0.8:
        return f"High confidence ({int(rate*100)}%). Best approach: {top[0] if top else 'n/a'}."
    elif rate >= 0.5:
        return f"Moderate confidence ({int(rate*100)}%). Consider trying: {top[0] if top else 'n/a'}."
    else:
        return f"Low confidence ({int(rate*100)}%). Current approaches failing — rethink strategy."


# ── Tool handlers ──────────────────────────────────────────────────────────────

def _h_skill_save(ctx: ToolContext, name: str, description: str, steps: list, tags: list = None):
    path = _save_skill(name, description, steps, tags)
    return {"saved": True, "path": path, "step_count": len(steps)}


def _h_skill_get(ctx: ToolContext, name: str):
    skill = _get_skill(name)
    if skill is None:
        return {"error": f"Skill '{name}' not found"}
    return skill


def _h_skill_search(ctx: ToolContext, query: str):
    results = _search_skills(query)
    return {"count": len(results), "results": results}


def _h_skill_list(ctx: ToolContext):
    skills = _list_skills()
    return {"count": len(skills), "skills": skills}


def _h_error_record(ctx: ToolContext, tool: str, error_type: str, context: str,
                    fix: str = None, pattern: str = None):
    _record_error(tool, error_type, context, fix, pattern)
    return {"recorded": True, "resolved": fix is not None}


def _h_error_lookup(ctx: ToolContext, tool: str = None, error_type: str = None):
    errors = _lookup_errors(tool, error_type)
    return {"count": len(errors), "errors": errors}


def _h_outcome_record(ctx: ToolContext, task_type: str, approach: str, tools_used: list,
                      success: bool, duration_s: float = None, notes: str = None):
    _record_outcome(task_type, approach, tools_used, success, duration_s, notes)
    return {"recorded": True}


def _h_outcome_stats(ctx: ToolContext, task_type: str = None):
    stats = _get_stats(task_type)
    hint = _get_calibration_hint(task_type) if task_type else None
    return {"stats": stats, "hint": hint}


# ── Tool registry ──────────────────────────────────────────────────────────────

def get_tools() -> list:
    return [
        ToolEntry(
            name="skill_save",
            schema={
                "name": "skill_save",
                "description": (
                    "Save a successful task plan as a reusable skill. "
                    "Call after completing a complex task that went well. "
                    "Future tasks of the same type can retrieve this plan via skill_search."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short skill name, e.g. 'etsy-listing'"},
                        "description": {"type": "string", "description": "What this skill does"},
                        "steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ordered steps to execute this skill",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags for search, e.g. ['etsy', 'business', 'pdf']",
                        },
                    },
                    "required": ["name", "description", "steps"],
                },
            },
            handler=_h_skill_save,
        ),
        ToolEntry(
            name="skill_get",
            schema={
                "name": "skill_get",
                "description": "Retrieve a saved skill by exact name. Increments use count.",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
            handler=_h_skill_get,
        ),
        ToolEntry(
            name="skill_search",
            schema={
                "name": "skill_search",
                "description": (
                    "Search skills by keyword. Call BEFORE starting a complex task "
                    "to find existing procedures. Returns skills sorted by use count."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Search keyword"}},
                    "required": ["query"],
                },
            },
            handler=_h_skill_search,
        ),
        ToolEntry(
            name="skill_list",
            schema={
                "name": "skill_list",
                "description": "List all saved skills with names, descriptions, tags, and use counts.",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=_h_skill_list,
        ),
        ToolEntry(
            name="error_record",
            schema={
                "name": "error_record",
                "description": (
                    "Record a tool error with optional fix for future reference. "
                    "Include 'fix' and 'pattern' if you found a solution — "
                    "this prevents repeating the same mistake."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "description": "Tool name that failed"},
                        "error_type": {"type": "string", "description": "Short error type, e.g. 'timeout', 'auth_error'"},
                        "context": {"type": "string", "description": "What you were trying to do"},
                        "fix": {"type": "string", "description": "What fixed it (if known)"},
                        "pattern": {"type": "string", "description": "Text pattern to match future errors"},
                    },
                    "required": ["tool", "error_type", "context"],
                },
            },
            handler=_h_error_record,
        ),
        ToolEntry(
            name="error_lookup",
            schema={
                "name": "error_lookup",
                "description": (
                    "Look up known errors for a tool. Returns resolved errors (with fix) first. "
                    "Call before retrying a failing tool to find known solutions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "description": "Tool name filter (optional)"},
                        "error_type": {"type": "string", "description": "Error type filter (optional)"},
                    },
                },
            },
            handler=_h_error_lookup,
        ),
        ToolEntry(
            name="outcome_record",
            schema={
                "name": "outcome_record",
                "description": (
                    "Record task outcome for self-calibration. "
                    "Call after any significant task (success or failure)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_type": {"type": "string", "description": "Task category, e.g. 'code-edit', 'web-research'"},
                        "approach": {"type": "string", "description": "Approach used, e.g. 'claude_code_task'"},
                        "tools_used": {"type": "array", "items": {"type": "string"}},
                        "success": {"type": "boolean"},
                        "duration_s": {"type": "number"},
                        "notes": {"type": "string"},
                    },
                    "required": ["task_type", "approach", "tools_used", "success"],
                },
            },
            handler=_h_outcome_record,
        ),
        ToolEntry(
            name="outcome_stats",
            schema={
                "name": "outcome_stats",
                "description": (
                    "Get success rate stats and calibration hints. "
                    "Call before a task to see historical success rates. "
                    "Low success rate = consider a different approach."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_type": {"type": "string", "description": "Filter by task type (optional)"}
                    },
                },
            },
            handler=_h_outcome_stats,
        ),
    ]
