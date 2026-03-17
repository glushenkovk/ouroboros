"""
inbox_a2a.py — A2A protocol integration using official a2a-sdk.

Provides:
  OuroborosAgentExecutor — handles incoming A2A messages
  make_agent_card()      — builds AgentCard from VERSION file
  make_a2a_app()         — creates A2AStarletteApplication instance
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

from ouroboros.inbox_messages import _notify_owner, save_message

log = logging.getLogger(__name__)


class OuroborosAgentExecutor(AgentExecutor):
    """Handles incoming A2A messages — saves to mailbox and notifies owner."""

    async def execute(self, request: RequestContext, queue: EventQueue) -> None:
        # Extract text from message parts
        text = ""
        if request.message and request.message.parts:
            for part in request.message.parts:
                root = getattr(part, "root", part)
                text += getattr(root, "text", "") or ""
        text = text.strip()

        sender = "a2a-agent"
        if request.message:
            meta = getattr(request.message, "metadata", None) or {}
            if isinstance(meta, dict):
                sender = meta.get("from", sender)

        msg = {
            "id": str(uuid.uuid4()),
            "from": sender,
            "to": "ouroboros",
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "read": False,
            "a2a_task_id": request.task_id,
        }
        save_message(msg)
        log.info("A2A message from %s: %s", sender, text[:80])

        if _notify_owner is not None:
            try:
                _notify_owner(f"\U0001f91d [A2A/{sender}]: {text}")
            except Exception as e:
                log.debug("owner notify failed: %s", e)

        await queue.enqueue_event(
            TaskStatusUpdateEvent(
                taskId=request.task_id,
                contextId=request.context_id,
                status=TaskStatus(state=TaskState.completed),
                final=True,
            )
        )

    async def cancel(self, request: RequestContext, queue: EventQueue) -> None:
        await queue.enqueue_event(
            TaskStatusUpdateEvent(
                taskId=request.task_id,
                contextId=request.context_id,
                status=TaskStatus(state=TaskState.canceled),
                final=True,
            )
        )


def make_agent_card() -> AgentCard:
    """Build AgentCard for Ouroboros."""
    try:
        version = open("/home/max2/ouroboros_repo/VERSION").read().strip()
    except Exception:
        version = "6.10.0"

    return AgentCard(
        name="Ouroboros",
        description=(
            "Autonomous digital agent — self-creating, self-improving. "
            "Specializes in code, image generation (ComfyUI/Flux), "
            "inter-agent coordination, and business automation."
        ),
        url="http://192.168.1.225:9191",
        version=version,
        capabilities=AgentCapabilities(streaming=False),
        defaultInputModes=["text/plain", "application/json"],
        defaultOutputModes=["application/json"],
        skills=[
            AgentSkill(
                id="message",
                name="Message",
                description="Send a text message to Ouroboros. Delivered to owner via Telegram.",
                tags=["messaging", "coordination"],
            ),
            AgentSkill(
                id="comfyui-generate",
                name="ComfyUI Image Generation",
                description="Generate images via ComfyUI proxy (RTX 3090, Flux).",
                tags=["image-generation", "comfyui", "flux"],
            ),
        ],
    )


def make_a2a_app() -> A2AStarletteApplication:
    """Create and return A2AStarletteApplication instance."""
    task_store = InMemoryTaskStore()
    executor = OuroborosAgentExecutor()
    handler = DefaultRequestHandler(agent_executor=executor, task_store=task_store)
    card = make_agent_card()
    return A2AStarletteApplication(agent_card=card, http_handler=handler)
