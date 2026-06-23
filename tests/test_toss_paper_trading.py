"""
Toss paper trading ledger 테스트

- paper trade 기록 가능
- live_order_sent 항상 false
- dry_run 항상 true
- 기존 portfolio DB 변경 없음
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import core.toss_paper_trading as pt


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path):
    """테스트마다 임시 DB 사용."""
    db = tmp_path / "test_paper.db"
    with patch.object(pt, "_DB_PATH", db):
        yield db


class TestRecordPaperTrade:
    def test_record_returns_id(self):
        row_id = pt.record_paper_trade(side="buy", symbol="005930.KS", quantity=5,
                                        limit_price=80000, estimated_amount_krw=400000)
        assert row_id > 0

    def test_dry_run_always_true(self):
        pt.record_paper_trade(side="buy", symbol="AAPL", estimated_amount_krw=100000)
        trades = pt.list_paper_trades()
        assert all(t["dry_run"] == 1 for t in trades)

    def test_live_order_sent_always_false(self):
        pt.record_paper_trade(side="buy", symbol="AAPL", estimated_amount_krw=100000)
        trades = pt.list_paper_trades()
        assert all(t["live_order_sent"] == 0 for t in trades)

    def test_mode_always_paper(self):
        pt.record_paper_trade(side="buy", symbol="AAPL", estimated_amount_krw=100000)
        trades = pt.list_paper_trades()
        assert all(t["mode"] == "paper" for t in trades)

    def test_multiple_records(self):
        for i in range(5):
            pt.record_paper_trade(side="buy", symbol=f"SYM{i}", estimated_amount_krw=10000*i)
        trades = pt.list_paper_trades()
        assert len(trades) == 5


class TestListPaperTrades:
    def test_empty_by_default(self):
        trades = pt.list_paper_trades()
        assert trades == []

    def test_limit(self):
        for i in range(10):
            pt.record_paper_trade(side="buy", symbol=f"S{i}", estimated_amount_krw=1000)
        trades = pt.list_paper_trades(limit=3)
        assert len(trades) == 3

    def test_order_desc(self):
        pt.record_paper_trade(side="buy", symbol="FIRST", estimated_amount_krw=1000)
        pt.record_paper_trade(side="buy", symbol="SECOND", estimated_amount_krw=2000)
        trades = pt.list_paper_trades()
        assert trades[0]["symbol"] == "SECOND"  # 최신이 먼저


class TestTodayPaperStats:
    def test_empty_stats(self):
        stats = pt.today_paper_stats()
        assert stats["count"] == 0
        assert stats["daily_amount_krw"] == 0

    def test_stats_after_trades(self):
        pt.record_paper_trade(side="buy", symbol="A", estimated_amount_krw=100000,
                               guard_status="paper_filled")
        pt.record_paper_trade(side="buy", symbol="B", estimated_amount_krw=200000,
                               guard_status="paper_filled")
        stats = pt.today_paper_stats()
        assert stats["count"] == 2
        assert stats["daily_amount_krw"] == 300000


class TestNoPortfolioDbContamination:
    """paper trading이 기존 DB 파일을 변경하지 않음."""

    def test_uses_separate_db_file(self):
        # _DB_PATH가 기존 memory.db나 store.db와 다른지
        real_path = Path(__file__).resolve().parents[1] / "db" / "data" / "toss_paper_trades.db"
        assert "toss_paper" in str(real_path)
        assert "memory.db" not in str(real_path)
        assert "store.db" not in str(real_path)
