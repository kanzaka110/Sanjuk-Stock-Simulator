#!/bin/bash
# 브리핑 cron 래퍼
# 사용: ~/run_stock_briefing.sh [KR_BEFORE|US_BEFORE|MANUAL]
REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"
export $(grep -v '^#' "$REPO_DIR/.env" | xargs)
export BRIEFING_TYPE="${1:-MANUAL}"
cd "$REPO_DIR"
"$REPO_DIR/venv/bin/python" -u main.py briefing
