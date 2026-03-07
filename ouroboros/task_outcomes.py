"""
Task Outcome Tracking — record results for self-calibration.

After each significant task: record type, approach, tools, outcome.
Builds stats for detecting what approaches work for what task types.
"""
import json
import time
from collections import defaultdict
from pathlib import Path

OUTCOMES_FILE = Path("/home/max2/ouroboros_data/memory/task_outcomes.jsonl")


def record_outcome(
    task_type: str,
    approach: str,
    tools_used: list,
    success: bool,
    duration_s: float = None,
    notes: str = None,
) -> None:
    """Record a task outcome for calibration."""
    OUTCOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "task_type": task_type,
        "approach": approach,
        "tools_used": tools_used,
        "success": success,
        "duration_s": duration_s,
        "notes": notes,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(OUTCOMES_FILE, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def get_stats(task_type: str = None) -> dict:
    """Get success rate stats by task type."""
    if not OUTCOMES_FILE.exists():
        return {}
    records = []
    with open(OUTCOMES_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if task_type is None or r.get("task_type") == task_type:
                    records.append(r)
            except Exception:
                continue
    if not records:
        return {}

    by_type = defaultdict(lambda: {"total": 0, "success": 0, "tools": defaultdict(int)})
    for r in records:
        tt = r.get("task_type", "unknown")
        by_type[tt]["total"] += 1
        if r.get("success"):
            by_type[tt]["success"] += 1
        for t in r.get("tools_used", []):
            by_type[tt]["tools"][t] += 1

    result = {}
    for tt, data in by_type.items():
        result[tt] = {
            "total": data["total"],
            "success_rate": round(data["success"] / data["total"], 2),
            "top_tools": sorted(
                data["tools"].items(), key=lambda x: x[1], reverse=True
            )[:5],
        }
    return result


def get_calibration_hint(task_type: str) -> str:
    """Return a human-readable calibration hint for a task type."""
    stats = get_stats(task_type)
    if task_type not in stats:
        return None
    s = stats[task_type]
    rate = s["success_rate"]
    hint = f"Task '{task_type}': {s['total']} attempts, {rate*100:.0f}% success."
    if s["top_tools"]:
        top = ", ".join(f"{t}({c})" for t, c in s["top_tools"][:3])
        hint += f" Most used tools: {top}."
    if rate < 0.5:
        hint += " Warning: low success rate — consider a different approach."
    return hint
