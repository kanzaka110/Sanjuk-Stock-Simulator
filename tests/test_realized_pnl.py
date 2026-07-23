"""실현 P&L 재구성(Tier0.1) 검증 — DB/네트워크 불필요 (합성 주문)."""

import pytest

from core import realized_pnl as rp


def _o(symbol, side, qty, price, ts):
    return {"symbol": symbol, "side": side, "qty": qty, "price": price, "ts": ts}


def test_simple_round_trip_profit():
    orders = [
        _o("NVDA", "buy", 10, 100.0, "t1"),
        _o("NVDA", "sell", 10, 110.0, "t2"),
    ]
    r = rp.reconstruct_round_trips(orders)
    assert len(r["round_trips"]) == 1
    rt = r["round_trips"][0]
    # gross = (110-100)*10 = 100, fee = 100(entry notional 1000)*0.10%/... US=0.10%
    assert rt["gross_pnl"] == pytest.approx(100.0)
    assert rt["fee"] == pytest.approx(1000.0 * 0.10 / 100)  # 1.0
    assert rt["net_pnl"] == pytest.approx(99.0)
    assert not r["open_positions"]


def test_fifo_partial_matching():
    orders = [
        _o("AAA", "buy", 10, 100.0, "t1"),
        _o("AAA", "buy", 10, 120.0, "t2"),
        _o("AAA", "sell", 15, 130.0, "t3"),   # 10@100 + 5@120 매칭
    ]
    r = rp.reconstruct_round_trips(orders)
    assert len(r["round_trips"]) == 2
    assert r["round_trips"][0]["qty"] == 10 and r["round_trips"][0]["entry_price"] == 100.0
    assert r["round_trips"][1]["qty"] == 5 and r["round_trips"][1]["entry_price"] == 120.0
    # 남은 5@120 미청산
    assert len(r["open_positions"]) == 1 and r["open_positions"][0]["qty"] == 5


def test_unmatched_sell_flagged():
    orders = [_o("BBB", "sell", 5, 50.0, "t1")]  # 매수 없이 매도
    r = rp.reconstruct_round_trips(orders)
    assert not r["round_trips"]
    assert len(r["unmatched_sells"]) == 1


def test_kr_higher_fee_than_us():
    kr = rp._round_trip_cost_pct("005930.KS")
    us = rp._round_trip_cost_pct("NVDA")
    assert kr > us


def test_summarize_stats():
    rts = [
        {"net_pnl": 100.0, "net_pnl_pct": 10.0, "fee": 1.0},
        {"net_pnl": -50.0, "net_pnl_pct": -5.0, "fee": 1.0},
    ]
    s = rp.summarize(rts)
    assert s["n"] == 2
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["total_net_pnl"] == pytest.approx(50.0)
    assert s["profit_factor"] == pytest.approx(100.0 / 50.0)


def test_summarize_empty():
    assert rp.summarize([]) == {"n": 0}


def test_text_flags_approximation():
    orders = [_o("NVDA", "buy", 1, 100.0, "t1"), _o("NVDA", "sell", 1, 105.0, "t2")]
    txt = rp.realized_pnl_text(rp.reconstruct_round_trips(orders))
    assert "실현 P&L" in txt
    assert "근사" in txt  # 데이터 한계 명시
