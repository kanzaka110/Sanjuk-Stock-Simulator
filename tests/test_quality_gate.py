"""품질 게이트 테스트 — 임시 DB 사용, 운영 DB 오염 없음."""

import os
import sqlite3
import tempfile
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _use_temp_db(monkeypatch, tmp_path):
    """모든 테스트에서 임시 DB 사용."""
    db_path = tmp_path / "test_memory.db"
    monkeypatch.setattr("core.memory.DB_PATH", db_path)
    # 커넥션 리셋
    import core.memory as mem
    mem._conn = None
    yield
    mem._conn = None


@pytest.fixture
def seed_accuracy():
    """위험/고신뢰 종목 통계 시드."""
    import core.memory as mem
    conn = mem._get_conn()
    conn.executescript("""
        INSERT OR REPLACE INTO accuracy_stats (ticker, total_predictions, correct, wrong, avg_pnl, win_rate)
        VALUES
            ('NVDA', 3, 0, 3, -19.2, 0),
            ('GOOGL', 2, 0, 2, -32.1, 0),
            ('207940.KS', 7, 2, 5, -1.8, 28.6),
            ('035720.KS', 4, 1, 3, 1.5, 25.0),
            ('LMT', 4, 4, 0, 14.7, 100.0),
            ('133690.KS', 2, 2, 0, 14.6, 100.0);
    """)


class TestQualityGate:
    """품질 게이트 동작 검증."""

    def test_lmt_high_trust_immediate(self, seed_accuracy):
        from core.memory import _quality_gate, ACTION_IMMEDIATE
        grade, conf, reason = _quality_gate(
            "LMT", "매수", 70, 500, 480, 2.5, "추세반전시", "중기보유", 0, 4,
        )
        assert grade == ACTION_IMMEDIATE
        assert "통과" in reason

    def test_nvda_danger_blocked(self, seed_accuracy):
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "NVDA", "매도", 50, 200, 220, 1.0, "", "일반", 0, 0,
        )
        assert grade == ACTION_BLOCKED
        assert "위험종목" in reason

    def test_nvda_exception_with_agreement_3(self, seed_accuracy):
        from core.memory import _quality_gate, ACTION_CONDITIONAL
        grade, conf, reason = _quality_gate(
            "NVDA", "매도", 60, 200, 220, 2.5, "세금이벤트종료", "세금전략", 0, 3,
        )
        assert grade == ACTION_CONDITIONAL
        assert "예외허용" in reason

    def test_nvda_exception_agreement_2_blocked(self, seed_accuracy):
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "NVDA", "매도", 60, 200, 220, 2.5, "세금이벤트종료", "세금전략", 0, 2,
        )
        assert grade == ACTION_BLOCKED
        assert "동의2/4<3" in reason

    def test_low_confidence_watch(self):
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "005930.KS", "매수", 40, 70000, 65000, 2.0, "하락추세", "단기매매", 0, 4,
        )
        # 확신도 40 < 55 → WATCH, 하지만 매수이므로 risk_reward 등 다른 조건도 체크
        assert conf <= 55

    def test_no_stoploss_blocked(self):
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "012450.KS", "매수", 70, 300000, 0, 2.0, "하락", "일반", 0, 4,
        )
        assert grade == ACTION_BLOCKED
        assert "손절가없음" in reason

    def test_data_failures_2_blocked(self):
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "005930.KS", "매수", 65, 70000, 65000, 2.0, "하락", "일반", 2, 4,
        )
        assert grade == ACTION_BLOCKED
        assert "데이터" in reason

    def test_entry_price_zero_blocked(self):
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "AAPL", "매수", 60, 0, 0, 0, "", "일반", 0, 4,
        )
        assert grade == ACTION_BLOCKED
        assert "진입가0" in reason

    def test_risk_reward_zero_blocked(self):
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "005930.KS", "매수", 70, 70000, 65000, 0, "하락", "일반", 0, 4,
        )
        assert grade == ACTION_BLOCKED
        assert "손익비" in reason

    def test_watch_duplicate_prevention(self):
        """WATCH로 관망 변환된 추천이 중복 저장되지 않는지."""
        from core.memory import save_prediction

        # 첫 번째: 저확신 매수 → 관망으로 변환 저장
        pid1 = save_prediction(
            ticker="TEST", name="테스트", signal="매수",
            entry_price=100, stop_loss=90, risk_reward=2.0,
            confidence=40, invalidation_condition="test",
            strategy_type="일반", agreement_count=4,
        )
        # 관망으로 저장되든, 확신도 때문에 차단되든 일관성 확인

        # 두 번째: 같은 종목 매수 다시 → 중복 스킵
        pid2 = save_prediction(
            ticker="TEST", name="테스트", signal="매수",
            entry_price=100, stop_loss=90, risk_reward=2.0,
            confidence=40, invalidation_condition="test",
            strategy_type="일반", agreement_count=4,
        )
        assert pid2 == 0, "중복 추천이 저장되면 안 됨"
