"""
서비스 관리 화면 — 텔레그램 챗봇, 주가 업데이트, 브리핑 cron
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, Static
from textual import work
from textual.worker import Worker


class ServicesScreen(Screen):
    """백그라운드 서비스 관리."""

    _chatbot_running: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main-content"):
            yield Label("⚙️ 서비스 관리", classes="section-title")

            yield Static(
                "텔레그램 챗봇, 주가 업데이트 등 백그라운드 작업을 관리합니다.",
                classes="info-panel",
            )

            yield Label("🤖 텔레그램 챗봇 (산적주식비서)", classes="section-title")
            yield Static(
                "Gemini 기반 실시간 대화 봇.\n"
                "⚠️ 챗봇은 폴링 모드로 실행되며, 터미널을 차단합니다.\n"
                "GCP에서는 systemd 서비스로 별도 실행하세요.",
                classes="warning-panel",
            )
            yield Button("🚀 챗봇 시작 (블로킹)", id="chatbot-btn", variant="primary")
            yield Static(id="chatbot-status")

            yield Label("📊 Notion 주가 업데이트", classes="section-title")
            yield Static(
                "Notion DB의 모든 종목 현재가를 yfinance로 업데이트합니다.",
                classes="info-panel",
            )
            yield Button("🔄 지금 업데이트", id="price-btn", variant="success")
            yield Static(id="price-status")

        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "chatbot-btn":
            status = self.query_one("#chatbot-status", Static)
            status.update("⏳ 챗봇 시작 중...")
            self._start_chatbot()
        elif event.button.id == "price-btn":
            status = self.query_one("#price-status", Static)
            status.update("⏳ 주가 업데이트 중...")
            self._update_prices()

    @work(thread=True)
    def _start_chatbot(self) -> str:
        from core.telegram import run_chatbot
        run_chatbot()
        return "챗봇 종료됨"

    @work(thread=True)
    def _update_prices(self) -> int:
        from core.price_updater import update_all_prices
        return update_all_prices()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state.name == "ERROR":
            # 어떤 작업이 실패했는지 확인
            for widget_id in ["chatbot-status", "price-status"]:
                try:
                    status = self.query_one(f"#{widget_id}", Static)
                    status.update(f"❌ 오류: {event.worker.error}")
                except Exception:
                    pass
            return

        if event.state.name != "SUCCESS":
            return

        result = event.worker.result
        if isinstance(result, int):
            status = self.query_one("#price-status", Static)
            status.update(f"✅ {result}개 종목 업데이트 완료")
        elif isinstance(result, str):
            status = self.query_one("#chatbot-status", Static)
            status.update(f"ℹ️ {result}")
