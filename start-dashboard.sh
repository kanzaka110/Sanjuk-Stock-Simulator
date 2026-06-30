#!/bin/bash
# 대시보드 시작 스크립트 — autonomous mode env 포함
# pytest에는 영향 안 줌 (systemd env + 이 스크립트에서만 설정)
cd /home/kanzaka110/Sanjuk-Stock-Simulator

export TOSS_LIVE_PILOT_ENABLED=true
export TOSS_LIVE_ORDER_ALLOWED=true
export TOSS_LIVE_ADAPTER_ENABLED=true
export TOSS_AUTONOMOUS_MODE=true
export TOSS_AUTONOMOUS_KILL_SWITCH=false
export TOSS_AUTONOMOUS_ALLOWED_ASSET_TYPES=US_STOCK,KR_STOCK
export TOSS_AUTONOMOUS_ALLOWED_SIDES=BUY,SELL

exec ./venv/bin/python -u main.py dashboard
