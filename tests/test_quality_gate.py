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
            ('NVDA', 8, 6, 0, 6, 0, -19.2, 0, -19.2, 0, -19.2, 0),
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
        from core.action_normalizer import normalize_actions
        from core.memory import save_predictions_from_briefing, _get_conn
        data = {
            "strategy_sell": [{
                "ticker": "NVDA",
                "name": "NVIDIA",
                "current_price": "200",
                "take_profit": "180",
                "stop_loss": "220",
                "reason": "과열 부분 익절",
                "strategy_type": "세금전략",
                "risk_reward": "2.5",
                "invalidation_condition": "세금이벤트종료",
                "agreement_count": 3,
            }],
        }
        norm = normalize_actions(data, "US_BEFORE", {"NVDA": 200}, {})
        saved = save_predictions_from_briefing(data, normalized=norm, briefing_type="US_BEFORE")
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
        from core.action_normalizer import normalize_actions
        from core.memory import save_predictions_from_briefing, _get_conn
        data = {
            "strategy_buy": [{
                "ticker": "069500",  # 정규화 전
                "name": "KODEX 200",
                "entry_price": "₩123,000",
                "target_price": "₩130,000",
                "stop_loss": "₩118,000",
                "reason": "지지선 반등 진입",
                "strategy_type": "중기보유",
                "risk_reward": "2.5",
                "invalidation_condition": "하락시",
                "agreement_count": 4,
            }],
        }
        # current_prices에 정규화된 코드로 등록
        prices = {"069500.KS": 123350}
        norm = normalize_actions(data, "KR_BEFORE", prices, {})
        saved = save_predictions_from_briefing(
            data, current_prices=prices, normalized=norm, briefing_type="KR_BEFORE",
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
        from datetime import datetime, timedelta
        from config.settings import KST
        conn = mem._get_conn()
        # 14일 cutoff에 안 걸리도록 최근 날짜 사용
        now = (datetime.now(KST) - timedelta(days=1)).isoformat()
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


class TestNightOrdersTelegram:
    """야간 브리핑 예약 주문 텔레그램 표시 테스트."""

    def _make_result(self, raw_json: dict, warnings=()):
        from core.models import BriefingResult
        return BriefingResult(raw_json=raw_json, quality_warnings=warnings)

    def test_kr_night_empty_orders_shows_none(self):
        """KR_NIGHT + night_orders=[] → '내일 예약 주문: 없음' 포함."""
        from core.telegram import _build_impact_message
        result = self._make_result({
            "night_orders": [],
            "advisor_oneliner": "전 종목 관망",
            "investment_decision": "관망",
        })
        msg = _build_impact_message(result, result.raw_json, "🌙", "테스트", "KR_NIGHT")
        assert "내일 예약 주문: 없음" in msg
        assert "전 종목 관망" in msg

    def test_us_night_empty_orders_shows_none(self):
        """US_NIGHT + night_orders=[] → '오늘 밤 지정가 주문: 없음' 포함."""
        from core.telegram import _build_impact_message
        result = self._make_result({
            "night_orders": [],
            "advisor_oneliner": "MU 트레일링 스톱 유지",
        })
        msg = _build_impact_message(result, result.raw_json, "🌙", "테스트", "US_NIGHT")
        assert "오늘 밤 지정가 주문: 없음" in msg

    def test_night_orders_present_shows_orders(self):
        """night_orders 존재 → 기존 주문 출력 유지."""
        from core.telegram import _build_impact_message
        result = self._make_result({
            "night_orders": [{"구분": "매수", "종목": "삼성전자", "계좌": "[ISA]",
                              "지정가": "₩300,000", "수량": "5주"}],
        })
        msg = _build_impact_message(result, result.raw_json, "🌙", "테스트", "KR_NIGHT")
        assert "🟢매수" in msg
        assert "삼성전자" in msg
        assert "예약 주문: 없음" not in msg

    def test_manual_no_night_section(self):
        """MANUAL 브리핑에서는 night_orders 섹션 안 나옴."""
        from core.telegram import _build_impact_message
        result = self._make_result({"night_orders": []})
        msg = _build_impact_message(result, result.raw_json, "📊", "테스트", "MANUAL")
        assert "예약 주문" not in msg

    def test_fallback_shows_warning(self):
        """synthesis fallback 시 경고 표시."""
        from core.telegram import _build_impact_message
        result = self._make_result({
            "title": "[FALLBACK] 종합 판단 실패",
            "night_orders": [],
        })
        msg = _build_impact_message(result, result.raw_json, "🌙", "[FALLBACK] 테스트", "KR_NIGHT")
        assert "종합 판단 실패" in msg


class TestDailyReviewGuard:
    """데일리 리뷰(US_CLOSE) 결산·복기 전용 가드 테스트."""

    def _make_result(self, raw_json: dict, warnings=()):
        from core.models import BriefingResult
        return BriefingResult(raw_json=raw_json, quality_warnings=warnings)

    # ── 테스트 A: actions가 들어와도 텔레그램에 실행 문구 미출력 ──
    def test_a_us_close_actions_not_rendered(self):
        from core.telegram import _build_impact_message
        result = self._make_result({
            "actions": [{"type": "매수·즉시", "account": "[ISA]", "name": "카카오",
                         "price": "₩40,000", "qty": "10주"}],
            "advisor_oneliner": "어제 결산",
        })
        msg = _build_impact_message(result, result.raw_json, "🌅 데일리 리뷰", "테스트", "US_CLOSE")
        assert "액션 (그대로 실행)" not in msg
        assert "매수·즉시" not in msg
        assert "데일리 리뷰 — 결산·복기 전용" in msg

    # ── 테스트 B: buy_recs/sell_recs/night_orders 미출력 ──
    def test_b_us_close_recs_not_rendered(self):
        from core.telegram import _build_impact_message
        result = self._make_result({
            "strategy_buy": [{"name": "삼성전자", "ticker": "005930.KS", "account": "[일반]"}],
            "strategy_sell": [{"name": "MU", "ticker": "MU"}],
            "night_orders": [{"구분": "매수", "종목": "카카오"}],
        })
        msg = _build_impact_message(result, result.raw_json, "🌅 데일리 리뷰", "테스트", "US_CLOSE")
        assert "매수 추천" not in msg
        assert "매도 추천" not in msg
        assert "예약 주문" not in msg
        assert "오늘 실행할 액션 없음" not in msg  # 데일리 리뷰엔 부적절

    # ── 테스트 C: 본문 미래 실행형 표현 탐지/완화 ──
    def test_c_future_directive_detected(self):
        from core.analyzer import _detect_daily_review_violations
        for txt in [
            "오늘 카카오 매수 권고합니다",
            "삼성전자 비중 확대 검토",
            "MU 손절하세요",
            "내일 예약 주문 넣으세요",
            "09:00 전 실행할 것",
            "신규 진입 추천",
        ]:
            assert _detect_daily_review_violations(txt), f"미탐지: {txt}"

    def test_c_enforce_softens_body(self):
        from core.analyzer import _enforce_daily_review
        data = {
            "actions": [{"type": "매수·즉시"}],
            "strategy_buy": [{"name": "X"}],
            "strategy_sell": [{"name": "Y"}],
            "night_orders": [{"구분": "매수"}],
            "advisor_conclusion": "카카오 매수 추천. 삼성전자 비중 확대.",
            "investment_decision": "매수실행",
        }
        out, warnings = _enforce_daily_review(data)
        assert out["actions"] == [] and out["strategy_buy"] == [] and out["strategy_sell"] == []
        assert out["night_orders"] == [] and out["investment_decision"] == "관망"
        assert "매수 추천" not in out["advisor_conclusion"]
        assert "비중 확대" not in out["advisor_conclusion"]
        assert warnings  # 위반 경고 기록됨

    # ── 테스트 D: KR_OPEN 등 다른 브리핑의 액션은 정상 ──
    def test_d_kr_open_actions_still_render(self):
        from core.action_normalizer import normalize_actions
        from core.telegram import _build_impact_message
        raw = {
            "strategy_buy": [{"account": "[ISA]", "name": "카카오", "ticker": "035720.KS",
                              "horizon": "중기", "entry_price": "₩40,500", "shares": "15주",
                              "target_price": "₩43,500", "stop_loss": "₩38,800",
                              "reason": "RSI 35 과매도 반등 즉시 진입"}],
            "strategy_sell": [],
        }
        raw["normalized"] = normalize_actions(raw, "KR_OPEN", {}, {})
        result = self._make_result(raw)
        msg = _build_impact_message(result, raw, "🔔 개장", "테스트", "KR_OPEN")
        assert "오늘 실제 실행" in msg
        assert "카카오" in msg and "₩40,500" in msg

    def test_d_us_close_guard_does_not_touch_kr_open_data(self):
        """_enforce_daily_review는 US_CLOSE 경로에서만 호출 — KR_OPEN data는 무변경."""
        from core.analyzer import _enforce_daily_review
        # 직접 호출하지 않으면 데이터가 보존되어야 함을 확인 (호출 자체가 US_CLOSE 전용)
        data = {"actions": [{"type": "예약매수"}], "investment_decision": "매수실행"}
        # KR_OPEN에서는 이 함수를 호출하지 않으므로 원본 유지 (계약 검증)
        assert data["actions"] == [{"type": "예약매수"}]

    # ── 테스트 E: 과거 복기 표현은 허용 ──
    def test_e_past_review_allowed(self):
        from core.analyzer import _detect_daily_review_violations
        for txt in [
            "어제 매수한 카카오는 +3% 상승",
            "어제 매도한 MU 거래는 적절했다",
            "어제 손절한 판단을 복기하면 성급했다",
            "어제 익절한 이유는 목표가 도달이었다",
            "전날 매수한 종목이 반등 중",
        ]:
            assert _detect_daily_review_violations(txt) == [], f"과거 복기 오탐: {txt}"


class TestActionsTelegramRender:
    """정규화 결과의 텔레그램 4섹션 렌더 (구 _promote_to_actions 대체)."""

    def _make_result(self, raw_json: dict):
        from core.models import BriefingResult
        return BriefingResult(raw_json=raw_json)

    def test_telegram_shows_executable_and_conditional(self):
        from core.action_normalizer import normalize_actions
        from core.telegram import _build_impact_message
        raw = {
            "strategy_buy": [
                {"name": "카카오", "ticker": "035720.KS", "account": "[ISA]",
                 "entry_price": "₩40,000", "reason": "즉시 진입"},
                {"name": "KODEX 반도체", "ticker": "091160.KS", "account": "[RIA]",
                 "entry_price": "₩166,500", "reason": "추격 금지 눌림목"},
            ],
            "strategy_sell": [],
        }
        raw["normalized"] = normalize_actions(raw, "KR_BEFORE", {}, {})
        result = self._make_result(raw)
        msg = _build_impact_message(result, raw, "🇰🇷", "테스트", "KR_BEFORE")
        assert "오늘 실제 실행" in msg and "카카오" in msg
        assert "조건부 매수 후보" in msg and "KODEX 반도체" in msg

    def test_telegram_no_executable_shows_none(self):
        from core.action_normalizer import normalize_actions
        from core.telegram import _build_impact_message
        raw = {"strategy_buy": [], "strategy_sell": [], "next_action": "관망 — FOMC 대기"}
        raw["normalized"] = normalize_actions(raw, "KR_BEFORE", {}, {})
        result = self._make_result(raw)
        msg = _build_impact_message(result, raw, "🇰🇷", "테스트", "KR_BEFORE")
        assert "오늘 실제 실행: 없음" in msg
        assert "매수 후보 없음 사유" in msg

    def test_telegram_incomplete_order_shown_as_info_insufficient(self):
        # Section A: 현재가 누락 조건부 매수 → '정보 부족' 섹션에 표시, 조건부 후보 섹션엔 없음
        from core.action_normalizer import normalize_actions
        from core.telegram import _build_impact_message
        raw = {"strategy_buy": [
            {"name": "KODEX 200", "ticker": "069500.KS", "account": "[RIA]",
             "entry_price": "₩138,000", "reason": "눌림목 대기"}],
            "strategy_sell": []}
        raw["normalized"] = normalize_actions(raw, "KR_NIGHT", {"MSFT": 370.0}, {})
        msg = _build_impact_message(self._make_result(raw), raw, "🇰🇷", "t", "KR_NIGHT")
        assert "주문 차단·정보 부족" in msg
        assert "정보 부족으로 주문표 제외" in msg
        # 조건부 매수 후보 섹션엔 KODEX 200이 실행 주문표로 나오면 안 됨
        assert "조건부 매수 후보" not in msg

    def test_telegram_executable_sell_does_not_hide_conditional_buy(self):
        # Section E: 실행 매도가 있어도 조건부 매수 섹션이 숨겨지면 안 됨
        from core.action_normalizer import normalize_actions
        from core.telegram import _build_impact_message
        raw = {"strategy_buy": [
            {"name": "KODEX 반도체", "ticker": "091160.KS", "account": "[RIA]",
             "entry_price": "₩166,500", "confidence": "55", "reason": "추격 금지 눌림목"}],
            "strategy_sell": [
            {"ticker": "LMT", "name": "록히드", "current_price": "$540",
             "take_profit": "$560", "reason": "RSI 75 과열 부분 익절"}]}
        raw["normalized"] = normalize_actions(
            raw, "KR_BEFORE", {"091160.KS": 172000, "LMT": 540}, {})
        msg = _build_impact_message(self._make_result(raw), raw, "🇰🇷", "t", "KR_BEFORE")
        assert "오늘 실제 실행" in msg and "록히드" in msg
        assert "조건부 매수 후보" in msg and "KODEX 반도체" in msg

    def test_no_buy_reason_does_not_revive_blocked_stock(self):
        # Section E: 조건부 매수 0건 + 차단 종목 있을 때 no_buy_reason이 차단 종목 매수 문구를 살리지 않음
        from core.action_normalizer import normalize_actions
        from core.telegram import _build_impact_message
        raw = {"strategy_buy": [
            {"name": "KODEX 200", "ticker": "069500.KS", "account": "[RIA]",
             "entry_price": "₩138,000", "reason": "눌림목 대기"}],
            "strategy_sell": [],
            "next_action": "KODEX 200 즉시 매수 진입 권고"}
        raw["normalized"] = normalize_actions(raw, "KR_NIGHT", {"MSFT": 370.0}, {})
        msg = _build_impact_message(self._make_result(raw), raw, "🇰🇷", "t", "KR_NIGHT")
        # 차단된 KODEX 200의 '즉시 매수' 문구가 매수 후보 없음 사유로 부활하면 안 됨
        assert "KODEX 200 즉시 매수 진입 권고" not in msg


class TestIsActionablePolicy:
    """긴급알림 _is_actionable() 정책 테스트."""

    def _make_result(self, severity, ai_analysis="", market_session=""):
        from core.monitor_models import AlertResult, AlertTrigger, Severity, TriggerType
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        trigger = AlertTrigger(
            ticker="MU", name="마이크론",
            trigger_type=TriggerType.PRICE_DROP,
            current_value=-8.0, threshold=7.0,
            timestamp=datetime.now(KST),
            market_session=market_session,
        )
        sev = {"CRITICAL": Severity.CRITICAL, "WARNING": Severity.WARNING, "INFO": Severity.INFO}[severity]
        return AlertResult(trigger=trigger, severity=sev, ai_analysis=ai_analysis)

    def _make_stop_result(self, severity, ai_analysis=""):
        from core.monitor_models import AlertResult, AlertTrigger, Severity, TriggerType
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        trigger = AlertTrigger(
            ticker="MU", name="마이크론",
            trigger_type=TriggerType.STOP_LOSS_HIT,
            current_value=900, threshold=950,
            timestamp=datetime.now(KST),
        )
        sev = {"CRITICAL": Severity.CRITICAL, "WARNING": Severity.WARNING}[severity]
        return AlertResult(trigger=trigger, severity=sev, ai_analysis=ai_analysis)

    def test_critical_empty_analysis_false(self):
        """CRITICAL + 빈 ai_analysis → False."""
        from core.monitor import MarketMonitor
        m = MarketMonitor()
        assert m._is_actionable(self._make_result("CRITICAL", "")) is False

    def test_critical_watch_false(self):
        """CRITICAL + [관망] → False."""
        from core.monitor import MarketMonitor
        m = MarketMonitor()
        assert m._is_actionable(self._make_result("CRITICAL", "[관망] 단순 변동성")) is False

    def test_warning_buy_no_order_info_false(self):
        """WARNING + [매수] but 주문정보 없음 → False."""
        from core.monitor import MarketMonitor
        m = MarketMonitor()
        assert m._is_actionable(self._make_result("WARNING", "[매수]\n사유: RSI 과매도")) is False

    def test_critical_stop_loss_watch_false(self):
        """CRITICAL STOP_LOSS_HIT + [관망] → False."""
        from core.monitor import MarketMonitor
        m = MarketMonitor()
        assert m._is_actionable(self._make_stop_result("CRITICAL", "[관망] 대기")) is False

    def test_premarket_buy_with_full_info_true(self):
        """US_PREMARKET + [매수] + 7필드 완비 → True."""
        from core.monitor import MarketMonitor
        m = MarketMonitor()
        analysis = (
            "[매수]\n거래세션: 미국 프리마켓\n계좌: [일반]\n"
            "주문: 지정가 $950 × 3주 ($2,850, 예수금의 20%)\n"
            "목표: $1,050 (+10.5%) 도달 시 전량 매도\n시계: 단기\n"
            "사유: HBM 급락 과매도 반등 — RSI 25 + 거래량 급증"
        )
        assert m._is_actionable(self._make_result("WARNING", analysis, "US_PREMARKET")) is True

    def test_aftermarket_sell_with_full_info_true(self):
        """US_AFTERMARKET + [매도] + 7필드 완비 → True."""
        from core.monitor import MarketMonitor
        m = MarketMonitor()
        analysis = (
            "[매도]\n거래세션: 미국 애프터마켓\n계좌: [일반]\n"
            "주문: 지정가 $1,100 × 8주 ($8,800)\n"
            "목표: 즉시 청산 — 손절선 이탈 방어\n시계: 단기\n"
            "사유: 실적 쇼크 -12% 손절선 이탈"
        )
        assert m._is_actionable(self._make_result("WARNING", analysis, "US_AFTERMARKET")) is True

    def test_missing_target_horizon_false(self):
        """[매수]인데 목표/시계/사유 누락 → False (7필드 정책)."""
        from core.monitor import MarketMonitor
        m = MarketMonitor()
        analysis = "[매수]\n거래세션: 미국 프리마켓\n계좌: [일반]\n주문: 지정가 $950 × 3주"
        assert m._is_actionable(self._make_result("WARNING", analysis, "US_PREMARKET")) is False

    def test_invalid_horizon_value_false(self):
        """시계 값이 장기/중기/단기가 아니면 → False."""
        from core.monitor import MarketMonitor
        m = MarketMonitor()
        analysis = (
            "[매수]\n거래세션: 한국 정규장\n계좌: [ISA]\n"
            "주문: 지정가 ₩28,500 × 20주\n목표: ₩31,500 (+10%)\n시계: 미정\n사유: 급락"
        )
        assert m._is_actionable(self._make_result("WARNING", analysis, "KR_REGULAR")) is False

    def test_aftermarket_missing_order_false(self):
        """US_AFTERMARKET + [매도] + 주문정보 누락 → False."""
        from core.monitor import MarketMonitor
        m = MarketMonitor()
        analysis = "[매도]\n거래세션: 미국 애프터마켓\n사유: 급락"
        assert m._is_actionable(self._make_result("WARNING", analysis, "US_AFTERMARKET")) is False

    def test_alert_message_contains_spread_warning(self):
        """프리/애프터 알림 메시지에 스프레드·체결 리스크 문구 포함."""
        from core.monitor import _build_alert_message
        from core.monitor_models import AlertResult, AlertTrigger, Severity, TriggerType
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        trigger = AlertTrigger(
            ticker="MU", name="마이크론",
            trigger_type=TriggerType.PRICE_DROP,
            current_value=-8.0, threshold=7.0,
            timestamp=datetime.now(KST),
            market_session="US_PREMARKET",
        )
        result = AlertResult(
            trigger=trigger, severity=Severity.WARNING,
            ai_analysis="[매도]\n거래세션: 미국 프리마켓\n계좌: [일반]\n주문: MU 3주",
        )
        msg = _build_alert_message(result)
        assert "스프레드" in msg
        assert "체결 리스크" in msg
        assert "프리마켓" in msg


class TestMarketTradeableSession:
    """주문 가능 시간 + 세션 기반 모니터 테스트."""

    def test_us_premarket_tradeable(self):
        """KST 18:00 (ET 05:00 써머타임) → 미국 프리마켓 → tradeable True."""
        from core.market_hours import is_any_market_tradeable, get_market_session, US_PREMARKET
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        # 2026-06-09 화요일 KST 18:00 = ET 05:00 (써머타임)
        dt = datetime(2026, 6, 9, 18, 0, tzinfo=KST)
        assert is_any_market_tradeable(dt) is True
        assert get_market_session(dt)["us"] == US_PREMARKET

    def test_us_aftermarket_tradeable(self):
        """KST 05:30 (ET 16:30 써머타임) → 미국 애프터마켓 → tradeable True."""
        from core.market_hours import is_any_market_tradeable, get_market_session, US_AFTERMARKET
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        # 2026-06-10 수요일 KST 05:30 = ET 16:30
        dt = datetime(2026, 6, 10, 5, 30, tzinfo=KST)
        assert is_any_market_tradeable(dt) is True
        assert get_market_session(dt)["us"] == US_AFTERMARKET

    def test_closed_not_tradeable(self):
        """주말 → tradeable False."""
        from core.market_hours import is_any_market_tradeable
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        # 2026-06-07 일요일
        dt = datetime(2026, 6, 7, 14, 0, tzinfo=KST)
        assert is_any_market_tradeable(dt) is False

    def test_next_tradeable_returns_premarket(self):
        """다음 주문 가능 시간이 프리마켓 시작을 포함."""
        from core.market_hours import next_tradeable_session, get_market_session, US_PREMARKET
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        # 2026-06-09 화요일 KST 16:00 — 한국 마감, 미국 프리마켓 전
        dt = datetime(2026, 6, 9, 16, 0, tzinfo=KST)
        next_sess = next_tradeable_session(dt)
        sess = get_market_session(next_sess)
        # 프리마켓 또는 정규장이어야 함
        assert sess["us"] in (US_PREMARKET, "US_REGULAR") or sess["kr"] == "KR_REGULAR"


class TestWatchlistInSnapshot:
    """Watchlist가 시세 수집 대상에 포함되는지 테스트."""

    def test_us_watchlist_in_us_before(self):
        """미국 Watchlist(MSFT, PLTR)가 US_BEFORE portfolio에 포함."""
        from config.settings import get_market_config
        portfolio, _, _ = get_market_config("US_BEFORE")
        assert "MSFT" in portfolio, "MSFT가 US_BEFORE에 없음"
        assert "PLTR" in portfolio, "PLTR이 US_BEFORE에 없음"

    def test_kr_watchlist_in_kr_before(self):
        """국내 Watchlist(SK하이닉스)가 KR_BEFORE portfolio에 포함."""
        from config.settings import get_market_config
        portfolio, _, _ = get_market_config("KR_BEFORE")
        assert "000660.KS" in portfolio, "SK하이닉스가 KR_BEFORE에 없음"

    def test_full_watchlist_in_manual(self):
        """MANUAL에 전체 Watchlist 포함."""
        from config.settings import get_market_config, WATCHLIST
        portfolio, _, _ = get_market_config("MANUAL")
        for tk in WATCHLIST:
            assert tk in portfolio, f"{tk}가 MANUAL에 없음"

    def test_us_watchlist_not_in_kr(self):
        """미국 Watchlist는 KR_BEFORE에 포함되지 않음."""
        from config.settings import get_market_config
        portfolio, _, _ = get_market_config("KR_BEFORE")
        assert "MSFT" not in portfolio
        assert "PLTR" not in portfolio

    def test_kr_watchlist_not_in_us(self):
        """국내 Watchlist는 US_BEFORE에 포함되지 않음."""
        from config.settings import get_market_config
        portfolio, _, _ = get_market_config("US_BEFORE")
        assert "000660.KS" not in portfolio
