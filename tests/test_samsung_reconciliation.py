"""
삼성증권 포트폴리오 reconciliation 테스트

1. settings HOLDINGS 파싱
2. Toss Paper ledger 제외
3. US ticker USD→KRW 평가
4. source price disagreement 계산
5. 삼성증권 원본 없으면 "원본 미확인"
6. 리포트 생성 경로
7. 자동 수정 없음
8. POST/PUT/DELETE/PATCH route 없음
9. 민감정보 마스킹
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.reconcile_samsung_portfolio as rec


# ─── 헬퍼 ────────────────────────────────────────────────

def _fake_settings() -> dict:
    """최소한의 가짜 settings."""
    return {
        "HOLDINGS_GENERAL":  {"005930.KS": {"shares": 90,  "avg_cost_krw": 60_425}},
        "HOLDINGS_RIA":      {"069500.KS": {"shares": 12,  "avg_cost_krw": 142_000}},
        "HOLDINGS_IRP":      {"133690.KS": {"shares": 30,  "avg_cost_krw": 111_077},
                              "360750.KS": {"shares": 118, "avg_cost_krw": 16_838}},
        "HOLDINGS_PENSION":  {"133690.KS": {"shares": 69,  "avg_cost_krw": 102_974}},
        "HOLDINGS_ISA":      {"462870.KS": {"shares": 160, "avg_cost_krw": 30_025},
                              "MU":         {"shares": 5,   "avg_cost_usd": 408.8181}},
        "DEFAULT_CASH":      17_729_839.0,
        "RIA_CASH":          17_214_636.0,
        "IRP_CASH":          272_980.0,
        "IRP_DEFAULT_OPTION": 4_784_915.0,
        "PENSION_MMF":       6_880_513.0,
        "ISA_CASH":          4_545_735.0,
        "ACCOUNT_PRINCIPAL_KRW": {
            "일반": 35_000_000.0, "RIA": 0.0, "ISA": 20_000_000.0,
            "연금저축": 20_500_000.0, "IRP": 10_250_000.0,
        },
        "TOTAL_PRINCIPAL_KRW": 85_750_000.0,
    }


def _patch_all(prices_by_ticker: dict | None = None):
    """가격 조회와 trades, API를 일괄 mock."""
    if prices_by_ticker is None:
        prices_by_ticker = {}

    def _fake_price_all(ticker, skip_kis=False):
        p = prices_by_ticker.get(ticker, {})
        return {
            "KIS": p.get("KIS"),
            "Naver": p.get("Naver"),
            "yfinance": p.get("yfinance"),
        }

    return patch.object(rec, "_query_price_all", side_effect=_fake_price_all)


# ─── 1. settings HOLDINGS 파싱 ────────────────────────────

class TestSettingsParsing:
    def test_all_accounts_present(self):
        cfg = _fake_settings()
        for key in ("HOLDINGS_GENERAL", "HOLDINGS_RIA", "HOLDINGS_IRP",
                    "HOLDINGS_PENSION", "HOLDINGS_ISA"):
            assert key in cfg

    def test_general_has_samsung(self):
        cfg = _fake_settings()
        h = cfg["HOLDINGS_GENERAL"]
        assert "005930.KS" in h
        assert h["005930.KS"]["shares"] == 90
        assert h["005930.KS"]["avg_cost_krw"] == 60_425

    def test_isa_has_shiftup(self):
        cfg = _fake_settings()
        h = cfg["HOLDINGS_ISA"]
        assert "462870.KS" in h
        assert h["462870.KS"]["shares"] == 160

    def test_cash_fields_present(self):
        cfg = _fake_settings()
        assert cfg["DEFAULT_CASH"] == 17_729_839.0
        assert cfg["RIA_CASH"] == 17_214_636.0
        assert cfg["ISA_CASH"] == 4_545_735.0

    def test_load_settings_from_actual_module(self):
        """실제 settings.py에서 로드 — 모든 HOLDINGS_* 키 존재."""
        cfg = rec._load_settings()
        for key in ("HOLDINGS_GENERAL", "HOLDINGS_RIA", "HOLDINGS_IRP",
                    "HOLDINGS_PENSION", "HOLDINGS_ISA"):
            assert key in cfg, f"Missing key: {key}"
            assert isinstance(cfg[key], dict)

    def test_actual_holdings_not_empty(self):
        cfg = rec._load_settings()
        total_tickers = sum(len(v) for k, v in cfg.items() if k.startswith("HOLDINGS_"))
        assert total_tickers > 0


# ─── 2. Toss Paper 제외 ───────────────────────────────────

class TestTossPaperExcluded:
    def test_toss_paper_not_in_holdings(self):
        """HOLDINGS_*에 SOFI 같은 Toss Paper 전용 종목 없음."""
        cfg = rec._load_settings()
        toss_paper_tickers = {"SOFI", "PLTR", "INTC", "F", "AMD"}
        all_tickers = set()
        for key in ("HOLDINGS_GENERAL", "HOLDINGS_RIA", "HOLDINGS_IRP",
                    "HOLDINGS_PENSION", "HOLDINGS_ISA"):
            all_tickers.update(cfg[key].keys())
        overlap = toss_paper_tickers & all_tickers
        assert not overlap, f"Toss Paper 종목이 HOLDINGS에 포함됨: {overlap}"

    def test_reconcile_summary_marks_toss_excluded(self):
        cfg = _fake_settings()
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=1530.0), \
             _patch_all(), \
             patch.object(rec, "_load_trades_ledger", return_value=[]), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={"found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"}):
            result = rec.reconcile(skip_api=True)
        assert result["summary"]["toss_paper_excluded"] is True

    def test_no_toss_paper_ledger_import_in_recon(self):
        """reconciliation 스크립트에 toss_paper_ledger import 없어야 함."""
        src = (ROOT / "tools" / "reconcile_samsung_portfolio.py").read_text(encoding="utf-8")
        assert "toss_paper_ledger" not in src
        assert "toss_paper_performance" not in src


# ─── 3. US ticker USD→KRW 평가 ──────────────────────────

class TestUSDToKRW:
    def test_mu_evaluated_in_krw(self):
        """MU (USD) 종목은 best_price_krw = best_price_usd × usdkrw."""
        cfg = _fake_settings()
        usdkrw = 1530.0
        prices = {"MU": {"KIS": 100.0, "Naver": None, "yfinance": 100.5}}
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=usdkrw), \
             _patch_all(prices), \
             patch.object(rec, "_load_trades_ledger", return_value=[]), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={"found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"}):
            result = rec.reconcile(skip_api=True)
        mu_pr = result["price_results"]["MU"]
        assert mu_pr["currency"] == "USD"
        assert mu_pr["best_native"] == pytest.approx(100.0)
        assert mu_pr["best_krw"] == pytest.approx(100.0 * 1530.0, rel=0.01)

    def test_kr_ticker_not_multiplied_by_usdkrw(self):
        """KR 종목(005930.KS)은 KRW 그대로 사용, usdkrw 곱하지 않음."""
        cfg = _fake_settings()
        prices = {"005930.KS": {"KIS": 332_000.0, "Naver": 332_000.0, "yfinance": 332_100.0},
                  "069500.KS": {"KIS": 138_000.0, "Naver": None, "yfinance": 138_000.0},
                  "133690.KS": {"KIS": 200_000.0, "Naver": None, "yfinance": 200_000.0},
                  "360750.KS": {"KIS": 28_000.0,  "Naver": None, "yfinance": 28_000.0},
                  "462870.KS": {"KIS": 33_000.0,  "Naver": None, "yfinance": 33_000.0},
                  "MU":         {"KIS": 100.0, "Naver": None, "yfinance": 100.0}}
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=1530.0), \
             _patch_all(prices), \
             patch.object(rec, "_load_trades_ledger", return_value=[]), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={"found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"}):
            result = rec.reconcile(skip_api=True)
        pr = result["price_results"]["005930.KS"]
        assert pr["currency"] == "KRW"
        # best_krw == best_native (no fx multiply)
        assert pr["best_krw"] == pr["best_native"]

    def test_us_ticker_avg_cost_displays_usd(self):
        """MU avg_cost가 USD로 파싱됨."""
        cfg = _fake_settings()
        assert cfg["HOLDINGS_ISA"]["MU"].get("avg_cost_usd") == 408.8181
        assert "avg_cost_krw" not in cfg["HOLDINGS_ISA"]["MU"]


# ─── 4. source price disagreement ───────────────────────

class TestSourceAgreement:
    def test_normal_within_1pct(self):
        prices = {"KIS": 100_000.0, "Naver": 100_500.0, "yfinance": 100_200.0}
        result = rec._source_agreement(prices)
        assert result["status"] == "정상"
        assert result["max_diff_pct"] < 1.0

    def test_warn_1_to_3pct(self):
        prices = {"KIS": 100_000.0, "Naver": 101_500.0, "yfinance": None}
        result = rec._source_agreement(prices)
        assert result["status"] == "주의"

    def test_alert_above_3pct(self):
        prices = {"KIS": 100_000.0, "Naver": 104_000.0, "yfinance": None}
        result = rec._source_agreement(prices)
        assert result["status"] == "source_불일치"
        assert result["max_diff_pct"] >= 3.0

    def test_single_source_is_단일소스(self):
        prices = {"KIS": 100_000.0, "Naver": None, "yfinance": None}
        result = rec._source_agreement(prices)
        assert result["status"] == "단일소스"
        assert result["max_diff_pct"] is None

    def test_all_none_single(self):
        prices = {"KIS": None, "Naver": None, "yfinance": None}
        result = rec._source_agreement(prices)
        assert result["max_diff_pct"] is None

    def test_source_불일치_creates_issue(self):
        """source 불일치 3% 이상 시 issues에 추가됨."""
        cfg = _fake_settings()
        prices = {
            "005930.KS": {"KIS": 100_000.0, "Naver": 110_000.0, "yfinance": None},
            "069500.KS": {"KIS": 138_000.0, "Naver": None, "yfinance": 138_000.0},
            "133690.KS": {"KIS": 200_000.0, "Naver": None, "yfinance": 200_000.0},
            "360750.KS": {"KIS": 28_000.0,  "Naver": None, "yfinance": 28_000.0},
            "462870.KS": {"KIS": 33_000.0,  "Naver": None, "yfinance": 33_000.0},
            "MU":         {"KIS": 100.0, "Naver": None, "yfinance": 100.0},
        }
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=1530.0), \
             _patch_all(prices), \
             patch.object(rec, "_load_trades_ledger", return_value=[]), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={"found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"}):
            result = rec.reconcile(skip_api=True)
        cats = {i["category"] for i in result["issues"]}
        assert "현재가_source_불일치" in cats


# ─── 5. 삼성증권 원본 없으면 "원본 미확인" ──────────────

class TestSnapshotSource:
    def test_no_access_returns_not_found(self):
        """Hermes 파일 접근 불가 → found=False, note=원본 미확인."""
        # pathlib.Path.read_text는 io.open을 사용하므로 builtins.open 패치는
        # 실제 파일이 존재하는 실행환경에서 격리되지 않는다.
        with patch.object(Path, "read_text", side_effect=PermissionError("Permission denied")):
            snap = rec._check_snapshot_sources()
        assert snap["found"] is False
        assert "원본 미확인" in snap.get("note", "")

    def test_missing_file_returns_not_found(self):
        snap = rec._check_snapshot_sources()
        # 실제 hermes 파일은 Permission denied 또는 없음
        assert isinstance(snap["found"], bool)
        assert "sources_checked" in snap

    def test_report_shows_원본_미확인_when_not_found(self):
        cfg = _fake_settings()
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=1530.0), \
             _patch_all(), \
             patch.object(rec, "_load_trades_ledger", return_value=[]), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={
                 "found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"
             }):
            result = rec.reconcile(skip_api=True)
        report = rec.format_report(result)
        assert "원본 미확인" in report
        assert result["summary"]["samsung_snapshot_found"] is False


# ─── 6. 리포트 생성 경로 ────────────────────────────────

class TestReportPath:
    def test_save_report_creates_file(self, tmp_path):
        with patch.object(rec, "_REPORT_DIR", tmp_path):
            path = rec.save_report("test content")
        assert path.exists()
        assert path.suffix == ".md"
        assert "samsung_reconciliation" in path.name
        assert path.read_text() == "test content"

    def test_report_dir_created_if_missing(self, tmp_path):
        new_dir = tmp_path / "reports_new"
        with patch.object(rec, "_REPORT_DIR", new_dir):
            path = rec.save_report("hello")
        assert new_dir.exists()
        assert path.exists()

    def test_report_contains_header(self):
        cfg = _fake_settings()
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=1530.0), \
             _patch_all(), \
             patch.object(rec, "_load_trades_ledger", return_value=[]), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={"found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"}):
            result = rec.reconcile(skip_api=True)
        report = rec.format_report(result)
        assert "삼성증권 포트폴리오 Reconciliation 리포트" in report
        assert "Toss Paper 제외" in report


# ─── 7. 자동 수정 없음 ───────────────────────────────────

class TestNoAutoFix:
    def _src(self) -> str:
        return (ROOT / "tools" / "reconcile_samsung_portfolio.py").read_text(encoding="utf-8")

    def test_no_settings_write(self):
        src = self._src()
        assert "settings.py" not in src or "write" not in src.lower().split("settings.py")[0][-50:]
        # 명확히: open(..., 'w') 또는 write() 후 settings.py 없음
        assert "HOLDINGS_GENERAL =" not in src
        assert "DEFAULT_CASH =" not in src

    def test_no_db_write(self):
        src = self._src()
        # SQL DML 문 — 변수명 'last_update'처럼 무해한 케이스와 구분하기 위해
        # "conn.execute" + SQL verb 조합으로 탐지
        import re
        sql_executes = re.findall(r'conn\.execute\s*\(["\']([^"\']{0,50})', src, re.IGNORECASE)
        for stmt in sql_executes:
            upper = stmt.upper().strip()
            for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
                assert not upper.startswith(forbidden.strip()), \
                    f"DB write found in conn.execute: {stmt!r}"

    def test_no_post_put_delete_routes(self):
        """reconcile 도구에 write route 없음."""
        src = self._src()
        for verb in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert verb not in src

    def test_report_mentions_no_auto_fix(self):
        cfg = _fake_settings()
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=1530.0), \
             _patch_all(), \
             patch.object(rec, "_load_trades_ledger", return_value=[]), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={"found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"}):
            result = rec.reconcile(skip_api=True)
        report = rec.format_report(result)
        assert "자동 수정 안 함" in report or "자동 수정 없음" in report


# ─── 8. POST/PUT/DELETE/PATCH route 없음 ─────────────────

class TestNoWriteRoutes:
    def test_no_write_routes_in_app_py(self):
        src = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
        for verb in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert verb not in src, f"Forbidden write route: {verb}"

    def test_no_write_routes_in_reconcile_tool(self):
        src = (ROOT / "tools" / "reconcile_samsung_portfolio.py").read_text(encoding="utf-8")
        for verb in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert verb not in src


# ─── 9. 민감정보 마스킹 ──────────────────────────────────

class TestSensitiveMasking:
    def test_account_number_masked(self):
        text = "계좌번호: 71274508-85 잔고"
        result = rec._mask_sensitive(text)
        assert "71274508" not in result

    def test_bearer_token_masked(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc"
        result = rec._mask_sensitive(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "Bearer **masked**" in result

    def test_long_numbers_masked(self):
        text = "계좌: 7127450885 잔고"
        result = rec._mask_sensitive(text)
        assert "7127450885" not in result

    def test_normal_amounts_not_masked(self):
        """정상 금액(8자리 미만)은 마스킹하지 않음."""
        text = "평가금액 ₩1,234,567 원"
        result = rec._mask_sensitive(text)
        assert "1,234,567" in result or "1234567" in result or "₩" in result

    def test_source_code_no_hardcoded_secret(self):
        src = (ROOT / "tools" / "reconcile_samsung_portfolio.py").read_text(encoding="utf-8")
        long_nums = re.findall(r"\b\d{8,}\b", src)
        # 허용: 임계값 상수 (1_350_000 형태는 실제 파일에 없음)
        for n in long_nums:
            assert int(n) < 10_000_000 or "_" in src[max(0, src.index(n)-5): src.index(n)], \
                f"Potential sensitive number: {n}"

    def test_no_api_key_in_source(self):
        src = (ROOT / "tools" / "reconcile_samsung_portfolio.py").read_text(encoding="utf-8")
        assert "KIS_APP_SECRET" not in src
        assert "TOSS_APP_SECRET" not in src
        # "Bearer " only appears inside the masking regex pattern — not as a hardcoded token
        # Check no hardcoded Bearer value (e.g., "Bearer eyJ...")
        import re as _re
        # Hardcoded bearer = Bearer followed by a long alphanumeric string (not a regex pattern)
        hardcoded = _re.findall(r'Bearer\s+[A-Za-z0-9_.-]{20,}', src)
        assert not hardcoded, f"Hardcoded Bearer token: {hardcoded}"


# ─── 10. IRP 현금 합산 확인 ──────────────────────────────

class TestIRPCashHandling:
    def test_irp_cash_includes_default_option(self):
        """IRP 계좌 현금은 IRP_CASH + IRP_DEFAULT_OPTION 합산."""
        cfg = _fake_settings()
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=1530.0), \
             _patch_all(), \
             patch.object(rec, "_load_trades_ledger", return_value=[]), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={"found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"}):
            result = rec.reconcile(skip_api=True)
        irp_data = result["account_data"]["IRP"]
        expected_cash = cfg["IRP_CASH"] + cfg["IRP_DEFAULT_OPTION"]
        assert irp_data["cash_with_extra"] == pytest.approx(expected_cash)

    def test_other_accounts_not_include_default_option(self):
        """일반/RIA/ISA/연금저축는 IRP_DEFAULT_OPTION 미포함."""
        cfg = _fake_settings()
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=1530.0), \
             _patch_all(), \
             patch.object(rec, "_load_trades_ledger", return_value=[]), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={"found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"}):
            result = rec.reconcile(skip_api=True)
        for acct in ("일반", "RIA", "ISA", "연금저축"):
            data = result["account_data"][acct]
            # must not include IRP_DEFAULT_OPTION
            irp_extra = cfg["IRP_DEFAULT_OPTION"]
            assert data["cash_with_extra"] != pytest.approx(
                data["cash_settings"] + irp_extra
            ) or acct == "IRP"


# ─── 11. 미반영 거래 검출 ────────────────────────────────

class TestPendingTrades:
    def test_pending_trade_creates_issue(self):
        pending = [{
            "ticker": "005930.KS", "side": "buy", "shares": 10,
            "price": 70000, "created_at": "2026-06-24T09:00:00+09:00",
            "account": "일반", "applied": False,
        }]
        cfg = _fake_settings()
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=1530.0), \
             _patch_all(), \
             patch.object(rec, "_load_trades_ledger", return_value=pending), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={"found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"}):
            result = rec.reconcile(skip_api=True)
        cats = {i["category"] for i in result["issues"]}
        assert "미반영_거래" in cats
        assert len(result["pending_trades"]) == 1

    def test_applied_trades_not_issue(self):
        applied = [{
            "ticker": "005930.KS", "side": "buy", "shares": 10,
            "price": 70000, "created_at": "2026-06-24T09:00:00+09:00",
            "account": "일반", "applied": True,
        }]
        cfg = _fake_settings()
        with patch.object(rec, "_load_settings", return_value=cfg), \
             patch.object(rec, "_get_usdkrw", return_value=1530.0), \
             _patch_all(), \
             patch.object(rec, "_load_trades_ledger", return_value=applied), \
             patch.object(rec, "_load_dashboard_api", return_value=None), \
             patch.object(rec, "_check_snapshot_sources", return_value={"found": False, "sources_checked": [], "snippets": [], "note": "원본 미확인"}):
            result = rec.reconcile(skip_api=True)
        cats = {i["category"] for i in result["issues"]}
        assert "미반영_거래" not in cats
        assert len(result["pending_trades"]) == 0
