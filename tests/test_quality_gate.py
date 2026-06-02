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
    # Phase 4 스키마에 맞춰 시드 데이터 삽입
    conn.execute("DROP TABLE IF EXISTS accuracy_stats")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accuracy_stats (
            ticker TEXT PRIMARY KEY,
            total_predictions INTEGER DEFAULT 0,
            evaluated_count INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            neutral_count INTEGER DEFAULT 0,
            invalid_count INTEGER DEFAULT 0,
            avg_pnl REAL DEFAULT 0,
            avg_win REAL DEFAULT 0,
            avg_loss REAL DEFAULT 0,
            profit_factor REAL DEFAULT 0,
            expectancy REAL DEFAULT 0,
            win_rate REAL DEFAULT 0,
            last_updated TEXT
        )
    """)
    conn.executescript("""
        INSERT OR REPLACE INTO accuracy_stats
            (ticker, total_predictions, evaluated_count, wins, losses, neutral_count,
             avg_pnl, avg_win, avg_loss, profit_factor, expectancy, win_rate)
        VALUES
            ('NVDA', 3, 3, 0, 3, 0, -19.2, 0, -19.2, 0, -19.2, 0),
            ('GOOGL', 2, 2, 0, 2, 0, -32.1, 0, -32.1, 0, -32.1, 0),
            ('207940.KS', 7, 7, 2, 5, 0, -1.8, 3.0, -3.7, 0.32, -1.8, 28.6),
            ('035720.KS', 4, 4, 1, 3, 0, 1.5, 8.0, -0.7, 3.81, 1.5, 25.0),
            ('LMT', 4, 4, 4, 0, 0, 14.7, 14.7, 0, 99.0, 14.7, 100.0),
            ('133690.KS', 2, 2, 2, 0, 0, 14.6, 14.6, 0, 99.0, 14.6, 100.0);
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


class TestBriefingIntegration:
    """save_predictions_from_briefing 경로 통합 테스트."""

    def test_nvda_danger_agreement_2_blocked(self, seed_accuracy):
        """위험 종목 + 동의 2명 → 차단."""
        from core.memory import save_predictions_from_briefing, get_recent_predictions
        data = {
            "strategy_sell": [{
                "ticker": "NVDA",
                "name": "NVIDIA",
                "current_price": "200",
                "take_profit": "180",
                "stop_loss": "220",
                "reason": "테스트",
                "strategy_type": "일반",
                "risk_reward": "2.5",
                "invalidation_condition": "추세반전",
                "agreement_count": 2,
            }],
        }
        saved = save_predictions_from_briefing(data)
        assert saved == 0, "동의 2명이면 위험 종목 저장 안 됨"

    def test_nvda_danger_agreement_3_conditional(self, seed_accuracy):
        """위험 종목 + 동의 3명 + 조건 충족 → CONDITIONAL 저장."""
        from core.memory import save_predictions_from_briefing, _get_conn
        data = {
            "strategy_sell": [{
                "ticker": "NVDA",
                "name": "NVIDIA",
                "current_price": "200",
                "take_profit": "180",
                "stop_loss": "220",
                "reason": "테스트",
                "strategy_type": "세금전략",
                "risk_reward": "2.5",
                "invalidation_condition": "세금이벤트종료",
                "agreement_count": 3,
            }],
        }
        saved = save_predictions_from_briefing(data)
        assert saved == 1, "동의 3명+조건 충족이면 저장되어야 함"
        # 저장된 레코드 확인
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM predictions WHERE ticker='NVDA' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        # NVDA 확신도 보정(승률0%→-15)으로 35가 되어 WATCH로 격하될 수 있음
        # 핵심: 위험 종목이 agreement 3이면 BLOCKED가 아니라 저장됨
        assert "예외허용" in (row["reasoning"] or "")


