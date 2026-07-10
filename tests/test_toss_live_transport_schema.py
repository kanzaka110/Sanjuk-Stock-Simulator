"""tests/test_toss_live_transport_schema.py

build_toss_order_create_request() dry-run schema 변환 테스트.

1. US symbol 허용 / KR symbol 차단
2. BUY+SELL (buy/sell → upper)
3. LIMIT only (limit → LIMIT, market → BLOCK)
4. quantity 변환/검증
5. price 변환/검증
6. clientOrderId 정규화 (36자, 허용문자)
7. 금액 한도 재확인
8. 민감정보 없음
9. 실제 HTTP/endpoint 없음
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_transport import build_toss_order_create_request


def _payload(**kw) -> dict:
    base = {
        "symbol": "SOFI",
        "side": "buy",
        "order_type": "limit",
        "quantity": 1,
        "limit_price": 30850,
        "estimated_amount_krw": 30850,
    }
    base.update(kw)
    return base


# ── 1. US symbol / KR 차단 ────────────────────────────────────

class TestSymbolNormalization(unittest.TestCase):
    def test_kr_suffix_blocked_when_us_asset_type(self):
        r = build_toss_order_create_request(
            _payload(symbol="091180.KS"), client_order_id="tlive_1", asset_type="US_STOCK",
        )
        self.assertFalse(r["ok"])
        self.assertTrue(any("non_us_symbol_not_allowed" in b for b in r["blocks"]))

    def test_kr_suffix_allowed_when_kr_asset_type(self):
        r = build_toss_order_create_request(
            _payload(symbol="091180.KS"), client_order_id="tlive_1", asset_type="KR_STOCK",
        )
        self.assertTrue(r["ok"])

    def test_us_symbol_unchanged(self):
        # MU 같은 미국 티커는 그대로 (단 BUY/LIMIT 유효해야 request 생성)
        r = build_toss_order_create_request(
            _payload(symbol="NVDA"), client_order_id="tlive_us"
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["request"]["symbol"], "NVDA")

    def test_empty_symbol_blocked(self):
        r = build_toss_order_create_request(
            _payload(symbol=""), client_order_id="tlive_e"
        )
        self.assertFalse(r["ok"])
        self.assertTrue(any("invalid_symbol" in b for b in r["blocks"]))


# ── 2. BUY+SELL ──────────────────────────────────────────

class TestBuySell(unittest.TestCase):
    def test_buy_uppercased(self):
        r = build_toss_order_create_request(_payload(side="buy"), client_order_id="t")
        self.assertEqual(r["request"]["side"], "BUY")

    def test_sell_uppercased(self):
        r = build_toss_order_create_request(_payload(side="sell"), client_order_id="t")
        self.assertTrue(r["ok"])
        self.assertEqual(r["request"]["side"], "SELL")


# ── 3. LIMIT only ────────────────────────────────────────

class TestLimitOnly(unittest.TestCase):
    def test_limit_uppercased(self):
        r = build_toss_order_create_request(_payload(order_type="limit"), client_order_id="t")
        self.assertEqual(r["request"]["orderType"], "LIMIT")

    def test_market_blocked(self):
        r = build_toss_order_create_request(_payload(order_type="market"), client_order_id="t")
        self.assertFalse(r["ok"])
        self.assertTrue(any("order_type_not_limit" in b for b in r["blocks"]))


# ── 4. quantity ──────────────────────────────────────────

class TestQuantity(unittest.TestCase):
    def test_int_to_string(self):
        r = build_toss_order_create_request(_payload(quantity=1), client_order_id="t")
        self.assertEqual(r["request"]["quantity"], "1")

    def test_float_whole_ok(self):
        r = build_toss_order_create_request(_payload(quantity=3.0), client_order_id="t")
        self.assertEqual(r["request"]["quantity"], "3")

    def test_zero_blocked(self):
        r = build_toss_order_create_request(_payload(quantity=0), client_order_id="t")
        self.assertFalse(r["ok"])
        self.assertTrue(any("invalid_quantity" in b for b in r["blocks"]))

    def test_negative_blocked(self):
        r = build_toss_order_create_request(_payload(quantity=-1), client_order_id="t")
        self.assertFalse(r["ok"])

    def test_fractional_blocked(self):
        r = build_toss_order_create_request(_payload(quantity=1.5), client_order_id="t")
        self.assertFalse(r["ok"])
        self.assertTrue(any("invalid_quantity" in b for b in r["blocks"]))


# ── 5. price ─────────────────────────────────────────────

class TestPrice(unittest.TestCase):
    def test_int_to_string(self):
        r = build_toss_order_create_request(_payload(limit_price=30850), client_order_id="t")
        self.assertEqual(r["request"]["price"], "30850")

    def test_float_whole_ok(self):
        r = build_toss_order_create_request(_payload(limit_price=30850.0), client_order_id="t")
        self.assertEqual(r["request"]["price"], "30850")

    def test_zero_blocked(self):
        r = build_toss_order_create_request(_payload(limit_price=0), client_order_id="t")
        self.assertFalse(r["ok"])
        self.assertTrue(any("invalid_price" in b for b in r["blocks"]))

    def test_negative_blocked(self):
        r = build_toss_order_create_request(_payload(limit_price=-100), client_order_id="t")
        self.assertFalse(r["ok"])

    def test_fractional_ok(self):
        r = build_toss_order_create_request(_payload(limit_price=30.85), client_order_id="t")
        self.assertTrue(r["ok"])
        self.assertEqual(r["request"]["price"], "30.85")


# ── 5-1. KR 호가단위 정규화 ───────────────────────────────

class TestKrPriceTick(unittest.TestCase):
    def test_kr_sell_off_tick_price_floored_to_valid_tick(self):
        r = build_toss_order_create_request(
            _payload(symbol="042660.KS", side="sell", limit_price=110050),
            client_order_id="tlive_tick_sell",
            asset_type="KR_STOCK",
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["request"]["symbol"], "042660")
        self.assertEqual(r["request"]["side"], "SELL")
        self.assertEqual(r["request"]["price"], "110000")
        self.assertTrue(any("kr_tick_price_adjusted" in w for w in r["warnings"]))

    def test_kr_buy_off_tick_price_floored_to_valid_tick(self):
        r = build_toss_order_create_request(
            _payload(symbol="024110.KS", side="buy", limit_price=110050),
            client_order_id="tlive_tick_buy",
            asset_type="KR_STOCK",
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["request"]["symbol"], "024110")
        self.assertEqual(r["request"]["side"], "BUY")
        self.assertEqual(r["request"]["price"], "110000")
        self.assertTrue(any("tick=100" in w for w in r["warnings"]))

    def test_kr_on_tick_price_unchanged(self):
        r = build_toss_order_create_request(
            _payload(symbol="042660.KS", side="sell", limit_price=110100),
            client_order_id="tlive_tick_exact",
            asset_type="KR_STOCK",
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["request"]["price"], "110100")
        self.assertFalse(any("kr_tick_price_adjusted" in w for w in r["warnings"]))

    def test_kr_44800_is_valid_50_tick(self):
        r = build_toss_order_create_request(
            _payload(symbol="403870.KS", side="sell", limit_price=44800),
            client_order_id="tlive_tick_44800",
            asset_type="KR_STOCK",
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["request"]["price"], "44800")


# ── 6. clientOrderId ─────────────────────────────────────

class TestClientOrderId(unittest.TestCase):
    def test_simple_id_preserved(self):
        r = build_toss_order_create_request(_payload(), client_order_id="tlive_abc-123")
        self.assertEqual(r["request"]["clientOrderId"], "tlive_abc-123")

    def test_max_36_chars(self):
        long_id = "tlive_" + "x" * 80
        r = build_toss_order_create_request(_payload(), client_order_id=long_id)
        self.assertLessEqual(len(r["request"]["clientOrderId"]), 36)

    def test_disallowed_chars_stripped(self):
        r = build_toss_order_create_request(
            _payload(), client_order_id="tlive!@#$%^&*() 001"
        )
        cid = r["request"]["clientOrderId"]
        self.assertTrue(re.fullmatch(r"[a-zA-Z0-9_-]+", cid))

    def test_empty_id_fallback(self):
        r = build_toss_order_create_request(_payload(), client_order_id="")
        self.assertTrue(r["request"]["clientOrderId"])
        self.assertTrue(re.fullmatch(r"[a-zA-Z0-9_-]+", r["request"]["clientOrderId"]))

    def test_long_id_deterministic(self):
        long_id = "tlive_" + "y" * 100
        r1 = build_toss_order_create_request(_payload(), client_order_id=long_id)
        r2 = build_toss_order_create_request(_payload(), client_order_id=long_id)
        self.assertEqual(r1["request"]["clientOrderId"], r2["request"]["clientOrderId"])


# ── 7. 금액 한도 ─────────────────────────────────────────

class TestAmountLimit(unittest.TestCase):
    def test_over_limit_blocked(self):
        r = build_toss_order_create_request(
            _payload(quantity=1, limit_price=150000, estimated_amount_krw=150000),
            client_order_id="t",
            max_order_krw=100000,
        )
        self.assertFalse(r["ok"])
        self.assertTrue(any("amount_over_limit" in b for b in r["blocks"]))

    def test_within_limit_ok(self):
        r = build_toss_order_create_request(
            _payload(estimated_amount_krw=30850),
            client_order_id="t",
            max_order_krw=100000,
        )
        self.assertTrue(r["ok"])


# ── 8. 고정 필드 ─────────────────────────────────────────

class TestFixedFields(unittest.TestCase):
    def test_time_in_force_day(self):
        r = build_toss_order_create_request(_payload(), client_order_id="t")
        self.assertEqual(r["request"]["timeInForce"], "DAY")

    def test_confirm_high_value_false(self):
        r = build_toss_order_create_request(_payload(), client_order_id="t")
        self.assertFalse(r["request"]["confirmHighValueOrder"])

    def test_warnings_present(self):
        r = build_toss_order_create_request(_payload(), client_order_id="t")
        self.assertIn("dry-run only", r["warnings"])
        self.assertIn("not sent", r["warnings"])


# ── 9. 민감정보 / endpoint 없음 ──────────────────────────

class TestNoSensitive(unittest.TestCase):
    def test_no_sensitive_in_request(self):
        r = build_toss_order_create_request(_payload(), client_order_id="t")
        s = str(r)
        for kw in ("accountNo", "Bearer", "Authorization",
                   "X-Tossinvest-Account", "APP_KEY", "APP_SECRET", "token"):
            self.assertNotIn(kw, s)

    def test_request_has_no_url(self):
        r = build_toss_order_create_request(_payload(), client_order_id="t")
        s = str(r["request"])
        self.assertNotIn("http", s.lower())
        self.assertNotIn("/api/", s)


if __name__ == "__main__":
    unittest.main()
