"""
산적 주식 시뮬레이터 — 엔트리포인트

사용법:
  python main.py              # TUI 터미널 실행
  python main.py briefing     # 브리핑 생성 → Notion + 텔레그램
  python main.py server       # API 서버 시작 (자동화용)
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


def cmd_tui() -> None:
    """Textual TUI 실행."""
    from terminal.app import StockSimulatorApp

    app = StockSimulatorApp()
    app.run()


def cmd_briefing() -> None:
    """브리핑 생성 → Notion 저장 → 텔레그램 전송."""
    from core.market import fetch_market
    from core.analyzer import analyze
    from core.notion import save_to_notion
    from core.telegram import send_briefing_telegram

    briefing_type = os.environ.get("BRIEFING_TYPE", "MANUAL")

    print(f"\n{'='*56}")
    print(f"  산적 주식 시뮬레이터 — 브리핑")
    print(f"  유형: {briefing_type}")
    print(f"{'='*56}\n")

    print("[1/3] 시장 데이터 수집...")
    snapshot = fetch_market()
    print(f"  포트폴리오 {len(snapshot.stocks)}종목 수집 완료")

    print("[2/3] AI 멀티 에이전트 분석...")
    result = analyze(snapshot)
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


def cmd_server() -> None:
    """API 서버 시작."""
    import uvicorn

    from api.server import app
    from config.settings import API_PORT

    print(f"산적 주식 시뮬레이터 API 서버 시작 (포트 {API_PORT})...")
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)


COMMANDS = {
    "briefing": cmd_briefing,
    "server": cmd_server,
    "price": cmd_price,
}

USAGE = """산적 주식 시뮬레이터

사용법:
  python main.py              TUI 터미널 실행
  python main.py briefing     브리핑 생성 (Notion + 텔레그램 알림)
  python main.py server       API 서버 시작 (자동화용)
  python main.py price        Notion 주가 업데이트
  python main.py help         이 도움말
"""


def main() -> None:
    args = sys.argv[1:]

    if not args:
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

    handler()


if __name__ == "__main__":
    main()
