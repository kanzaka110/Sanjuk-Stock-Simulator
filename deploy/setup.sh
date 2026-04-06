#!/bin/bash
# ═══════════════════════════════════════════════════════
# 산적 주식 시뮬레이터 — GCP 초기 설정
# ═══════════════════════════════════════════════════════
set -e

REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"
VENV_DIR="$REPO_DIR/venv"

echo ""
echo "================================================"
echo "  산적 주식 시뮬레이터 — GCP 설정"
echo "================================================"
echo ""

# 1. venv 생성
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/5] 가상환경 생성..."
    python3 -m venv "$VENV_DIR"
else
    echo "[1/5] 가상환경 존재 확인"
fi

# 2. 패키지 설치
echo "[2/5] 패키지 설치..."
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r "$REPO_DIR/requirements.txt"

# 3. .env 확인
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "[3/5] .env 파일 생성..."
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    echo "  >> .env 파일을 편집하여 API 키를 설정하세요:"
    echo "     nano $REPO_DIR/.env"
else
    echo "[3/5] .env 파일 존재 확인"
fi

# 4. DB 디렉토리 생성
echo "[4/5] 데이터 디렉토리 생성..."
mkdir -p "$REPO_DIR/db/data"

# 5. systemd 서비스 설치
echo "[5/5] systemd 서비스 설치..."

# 챗봇 서비스
sudo cp "$REPO_DIR/deploy/stock-chatbot.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable stock-chatbot

# cron 래퍼 설치
mkdir -p "$HOME/logs/briefing"
cp "$REPO_DIR/deploy/run_briefing.sh" "$HOME/run_stock_briefing.sh"
cp "$REPO_DIR/deploy/run_price_update.sh" "$HOME/run_stock_price.sh"
chmod +x "$HOME/run_stock_briefing.sh" "$HOME/run_stock_price.sh"

echo ""
echo "================================================"
echo "  설정 완료!"
echo "================================================"
echo ""
echo "  다음 단계:"
echo "  1. .env 편집:  nano $REPO_DIR/.env"
echo "  2. 챗봇 시작:  sudo systemctl start stock-chatbot"
echo "  3. 브리핑 cron: crontab -e 후 아래 추가"
echo ""
echo "  # 주식 브리핑 (국내장 전 8:00, 미국장 전 22:00)"
echo "  0 8 * * 1-5  ~/run_stock_briefing.sh KR_BEFORE >> ~/logs/briefing/kr.log 2>&1"
echo "  0 22 * * 1-5 ~/run_stock_briefing.sh US_BEFORE >> ~/logs/briefing/us.log 2>&1"
echo ""
echo "  # 주가 업데이트 (매시간)"
echo "  0 * * * * ~/run_stock_price.sh >> ~/logs/briefing/price.log 2>&1"
echo ""
echo "  TUI 실행: cd $REPO_DIR && source venv/bin/activate && python main.py"
echo ""
