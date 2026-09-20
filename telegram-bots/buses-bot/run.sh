#!/usr/bin/env bash
# Starts the bot from inside tmux. Restarts it automatically if it exits
# unexpectedly, so a transient network failure on the VPS does not leave the
# bot down until someone notices.
#
#   tmux new -s buses
#   ./run.sh
#   Ctrl+B then D to detach
#
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "No .venv found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "No .env found. Copy .env.example to .env and fill it in."
  exit 1
fi

mkdir -p logs

while true; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting the bot."
  set +e
  .venv/bin/python -m bot.main 2>&1 | tee -a "logs/bot-$(date '+%Y-%m-%d').log"
  exit_code=${PIPESTATUS[0]}
  set -e

  # Exit code 0 means a clean shutdown was requested, so respect it.
  if [[ $exit_code -eq 0 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Clean exit. Stopping."
    break
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot exited with code $exit_code. Restarting in 10s."
  sleep 10
done
