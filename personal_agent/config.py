import os

TG_TOKEN = os.environ.get("AGENT_TG_TOKEN", "")
OWNER_ID = int(os.environ.get("AGENT_OWNER_ID", "0"))
DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/home/max2/agent_data")
AGENT_NAME = os.environ.get("AGENT_NAME", "Agent")
CLAUDE_MODEL = os.environ.get("AGENT_CLAUDE_MODEL", "google/gemini-2.5-pro-preview")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
BG_INTERVAL = int(os.environ.get("AGENT_BG_INTERVAL", "900"))
