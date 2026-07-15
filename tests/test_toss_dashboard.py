"""
Toss dashboard endpoint 단위 테스트

- /api/toss/account-summary GET-only 검증
- 기존 /api/portfolio 합산 오염 없음
- 응답 필수 필드 검증
- 민감정보 미포함
- HTML 문구 존재 검증
- CTA 버튼 금지
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _summary_tests_run_as_owner(monkeypatch):
    """(2026-07-15 계약) 비소유 프로세스는 브로커 직접 조회가 차단된다.

    이 파일은 브로커 응답 → 요약 가공 로직 자체를 검증하므로 owner를
    명시한다. 운영 dashboard는 consumer로서 snapshot을 소비한다.
    """
    monkeypatch.setenv("TOSS_PROCESS_ROLE", "broker_owner")


# ═══ dashboard_data.toss_account_summary 테스트 ═══

class TestTossAccountSummary:
    """toss_account_summary 응답 구조 검증."""

    def _get_summary(self, configured=True, accounts=None, holdings=None, fx=None,
                     buying_power=None):
        """mock된 toss_client로 summary 생성."""
        if accounts is None:
            accounts = [{"accountSeq": 1, "accountType": "BROKERAGE", "accountNo": "99900001234"}]
        if holdings is None:
            holdings = {"items": [], "marketValue": {"amount": {"krw": "0", "usd": None}}}
        if fx is None:
            fx = {"baseCurrency": "USD", "quoteCurrency": "KRW", "rate": "1538.5"}
        if buying_power is None:
            buying_power = {"currency": "KRW", "cashBuyingPower": "10000000"}

        from core.dashboard_data import _fetch_toss_account_summary_raw

        with patch("core.toss_client.is_configured", return_value=configured), \
             patch("core.toss_client.get_accounts", return_value=accounts), \
             patch("core.toss_client.get_holdings", return_value=holdings), \
             patch("core.toss_client.get_exchange_rate", return_value=fx), \
             patch("core.toss_client.get_buying_power", return_value=buying_power), \
             patch("core.toss_client.sanitize_dict", side_effect=lambda x: x), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", return_value={}):
            return _fetch_toss_account_summary_raw()

    def test_included_in_total_portfolio_is_false(self):
        d = self._get_summary()
        assert d["included_in_total_portfolio"] is False

    def test_trading_enabled_is_false(self):
        d = self._get_summary()
        assert d["trading_enabled"] is False

    def test_automation_status_is_disabled(self):
        d = self._get_summary()
        assert d["automation_status"] == "disabled"

    def test_separate_from_portfolio_is_true(self):
        d = self._get_summary()
        assert d["separate_from_portfolio"] is True

    def test_label(self):
        d = self._get_summary()
        assert d["label"] == "Toss 실전 AI 자동거래 계좌"

    def test_label_no_experiment_wording(self):
        d = self._get_summary()
        assert "실험" not in d["label"]
        assert "모의" not in d["label"]

    def test_account_no_masked(self):
        d = self._get_summary()
        for acct in d["accounts"]:
            assert acct["account_no_masked"] == "[REDACTED]"
            assert "99900001234" not in str(acct)

    def test_no_raw_account_no_in_response(self):
        d = self._get_summary()
        s = str(d)
        assert "99900001234" not in s

    def test_no_token_in_response(self):
        d = self._get_summary()
        s = str(d)
        assert "access_token" not in s
        assert "Bearer " not in s

    def test_warnings_present(self):
        d = self._get_summary()
        warns = " ".join(d.get("warnings", []))
        assert "합산" in warns
        assert "주문" in warns

    def test_not_configured(self):
        d = self._get_summary(configured=False)
        assert d["enabled"] is False
        assert d["account_count"] == 0

    def test_accounts_retry_after_empty_response(self):
        from core.dashboard_data import _fetch_toss_account_summary_raw

        accounts = [{"accountSeq": 1, "accountType": "BROKERAGE", "accountNo": "99900001234"}]
        with (
            patch("core.toss_client.is_configured", return_value=True),
            patch("core.toss_client.get_accounts", side_effect=[[], accounts]),
            patch("core.toss_client.get_holdings", return_value={"items": [], "marketValue": {"amount": {"krw": "0"}}}),
            patch("core.toss_client.get_exchange_rate", return_value={}),
            patch("core.toss_client.get_buying_power", return_value={}),
            patch("core.toss_client.sanitize_dict", side_effect=lambda x: x),
            patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", return_value={}),
        ):
            d = _fetch_toss_account_summary_raw()

        assert d["account_count"] == 1

    def test_with_holdings(self):
        holdings = {
            "items": [{"symbol": "AAPL", "quantity": 10}],
            "marketValue": {"amount": {"krw": "5000000", "usd": "3500"}},
        }
        d = self._get_summary(holdings=holdings)
        assert d["holdings_count"] == 1
        assert d["market_value"]["krw"] == 5000000.0 + 3500 * 1538.5

    def test_exchange_rate_present(self):
        d = self._get_summary()
        fx = d["exchange_rate"]
        assert fx is not None
        assert fx["rate"] == 1538.5
        assert fx["source"] == "Toss"

    def test_cash_field_present(self):
        d = self._get_summary()
        assert "cash" in d
        assert d["cash"]["krw"] == 10000000.0
        assert d["cash"]["source"] == "Toss"

    def test_total_account_value_present(self):
        d = self._get_summary()
        assert "total_account_value" in d
        # cash 10M + market_value 0 = 10M; USD cash is counted only when the returned currency is USD
        assert d["total_account_value"]["krw"] == 10000000.0

    def test_total_with_holdings(self):
        holdings = {
            "items": [{"symbol": "AAPL", "quantity": 10}],
            "marketValue": {"amount": {"krw": "5000000", "usd": "3500"}},
        }
        bp = {"currency": "KRW", "cashBuyingPower": "2000000"}
        d = self._get_summary(holdings=holdings, buying_power=bp)
        assert d["market_value"]["krw"] == 5000000.0 + 3500 * 1538.5
        assert d["cash"]["krw"] == 2000000.0
        assert d["total_account_value"]["krw"] == 7000000.0 + 3500 * 1538.5

    def test_no_experiment_wording_in_warnings(self):
        d = self._get_summary()
        warns = " ".join(d.get("warnings", []))
        assert "실험" not in warns


    def test_empty_accounts_fail_fast_before_fx(self):
        from core.dashboard_data import _fetch_toss_account_summary_raw

        with (
            patch("core.toss_client.is_configured", return_value=True),
            patch("core.toss_client.get_accounts", side_effect=[[], []]),
            patch("core.toss_client.get_holdings") as get_holdings,
            patch("core.toss_client.get_exchange_rate") as get_fx,
            patch("core.toss_client.get_buying_power") as get_bp,
            patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", return_value={}),
        ):
            d = _fetch_toss_account_summary_raw()

        assert d["account_count"] == 0
        assert d["error"] == "Toss account unavailable"
        assert d["data_quality"]["reason"] == "accounts_empty_after_retry"
        get_holdings.assert_not_called()
        get_fx.assert_not_called()
        get_bp.assert_not_called()

    def test_policy_fast_fallback_does_not_block_dashboard(self, monkeypatch):
        import time
        from core import dashboard_data as dd

        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setenv("TOSS_LIVE_PILOT_ENABLED", "true")
        monkeypatch.setenv("TOSS_LIVE_ORDER_ALLOWED", "true")
        monkeypatch.setenv("TOSS_LIVE_ADAPTER_ENABLED", "true")
        monkeypatch.setenv("TOSS_LIVE_TRANSPORT_ARMED", "true")
        monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
        monkeypatch.setenv("TOSS_AUTONOMOUS_KILL_SWITCH", "false")
        monkeypatch.setenv("TOSS_AUTONOMOUS_ALLOWED_ASSET_TYPES", "US_STOCK,KR_STOCK")
        dd._cache.pop("toss_live_policy_fast", None)
        dd._cache.pop("toss_live_policy_last_error", None)
        dd._toss_policy_refreshing = None

        def slow_policy():
            time.sleep(0.15)
            return {"live_order_allowed": False, "adapter_status": "slow"}

        started = time.monotonic()
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", side_effect=slow_policy):
            policy = dd._toss_live_policy_fast(timeout=0.01)

        assert time.monotonic() - started < 0.08
        assert policy["cache_status"] == "fallback"
        assert policy["live_order_allowed"] is True
        assert policy["adapter_status"] == "enabled"

        # background refresh may still be finishing; reset so later tests are isolated
        if dd._toss_policy_refreshing:
            dd._toss_policy_refreshing.wait(0.3)
        dd._cache.pop("toss_live_policy_fast", None)
        dd._cache.pop("toss_live_policy_last_error", None)
        dd._toss_policy_refreshing = None

    def test_account_summary_uses_stale_cache_during_cooldown(self):
        import time
        from core import dashboard_data as dd

        good = {
            "enabled": True,
            "label": "Toss 실전 AI 자동거래 계좌",
            "separate_from_portfolio": True,
            "included_in_total_portfolio": False,
            "trading_enabled": True,
            "automation_status": "autonomous_live_pilot",
            "live_policy": {},
            "account_count": 1,
            "accounts": [{"account_seq": 1, "account_type": "BROKERAGE", "account_no_masked": "[REDACTED]"}],
            "holdings_count": 1,
            "holdings_items": [{"symbol": "AAPL"}],
            "market_value": {"krw": 1000, "usd": None},
            "cash": {"krw": 2000, "usd": None, "source": "Toss"},
            "total_account_value": {"krw": 3000, "usd": None},
            "exchange_rate": None,
            "warnings": [],
            "updated_at": "test",
            "error": "",
        }
        bad = {
            **good,
            "account_count": 0,
            "accounts": [],
            "holdings_count": 0,
            "holdings_items": [],
            "error": "Toss account unavailable",
        }

        dd._cache.pop("toss_account_summary", None)
        dd._toss_account_summary_last_good = None
        dd._toss_account_summary_cooldown_until = 0.0

        with patch.object(dd, "_fetch_toss_account_summary_raw", side_effect=[good, bad]) as fetch:
            live = dd.toss_account_summary()
            assert live["cache_status"] == "live"

            with dd._cache_lock:
                dd._cache["toss_account_summary"] = (time.monotonic() - 61, live)
            stale = dd.toss_account_summary()

            assert stale["cache_status"] == "stale"
            assert stale["account_count"] == 1
            assert stale["total_account_value"]["krw"] == 3000
            assert stale["stale_reason"] == "Toss account unavailable"

            dd._cache.pop("toss_account_summary", None)
            again = dd.toss_account_summary()
            assert again["cache_status"] == "stale"
            assert fetch.call_count == 2

        dd._cache.pop("toss_account_summary", None)
        dd._toss_account_summary_last_good = None
        dd._toss_account_summary_cooldown_until = 0.0


# ═══ 기존 포트폴리오 오염 없음 ═══

class TestPortfolioNotContaminated:
    """Toss 데이터 존재 여부가 /api/portfolio에 영향 없음."""

    def test_portfolio_data_does_not_import_toss(self):
        """portfolio_data 함수가 toss_client를 임포트하지 않음."""
        from core.dashboard_data import _fetch_portfolio_raw
        source = Path(ROOT / "core" / "dashboard_data.py").read_text()
        # _fetch_portfolio_raw 함수 소스만 추출
        import inspect
        fn_source = inspect.getsource(_fetch_portfolio_raw)
        assert "toss_client" not in fn_source
        assert "toss" not in fn_source.lower().replace("toss_account_summary", "")


# ═══ API endpoint 검증 ═══

class TestApiEndpoint:
    """web/app.py에 올바른 GET endpoint 등록 확인."""

    def test_toss_endpoint_exists(self):
        source = (ROOT / "web" / "app.py").read_text()
        assert '/api/toss/account-summary' in source

    def test_toss_endpoint_is_get(self):
        source = (ROOT / "web" / "app.py").read_text()
        # @app.get 데코레이터 뒤에 toss endpoint가 있는지
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "toss/account-summary" in line:
                # 데코레이터 라인 확인
                assert "@app.get" in line or (i > 0 and "@app.get" in lines[i - 1])
                break
        else:
            pytest.fail("toss endpoint not found")

    def test_no_post_toss_endpoint(self):
        source = (ROOT / "web" / "app.py").read_text()
        assert "@app.post" not in source or "toss" not in source.split("@app.post")[1].split("\n")[0] if "@app.post" in source else True


# ═══ HTML 탭/문구 검증 ═══

class TestHtmlLabels:
    """PC/Mobile HTML에 필수 문구/탭 존재, CTA 금지."""

    def _read_pc(self) -> str:
        return (ROOT / "web" / "index_pc.html").read_text(encoding="utf-8")

    def _read_mobile(self) -> str:
        return (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    # ── PC 탭 ──
    def test_pc_nav_has_toss_tab(self):
        assert "토스 AI" in self._read_pc()

    def test_pc_has_toss_page(self):
        assert 'id="p-toss"' in self._read_pc()

    def test_pc_has_toss_label(self):
        assert "Toss 실전 AI 자동거래 계좌" in self._read_pc()

    def test_pc_has_not_included_label(self):
        assert "기존 포트폴리오 미합산" in self._read_pc()

    def test_pc_has_live_status_label(self):
        html = self._read_pc()
        assert "자동거래" in html
        assert "Toss 수익" in html

    def test_pc_has_api_call(self):
        assert "/api/toss/account-summary" in self._read_pc()

    def test_pc_no_experiment_wording(self):
        html = self._read_pc()
        # p-toss 페이지 안에 '실험' 없어야
        assert "실험 계좌" not in html
        assert "실험 준비용" not in html

    # ── Mobile 탭 ──
    def test_mobile_nav_has_toss_tab(self):
        assert "토스 AI" in self._read_mobile()

    def test_mobile_has_toss_section(self):
        html = self._read_mobile()
        assert 't-toss' in html or 'toss-page-m' in html

    def test_mobile_has_toss_label(self):
        assert "Toss 실전 AI 자동거래 계좌" in self._read_mobile()

    def test_mobile_has_not_included_label(self):
        assert "기존 포트폴리오 미합산" in self._read_mobile()

    def test_mobile_has_live_status_label(self):
        html = self._read_mobile()
        # 정적 HTML 또는 JS 렌더링 코드에 Toss 손익/자동거래 상태 문구 존재
        assert "자동거래" in html
        assert "Toss 수익" in html

    def test_mobile_has_api_call(self):
        assert "/api/toss/account-summary" in self._read_mobile()

    def test_mobile_no_experiment_wording(self):
        html = self._read_mobile()
        assert "실험 계좌" not in html
        assert "실험 준비용" not in html

    # ── CTA 금지 ──
    def test_no_buy_sell_cta_in_toss_section_pc(self):
        """PC Toss 페이지에 매수/매도/주문실행/자동매매시작 CTA 버튼 없음."""
        html = self._read_pc()
        toss_match = re.search(r'id="p-toss".*?</div>\s*</div>', html, re.DOTALL)
        if toss_match:
            section = toss_match.group()
            buttons = re.findall(r'<button[^>]*>.*?</button>', section, re.DOTALL)
            for btn in buttons:
                for word in ["매수", "매도", "주문 실행", "자동매매 시작"]:
                    assert word not in btn, f"CTA '{word}' found in PC Toss page"

    def test_no_buy_sell_cta_in_toss_section_mobile(self):
        """Mobile Toss 섹션에 매수/매도/주문실행/자동매매시작 CTA 버튼 없음."""
        html = self._read_mobile()
        toss_match = re.search(r'id="t-toss".*?</div>\s*</div>', html, re.DOTALL)
        if toss_match:
            section = toss_match.group()
            buttons = re.findall(r'<button[^>]*>.*?</button>', section, re.DOTALL)
            for btn in buttons:
                for word in ["매수", "매도", "주문 실행", "자동매매 시작"]:
                    assert word not in btn, f"CTA '{word}' found in mobile Toss section"

    # ── 자동거래 문구 ──
    def test_pc_has_paper_trading_text(self):
        assert "paper trading" in self._read_pc() or "paper" in self._read_pc()

    def test_pc_has_kill_switch_text(self):
        assert "킬스위치" in self._read_pc()

    def test_pc_has_live_disabled_text(self):
        assert "실주문 비활성" in self._read_pc()

    def test_mobile_has_paper_trading_text(self):
        assert "paper trading" in self._read_mobile() or "paper" in self._read_mobile()

    def test_mobile_has_live_disabled_text(self):
        assert "실주문 비활성" in self._read_mobile()

    # ── API endpoints ──
    def test_pc_has_automation_api(self):
        assert "/api/toss/automation-status" in self._read_pc()

    def test_pc_has_paper_trades_api(self):
        assert "/api/toss/paper-trades" in self._read_pc()

    def test_mobile_has_automation_api(self):
        assert "/api/toss/automation-status" in self._read_mobile()


class TestTossLivePilotRuntimeDisplay:
    """Live Pilot dashboard summaries must show current runtime policy, not stale ledger gates."""

    def test_previews_summary_uses_current_runtime_policy(self):
        from core.dashboard_data import toss_live_pilot_previews_data

        policy = {
            "live_order_allowed": True,
            "adapter_status": "enabled",
            "live_transport_status": "configured",
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
        }
        stale_summary = {
            "counts": {"previewed": 1},
            "live_order_sent_total": 1,
            "adapter_status": "disabled",
            "live_order_allowed": False,
        }
        with (
            patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[]),
            patch("core.toss_live_pilot_ledger.live_pilot_ledger_summary", return_value=stale_summary),
            patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", return_value=policy),
        ):
            data = toss_live_pilot_previews_data(limit=5)

        summary = data["summary"]
        assert summary["live_order_allowed"] is True
        assert summary["adapter_status"] == "enabled"
        assert summary["live_transport_status"] == "configured"
        assert summary["historical_live_order_allowed"] is False
        assert summary["historical_adapter_status"] == "disabled"

    def test_verifications_summary_keeps_gate_false_but_exposes_policy(self):
        from core.dashboard_data import toss_live_pilot_verifications_data

        policy = {
            "live_order_allowed": True,
            "adapter_status": "enabled",
            "live_transport_status": "configured",
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
        }
        with (
            patch("core.toss_live_pilot_verification.verification_summary", return_value={"summary": {"PASS": 1}, "live_order_allowed": False}),
            patch("core.toss_live_pilot_verification.list_verifications", return_value=[]),
            patch("core.toss_live_pilot_hermes_bridge.get_mirror_status", return_value={"mirror_enabled": False, "mirror_target_configured": False}),
            patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", return_value=policy),
        ):
            data = toss_live_pilot_verifications_data(limit=5)

        assert data["live_order_allowed"] is False
        assert data["policy_live_order_allowed"] is True
        assert data["summary"]["gate_live_order_allowed"] is False
        assert data["summary"]["policy_live_order_allowed"] is True
        assert data["summary"]["policy_adapter_status"] == "enabled"


    def test_recent_broker_orders_failure_sets_cooldown(self):
        import time
        from core import dashboard_data as dd

        dd._cache.pop("toss_broker_orders:2", None)
        dd._toss_broker_orders_last_good = None
        dd._toss_broker_orders_cooldown_until = 0.0

        with patch("core.toss_live_order_http.list_orders", return_value={"ok": False, "reason": "http_401", "orders": []}) as list_orders:
            out = dd._recent_toss_broker_orders(limit=2)

        assert out["ok"] is False
        assert out["cache_status"] == "error"
        assert "http_401" in out["error"]
        assert dd._toss_broker_orders_cooldown_until > time.monotonic()
        assert list_orders.call_count == 2

        dd._cache.pop("toss_broker_orders:2", None)
        dd._toss_broker_orders_last_good = None
        dd._toss_broker_orders_cooldown_until = 0.0

    def test_recent_broker_orders_cooldown_returns_stale_without_external_call(self):
        import time
        from core import dashboard_data as dd

        good = {
            "ok": True, "error": "",
            "orders": [{"symbol": "AAPL", "status": "FILLED"}],
            "open_count": 0, "closed_count": 1,
            "source": "GET /api/v1/orders OPEN+CLOSED",
            "read_only_notice": "브로커 주문 조회 전용 · 주문 생성/취소/수정 없음",
            "cache_status": "live",
        }
        dd._cache.pop("toss_broker_orders:1", None)
        dd._toss_broker_orders_last_good = (time.monotonic(), good)
        dd._toss_broker_orders_cooldown_until = time.monotonic() + 300

        out = dd._recent_toss_broker_orders(limit=1)

        assert out["cache_status"] == "stale"
        assert out["orders"][0]["symbol"] == "AAPL"
        assert out["stale_reason"] == "toss_broker_orders_cooldown"

        dd._cache.pop("toss_broker_orders:1", None)
        dd._toss_broker_orders_last_good = None
        dd._toss_broker_orders_cooldown_until = 0.0


# ═══ API endpoint 검증 (automation) ═══

class TestApiAutomationEndpoint:
    def test_automation_status_exists(self):
        source = (ROOT / "web" / "app.py").read_text()
        assert "/api/toss/automation-status" in source

    def test_paper_trades_exists(self):
        source = (ROOT / "web" / "app.py").read_text()
        assert "/api/toss/paper-trades" in source

    def test_all_toss_endpoints_are_get(self):
        source = (ROOT / "web" / "app.py").read_text()
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "/api/toss/" in line and "def " not in line:
                assert "@app.get" in line, f"non-GET toss endpoint: {line}"
