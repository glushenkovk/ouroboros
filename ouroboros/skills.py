"""
Skill Library — reusable successful task plans.

When a task succeeds with a clear plan, save it as a skill.
Before starting a complex task, search for matching skills.
"""
import json
import time
from pathlib import Path

SKILLS_DIR = Path("/home/max2/ouroboros_data/memory/skills")


def save_skill(name: str, description: str, steps: list, tags: list = None) -> str:
    """Save a successful task plan as a reusable skill."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    skill = {
        "name": name,
        "description": description,
        "steps": steps,
        "tags": tags or [],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "use_count": 0,
    }
    path = SKILLS_DIR / f"{name.replace(' ', '_').lower()}.json"
    path.write_text(json.dumps(skill, ensure_ascii=False, indent=2))
    return str(path)


def get_skill(name: str):
    """Retrieve a skill by name (increments use_count)."""
    path = SKILLS_DIR / f"{name.replace(' ', '_').lower()}.json"
    if not path.exists():
        return None
    skill = json.loads(path.read_text())
    skill["use_count"] = skill.get("use_count", 0) + 1
    path.write_text(json.dumps(skill, ensure_ascii=False, indent=2))
    return skill


def search_skills(query: str) -> list:
    """Search skills by name, description, or tags."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    q = query.lower()
    for f in SKILLS_DIR.glob("*.json"):
        s = json.loads(f.read_text())
        if (
            q in s["name"].lower()
            or q in s["description"].lower()
            or any(q in t for t in s.get("tags", []))
        ):
            results.append(s)
    return sorted(results, key=lambda x: x.get("use_count", 0), reverse=True)


def list_skills() -> list:
    """List all skills with summary (no full steps)."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    skills = []
    for f in sorted(SKILLS_DIR.glob("*.json")):
        s = json.loads(f.read_text())
        skills.append(
            {
                "name": s["name"],
                "description": s["description"],
                "tags": s.get("tags", []),
                "use_count": s.get("use_count", 0),
                "step_count": len(s.get("steps", [])),
            }
        )
    return skills
