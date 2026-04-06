#!/bin/bash
# 산적 주식 시뮬레이터 실행 래퍼
cd /home/kanzaka110/Sanjuk-Stock-Simulator
source venv/bin/activate
export $(grep -v '^#' .env | xargs)
python -u main.py
