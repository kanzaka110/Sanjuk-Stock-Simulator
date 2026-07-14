"""tests/test_toss_live_pilot_live_callback.py

Telegram confirm callback — policy 상태에 따른 분기 테스트.
- policy disabled → 기존 차단 문구
- policy enabled + guard blocked → 조건 미충족 차단
- policy enabled + fake success → success 문구 (live_order_sent=True)
- policy enabled + transport=None → blocked
- 금지 CTA 없음
- Paper SOFI 미접촉
- web write route 없음
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import os
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_telegram import handle_live_pilot_callback

_ALL_GATES_ENV = {
    "TOSS_LIVE_PILOT_ENABLED": "true",
    "TOSS_LIVE_ORDER_ALLOWED": "true",
    "TOSS_LIVE_ADAPTER_ENABLED": "true",
}

_CLEARED_ENV = {
    "TOSS_LIVE_PILOT_ENABLED": "",
    "TOSS_LIVE_ORDER_ALLOWED": "",
    "TOSS_LIVE_ADAPTER_ENABLED": "",
}


def _seal_quality_proof_for_test(qg, candidate):
    candidate.setdefault("side", "buy")
    breakdown = candidate["quality_breakdown"]
    breakdown["decision_bucket"] = candidate.get("decision_bucket", "")
    breakdown["decision_reason"] = candidate.get("decision_reason", "")
    breakdown["score_symbol"] = str(candidate.get("symbol") or candidate.get("ticker") or "").upper()
    breakdown["score_side"] = str(candidate.get("side") or "buy").lower()
    event_penalty = float(breakdown.get("penalty_event_risk") or 0.0)
    breakdown.update({
        "decision_change_pct": float(candidate.get("change_pct") or 0.0),
        "decision_days_to_earnings": 0 if event_penalty == -15.0 else (5 if event_penalty == -5.0 else -1),
        "decision_has_stop": bool(candidate.get("stop_loss")),
        "decision_has_target": bool(candidate.get("target_price")),
        "decision_blocking_risk_flags": list(candidate.get("blocking_risk_flags") or []),
        "decision_origin_bucket": breakdown["decision_bucket"],
        "decision_origin_reason": breakdown["decision_reason"],
    })
    breakdown["score_schema_version"] = qg.QUALITY_SCORE_SCHEMA_VERSION
    weight_hash = qg._weight_profile_hash()
    breakdown["weight_profile_hash"] = weight_hash
    breakdown["score_breakdown_sha256"] = qg._score_breakdown_hash(
        breakdown, schema_version=qg.QUALITY_SCORE_SCHEMA_VERSION,
        weight_hash=weight_hash,
    )
    assert breakdown["score_breakdown_sha256"]
    assert qg.attach_quality_proof(candidate) is True


def _make_db():
    """임시 DB + 패치 반환 (setUp용)."""
    tmp = tempfile.mkdtemp()
    p = Path(tmp) / "test_pilot.db"
    return tmp, p


def _create_pilot(db_patch_target, preview_ok=True, symbol="091180.KS"):
    from core.toss_live_pilot_ledger import record_live_pilot_preview
    preview = {
        "ok": preview_ok,
        "preview_id": "tlive_cb_test",
        "symbol": symbol,
        "side": "buy",
        "quantity": 1,
        "limit_price": 30000.0,
        "estimated_amount_krw": 30000.0,
        "stop_loss": 28000.0,
        "target_price": 34000.0,
        "invalidation": "below 28000",
        "blocks": [] if preview_ok else ["test_block"],
        "warnings": [],
    }
    return record_live_pilot_preview(preview)


# ─── 1. confirm — policy disabled ────────────────────────

class TestConfirmPolicyDisabled(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db_path = _make_db()
        self._db_patch = patch(
            "core.toss_live_pilot_ledger._db_path", return_value=self.db_path
        )
        self._db_patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False
        self._env_patch = patch.dict(os.environ, _CLEARED_ENV)
        self._env_patch.start()
        # Hermes 게이트 PASS로 우회 — 이 클래스는 adapter 비활성 레이어 테스트
        self._hermes_patch = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._hermes_patch.start()

    def tearDown(self):
        self._hermes_patch.stop()
        self._db_patch.stop()
        self._env_patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def test_confirm_disabled_ok_false(self):
        rec = _create_pilot(self.db_path)
        result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertFalse(result["ok"])

    def test_confirm_disabled_live_order_sent_false(self):
        rec = _create_pilot(self.db_path)
        result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertFalse(result["live_order_sent"])

    def test_confirm_disabled_blocked_true(self):
        rec = _create_pilot(self.db_path)
        result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertTrue(result.get("blocked"))

    def test_confirm_disabled_message_not_sent(self):
        rec = _create_pilot(self.db_path)
        result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertIn("아직 주문 전송 안 함", result["message"])

    def test_confirm_disabled_message_disabled(self):
        rec = _create_pilot(self.db_path)
        result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertIn("비활성", result["message"])

    def test_confirm_disabled_no_forbidden_cta(self):
        rec = _create_pilot(self.db_path)
        result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        for phrase in ("매수하기", "매도하기", "주문 실행", "자동매매 시작", "실주문: 활성"):
            self.assertNotIn(phrase, result["message"])


# ─── 2. confirm — policy enabled + transport=None ────────

class TestConfirmEnabledNoTransport(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db_path = _make_db()
        self._db_patch = patch(
            "core.toss_live_pilot_ledger._db_path", return_value=self.db_path
        )
        self._db_patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False
        self._env_patch = patch.dict(os.environ, _ALL_GATES_ENV)
        self._env_patch.start()
        # Hermes/quality 게이트는 PASS로 고정 — 이 클래스는 transport=None 레이어 테스트
        self._hermes_patch = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._quality_patch = patch(
            "core.toss_quality_gate.validate_execution_quality_decision",
            return_value={"ok": True, "reason": "quality_decision_exact"},
        )
        self._hermes_patch.start()
        self._quality_patch.start()

    def tearDown(self):
        self._quality_patch.stop()
        self._hermes_patch.stop()
        self._db_patch.stop()
        self._env_patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def test_confirm_enabled_no_transport_live_order_sent_false(self):
        rec = _create_pilot(self.db_path)
        result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertFalse(result["live_order_sent"])

    def test_confirm_enabled_no_transport_blocked(self):
        rec = _create_pilot(self.db_path)
        result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        # transport=None이므로 dispatch에서 차단
        self.assertFalse(result.get("ok") and result.get("live_order_sent"))


# ─── 3. confirm — policy enabled + fake success transport ─

class TestConfirmEnabledFakeSuccess(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db_path = _make_db()
        self._db_patch = patch(
            "core.toss_live_pilot_ledger._db_path", return_value=self.db_path
        )
        self._db_patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False
        self._env_patch = patch.dict(os.environ, _ALL_GATES_ENV)
        self._env_patch.start()
        # Hermes/quality 게이트는 PASS로 고정 — 이 클래스는 adapter/transport 레이어 테스트
        self._hermes_patch = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._quality_patch = patch(
            "core.toss_quality_gate.validate_execution_quality_decision",
            return_value={"ok": True, "reason": "quality_decision_exact"},
        )
        self._hermes_patch.start()
        self._quality_patch.start()

    def tearDown(self):
        self._quality_patch.stop()
        self._hermes_patch.stop()
        self._db_patch.stop()
        self._env_patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def _fake_success_transport(self, payload, policy):
        return {
            "ok": True, "live_order_sent": True,
            "broker_order_id": "ORD-FAKE-001", "status": "submitted",
        }

    def test_fake_success_live_order_sent_true(self):
        rec = _create_pilot(self.db_path)
        with patch(
            "core.toss_live_pilot_adapter.dispatch_toss_order_live",
            return_value={
                "ok": True, "live_order_sent": True,
                "broker_order_id": "ORD-FAKE-001",
                "payload_hash": "abc123",
                "message": "승인형 live pilot 주문 전송 완료\n자동매매 아님\n사용자 최종 승인 1건\nlive_order_sent=true",
            }
        ), patch(
            "core.toss_live_pilot_adapter.can_send_live_pilot_order",
            return_value=(True, [])
        ):
            result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertTrue(result["live_order_sent"])
        self.assertTrue(result["ok"])

    def test_fake_success_message_no_auto_trade(self):
        rec = _create_pilot(self.db_path)
        with patch(
            "core.toss_live_pilot_adapter.dispatch_toss_order_live",
            return_value={
                "ok": True, "live_order_sent": True,
                "broker_order_id": "ORD-FAKE-001",
                "payload_hash": "abc123",
                "message": "승인형 live pilot 주문 전송 완료\n자동매매 아님\n사용자 최종 승인 1건\nlive_order_sent=true",
            }
        ), patch(
            "core.toss_live_pilot_adapter.can_send_live_pilot_order",
            return_value=(True, [])
        ):
            result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        # 금지 CTA 없음
        for phrase in ("자동매매 시작", "자동거래 시작", "매수하기", "실주문: 활성"):
            self.assertNotIn(phrase, result["message"])

    def test_fake_success_ledger_live_sent(self):
        from core.toss_live_pilot_ledger import list_live_pilot_records
        rec = _create_pilot(self.db_path)
        with patch(
            "core.toss_live_pilot_adapter.dispatch_toss_order_live",
            return_value={
                "ok": True, "live_order_sent": True,
                "broker_order_id": "ORD-FAKE-001",
                "payload_hash": "abc123",
                "message": "승인형 live pilot 주문 전송 완료\n자동매매 아님",
            }
        ), patch(
            "core.toss_live_pilot_adapter.can_send_live_pilot_order",
            return_value=(True, [])
        ):
            handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        records = list_live_pilot_records()
        matched = [r for r in records if r["pilot_id"] == rec["pilot_id"]]
        self.assertTrue(matched)
        self.assertEqual(matched[0]["status"], "live_sent")
        self.assertTrue(bool(matched[0]["live_order_sent"]))


# ─── 4. confirm — exact quality row gate ──────────────────

class TestConfirmExactQualityLastMile(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db_path = _make_db()
        self.quality_path = Path(self.tmp) / "quality.db"
        self._db_patch = patch(
            "core.toss_live_pilot_ledger._db_path", return_value=self.db_path
        )
        self._quality_patch = patch(
            "core.toss_quality_gate._outcomes_db_path", return_value=self.quality_path
        )
        self._db_patch.start()
        self._quality_patch.start()
        import core.toss_live_pilot_ledger as ledger
        import core.toss_quality_gate as qg
        ledger._schema_created = False
        qg._outcomes_schema_created = False
        self._env_patch = patch.dict(os.environ, _ALL_GATES_ENV)
        self._env_patch.start()
        self._hermes_patch = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._hermes_patch.start()

    def tearDown(self):
        self._hermes_patch.stop()
        self._env_patch.stop()
        self._quality_patch.stop()
        self._db_patch.stop()
        import core.toss_live_pilot_ledger as ledger
        import core.toss_quality_gate as qg
        ledger._schema_created = False
        qg._outcomes_schema_created = False

    def test_missing_quality_row_blocks_before_dispatch(self):
        rec = _create_pilot(self.db_path)
        with patch(
            "core.toss_live_pilot_adapter.can_send_live_pilot_order",
            return_value=(True, []),
        ), patch(
            "core.toss_live_pilot_telegram.resolve_live_transport_for_confirm",
            return_value=object(),
        ) as resolver, patch(
            "core.toss_live_pilot_adapter.dispatch_toss_order_live",
            return_value={"ok": True, "live_order_sent": True},
        ) as dispatch:
            result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")

        self.assertFalse(result["ok"])
        self.assertFalse(result["live_order_sent"])
        self.assertEqual(result["reason"], "quality_decision_missing")
        resolver.assert_not_called()
        dispatch.assert_not_called()

    def test_exact_quality_row_allows_transport_resolution(self):
        from core import toss_quality_gate as qg
        from core.toss_live_pilot_telegram import handle_live_pilot_callback

        created_rec = _create_pilot(self.db_path)
        from core.toss_live_pilot_ledger import list_live_pilot_records
        rec = next(
            row for row in list_live_pilot_records()
            if row["pilot_id"] == created_rec["pilot_id"]
        )
        candidate = {
            "symbol": rec["symbol"],
            "side": "buy",
            "quantity": rec["quantity"],
            "limit_price": rec["limit_price"],
            "stop_loss": rec["stop_loss"],
            "target_price": rec["target_price"],
            "risk_reward": 2.0,
            "decision_bucket": "PASS_EXECUTE",
            "decision_reason": "quality pass",
            "quality_score": 82.0,
            "quality_breakdown": {
                "score_total": 82.0,
                "score_momentum": 20.0,
                "score_liquidity": 15.0,
                "score_risk_reward": 15.0,
                "score_reliability": 10.0,
                "score_market_regime": 10.0,
                "score_supply_demand": 12.0,
                "penalty_overheat": 0.0,
                "penalty_duplicate": 0.0,
                "penalty_event_risk": 0.0,
                "rr_ratio": 2.0,
                "regime": "neutral",
            },
        }
        _seal_quality_proof_for_test(qg, candidate)
        created = qg.record_execution_quality_decision(
            candidate,
            pilot_id=rec["pilot_id"],
            decision_ref=rec["decision_ref"],
        )
        self.assertTrue(created["ok"])

        with patch(
            "core.toss_live_pilot_adapter.can_send_live_pilot_order",
            return_value=(True, []),
        ), patch(
            "core.toss_live_pilot_telegram.resolve_live_transport_for_confirm",
            return_value=object(),
        ) as resolver, patch(
            "core.toss_live_pilot_adapter.dispatch_toss_order_live",
            return_value={"ok": True, "live_order_sent": True},
        ) as dispatch:
            result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")

        self.assertTrue(result["ok"])
        self.assertTrue(result["live_order_sent"])
        resolver.assert_called_once()
        dispatch.assert_called_once()

    def test_quality_lookup_error_blocks_before_transport_resolution(self):
        from core import toss_quality_gate as qg
        from core.toss_live_pilot_telegram import handle_live_pilot_callback

        rec = _create_pilot(self.db_path)
        with patch(
            "core.toss_live_pilot_adapter.can_send_live_pilot_order",
            return_value=(True, []),
        ), patch.object(
            qg,
            "validate_execution_quality_decision",
            side_effect=RuntimeError("synthetic"),
        ), patch(
            "core.toss_live_pilot_telegram.resolve_live_transport_for_confirm",
            return_value=object(),
        ) as resolver, patch(
            "core.toss_live_pilot_adapter.dispatch_toss_order_live",
            return_value={"ok": True, "live_order_sent": True},
        ) as dispatch:
            result = handle_live_pilot_callback(
                f"tlp:confirm:{rec['pilot_id']}"
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["live_order_sent"])
        self.assertEqual(result["reason"], "quality_decision_unavailable")
        resolver.assert_not_called()
        dispatch.assert_not_called()


# ─── 4. confirm — guard blocked ──────────────────────────

class TestConfirmGuardBlocked(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db_path = _make_db()
        self._db_patch = patch(
            "core.toss_live_pilot_ledger._db_path", return_value=self.db_path
        )
        self._db_patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False
        self._env_patch = patch.dict(os.environ, _ALL_GATES_ENV)
        self._env_patch.start()
        # Hermes 게이트 PASS로 우회
        self._hermes_patch = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._hermes_patch.start()

    def tearDown(self):
        self._hermes_patch.stop()
        self._db_patch.stop()
        self._env_patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def test_guard_blocked_live_order_sent_false(self):
        rec = _create_pilot(self.db_path)
        with patch(
            "core.toss_live_pilot_adapter.can_send_live_pilot_order",
            return_value=(False, ["amount_over_limit"])
        ):
            result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertFalse(result["live_order_sent"])

    def test_guard_blocked_message(self):
        rec = _create_pilot(self.db_path)
        with patch(
            "core.toss_live_pilot_adapter.can_send_live_pilot_order",
            return_value=(False, ["daily_order_count_exceeded: 1/1"])
        ):
            result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertIn("차단", result["message"])
        self.assertIn("live_order_sent=false", result["message"])


# ─── 5. ledger live_sent / live_send_failed ───────────────

class TestLedgerLiveStatuses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patch = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "test_pilot.db",
        )
        self._patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def tearDown(self):
        self._patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def _make_pilot(self):
        from core.toss_live_pilot_ledger import record_live_pilot_preview
        rec = record_live_pilot_preview({
            "ok": True, "preview_id": "tlive_ls",
            "symbol": "091180.KS", "side": "buy", "quantity": 1,
            "limit_price": 30000, "estimated_amount_krw": 30000,
            "blocks": [], "warnings": [],
        })
        return rec["pilot_id"]

    def test_record_live_sent(self):
        from core.toss_live_pilot_ledger import record_live_sent, list_live_pilot_records
        pid = self._make_pilot()
        result = record_live_sent(pid, broker_order_id="ORD-FAKE", payload_hash="abc")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "live_sent")
        self.assertTrue(result["live_order_sent"])
        records = list_live_pilot_records()
        matched = [r for r in records if r["pilot_id"] == pid]
        self.assertEqual(matched[0]["status"], "live_sent")
        self.assertTrue(bool(matched[0]["live_order_sent"]))

    def test_record_live_send_failed(self):
        from core.toss_live_pilot_ledger import record_live_send_failed, list_live_pilot_records
        pid = self._make_pilot()
        result = record_live_send_failed(pid, failure_reason="exchange_rejected")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "live_send_failed")
        self.assertFalse(result["live_order_sent"])
        records = list_live_pilot_records()
        matched = [r for r in records if r["pilot_id"] == pid]
        self.assertEqual(matched[0]["status"], "live_send_failed")
        self.assertFalse(bool(matched[0]["live_order_sent"]))

    def test_record_live_send_blocked(self):
        from core.toss_live_pilot_ledger import record_live_send_blocked, list_live_pilot_records
        pid = self._make_pilot()
        result = record_live_send_blocked(pid, ["amount_over_limit"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "live_send_blocked")
        self.assertFalse(result["live_order_sent"])

    def test_ledger_live_sent_no_sensitive_fields(self):
        from core.toss_live_pilot_ledger import record_live_sent, list_live_pilot_records
        pid = self._make_pilot()
        record_live_sent(pid, broker_order_id="ORD-SAFE", payload_hash="xyz")
        records = list_live_pilot_records()
        rec_str = str(records)
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, rec_str)


# ─── 6. web route GET-only ───────────────────────────────

class TestWebRouteGetOnly(unittest.TestCase):
    def test_no_write_routes_in_app(self):
        src = (_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        for pat in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            self.assertNotIn(pat, src.lower())


# ─── 7. Paper SOFI 미접촉 ────────────────────────────────

class TestPaperSOFIUnaffected(unittest.TestCase):
    def test_sofi_paper_open_unchanged(self):
        from core.toss_paper_performance import get_paper_performance_summary
        before = get_paper_performance_summary().get("summary", {}).get("open", 0)
        # confirm disabled 시나리오
        with patch.dict(os.environ, _CLEARED_ENV):
            handle_live_pilot_callback("tlp:confirm:nonexistent_id")
        after = get_paper_performance_summary().get("summary", {}).get("open", 0)
        self.assertEqual(before, after)

    def test_paper_approve_not_called_by_confirm(self):
        with patch("core.toss_paper_ledger.approve_paper_order") as mock, \
             patch.dict(os.environ, _CLEARED_ENV):
            handle_live_pilot_callback("tlp:confirm:nonexistent_id")
            mock.assert_not_called()


# ─── 8. 금지 CTA 소스 체크 ───────────────────────────────

class TestNoForbiddenCTAInSources(unittest.TestCase):
    def _code_lines(self, path) -> str:
        import re
        src = path.read_text(encoding="utf-8")
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        src = re.sub(r"'''[\s\S]*?'''", "", src)
        src = re.sub(r"#[^\n]*", "", src)
        return src

    def test_no_forbidden_in_telegram(self):
        src = self._code_lines(_ROOT / "core" / "toss_live_pilot_telegram.py")
        for phrase in ("자동매매 시작", "자동거래 시작", "실주문: 활성"):
            self.assertNotIn(phrase, src)

    def test_no_forbidden_in_adapter(self):
        src = self._code_lines(_ROOT / "core" / "toss_live_pilot_adapter.py")
        for phrase in ("자동매매 시작", "자동거래 시작"):
            self.assertNotIn(phrase, src)


if __name__ == "__main__":
    unittest.main()
