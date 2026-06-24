"""
텔레그램 봇 명령 수신 모듈

getUpdates long-polling으로 사용자 명령을 수신하고 처리한다.
기존 telegram.py(전송 전용)와 분리되어 명령 수신만 담당.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import requests

from config.settings import (
    HOLDINGS_GENERAL,
    HOLDINGS_IRP,
    HOLDINGS_ISA,
    HOLDINGS_PENSION,
    HOLDINGS_RIA,
    ISA_CASH,
    IRP_CASH,
    RIA_CASH,
    IRP_DEFAULT_OPTION,
    KST,
    KRW_TICKERS,
    PENSION_MMF,
    PORTFOLIO,
    DEFAULT_CASH,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)

log = logging.getLogger(__name__)

# ─── 명령어 매핑 ──────────────────────────────────────
COMMANDS: dict[str, str] = {
    "보유종목 확인": "PORTFOLIO",
    "/portfolio": "PORTFOLIO",
    "/status": "PORTFOLIO",
    "/help": "HELP",
    "도움말": "HELP",
}

HELP_TEXT = """📋 *사용 가능한 명령어*

🔹 *보유종목 확인* — 현재 시세 + 수익률
🔹 *매매 [종목] [매수/매도] [N]주 [가격] [계좌]* — 매매 기록
    예: 매매 삼성전자 매수 10주 290000 일반
    (다음 브리핑부터 AI가 미반영 매매를 감안)
🔹 *매매내역* — 미반영 매매 목록
🔹 *매매반영* — settings.py 갱신 후 기록 정리

📊 브리핑은 매일 자동 전송됩니다.
  • 한국장: KST 08:30
  • 미국장: KST 21:00
