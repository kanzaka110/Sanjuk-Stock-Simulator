"""portfolio_live — settings HOLDINGS + 미반영 매매 합성 검증."""

from __future__ import annotations

import sqlite3

import pytest

from core import portfolio_live as pl


# ─── apply_trades (순수 함수) ────────────────────────

BASE_KR = {"005930.KS": {"shares": 100, "avg_cost_krw": 83_482}}


def _trade(ticker="005930.KS", side="매수", shares=10, price=286_000.0,
           account="일반", name="삼성전자", created="2026-07-04T10:00:00+09:00"):
    return {
        "id": 1, "created_at": created, "ticker": ticker, "name": name,
        "side": side, "shares": shares, "price": price, "account": account,
    }


class TestApplyTrades:
    def test_buy_recalculates_weighted_avg_and_deducts_cash(self):
        holdings, cash, notes = pl.apply_trades(
            BASE_KR, 10_000_000, [_trade(side="매수", shares=10, price=286_000)], 1500.0)
        info = holdings["005930.KS"]
        assert info["shares"] == 110
        expected_avg = (83_482 * 100 + 286_000 * 10) / 110
        assert info["avg_cost_krw"] == pytest.approx(expected_avg, rel=1e-6)
        assert cash == pytest.approx(10_000_000 - 286_000 * 10)
        assert notes == []
        # 불변성: 원본 미변경
        assert BASE_KR["005930.KS"]["shares"] == 100

    def test_sell_reduces_shares_and_adds_cash(self):
        holdings, cash, _ = pl.apply_trades(
            BASE_KR, 1_000_000, [_trade(side="매도", shares=30, price=290_000)], 1500.0)
        assert holdings["005930.KS"]["shares"] == 70
        # 평단은 매도로 변하지 않음
        assert holdings["005930.KS"]["avg_cost_krw"] == 83_482
        assert cash == pytest.approx(1_000_000 + 290_000 * 30)

    def test_full_sell_removes_position(self):
        holdings, cash, _ = pl.apply_trades(
            BASE_KR, 0, [_trade(side="매도", shares=100, price=290_000)], 1500.0)
        assert "005930.KS" not in holdings
        assert cash == pytest.approx(290_000 * 100)

    def test_buy_new_ticker_creates_position(self):
        holdings, cash, _ = pl.apply_trades(
            {}, 5_000_000, [_trade(ticker="000660.KS", name="SK하이닉스",
                                   side="매수", shares=1, price=2_187_000)], 1500.0)
        assert holdings["000660.KS"]["shares"] == 1
        assert holdings["000660.KS"]["avg_cost_krw"] == 2_187_000
        assert cash == pytest.approx(5_000_000 - 2_187_000)

    def test_usd_ticker_cash_converted_with_fx(self):
        base = {"NVDA": {"shares": 10, "avg_cost_usd": 190.0}}
        holdings, cash, _ = pl.apply_trades(
            base, 3_000_000, [_trade(ticker="NVDA", name="엔비디아",
                                     side="매수", shares=5, price=200.0)], 1500.0)
        assert holdings["NVDA"]["shares"] == 15
        expected_avg = (190 * 10 + 200 * 5) / 15
        assert holdings["NVDA"]["avg_cost_usd"] == pytest.approx(expected_avg, rel=1e-6)
        assert cash == pytest.approx(3_000_000 - 200 * 5 * 1500)

    def test_oversell_clamps_to_zero_with_note(self):
        holdings, _, notes = pl.apply_trades(
            BASE_KR, 0, [_trade(side="매도", shares=999, price=290_000)], 1500.0)
        assert "005930.KS" not in holdings
        assert any("초과" in n for n in notes)

    def test_sell_unknown_ticker_skipped_with_note(self):
        holdings, cash, notes = pl.apply_trades(
            {}, 100, [_trade(side="매도")], 1500.0)
        assert holdings == {}
        assert cash == 100
        assert any("보유 내역 없음" in n for n in notes)


# ─── pending_trades / effective_holdings (DB 연동) ───

@pytest.fixture
def trades_db(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(pl, "_DB_PATH", db)
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, ticker TEXT NOT NULL, name TEXT,
            side TEXT NOT NULL, shares INTEGER NOT NULL, price REAL NOT NULL,
            account TEXT DEFAULT '', applied INTEGER DEFAULT 0
        )""")
    conn.commit()
    yield conn
    conn.close()


def _insert(conn, created, ticker, name, side, shares, price, account="", applied=0):
    conn.execute(
        "INSERT INTO trades (created_at, ticker, name, side, shares, price, account, applied)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (created, ticker, name, side, shares, price, account, applied))
    conn.commit()


class TestPendingTrades:
    def test_applied_trades_excluded(self, trades_db):
        _insert(trades_db, "2026-07-04T10:00:00+09:00", "005930.KS", "삼성전자",
                "매수", 10, 286_000, "일반", applied=1)
        grouped, warnings = pl.pending_trades()
        assert grouped == {}
        assert warnings == []

    def test_grouped_by_normalized_account(self, trades_db):
        _insert(trades_db, "2026-07-04T10:00:00+09:00", "005930.KS", "삼성전자",
                "매수", 10, 286_000, "")  # 계좌 미지정 → 일반
        _insert(trades_db, "2026-07-04T11:00:00+09:00", "462870.KS", "시프트업",
                "매수", 10, 34_000, "isa")  # 소문자 → ISA
        grouped, _ = pl.pending_trades()
        assert len(grouped["일반"]) == 1
        assert len(grouped["ISA"]) == 1

    def test_trades_before_as_of_excluded_with_warning(self, trades_db):
        _insert(trades_db, "2026-07-01T10:00:00+09:00", "005930.KS", "삼성전자",
                "매수", 10, 286_000, "일반")
        grouped, warnings = pl.pending_trades(as_of="2026-07-03")
        assert grouped == {}
        assert any("이중계산" in w for w in warnings)


class TestEffectiveHoldings:
    def test_no_pending_returns_base_unchanged(self, trades_db):
        holdings, cash, meta = pl.effective_holdings(
            "일반", BASE_KR, 5_000_000, 1500.0)
        assert holdings == BASE_KR
        assert cash == 5_000_000
        assert meta["pending_trade_count"] == 0

    def test_pending_buy_reflected(self, trades_db):
        _insert(trades_db, "2026-07-04T10:00:00+09:00", "005930.KS", "삼성전자",
                "매수", 10, 286_000, "일반")
        holdings, cash, meta = pl.effective_holdings(
            "일반", BASE_KR, 5_000_000, 1500.0)
        assert holdings["005930.KS"]["shares"] == 110
        assert cash == pytest.approx(5_000_000 - 2_860_000)
        assert meta["pending_trade_count"] == 1

    def test_invariant_after_mark_applied(self, trades_db):
        """'매매반영' 후 델타 0 — settings 값 그대로."""
        _insert(trades_db, "2026-07-04T10:00:00+09:00", "005930.KS", "삼성전자",
                "매수", 10, 286_000, "일반")
        trades_db.execute("UPDATE trades SET applied = 1")
        trades_db.commit()
        holdings, cash, meta = pl.effective_holdings(
            "일반", BASE_KR, 5_000_000, 1500.0)
        assert holdings == BASE_KR
        assert cash == 5_000_000
        assert meta["pending_trade_count"] == 0
