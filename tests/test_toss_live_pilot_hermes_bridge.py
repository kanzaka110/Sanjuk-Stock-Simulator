"""tests/test_toss_live_pilot_hermes_bridge.py

Hermes 미러링 브릿지 v1 테스트.

1. mirror_disabled → skipped
2. mirror_target_missing → skipped
3. mirror_enabled mock → Telegram send mock 호출
4. get_mirror_status → no secrets
5. --send --mirror-hermes → preview + hermes verify 둘 다 호출
6. live_order_sent=false 보장
7. 민감정보 없음
"""

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


_BASE_POLICY = {
    "live_order_allowed": False,
    "adapter_status": "disabled",
    "live_pilot_enabled": False,
    "side_mode": "BUY_ONLY",
    "sell_allowed": False,
    "live_transport_status": "not_configured",
    "blocked_symbols": ["161510.KS", "005930.KS", "MU"],
    "max_order_krw": 100_000,
    "max_daily_krw": 300_000,
}

_BASE_PREVIEW = {
    "symbol": "091180.KS",
    "side": "buy",
    "quantity": 1,
    "limit_price": 30815.0,
    "estimated_amount_krw": 30815.0,
    "pilot_id": "tlive_test_001",
    "preview_id": "tlive_test_001",
}

_BASE_VERIFICATION = {
    "verification_id": "hv_test_001",
    "pilot_id": "tlive_test_001",
    "preview_id": "tlive_test_001",
    "status": "PENDING",
}


# ── 1. mirror disabled ─────────────────────────────────────

class TestMirrorDisabled(unittest.TestCase):
    def test_skipped_when_disabled(self):
        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "false",
                         "HERMES_VERIFY_CHAT_ID": ""}):
            from core.toss_live_pilot_hermes_bridge import maybe_send_hermes_verification_request
            result = maybe_send_hermes_verification_request(
                _BASE_PREVIEW, _BASE_VERIFICATION, _BASE_POLICY
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "mirror_disabled")

    def test_live_order_sent_not_in_result(self):
        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "false"}):
            from core.toss_live_pilot_hermes_bridge import maybe_send_hermes_verification_request
            result = maybe_send_hermes_verification_request(
                _BASE_PREVIEW, _BASE_VERIFICATION, _BASE_POLICY
            )
        # live_order_sent 키가 있어도 False여야 함
        self.assertFalse(result.get("live_order_sent", False))


# ── 2. mirror target missing ───────────────────────────────

class TestMirrorTargetMissing(unittest.TestCase):
    def test_skipped_when_chat_missing(self):
        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "true",
                         "HERMES_VERIFY_CHAT_ID": ""}):
            from core.toss_live_pilot_hermes_bridge import maybe_send_hermes_verification_request
            result = maybe_send_hermes_verification_request(
                _BASE_PREVIEW, _BASE_VERIFICATION, _BASE_POLICY
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "mirror_target_missing")

    def test_preview_verification_unaffected(self):
        """skipped 결과도 preview/verification 자체는 영향 없음 — skipped=True만 반환."""
        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "true",
                         "HERMES_VERIFY_CHAT_ID": ""}):
            from core.toss_live_pilot_hermes_bridge import maybe_send_hermes_verification_request
            result = maybe_send_hermes_verification_request(
                _BASE_PREVIEW, _BASE_VERIFICATION, _BASE_POLICY
            )
        self.assertTrue(result["skipped"])
        # ok=False이지만 preview/verification에 영향을 주는 side effect 없음


# ── 3. mirror enabled mock → Telegram send mock ───────────

class TestMirrorEnabledMock(unittest.TestCase):
    def _run_with_mock(self):
        import importlib
        import core.toss_live_pilot_hermes_bridge as bridge_mod
        importlib.reload(bridge_mod)

        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "true",
                         "HERMES_VERIFY_CHAT_ID": "99999",
                         "TELEGRAM_BOT_TOKEN": "fake:token",
                         "HERMES_VERIFY_THREAD_ID": ""}):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_post.return_value = mock_resp
                result = bridge_mod.maybe_send_hermes_verification_request(
                    _BASE_PREVIEW, _BASE_VERIFICATION, _BASE_POLICY
                )
        return result, mock_post

    def test_telegram_send_called(self):
        _, mock_post = self._run_with_mock()
        mock_post.assert_called_once()

    def test_result_ok_true(self):
        result, _ = self._run_with_mock()
        self.assertTrue(result["ok"])

    def test_sent_true(self):
        result, _ = self._run_with_mock()
        self.assertTrue(result.get("sent"))

    def test_verification_id_in_result(self):
        result, _ = self._run_with_mock()
        self.assertEqual(result.get("verification_id"), "hv_test_001")

    def test_message_contains_verify_block(self):
        """전송된 메시지에 [HERMES_LIVE_PILOT_VERIFY] 블록 포함."""
        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "true",
                         "HERMES_VERIFY_CHAT_ID": "99999",
                         "TELEGRAM_BOT_TOKEN": "fake:token",
                         "HERMES_VERIFY_THREAD_ID": ""}):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_post.return_value = mock_resp
                from core.toss_live_pilot_hermes_bridge import maybe_send_hermes_verification_request
                maybe_send_hermes_verification_request(
                    _BASE_PREVIEW, _BASE_VERIFICATION, _BASE_POLICY
                )
        call_kwargs = mock_post.call_args
        sent_text = call_kwargs[1]["json"]["text"]  # or call_args.kwargs
        self.assertIn("[HERMES_LIVE_PILOT_VERIFY]", sent_text)
        self.assertIn("[/HERMES_LIVE_PILOT_VERIFY]", sent_text)

    def test_no_token_in_log(self):
        """requests.post URL에 토큰이 포함되지만 로그엔 출력 안 됨 — 결과에 없음."""
        result, _ = self._run_with_mock()
        result_str = str(result)
        for kw in ("APP_KEY", "APP_SECRET", "accountNo", "Bearer"):
            self.assertNotIn(kw, result_str)

    def test_live_order_sent_false(self):
        result, _ = self._run_with_mock()
        self.assertFalse(result.get("live_order_sent", False))


