"""
Coffee Roasting RAG Query Tool.

Reads all .md files from Drive memory/coffee_roasting/ and answers
questions using a single LLM call. Used by the /coffee Telegram command.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Tuple

log = logging.getLogger(__name__)

# Cheap large-context model for RAG queries
COFFEE_RAG_MODEL = "google/gemini-2.0-flash-lite"

COFFEE_RAG_SYSTEM = """You are a coffee roasting expert assistant. The user home-roasts coffee on a Behmor 2000AB Plus roaster in Austin, Texas.

Answer the question using ONLY the provided knowledge base. Be specific, practical, and concise.
Format your answer cleanly for Telegram — use bullet points for lists, bold key terms with **asterisks**.
Keep it under 600 words unless the question clearly demands more detail.
If the answer isn't in the knowledge base, say so honestly and give your best general advice."""


def _load_coffee_files(drive_root: pathlib.Path) -> Tuple[str, list]:
    """Read all .md files from memory/coffee_roasting/ and return (context, file_names)."""
    kb_dir = drive_root / "memory" / "coffee_roasting"
    files_loaded = []
    sections = []

    if not kb_dir.exists():
        return "", []

    for md_file in sorted(kb_dir.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8").strip()
            if content:
                sections.append(f"## [{md_file.stem}]\n\n{content}")
                files_loaded.append(md_file.stem)
        except Exception as e:
            log.warning("Failed to read coffee knowledge file %s: %s", md_file, e)

    return "\n\n---\n\n".join(sections), files_loaded


def query_coffee(question: str, drive_root: pathlib.Path) -> str:
    """Answer a coffee roasting question using the local knowledge base.

    Args:
        question: The user's question about coffee roasting.
        drive_root: Path to the Ouroboros Drive root (MyDrive/Ouroboros/).

    Returns:
        A formatted answer string ready for Telegram, or an error message.
    """
    question = (question or "").strip()
    if not question:
        return (
            "❓ Задай вопрос после команды, например:\n\n"
            "• `/coffee как настроить P3 для Brazil natural?`\n"
            "• `/coffee что такое DTR и какой должен быть?`\n"
            "• `/coffee признаки недоразвитой обжарки`\n"
            "• `/coffee Brazil natural на Behmor — с чего начать?`"
        )

    context, files_loaded = _load_coffee_files(drive_root)
    if not context:
        return (
            "⚠️ База знаний по кофе не найдена.\n"
            "Файлы должны быть в `memory/coffee_roasting/*.md` на Drive."
        )

    user_msg = f"Knowledge Base:\n{context}\n\n---\n\nQuestion: {question}"

    try:
        from ouroboros.llm import LLMClient
        client = LLMClient()
        resp_msg, usage = client.chat(
            messages=[
                {"role": "system", "content": COFFEE_RAG_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            model=COFFEE_RAG_MODEL,
            max_tokens=1500,
        )
        answer = (resp_msg.get("content") or "").strip()
        if not answer:
            return "⚠️ Пустой ответ от LLM, попробуй ещё раз."

        cost = float((usage or {}).get("cost") or 0.0)
        sources = ", ".join(files_loaded)
        cost_str = f"${cost:.4f}" if cost > 0 else "free"
        footer = f"\n\n_📚 {sources} | {cost_str}_"
        return answer + footer

    except Exception as e:
        log.error("Coffee RAG query failed: %s", e, exc_info=True)
        return f"❌ Ошибка запроса: {e}"
