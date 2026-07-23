"""손절 청산 체결보장(marketable limit) flag 검증 — 기본 OFF=현재가 유지."""

import pytest

from core import toss_order_watch as ow


def _alert(atype, current):
    return {"type": atype, "current_price": current}


def test_aggressive_pct_env_parsing(monkeypatch):
    monkeypatch.delenv("TOSS_STOP_EXIT_AGGRESSIVE_PCT", raising=False)
    assert ow._stop_exit_aggressive_pct() == 0.0
    monkeypatch.setenv("TOSS_STOP_EXIT_AGGRESSIVE_PCT", "1.5")
    assert ow._stop_exit_aggressive_pct() == pytest.approx(1.5)
    monkeypatch.setenv("TOSS_STOP_EXIT_AGGRESSIVE_PCT", "-2")  # 음수 → 0
    assert ow._stop_exit_aggressive_pct() == 0.0


def test_default_off_uses_current_price(monkeypatch):
    monkeypatch.delenv("TOSS_STOP_EXIT_AGGRESSIVE_PCT", raising=False)
    assert ow._exit_limit_price(_alert("stop_loss_hit", 100.0)) == pytest.approx(100.0)
    assert ow._exit_limit_price(_alert("target_hit", 100.0)) == pytest.approx(100.0)


def test_flag_on_stop_exit_priced_aggressively(monkeypatch):
    monkeypatch.setenv("TOSS_STOP_EXIT_AGGRESSIVE_PCT", "2.0")
    # 손절: 100 * (1-0.02) = 98 → 급락장에서도 체결되는 marketable limit
    assert ow._exit_limit_price(_alert("stop_loss_hit", 100.0)) == pytest.approx(98.0)


def test_flag_on_target_hit_unchanged(monkeypatch):
    monkeypatch.setenv("TOSS_STOP_EXIT_AGGRESSIVE_PCT", "2.0")
    # 익절은 공격적 적용 안 함 (현재가 유지)
    assert ow._exit_limit_price(_alert("target_hit", 100.0)) == pytest.approx(100.0)


def test_zero_or_missing_price_safe():
    assert ow._exit_limit_price(_alert("stop_loss_hit", 0)) == 0.0
    assert ow._exit_limit_price({"type": "stop_loss_hit"}) == 0.0
