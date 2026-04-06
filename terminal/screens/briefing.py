"""
브리핑 화면 — Notion 저장 + 텔레그램 알림 실행
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    Select,
    Static,
)
from textual.worker import Worker, work

from core.analyzer import analyze
from core.market import fetch_market, signal_badge
from core.models import BriefingResult, MarketSnapshot
from core.notion import save_to_notion
from core.telegram import send_briefing_telegram


class BriefingScreen(Screen):
    """브리핑 생성 → Notion 저장 → 텔레그램 전송."""

    _last_result: BriefingResult | None = None
    _last_snapshot: MarketSnapshot | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main-content"):
            yield Label("📊 AI 브리핑 → Notion + 텔레그램", classes="section-title")
            with Horizontal():
                yield Select(
                    [
                        ("📊 수시 브리핑", "MANUAL"),
                        ("🇰🇷 국내장 시작 전", "KR_BEFORE"),
                        ("🇺🇸 미국장 시작 전", "US_BEFORE"),
                    ],
                    value="MANUAL",
                    id="briefing-type",
                )
                yield Button("🚀 브리핑 생성", id="generate-btn", variant="primary")
                yield Button("📝 Notion 저장", id="notion-btn", variant="success", disabled=True)
                yield Button("📨 텔레그램 전송", id="telegram-btn", variant="warning", disabled=True)
            yield Static(id="briefing-status")
            yield Static(id="briefing-preview")

            yield Label("📋 종목 신호 미리보기", classes="section-title")
            yield DataTable(id="preview-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#preview-table", DataTable)
        table.add_columns("종목", "신호", "근거")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "generate-btn":
            self._run_generate()
        elif event.button.id == "notion-btn":
            self._run_notion_save()
        elif event.button.id == "telegram-btn":
            self._run_telegram()

    def _run_generate(self) -> None:
        status = self.query_one("#briefing-status", Static)
        status.update("⏳ 데이터 수집 + AI 분석 중... (1-2분 소요)")
        self.query_one("#notion-btn", Button).disabled = True
        self.query_one("#telegram-btn", Button).disabled = True
        self._do_generate()

    @work(thread=True)
    def _do_generate(self) -> tuple[BriefingResult, MarketSnapshot]:
        snapshot = fetch_market()
        result = analyze(snapshot)
        return result, snapshot

    def _run_notion_save(self) -> None:
        if not self._last_result or not self._last_snapshot:
            return
        status = self.query_one("#briefing-status", Static)
        status.update("⏳ Notion 저장 중...")
        briefing_type = self.query_one("#briefing-type", Select).value
        self._do_notion_save(self._last_result, self._last_snapshot, briefing_type)

    @work(thread=True)
    def _do_notion_save(
        self, result: BriefingResult, snapshot: MarketSnapshot, briefing_type: str
    ) -> str:
        return save_to_notion(result, snapshot, briefing_type)

    def _run_telegram(self) -> None:
        if not self._last_result:
            return
        status = self.query_one("#briefing-status", Static)
        status.update("⏳ 텔레그램 전송 중...")
        briefing_type = self.query_one("#briefing-type", Select).value
        self._do_telegram(self._last_result, briefing_type)

    @work(thread=True)
    def _do_telegram(self, result: BriefingResult, briefing_type: str) -> bool:
        # notion_page_id가 없으면 빈 문자열
        page_id = getattr(self, "_last_notion_page_id", "")
        return send_briefing_telegram(result, page_id, briefing_type)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        status = self.query_one("#briefing-status", Static)

        if event.state.name == "ERROR":
            status.update(f"❌ 오류: {event.worker.error}")
            return

        if event.state.name != "SUCCESS":
            return

        result = event.worker.result

        # 브리핑 생성 결과
        if isinstance(result, tuple) and len(result) == 2:
            briefing_result, snapshot = result
            self._last_result = briefing_result
            self._last_snapshot = snapshot
            self._render_preview(briefing_result)
            status.update(f"✅ 브리핑 생성 완료: {briefing_result.title}")
            self.query_one("#notion-btn", Button).disabled = False
            self.query_one("#telegram-btn", Button).disabled = False

        # Notion 저장 결과
        elif isinstance(result, str) and len(result) > 10:
            self._last_notion_page_id = result
            page_url = f"https://notion.so/{result.replace('-', '')}"
            status.update(f"✅ Notion 저장 완료: {page_url}")

        # 텔레그램 결과
        elif isinstance(result, bool):
            if result:
                status.update("✅ 텔레그램 전송 완료")
            else:
                status.update("⚠️ 텔레그램 전송 실패 (설정 확인)")

    def _render_preview(self, result: BriefingResult) -> None:
        preview = self.query_one("#briefing-preview", Static)
        preview.update(
            f"🎯 {result.advisor_verdict}  |  시장: {result.market_status}\n"
            f"💬 {result.advisor_oneliner}\n\n"
            f"📝 {result.strategy_summary[:200]}..."
        )

        table = self.query_one("#preview-table", DataTable)
        table.clear()
        for sig in result.portfolio_signals:
            table.add_row(sig.name, signal_badge(sig.signal), sig.reason[:60])
