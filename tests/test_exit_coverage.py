"""손절 커버리지(#3) flag 검증 — 기본 OFF=현재 동작, ON=실보유 전체 커버."""

import pytest

from core import toss_order_watch as ow


def _buy_rec(sym, entry=100.0, stop=95.0, reason="manual"):
    return {"symbol": sym, "side": "buy", "status": "live_sent",
            "limit_price": entry, "stop_loss": stop, "target_price": 0,
            "reason": reason, "quantity": 1, "pilot_id": "p",
            "sent_at": None, "created_at": None}


def test_record_to_exit_alert_stop_hit():
    a = ow._record_to_exit_alert(_buy_rec("AAA", 100, 95), lambda s: 90.0)
    assert a is not None and a["type"] == "stop_loss_hit"


def test_record_to_exit_alert_none_above_stop():
    assert ow._record_to_exit_alert(_buy_rec("AAA", 100, 95), lambda s: 99.0) is None


def test_record_to_exit_alert_income_managed_stop():
    # auto_pipeline → stop = entry*0.975 = 97.5
    a = ow._record_to_exit_alert(_buy_rec("AAA", 100, 0, reason="auto_pipeline"), lambda s: 97.0)
    assert a is not None and a["type"] == "stop_loss_hit"


def test_flag_parsing(monkeypatch):
    monkeypatch.delenv("TOSS_EXIT_CHECK_ALL_HOLDINGS", raising=False)
    assert ow._exit_check_all_holdings() is False
    monkeypatch.setenv("TOSS_EXIT_CHECK_ALL_HOLDINGS", "true")
    assert ow._exit_check_all_holdings() is True


def test_coverage_off_skips_unwindowed_holding(monkeypatch):
    monkeypatch.delenv("TOSS_EXIT_CHECK_ALL_HOLDINGS", raising=False)
    monkeypatch.setattr(ow, "_held_symbols_for_exit", lambda: {"BBB": 1.0})
    monkeypatch.setattr(ow, "_recent_live_buy_record", lambda s, r: _buy_rec("BBB"))
    alerts = ow.check_exit_levels(records=[], price_fn=lambda s: 90.0)
    assert not any(a["symbol"] == "BBB" for a in alerts)  # flag OFF → 커버 안 함


def test_coverage_on_checks_unwindowed_holding(monkeypatch):
    monkeypatch.setenv("TOSS_EXIT_CHECK_ALL_HOLDINGS", "true")
    monkeypatch.setattr(ow, "_held_symbols_for_exit", lambda: {"BBB": 1.0})
    monkeypatch.setattr(ow, "_recent_live_buy_record", lambda s, r: _buy_rec("BBB"))
    alerts = ow.check_exit_levels(records=[], price_fn=lambda s: 90.0)
    bbb = [a for a in alerts if a["symbol"] == "BBB"]
    assert bbb and bbb[0]["type"] == "stop_loss_hit"  # flag ON → 창 밖 보유도 손절 감지


def test_coverage_on_no_double_when_in_window(monkeypatch):
    # 이미 records에 있으면 커버리지가 중복 추가 안 함
    monkeypatch.setenv("TOSS_EXIT_CHECK_ALL_HOLDINGS", "true")
    monkeypatch.setattr(ow, "_held_symbols_for_exit", lambda: {"AAA": 1.0})
    monkeypatch.setattr(ow, "_recent_live_buy_record", lambda s, r: _buy_rec("AAA"))
    import core.toss_order_watch as m
    recs = [_buy_rec("AAA")]
    # sent_at None → 메인 루프 lookback 스킵될 수 있으므로 커버리지만 확인: 1건 이하
    alerts = ow.check_exit_levels(records=recs, price_fn=lambda s: 90.0)
    assert len([a for a in alerts if a["symbol"] == "AAA"]) <= 1