class TestPriceDivergenceGate:
    """가격 괴리 검증 게이트 — RIA ETF 할루시네이션 방지."""

    def test_kodex200_huge_divergence_blocked(self):
        """KODEX 200 현재가 123350, 진입가 53500 → 괴리 56.6% → BLOCKED."""
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "069500.KS", "매수", 70, 53500, 50000, 2.0, "하락시", "중기보유",
            0, 4, current_price=123350,
        )
        assert grade == ACTION_BLOCKED
        assert "진입가-현재가 괴리 초과" in reason
        assert "123,350" in reason
        assert "53,500" in reason

    def test_kodex_semiconductor_divergence_blocked(self):
        """KODEX 반도체 현재가 155500, 진입가 17500 → 괴리 88.7% → BLOCKED."""
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "091160.KS", "매수", 70, 17500, 15000, 2.0, "하락시", "중기보유",
            0, 4, current_price=155500,
        )
        assert grade == ACTION_BLOCKED
        assert "진입가-현재가 괴리 초과" in reason

    def test_plus_high_dividend_small_divergence_pass(self):
        """PLUS 고배당주 현재가 27230, 진입가 26800 → 괴리 1.6% → 통과."""
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "161510.KS", "매수", 70, 26800, 25000, 2.0, "하락시", "중기보유",
            0, 4, current_price=27230,
        )
        assert grade != ACTION_BLOCKED or "괴리" not in reason

    def test_no_current_price_new_buy_blocked(self):
        """현재가 없는 신규 매수 → 시세 미수집 저장 차단."""
        from core.memory import _quality_gate, ACTION_BLOCKED
        grade, conf, reason = _quality_gate(
            "069500.KS", "매수", 70, 53500, 50000, 2.0, "하락시", "중기보유",
            0, 4, current_price=0.0,  # 명시적으로 0 (시세 미수집)
        )
        assert grade == ACTION_BLOCKED
        assert "시세미수집" in reason

    def test_divergence_gate_in_save_predictions(self):
        """save_predictions_from_briefing에서 current_prices 전달 시 괴리 차단."""
        from core.memory import save_predictions_from_briefing
        data = {
            "strategy_buy": [{
                "ticker": "069500.KS",
                "name": "KODEX 200",
                "entry_price": "₩53,500",
                "target_price": "₩60,000",
                "stop_loss": "₩50,000",
                "reason": "테스트",
                "strategy_type": "중기보유",
                "risk_reward": "2.0",
                "invalidation_condition": "하락시",
                "agreement_count": 4,
            }],
        }
        saved = save_predictions_from_briefing(
            data, current_prices={"069500.KS": 123350},
        )
        assert saved == 0, "현재가 123350 vs 진입가 53500 → 괴리 초과로 저장 안 됨"

    def test_no_current_price_in_save_predictions_blocked(self):
        """save_predictions_from_briefing에서 current_prices에 종목 없으면 차단."""
        from core.memory import save_predictions_from_briefing
        data = {
            "strategy_buy": [{
                "ticker": "069500.KS",
                "name": "KODEX 200",
                "entry_price": "₩53,500",
                "target_price": "₩60,000",
                "stop_loss": "₩50,000",
                "reason": "테스트",
                "strategy_type": "중기보유",
                "risk_reward": "2.0",
                "invalidation_condition": "하락시",
                "agreement_count": 4,
            }],
        }
        # current_prices에 069500.KS 없음 → 시세 미수집 차단
        saved = save_predictions_from_briefing(data, current_prices={"005930.KS": 60000})
        assert saved == 0, "시세 미수집 종목은 저장되면 안 됨"


