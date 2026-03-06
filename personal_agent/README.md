# Personal Autonomous Agent

A lightweight autonomous agent built from scratch for Konstantin Glushenkov.

## Features

- 🧠 **LLM brain** — Claude CLI (free via Max subscription) + OpenRouter fallback
- 💬 **Telegram** — communicates via Telegram bot
- 🔄 **Background consciousness** — wakes periodically, reflects, messages you if needed
- 💾 **Memory** — scratchpad (working memory) + identity + chat history
- 🛠️ **Tools** — web search, file I/O, shell, HTTP calls

## Quick Start

### 1. Create a Telegram bot

Open @BotFather in Telegram, send `/newbot`, follow instructions.
You get a token like `7123456789:ABCDxxx...`

To get your Telegram user ID, message @userinfobot.

### 2. Set environment variables

```bash
export AGENT_TG_TOKEN="your_bot_token_here"
export AGENT_OWNER_ID="your_telegram_user_id"
export AGENT_NAME="MyAgent"  # optional
export AGENT_DATA_DIR="/home/max2/agent_data"  # where to store memory
export OPENROUTER_API_KEY="sk-or-..."  # optional, for LLM fallback
export AGENT_BG_INTERVAL="900"  # background wakeup every 15 min
```

### 3. Run

```bash
cd /home/max2/ouroboros_repo/personal_agent
python3 main.py
```

### Run as background service

```bash
nohup python3 main.py > /home/max2/agent_data/agent.log 2>&1 &
echo $! > /home/max2/agent_data/agent.pid
```

To stop:
```bash
kill $(cat /home/max2/agent_data/agent.pid)
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Wake up the agent |
| `/memory` | Show current scratchpad |
| `/identity` | Show agent identity |
| `/clear` | Clear chat history |

Any other message is processed by the LLM.

## Architecture

```
main.py           — entry point, starts everything
agent.py          — LLM loop (Claude CLI → OpenRouter fallback)
telegram_bot.py   — Telegram I/O
consciousness.py  — background wakeup loop
memory.py         — persistent memory (files)
tools.py          — tool definitions and execution
config.py         — environment variables
```

Data files (in `AGENT_DATA_DIR`):
```
scratchpad.md   — working memory (agent reads/writes freely)
identity.md     — who the agent is (personality, goals)
chat.jsonl      — conversation history
```

## Dependencies

```
python-telegram-bot>=20.0
httpx>=0.25.0
```

Install:
```bash
pip install python-telegram-bot httpx
```

## Customization

- Edit `agent_data/identity.md` to change agent personality and goals
- Edit `agent_data/scratchpad.md` to give the agent context
- Add tools in `tools.py` — just add to `TOOL_DEFINITIONS` and `execute_tool()`