🚨 긴급 알림은 자동 감지됩니다."""


class TelegramBot:
    """텔레그램 봇 폴링 핸들러."""

    def __init__(self) -> None:
        self._offset: int = 0
        self._running: bool = False
        self._poll_timeout: int = 30
        self._retry_delay: float = 5.0
        self._max_retry_delay: float = 300.0

    def run_polling(self) -> None:
        """메인 폴링 루프. KeyboardInterrupt로 종료."""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            log.error("텔레그램 설정 없음 — 봇 시작 불가")
            return

        self._running = True
        log.info("텔레그램 봇 폴링 시작")
        retry_delay = self._retry_delay

        while self._running:
            try:
                updates = self._get_updates()
                if updates:
                    retry_delay = self._retry_delay  # 성공 시 리셋
                    for update in updates:
                        self._process_update(update)
            except requests.ConnectionError:
                log.warning(f"연결 끊김 — {retry_delay:.0f}초 후 재시도")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, self._max_retry_delay)
            except Exception as e:
                log.error(f"폴링 오류: {e}")
                time.sleep(retry_delay)

    def stop(self) -> None:
        """폴링 종료."""
        self._running = False
        log.info("텔레그램 봇 폴링 종료")

    def _get_updates(self) -> list[dict]:
        """Telegram getUpdates API 호출."""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {
            "offset": self._offset,
            "timeout": self._poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        resp = requests.get(url, params=params, timeout=self._poll_timeout + 10)
        data = resp.json()

        if not data.get("ok"):
            log.warning(f"getUpdates 실패: {data}")
            return []

        results = data.get("result", [])
        if results:
            self._offset = results[-1]["update_id"] + 1
        return results

    def _process_update(self, update: dict) -> None:
        """수신된 업데이트 처리."""
        if "callback_query" in update:
            self._process_callback_query(update["callback_query"])
            return

        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()

        if not text:
            return

        # 인증: 설정된 chat_id만 허용
        if chat_id != TELEGRAM_CHAT_ID:
            log.warning(f"미인증 접근: chat_id={chat_id}")
            return

        log.info(f"명령 수신: {text}")

        # 매매 기록 명령 (prefix 매칭)
        if text.startswith("매매반영"):
            self._handle_trade_apply()
            return
        if text.startswith("매매내역"):
            self._handle_trade_list()
            return
        if text.startswith("매매 "):
            self._handle_trade_record(text)
            return

        action = COMMANDS.get(text)

        if action is None:
            self._reply("❓ 알 수 없는 명령입니다.\n\n" + HELP_TEXT)
            return

        if action == "HELP":
            self._reply(HELP_TEXT)
        elif action == "PORTFOLIO":
            self._handle_portfolio()

    def _handle_trade_record(self, text: str) -> None:
        """매매 기록 입력 처리."""
        from core.trade_log import parse_trade_message, record_trade

        trade = parse_trade_message(text)
        if trade is None:
            self._reply(
                "❌ 형식 오류. 예시:\n"
                "매매 삼성전자 매수 10주 290000 일반\n"
                "매매 005930 매도 5주 295000 ISA\n"
                "매매 MU 매도 3주 1080"
            )
            return
        tid = record_trade(trade)
        unit = "₩" if trade["ticker"].endswith((".KS", ".KQ")) else "$"
        acct = f" [{trade['account']}]" if trade["account"] else ""
        self._reply(
            f"✅ 매매 기록 #{tid}\n"
            f"{trade['name']}({trade['ticker']}){acct} "
            f"{trade['side']} {trade['shares']}주 @ {unit}{trade['price']:,.0f}\n\n"
            f"다음 브리핑부터 AI가 이 매매를 감안합니다.\n"
            f"settings.py 갱신 후 '매매반영'을 입력하세요."
        )

    def _handle_trade_list(self) -> None:
        """미반영 매매 목록."""
        from core.trade_log import pending_trades_text

        text = pending_trades_text()
        self._reply(text if text else "✅ 미반영 매매 없음")

    def _handle_trade_apply(self) -> None:
        """매매 기록 반영 처리."""
        from core.trade_log import mark_all_applied

        n = mark_all_applied()
        self._reply(f"✅ {n}건 매매 기록을 반영 처리했습니다." if n else "반영할 기록 없음")

    def _handle_portfolio(self) -> None:
        """보유종목 현재 시세 조회."""
        self._reply("📈 보유종목 시세를 조회 중...")

        thread = threading.Thread(
            target=self._run_portfolio_thread,
            daemon=True,
        )
        thread.start()

    def _run_portfolio_thread(self) -> None:
        """보유종목 조회 스레드."""
        try:
            msg = _build_portfolio_message()
            self._reply(msg)
        except Exception as e:
            log.error(f"보유종목 조회 실패: {e}")
            self._reply(f"❌ 조회 실패: {e}")

    def _reply(self, text: str) -> None:
        """텔레그램 메시지 전송."""
        from core.telegram import send_simple_message
        send_simple_message(text)

    def _process_callback_query(self, callback_query: dict) -> None:
        """callback_query 처리. tp: → paper, tlp: → live pilot 라우팅."""
        data = callback_query.get("data", "")
        callback_id = callback_query.get("id", "")
        chat_id = str(
            callback_query.get("message", {}).get("chat", {}).get("id", "")
        )

        # 인증: 설정된 chat_id만 허용
        if chat_id and chat_id != TELEGRAM_CHAT_ID:
            log.warning(f"미인증 callback: chat_id={chat_id}")
            return

        # tlp: prefix → live pilot handler
        if data.startswith("tlp:"):
            log.info(f"Live Pilot callback 수신: {data}")
            try:
                from core.toss_live_pilot_telegram import handle_live_pilot_callback
                result = handle_live_pilot_callback(data)
                message = result.get("message", "처리 결과 없음\n실주문: 비활성")
            except Exception as e:
                log.error(f"Live Pilot callback 처리 오류: {e}")
                result = {"ok": False}
                message = f"Live Pilot 처리 오류\n실주문: 비활성"
            if callback_id:
                self._answer_callback(callback_id, result.get("ok", False))
            self._reply(message)
            return

        # tp: prefix → paper handler
        if not data.startswith("tp:"):
            return

        log.info(f"Paper callback 수신: {data}")

        try:
            from core.toss_paper_telegram import handle_toss_paper_callback
            result = handle_toss_paper_callback(data)
            message = result.get("message", "처리 결과 없음\n실주문: 비활성")
        except Exception as e:
            log.error(f"Paper callback 처리 오류: {e}")
            message = f"⚠ Paper 처리 오류\n실주문: 비활성"

        # answerCallbackQuery
        if callback_id:
            self._answer_callback(callback_id, result.get("ok", False))

        # 결과 메시지 전송
        self._reply(message)

    def _answer_callback(self, callback_id: str, ok: bool) -> None:
        """Telegram answerCallbackQuery 호출."""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        try:
            requests.post(url, json={
                "callback_query_id": callback_id,
                "text": "Paper 처리 완료" if ok else "Paper 처리 실패",
                "show_alert": False,
            }, timeout=5)
        except Exception as e:
            log.warning(f"answerCallbackQuery 실패: {e}")


# ─── 보유종목 메시지 빌더 ─────────────────────────────
def _build_portfolio_message() -> str:
    """전체 계좌 보유종목 시세 메시지 생성."""
    from core.market import _get_quote_realtime
    from core.market_hours import market_status_text

    lines: list[str] = []
    lines.append("━" * 24)
    lines.append("📈  *보유종목 현황*")
    lines.append(f"_{datetime.now(KST).strftime('%Y.%m.%d %H:%M')}_")
    lines.append(market_status_text())
    lines.append("━" * 24)

    total_value = 0.0

    # 일반계좌
    gen_value, gen_lines = _format_account(
        "⬜ 일반계좌", HOLDINGS_GENERAL, _get_quote_realtime,
    )
    lines.extend(gen_lines)
    total_value += gen_value + DEFAULT_CASH

    # RIA (종합·5/31 면제)
    ria_value, ria_lines = _format_account(
        "🟥 RIA", HOLDINGS_RIA, _get_quote_realtime,
    )
    lines.extend(ria_lines)
    total_value += ria_value + RIA_CASH

    # ISA
    isa_value, isa_lines = _format_account(
        "🟦 ISA", HOLDINGS_ISA, _get_quote_realtime,
    )
    lines.extend(isa_lines)
    total_value += isa_value + ISA_CASH

    # IRP
    irp_value, irp_lines = _format_account(
        "🟪 IRP", HOLDINGS_IRP, _get_quote_realtime,
    )
    lines.extend(irp_lines)
    total_value += irp_value + IRP_CASH + IRP_DEFAULT_OPTION

    # 연금저축
    pen_value, pen_lines = _format_account(
        "🟩 연금저축", HOLDINGS_PENSION, _get_quote_realtime,
    )
    lines.extend(pen_lines)
    total_value += pen_value + PENSION_MMF

    # 총합
    lines.append("")
    lines.append("━" * 24)
    lines.append(f"💰 *총 자산: ₩{total_value:,.0f}*")

    return "\n".join(lines)


def _format_account(
    label: str,
    holdings: dict[str, dict],
    quote_fn,
) -> tuple[float, list[str]]:
    """단일 계좌 포맷. (평가금액 합계, 메시지 라인 리스트) 반환."""
    lines: list[str] = []
    lines.append("")
    lines.append(f"{'─' * 24}")
    lines.append(f"{label}")
    lines.append("")

    usdkrw = _get_usdkrw()
    account_value = 0.0

    if not holdings:
        lines.append("  (보유 종목 없음)")
        return 0.0, lines

    for ticker, info in holdings.items():
        quote = quote_fn(ticker)
        if quote is None:
            continue

        shares = info.get("shares", 0)
        name = PORTFOLIO.get(ticker, ticker)

        if ticker in KRW_TICKERS:
            avg = info.get("avg_cost_krw", 0)
            value = quote.price * shares
            cost = avg * shares
            pnl_pct = ((value - cost) / cost * 100) if cost > 0 else 0
            price_str = f"₩{quote.price:,.0f}"
            value_str = f"₩{value:,.0f}"
        else:
            avg = info.get("avg_cost_usd", 0)
            value = quote.price * shares * usdkrw
            cost = avg * shares * usdkrw
            pnl_pct = ((quote.price - avg) / avg * 100) if avg > 0 else 0
            price_str = f"${quote.price:,.2f}"
            value_str = f"₩{value:,.0f}"

        account_value += value
        arrow = "📈" if pnl_pct >= 0 else "📉"
        lines.append(f"  {arrow} *{name}* {shares}주")
        lines.append(f"      {price_str} ({pnl_pct:+.1f}%) = {value_str}")

    return account_value, lines


def _get_usdkrw() -> float:
    """USD/KRW 환율 조회."""
    from core.market import _get_quote_realtime

    quote = _get_quote_realtime("USDKRW=X")
    return quote.price if quote else 1450.0
