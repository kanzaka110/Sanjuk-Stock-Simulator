"""
AI 질의 화면 — 자연어로 주식 관련 질문
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, Static
from textual.worker import Worker, work

from core.analyzer import ask_ai
from core.market import fetch_market
from core.models import MarketSnapshot


class AskScreen(Screen):
    """AI 질의 인터페이스."""

    _snapshot: MarketSnapshot | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main-content"):
            yield Label("💬 AI 주식 파트너에게 질문", classes="section-title")
            yield Static(
                "시장 데이터를 기반으로 AI가 답변합니다.\n"
                "예: \"한화에어로스페이스 팔때 됐나?\"\n"
                "예: \"지금 삼성전자 추가 매수해도 될까?\"\n"
                "예: \"엔비디아 전망 어때?\"",
                classes="info-panel",
            )
            yield Input(
                placeholder="질문을 입력하세요...",
                id="question-input",
            )
            yield Static(id="loading-status")
            yield Static(id="answer-panel")
            yield Label("📜 대화 기록", classes="section-title")
            yield Static(id="history-panel")
        yield Footer()

    def on_mount(self) -> None:
        self._load_market_data()

    @work(thread=True)
    def _load_market_data(self) -> MarketSnapshot:
        return fetch_market()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state.name == "SUCCESS":
            result = event.worker.result
            if isinstance(result, MarketSnapshot):
                self._snapshot = result
                status = self.query_one("#loading-status", Static)
                status.update("✅ 시장 데이터 로드 완료 — 질문을 입력하세요")
            elif isinstance(result, str):
                # AI 답변
                answer = self.query_one("#answer-panel", Static)
                answer.update(f"🤖 AI 답변\n{'─' * 50}\n{result}")
                status = self.query_one("#loading-status", Static)
                status.update("✅ 답변 완료")
        elif event.state.name == "ERROR":
            status = self.query_one("#loading-status", Static)
            status.update(f"❌ 오류: {event.worker.error}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "question-input":
            return

        question = event.value.strip()
        if not question:
            return

        if self._snapshot is None:
            status = self.query_one("#loading-status", Static)
            status.update("⏳ 시장 데이터를 먼저 로드합니다...")
            self._load_market_data()
            return

        # 기록 추가
        history = self.query_one("#history-panel", Static)
        current = history.renderable
        if isinstance(current, str) and current:
            history.update(f"{current}\n\n👤 {question}")
        else:
            history.update(f"👤 {question}")

        status = self.query_one("#loading-status", Static)
        status.update("⏳ AI 분석 중...")

        event.input.value = ""
        self._ask_question(question)

    @work(thread=True)
    def _ask_question(self, question: str) -> str:
        return ask_ai(question, self._snapshot)
