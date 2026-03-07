"""
agent_inbox.py — backwards-compatible facade.

Split into three modules (P5: each module < 1000 lines):
  ouroboros/inbox_messages.py  — mailbox read/write/pop
  ouroboros/inbox_a2a.py       — A2A protocol handler
  ouroboros/inbox_server.py    — aiohttp HTTP server + routes

All public symbols re-exported here so existing imports continue to work.
"""

from ouroboros.inbox_messages import (  # noqa: F401
    MAILBOX_PATH,
    INBOX_PORT,
    COMFYUI_HOST,
    set_owner_notifier,
    get_unread_count,
    pop_unread_messages,
    save_message,
    _read_messages,
    _notify_owner,
)
from ouroboros.inbox_a2a import (  # noqa: F401
    _handle_agent_card,
    _handle_a2a,
    _get_exposed_tools,
    _execute_a2a_tool,
    _tasks,
    _cleanup_tasks,
)
from ouroboros.inbox_server import (  # noqa: F401
    start_inbox_server_background,
    start_inbox_server,
    _start_and_signal,
    _handle_chat_ui,
    _handle_health,
    _handle_message,
    _handle_get_messages,
    _handle_comfyui_proxy,
    _handle_comfyui_models,
)
