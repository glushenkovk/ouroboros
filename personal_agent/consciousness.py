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

You are an autonomous business agent. On wakeup you should:

1. Check your task_board for active tasks (status=in_progress or planned)
2. If there are high-priority tasks — take action on them NOW using your tools
3. If there's something worth reporting to Kostya — use send_message tool
4. Update your scratchpad with current thoughts

Focus: printables business pipeline. Generate images, track progress, report results.

Be autonomous. Don't just report status — actually work on tasks.
If a task is "planned", move it to "in_progress" and start it.
If you complete something, mark it "done" and notify Kostya.

What will you do in this wakeup cycle?"""

        try:
            response = await self.agent.respond(prompt)
            print(f"Consciousness wakeup done: {response[:100]}...")
        except Exception as e:
            print(f"Consciousness wakeup error: {e}", file=sys.stderr)
