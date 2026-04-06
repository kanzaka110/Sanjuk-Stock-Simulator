#!/bin/bash
# GCP 초기 설정 스크립트
set -e

echo "📦 산적 주식 시뮬레이터 — GCP 설정"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"

# 1. venv 생성
if [ ! -d "$REPO_DIR/venv" ]; then
    echo "🐍 가상환경 생성..."
    python3 -m venv "$REPO_DIR/venv"
fi

# 2. 패키지 설치
echo "📦 패키지 설치..."
source "$REPO_DIR/venv/bin/activate"
pip install -r "$REPO_DIR/requirements.txt"

# 3. .env 확인
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "⚠️  .env 파일이 없습니다. .env.example을 복사합니다."
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    echo "   → .env 파일을 편집하여 API 키를 설정하세요"
fi

# 4. DB 디렉토리 생성
mkdir -p "$REPO_DIR/db/data"

echo ""
echo "✅ 설정 완료!"
echo "   실행: cd $REPO_DIR && source venv/bin/activate && python main.py"
