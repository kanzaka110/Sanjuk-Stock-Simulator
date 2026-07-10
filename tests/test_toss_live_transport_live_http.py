"""tests/test_toss_live_transport_live_http.py

LiveTossTransport + toss_live_order_http mock 테스트.

실제 주문 전송 없음 — requests.post는 전부 mock.
1. 성공 mock: endpoint/body/live_order_sent=true, 민감정보 없음
2. token 없음 → no send
3. account 없음 → no send
4. schema block(KR/market) → no send (post 미호출)
5. HTTP 4xx/5xx → live_order_sent=false
6. requests exception → live_order_sent=false
7. 기본 transport는 not_configured 유지
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_transport import LiveTossTransport
# header 키 상수만 import (literal 노출 회피)
from core import toss_live_order_http as _order_http
from core.toss_live_order_http import _H_AUTH, _H_ACCOUNT

_MOCK_TOKEN = "tok_mock_value"
_MOCK_BASE = "https://test.example"


def _payload(**kw) -> dict:
    base = {
        "symbol": "SOFI",
        "side": "buy",
        "order_type": "limit",
        "quantity": 1,
        "limit_price": 30850,
        "estimated_amount_krw": 30850,
        "client_order_id": "tlive_http_001",
    }
    base.update(kw)
    return base


class _Resp:
    def __init__(self, status, payload=None, raise_json=False, text=None):
        self.status_code = status
        self._payload = payload or {}
        self._raise = raise_json
        self.text = text if text is not None else str(self._payload)

    def json(self):
        if self._raise:
            raise ValueError("no json")
        return self._payload


def _run_send(
    *,
    token=_MOCK_TOKEN,
    accounts=None,
    holdings=None,
    post_return=None,
    post_exc=None,
    payload_kw=None,
):
    """LiveTossTransport.send_buy_order를 mock 환경에서 실행."""
    if accounts is None:
        accounts = [{"accountSeq": "55501234"}]
    if holdings is None:
        holdings = {"items": [{"symbol": "SOFI", "sellableQuantity": "1", "quantity": "1"}]}
    _order_http._clear_account_seq_cache()
    mock_post = MagicMock()
    if post_exc is not None:
        mock_post.side_effect = post_exc
    else:
        mock_post.return_value = post_return or _Resp(200, {"result": {"orderId": "OID-abc-7"}})

    with patch("core.toss_client._get_access_token", return_value=token), \
         patch("core.toss_client.get_accounts", return_value=accounts), \
         patch("core.toss_client.get_holdings", return_value=holdings), \
         patch("core.toss_client.TOSS_BASE_URL", _MOCK_BASE), \
         patch("requests.post", mock_post):
        transport = LiveTossTransport(timeout=5)
        result = transport.send_buy_order(_payload(**(payload_kw or {})))
    return result, mock_post


# ── 1. 성공 ──────────────────────────────────────────────

class TestLiveSuccess(unittest.TestCase):
    def test_live_order_sent_true(self):
        result, _ = _run_send()
        self.assertTrue(result["live_order_sent"])

    def test_ok_true(self):
        result, _ = _run_send()
        self.assertTrue(result["ok"])

    def test_transport_status_live_sent(self):
        result, _ = _run_send()
        self.assertEqual(result["transport_status"], "live_sent")

    def test_endpoint_called(self):
        _, mock_post = _run_send()
        self.assertEqual(mock_post.call_count, 1)
        url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args.kwargs.get("url")
        self.assertTrue(url.endswith("/api/v1/orders"))

    def test_body_schema(self):
        _, mock_post = _run_send()
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["symbol"], "SOFI")
        self.assertEqual(body["side"], "BUY")
        self.assertEqual(body["orderType"], "LIMIT")
        self.assertEqual(body["quantity"], "1")
        self.assertEqual(body["price"], "30850")
        self.assertEqual(body["timeInForce"], "DAY")
        self.assertFalse(body["confirmHighValueOrder"])

    def test_headers_present(self):
        _, mock_post = _run_send()
        headers = mock_post.call_args.kwargs["headers"]
        self.assertIn(_H_AUTH, headers)
        self.assertIn(_H_ACCOUNT, headers)
        # 토큰은 header에는 들어가지만 (실제 요청), 결과엔 노출 안 됨
        self.assertIn(_MOCK_TOKEN, headers[_H_AUTH])

    def test_no_token_in_result(self):
        result, _ = _run_send()
        self.assertNotIn(_MOCK_TOKEN, str(result))

    def test_no_headers_in_result(self):
        result, _ = _run_send()
        s = str(result)
        self.assertNotIn(_H_AUTH, s)
        self.assertNotIn(_H_ACCOUNT, s)

    def test_broker_order_id_returned(self):
        result, _ = _run_send()
        self.assertIn("broker_order_id", result)


# ── 2. token 없음 ────────────────────────────────────────

class TestNoToken(unittest.TestCase):
    def test_no_token_no_send(self):
        result, mock_post = _run_send(token=None)
        self.assertFalse(result["live_order_sent"])
        self.assertEqual(mock_post.call_count, 0)

    def test_no_token_reason(self):
        result, _ = _run_send(token=None)
        self.assertEqual(result["reason"], "token_unavailable")


# ── 3. account 없음 ──────────────────────────────────────

class TestNoAccount(unittest.TestCase):
    def test_no_account_no_send(self):
        result, mock_post = _run_send(accounts=[])
        self.assertFalse(result["live_order_sent"])
        self.assertEqual(mock_post.call_count, 0)

    def test_no_account_reason(self):
        result, _ = _run_send(accounts=[])
        self.assertEqual(result["reason"], "account_unavailable")


# ── 4. schema block (US-only/LIMIT) ──────────────────────────

class TestSchemaBlock(unittest.TestCase):
    def test_kr_symbol_blocked_when_us_asset_type(self):
        result, mock_post = _run_send(payload_kw={"symbol": "091180.KS", "asset_type": "US_STOCK"})
        self.assertFalse(result["live_order_sent"])
        self.assertTrue(result["blocked"])
        self.assertEqual(mock_post.call_count, 0)

    def test_kr_symbol_allowed_when_kr_asset_type(self):
        result, mock_post = _run_send(payload_kw={"symbol": "091180.KS", "asset_type": "KR_STOCK"})
        self.assertTrue(result["live_order_sent"])

    def test_sell_allowed_sends(self):
        result, mock_post = _run_send(payload_kw={"side": "sell"})
        self.assertTrue(result["live_order_sent"])
        self.assertEqual(mock_post.call_args.kwargs["json"]["side"], "SELL")

    def test_market_blocked_no_send(self):
        result, mock_post = _run_send(payload_kw={"order_type": "market"})
        self.assertFalse(result["live_order_sent"])
        self.assertEqual(mock_post.call_count, 0)


# ── 5. HTTP 에러 ─────────────────────────────────────────

class TestHttpError(unittest.TestCase):
    def test_400_no_send(self):
        result, _ = _run_send(post_return=_Resp(400, {"error": "bad"}))
        self.assertFalse(result["live_order_sent"])
        self.assertIn("400", result["reason"])

    def test_500_no_send(self):
        result, _ = _run_send(post_return=_Resp(500))
        self.assertFalse(result["live_order_sent"])
        self.assertIn("500", result["reason"])

    def test_422_keeps_error_body_and_request_preview(self):
        result, _ = _run_send(
            post_return=_Resp(422, text='{"code":"INVALID_PRICE"}')
        )
        self.assertFalse(result["live_order_sent"])
        self.assertEqual(result["reason"], "http_422")
        self.assertIn("INVALID_PRICE", result["error_body"])
        self.assertEqual(result["order_request_preview"]["symbol"], "SOFI")
        self.assertNotIn(_MOCK_TOKEN, str(result))


# ── 6. requests 예외 ─────────────────────────────────────

class TestRequestException(unittest.TestCase):
    def test_network_error_no_send(self):
        import requests
        result, _ = _run_send(post_exc=requests.RequestException("conn refused"))
        self.assertFalse(result["live_order_sent"])
        self.assertEqual(result["reason"], "network_error")

    def test_network_error_message_safe(self):
        import requests
        result, _ = _run_send(post_exc=requests.RequestException("boom"))
        self.assertNotIn(_MOCK_TOKEN, str(result))


# ── 7. 기본 transport는 여전히 not_configured ───────────

class TestDefaultsUnchanged(unittest.TestCase):
    def test_default_is_not_configured(self):
        from core.toss_live_transport import (
            DEFAULT_LIVE_TRANSPORT, NotConfiguredTossLiveTransport,
        )
        self.assertIsInstance(DEFAULT_LIVE_TRANSPORT, NotConfiguredTossLiveTransport)

    def test_status_still_not_configured(self):
        from core.toss_live_transport import LIVE_TRANSPORT_STATUS
        self.assertEqual(LIVE_TRANSPORT_STATUS, "not_configured")

    def test_live_order_sent_possible_false(self):
        from core.toss_live_transport import get_transport_status
        self.assertFalse(get_transport_status()["live_order_sent_possible"])


# ── 8. 직접 account_seq 주입 (get_accounts 우회) ─────────

class TestExplicitAccountSeq(unittest.TestCase):
    def test_explicit_seq_used(self):
        mock_post = MagicMock(return_value=_Resp(200, {"result": {"orderId": "X1"}}))
        with patch("core.toss_client._get_access_token", return_value=_MOCK_TOKEN), \
             patch("core.toss_client.TOSS_BASE_URL", _MOCK_BASE), \
             patch("requests.post", mock_post):
            transport = LiveTossTransport(account_seq="99988877", timeout=5)
            result = transport.send_buy_order(_payload())
        self.assertTrue(result["live_order_sent"])
        self.assertEqual(mock_post.call_args.kwargs["headers"][_H_ACCOUNT], "99988877")


# ── 9. SELL 보유수량 polling ─────────────────────────────

class TestSellablePositionPolling(unittest.TestCase):
    def test_sell_without_sellable_position_no_send(self):
        result, mock_post = _run_send(
            payload_kw={"side": "sell"},
            holdings={"items": []},
        )
        self.assertFalse(result["live_order_sent"])
        self.assertEqual(result["reason"], "sellable_position_not_ready")
        self.assertEqual(mock_post.call_count, 0)

    def test_sell_waits_until_position_visible_then_sends(self):
        mock_post = MagicMock(return_value=_Resp(200, {"result": {"orderId": "S1"}}))
        holdings_seq = [
            {"items": []},
            {"items": [{"symbol": "SOFI", "availableQuantity": "1"}]},
        ]
        with patch("core.toss_client._get_access_token", return_value=_MOCK_TOKEN),              patch("core.toss_client.get_accounts", return_value=[{"accountSeq": "55501234"}]),              patch("core.toss_client.get_holdings", side_effect=holdings_seq),              patch("core.toss_client.TOSS_BASE_URL", _MOCK_BASE),              patch("time.sleep", return_value=None),              patch("requests.post", mock_post):
            transport = LiveTossTransport(timeout=5)
            result = transport.send_buy_order(_payload(side="sell"))
        self.assertTrue(result["live_order_sent"])
        self.assertEqual(mock_post.call_args.kwargs["json"]["side"], "SELL")

    def test_buy_does_not_require_holdings(self):
        result, mock_post = _run_send(payload_kw={"side": "buy"}, holdings={"items": []})
        self.assertTrue(result["live_order_sent"])
        self.assertEqual(mock_post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