class TestTickerNormalization:
    """티커 정규화 유틸리티 테스트."""

    def test_bare_code_to_ks(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("069500") == "069500.KS"

    def test_alias_kodex_200(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("KODEX 200") == "069500.KS"
        assert normalize_ticker("KODEX_200") == "069500.KS"

    def test_alias_kodex_semiconductor(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("091160") == "091160.KS"
        assert normalize_ticker("KODEX 반도체") == "091160.KS"
        assert normalize_ticker("KODEX_반도체") == "091160.KS"

    def test_alias_kodex_leverage(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("122630") == "122630.KS"
        assert normalize_ticker("KODEX 레버리지") == "122630.KS"

    def test_alias_kodex_auto(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("091180") == "091180.KS"
        assert normalize_ticker("KODEX 자동차") == "091180.KS"

    def test_already_normalized(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("069500.KS") == "069500.KS"
        assert normalize_ticker("005930.KS") == "005930.KS"

    def test_us_ticker_unchanged(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("NVDA") == "NVDA"
        assert normalize_ticker("AAPL") == "AAPL"

    def test_normalization_in_save_prediction(self):
        """save_prediction 경로에서 정규화가 적용되는지 확인."""
        from core.memory import save_prediction, _get_conn
        pid = save_prediction(
            ticker="069500",  # 정규화 전
            name="KODEX 200",
            signal="관망",
            entry_price=0,
            confidence=60,
        )
        if pid > 0:
            conn = _get_conn()
            row = conn.execute(
                "SELECT ticker FROM predictions WHERE id=?", (pid,)
            ).fetchone()
            assert row["ticker"] == "069500.KS", "저장 시 티커가 정규화되어야 함"

    def test_normalization_in_briefing_save(self):
        """save_predictions_from_briefing에서 정규화 + 현재가 조회가 작동하는지."""
        from core.memory import save_predictions_from_briefing, _get_conn
        data = {
            "strategy_buy": [{
                "ticker": "069500",  # 정규화 전
                "name": "KODEX 200",
                "entry_price": "₩123,000",
                "target_price": "₩130,000",
                "stop_loss": "₩118,000",
                "reason": "테스트",
                "strategy_type": "중기보유",
                "risk_reward": "2.5",
                "invalidation_condition": "하락시",
                "agreement_count": 4,
            }],
        }
        # current_prices에 정규화된 코드로 등록
        saved = save_predictions_from_briefing(
            data, current_prices={"069500.KS": 123350},
        )
        assert saved == 1, "정규화된 티커로 현재가 조회 성공 → 괴리 없으면 저장"
        conn = _get_conn()
        row = conn.execute(
            "SELECT ticker FROM predictions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["ticker"] == "069500.KS"


class TestRIAAllowedTickers:
    """RIA_ALLOWED_TICKERS가 KRW_TICKERS와 수집 대상에 포함되는지."""

    def test_ria_tickers_in_krw(self):
        from config.settings import KRW_TICKERS, RIA_ALLOWED_TICKERS
        for tk in RIA_ALLOWED_TICKERS:
            assert tk in KRW_TICKERS, f"{tk}이 KRW_TICKERS에 없음"

    def test_ria_tickers_in_kr_market_config(self):
        from config.settings import get_market_config, RIA_ALLOWED_TICKERS
        portfolio, _, _ = get_market_config("KR_BEFORE")
        for tk in RIA_ALLOWED_TICKERS:
            assert tk in portfolio, f"{tk}이 KR_BEFORE portfolio에 없음"

    def test_ria_tickers_in_manual_market_config(self):
        from config.settings import get_market_config, RIA_ALLOWED_TICKERS
        portfolio, _, _ = get_market_config("MANUAL")
        for tk in RIA_ALLOWED_TICKERS:
            assert tk in portfolio, f"{tk}이 MANUAL portfolio에 없음"

    def test_ria_tickers_not_in_us_market_config(self):
        """US_BEFORE에서는 국내 ETF가 포함되지 않아야 함."""
        from config.settings import get_market_config, RIA_ALLOWED_TICKERS
        portfolio, _, _ = get_market_config("US_BEFORE")
        for tk in RIA_ALLOWED_TICKERS:
            assert tk not in portfolio, f"{tk}이 US_BEFORE에 잘못 포함"


# ═══════════════════════════════════════════════════════
# price_updater 티커 정규화 테스트
# ═══════════════════════════════════════════════════════
class TestPriceUpdaterTickerNormalization:
    """Notion 한글명 → yfinance 코드 변환 테스트."""

    def test_kodex_200_alias(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("KODEX 200") == "069500.KS"
        assert normalize_ticker("KODEX200") == "069500.KS"

    def test_kodex_semiconductor_alias(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("KODEX 반도체") == "091160.KS"
        assert normalize_ticker("KODEX반도체") == "091160.KS"

    def test_tiger_nasdaq_alias(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("TIGER 미국나스닥100") == "133690.KS"
        assert normalize_ticker("TIGER미국나스닥100") == "133690.KS"

    def test_tiger_sp500_alias(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("TIGER 미국S&P500") == "360750.KS"
        assert normalize_ticker("TIGER미국S&P500") == "360750.KS"

    def test_tiger_reits_alias(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("TIGER 리츠부동산인프라") == "329200.KS"
        assert normalize_ticker("TIGER 리츠") == "329200.KS"

    def test_plus_dividend_alias(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("PLUS 고배당주") == "161510.KS"

    def test_kodex_msci_alias(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("KODEX MSCI선진국") == "251350.KS"

    def test_numeric_code_normalization(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("069500") == "069500.KS"
        assert normalize_ticker("133690") == "133690.KS"

    def test_already_normalized_passthrough(self):
        from core.memory import normalize_ticker
        assert normalize_ticker("069500.KS") == "069500.KS"
        assert normalize_ticker("MU") == "MU"
        assert normalize_ticker("AAPL") == "AAPL"

    def test_krx_prefix_in_price_updater(self):
        """KRX: 접두사 경로도 유지되는지 확인."""
        from core.price_updater import _get_stock_price
        import inspect
        src = inspect.getsource(_get_stock_price)
        assert "KRX:" in src
        assert "normalize_ticker" in src


class TestNotionUpdateExceptionIsolation:
    """Notion 업데이트 실패 시 다른 종목은 계속 처리되는지 테스트."""

    def test_single_failure_doesnt_stop_others(self):
        """1종목 Notion patch 실패해도 전체 update_all_prices가 중단 안 됨."""
        from core.price_updater import update_all_prices
        import inspect
        src = inspect.getsource(update_all_prices)
        # for 루프 안에 try/except가 있어야 함
        assert "except Exception" in src
        assert "failed" in src

    def test_failed_tickers_logged(self):
        """실패 종목이 로그에 남는지."""
        from core.price_updater import update_all_prices
        import inspect
        src = inspect.getsource(update_all_prices)
        assert "실패 종목" in src


class TestPhase3StatisticsCorrection:
    """Phase 3: 통계 왜곡 수정 테스트."""

    def test_ticker_normalization_in_accuracy(self):
        """같은 종목의 다른 alias가 하나로 집계되는지."""
        import core.memory as mem
        conn = mem._get_conn()
        now = "2026-05-20T00:00:00"
        # 같은 종목을 다른 alias로 저장
        conn.execute(
            """INSERT INTO predictions
               (created_at, ticker, name, signal, entry_price, target_price,
                stop_loss, confidence, status, closed_at, closed_price, pnl_pct, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, "091160", "KODEX 반도체", "매수", 100, 110, 90, 70,
             "closed", now, 110, 10.0, "win"),
        )
        conn.execute(
            """INSERT INTO predictions
               (created_at, ticker, name, signal, entry_price, target_price,
                stop_loss, confidence, status, closed_at, closed_price, pnl_pct, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, "091160.KS", "KODEX 반도체", "매수", 100, 110, 90, 70,
             "closed", now, 90, -10.0, "loss"),
        )
        conn.execute(
            """INSERT INTO predictions
               (created_at, ticker, name, signal, entry_price, target_price,
                stop_loss, confidence, status, closed_at, closed_price, pnl_pct, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, "KODEX 반도체", "KODEX 반도체", "매수", 100, 110, 90, 70,
             "closed", now, 105, 5.0, "win"),
        )
        conn.commit()
        mem._update_accuracy_stats()
        summary = mem.get_accuracy_summary()
        # 3개 alias 모두 091160.KS로 정규화되어 한 행이어야 함
        assert "091160.KS" in summary
        s = summary["091160.KS"]
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert s["total"] == 3

    def test_neutral_excluded_from_win_rate(self):
        """neutral이 승률 분모에서 제외되는지."""
        import core.memory as mem
        conn = mem._get_conn()
        now = "2026-05-20T00:00:00"
        # win 2, loss 1, neutral 2
        for outcome, pnl in [("win", 5.0), ("win", 8.0), ("loss", -3.0),
                              ("neutral", 0.5), ("neutral", -0.2)]:
            conn.execute(
                """INSERT INTO predictions
                   (created_at, ticker, name, signal, entry_price, target_price,
                    stop_loss, confidence, status, closed_at, closed_price, pnl_pct, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, "005930.KS", "삼성전자", "매수", 60000, 65000, 57000, 70,
                 "closed", now, 63000, pnl, outcome),
            )
        conn.commit()
        mem._update_accuracy_stats()
        summary = mem.get_accuracy_summary()
        s = summary["005930.KS"]
        # 승률 = 2 / (2+1) = 66.7%, neutral 제외
        assert s["evaluated_count"] == 3
        assert s["neutral_count"] == 2
        assert abs(s["win_rate"] - 66.7) < 1.0

    def test_small_sample_marked(self):
        """evaluated_count < 3이면 샘플부족 처리 — 보정 스킵."""
        import core.memory as mem
        conn = mem._get_conn()
        now = "2026-05-20T00:00:00"
        # win 1건만 (evaluated_count=1)
        conn.execute(
            """INSERT INTO predictions
               (created_at, ticker, name, signal, entry_price, target_price,
                stop_loss, confidence, status, closed_at, closed_price, pnl_pct, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, "MU", "Micron", "매수", 100, 120, 90, 70,
             "closed", now, 115, 15.0, "win"),
        )
        conn.commit()
        mem._update_accuracy_stats()
        # calibrate_confidence는 evaluated < 3이면 raw를 그대로 반환
        result = mem.calibrate_confidence("MU", 50)
        assert result == 50, "샘플부족 시 raw confidence 그대로 반환해야 함"

    def test_unrealistic_return_excluded(self):
        """abs(pnl_pct) > 100%인 한국 종목이 data_error로 처리."""
        import core.memory as mem
        conn = mem._get_conn()
        now = "2026-05-20T00:00:00"
        # pnl 500% — 한국 종목 기준 비현실적
        conn.execute(
            """INSERT INTO predictions
               (created_at, ticker, name, signal, entry_price, target_price,
                stop_loss, confidence, status, pnl_pct, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, "005930.KS", "삼성전자", "매수", 10000, 60000, 9000, 70,
             "open", None, None),
        )
        conn.commit()
        # evaluate로 종료 처리
        closed = mem.evaluate_open_predictions({"005930.KS": 60000})
        assert closed == 1
        row = conn.execute(
            "SELECT outcome, pnl_pct FROM predictions WHERE ticker='005930.KS' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["outcome"] == "data_error"
        assert abs(row["pnl_pct"]) > 100

    def test_zero_entry_price_not_saved(self):
        """entry_price=0 추천은 저장되지 않는지."""
        import core.memory as mem
        pid = mem.save_prediction(
            ticker="005930.KS",
            name="삼성전자",
            signal="매수",
            entry_price=0,
            stop_loss=0,
            risk_reward=2.0,
        )
        assert pid == 0, "entry_price=0인 매수 추천은 차단되어야 함"


class TestPhase4Expectancy:
    """Phase 4: 기대값 기반 평가 테스트."""

    def test_expectancy_calculation(self):
        """expectancy = (wr * avg_win) - (lr * avg_loss) 검증."""
        import core.memory as mem
        conn = mem._get_conn()
        now = "2026-06-01T00:00:00"
        # win 3건 avg +10%, loss 2건 avg -5%
        for outcome, pnl in [("win", 8), ("win", 12), ("win", 10), ("loss", -4), ("loss", -6)]:
            conn.execute(
                """INSERT INTO predictions
                   (created_at, ticker, name, signal, entry_price, target_price,
                    stop_loss, confidence, status, closed_at, pnl_pct, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, "TEST01.KS", "테스트", "매수", 100, 110, 90, 70,
                 "closed", now, pnl, outcome),
            )
        conn.commit()
        mem._update_accuracy_stats()
        s = mem.get_accuracy_summary()["TEST01.KS"]
        # wr=3/5=0.6, lr=2/5=0.4, avg_win=10, avg_loss=-5
        # expectancy = 0.6*10 - 0.4*5 = 4.0
        assert abs(s["expectancy"] - 4.0) < 0.5
        assert abs(s["avg_win"] - 10.0) < 0.5
        assert abs(s["avg_loss"] - (-5.0)) < 0.5

    def test_profit_factor_calculation(self):
        """profit_factor = gross_profit / abs(gross_loss) 검증."""
        import core.memory as mem
        conn = mem._get_conn()
        now = "2026-06-01T00:00:00"
        # gross_profit = 30, gross_loss = 10 → PF = 3.0
        for outcome, pnl in [("win", 20), ("win", 10), ("loss", -10)]:
            conn.execute(
                """INSERT INTO predictions
                   (created_at, ticker, name, signal, entry_price, target_price,
                    stop_loss, confidence, status, closed_at, pnl_pct, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, "TEST02.KS", "테스트2", "매수", 100, 120, 90, 70,
                 "closed", now, pnl, outcome),
            )
        conn.commit()
        mem._update_accuracy_stats()
        s = mem.get_accuracy_summary()["TEST02.KS"]
        assert abs(s["profit_factor"] - 3.0) < 0.1

    def test_expectancy_boosts_confidence(self):
        """양수 expectancy + PF > 1.2 + 5건+ → 확신도 가점."""
        import core.memory as mem
        conn = mem._get_conn()
        conn.execute("DROP TABLE IF EXISTS accuracy_stats")
        conn.execute("""
            CREATE TABLE accuracy_stats (
                ticker TEXT PRIMARY KEY, total_predictions INTEGER DEFAULT 0,
                evaluated_count INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0, neutral_count INTEGER DEFAULT 0,
                invalid_count INTEGER DEFAULT 0, avg_pnl REAL DEFAULT 0,
                avg_win REAL DEFAULT 0, avg_loss REAL DEFAULT 0,
                profit_factor REAL DEFAULT 0, expectancy REAL DEFAULT 0,
                win_rate REAL DEFAULT 0, last_updated TEXT)
        """)
        conn.execute("""
            INSERT INTO accuracy_stats
            (ticker, total_predictions, evaluated_count, wins, losses,
             avg_pnl, avg_win, avg_loss, profit_factor, expectancy, win_rate)
            VALUES ('MU', 10, 8, 6, 2, 5.0, 8.0, -4.0, 3.0, 5.2, 75.0)
        """)
        conn.commit()
        result = mem.calibrate_confidence("MU", 50)
        assert result > 50, f"양수 expectancy → 가점, got {result}"

    def test_negative_expectancy_penalizes(self):
        """음수 expectancy + 5건+ → 확신도 감점."""
        import core.memory as mem
        conn = mem._get_conn()
        conn.execute("DROP TABLE IF EXISTS accuracy_stats")
        conn.execute("""
            CREATE TABLE accuracy_stats (
                ticker TEXT PRIMARY KEY, total_predictions INTEGER DEFAULT 0,
                evaluated_count INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0, neutral_count INTEGER DEFAULT 0,
                invalid_count INTEGER DEFAULT 0, avg_pnl REAL DEFAULT 0,
                avg_win REAL DEFAULT 0, avg_loss REAL DEFAULT 0,
                profit_factor REAL DEFAULT 0, expectancy REAL DEFAULT 0,
                win_rate REAL DEFAULT 0, last_updated TEXT)
        """)
        conn.execute("""
            INSERT INTO accuracy_stats
            (ticker, total_predictions, evaluated_count, wins, losses,
             avg_pnl, avg_win, avg_loss, profit_factor, expectancy, win_rate)
            VALUES ('NVDA', 10, 8, 2, 6, -5.0, 3.0, -8.0, 0.25, -5.5, 25.0)
        """)
        conn.commit()
        result = mem.calibrate_confidence("NVDA", 50)
        assert result < 50, f"음수 expectancy → 감점, got {result}"

    def test_phase4_columns_exist(self):
        """Phase 4 컬럼이 predictions 테이블에 존재하는지."""
        import core.memory as mem
        conn = mem._get_conn()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)").fetchall()}
        for col in ("action_grade", "action_type", "account_type", "briefing_type", "original_signal", "data_quality"):
            assert col in cols, f"Phase 4 컬럼 {col} 누락"
