#!/bin/bash
# 주가 업데이트 cron 래퍼
REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"
export $(grep -v '^#' "$REPO_DIR/.env" | xargs)
cd "$REPO_DIR"
"$REPO_DIR/venv/bin/python" -u main.py price
