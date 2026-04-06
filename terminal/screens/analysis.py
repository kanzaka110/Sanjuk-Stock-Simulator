"""
AI 분석 화면 — 브리핑 결과 표시
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, Static
from textual.worker import Worker, work

from core.analyzer import analyze
from core.market import fetch_market, signal_badge
from core.models import BriefingResult, MarketSnapshot


class AnalysisScreen(Screen):
    """AI 분석 브리핑 화면."""

    BINDINGS = [("r", "run_analysis", "분석 실행")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main-content"):
            yield Label("🤖 AI 투자 분석", classes="section-title")
            yield Static(
                "[r] 키를 눌러 AI 분석을 실행하세요",
                id="analysis-status",
            )
            yield Static(id="verdict-panel")
            yield Label("📋 종목별 신호", classes="section-title")
            yield DataTable(id="signals-table")
            yield Label("🟢 매수 전략", classes="section-title")
            yield DataTable(id="buy-table")
            yield Label("🔴 매도 전략", classes="section-title")
            yield DataTable(id="sell-table")
            yield Static(id="summary-panel")
            yield Static(id="conclusion-panel")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()

    def _setup_tables(self) -> None:
        signals = self.query_one("#signals-table", DataTable)
        signals.add_columns("종목", "현재가", "등락률", "신호", "근거")

        buy = self.query_one("#buy-table", DataTable)
        buy.add_columns("종목", "긴급도", "진입가", "목표가", "손절가", "수량", "타이밍")

        sell = self.query_one("#sell-table", DataTable)
        sell.add_columns("종목", "긴급도", "현재가", "익절가", "손절가", "수량")

    def action_run_analysis(self) -> None:
        status = self.query_one("#analysis-status", Static)
        status.update("⏳ 데이터 수집 + AI 분석 중... (1-2분 소요)")
        self._do_analysis()

    @work(thread=True)
    def _do_analysis(self) -> BriefingResult:
        snapshot = fetch_market()
        return analyze(snapshot)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state.name == "ERROR":
            status = self.query_one("#analysis-status", Static)
            status.update(f"❌ 분석 실패: {event.worker.error}")
            return
        if event.state.name != "SUCCESS":
            return

        result: BriefingResult = event.worker.result
        self._render_result(result)

    def _render_result(self, result: BriefingResult) -> None:
        status = self.query_one("#analysis-status", Static)
        status.update(f"✅ {result.title}")

        # 판단 패널
        verdict = self.query_one("#verdict-panel", Static)
        verdict.update(
            f"🎯 AI 판단: {result.advisor_verdict}\n"
            f"💬 {result.advisor_oneliner}\n"
            f"📊 시장: {result.market_status} | 결정: {result.investment_decision}"
        )

        # 종목별 신호
        table = self.query_one("#signals-table", DataTable)
        table.clear()
        for sig in result.portfolio_signals:
            table.add_row(
                sig.name,
                "",
                "",
                signal_badge(sig.signal),
                sig.reason[:60],
            )

        # 매수 전략
        buy_table = self.query_one("#buy-table", DataTable)
        buy_table.clear()
        for sig in result.buy_signals:
            buy_table.add_row(
                sig.name,
                sig.urgency,
                sig.entry_price,
                sig.target_price,
                sig.stop_loss,
                sig.shares,
                sig.timing[:40],
            )

        # 매도 전략
        sell_table = self.query_one("#sell-table", DataTable)
        sell_table.clear()
        for sig in result.sell_signals:
            sell_table.add_row(
                sig.name,
                sig.urgency,
                sig.target_price,
                sig.target_price,
                sig.stop_loss,
                sig.shares,
            )

        # 요약
        summary = self.query_one("#summary-panel", Static)
        summary.update(f"📝 전략 요약\n{result.strategy_summary}")

        conclusion = self.query_one("#conclusion-panel", Static)
        conclusion.update(f"📝 종합 결론\n{result.advisor_conclusion}")
