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
    ISA_CASH,
    IRP_CASH,
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
    "전체 브리핑": "MANUAL",
    "미국장 브리핑": "US_BEFORE",
    "한국장 브리핑": "KR_BEFORE",
    "보유종목 확인": "PORTFOLIO",
    # 슬래시 명령 (영어 대체)
    "/briefing": "MANUAL",
    "/us": "US_BEFORE",
    "/kr": "KR_BEFORE",
    "/portfolio": "PORTFOLIO",
    "/status": "PORTFOLIO",
    "/help": "HELP",
    "도움말": "HELP",
}

HELP_TEXT = """📋 *사용 가능한 명령어*

🔹 *전체 브리핑* — 전체 포트폴리오 AI 분석
🔹 *한국장 브리핑* — 한국 종목 중심 분석
🔹 *미국장 브리핑* — 미국 종목 중심 분석
🔹 *보유종목 확인* — 현재 시세 + 수익률

⏱ 브리핑은 3~5분 소요됩니다."""


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
            "allowed_updates": ["message"],
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
        action = COMMANDS.get(text)

        if action is None:
            self._reply("❓ 알 수 없는 명령입니다.\n\n" + HELP_TEXT)
            return

        if action == "HELP":
            self._reply(HELP_TEXT)
        elif action == "PORTFOLIO":
            self._handle_portfolio()
        else:
            self._handle_briefing(action)

    def _handle_briefing(self, briefing_type: str) -> None:
        """브리핑 실행 요청."""
        from core.briefing_runner import is_briefing_running, run_briefing

        if is_briefing_running():
            self._reply("⏳ 브리핑이 이미 진행 중입니다. 완료 후 결과를 전송합니다.")
            return

        labels = {
            "MANUAL": "전체",
            "US_BEFORE": "미국장",
            "KR_BEFORE": "한국장",
        }
        label = labels.get(briefing_type, briefing_type)
        self._reply(f"📊 *{label} 브리핑* 생성을 시작합니다.\n⏱ 3~5분 소요됩니다.")

        # 별도 스레드에서 실행 (폴링 루프 블로킹 방지)
        thread = threading.Thread(
            target=self._run_briefing_thread,
            args=(briefing_type, label),
            daemon=True,
        )
        thread.start()

    def _run_briefing_thread(self, briefing_type: str, label: str) -> None:
        """브리핑 스레드 실행."""
        from core.briefing_runner import run_briefing

        result = run_briefing(briefing_type)
        if not result.success:
            self._reply(f"❌ {label} 브리핑 실패: {result.error}")
        # 성공 시: briefing_runner가 이미 텔레그램으로 결과 전송함

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
