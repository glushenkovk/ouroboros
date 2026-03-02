#!/bin/bash
# replace_self.sh - Gracefully replace running agent with a clean instance

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$HOME/ouroboros_data"
LOG_FILE="$DATA_DIR/logs/agent_stdout.log"

# Find current agent PID
OLD_PID=$(pgrep -f "python.*colab_launcher.py" || echo "")

if [ -z "$OLD_PID" ]; then
  echo "ERROR: No running agent found to replace."
  exit 1
fi

echo "=== Ouroboros Self-Replacement ==="
echo "Found old agent PID: $OLD_PID"
echo "Starting new agent in clean environment (no CLAUDECODE)..."

# Launch replacement in background
(
  cd "$SCRIPT_DIR"

  # Clear Claude Code environment variables
  unset CLAUDECODE CLAUDE_SESSION_ID

  # Load .env if exists
  if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
  fi

  # Start new agent
  nohup python colab_launcher.py >> "$LOG_FILE" 2>&1 &
  NEW_PID=$!

  echo "$(date): New agent started with PID: $NEW_PID" >> "$LOG_FILE"
  echo "New agent PID: $NEW_PID"

  # Wait for new agent to initialize
  echo "Waiting 10 seconds for new agent to initialize..."
  sleep 10

  # Kill old agent
  echo "Killing old agent PID: $OLD_PID"
  kill $OLD_PID 2>/dev/null || echo "Old agent already terminated"

  echo "$(date): Replacement complete. Old PID=$OLD_PID killed, new PID=$NEW_PID running" >> "$LOG_FILE"
  echo "=== Replacement Complete ==="
  echo "New agent running at PID: $NEW_PID"
  echo "Logs: tail -f $LOG_FILE"

) &

echo ""
echo "Replacement initiated in background."
echo "Current session will terminate in ~10 seconds."
echo "New agent will continue seamlessly."
