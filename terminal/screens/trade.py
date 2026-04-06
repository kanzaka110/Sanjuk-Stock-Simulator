"""
매매 시뮬레이션 화면 — 가상 매수/매도, 포지션 관리
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
)

from config.settings import KRW_TICKERS, PORTFOLIO
from core.market import fmt_price
from core.portfolio import (
    execute_buy,
    execute_sell,
    get_portfolio_summary,
    get_trade_history,
)


class TradeScreen(Screen):
    """매매 시뮬레이션."""

    BINDINGS = [("r", "refresh", "새로고침")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main-content"):
            yield Label("💹 매매 시뮬레이션", classes="section-title")
            yield Static(id="account-info")

            yield Label("📋 보유 포지션", classes="section-title")
            yield DataTable(id="positions-table")

            yield Label("🔄 매매 주문", classes="section-title")
            with Vertical(classes="info-panel"):
                with Horizontal():
                    yield Input(
                        placeholder="종목코드 (예: 012450.KS)",
                        id="ticker-input",
                    )
                    yield Input(
                        placeholder="가격",
                        id="price-input",
                        type="number",
                    )
                    yield Input(
                        placeholder="수량",
                        id="shares-input",
                        type="integer",
                    )
                with Horizontal():
                    yield Input(
                        placeholder="매매 사유 (선택)",
                        id="reason-input",
                    )
                    yield Button("매수", id="buy-btn", variant="success")
                    yield Button("매도", id="sell-btn", variant="error")

            yield Static(id="trade-result")

            yield Label("📜 최근 매매 기록", classes="section-title")
            yield DataTable(id="history-table")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self.action_refresh()

    def _setup_tables(self) -> None:
        pos_table = self.query_one("#positions-table", DataTable)
        pos_table.add_columns("종목", "수량", "평균단가", "평가금액", "손익", "수익률")

        hist_table = self.query_one("#history-table", DataTable)
        hist_table.add_columns("시간", "종목", "매수/매도", "가격", "수량", "사유")

    def action_refresh(self) -> None:
        self._update_account()
        self._update_positions()
        self._update_history()

    def _update_account(self) -> None:
        cash, positions = get_portfolio_summary()
        total_eval = sum(p.shares * p.avg_price for p in positions)
        info = self.query_one("#account-info", Static)
        info.update(
            f"💰 예수금: ₩{cash:,.0f}  |  "
            f"보유 평가: ₩{total_eval:,.0f}  |  "
            f"총 자산: ₩{cash + total_eval:,.0f}"
        )

    def _update_positions(self) -> None:
        _, positions = get_portfolio_summary()
        table = self.query_one("#positions-table", DataTable)
        table.clear()
        for p in positions:
            eval_amt = p.shares * p.avg_price
            table.add_row(
                f"{p.name} ({p.ticker})",
                str(p.shares),
                f"₩{p.avg_price:,.0f}",
                f"₩{eval_amt:,.0f}",
                "—",
                "—",
            )

    def _update_history(self) -> None:
        trades = get_trade_history(15)
        table = self.query_one("#history-table", DataTable)
        table.clear()
        for t in trades:
            action_label = "🟢 매수" if t.action == "buy" else "🔴 매도"
            table.add_row(
                t.created_at[:16],
                f"{t.name}",
                action_label,
                f"₩{t.price:,.0f}",
                str(t.shares),
                t.reason[:30],
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        ticker = self.query_one("#ticker-input", Input).value.strip()
        price_str = self.query_one("#price-input", Input).value.strip()
        shares_str = self.query_one("#shares-input", Input).value.strip()
        reason = self.query_one("#reason-input", Input).value.strip()
        result_widget = self.query_one("#trade-result", Static)

        if not ticker or not price_str or not shares_str:
            result_widget.update("⚠️ 종목코드, 가격, 수량을 모두 입력하세요")
            return

        try:
            price = float(price_str)
            shares = int(shares_str)
        except ValueError:
            result_widget.update("⚠️ 가격/수량 형식이 올바르지 않습니다")
            return

        name = PORTFOLIO.get(ticker, ticker)

        try:
            if event.button.id == "buy-btn":
                record = execute_buy(ticker, name, price, shares, reason)
                result_widget.update(
                    f"✅ 매수 완료: {name} {shares}주 @ ₩{price:,.0f}"
                )
            elif event.button.id == "sell-btn":
                record = execute_sell(ticker, name, price, shares, reason)
                result_widget.update(
                    f"✅ 매도 완료: {name} {shares}주 @ ₩{price:,.0f}"
                )
        except ValueError as e:
            result_widget.update(f"❌ {e}")

        self.action_refresh()
