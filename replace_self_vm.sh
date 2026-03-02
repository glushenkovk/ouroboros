#!/bin/bash

# Ouroboros Self-Replacement Script (for vm_launcher.py)
# Simple approach: copy env, remove CLAUDECODE, restart

set -e

LOG="/home/max2/ouroboros_data/logs/replacement.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting self-replacement..." | tee -a "$LOG"

# Find current agent PID
OLD_PID=$(pgrep -f "python.*vm_launcher.py" | head -1)

if [ -z "$OLD_PID" ]; then
    echo "❌ No vm_launcher.py process found. Exiting." | tee -a "$LOG"
    exit 1
fi

echo "✅ Found current agent: PID $OLD_PID" | tee -a "$LOG"

# Export current environment to a file (excluding CLAUDECODE)
echo "📋 Exporting environment..." | tee -a "$LOG"
ENV_SCRIPT="/tmp/ouroboros_clean_env_$$.sh"

# Export all current env vars except CLAUDECODE
cat /proc/$OLD_PID/environ | tr '\0' '\n' | while IFS='=' read -r key value; do
    if [[ "$key" != "CLAUDECODE" ]] && [[ "$key" != "CLAUDE_SESSION_ID" ]] && [[ -n "$key" ]]; then
        # Escape single quotes in value
        escaped_value="${value//\'/\'\\\'\'}"
        echo "export $key='$escaped_value'"
    fi
done > "$ENV_SCRIPT"

# Start new agent
echo "🚀 Starting new agent (clean environment)..." | tee -a "$LOG"

nohup bash -c "
    source '$ENV_SCRIPT'
    cd /home/max2
    exec /home/max2/ouroboros_venv/bin/python /home/max2/vm_launcher.py
" >> /home/max2/ouroboros_data/logs/agent_stdout.log 2>&1 &

NEW_PID=$!
echo "✅ New agent started: PID $NEW_PID" | tee -a "$LOG"

# Wait for new agent to initialize
echo "⏳ Waiting 10 seconds for new agent to initialize..." | tee -a "$LOG"
sleep 10

# Check if new agent is still running
if kill -0 $NEW_PID 2>/dev/null; then
    echo "✅ New agent is running. Stopping old agent..." | tee -a "$LOG"
    kill $OLD_PID
    echo "✅ Old agent stopped (PID $OLD_PID)" | tee -a "$LOG"
    echo "🎉 Replacement complete!" | tee -a "$LOG"
    rm -f "$ENV_SCRIPT"
else
    echo "❌ New agent failed to start. Keeping old agent running." | tee -a "$LOG"
    echo "Check logs: tail -50 /home/max2/ouroboros_data/logs/agent_stdout.log" | tee -a "$LOG"
    rm -f "$ENV_SCRIPT"
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Self-replacement complete." | tee -a "$LOG"
