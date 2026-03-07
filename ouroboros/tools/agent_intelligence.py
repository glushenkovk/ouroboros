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
import logging

from ouroboros.tools.registry import ToolEntry, ToolContext
from ouroboros.skills import save_skill, get_skill, search_skills, list_skills
from ouroboros.error_patterns import record_error, lookup_errors, get_fix
from ouroboros.task_outcomes import record_outcome, get_stats, get_calibration_hint

log = logging.getLogger(__name__)


def get_tools() -> list:
    return [
        # ── Skill Library ──────────────────────────────────────────────────
        ToolEntry(
            name="skill_save",
            description=(
                "Save a successful task plan as a reusable skill. "
                "Call after completing a complex task that went well. "
                "Future tasks of the same type can retrieve this plan."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short skill name, e.g. 'etsy-listing' or 'pdf-assembly'"},
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
            fn=_skill_save,
        ),
        ToolEntry(
            name="skill_get",
            description="Retrieve a saved skill by exact name.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            fn=_skill_get,
        ),
        ToolEntry(
            name="skill_search",
            description=(
                "Search skills by keyword. Call BEFORE starting a complex task "
                "to find existing procedures. Returns skills sorted by use count."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search keyword"}},
                "required": ["query"],
            },
            fn=_skill_search,
        ),
        ToolEntry(
            name="skill_list",
            description="List all saved skills with their names, descriptions, and use counts.",
            input_schema={"type": "object", "properties": {}},
            fn=_skill_list,
        ),
        # ── Error Patterns ─────────────────────────────────────────────────
        ToolEntry(
            name="error_record",
            description=(
                "Record a tool error with optional fix for future reference. "
                "Include 'fix' and 'pattern' if you found a solution — "
                "this prevents repeating the same mistake."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "description": "Tool name that failed"},
                    "error_type": {"type": "string", "description": "Short error type, e.g. 'timeout', 'auth_error'"},
                    "context": {"type": "string", "description": "What you were trying to do"},
                    "fix": {"type": "string", "description": "What fixed it (if known)"},
                    "pattern": {"type": "string", "description": "Text pattern to match in future error messages"},
                },
                "required": ["tool", "error_type", "context"],
            },
            fn=_error_record,
        ),
        ToolEntry(
            name="error_lookup",
            description=(
                "Look up known errors for a tool. Returns resolved errors (with fix) first. "
                "Call before retrying a failing tool to find known solutions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "description": "Tool name (optional filter)"},
                    "error_type": {"type": "string", "description": "Error type filter (optional)"},
                },
            },
            fn=_error_lookup,
        ),
        # ── Task Outcomes ──────────────────────────────────────────────────
        ToolEntry(
            name="outcome_record",
            description=(
                "Record task outcome for self-calibration. "
                "Call after any significant task (success or failure). "
                "Builds stats over time about what approaches work."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_type": {"type": "string", "description": "Task category, e.g. 'code-edit', 'web-research', 'pdf-assembly'"},
                    "approach": {"type": "string", "description": "Approach used, e.g. 'claude_code_task', 'direct shell'"},
                    "tools_used": {"type": "array", "items": {"type": "string"}, "description": "List of tools called"},
                    "success": {"type": "boolean"},
                    "duration_s": {"type": "number", "description": "Duration in seconds (optional)"},
                    "notes": {"type": "string", "description": "What worked or what failed (optional)"},
                },
                "required": ["task_type", "approach", "tools_used", "success"],
            },
            fn=_outcome_record,
        ),
        ToolEntry(
            name="outcome_stats",
            description=(
                "Get success rate stats and calibration hints. "
                "Call before a task to see historical success rates. "
                "Low success rate = consider a different approach."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_type": {"type": "string", "description": "Filter by task type (optional, omit for all)"}
                },
            },
            fn=_outcome_stats,
        ),
    ]


# ── Implementations ────────────────────────────────────────────────────────────

def _skill_save(ctx: ToolContext, name: str, description: str, steps: list, tags: list = None):
    path = save_skill(name, description, steps, tags)
    return {"saved": True, "path": path, "step_count": len(steps)}


def _skill_get(ctx: ToolContext, name: str):
    skill = get_skill(name)
    if skill is None:
        return {"error": f"Skill '{name}' not found"}
    return skill


def _skill_search(ctx: ToolContext, query: str):
    results = search_skills(query)
    return {"count": len(results), "results": results}


def _skill_list(ctx: ToolContext):
    skills = list_skills()
    return {"count": len(skills), "skills": skills}


def _error_record(ctx: ToolContext, tool: str, error_type: str, context: str,
                  fix: str = None, pattern: str = None):
    record_error(tool, error_type, context, fix, pattern)
    return {"recorded": True, "resolved": fix is not None}


def _error_lookup(ctx: ToolContext, tool: str = None, error_type: str = None):
    errors = lookup_errors(tool, error_type)
    return {"count": len(errors), "errors": errors}


def _outcome_record(ctx: ToolContext, task_type: str, approach: str, tools_used: list,
                    success: bool, duration_s: float = None, notes: str = None):
    record_outcome(task_type, approach, tools_used, success, duration_s, notes)
    return {"recorded": True}


def _outcome_stats(ctx: ToolContext, task_type: str = None):
    stats = get_stats(task_type)
    hint = get_calibration_hint(task_type) if task_type else None
    return {"stats": stats, "hint": hint}
