
import asyncio
import sys
from datetime import datetime, timezone

from agent import Agent
from memory import Memory


class Consciousness:
    def __init__(self, agent: Agent, memory: Memory, telegram, interval: int = 900):
        self.agent = agent
        self.memory = memory
        self.telegram = telegram
        self.interval = interval
        self._running = False
        self._task = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())
        print(f"Consciousness started (interval={self.interval}s)")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                if self._running:
                    await self._wakeup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Consciousness error: {e}", file=sys.stderr)
                await asyncio.sleep(60)

    async def _wakeup(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"Consciousness wakeup at {now}")

        scratchpad = self.memory.read_scratchpad() or "(empty)"
        prompt = f"""Background consciousness wakeup. Time: {now}.

Your scratchpad:
{scratchpad}

Review your memory and current situation.
Is there anything important to:
1. Think about or plan?
2. Act on right now?
3. Communicate to Kostya?

If you want to send Kostya a message, start your response with [MESSAGE TO OWNER]:
Otherwise, just update your scratchpad with current thoughts and timestamp.
Keep it brief."""

        try:
            response = await self.agent.respond(prompt)
            
            if "[MESSAGE TO OWNER]:" in response:
                # Extract message after the tag
                parts = response.split("[MESSAGE TO OWNER]:", 1)
                message = parts[1].strip() if len(parts) > 1 else response
                await self.telegram.send_message(f"🧠 {message}")
                print(f"Consciousness sent message to owner")
            else:
                print(f"Consciousness silent wakeup done")
        except Exception as e:
            print(f"Consciousness wakeup error: {e}", file=sys.stderr)