# ── 4. get_mirror_status ──────────────────────────────────

class TestGetMirrorStatus(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "",
                         "HERMES_VERIFY_CHAT_ID": ""}):
            from core.toss_live_pilot_hermes_bridge import get_mirror_status
            s = get_mirror_status()
        self.assertFalse(s["mirror_enabled"])
        self.assertFalse(s["mirror_target_configured"])

    def test_no_sensitive_in_status(self):
        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "true",
                         "HERMES_VERIFY_CHAT_ID": "99999",
                         "TELEGRAM_BOT_TOKEN": "fake:token"}):
            from core.toss_live_pilot_hermes_bridge import get_mirror_status
            s = get_mirror_status()
        # chat_id 값 자체가 노출되면 안 됨 — 존재 여부(bool)만 반환
        self.assertNotIn("99999", str(s))
        self.assertNotIn("fake:token", str(s))

    def test_target_configured_when_chat_set(self):
        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "true",
                         "HERMES_VERIFY_CHAT_ID": "99999"}):
            from core.toss_live_pilot_hermes_bridge import get_mirror_status
            s = get_mirror_status()
        self.assertTrue(s["mirror_target_configured"])


# ── 5. Telegram send failed → ok=False, skipped=False ────

class TestMirrorSendFailed(unittest.TestCase):
    def test_send_failure_returns_ok_false(self):
        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "true",
                         "HERMES_VERIFY_CHAT_ID": "99999",
                         "TELEGRAM_BOT_TOKEN": "fake:token"}):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 400
                mock_post.return_value = mock_resp
                from core.toss_live_pilot_hermes_bridge import maybe_send_hermes_verification_request
                result = maybe_send_hermes_verification_request(
                    _BASE_PREVIEW, _BASE_VERIFICATION, _BASE_POLICY
                )
        self.assertFalse(result["ok"])
        self.assertFalse(result.get("skipped", True))
        self.assertEqual(result.get("reason"), "telegram_send_failed")

    def test_send_exception_returns_ok_false(self):
        with patch.dict(__import__("os").environ,
                        {"HERMES_VERIFY_MIRROR_ENABLED": "true",
                         "HERMES_VERIFY_CHAT_ID": "99999",
                         "TELEGRAM_BOT_TOKEN": "fake:token"}):
            with patch("requests.post", side_effect=Exception("network error")):
                from core.toss_live_pilot_hermes_bridge import maybe_send_hermes_verification_request
                result = maybe_send_hermes_verification_request(
                    _BASE_PREVIEW, _BASE_VERIFICATION, _BASE_POLICY
                )
        self.assertFalse(result["ok"])


# ── 6. API summary에 mirror 정보 포함 ──────────────────────

class TestDashboardDataMirrorSummary(unittest.TestCase):
    def test_verifications_data_includes_mirror_fields(self):
        with patch("core.toss_live_pilot_hermes_bridge.get_mirror_status",
                   return_value={"mirror_enabled": False, "mirror_target_configured": False}):
            from core.dashboard_data import toss_live_pilot_verifications_data
            data = toss_live_pilot_verifications_data(limit=1)
        self.assertIn("mirror_enabled", data)
        self.assertIn("mirror_target_configured", data)
        self.assertIn("pending_count", data)
        self.assertIn("expired_count", data)

    def test_live_order_allowed_always_false(self):
        from core.dashboard_data import toss_live_pilot_verifications_data
        data = toss_live_pilot_verifications_data(limit=1)
        self.assertFalse(data.get("live_order_allowed", True))


# ── 7. no sensitive in source ─────────────────────────────

class TestNoSensitiveInSource(unittest.TestCase):
    def test_no_hardcoded_secrets(self):
        import re
        src = (_ROOT / "core" / "toss_live_pilot_hermes_bridge.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'\d{8}-\d{2}', src), [])
        self.assertEqual(re.findall(r'Bearer [A-Za-z0-9._\-]{20,}', src), [])

    def test_no_http_write_routes(self):
        src = (_ROOT / "core" / "toss_live_pilot_hermes_bridge.py").read_text(encoding="utf-8")
        import re
        src_no_doc = re.sub(r'"""[\s\S]*?"""', "", src)
        src_no_doc = re.sub(r"#[^\n]*", "", src_no_doc)
        self.assertNotIn("requests.post", src_no_doc.replace("req.post", ""))
        # _send_to_hermes_channel은 req.post 사용이 허용됨 — Telegram 전용
        # 하지만 실제 Toss 주문 API가 없어야 함
        self.assertNotIn("/api/v1/orders", src)
        self.assertNotIn("/trade", src)


if __name__ == "__main__":
    unittest.main()
