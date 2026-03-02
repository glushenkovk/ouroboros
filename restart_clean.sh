#!/bin/bash
# Clean restart script — launches Ouroboros outside Claude Code session

set -e

echo "🔄 Ouroboros Clean Restart"
echo "=========================="

# Store repo path
REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$REPO_DIR"

# Check if agent is already running
AGENT_PID=$(pgrep -f "python.*colab_launcher.py" || true)
if [ -n "$AGENT_PID" ]; then
    echo "⚠️  Agent already running (PID: $AGENT_PID)"
    read -p "Stop it and restart? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Stopping PID $AGENT_PID..."
        kill -TERM "$AGENT_PID" || true
        sleep 2
    else
        echo "Aborted."
        exit 1
    fi
fi

# Unset Claude Code env vars
unset CLAUDECODE
unset CLAUDE_SESSION_ID
unset CLAUDE_AGENT_MODE

# Source .env if exists (for API keys, etc)
if [ -f "$REPO_DIR/.env" ]; then
    echo "📄 Loading .env..."
    set -a
    source "$REPO_DIR/.env"
    set +a
fi

# Launch in background with logging
LOG_DIR="$HOME/ouroboros_data/logs"
mkdir -p "$LOG_DIR"

echo "🚀 Starting agent..."
echo "   Repo: $REPO_DIR"
echo "   Logs: $LOG_DIR/agent_stdout.log"
echo ""

nohup python3 "$REPO_DIR/colab_launcher.py" \
    > "$LOG_DIR/agent_stdout.log" 2>&1 &

AGENT_PID=$!
echo "✅ Started (PID: $AGENT_PID)"
echo ""
echo "📊 Monitor with:"
echo "   tail -f $LOG_DIR/agent_stdout.log"
echo "   tail -f $LOG_DIR/supervisor.jsonl"
echo ""
echo "🛑 Stop with:"
echo "   kill $AGENT_PID"
