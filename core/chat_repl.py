"""
대화형 CLI REPL — 멀티턴 투자 전략 논의

사용법: python main.py chat
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from core.analyzer import ask_ai, analyze
from core.market import fetch_market
from core.models import MarketSnapshot

log = logging.getLogger(__name__)
console = Console()

MAX_HISTORY = 40

HELP_TEXT = """
[bold cyan]명령어[/bold cyan]
  /refresh   시장 데이터 다시 수집
  /analyze   전체 AI 브리핑 분석 실행
  /clear     대화 초기화
  /help      이 도움말
  /quit      종료
"""

WELCOME = """[bold green]산적 주식 파트너[/bold green] — 대화형 분석 모드
시장 데이터를 로드하고 AI와 매매 전략을 논의합니다.
[dim]/help 로 명령어 확인[/dim]
"""


@dataclass
class ChatSession:
    """대화 세션 상태 관리."""

    history: list[dict] = field(default_factory=list)
    snapshot: MarketSnapshot | None = None

    def add_user(self, content: str) -> None:
        self.history.append({"role": "user", "content": content})
        self._trim()

    def add_assistant(self, content: str) -> None:
        self.history.append({"role": "assistant", "content": content})
        self._trim()

    def clear(self) -> None:
        self.history.clear()

    def _trim(self) -> None:
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]


def _load_market(session: ChatSession) -> None:
    """시장 데이터 수집."""
    console.print("[yellow]⏳ 시장 데이터 수집 중...[/yellow]")
    try:
        session.snapshot = fetch_market()
        count = len(session.snapshot.stocks)
        console.print(f"[green]✅ {count}종목 수집 완료[/green]")
    except Exception as e:
        console.print(f"[red]❌ 시장 데이터 수집 실패: {e}[/red]")


def _handle_analyze(session: ChatSession) -> None:
    """전체 AI 분석 실행."""
    if not session.snapshot:
        console.print("[red]시장 데이터가 없습니다. /refresh 먼저 실행하세요.[/red]")
        return

    console.print("[yellow]⏳ AI 전체 분석 중... (1-2분 소요)[/yellow]")
    try:
        result = analyze(session.snapshot)
        summary = (
            f"🎯 **{result.advisor_verdict}**  |  시장: {result.market_status}\n\n"
            f"💬 {result.advisor_oneliner}\n\n"
            f"**전략 요약**\n{result.strategy_summary}\n\n"
        )
        if result.buy_signals:
            summary += "**🟢 매수 신호**\n"
            for sig in result.buy_signals:
                summary += f"- {sig.urgency} {sig.name}: {sig.entry_price} → {sig.target_price} (손절 {sig.stop_loss})\n"
            summary += "\n"
        if result.sell_signals:
            summary += "**🔴 매도 신호**\n"
            for sig in result.sell_signals:
                summary += f"- {sig.urgency} {sig.name}: 익절 {sig.target_price} (손절 {sig.stop_loss})\n"
            summary += "\n"
        if result.advisor_conclusion:
            summary += f"**종합 결론**\n{result.advisor_conclusion}"

        console.print(Panel(Markdown(summary), title="AI 분석 결과", border_style="cyan"))
    except Exception as e:
        console.print(f"[red]❌ 분석 실패: {e}[/red]")


def _handle_question(session: ChatSession, question: str) -> None:
    """AI에게 질문."""
    if not session.snapshot:
        console.print("[red]시장 데이터가 없습니다. /refresh 먼저 실행하세요.[/red]")
        return

    try:
        reply = ask_ai(question, session.snapshot, history=session.history)
        session.add_user(question)
        session.add_assistant(reply)
        console.print()
        console.print(Panel(Markdown(reply), title="AI", border_style="green"))
    except Exception as e:
        console.print(f"[red]❌ AI 응답 오류: {e}[/red]")


def run_repl() -> None:
    """대화형 REPL 시작."""
    console.print(Panel(WELCOME, border_style="green"))

    session = ChatSession()
    _load_market(session)

    while True:
        try:
            console.print()
            user_input = console.input("[bold cyan]You>[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]종료합니다.[/dim]")
            break

        if not user_input:
            continue

        cmd = user_input.lower()

        if cmd in ("/quit", "/exit", "/q"):
            console.print("[dim]종료합니다.[/dim]")
            break
        elif cmd == "/help":
            console.print(HELP_TEXT)
        elif cmd == "/clear":
            session.clear()
            console.print("[green]대화가 초기화되었습니다.[/green]")
        elif cmd == "/refresh":
            _load_market(session)
        elif cmd == "/analyze":
            _handle_analyze(session)
        else:
            _handle_question(session, user_input)
