"""tests/test_data_quality_gate.py — 브리핑 데이터 품질 게이트 판정."""

from types import SimpleNamespace

import pytest

from core.data_quality_gate import (
    STATUS_LIMITED,
    STATUS_NORMAL,
    STATUS_WARNING,
    DataQualityReport,
    assess_data_quality,
)


def _snapshot(fx=1450.0):
    """USDKRW=X macro Quote 하나만 든 최소 스냅샷."""
    macro = {}
    if fx is not None:
        macro["원달러(₩)"] = SimpleNamespace(ticker="USDKRW=X", price=fx)
    return SimpleNamespace(stocks={}, indices={}, macro=macro, news={})


def _holdings():
    return {
        "005930.KS": {"shares": 90, "avg_cost_krw": 60_425},
        "MU": {"shares": 5, "avg_cost_usd": 408.8},
    }


def test_all_normal():
    prices = {"005930.KS": 62_000.0, "MU": 420.0, "USDKRW=X": 1450.0}
    r = assess_data_quality(_snapshot(), prices, list(prices), _holdings())
    assert r.status == STATUS_NORMAL
    assert r.execution_limited is False
    assert r.warnings == ()
    assert "데이터 품질: 정상" in r.header_text()


def test_price_scale_anomaly_triggers_execution_limit():
    # 삼성전자 평단 60,425인데 현재가 620,000 → 10.3배 (자릿수 오류급) 스케일 이상
    prices = {"005930.KS": 620_000.0, "MU": 420.0, "USDKRW=X": 1450.0}
    r = assess_data_quality(_snapshot(), prices, list(prices), _holdings())
    assert r.status == STATUS_LIMITED
    assert r.execution_limited is True
    assert any("005930.KS" in a for a in r.price_scale_anomalies)
    assert "가격 스케일 이상" in r.header_text()
    assert "HOLD/BLOCK" in r.header_text()


def test_long_held_core_multiple_is_not_anomaly():
    # 장기 저가매집: 평단 60,425 vs 현재가 290,000 (4.8배) — 정상 시세, 오탐 금지
    # (2026-07-02 삼성전자 실체결가 ₩290,000으로 검증된 실사례)
    prices = {"005930.KS": 290_000.0, "MU": 420.0, "USDKRW=X": 1450.0}
    r = assess_data_quality(_snapshot(), prices, list(prices), _holdings())
    assert r.price_scale_anomalies == ()
    assert r.execution_limited is False
    assert r.status == STATUS_NORMAL


def test_missing_prices_majority_execution_limited():
    # 요청 4종목 중 3종목 시세 결측 (401/상장폐지 신호) → 결측비율 0.75 → 실행제한
    requested = ["005930.KS", "MU", "AAPL", "NVDA"]
    prices = {"005930.KS": 62_000.0, "USDKRW=X": 1450.0}
    r = assess_data_quality(_snapshot(), prices, requested, _holdings())
    assert r.status == STATUS_LIMITED
    assert r.execution_limited is True
    assert len(r.missing_price_tickers) == 3
    assert any("시세 조회 실패" in s for s in r.failed_sources)


def test_minor_missing_is_warning_not_limited():
    # 요청 4종목 중 1종목만 결측 (0.25) → 경고, 실행제한 아님
    requested = ["005930.KS", "MU", "AAPL", "NVDA"]
    prices = {
        "005930.KS": 62_000.0, "MU": 420.0, "AAPL": 230.0, "USDKRW=X": 1450.0,
    }
    r = assess_data_quality(_snapshot(), prices, requested, _holdings())
    assert r.status == STATUS_WARNING
    assert r.execution_limited is False
    assert "NVDA" in r.missing_price_tickers


def test_missing_fx_flags_warning():
    prices = {"005930.KS": 62_000.0, "MU": 420.0}
    r = assess_data_quality(_snapshot(fx=None), prices, list(prices), _holdings())
    assert r.fx_ok is False
    assert r.status in (STATUS_WARNING, STATUS_LIMITED)
    assert any("환율" in s for s in r.failed_sources)


def test_abnormal_fx_flags_warning():
    # 원달러 50 → 비정상 범위
    prices = {"005930.KS": 62_000.0, "MU": 420.0, "USDKRW=X": 50.0}
    r = assess_data_quality(_snapshot(fx=50.0), prices, list(prices), _holdings())
    assert r.fx_ok is False
    assert any("환율" in w for w in r.warnings)


def test_broker_snapshot_stale_note():
    prices = {"005930.KS": 62_000.0, "MU": 420.0, "USDKRW=X": 1450.0}
    r = assess_data_quality(
        _snapshot(), prices, list(prices), _holdings(),
        broker_snapshot_stale=True,
    )
    assert any("삼성 원본 미확인" in w for w in r.warnings)
    assert r.status == STATUS_WARNING


def test_no_baseline_ticker_skipped():
    # 평단 baseline 없는 종목은 스케일 판정에서 제외 (오탐 방지)
    prices = {"TSLA": 999_999.0, "USDKRW=X": 1450.0}
    r = assess_data_quality(_snapshot(), prices, list(prices), _holdings())
    assert r.price_scale_anomalies == ()
    assert r.execution_limited is False


def test_report_is_frozen():
    r = DataQualityReport()
    with pytest.raises(Exception):
        r.status = "x"  # type: ignore[misc]
