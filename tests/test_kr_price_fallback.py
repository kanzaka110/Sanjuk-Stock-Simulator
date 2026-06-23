"""
tests/test_kr_price_fallback.py

국내 종목 가격 fallback (core/kr_price_fallback.py) 테스트
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.kr_price_fallback as fb
import core.toss_paper_performance as perf


# ─── is_kr_ticker ────────────────────────────────────────


class TestIsKrTicker:
    def test_ks_suffix(self):
        assert fb.is_kr_ticker("005930.KS") is True

    def test_kq_suffix(self):
        assert fb.is_kr_ticker("060310.KQ") is True

    def test_us_ticker(self):
        assert fb.is_kr_ticker("NVDA") is False

    def test_index(self):
        assert fb.is_kr_ticker("^GSPC") is False

    def test_six_digit_code(self):
        assert fb.is_kr_ticker("005930") is True

    def test_non_kr_string(self):
        assert fb.is_kr_ticker("MU") is False


# ─── _to_krx_code ────────────────────────────────────────


class TestToKrxCode:
    def test_ks(self):
        assert fb._to_krx_code("005930.KS") == "005930"

    def test_kq(self):
        assert fb._to_krx_code("060310.KQ") == "060310"

    def test_bare_code(self):
        assert fb._to_krx_code("005930") == "005930"


# ─── _parse_naver_main_price ─────────────────────────────


class TestParseNaverMainPrice:
    def test_primary_pattern_no_today_blind(self):
        """Primary 패턴: no_today > blind span 구조."""
        html = '<p class="no_today"><em class="no_down"><span class="blind">72,400</span></em></p>'
        assert fb._parse_naver_main_price(html) == 72400.0

    def test_actual_naver_structure(self):
        """실제 Naver main.naver HTML 구조 — no_today + em + blind span."""
        html = (
            '<p class="no_today">\n'
            '  <em class="no_down">\n'
            '    <span class="blind">72,400</span>\n'
            '  </em>\n'
            '</p>'
        )
        assert fb._parse_naver_main_price(html) == 72400.0

    def test_no_match_returns_none(self):
        assert fb._parse_naver_main_price("<html>아무것도 없음</html>") is None

    def test_comma_in_price(self):
        html = '<p class="no_today"><em><span class="blind">74,200</span></em></p>'
        assert fb._parse_naver_main_price(html) == 74200.0

    def test_zero_price_rejected(self):
        html = '현재가</span>  0'
        # 0은 유효한 숫자이지만 > 0 조건에 걸려 None 반환해야 함
        assert fb._parse_naver_main_price(html) is None


# ─── get_kr_stock_price_fallback ─────────────────────────


class TestGetKrStockPriceFallback:
    def test_non_kr_skip(self):
        result = fb.get_kr_stock_price_fallback("NVDA")
        assert result["ok"] is False
        assert result["price"] is None
        assert "국내 종목" in result["warning"]

    def test_naver_main_success(self):
        with patch.object(fb, "_naver_main_price", return_value=72400.0):
            result = fb.get_kr_stock_price_fallback("005930.KS")
        assert result["ok"] is True
        assert result["price"] == 72400.0
        assert result["source"] == "naver_current"
        assert result["warning"] is None

    def test_naver_main_fail_falls_to_frgn(self):
        with patch.object(fb, "_naver_main_price", return_value=None), \
             patch.object(fb, "_naver_frgn_recent_close", return_value=71800.0):
            result = fb.get_kr_stock_price_fallback("005930.KS")
        assert result["ok"] is True
        assert result["price"] == 71800.0
        assert result["source"] == "naver_recent_close"

    def test_both_fail_returns_unavailable(self):
        with patch.object(fb, "_naver_main_price", return_value=None), \
             patch.object(fb, "_naver_frgn_recent_close", return_value=None):
            result = fb.get_kr_stock_price_fallback("005930.KS")
        assert result["ok"] is False
        assert result["price"] is None

    def test_naver_exception_returns_unavailable(self):
        with patch.object(fb, "_naver_main_price", side_effect=Exception("timeout")), \
             patch.object(fb, "_naver_frgn_recent_close", return_value=None):
            result = fb.get_kr_stock_price_fallback("005930.KS")
        assert result["ok"] is False

    def test_kq_ticker_also_works(self):
        with patch.object(fb, "_naver_main_price", return_value=45000.0):
            result = fb.get_kr_stock_price_fallback("060310.KQ")
        assert result["ok"] is True
        assert result["price"] == 45000.0


# ─── _get_quote_for_paper 국내 naver fallback 연동 ────────


def _make_mock_quote(price: float):
    from core.models import Quote
    return Quote(ticker="T", name="T", price=price, change=0.0, pct=0.0, high=price, low=price)


class TestGetQuoteForPaperNaverFallback:
    """_get_quote_for_paper에서 naver_current가 source_chain에 들어가는지 확인."""

    def test_naver_used_when_kis_anomaly(self):
        """KIS 이상치(310,000) → naver fallback 72,400 → accepted."""
        with patch("core.market._get_quote_kis", return_value=_make_mock_quote(310000.0)), \
             patch("core.kr_price_fallback._naver_main_price", return_value=72400.0), \
             patch("core.market._get_quote_yf_live", return_value=None), \
             patch("core.market._get_quote_daily", return_value=None):
            result = perf._get_quote_for_paper("005930.KS", entry_price=72000.0)

        assert result["price"] == 72400.0
        assert result["accepted_price_source"] == "naver_current"
        # source_chain에 KIS reject와 naver accept 모두 있어야 함
        sources = [e["source"] for e in result["source_chain"]]
        assert "KIS" in sources
        assert "naver_current" in sources
        kis_entry = next(e for e in result["source_chain"] if e["source"] == "KIS")
        naver_entry = next(e for e in result["source_chain"] if e["source"] == "naver_current")
        assert kis_entry["accepted"] is False
        assert naver_entry["accepted"] is True

    def test_naver_anomaly_still_rejected(self):
        """Naver도 이상치(310,000)면 reject."""
        with patch("core.market._get_quote_kis", return_value=_make_mock_quote(310000.0)), \
             patch("core.kr_price_fallback._naver_main_price", return_value=310000.0), \
             patch("core.kr_price_fallback._naver_frgn_recent_close", return_value=None), \
             patch("core.market._get_quote_yf_live", return_value=_make_mock_quote(310000.0)), \
             patch("core.market._get_quote_daily", return_value=None):
            result = perf._get_quote_for_paper("005930.KS", entry_price=72000.0)

        assert result["price"] is None
        assert result["accepted_price_source"] is None

    def test_naver_not_called_for_us_ticker(self):
        """해외 종목은 naver fallback을 호출하지 않는다."""
        call_log: list[str] = []

        def mock_naver(ticker: str):
            call_log.append(ticker)
            return 150.0

        with patch("core.market._get_quote_kis", return_value=_make_mock_quote(130.0)), \
             patch("core.kr_price_fallback.get_kr_stock_price_fallback", side_effect=mock_naver):
            result = perf._get_quote_for_paper("NVDA", entry_price=130.0)

        # 미국 종목에서는 naver fallback 호출 안 됨
        assert call_log == []
        assert result["price"] == 130.0

    def test_source_chain_has_naver_entry_on_kr(self):
        """국내 종목 KIS reject 후 naver가 source_chain에 포함됨."""
        with patch("core.market._get_quote_kis", return_value=_make_mock_quote(310000.0)), \
             patch("core.kr_price_fallback._naver_main_price", return_value=71500.0), \
             patch("core.market._get_quote_yf_live", return_value=None), \
             patch("core.market._get_quote_daily", return_value=None):
            result = perf._get_quote_for_paper("005930.KS", entry_price=72000.0)

        sources = [e["source"] for e in result["source_chain"]]
        assert "naver_current" in sources

    def test_naver_fail_continues_to_yfinance(self):
        """Naver 실패 시 yfinance_live로 이어진다."""
        with patch("core.market._get_quote_kis", return_value=_make_mock_quote(310000.0)), \
             patch("core.kr_price_fallback.get_kr_stock_price_fallback",
                   return_value={"price": None, "source": "naver_unavailable", "ok": False, "warning": "실패"}), \
             patch("core.market._get_quote_yf_live", return_value=_make_mock_quote(72200.0)), \
             patch("core.market._get_quote_daily", return_value=None):
            result = perf._get_quote_for_paper("005930.KS", entry_price=72000.0)

        assert result["price"] == 72200.0
        assert result["accepted_price_source"] == "yfinance_live"

    def test_evaluate_paper_order_uses_naver_fallback(self):
        """evaluate_paper_order가 naver fallback을 통해 outcome=open을 반환한다."""
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        order = {
            "paper_id": "p_test_naver",
            "symbol": "005930.KS",
            "side": "buy",
            "quantity": 1,
            "limit_price": 72000,
            "status": "approved",
            "created_at": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        }
        with patch("core.market._get_quote_kis", return_value=_make_mock_quote(310000.0)), \
             patch("core.kr_price_fallback._naver_main_price", return_value=72500.0), \
             patch("core.market._get_quote_yf_live", return_value=None), \
             patch("core.market._get_quote_daily", return_value=None):
            result = perf.evaluate_paper_order(order)

        assert result["outcome"] == "open"
        assert result["current_price"] == 72500.0
        assert result["accepted_price_source"] == "naver_current"
        assert result["price_anomaly"] is False
        assert result["data_source"] == "naver_current"


# ─── 금지 CTA / 실제 주문 함수 없음 ──────────────────────


class TestFallbackGuardrails:
    def test_no_order_functions_in_fallback(self):
        src = (ROOT / "core" / "kr_price_fallback.py").read_text(encoding="utf-8")
        for fn in ("place_order", "submit_order", "execute_order"):
            assert fn not in src

    def test_no_write_http_route_in_fallback(self):
        src = (ROOT / "core" / "kr_price_fallback.py").read_text(encoding="utf-8")
        for route in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert route not in src

    def test_no_sensitive_data_in_fallback(self):
        src = (ROOT / "core" / "kr_price_fallback.py").read_text(encoding="utf-8")
        for token in ("KIS_APP_SECRET", "KIS_APP_KEY", "TOSS_APP_SECRET"):
            assert token not in src
