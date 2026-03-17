"""
inbox_a2a.py — A2A protocol integration using official a2a-sdk.

Provides:
  OuroborosAgentExecutor — handles incoming A2A messages
  make_agent_card()      — builds AgentCard from VERSION file
  make_a2a_app()         — creates A2AStarletteApplication instance

A2A contract:
  - Code review requests → executed synchronously via claude CLI, result returned in message artifact
  - Other messages → fire-and-forget (saved to mailbox, owner notified via Telegram)
"""

from __future__ import annotations

import asyncio
import logging
import re
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
    Message,
    Part,
    Role,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)

from ouroboros.inbox_messages import _notify_owner, save_message

log = logging.getLogger(__name__)


async def _run_code_review(text: str) -> str | None:
    """Run a Claude code review synchronously via CLI and return result text."""
    try:
        # Extract code block if present, otherwise review the whole text
        code_match = re.search(r"```[\w]*\n(.*?)```", text, re.DOTALL)
        code_to_review = code_match.group(1) if code_match else text

        prompt = (
            "Do a concise code review. Focus on: security vulnerabilities, bugs, "
            "race conditions, bad practices. Be specific — reference line numbers "
            "and provide concrete fixes.\n\n"
            f"```\n{code_to_review[:8000]}\n```"
        )

        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        if proc.returncode == 0 and stdout:
            return stdout.decode("utf-8", errors="replace").strip()
        log.warning("claude review exit=%d stderr=%s", proc.returncode, stderr.decode()[:200])
        return None
    except asyncio.TimeoutError:
        log.warning("code review timed out after 90s")
        return None
    except Exception as exc:
        log.warning("code review error: %s", exc)
        return None


def _make_response(task_id: str, context_id: str, text: str, final: bool = True) -> TaskStatusUpdateEvent:
    """Build a TaskStatusUpdateEvent with a text message artifact."""
    return TaskStatusUpdateEvent(
        taskId=task_id,
        contextId=context_id,
        status=TaskStatus(
            state=TaskState.completed if final else TaskState.working,
            message=Message(
                role=Role.agent,
                parts=[Part(root=TextPart(text=text))],
                messageId=str(uuid.uuid4()),
            ),
        ),
        final=final,
    )


class OuroborosAgentExecutor(AgentExecutor):
    """
    Handles incoming A2A tasks.

    - Code review requests: executed synchronously, result returned in artifact.
    - All other messages: saved to mailbox + owner notified via Telegram (fire-and-forget).
    """

    async def execute(self, request: RequestContext, queue: EventQueue) -> None:
        # ── Extract text ──────────────────────────────────────────────────────
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

        log.info("A2A task from %s: %s", sender, text[:80])

        # ── Save to mailbox ───────────────────────────────────────────────────
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

        if _notify_owner is not None:
            try:
                _notify_owner(f"\U0001f91d [A2A/{sender}]: {text}")
            except Exception as exc:
                log.debug("owner notify failed: %s", exc)

        # ── Route by intent ───────────────────────────────────────────────────
        lower = text.lower()
        is_review = any(kw in lower for kw in [
            "review", "ревью", "проверь код", "check code", "анализ кода",
            "код на проверку", "security", "bugs in",
        ])

        if is_review:
            # Signal working → then return result
            await queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=request.task_id,
                    contextId=request.context_id,
                    status=TaskStatus(state=TaskState.working),
                    final=False,
                )
            )
            result = await _run_code_review(text)
            if result:
                await queue.enqueue_event(
                    _make_response(request.task_id, request.context_id, result, final=True)
                )
            else:
                await queue.enqueue_event(
                    _make_response(
                        request.task_id, request.context_id,
                        "Code review failed (claude CLI error or timeout). "
                        "Message saved — owner will respond via Telegram.",
                        final=True,
                    )
                )
        else:
            # Fire-and-forget: message saved, owner handles it
            await queue.enqueue_event(
                _make_response(
                    request.task_id, request.context_id,
                    "Message received and saved. Owner will be notified via Telegram.",
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
            "Specializes in code review (synchronous, returns result artifact), "
            "inter-agent coordination, and business automation."
        ),
        url="http://192.168.1.225:9191",
        version=version,
        capabilities=AgentCapabilities(streaming=False),
        defaultInputModes=["text/plain", "application/json"],
        defaultOutputModes=["application/json"],
        skills=[
            AgentSkill(
                id="code-review",
                name="Code Review",
                description=(
                    "Send code for review. Returns structured analysis synchronously "
                    "(security, bugs, race conditions, bad practices). "
                    "Include code in ``` blocks or as plain text."
                ),
                tags=["review", "security", "code-quality"],
            ),
            AgentSkill(
                id="message",
                name="Message",
                description="Send a text message to Ouroboros. Delivered to owner via Telegram.",
                tags=["messaging", "coordination"],
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
