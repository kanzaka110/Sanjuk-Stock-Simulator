#!/bin/bash
# 봇 + 모니터 수동 실행 래퍼
REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"
export $(grep -v '^#' "$REPO_DIR/.env" | xargs)
cd "$REPO_DIR"
"$REPO_DIR/venv/bin/python" -u main.py bot
