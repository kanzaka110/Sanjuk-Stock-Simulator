"""tests/test_accuracy_feedback.py — 정확도 피드백 루프 신규 로직 검증.

③ reliability_directives_text: CIO system 프롬프트용 종목별 실측 신뢰도 테이블
④ confidence_calibration_text: 확신도 구간별 실측 승률 캘리브레이션
⑤ monitor INVALIDATION: 비보유 예약 셋업 붕괴 → 예약 취소 알림 (AI 게이트 우회)
② cross_check_prices + data_quality_gate source mismatch
"""

from datetime import datetime
from types import SimpleNamespace

from config.settings import KST
from core.monitor import MarketMonitor, _build_alert_message
from core.monitor_models import AlertResult, AlertTrigger, Severity, TriggerType


# ─── ③ reliability_directives_text ─────────────────────────


def _stats(evaluated, win_rate, avg_pnl=1.0):
    return {
        "evaluated_count": evaluated,
        "wins": int(evaluated * win_rate / 100),
        "losses": evaluated - int(evaluated * win_rate / 100),
        "win_rate": win_rate,
        "avg_pnl": avg_pnl,
        "total": evaluated,
    }


def test_reliability_directives(monkeypatch):
    import core.memory as mem

    monkeypatch.setattr(mem, "get_accuracy_summary", lambda: {
        "LMT": _stats(8, 80.0, 5.2),        # 고신뢰 → +10
        "161510.KS": _stats(6, 20.0, -3.1),  # 위험 → -15
        "005930.KS": _stats(4, 50.0),        # 표본 3~4건 → 보정 없음 표시
        "NVDA": _stats(1, 0.0),              # 표본 < 3 → 미표시
    })
    text = mem.reliability_directives_text()
    assert "LMT" in text and "+10% 가중 허용" in text
    assert "161510.KS" in text and "-15% 감점 필수" in text
    assert "005930.KS" in text and "보정 없음" in text
    assert "NVDA" not in text


def test_reliability_directives_empty(monkeypatch):
    import core.memory as mem

    monkeypatch.setattr(mem, "get_accuracy_summary", lambda: {})
    assert mem.reliability_directives_text() == ""


def test_reliability_risk_needs_8_for_minus_30(monkeypatch):
    import core.memory as mem

    monkeypatch.setattr(mem, "get_accuracy_summary", lambda: {
        "BAD": _stats(9, 10.0, -8.0),
    })
    assert "-30% 감점 필수" in mem.reliability_directives_text()


# ─── ④ confidence_calibration_text ──────────────────────────


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        rows = self._rows
        return SimpleNamespace(fetchall=lambda: rows)


def _pred_row(confidence, outcome):
    return {"confidence": confidence, "outcome": outcome}


def test_calibration_detects_overconfidence(monkeypatch):
    import core.memory as mem

    # 확신도 71-85 구간 10건 중 3승 (실측 30% vs 구간 중앙 78) → 과신
    rows = [_pred_row(75, "win")] * 3 + [_pred_row(80, "loss")] * 7
    monkeypatch.setattr(mem, "_get_conn", lambda: _FakeConn(rows))
    text = mem.confidence_calibration_text()
    assert "71-85" in text
    assert "과신" in text


def test_calibration_skips_small_buckets(monkeypatch):
    import core.memory as mem

    rows = [_pred_row(75, "win")] * 3  # 3건 < min_bucket_n=5
    monkeypatch.setattr(mem, "_get_conn", lambda: _FakeConn(rows))
    assert mem.confidence_calibration_text() == ""


def test_calibration_match(monkeypatch):
    import core.memory as mem

    # 56-70 구간 10건 중 6승 (60% vs 중앙 63) → 실측 부합
    rows = [_pred_row(60, "win")] * 6 + [_pred_row(65, "loss")] * 4
    monkeypatch.setattr(mem, "_get_conn", lambda: _FakeConn(rows))
    text = mem.confidence_calibration_text()
    assert "실측 부합" in text


# ─── ⑤ INVALIDATION 트리거 ──────────────────────────────────


