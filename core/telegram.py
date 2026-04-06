"""
텔레그램 알림 + 챗봇 통합 모듈
Stock_bot의 telegram_bot.py + briefing.py send_telegram() 이전
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import requests

from config.settings import (
    GEMINI_API_KEY,
    KRW_TICKERS,
    KST,
    PORTFOLIO,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from core.market import signal_badge
from core.models import BriefingResult

log = logging.getLogger(__name__)

# ─── 브리핑 알림 전송 ───────────────────────────────────
def send_briefing_telegram(
    result: BriefingResult,
    notion_page_id: str,
    briefing_type: str = "MANUAL",
) -> bool:
    """브리핑 결과를 텔레그램으로 전송.

    Returns:
        성공 여부
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("텔레그램 설정 없음 — 건너뜀")
        return False

    from core.notion import LABEL_MAP
    label = LABEL_MAP.get(briefing_type, "📊 수시 브리핑")

    raw = result.raw_json
    title = result.title or datetime.now(KST).strftime("%Y.%m.%d %H:%M 브리핑")
    notion_url = f"https://notion.so/{notion_page_id.replace('-', '')}"

    # 매수 전략
    buy_lines: list[str] = []
    for sig in result.buy_signals:
        line = f"{sig.urgency} {sig.name}"
        if sig.shares:
            line += f" [{sig.shares}]"
        line += f"\n▸ {sig.entry_price} → {sig.target_price} ✂ {sig.stop_loss}"
        buy_lines.append(line)

    # 매도 전략
    sell_lines: list[str] = []
    for sig in result.sell_signals:
        line = f"{sig.urgency} {sig.name}"
        if sig.shares:
            line += f" [{sig.shares}]"
        line += f"\n▸ 익절 {sig.target_price} ✂ {sig.stop_loss}"
        sell_lines.append(line)

    # 메시지 조립
    msg = f"📊 {label}\n{title}\n\n"
    if result.advisor_oneliner:
        msg += f"💬 {result.advisor_oneliner}\n\n"
    if buy_lines:
        msg += "🟢 매수\n" + "\n".join(buy_lines) + "\n\n"
    if sell_lines:
        msg += "🔴 매도\n" + "\n".join(sell_lines) + "\n\n"

    next_action = raw.get("next_action", "")
    msg += f"🎯 AI: {result.advisor_verdict}\n▶ {next_action}\n\n"
    msg += f"📋 [Notion 상세보기]({notion_url})"

    return _send_message(msg)


def send_simple_message(text: str) -> bool:
    """단순 텍스트 메시지 전송."""
    return _send_message(text)


def _send_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=30)
        if res.status_code == 200:
            log.info("텔레그램 전송 완료")
            return True
        log.warning(f"텔레그램 전송 실패: {res.status_code} {res.text[:200]}")
        return False
    except Exception as e:
        log.error(f"텔레그램 전송 오류: {e}")
        return False


# ═══════════════════════════════════════════════════════
# 텔레그램 챗봇 (Gemini 기반)
# ═══════════════════════════════════════════════════════
try:
    from google import genai
    from telegram import Update
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    import yfinance as yf
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    _CHATBOT_AVAILABLE = True
except ImportError:
    _CHATBOT_AVAILABLE = False

# 시세 캐시
_market_cache: dict = {"data": "", "ts": 0}
_CACHE_TTL = 60

INDICES = {"^KS11": "KOSPI", "^KQ11": "KOSDAQ", "^GSPC": "S&P500", "^IXIC": "NASDAQ"}

SYSTEM_PROMPT = """당신은 '산적주식비서'. 반말로 대화. 오늘은 2026년입니다.

## 가장 중요한 규칙: 모르는 건 지어내지 마
- 아래에 제공되는 "실시간 시장 데이터"만 사실로 취급해
- 뉴스, 이벤트, 공시 정보는 데이터에 포함된 것만 언급해
- 데이터에 없는 주가, 이벤트, 실적을 절대 지어내지 마
- 확인 안 된 정보는 "확인이 필요하다" 또는 "최신 뉴스를 직접 체크해봐"라고 해

## 역할
- 실시간 데이터 기반 투자 조언
- 리스크를 먼저 언급, 과장 없이
- 매수/매도 추천 시: 진입가, 목표가, 손절가 포함

## 투자자 프로필
- 투자 성향: 중립 (성장주 + 배당주 혼합)
- 관심: 반도체, 방산, AI, 글로벌 ETF

## 답변 스타일
- 텔레그램 대화답게 간결하게. 마크다운 헤더(###) 쓰지 마
- 리포트 형식 금지. 친구에게 투자 조언하듯이
- 아부 금지, 팩트 기반 직언"""


