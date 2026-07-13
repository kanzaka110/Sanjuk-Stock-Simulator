"""tests/test_toss_kr_order_schema.py

KR_STOCK 주문 스키마 테스트.
"""

from __future__ import annotations

import pytest

from core.toss_live_transport import build_toss_order_create_request


_US_PAYLOAD = {
    "symbol": "NVDA",
    "side": "buy",
    "order_type": "limit",
    "quantity": 1,
    "limit_price": 190.0,
    "estimated_amount_krw": 280_000,
}

_KR_PAYLOAD = {
    "symbol": "069500.KS",
    "side": "buy",
    "order_type": "limit",
    "quantity": 10,
    "limit_price": 140_000,
    "estimated_amount_krw": 1_400_000,
}


class TestKrStockSchemaAllowed:
    """KR_STOCK asset_type에서 .KS/.KQ 심볼 허용, API에는 suffix strip."""

    def test_kr_symbol_allowed_with_kr_asset_type(self):
        result = build_toss_order_create_request(
            _KR_PAYLOAD, client_order_id="tlive_test_kr", asset_type="KR_STOCK",
        )
        assert result["ok"] is True
        # Toss API에는 suffix 없이 숫자 코드만 전송
        assert result["request"]["symbol"] == "069500"

    def test_kr_symbol_blocked_with_us_asset_type(self):
        result = build_toss_order_create_request(
            _KR_PAYLOAD, client_order_id="tlive_test_kr", asset_type="US_STOCK",
        )
        assert result["ok"] is False
        assert any("non_us" in b for b in result["blocks"])

    def test_kr_symbol_auto_detect_asset_type(self):
        """asset_type=None이면 심볼에서 자동 판별, suffix strip."""
        result = build_toss_order_create_request(
            _KR_PAYLOAD, client_order_id="tlive_test_kr",
        )
        assert result["ok"] is True
        assert result["request"]["symbol"] == "069500"


class TestKrStockPriceFormat:
    """KR_STOCK 가격은 정수 KRW."""

    def test_kr_price_integer(self):
        result = build_toss_order_create_request(
            _KR_PAYLOAD, client_order_id="tlive_test_kr", asset_type="KR_STOCK",
        )
        assert result["ok"] is True
        assert result["request"]["price"] == "140000"

    def test_kr_price_float_truncated(self):
        payload = {**_KR_PAYLOAD, "limit_price": 140000.5}
        result = build_toss_order_create_request(
            payload, client_order_id="tlive_test_kr", asset_type="KR_STOCK",
        )
        assert result["ok"] is True
        assert result["request"]["price"] == "140000"


class TestDigitOnlyBlocked:
    """숫자만으로 된 심볼(삼성증권 형식)은 항상 차단."""

    def test_digit_only_blocked_us(self):
        payload = {**_US_PAYLOAD, "symbol": "005930"}
        result = build_toss_order_create_request(
            payload, client_order_id="tlive_test", asset_type="US_STOCK",
        )
        assert result["ok"] is False
        assert any("digit_only" in b for b in result["blocks"])

    def test_digit_only_blocked_kr(self):
        payload = {**_KR_PAYLOAD, "symbol": "005930"}
        result = build_toss_order_create_request(
            payload, client_order_id="tlive_test", asset_type="KR_STOCK",
        )
        assert result["ok"] is False
        assert any("digit_only" in b for b in result["blocks"])


class TestUsStockUnchanged:
    """US_STOCK 기존 동작 유지."""

    def test_us_symbol_ok(self):
        result = build_toss_order_create_request(
            _US_PAYLOAD, client_order_id="tlive_test_us", asset_type="US_STOCK",
        )
        assert result["ok"] is True
        assert result["request"]["symbol"] == "NVDA"

    def test_us_price_decimal(self):
        payload = {**_US_PAYLOAD, "limit_price": 190.50}
        result = build_toss_order_create_request(
            payload, client_order_id="tlive_test_us", asset_type="US_STOCK",
        )
        assert result["ok"] is True
        assert result["request"]["price"] == "190.5"


class TestKosdaq:
    """코스닥 .KQ 심볼도 KR_STOCK으로 허용."""

    def test_kq_symbol_allowed(self):
        payload = {**_KR_PAYLOAD, "symbol": "091160.KQ"}
        result = build_toss_order_create_request(
            payload, client_order_id="tlive_test_kq", asset_type="KR_STOCK",
        )
        assert result["ok"] is True
        assert result["request"]["symbol"] == "091160"
