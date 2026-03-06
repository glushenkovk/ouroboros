import json
import os
from datetime import datetime, timezone


class Memory:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.scratchpad_path = os.path.join(data_dir, "scratchpad.md")
        self.identity_path = os.path.join(data_dir, "identity.md")
        self.chat_path = os.path.join(data_dir, "chat.jsonl")

    def read_scratchpad(self) -> str:
        if not os.path.exists(self.scratchpad_path):
            return ""
        with open(self.scratchpad_path) as f:
            return f.read()

    def write_scratchpad(self, content: str):
        with open(self.scratchpad_path, "w") as f:
            f.write(content)

    def read_identity(self) -> str:
        if not os.path.exists(self.identity_path):
            return ""
        with open(self.identity_path) as f:
            return f.read()

    def write_identity(self, content: str):
        with open(self.identity_path, "w") as f:
            f.write(content)

    def add_chat_message(self, role: str, content: str):
        entry = {
            "role": role,
            "content": content,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with open(self.chat_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_chat_history(self, last_n: int = 20) -> list:
        if not os.path.exists(self.chat_path):
            return []
        lines = []
        with open(self.chat_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(line)
        entries = []
        for line in lines[-last_n:]:
            try:
                e = json.loads(line)
                entries.append({"role": e["role"], "content": e["content"]})
            except Exception:
                pass
        return entries

    def clear_chat_history(self):
        if os.path.exists(self.chat_path):
            os.remove(self.chat_path)
