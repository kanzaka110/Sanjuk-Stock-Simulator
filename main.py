"""
산적 주식 시뮬레이터 — 엔트리포인트

사용법:
  python main.py              # TUI 터미널 실행
  python main.py briefing     # 브리핑 생성 → Notion + 텔레그램
  python main.py price        # Notion 주가 업데이트
"""

import io
import os
import sys
from pathlib import Path

# Windows CP949 깨짐 방지 — stdout/stderr UTF-8 강제
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# .env 로드
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# 설정 계층 로드 (settings.local.json 등)
from core.config_loader import init_config, load_local_settings

_local_settings = load_local_settings()
if _local_settings:
    from core.config_loader import apply_overrides
    apply_overrides(_local_settings)


def cmd_tui() -> None:
    """Textual TUI 실행."""
    from terminal.app import StockSimulatorApp

    app = StockSimulatorApp()
    app.run()


def cmd_briefing() -> None:
    """브리핑 생성 → Notion 저장 → 텔레그램 전송."""
    from core.config_loader import validate_config
    from core.market import fetch_market
    from core.analyzer import analyze
    from core.notion import save_to_notion
    from core.telegram import send_briefing_telegram

    # 설정 검증
    validation = validate_config("briefing")
    if not validation.valid:
        print(f"[ERROR] 필수 설정 누락: {', '.join(validation.missing_required)}")
        sys.exit(1)

    briefing_type = os.environ.get("BRIEFING_TYPE", "MANUAL")

    print(f"\n{'='*56}")
    print(f"  산적 주식 시뮬레이터 — 브리핑")
    print(f"  유형: {briefing_type}")
    print(f"{'='*56}\n")

    print("[1/3] 시장 데이터 수집...")
    snapshot = fetch_market(briefing_type)
    print(f"  포트폴리오 {len(snapshot.stocks)}종목 수집 완료")

    print("[2/3] AI 멀티 에이전트 분석...")
    result = analyze(snapshot, briefing_type)
    print(f"  제목: {result.title}")
    print(f"  판단: {result.advisor_verdict}")
    persona_summary = result.raw_json.get("persona_summary", {})
    if persona_summary:
        for name, summary in persona_summary.items():
            print(f"  [{name}] {summary}")

    print("[3/3] Notion 저장...")
    try:
        page_id = save_to_notion(result, snapshot, briefing_type)
        page_url = f"https://notion.so/{page_id.replace('-', '')}"
        print(f"  Notion: {page_url}")

        print("텔레그램 전송...")
        sent = send_briefing_telegram(result, page_id, briefing_type)
        if sent:
            print("  텔레그램 전송 완료")
        else:
            print("  텔레그램 전송 실패 또는 설정 없음")
    except Exception as e:
        print(f"  Notion 저장 실패: {e}")
        # Notion 없이도 텔레그램 전송 시도
        send_briefing_telegram(result, "", briefing_type)

    print(f"\n{'='*56}")
    print("  브리핑 완료!")
    print(f"{'='*56}\n")


def cmd_price() -> None:
    """Notion 주가 업데이트."""
    from core.price_updater import update_all_prices

    count = update_all_prices()
    print(f"주가 업데이트 완료: {count}종목")


def cmd_bot() -> None:
    """텔레그램 봇 + 시장 모니터 동시 실행."""
    import signal
    import threading

    from core.config_loader import validate_config
    from core.monitor import MarketMonitor
    from core.telegram_bot import TelegramBot

    validation = validate_config("bot")
    if not validation.valid:
        print(f"[ERROR] 필수 설정 누락: {', '.join(validation.missing_required)}")
        sys.exit(1)

    print(f"\n{'='*56}")
    print("  산적 주식 시뮬레이터 — 봇 + 모니터")
    print(f"{'='*56}\n")

    bot = TelegramBot()
    monitor = MarketMonitor()

    # 모니터를 별도 스레드에서 실행
    monitor_thread = threading.Thread(target=monitor.run, daemon=True)
    monitor_thread.start()
    print("  [OK] 시장 모니터 시작")

    # graceful shutdown
    def shutdown(signum, frame):
        print("\n종료 중...")
        bot.stop()
        monitor.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 봇 폴링은 메인 스레드에서 (KeyboardInterrupt 수신용)
    print("  [OK] 텔레그램 봇 폴링 시작")
    print(f"\n  텔레그램에서 '도움말'을 입력하세요")
    print(f"{'='*56}\n")
    bot.run_polling()


def cmd_monitor() -> None:
    """시장 모니터만 단독 실행."""
    import signal

    from core.monitor import MarketMonitor

    print(f"\n{'='*56}")
    print("  산적 주식 시뮬레이터 — 시장 모니터")
    print(f"{'='*56}\n")

    monitor = MarketMonitor()

    def shutdown(signum, frame):
        print("\n종료 중...")
        monitor.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    monitor.run()


COMMANDS = {
    "briefing": cmd_briefing,
    "price": cmd_price,
    "bot": cmd_bot,
    "monitor": cmd_monitor,
}

USAGE = """산적 주식 시뮬레이터

사용법:
  python main.py              TUI 터미널 실행
  python main.py briefing     브리핑 생성 (Notion + 텔레그램 알림)
  python main.py price        Notion 주가 업데이트
  python main.py bot          텔레그램 봇 + 시장 모니터 실행
  python main.py monitor      시장 모니터만 실행
  python main.py help         이 도움말
"""


def main() -> None:
    args = sys.argv[1:]

    if not args:
        # TUI: ANALYSIS 모드
        from core.permissions import set_mode, OperationMode
        set_mode(OperationMode.ANALYSIS)
        cmd_tui()
        return

    command = args[0].lower()

    if command in ("help", "--help", "-h"):
        print(USAGE)
        return

    handler = COMMANDS.get(command)
    if handler is None:
        print(f"알 수 없는 명령: {command}")
        print(USAGE)
        sys.exit(1)

    # 운영 모드 자동 설정
    from core.permissions import mode_from_command, set_mode
    set_mode(mode_from_command(command))

    handler()


if __name__ == "__main__":
    main()