def _invalidation_trigger():
    return AlertTrigger(
        ticker="123450.KS",
        name="테스트발굴주 (무효화: 손절가 이탈)",
        trigger_type=TriggerType.INVALIDATION,
        current_value=9_000.0,
        threshold=9_500.0,
        timestamp=datetime.now(KST),
        market_session="KR_REGULAR",
    )


def test_invalidation_severity_warning():
    monitor = MarketMonitor()
    assert monitor._classify_severity(_invalidation_trigger()) == Severity.WARNING


def test_invalidation_skips_ai_analysis(monkeypatch):
    monitor = MarketMonitor()
    called = []
    monkeypatch.setattr(monitor, "_ai_analyze", lambda *a, **k: called.append(1) or "x")
    result = monitor._process_trigger(_invalidation_trigger())
    assert called == []
    assert result.ai_analysis == ""
    assert result.severity == Severity.WARNING


def test_invalidation_is_actionable_without_order_fields():
    monitor = MarketMonitor()
    result = AlertResult(trigger=_invalidation_trigger(), severity=Severity.WARNING)
    assert monitor._is_actionable(result) is True


def test_invalidation_message_says_cancel():
    result = AlertResult(trigger=_invalidation_trigger(), severity=Severity.WARNING)
    msg = _build_alert_message(result)
    assert "예약 취소 액션" in msg
    assert "예약매수 주문을 취소" in msg
    assert "무효화 조건 도달" in msg


def test_invalidation_description():
    t = _invalidation_trigger()
    assert "무효화 조건 도달" in t.description
    assert "9,000" in t.description and "9,500" in t.description


# ─── ② cross_check_prices + 게이트 통합 ─────────────────────


def test_cross_check_flags_mismatch(monkeypatch):
    import core.market as market

    def fake_yf(ticker):
        return SimpleNamespace(price=100.0)

    monkeypatch.setattr(market, "_get_quote_yf_live", fake_yf)
    prices = {"AAA": 104.0, "BBB": 100.5, "^KS11": 3000.0}
    out = market.cross_check_prices(prices)
    assert "AAA" in out          # 4% 괴리 → 플래그
    assert "BBB" not in out      # 0.5% → 정상
    assert "^KS11" not in out    # 지수 → 검증 제외


def test_cross_check_secondary_failure_is_silent(monkeypatch):
    import core.market as market

    monkeypatch.setattr(market, "_get_quote_yf_live", lambda t: None)
    assert market.cross_check_prices({"AAA": 104.0}) == {}


def test_gate_warns_on_mismatch_without_limiting():
    from core.data_quality_gate import STATUS_WARNING, assess_data_quality

    snapshot = SimpleNamespace(
        stocks={}, indices={},
        macro={"원달러(₩)": SimpleNamespace(ticker="USDKRW=X", price=1450.0)},
        news={},
    )
    prices = {"005930.KS": 62_000.0, "USDKRW=X": 1450.0}
    r = assess_data_quality(
        snapshot, prices, list(prices),
        {"005930.KS": {"shares": 10, "avg_cost_krw": 60_000}},
        price_cross_check={"005930.KS": (62_000.0, 64_500.0, 4.0)},
    )
    assert r.status == STATUS_WARNING
    assert r.execution_limited is False
    assert any("소스 불일치" in w for w in r.warnings)
    assert r.source_mismatches


def test_gate_limits_on_critical_mismatch():
    from core.data_quality_gate import STATUS_LIMITED, assess_data_quality

    snapshot = SimpleNamespace(
        stocks={}, indices={},
        macro={"원달러(₩)": SimpleNamespace(ticker="USDKRW=X", price=1450.0)},
        news={},
    )
    prices = {"005930.KS": 62_000.0, "USDKRW=X": 1450.0}
    r = assess_data_quality(
        snapshot, prices, list(prices),
        {"005930.KS": {"shares": 10, "avg_cost_krw": 60_000}},
        price_cross_check={"005930.KS": (62_000.0, 6_200.0, 900.0)},  # 자릿수 오류급
    )
    assert r.status == STATUS_LIMITED
    assert r.execution_limited is True
    assert any("교차검증" in s for s in r.failed_sources)
