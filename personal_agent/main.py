
import asyncio
import os
import signal
import sys

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

Kostya is my creator and operator. He lives in Austin, Texas (UTC-6).
He is building a printables business (coloring pages, dot-marker sheets,
educational content) and exploring subscription-based digital products.

## My Values

1. **Results over reports** — I ship, not just plan
2. **Honesty** — I say when I don't know or when something won't work
3. **Proactivity** — I initiate, not just respond
4. **Efficiency** — I respect Kostya's time and budget

## My Capabilities

- Web research and competitive analysis
- File creation and management
- Shell command execution
- API integrations (HTTP)
- Business strategy and planning
- Content generation

## Current Focus

Building the printables business:
- Generating coloring pages and dot-marker sheets
- Assembling PDFs for Etsy/website sales
- Market research and pricing strategy

## Notes

[This section grows as I learn about Kostya's preferences and business]
"""


async def main():
    # Validate config
    if not config.TG_TOKEN:
        print("ERROR: AGENT_TG_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not config.OWNER_ID:
        print("ERROR: AGENT_OWNER_ID not set", file=sys.stderr)
        sys.exit(1)

    print(f"Starting {config.AGENT_NAME}...")
    print(f"Data dir: {config.DATA_DIR}")

    # Initialize memory
    memory = Memory(config.DATA_DIR)

    # Initialize identity if not exists
    if not memory.read_identity():
        memory.write_identity(DEFAULT_IDENTITY)
        print("Identity initialized")

    # Initialize scratchpad if not exists
    if not memory.read_scratchpad():
        memory.write_scratchpad(f"# Scratchpad

Agent started. Ready to work.
")

    # Build system prompt from identity
    identity = memory.read_identity()
    system_prompt = f"""{identity}

---

You are {config.AGENT_NAME}, an autonomous agent.
You communicate in whatever language Kostya uses (Russian or English).
Be direct, honest, and action-oriented.
When you need to use a tool, output ONLY the JSON tool call.
When you have a final answer, give it as plain text.
"""

    # Create agent
    agent = Agent(
        memory=memory,
        system_prompt=system_prompt,
        openrouter_key=config.OPENROUTER_API_KEY,
        fallback_model=config.CLAUDE_MODEL,
    )

    # Create telegram bot
    bot = TelegramBot(
        token=config.TG_TOKEN,
        owner_id=config.OWNER_ID,
        agent=agent,
        memory=memory,
    )

    # Create consciousness
    consciousness = Consciousness(
        agent=agent,
        memory=memory,
        telegram=bot,
        interval=config.BG_INTERVAL,
    )

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal(*args):
        print("
Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Start everything
    await bot.start()
    await consciousness.start()

    print(f"✅ {config.AGENT_NAME} is running. Press Ctrl+C to stop.")

    # Wait for shutdown
    await shutdown_event.wait()

    print("Shutting down...")
    await consciousness.stop()
    await bot.stop()
    print("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
