"""
대시보드 화면 — 포트폴리오 현황, 시장 지수, 매크로 지표
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, Static
from textual.worker import Worker, work

from config.settings import KRW_TICKERS
from core.market import fetch_market, fmt_price, pct_bar
from core.models import MarketSnapshot
from db.store import get_cash, get_positions


class DashboardScreen(Screen):
    """메인 대시보드."""

    BINDINGS = [("r", "refresh", "새로고침")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main-content"):
            yield Label("📊 산적 주식 시뮬레이터 — 대시보드", classes="section-title")
            yield Static(id="account-summary")
            yield Label("📈 시장 지수", classes="section-title")
            yield DataTable(id="indices-table")
            yield Label("🌐 매크로 지표", classes="section-title")
            yield DataTable(id="macro-table")
            yield Label("📋 포트폴리오", classes="section-title")
            yield DataTable(id="portfolio-table")
            yield Static(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self._load_account()
        self.action_refresh()

    def _setup_tables(self) -> None:
        indices = self.query_one("#indices-table", DataTable)
        indices.add_columns("지수", "현재가", "등락률", "방향")

        macro = self.query_one("#macro-table", DataTable)
        macro.add_columns("지표", "현재값", "전일비", "방향")

        portfolio = self.query_one("#portfolio-table", DataTable)
        portfolio.add_columns(
            "종목", "현재가", "등락률", "변동액", "고가", "저가"
        )

    def _load_account(self) -> None:
        cash = get_cash()
        positions = get_positions()
        total_value = sum(p.shares * p.avg_price for p in positions)
        summary = self.query_one("#account-summary", Static)
        summary.update(
            f"💰 예수금: ₩{cash:,.0f}  |  "
            f"보유종목 평가: ₩{total_value:,.0f}  |  "
            f"총 자산: ₩{cash + total_value:,.0f}"
        )

    def action_refresh(self) -> None:
        status = self.query_one("#status-bar", Static)
        status.update("⏳ 시장 데이터 수집 중...")
        self._fetch_data()

    @work(thread=True)
    def _fetch_data(self) -> MarketSnapshot:
        return fetch_market()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state.name != "SUCCESS":
            return
        snapshot: MarketSnapshot = event.worker.result
        self._update_indices(snapshot)
        self._update_macro(snapshot)
        self._update_portfolio(snapshot)
        status = self.query_one("#status-bar", Static)
        status.update(f"✅ 마지막 업데이트: {snapshot.timestamp}")

    def _update_indices(self, snapshot: MarketSnapshot) -> None:
        table = self.query_one("#indices-table", DataTable)
        table.clear()
        for nm, q in snapshot.indices.items():
            arrow = "▲" if q.pct >= 0 else "▼"
            table.add_row(nm, f"{q.price:,.2f}", f"{arrow} {q.pct:+.2f}%", pct_bar(q.pct))

    def _update_macro(self, snapshot: MarketSnapshot) -> None:
        table = self.query_one("#macro-table", DataTable)
        table.clear()
        for nm, q in snapshot.macro.items():
            if "원달러" in nm:
                val = f"₩{q.price:,.2f}"
            elif "VIX" in nm or "국채" in nm:
                val = f"{q.price:.2f}"
            else:
                val = f"${q.price:,.2f}"
            arrow = "▲" if q.pct >= 0 else "▼"
            table.add_row(nm, val, f"{arrow} {q.pct:+.2f}%", pct_bar(q.pct))

    def _update_portfolio(self, snapshot: MarketSnapshot) -> None:
        table = self.query_one("#portfolio-table", DataTable)
        table.clear()
        for tk, q in snapshot.stocks.items():
            arrow = "▲" if q.pct >= 0 else "▼"
            table.add_row(
                f"{q.name}",
                fmt_price(tk, q.price),
                f"{arrow} {q.pct:+.2f}%",
                fmt_price(tk, abs(q.change)),
                fmt_price(tk, q.high),
                fmt_price(tk, q.low),
            )
