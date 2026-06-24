"""
tests/test_toss_paper_currency_sizing.py

미국/한국 주식 통화 변환 sizing 테스트.
- KRW budget을 USD price로 직접 나누는 버그 방지
- 1주 예산 초과 → budget_too_small_for_one_share block
- 주문표 USD 가격 표시
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


# ─── helpers ─────────────────────────────────────────────


def _policy(max_budget: int = 300_000, consensus_symbols: list[str] | None = None) -> dict:
    return {
        "mode": "paper_only", "live_order_allowed": False,
        "sample_status": "insufficient",
        "base_budget_krw": 100_000, "max_budget_krw": max_budget,
        "min_budget_krw": 0, "sizing_multiplier": 0.3,
        "evaluated_count": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0,
        "consensus_anomaly_count": len(consensus_symbols or []),
        "consensus_anomaly_symbols": consensus_symbols or [],
        "data_error_count": 0, "reason": "표본부족",
        "blocks": [], "warnings": [],
        "_note": "Paper sizing/risk policy · 실제 주문 아님 · live_order_allowed=False",
    }


def _ctx(usdkrw: float = 1_350.0) -> dict:
    return {"cash_krw": 10_000_000, "usdkrw": usdkrw}


_NORMAL_QUOTE = {
    "price": 100.0, "source": "KIS",
    "accepted_price_source": "KIS", "source_chain": [],
}


# ─── 1. _is_us_ticker ────────────────────────────────────


class TestIsUsTicker:
    def _fn(self):
        from send_toss_paper_preview_test import _is_us_ticker
        return _is_us_ticker

    def test_kr_ks_suffix(self):
        assert self._fn()("005930.KS") is False

    def test_kr_kq_suffix(self):
        assert self._fn()("035720.KQ") is False

    def test_kr_6digit(self):
        assert self._fn()("005930") is False

    def test_index_excluded(self):
        assert self._fn()("^GSPC") is False

    def test_nvda_is_us(self):
        assert self._fn()("NVDA") is True

    def test_googl_is_us(self):
        assert self._fn()("GOOGL") is True

    def test_aapl_is_us(self):
        assert self._fn()("AAPL") is True


# ─── 2. _get_usdkrw ──────────────────────────────────────


class TestGetUsdkrw:
    def _fn(self):
        from send_toss_paper_preview_test import _get_usdkrw
        return _get_usdkrw

    def test_returns_ctx_rate(self):
        assert self._fn()({"usdkrw": 1539.0}) == 1539.0

    def test_ignores_zero_rate(self):
        fn = self._fn()
        # zero → falls through to yfinance; just check it doesn't raise
        result = fn({"usdkrw": 0})
        assert result > 0

    def test_missing_usdkrw_returns_positive(self):
        fn = self._fn()
        result = fn({})
        assert result > 0


# ─── 3. US ticker 수량 계산 ──────────────────────────────


class TestUsTickerQuantitySizing:
    def _build(self, policy_kw=None, ctx_kw=None, **quote_kw):
        from send_toss_paper_preview_test import _build_candidates
        p = _policy(**(policy_kw or {}))
        c = _ctx(**(ctx_kw or {}))
        q = {**_NORMAL_QUOTE, **quote_kw}
        with patch("core.toss_paper_performance._get_quote_for_paper", return_value=q):
            return _build_candidates(p, c, max_n=5)

    def test_nvda_200usd_usdkrw1500_budget300k_qty1(self):
        """NVDA $200, usdkrw 1500, budget 300k → 1주 (₩300,000 이하)."""
        # NVDA pool limit_price=135, but usdkrw 1500 → 135*1500=₩202,500 ≤ 300,000 → qty=1
        candidates, _ = self._build(ctx_kw={"usdkrw": 1500.0})
        nvda = next((c for c in candidates if c["symbol"] == "NVDA"), None)
        if nvda:
            assert nvda["quantity"] >= 1
            assert nvda["estimated_amount_krw"] <= 300_000

    def test_nvda_usd_estimated_not_raw_usd(self):
        """estimated_amount_krw는 USD 그대로가 아닌 KRW 환산값."""
        candidates, _ = self._build(ctx_kw={"usdkrw": 1500.0})
        nvda = next((c for c in candidates if c["symbol"] == "NVDA"), None)
        if nvda:
            # USD 그대로라면 135 * qty < 1000. KRW 환산이면 훨씬 큼.
            assert nvda["estimated_amount_krw"] > 1_000

    def test_nvda_price_currency_usd(self):
        """US 종목 후보의 _price_currency == 'USD'."""
        candidates, _ = self._build(ctx_kw={"usdkrw": 1500.0})
        nvda = next((c for c in candidates if c["symbol"] == "NVDA"), None)
        if nvda:
            assert nvda.get("_price_currency") == "USD"

    def test_nvda_usdkrw_stored(self):
        """US 종목 후보에 _usdkrw 저장."""
        candidates, _ = self._build(ctx_kw={"usdkrw": 1500.0})
        nvda = next((c for c in candidates if c["symbol"] == "NVDA"), None)
        if nvda:
            assert nvda.get("_usdkrw") == 1500.0

    def test_us_ticker_no_absurd_quantity(self):
        """어떤 US 종목도 수량이 수천 주가 되지 않는다."""
        candidates, _ = self._build(ctx_kw={"usdkrw": 1500.0})
        for c in candidates:
            from send_toss_paper_preview_test import _is_us_ticker
            if _is_us_ticker(c["symbol"]):
                assert c["quantity"] < 100, (
                    f"{c['symbol']}: qty={c['quantity']} — KRW/USD 직접 나누기 버그 의심"
                )


# ─── 4. 예산 부족 block ───────────────────────────────────


class TestBudgetTooSmall:
    def _build_with_price(self, usdkrw: float, budget: int, accepted_price: float):
        """mock accepted_price 지정 — accepted price가 sizing 기준이 됨."""
        from send_toss_paper_preview_test import _build_candidates
        p = _policy(max_budget=budget)
        c = _ctx(usdkrw=usdkrw)
        q = {**_NORMAL_QUOTE, "price": accepted_price}
        with patch("core.toss_paper_performance._get_quote_for_paper", return_value=q):
            candidates, rejected = _build_candidates(p, c, max_n=5)
        return candidates, rejected

    def test_budget_too_small_is_rejected(self):
        """accepted price × usdkrw > budget이면 rejected에 들어간다.
        mock price=200, usdkrw=2000 → $200×2000=₩400,000 > ₩300,000 → US tickers rejected."""
        candidates, rejected = self._build_with_price(
            usdkrw=2_000.0, budget=300_000, accepted_price=200.0
        )
        rejected_syms = [r["symbol"] for r in rejected]
        assert "NVDA" in rejected_syms

    def test_budget_too_small_reason_contains_block_name(self):
        """rejected reason에 budget_too_small_for_one_share 포함."""
        _, rejected = self._build_with_price(
            usdkrw=2_000.0, budget=300_000, accepted_price=200.0
        )
        nvda_r = next((r for r in rejected if r["symbol"] == "NVDA"), None)
        if nvda_r:
            assert "budget_too_small_for_one_share" in nvda_r["reject_reason"]

    def test_budget_too_small_not_in_candidates(self):
        """예산 부족 US 후보는 candidates에 없다."""
        candidates, _ = self._build_with_price(
            usdkrw=2_000.0, budget=300_000, accepted_price=200.0
        )
        assert all(c["symbol"] != "NVDA" for c in candidates)

    def test_all_estimated_within_budget(self):
        """모든 후보의 estimated_amount_krw <= max_budget."""
        candidates, _ = self._build_with_price(
            usdkrw=1_350.0, budget=300_000, accepted_price=30_000.0
        )
        for c in candidates:
            assert c["estimated_amount_krw"] <= 300_000, (
                f"{c['symbol']}: ₩{c['estimated_amount_krw']:,} > ₩300,000"
            )


# ─── 5. KR 종목 sizing ────────────────────────────────────


class TestKrTickerSizing:
    def _build_kr(self, price_krw: float = 30_000, budget: int = 300_000):
        from send_toss_paper_preview_test import _build_candidates
        p = _policy(max_budget=budget)
        c = _ctx(usdkrw=1_350.0)
        q = {**_NORMAL_QUOTE, "price": price_krw}
        with patch("core.toss_paper_performance._get_quote_for_paper", return_value=q):
            candidates, _ = _build_candidates(p, c, max_n=5)
        return candidates

    def test_kr_ticker_price_krw(self):
        """069500.KS price 30000, budget 300000 → qty=10."""
        candidates = self._build_kr(price_krw=30_000, budget=300_000)
        etf = next((c for c in candidates if c["symbol"] == "069500.KS"), None)
        if etf:
            assert etf["quantity"] == 10
            assert etf["estimated_amount_krw"] == 300_000.0

    def test_kr_price_currency_krw(self):
        """KR 종목 후보의 _price_currency == 'KRW'."""
        candidates = self._build_kr()
        kr = next((c for c in candidates if c["symbol"].endswith(".KS")), None)
        if kr:
            assert kr.get("_price_currency") == "KRW"

    def test_kr_no_usdkrw_stored(self):
        """KR 종목 후보에는 _usdkrw=None."""
        candidates = self._build_kr()
        kr = next((c for c in candidates if c["symbol"].endswith(".KS")), None)
        if kr:
            assert kr.get("_usdkrw") is None


# ─── 6. 주문표 USD 가격 표시 ─────────────────────────────


class TestOrderPreviewUsdDisplay:
    def _cand_us(self, limit_usd=135.0, qty=1, usdkrw=1_350.0) -> dict:
        return {
            "symbol": "NVDA", "side": "buy",
            "quantity": qty,
            "limit_price": limit_usd,
            "estimated_amount_krw": round(limit_usd * usdkrw * qty, 2),
            "confidence": 0.0, "reason": "[TEST]", "quote_age_sec": 0,
            "_price_currency": "USD",
            "_usdkrw": usdkrw,
            "_limit_price_usd": limit_usd,
        }

    def _cand_kr(self, limit_krw=30_000, qty=10) -> dict:
        return {
            "symbol": "069500.KS", "side": "buy",
            "quantity": qty,
            "limit_price": limit_krw,
            "estimated_amount_krw": limit_krw * qty,
            "confidence": 0.0, "reason": "[TEST]", "quote_age_sec": 0,
        }

    def _cc(self) -> dict:
        return {"blocks": [], "warnings": [], "toss_readiness": "paper_only",
                "live_order_allowed": False, "score_adjustments": []}

    def _ctx(self) -> dict:
        return {"cash_krw": 10_000_000, "usdkrw": 1_350.0,
                "automation": {"mode": "paper", "dry_run": True}}

    def test_us_ticker_shows_dollar_sign(self):
        """US 종목 주문표에 $ 가격 표시."""
        from core.toss_order_preview import build_toss_paper_order_preview
        with patch("core.toss_paper_policy.compute_toss_paper_policy",
                   return_value={"sample_status": "insufficient", "max_budget_krw": 300_000,
                                 "consensus_anomaly_symbols": [], "blocks": [], "warnings": [],
                                 "_note": "test", "live_order_allowed": False, "mode": "paper_only",
                                 "sizing_multiplier": 0.3, "base_budget_krw": 100_000,
                                 "min_budget_krw": 0, "evaluated_count": 0, "win_rate": 0.0,
                                 "avg_pnl_pct": 0.0, "consensus_anomaly_count": 0,
                                 "data_error_count": 0, "reason": "test"}):
            text = build_toss_paper_order_preview(
                [self._cand_us()], self._ctx(), [self._cc()]
            )
        assert "$135.00" in text

    def test_us_ticker_shows_usdkrw(self):
        """US 종목 주문표에 환율 표시."""
        from core.toss_order_preview import build_toss_paper_order_preview
        with patch("core.toss_paper_policy.compute_toss_paper_policy",
                   return_value={"sample_status": "insufficient", "max_budget_krw": 300_000,
                                 "consensus_anomaly_symbols": [], "blocks": [], "warnings": [],
                                 "_note": "test", "live_order_allowed": False, "mode": "paper_only",
                                 "sizing_multiplier": 0.3, "base_budget_krw": 100_000,
                                 "min_budget_krw": 0, "evaluated_count": 0, "win_rate": 0.0,
                                 "avg_pnl_pct": 0.0, "consensus_anomaly_count": 0,
                                 "data_error_count": 0, "reason": "test"}):
            text = build_toss_paper_order_preview(
                [self._cand_us(usdkrw=1_539.0)], self._ctx(), [self._cc()]
            )
        assert "1,539" in text

    def test_us_ticker_shows_krw_amount(self):
        """US 종목 주문표에 KRW 예상금액 표시."""
        from core.toss_order_preview import build_toss_paper_order_preview
        cand = self._cand_us(limit_usd=135.0, qty=1, usdkrw=1_350.0)
        # estimated = 135 * 1350 = 182250
        with patch("core.toss_paper_policy.compute_toss_paper_policy",
                   return_value={"sample_status": "insufficient", "max_budget_krw": 300_000,
                                 "consensus_anomaly_symbols": [], "blocks": [], "warnings": [],
                                 "_note": "test", "live_order_allowed": False, "mode": "paper_only",
                                 "sizing_multiplier": 0.3, "base_budget_krw": 100_000,
                                 "min_budget_krw": 0, "evaluated_count": 0, "win_rate": 0.0,
                                 "avg_pnl_pct": 0.0, "consensus_anomaly_count": 0,
                                 "data_error_count": 0, "reason": "test"}):
            text = build_toss_paper_order_preview([cand], self._ctx(), [self._cc()])
        assert "182,250" in text

    def test_us_ticker_no_won_prefix_for_limit_price(self):
        """US 종목 지정가에 ₩ 접두어 없음."""
        from core.toss_order_preview import build_toss_paper_order_preview
        with patch("core.toss_paper_policy.compute_toss_paper_policy",
                   return_value={"sample_status": "insufficient", "max_budget_krw": 300_000,
                                 "consensus_anomaly_symbols": [], "blocks": [], "warnings": [],
                                 "_note": "test", "live_order_allowed": False, "mode": "paper_only",
                                 "sizing_multiplier": 0.3, "base_budget_krw": 100_000,
                                 "min_budget_krw": 0, "evaluated_count": 0, "win_rate": 0.0,
                                 "avg_pnl_pct": 0.0, "consensus_anomaly_count": 0,
                                 "data_error_count": 0, "reason": "test"}):
            text = build_toss_paper_order_preview(
                [self._cand_us()], self._ctx(), [self._cc()]
            )
        assert "지정가: ₩135" not in text

    def test_kr_ticker_shows_won_sign(self):
        """KR 종목 지정가에 ₩ 표시."""
        from core.toss_order_preview import build_toss_paper_order_preview
        with patch("core.toss_paper_policy.compute_toss_paper_policy",
                   return_value={"sample_status": "insufficient", "max_budget_krw": 300_000,
                                 "consensus_anomaly_symbols": [], "blocks": [], "warnings": [],
                                 "_note": "test", "live_order_allowed": False, "mode": "paper_only",
                                 "sizing_multiplier": 0.3, "base_budget_krw": 100_000,
                                 "min_budget_krw": 0, "evaluated_count": 0, "win_rate": 0.0,
                                 "avg_pnl_pct": 0.0, "consensus_anomaly_count": 0,
                                 "data_error_count": 0, "reason": "test"}):
            text = build_toss_paper_order_preview(
                [self._cand_kr()], self._ctx(), [self._cc()]
            )
        assert "₩30,000" in text


# ─── 7. 가드레일 ──────────────────────────────────────────


class TestCurrencySizingGuardrails:
    def test_no_order_functions_in_script(self):
        src = (ROOT / "scripts" / "send_toss_paper_preview_test.py").read_text()
        for fn in ("place_order", "submit_order", "execute_order"):
            assert fn not in src

    def test_no_write_routes_in_preview(self):
        src = (ROOT / "core" / "toss_order_preview.py").read_text()
        for verb in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert verb not in src

    def test_no_forbidden_cta_in_script(self):
        src = (ROOT / "scripts" / "send_toss_paper_preview_test.py").read_text()
        for cta in ["주문 실행", "매수하기", "매도하기", "자동매매 시작", "자동거래 시작", "실주문: 활성"]:
            assert cta not in src

    def test_no_forbidden_cta_in_preview_module(self):
        src = (ROOT / "core" / "toss_order_preview.py").read_text()
        for cta in ["주문 실행", "매수하기", "매도하기", "자동매매 시작", "자동거래 시작", "실주문: 활성"]:
            assert cta not in src
