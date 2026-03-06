"""Telegram bot interface for personal agent."""

import asyncio
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, token: str, owner_id: int, agent):
        self.token = token
        self.owner_id = owner_id
        self.agent = agent
        self.app = Application.builder().token(token).build()
        self._setup_handlers()

    def _setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("memory", self._cmd_memory))
        self.app.add_handler(CommandHandler("identity", self._cmd_identity))
        self.app.add_handler(CommandHandler("clear", self._cmd_clear))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))

    def _check_owner(self, update: Update) -> bool:
        return update.effective_user.id == self.owner_id

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_owner(update):
            return
        text = (
            "Da, ya zdes'. Avtonomny agent, gotov k rabote.\n\n"
            "/memory - rabochaya pamyat'\n"
            "/identity - kto ya\n"
            "/clear - ochistit' istoriyu chata"
        )
        await update.message.reply_text(text)

    async def _cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_owner(update):
            return
        mem = self.agent.memory.read_scratchpad() or "(pusto)"
        await update.message.reply_text(f"Scratchpad:\n\n{mem[:3000]}")

    async def _cmd_identity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_owner(update):
            return
        identity = self.agent.memory.read_identity() or "(pusto)"
        await update.message.reply_text(f"Identity:\n\n{identity[:3000]}")

    async def _cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_owner(update):
            return
        self.agent.memory.clear_history()
        await update.message.reply_text("Istoriya ochishchena.")

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_owner(update):
            return
        user_text = update.message.text
        logger.info(f"Message from owner: {user_text[:100]}")

        # Show typing indicator
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # Process through agent
        response = await self.agent.handle_message(user_text)

        # Split long responses
        if len(response) > 4000:
            parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(response)

    async def send_message(self, text: str):
        """Send a message to owner proactively."""
        try:
            await self.app.bot.send_message(chat_id=self.owner_id, text=text[:4000])
        except Exception as e:
            logger.error(f"Failed to send message: {e}")

    async def start(self):
        """Start polling in background."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started")

    async def stop(self):
        """Stop the bot."""
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
