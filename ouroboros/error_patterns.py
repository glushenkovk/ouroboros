"""
Error Pattern Base — systematic tracking of tool failures and their fixes.

Record errors when they happen. Look them up before retrying the same approach.
"""
import json
import time
from pathlib import Path

ERRORS_DIR = Path("/home/max2/ouroboros_data/memory/error_patterns")


def record_error(
    tool: str,
    error_type: str,
    context: str,
    fix: str = None,
    pattern: str = None,
) -> None:
    """Record a tool error, optionally with a known fix."""
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "tool": tool,
        "error_type": error_type,
        "context": context,
        "fix": fix,
        "pattern": pattern,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resolved": fix is not None,
    }
    fname = f"{tool}_{error_type}_{int(time.time())}.json"
    (ERRORS_DIR / fname).write_text(json.dumps(record, ensure_ascii=False, indent=2))


def lookup_errors(tool: str = None, error_type: str = None) -> list:
    """Find known errors. Resolved ones (with fix) come first."""
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for f in ERRORS_DIR.glob("*.json"):
        r = json.loads(f.read_text())
        if tool and r.get("tool") != tool:
            continue
        if error_type and error_type.lower() not in r.get("error_type", "").lower():
            continue
        results.append(r)
    return sorted(results, key=lambda x: x.get("resolved", False), reverse=True)


def get_fix(tool: str, error_snippet: str) -> str:
    """Quick lookup: given tool + error text snippet, return known fix."""
    matches = lookup_errors(tool=tool)
    snippet_lower = error_snippet.lower()
    for r in matches:
        if r.get("resolved") and r.get("pattern"):
            if r["pattern"].lower() in snippet_lower:
                return r.get("fix")
    return None
