
import asyncio
import sys
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from agent import Agent
from memory import Memory


class TelegramBot:
    def __init__(self, token: str, owner_id: int, agent: Agent, memory: Memory):
        self.token = token
        self.owner_id = owner_id
        self.agent = agent
        self.memory = memory
        self.app = None
        self._app_instance = None

    async def _check_owner(self, update: Update) -> bool:
        """Return True if message is from owner."""
        if update.effective_user and update.effective_user.id == self.owner_id:
            return True
        return False

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_owner(update):
            return
        identity = self.memory.read_identity()
        name = identity.split('\n')[0].replace('# ', '').replace('Identity', '').strip()
        await update.message.reply_text(
            f"Да, я здесь. Автономный агент, готов к работе.

/memory - рабочая память
/identity - кто я
/clear - очистить историю"
        )

    async def _cmd_memory(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_owner(update):
            return
        content = self.memory.read_scratchpad() or "(empty)"
        await self._send_long(update, f"📝 Scratchpad:

{content}")

    async def _cmd_identity(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_owner(update):
            return
        content = self.memory.read_identity() or "(empty)"
        await self._send_long(update, f"🧠 Identity:

{content}")

    async def _cmd_clear(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_owner(update):
            return
        self.memory.clear_chat_history()
        await update.message.reply_text("✅ History cleared")

    async def _handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._check_owner(update):
            return
        
        user_text = update.message.text or ""
        if not user_text:
            return

        # Typing indicator
        await ctx.bot.send_chat_action(self.owner_id, "typing")

        # Save user message
        self.memory.add_chat_message("user", user_text)

        # Get response
        try:
            response = await self.agent.respond(user_text)
        except Exception as e:
            response = f"❌ Error: {e}"
        
        # Save assistant response
        self.memory.add_chat_message("assistant", response)

        # Send response
        await self._send_long(update, response)

    async def _send_long(self, update: Update, text: str):
        """Send text, splitting if > 4000 chars."""
        MAX = 4000
        for i in range(0, len(text), MAX):
            await update.message.reply_text(text[i:i+MAX])

    async def send_message(self, text: str):
        """Send a message to owner proactively."""
        if self._app_instance is None:
            return
        MAX = 4000
        for i in range(0, len(text), MAX):
            await self._app_instance.bot.send_message(
                chat_id=self.owner_id,
                text=text[i:i+MAX]
            )

    async def start(self):
        """Start the bot (blocking polling)."""
        self._app_instance = (
            Application.builder()
            .token(self.token)
            .build()
        )
        app = self._app_instance
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("memory", self._cmd_memory))
        app.add_handler(CommandHandler("identity", self._cmd_identity))
        app.add_handler(CommandHandler("clear", self._cmd_clear))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        
        print("Telegram bot starting...")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        print("Bot polling started")

    async def stop(self):
        if self._app_instance:
            await self._app_instance.updater.stop()
            await self._app_instance.stop()
            await self._app_instance.shutdown()
