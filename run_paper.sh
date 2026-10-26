#!/usr/bin/env bash
# Push350 bot paper-mode launcher.
#
# Forces paper mode (PUSH350_FORCE_PAPER=1) even if config.yaml was edited
# to live, so this script can never place an order.
#
# Credentials: set TOPSTEPX_USERNAME / TOPSTEPX_API_KEY / TOPSTEPX_ACCOUNT_ID
# in your environment before running. This script does NOT read
# ~/workspace/trading-bot/.env and never prints credentials.
#
# Usage: ./run_paper.sh        # starts bot.py detached in paper mode
set -euo pipefail
cd "$(dirname "$0")"

export PUSH350_FORCE_PAPER=1

[ -n "${TOPSTEPX_USERNAME:-}" ] && [ -n "${TOPSTEPX_API_KEY:-}" ] \
  || { echo "Set TOPSTEPX_USERNAME and TOPSTEPX_API_KEY in the environment first." >&2; exit 1; }

pip install -q -r requirements.txt

LOG="paper-$(date +%F).log"
nohup python3 bot.py > "$LOG" 2>&1 &
echo "Push350 paper bot started, PID $!. Log: $LOG"
echo "Paper mode: connects, reads live 1-min bars, logs signals, places ZERO orders."
echo "Offline check instead (no auth, no orders): python3 bot.py --smoke"
