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


# ═══ dashboard_data.toss_account_summary 테스트 ═══

class TestTossAccountSummary:
    """toss_account_summary 응답 구조 검증."""

    def _get_summary(self, configured=True, accounts=None, holdings=None, fx=None):
        """mock된 toss_client로 summary 생성."""
        if accounts is None:
            accounts = [{"accountSeq": 1, "accountType": "BROKERAGE", "accountNo": "17401007263"}]
        if holdings is None:
            holdings = {"items": [], "marketValue": {"amount": {"krw": "0", "usd": None}}}
        if fx is None:
            fx = {"baseCurrency": "USD", "quoteCurrency": "KRW", "rate": "1538.5"}

        from core.dashboard_data import _fetch_toss_account_summary_raw

        with patch("core.toss_client.is_configured", return_value=configured), \
             patch("core.toss_client.get_accounts", return_value=accounts), \
             patch("core.toss_client.get_holdings", return_value=holdings), \
             patch("core.toss_client.get_exchange_rate", return_value=fx), \
             patch("core.toss_client.sanitize_dict", side_effect=lambda x: x):
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
        assert "실험" in d["label"] or "Toss" in d["label"]

    def test_account_no_masked(self):
        d = self._get_summary()
        for acct in d["accounts"]:
            assert acct["account_no_masked"] == "[REDACTED]"
            assert "17401007263" not in str(acct)

    def test_no_raw_account_no_in_response(self):
        d = self._get_summary()
        s = str(d)
        assert "17401007263" not in s

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

    def test_with_holdings(self):
        holdings = {
            "items": [{"symbol": "AAPL", "quantity": 10}],
            "marketValue": {"amount": {"krw": "5000000", "usd": "3500"}},
        }
        d = self._get_summary(holdings=holdings)
        assert d["holdings_count"] == 1
        assert d["market_value"]["krw"] == 5000000.0

    def test_exchange_rate_present(self):
        d = self._get_summary()
        fx = d["exchange_rate"]
        assert fx is not None
        assert fx["rate"] == 1538.5
        assert fx["source"] == "Toss"


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


# ═══ HTML 문구 검증 ═══

class TestHtmlLabels:
    """PC/Mobile HTML에 필수 문구 존재, CTA 금지."""

    def _read_pc(self) -> str:
        return (ROOT / "web" / "index_pc.html").read_text(encoding="utf-8")

    def _read_mobile(self) -> str:
        return (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_pc_has_toss_label(self):
        html = self._read_pc()
        assert "Toss AI 실험 계좌" in html

    def test_pc_has_not_included_label(self):
        html = self._read_pc()
        assert "기존 포트폴리오 미합산" in html

    def test_pc_has_no_trading_label(self):
        html = self._read_pc()
        # JS 코드에서 "주문 기능 없음" 경고가 렌더링됨
        assert "주문 기능 없음" in html

    def test_mobile_has_toss_label(self):
        html = self._read_mobile()
        assert "Toss AI 실험 계좌" in html

    def test_mobile_has_not_included_label(self):
        html = self._read_mobile()
        assert "기존 포트폴리오 미합산" in html

    def test_mobile_has_no_trading_label(self):
        html = self._read_mobile()
        assert "주문 기능 없음" in html

    def test_no_buy_sell_cta_in_toss_section_pc(self):
        """PC Toss 섹션에 매수/매도/주문 CTA 버튼 없음."""
        html = self._read_pc()
        # Toss 관련 섹션만 추출 (toss-exp-panel ~ 다음 aside)
        toss_match = re.search(r'id="toss-exp-panel".*?</aside>', html, re.DOTALL)
        if toss_match:
            section = toss_match.group()
            # button 태그 안에 매수/매도/주문/자동매매 없어야
            buttons = re.findall(r'<button[^>]*>.*?</button>', section, re.DOTALL)
            for btn in buttons:
                for word in ["매수", "매도", "주문", "자동매매"]:
                    assert word not in btn, f"CTA '{word}' found in Toss section"

    def test_no_buy_sell_cta_in_toss_section_mobile(self):
        """Mobile Toss 섹션에 매수/매도/주문 CTA 버튼 없음."""
        html = self._read_mobile()
        toss_match = re.search(r'id="toss-exp-m-panel".*?</div>\s*<div class="c">', html, re.DOTALL)
        if toss_match:
            section = toss_match.group()
            buttons = re.findall(r'<button[^>]*>.*?</button>', section, re.DOTALL)
            for btn in buttons:
                for word in ["매수", "매도", "주문", "자동매매"]:
                    assert word not in btn, f"CTA '{word}' found in mobile Toss section"
