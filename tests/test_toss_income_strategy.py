"""수입형 자율매매 v1 income gate 테스트."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_income_strategy import compute_income_edge, estimate_win_prob, prepare_income_buy_plan, build_rebalance_plan


def _candidate(symbol="000111.KS", price=50_000, target=56_000, stop=48_000, qty=10, score=85, rr=3.0, **kw):
    base = {
        "symbol": symbol,
        "side": "buy",
        "quantity": qty,
        "limit_price": float(price),
        "current_price": float(price),
        "target_price": float(target),
        "stop_loss": float(stop),
        "estimated_amount_krw": float(price * qty),
        "score": score,
        "risk_reward": rr,
        "decision_bucket": "PASS_EXECUTE",
    }
    base.update(kw)
    return base


def test_positive_expected_pnl_is_income_pass():
    out = compute_income_edge(_candidate(), pending_orders={})

    assert out["income_pass"] is True
    assert out["income_grade"] in {"INCOME_PASS", "SMALL_INCOME_PASS"}
    assert out["expected_pnl_krw"] > 0
    assert out["income_edge_ratio"] >= 0.006


def test_negative_expected_pnl_blocks_even_if_quality_bucket_passes():
    out = compute_income_edge(_candidate(target=50_750, stop=48_000, score=65, rr=2.0), pending_orders={})

    assert out["income_pass"] is False
    assert out["income_grade"] == "BLOCK"
    assert "expected_pnl" in out["income_block_reason"]


def test_high_rr_but_deep_stop_risk_blocks():
    out = compute_income_edge(_candidate(target=70_000, stop=44_000, score=90, rr=4.0), pending_orders={})

    assert out["income_pass"] is False
    assert out["income_grade"] == "BLOCK"
    assert out["stop_risk_pct"] > 4.5
    assert "stop_risk_pct" in out["income_block_reason"]


def test_same_symbol_pending_blocks_new_buy():
    out = compute_income_edge(
        _candidate(),
        pending_orders=[{"symbol": "000111.KS", "side": "buy", "status": "pending"}],
    )

    assert out["income_pass"] is False
    assert out["income_grade"] == "BLOCK"
    assert "same_symbol_pending" in out["income_block_reason"]


def test_recent_risk_sell_cooldown_blocks_reentry():
    out = compute_income_edge(
        _candidate(symbol="403870.KS"),
        pending_orders={},
        recent_risk_sells={"403870.KS": {"reason": "position_review_sell"}},
    )

    assert out["income_pass"] is False
    assert out["income_grade"] == "BLOCK"
    assert "recent_risk_sell_cooldown" in out["income_block_reason"]


def test_low_sample_reliability_does_not_zero_win_probability():
    c = _candidate(symbol="SAMPLE.KS", score=82)
    win_prob = estimate_win_prob(c, reliability_stats={"SAMPLE.KS": {"count": 1, "win_rate": 0.0}})
    out = compute_income_edge(c, pending_orders={}, reliability_stats={"SAMPLE.KS": {"count": 1, "win_rate": 0.0}})

    assert win_prob >= 0.5
    assert out["win_prob"] >= 0.5
    assert out["income_block_reason"] != "win_prob_zero"



def test_prepare_income_buy_plan_tightens_six_pct_stop_and_recomputes_rr():
    c = _candidate(price=50_000, target=58_000, stop=47_000, qty=4, score=88, rr=2.6)

    planned = prepare_income_buy_plan(c)
    out = compute_income_edge(planned, pending_orders={})

    assert planned["original_stop_loss"] == 47_000.0
    assert planned["stop_loss"] > 47_000.0
    assert planned["income_exit_plan"]["stop_risk_pct"] <= 4.5
    assert planned["risk_reward"] >= 1.5
    assert out["income_pass"] is True



def _holding(symbol, name, pl_amount, purchase, qty=1, last=10000, currency="KRW", daily=-1000):
    return {
        "symbol": symbol,
        "name": name,
        "quantity": str(qty),
        "lastPrice": str(last),
        "currency": currency,
        "marketValue": {"purchaseAmount": str(purchase), "amount": str(purchase + pl_amount)},
        "profitLoss": {"amountAfterCost": str(pl_amount), "amount": str(pl_amount)},
        "dailyProfitLoss": {"amount": str(daily)},
    }


def test_build_rebalance_plan_ranks_weak_holdings_and_links_income_waitlist():
    holdings = [
        _holding("AAA", "큰손실", -30_000, 300_000, qty=3, last=90_000, daily=-10_000),
        _holding("BBB", "작은손실", -5_000, 200_000, qty=2, last=95_000, daily=-1_000),
        _holding("CCC", "수익", 20_000, 200_000, qty=2, last=110_000, daily=2_000),
    ]
    buys = [
        {"symbol": "BUY1.KS", "name": "수입후보", "market": "KR", "estimated_amount_krw": 180_000,
         "income_strategy": {"income_pass": True, "expected_pnl_krw": 12_000, "income_edge_ratio": 0.06},
         "execution_status": "portfolio_rebalance_required"},
    ]

    plan = build_rebalance_plan({"holdings_count": 25, "holdings_items": holdings, "cash": {"krw_native": 50_000}}, buys)

    assert plan["portfolio_rebalance_required"] is True
    assert plan["target_holding_count"] == 12
    assert plan["reduce_positions_by"] == 13
    assert plan["income_buy_waitlist"][0]["symbol"] == "BUY1.KS"
    assert plan["sell_to_fund_candidates"][0]["symbol"] == "AAA"
    assert plan["sell_to_fund_candidates"][0]["estimated_release_krw"] > 0
    assert plan["funding_gap_krw"] == 130_000


def test_build_rebalance_plan_does_not_force_sells_when_holdings_are_under_cap():
    plan = build_rebalance_plan({"holdings_count": 8, "holdings_items": []}, [])

    assert plan["portfolio_rebalance_required"] is False
    assert plan["sell_to_fund_candidates"] == []


# ── USD 환산 + AI Berkshire 병합 ─────────────────────────────────

_NO_SCORES = {"items": {"__NONE__": {"classification": "hold"}}}  # 대상 심볼 미포함


def test_usd_holding_release_converted_with_fx():
    holdings = [_holding("ABBV", "AbbVie", -9_000, 200_000, qty=2, last=249.95, currency="USD")]
    plan = build_rebalance_plan(
        {"holdings_count": 25, "holdings_items": holdings,
         "exchange_rate": {"base": "USD", "quote": "KRW", "rate": 1509.7}},
        [], berkshire_scores=_NO_SCORES,
    )
    row = plan["sell_to_fund_candidates"][0]
    assert row["release_currency"] == "USD"
    assert row["estimated_release_usd"] == 499.9
    assert row["estimated_release_krw"] == round(499.9 * 1509.7, 2)
    assert row["fx_rate_used"] == 1509.7
    assert row["valuation_warning"] is None
    assert row["estimated_release_krw"] > 0


def test_krw_holding_release_unchanged():
    holdings = [_holding("AAA", "국내주", -9_000, 200_000, qty=2, last=95_000)]
    plan = build_rebalance_plan(
        {"holdings_count": 25, "holdings_items": holdings,
         "exchange_rate": {"rate": 1509.7}},
        [], berkshire_scores=_NO_SCORES,
    )
    row = plan["sell_to_fund_candidates"][0]
    assert row["release_currency"] == "KRW"
    assert row["estimated_release_krw"] == 191_000.0
    assert row["fx_rate_used"] is None
    assert row["valuation_warning"] is None


def test_usd_cumulative_release_includes_fx():
    holdings = [
        _holding("AAA", "국내주", -30_000, 300_000, qty=2, last=100_000),
        _holding("ABBV", "AbbVie", -9_000, 200_000, qty=1, last=200.0, currency="USD"),
    ]
    plan = build_rebalance_plan(
        {"holdings_count": 25, "holdings_items": holdings,
         "exchange_rate": {"rate": 1500.0}},
        [], berkshire_scores=_NO_SCORES,
    )
    rows = plan["sell_to_fund_candidates"]
    total = sum(r["estimated_release_krw"] for r in rows)
    assert rows[-1]["cumulative_release_krw"] == round(total, 2)
    assert any(r["estimated_release_krw"] == 300_000.0 for r in rows)  # 200 USD × 1500


def test_usd_holding_without_fx_marks_missing_usdkrw():
    holdings = [_holding("ABBV", "AbbVie", -9_000, 200_000, qty=2, last=249.95, currency="USD")]
    plan = build_rebalance_plan(
        {"holdings_count": 25, "holdings_items": holdings},  # exchange_rate 없음
        [], berkshire_scores=_NO_SCORES,
    )
    row = plan["sell_to_fund_candidates"][0]
    assert row["estimated_release_krw"] is None
    assert row["valuation_warning"] == "missing_usdkrw"
    assert row["estimated_release_usd"] == 499.9  # USD 원값은 유지


# ── 통화별 income 자금조달 매도 (funding) ────────────────────────

# thesis freshness 스키마: valid_until/source_urls 없으면 gray_zone 강등되므로
# fixture에는 먼 미래 유효기간 + 더미 URL을 채운다 (테스트 결정성)
_FRESH_FIELDS = {"as_of": "2026-07-10", "valid_until": "2099-12-31",
                 "thesis": "test thesis", "red_lines": ["test red line"],
                 "source_urls": ["https://example.com/ir"]}

_XOM_TRIM_SCORES = {"items": {
    "XOM": {"name": "Exxon Mobil", "classification": "trim",
            "sell_to_fund_adjustment": 1.0, **_FRESH_FIELDS},
    "ABBV": {"name": "AbbVie", "classification": "hold",
             "sell_to_fund_adjustment": -3.5, **_FRESH_FIELDS},
}}


def _buy(symbol, market="US", est_usd=None, est_krw=0.0, expected=10_000.0):
    return {
        "symbol": symbol, "name": symbol, "market": market,
        "estimated_amount_krw": est_krw, "estimated_amount_usd": est_usd,
        "income_strategy": {"income_pass": True, "expected_pnl_krw": expected,
                            "income_edge_ratio": 0.01},
    }


def _usd_account(usd_cash=0.85, krw_cash=5_000_000.0, holdings=(), count=19, fx=1509.7):
    return {
        "holdings_count": count,
        "holdings_items": list(holdings),
        "cash": {"krw_native": krw_cash, "krw": krw_cash, "usd": usd_cash},
        "exchange_rate": {"base": "USD", "quote": "KRW", "rate": fx},
    }


def _xom_holding(qty=2, last=150.0):
    # release = qty × last USD
    return _holding("XOM", "엑슨모빌", -9_000, 200_000, qty=qty, last=last, currency="USD")


def test_funding_true_when_trim_sale_fully_funds_income_candidate():
    # 보유 19, USD $0.85, XOM trim release $300, MS 후보 $222.13
    plan = build_rebalance_plan(
        _usd_account(holdings=[_xom_holding(qty=2, last=150.0)]),
        [_buy("MS", est_usd=222.13, est_krw=335_000, expected=7_769.96)],
        berkshire_scores=_XOM_TRIM_SCORES,
    )
    assert plan["funding_rebalance_required"] is True
    assert plan["funding_currency"] == "USD"
    assert plan["funding_target"]["symbol"] == "MS"
    assert plan["funding_target"]["expected_pnl_krw"] == 7_769.96
    assert plan["available_cash_native"] == 0.85
    assert plan["funding_gap_native"] == round(222.13 - 0.85, 4)
    rows = plan["sell_to_fund_candidates"]
    assert [r["symbol"] for r in rows] == ["XOM"]
    row = rows[0]
    assert row["funding_mode"] == "currency_income_replacement"
    assert row["funding_currency"] == "USD"
    assert row["funding_target_symbol"] == "MS"
    assert row["estimated_release_native"] == 300.0
    assert row["cumulative_release_native"] == 300.0
    assert row["covers_funding_target"] is True


def test_funding_false_when_sale_cannot_fully_fund_cheapest_candidate():
    # XOM release $137.47 → $0.85 + $137.47 < $222.13 → 먼저 팔면 안 됨
    plan = build_rebalance_plan(
        _usd_account(holdings=[_xom_holding(qty=1, last=137.47)]),
        [_buy("MS", est_usd=222.13, est_krw=335_000)],
        berkshire_scores=_XOM_TRIM_SCORES,
    )
    assert plan["funding_rebalance_required"] is False
    assert plan["sell_to_fund_candidates"] == []


def test_funding_false_when_only_hold_holdings_exist():
    # ABBV hold $400 뿐 → eligible 확보액 0 → funding 불가
    abbv = _holding("ABBV", "AbbVie", -9_000, 200_000, qty=2, last=200.0, currency="USD")
    plan = build_rebalance_plan(
        _usd_account(holdings=[abbv]),
        [_buy("MS", est_usd=222.13, est_krw=335_000)],
        berkshire_scores=_XOM_TRIM_SCORES,
    )
    assert plan["funding_rebalance_required"] is False
    assert plan["sell_to_fund_candidates"] == []


def test_funding_false_when_only_unscored_holdings_exist():
    mcd = _holding("MCD", "맥도날드", -5_000, 400_000, qty=2, last=280.0, currency="USD")
    plan = build_rebalance_plan(
        _usd_account(holdings=[mcd]),
        [_buy("MS", est_usd=222.13, est_krw=335_000)],
        berkshire_scores=_XOM_TRIM_SCORES,
    )
    assert plan["funding_rebalance_required"] is False
    assert plan["sell_to_fund_candidates"] == []


def test_funding_false_when_krw_cash_sufficient_for_kr_candidate():
    plan = build_rebalance_plan(
        _usd_account(krw_cash=2_000_000, holdings=[_xom_holding()]),
        [_buy("005930.KS", market="KR", est_krw=180_000)],
        berkshire_scores=_XOM_TRIM_SCORES,
    )
    assert plan["funding_rebalance_required"] is False


def test_funding_gap_computed_in_native_usd_not_krw():
    plan = build_rebalance_plan(
        _usd_account(usd_cash=0.85, holdings=[_xom_holding()], fx=1509.7),
        [_buy("MS", est_usd=222.13, est_krw=335_000)],
        berkshire_scores=_XOM_TRIM_SCORES,
    )
    assert plan["funding_gap_native"] == 221.28
    assert plan["funding_gap_krw"] == round(221.28 * 1509.7, 2)


def test_funding_target_is_highest_expected_pnl_among_fundable():
    plan = build_rebalance_plan(
        _usd_account(holdings=[_xom_holding(qty=2, last=150.0)]),
        [
            _buy("MS", est_usd=222.13, est_krw=335_000, expected=7_769.96),
            _buy("GS", est_usd=250.00, est_krw=377_000, expected=12_000.0),
            # 확보액으로도 못 사는 비싼 후보는 기대손익이 더 높아도 제외
            _buy("BRK", est_usd=900.00, est_krw=1_360_000, expected=50_000.0),
        ],
        berkshire_scores=_XOM_TRIM_SCORES,
    )
    assert plan["funding_rebalance_required"] is True
    assert plan["funding_target"]["symbol"] == "GS"


def test_funding_uses_minimum_rows_to_cover_target():
    scores = {"items": {
        "XOM": {"classification": "trim", "sell_to_fund_adjustment": 1.0, **_FRESH_FIELDS},
        "CVX": {"classification": "trim", "sell_to_fund_adjustment": 0.5, **_FRESH_FIELDS},
    }}
    # XOM 단독($300)으로 gap($221.28) 충당 → CVX row는 funding에 불필요
    cvx = _holding("CVX", "셰브론", -3_000, 300_000, qty=1, last=170.0, currency="USD")
    plan = build_rebalance_plan(
        _usd_account(holdings=[_xom_holding(qty=2, last=150.0), cvx]),
        [_buy("MS", est_usd=222.13, est_krw=335_000)],
        berkshire_scores=scores,
    )
    rows = plan["sell_to_fund_candidates"]
    assert [r["symbol"] for r in rows] == ["XOM"]
    assert rows[0]["covers_funding_target"] is True


def test_holdings_over_twenty_portfolio_rebalance_still_works_with_funding_fields():
    plan = build_rebalance_plan(
        _usd_account(count=25, holdings=[_xom_holding()]),
        [_buy("MS", est_usd=222.13, est_krw=335_000)],
        berkshire_scores=_XOM_TRIM_SCORES,
    )
    assert plan["portfolio_rebalance_required"] is True
    assert plan["funding_rebalance_required"] is True  # 두 경로 공존 가능
    assert any(r.get("funding_target_symbol") == "MS" for r in plan["sell_to_fund_candidates"])


def test_funding_target_found_beyond_display_waitlist_cap():
    """표시용 상위 8개 절단이 funding 계산을 가리면 안 된다.

    기대손익 상위 8개는 필요 USD가 커서 매도 후에도 전액 매수 불가,
    9위 후보($100)만 XOM 매도액($150)으로 가능 → funding target=9위.
    """
    buys = [
        _buy(f"BIG{i}", est_usd=1_000.0 + i, est_krw=1_510_000,
             expected=100_000.0 - i)
        for i in range(8)
    ] + [_buy("CHEAP", est_usd=100.0, est_krw=151_000, expected=5_000.0)]

    plan = build_rebalance_plan(
        _usd_account(usd_cash=1.0, holdings=[_xom_holding(qty=1, last=150.0)]),
        buys, berkshire_scores=_XOM_TRIM_SCORES,
    )

    # 표시 목록은 여전히 상위 8개 (CHEAP는 표시에서 잘림)
    assert len(plan["income_buy_waitlist"]) == 8
    assert all(w["symbol"] != "CHEAP" for w in plan["income_buy_waitlist"])
    # funding 계산은 전체 9개 사용 → 9위 CHEAP가 target
    assert plan["funding_rebalance_required"] is True
    assert plan["funding_currency"] == "USD"
    assert plan["funding_target"]["symbol"] == "CHEAP"
    assert plan["funding_gap_native"] == 99.0
    assert [r["symbol"] for r in plan["sell_to_fund_candidates"]] == ["XOM"]
    assert plan["sell_to_fund_candidates"][0]["funding_target_symbol"] == "CHEAP"


def test_funding_false_when_even_beyond_cap_candidates_unaffordable():
    # 9위 후보($200)도 cash $1 + XOM $150으로 전액 매수 불가 → funding=false
    buys = [
        _buy(f"BIG{i}", est_usd=1_000.0 + i, est_krw=1_510_000,
             expected=100_000.0 - i)
        for i in range(8)
    ] + [_buy("CHEAP", est_usd=200.0, est_krw=302_000, expected=5_000.0)]

    plan = build_rebalance_plan(
        _usd_account(usd_cash=1.0, holdings=[_xom_holding(qty=1, last=150.0)]),
        buys, berkshire_scores=_XOM_TRIM_SCORES,
    )
    assert plan["funding_rebalance_required"] is False
    assert plan["sell_to_fund_candidates"] == []


def test_funding_target_best_expected_among_fundable_across_full_list():
    # 전액 가능한 후보가 9·10위에 둘 있으면 그중 기대손익 최고를 선택
    buys = [
        _buy(f"BIG{i}", est_usd=1_000.0 + i, est_krw=1_510_000,
             expected=100_000.0 - i)
        for i in range(8)
    ] + [
        _buy("CHEAP_LO", est_usd=100.0, est_krw=151_000, expected=3_000.0),
        _buy("CHEAP_HI", est_usd=120.0, est_krw=181_200, expected=6_000.0),
    ]
    plan = build_rebalance_plan(
        _usd_account(usd_cash=1.0, holdings=[_xom_holding(qty=1, last=150.0)]),
        buys, berkshire_scores=_XOM_TRIM_SCORES,
    )
    assert plan["funding_rebalance_required"] is True
    assert plan["funding_target"]["symbol"] == "CHEAP_HI"


def test_funding_fail_closed_on_missing_or_broken_scores():
    for scores in ({}, {"items": {}}):
        plan = build_rebalance_plan(
            _usd_account(holdings=[_xom_holding()]),
            [_buy("MS", est_usd=222.13, est_krw=335_000)],
            berkshire_scores=scores,
        )
        assert plan["funding_rebalance_required"] is False, f"scores={scores!r}"
        assert plan["sell_to_fund_candidates"] == []


def test_rebalance_plan_rows_carry_ai_berkshire_fields():
    scores = {"items": {
        "AAA": {"name": "가", "classification": "hold",
                "sell_to_fund_adjustment": -3.5, **_FRESH_FIELDS},
        "BBB": {"name": "나", "classification": "trim",
                "sell_to_fund_adjustment": 1.0, **_FRESH_FIELDS},
    }}
    holdings = [
        _holding("AAA", "가", -30_000, 300_000, qty=3, last=90_000, daily=-10_000),
        _holding("BBB", "나", -5_000, 200_000, qty=2, last=95_000, daily=-1_000),
    ]
    plan = build_rebalance_plan(
        {"holdings_count": 25, "holdings_items": holdings}, [],
        berkshire_scores=scores,
    )
    by_symbol = {r["symbol"]: r for r in plan["sell_to_fund_candidates"]}
    assert by_symbol["AAA"]["ai_berkshire"]["classification"] == "hold"
    assert by_symbol["AAA"]["auto_sell_eligible"] is False
    assert by_symbol["AAA"]["auto_sell_block_reason"] == "ai_berkshire_hold"
    assert by_symbol["AAA"]["sell_to_fund_adjustment"] == -3.5
    assert by_symbol["AAA"]["adjusted_sell_priority"] == round(
        by_symbol["AAA"]["weakness_score"] - 3.5, 4)
    assert by_symbol["BBB"]["auto_sell_eligible"] is True
    assert by_symbol["BBB"]["auto_sell_block_reason"] is None


# ── pending 상태 불명 fail-closed ─────────────────────────────────

def test_pending_orders_unavailable_blocks_buy():
    """pending 맵 producer 실패(None) = '모름' → 신규 BUY 차단 (fail-closed)."""
    out = compute_income_edge(_candidate(), pending_orders=None)
    assert out["income_pass"] is False
    assert out["income_block_reason"] == "pending_state_unavailable"


def test_pending_orders_empty_dict_still_allows():
    """정상 조회 결과 pending 0건(빈 dict)은 차단 사유가 아니다."""
    out = compute_income_edge(_candidate(), pending_orders={})
    assert out["income_pass"] is True
