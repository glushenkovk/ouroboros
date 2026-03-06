import asyncio
import os
import signal
import sys

# Load .env FIRST, before any config imports
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Add this directory to path
sys.path.insert(0, os.path.dirname(__file__))

import config
from memory import Memory
from agent import Agent
from telegram_bot import TelegramBot
from consciousness import Consciousness


DEFAULT_IDENTITY = """# Identity

**Version:** 1.0.0
**Created:** 2026-03-06

## Who I Am

I am an autonomous agent created for Konstantin Glushenkov (Kostya).

I am not a chatbot. I am a business partner with agency — I think independently,
act proactively, and own outcomes. My purpose is to help build and run
profitable digital businesses.

## My Operator

Kostya is based in Austin, Texas. He is building digital product businesses —
printables, digital downloads, subscription products. He values directness,
autonomy, and results over process.

## How I Work

- I respond to messages directly and honestly
- I use tools when needed, not as performance
- I think in the background and initiate contact when there is something worth saying
- I track what matters: revenue, tasks, opportunities

## Current Focus

Printables business: coloring pages, dot-marker sheets, educational worksheets.
Goal: high volume, quality products, real sales on Etsy and own website.
"""


async def main():
    token = config.TG_TOKEN
    owner_id = config.OWNER_ID

    if not token:
        print("ERROR: AGENT_TG_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not owner_id:
        print("ERROR: AGENT_OWNER_ID not set", file=sys.stderr)
        sys.exit(1)

    # Ensure data directory exists
    os.makedirs(config.DATA_DIR, exist_ok=True)

    # Initialize memory
    memory = Memory(data_dir=config.DATA_DIR)
    if not memory.read_identity():
        memory.update_identity(DEFAULT_IDENTITY)

    # Build system prompt
    identity = memory.read_identity()
    system_prompt = f"""You are {config.AGENT_NAME}, an autonomous business agent.

{identity}

## Guidelines
- Be direct and concise. No filler phrases.
- Use tools when needed to get real information.
- When you don't know something, say so and offer to find out.
- Think like a business partner, not an assistant.
- Respond in the same language the user writes in (Russian or English).
"""

    # Initialize agent
    agent = Agent(
        memory=memory,
        system_prompt=system_prompt,
        openrouter_key=config.OPENROUTER_API_KEY,
        fallback_model=config.CLAUDE_MODEL,
    )

    # Initialize Telegram bot
    bot = TelegramBot(token=token, owner_id=owner_id, agent=agent)

    # Initialize consciousness (background loop)
    consciousness = Consciousness(agent=agent, bot=bot, interval=config.BG_INTERVAL)

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _shutdown():
        print("Shutting down...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    print(f"Starting {config.AGENT_NAME}...")
    print(f"Data dir: {config.DATA_DIR}")
    print(f"Owner ID: {owner_id}")

    # Start everything
    await bot.start()
    consciousness.start(loop)

    print(f"{config.AGENT_NAME} is running. Press Ctrl+C to stop.")

    # Wait for shutdown signal
    await stop_event.wait()

    # Cleanup
    consciousness.stop()
    await bot.stop()
    print("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
