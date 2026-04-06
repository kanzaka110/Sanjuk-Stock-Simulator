"""
산적 주식 시뮬레이터 — Textual TUI 메인 앱
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from terminal.screens.dashboard import DashboardScreen
from terminal.screens.analysis import AnalysisScreen
from terminal.screens.trade import TradeScreen
from terminal.screens.ask import AskScreen


class StockSimulatorApp(App):
    """산적 주식 시뮬레이터 TUI."""

    TITLE = "산적 주식 시뮬레이터"
    SUB_TITLE = "AI 매매 의사결정 터미널"
    CSS_PATH = None

    BINDINGS = [
        Binding("d", "switch_screen('dashboard')", "대시보드", show=True),
        Binding("a", "switch_screen('analysis')", "AI 분석", show=True),
        Binding("t", "switch_screen('trade')", "매매", show=True),
        Binding("q", "switch_screen('ask')", "AI 질의", show=True),
        Binding("ctrl+c", "quit", "종료", show=True),
    ]

    SCREENS = {
        "dashboard": DashboardScreen,
        "analysis": AnalysisScreen,
        "trade": TradeScreen,
        "ask": AskScreen,
    }

    DEFAULT_CSS = """
    Screen {
        background: $surface;
    }

    #main-content {
        height: 1fr;
        padding: 1;
    }

    .section-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    .positive {
        color: #00ff00;
    }

    .negative {
        color: #ff4444;
    }

    .signal-buy {
        color: #00ff00;
        text-style: bold;
    }

    .signal-sell {
        color: #ff4444;
        text-style: bold;
    }

    .signal-hold {
        color: #4488ff;
    }

    DataTable {
        height: auto;
        max-height: 20;
    }

    .info-panel {
        border: solid $accent;
        padding: 1;
        margin: 1 0;
    }

    .warning-panel {
        border: solid #ff8800;
        padding: 1;
        margin: 1 0;
    }
    """

    def on_mount(self) -> None:
        self.push_screen("dashboard")

    def action_switch_screen(self, screen_name: str) -> None:
        self.switch_screen(screen_name)
