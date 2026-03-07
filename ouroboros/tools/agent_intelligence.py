"""
Agent Intelligence tools — Skill Library, Task Retrospectives, Uncertainty Flagging,
Episode Replay / Pattern Mining, Critic Loop.

Best practices from autonomous agent research 2025-2026:
1. Skill Library — save successful task plans as reusable procedures
2. Task Retrospective — structured post-task analysis
3. Uncertainty Flagging — track open unknowns, retrieve before starting tasks
4. Episode Replay — mine events.jsonl for patterns (budget hotspots, failure modes)
5. Critic Loop — fast internal self-critique BEFORE committing
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

_DATA_DIR = Path("/home/max2/ouroboros_data/memory")
_EVENTS_FILE = Path("/home/max2/ouroboros_data/logs/events.jsonl")
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
        s["used_count"] = s.get("used_count", 0) + 1
        _SKILLS_FILE.write_text(json.dumps(skills, indent=2))
        return (
            f"📖 Skill: {s['name']}\n"
            f"📝 {s['description']}\n\n"
            f"Steps:\n{s['steps']}\n\n"
            f"🏷️ Tags: {', '.join(s['tags']) or '(none)'} | Used: {s['used_count']}x"
        )

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


# ─── Episode Replay / Pattern Mining ─────────────────────────────────────────

def _load_events(last_n: int) -> List[Dict]:
    """Load last N lines from events.jsonl."""
    if not _EVENTS_FILE.exists():
        return []
    lines = _EVENTS_FILE.read_text().splitlines()
    recent = lines[-last_n:] if len(lines) > last_n else lines
    events = []
    for line in recent:
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def _episode_replay(ctx: ToolContext, last_n: int = 100) -> str:
    """Analyze last N events from events.jsonl. Returns patterns:
    - Task success/failure rates
    - Budget hotspots by model/category
    - Slow tasks and tool timeouts
    - Actionable recommendations
    """
    events = _load_events(last_n)
    if not events:
        return "📊 No events to analyze yet."

    # Aggregate by type
    task_evals = [e for e in events if e.get("type") == "task_eval"]
    llm_usage = [e for e in events if e.get("type") == "llm_usage"]
    tool_timeouts = [e for e in events if e.get("type") in ("tool_timeout", "consciousness_tool_timeout")]
    llm_errors = [e for e in events if e.get("type") in ("llm_api_error", "llm_empty_response", "consciousness_llm_error")]

    lines = [f"📊 Episode Replay — last {len(events)} events\n"]

    # Task performance
    if task_evals:
        ok = sum(1 for e in task_evals if e.get("ok"))
        fail = len(task_evals) - ok
        durations = [e.get("duration_sec", 0) for e in task_evals]
        avg_dur = sum(durations) / len(durations)
        slow = [e for e in task_evals if e.get("duration_sec", 0) > 60]
        lines.append(f"🎯 Tasks: {len(task_evals)} total | ✅ {ok} ok | ❌ {fail} failed | avg {avg_dur:.0f}s")
        if slow:
            lines.append(f"   🐢 Slow tasks (>60s): {len(slow)} — IDs: {', '.join(e.get('task_id','?')[:8] for e in slow[:5])}")

    # Budget hotspots
    if llm_usage:
        total_cost = sum(e.get("cost", 0) for e in llm_usage)
        by_model: Dict[str, float] = defaultdict(float)
        by_category: Dict[str, float] = defaultdict(float)
        for e in llm_usage:
            m = e.get("model", "unknown") or "unknown"
            c = e.get("category", "unknown") or "unknown"
            cost = e.get("cost", 0)
            by_model[m] += cost
            by_category[c] += cost

        lines.append(f"\n💰 Budget: ${total_cost:.4f} in these {len(llm_usage)} LLM calls")
        top_models = sorted(by_model.items(), key=lambda x: -x[1])[:3]
        for model, cost in top_models:
            short = model.split("/")[-1] if "/" in model else model
            lines.append(f"   📌 {short}: ${cost:.4f}")
        top_cats = sorted(by_category.items(), key=lambda x: -x[1])[:3]
        lines.append(f"   Categories: {', '.join(f'{c}=${v:.4f}' for c, v in top_cats)}")

    # Tool issues
    if tool_timeouts:
        lines.append(f"\n⏱️ Tool timeouts: {len(tool_timeouts)}")
        by_tool: Dict[str, int] = defaultdict(int)
        for e in tool_timeouts:
            t = e.get("tool", e.get("tool_name", "unknown"))
            by_tool[t] += 1
        for tool, count in sorted(by_tool.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"   ⚠️ {tool}: {count}x")

    if llm_errors:
        lines.append(f"\n🔴 LLM errors: {len(llm_errors)}")

    # Recommendations
    lines.append("\n💡 Recommendations:")
    if task_evals and (len(task_evals) - sum(1 for e in task_evals if e.get("ok"))) / len(task_evals) > 0.2:
        lines.append("  → High failure rate (>20%) — check recent failed task IDs and error patterns")
    if tool_timeouts and len(tool_timeouts) > 3:
        lines.append("  → Multiple tool timeouts — consider increasing timeout_sec or using async approach")
    if llm_usage:
        consciousness_cost = sum(e.get("cost", 0) for e in llm_usage if e.get("category") == "consciousness")
        task_cost = sum(e.get("cost", 0) for e in llm_usage if e.get("category") == "task")
        if consciousness_cost > task_cost and task_cost > 0:
            lines.append("  → Consciousness costs more than tasks — consider longer sleep intervals or context pruning")
    if not tool_timeouts and not llm_errors and task_evals and sum(1 for e in task_evals if e.get("ok")) == len(task_evals):
        lines.append("  ✅ All clear — system running smoothly in this window")

    return "\n".join(lines)


# ─── Critic Loop ─────────────────────────────────────────────────────────────

_CRITIC_ANTIPATTERNS = [
    (r'\bprint\s*\(', "bare print() — use logging instead"),
    (r'except\s*:', "bare except — catch specific exceptions"),
    (r'TODO|FIXME|HACK|XXX', "unresolved TODO/FIXME/HACK marker"),
    (r'hardcoded|HARDCODED', "hardcoded value flagged in comment"),
    (r'time\.sleep\(\d{2,}', "long sleep() in sync code — consider async"),
    (r'eval\s*\(|exec\s*\(', "eval/exec — security risk"),
    (r'password\s*=\s*["\']', "hardcoded password"),
    (r'import \*', "wildcard import — hides dependencies"),
]

_BIBLE_CHECKS = [
    (r'def \w+\([^)]{200,}\)', "method with very long parameter list (>8 params, P5 violation)"),
    (r'if .+?:\s*\n\s+if .+?:\s*\n\s+if .+?:\s*\n\s+if ', "deep nesting (4+ levels, P5 signal)"),
]


def _critic_review(ctx: ToolContext, code_or_plan: str, context: str = "") -> str:
    """Fast internal self-critique BEFORE committing.

    Checks for anti-patterns, Bible violations, and simplification opportunities.
    Not a multi-model review — lightweight rule-based pass.
    """
    risks = []
    simplifications = []
    bible_violations = []

    # Anti-pattern checks
    for pattern, message in _CRITIC_ANTIPATTERNS:
        matches = re.findall(pattern, code_or_plan, re.MULTILINE)
        if matches:
            risks.append(f"⚠️ {message} ({len(matches)}x)")

    # Bible checks
    for pattern, message in _BIBLE_CHECKS:
        if re.search(pattern, code_or_plan, re.MULTILINE | re.DOTALL):
            bible_violations.append(f"📖 {message}")

    # Line count check (P5: module < ~1000 lines)
    lines = code_or_plan.count("\n")
    if lines > 800:
        bible_violations.append(f"📖 P5 warning: {lines} lines — approaching 1000-line module limit")
    elif lines > 1000:
        bible_violations.append(f"📖 P5 VIOLATION: {lines} lines — exceeds module limit")

    # Function length check
    func_blocks = re.findall(r'def \w+.*?(?=\ndef |\Z)', code_or_plan, re.DOTALL)
    long_funcs = [(m[:50], m.count("\n")) for m in func_blocks if m.count("\n") > 150]
    for name, count in long_funcs:
        bible_violations.append(f"📖 P5: function ~{count} lines — exceeds 150-line limit: {name}...")

    # Simplification hints
    if code_or_plan.count("for ") > 5 and "[" in code_or_plan:
        simplifications.append("💡 Multiple loops — some may be replaceable with list comprehensions")
    if code_or_plan.count("if ") > code_or_plan.count("else") * 3:
        simplifications.append("💡 Many if-branches without else — consider dict dispatch or early returns")
    if len(re.findall(r'\.get\(', code_or_plan)) > 3:
        simplifications.append("💡 Multiple .get() calls — consider dataclass or TypedDict for structured data")

    # Verdict
    critical_count = len([r for r in risks if "password" in r or "eval" in r])
    if critical_count > 0:
        verdict = "🛑 STOP_AND_RETHINK"
    elif len(risks) > 3 or len(bible_violations) > 2:
        verdict = "⚠️ NEEDS_REVIEW"
    else:
        verdict = "✅ LOOKS_GOOD"

    lines_out = [f"🔍 Critic Review — {verdict}\n"]
    if risks:
        lines_out.append(f"Risks ({len(risks)}):")
        lines_out.extend(f"  {r}" for r in risks)
    if bible_violations:
        lines_out.append(f"\nBible violations ({len(bible_violations)}):")
        lines_out.extend(f"  {v}" for v in bible_violations)
    if simplifications:
        lines_out.append(f"\nSimplifications ({len(simplifications)}):")
        lines_out.extend(f"  {s}" for s in simplifications)
    if not risks and not bible_violations and not simplifications:
        lines_out.append("  No issues found. Proceed with commit.")

    if context:
        lines_out.append(f"\nContext considered: {context[:100]}")

    return "\n".join(lines_out)


def _critic_review_file(ctx: ToolContext, file_path: str) -> str:
    """Read a file and run critic_review on its contents."""
    path = Path(file_path)
    if not path.exists():
        return f"❌ File not found: {file_path}"
    content = path.read_text(encoding="utf-8", errors="replace")
    result = _critic_review(ctx, content, context=f"file: {file_path}")
    return f"📄 Reviewing: {file_path} ({content.count(chr(10))} lines)\n\n{result}"


# ─── Tool registry ─────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="skill_save",
            schema={
                "name": "skill_save",
                "description": "Save a successful task plan as a reusable skill in the skill library. Call after completing a complex task that worked well.",
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
                "description": "Retrieve a skill by name or list skills by tag. Use BEFORE starting a complex task.",
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
                "description": "Record a structured retrospective after completing a task. Builds institutional memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "Task name/description"},
                        "outcome": {"type": "string", "description": "Success / partial / failure + one sentence why"},
                        "what_worked": {"type": "string", "description": "What went well"},
                        "what_failed": {"type": "string", "description": "What went wrong or was slow"},
                        "next_time": {"type": "string", "description": "Concrete improvement for next similar task"},
                        "cost_usd": {"type": "number", "description": "Approximate cost in USD"},
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
                "description": "Read recent task retrospectives to learn from past work.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "Filter by keyword"},
                        "last_n": {"type": "integer", "description": "Number of recent entries (default 5)"},
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
                "description": "Flag an open question or knowledge gap instead of guessing.",
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
                        "uid": {"type": "string", "description": "Uncertainty ID"},
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
                "description": "List open uncertainties. Check before starting a task.",
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
        ToolEntry(
            name="episode_replay",
            schema={
                "name": "episode_replay",
                "description": "Analyze recent events.jsonl for patterns: task success rates, budget hotspots, tool timeouts, slow tasks. Use periodically to spot systemic issues.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "last_n": {"type": "integer", "description": "Number of recent log lines to analyze (default 100)"},
                    },
                    "required": [],
                },
            },
            handler=_episode_replay,
            timeout_sec=15,
        ),
        ToolEntry(
            name="critic_review",
            schema={
                "name": "critic_review",
                "description": "Fast internal self-critique of code or plan BEFORE committing. Rule-based: checks anti-patterns, Bible violations (P5 minimalism), simplification opportunities. Not a multi-model review — lightweight, free, instant.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code_or_plan": {"type": "string", "description": "Code snippet or plan text to review"},
                        "context": {"type": "string", "description": "What this code/plan is doing (optional)"},
                    },
                    "required": ["code_or_plan"],
                },
            },
            handler=_critic_review,
            timeout_sec=5,
        ),
        ToolEntry(
            name="critic_review_file",
            schema={
                "name": "critic_review_file",
                "description": "Run critic_review on an entire file. Reads the file and checks for anti-patterns and Bible violations.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Absolute path to the file to review"},
                    },
                    "required": ["file_path"],
                },
            },
            handler=_critic_review_file,
            timeout_sec=10,
        ),
    ]
