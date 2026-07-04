"""briefing_enrichment — 수집 데이터 통합 주입 테스트 (전부 mock, 네트워크 없음)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.briefing_enrichment as be


class TestHoldingCodes:
    def test_collects_kr_codes_with_names(self, monkeypatch):
        codes = be._all_holding_kr_codes()
        # settings 실데이터 기반 — 국내 코드만, 6자리
        assert codes
        assert all(c.isdigit() and len(c) == 6 for c in codes)
        assert "005930" in codes  # 삼성전자 보유

    def test_us_tickers_excluded(self):
        codes = be._all_holding_kr_codes()
        assert "NVDA" not in codes and "MU" not in codes


class TestDartText:
    def _patch(self, monkeypatch, items):
        monkeypatch.setattr(
            "core.dart_monitor.fetch_recent_disclosures",
            lambda days=2, now=None: {"ok": True, "items": items})
        monkeypatch.setattr(
            be, "_all_holding_kr_codes", lambda: {"005930": "삼성전자"})

    def test_risk_hit_rendered(self, monkeypatch):
        self._patch(monkeypatch, [{
            "rcept_no": "1", "rcept_dt": "20260703", "stock_code": "005930",
            "corp_name": "삼성전자", "report_nm": "유상증자결정"}])
        out = be.dart_briefing_text()
        assert "리스크 공시" in out and "유상증자" in out
        assert "공시를 우선하라" in out

    def test_normal_disclosure_rendered(self, monkeypatch):
        self._patch(monkeypatch, [{
            "rcept_no": "2", "rcept_dt": "20260703", "stock_code": "005930",
            "corp_name": "삼성전자", "report_nm": "기업설명회(IR)개최"}])
        out = be.dart_briefing_text()
        assert "보유종목 최근 공시" in out and "IR" in out
        assert "리스크 공시" not in out

    def test_unrelated_ticker_ignored(self, monkeypatch):
        self._patch(monkeypatch, [{
            "rcept_no": "3", "rcept_dt": "20260703", "stock_code": "999999",
            "corp_name": "남의회사", "report_nm": "유상증자결정"}])
        assert be.dart_briefing_text() == ""

    def test_fetch_failure_empty(self, monkeypatch):
        monkeypatch.setattr(
            "core.dart_monitor.fetch_recent_disclosures",
            lambda days=2, now=None: {"ok": False, "reason": "no_api_key", "items": []})
        assert be.dart_briefing_text() == ""


class TestOrderbookText:
    def test_renders_imbalance(self, monkeypatch):
        monkeypatch.setattr(
            be, "_all_holding_kr_codes", lambda: {"005930": "삼성전자"})
        monkeypatch.setattr(
            "core.market_kis.get_domestic_orderbook",
            lambda code: {"imbalance_pct": 12.0, "liquidity_label": "양호",
                          "spread_pct": 0.10, "error": None})
        out = be.kis_orderbook_briefing_text()
        assert "삼성전자(005930)" in out
        assert "+12%" in out and "매수우위" in out

    def test_none_result_empty(self, monkeypatch):
        monkeypatch.setattr(
            be, "_all_holding_kr_codes", lambda: {"005930": "삼성전자"})
        monkeypatch.setattr(
            "core.market_kis.get_domestic_orderbook", lambda code: None)
        assert be.kis_orderbook_briefing_text() == ""

    def test_explicit_tickers(self, monkeypatch):
        monkeypatch.setattr(
            "core.market_kis.get_domestic_orderbook",
            lambda code: {"imbalance_pct": -30.0, "liquidity_label": "낮음",
                          "spread_pct": 0.50, "error": None})
        out = be.kis_orderbook_briefing_text(["069500.KS", "NVDA"])
        assert "069500" in out and "NVDA" not in out
        assert "매도우위" in out


class TestQualityGateText:
    def test_summary_rendered(self, monkeypatch):
        monkeypatch.setattr(
            "core.toss_quality_gate.generate_daily_quality_report",
            lambda date=None: {
                "date": "2026-07-04", "pass_count": 2, "small_pass_count": 1,
                "wait_count": 3, "watch_count": 4, "chase_block_count": 1,
                "block_count": 5, "avg_pass_score": 72.5, "avg_pass_rr": 2.1,
                "outcome_hit_rate": 0.65, "outcome_evaluated": 20,
                "top_block_reasons": ["RR 미달", "추격 금지"]})
        out = be.quality_gate_briefing_text()
        assert "판정 16건" in out
        assert "PASS 2" in out and "BLOCK 5" in out
        assert "65%" in out and "RR 미달" in out
        assert "매수 추천하지 마라" in out

    def test_no_decisions_empty(self, monkeypatch):
        monkeypatch.setattr(
            "core.toss_quality_gate.generate_daily_quality_report",
            lambda date=None: {
                "pass_count": 0, "small_pass_count": 0, "wait_count": 0,
                "watch_count": 0, "chase_block_count": 0, "block_count": 0})
        assert be.quality_gate_briefing_text() == ""


class TestBuildContext:
    def test_us_briefing_skipped(self):
        assert be.build_enrichment_context("US_NIGHT") == ""
        assert be.build_enrichment_context("US_BEFORE") == ""

    def test_kr_briefing_merges_sections(self, monkeypatch):
        monkeypatch.setattr(be, "dart_briefing_text", lambda days=2: "공시내용")
        monkeypatch.setattr(be, "kis_orderbook_briefing_text", lambda t=None: "호가내용")
        monkeypatch.setattr(be, "quality_gate_briefing_text", lambda: "게이트내용")
        out = be.build_enrichment_context("KR_NIGHT")
        assert "DART 전자공시" in out and "공시내용" in out
        assert "KIS 실시간 호가" in out and "호가내용" in out
        assert "품질게이트" in out and "게이트내용" in out

    def test_all_empty_returns_empty(self, monkeypatch):
        monkeypatch.setattr(be, "dart_briefing_text", lambda days=2: "")
        monkeypatch.setattr(be, "kis_orderbook_briefing_text", lambda t=None: "")
        monkeypatch.setattr(be, "quality_gate_briefing_text", lambda: "")
        assert be.build_enrichment_context("MANUAL") == ""