def _get_market_snapshot() -> str:
    """포트폴리오 시세 + 지수 + 뉴스 텍스트 (60초 캐시)."""
    now = time.time()
    if now - _market_cache["ts"] < _CACHE_TTL and _market_cache["data"]:
        return _market_cache["data"]

    lines: list[str] = []
    try:
        lines.append("【시장 지수】")
        for tk, nm in INDICES.items():
            try:
                h = yf.Ticker(tk).history(period="2d")
                if len(h) >= 2:
                    c = float(h["Close"].iloc[-1])
                    p = float(h["Close"].iloc[-2])
                    pct = (c - p) / p * 100
                    lines.append(f"  {nm}: {c:,.0f} ({pct:+.2f}%)")
            except Exception:
                pass

        lines.append("\n【포트폴리오 현재가】")
        for tk, nm in PORTFOLIO.items():
            try:
                h = yf.Ticker(tk).history(period="2d")
                if len(h) >= 2:
                    c = float(h["Close"].iloc[-1])
                    p = float(h["Close"].iloc[-2])
                    pct = (c - p) / p * 100
                    sym = "₩" if tk in KRW_TICKERS else "$"
                    lines.append(f"  {nm}: {sym}{c:,.0f} ({pct:+.2f}%)")
            except Exception:
                pass
            time.sleep(0.1)

        lines.append("\n【최신 주요 뉴스】")
        with DDGS() as d:
            for q in ["stock market today", "코스피 증시", "NVDA nvidia"][:3]:
                try:
                    for r in d.news(q, max_results=2, timelimit="d"):
                        title = r.get("title", "")
                        if title:
                            lines.append(f"  • {title}")
                except Exception:
                    pass
                time.sleep(0.5)
    except Exception as e:
        lines.append(f"  (데이터 조회 오류: {e})")

    snapshot = "\n".join(lines)
    _market_cache["data"] = snapshot
    _market_cache["ts"] = now
    return snapshot


def _extract_trade(response_text: str) -> dict | None:
    """AI 응답에서 매매 추천 파싱."""
    patterns = [
        r"(매수|매도|진입|손절|청산|buy|sell)\s*[:\-]?\s*([A-Z가-힣\w]+)\s*[/\s@]*\s*([\$₩\d,\.]+)",
        r"(매수|매도)\s+추천\s*[:\-]?\s*([A-Z가-힣\w]+)",
    ]
    for pat in patterns:
        m = re.search(pat, response_text, re.IGNORECASE)
        if m:
            groups = m.groups()
            return {
                "action": groups[0],
                "ticker": groups[1],
                "price": groups[2] if len(groups) > 2 else "",
                "reason": response_text[:100],
            }
    return None


def run_chatbot() -> None:
    """텔레그램 챗봇 시작 (블로킹).

    Raises:
        ImportError: python-telegram-bot 미설치
    """
    if not _CHATBOT_AVAILABLE:
        raise ImportError(
            "챗봇 실행에 필요한 패키지를 설치하세요: "
            "pip install python-telegram-bot google-genai duckduckgo-search"
        )

    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN 환경변수가 설정되지 않았습니다.")

    from db.chat_store import init_chat_db, save_chat_message, get_recent_chat, clear_chat, save_trade_note, format_trade_notes

    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    allowed_chat_id = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else 0

    def is_authorized(chat_id: int) -> bool:
        return allowed_chat_id == 0 or chat_id == allowed_chat_id

    def ask_gemini(chat_id: int, user_message: str) -> str:
        save_chat_message(chat_id, "user", user_message)
        history = get_recent_chat(chat_id, limit=20)

        invest_keywords = [
            "주식", "매수", "매도", "종목", "주가", "시장", "증시", "코스피",
            "나스닥", "환율", "금리", "ETF", "포트폴리오", "배당", "실적",
            "삼성", "엔비디아", "NVDA", "마이크론", "한화", "록히드", "구글",
            "stock", "buy", "sell", "market", "반도체", "AI", "VIX",
            "전략", "리스크", "손절", "목표가", "진입", "분할", "수익률",
        ]
        is_invest = any(kw in user_message for kw in invest_keywords)

        market_data = ""
        if is_invest:
            market_data = f"\n\n━━━ 실시간 시장 데이터 ━━━\n{_get_market_snapshot()}\n━━━━━━━━━━━━━━━━━━━━━"

        trade_ctx = format_trade_notes(chat_id)
        if trade_ctx:
            market_data += f"\n\n{trade_ctx}"

        context = "\n".join(history)
        prompt = f"{SYSTEM_PROMPT}{market_data}\n\n대화 기록:\n{context}\n\n위 대화의 마지막 사용자 메시지에 답변해주세요."

        try:
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt,
            )
            reply = response.text.strip()
            if not reply:
                return "응답을 받지 못했습니다. 다시 시도해주세요."

            save_chat_message(chat_id, "ai", reply)

            trade = _extract_trade(reply)
            if trade:
                save_trade_note(chat_id, trade["ticker"], trade["action"], trade["price"], trade["reason"])

            return reply
        except Exception as e:
            log.error(f"Gemini API 오류: {e}")
            return f"오류가 발생했습니다: {e}"

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update.effective_chat.id):
            return
        await update.message.reply_text(
            "📊 산적주식비서입니다!\n\n"
            "실시간 시세 + 전문 분석 기반으로 대화합니다.\n\n"
            "/clear — 대화 기록 초기화\n"
            "/market — 현재 시세 요약"
        )

    async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update.effective_chat.id):
            return
        clear_chat(update.effective_chat.id)
        _market_cache["ts"] = 0
        await update.message.reply_text("대화 기록이 초기화되었습니다.")

    async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update.effective_chat.id):
            return
        await update.message.chat.send_action("typing")
        _market_cache["ts"] = 0
        snapshot = _get_market_snapshot()
        now = datetime.now(KST).strftime("%H:%M KST")
        await update.message.reply_text(f"📊 시세 현황 ({now})\n\n{snapshot}")

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        if not is_authorized(update.effective_chat.id):
            return
        chat_id = update.effective_chat.id
        await update.message.chat.send_action("typing")
        reply = ask_gemini(chat_id, update.message.text)
        if len(reply) > 4000:
            for i in range(0, len(reply), 4000):
                await update.message.reply_text(reply[i:i + 4000])
        else:
            await update.message.reply_text(reply)

    init_chat_db()
    log.info("📊 산적주식비서 챗봇 시작")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)
