"""tests/test_toss_position_review.py

보유 포지션 일일 재평가 → 자동 매도 후보 테스트.

1. evaluate_holdings: 손절/익절 기준 판정, exit watch 담당 심볼 제외, fail-safe
2. evaluate_sell_to_fund_candidates: 리밸런싱 매도 하드가드 (보유 하한/루프·일일 상한/보호 심볼)
3. execute_sell_candidates: 가드 체인 (autonomous/kill switch/env sell/장중/dedup)
4. run_toss_position_review: 1일 1회 dedup + 시간 게이트 + 리스크 후보 우선순위
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

KST = timezone(timedelta(hours=9))

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.toss_position_review as tpr


_NOW = datetime(2026, 7, 2, 11, 0, tzinfo=KST)  # 목요일 11:00 (익일도 평일)

_REBALANCE_ENV_KEYS = (
    "TOSS_REBALANCE_MIN_HOLDINGS",
    "TOSS_REBALANCE_TARGET_HOLDINGS",
    "TOSS_REBALANCE_MAX_SELLS_PER_RUN",
    "TOSS_REBALANCE_MAX_SELLS_PER_DAY",
    "TOSS_REBALANCE_PROTECTED_SYMBOLS",
)


@contextmanager
def _rebalance_env(**overrides):
    """TOSS_REBALANCE_* 만 초기화 후 주입.

    os.environ 전체를 clear하면 이 구간에서 처음 import되는 모듈이
    빈 env를 영구 캐시할 수 있어 다른 테스트를 오염시킨다.
    """
    saved = {k: os.environ.get(k) for k in _REBALANCE_ENV_KEYS}
    for key in _REBALANCE_ENV_KEYS:
        os.environ.pop(key, None)
    for key, value in overrides.items():
        os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, previous in saved.items():
            os.environ.pop(key, None)
            if previous is not None:
                os.environ[key] = previous

_POLICY_SELL = {
    "mode": "autonomous_live_pilot",
    "autonomous_mode": True,
    "autonomous_kill_switch": False,
    "live_pilot_enabled": True,
    "requires_user_confirmation": False,
    "requires_second_confirmation": False,
    "live_order_allowed": True,
    "all_live_gates_open": True,
    "env_live_pilot_enabled": True,
    "env_live_order_allowed": True,
    "env_live_adapter_enabled": True,
    "adapter_status": "enabled",
    "live_transport_status": "configured",
    "side_mode": "BUY_SELL",
    "allowed_sides": ["buy", "sell"],
    "sell_allowed": True,
    "autonomous_allowed_sides": ["buy", "sell"],
    "max_order_krw": 0,
    "blocked_symbols": [],
}


def _holding(symbol="123450", pl_amount=-9000, purchase=100000, qty=10,
             last_price=9100, **kw) -> dict:
    base = {
        "symbol": symbol,
        "name": "테스트종목",
        "quantity": qty,
        "lastPrice": last_price,
        "currency": "KRW",
        "profitLoss": {"amount": pl_amount, "amountAfterCost": pl_amount},
        "marketValue": {"purchaseAmount": purchase, "amount": purchase + pl_amount},
    }
    base.update(kw)
    return base


# ── 1. evaluate_holdings ─────────────────────────────────────────

class TestEvaluateHoldings(unittest.TestCase):
    def _eval(self, items, exit_covered=None, income_managed=None):
        with patch.object(tpr, "_symbols_with_active_exit_levels",
                          return_value=exit_covered or set()), \
             patch.object(tpr, "_income_managed_symbols",
                          return_value=income_managed or set()):
            return tpr.evaluate_holdings(items)

    def test_stop_loss_full_sell(self):
        # -9% → 손절, 전량
        out = self._eval([_holding(pl_amount=-9000)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "stop_loss")
        self.assertEqual(out[0]["quantity"], 10)
        self.assertEqual(out[0]["symbol"], "123450.KS")
        self.assertEqual(out[0]["pl_pct"], -9.0)

    def test_take_profit_partial_sell(self):
        # +16% → 분할 익절 절반
        out = self._eval([_holding(pl_amount=16000)])
        self.assertEqual(out[0]["action"], "take_profit")
        self.assertEqual(out[0]["quantity"], 5)

    def test_in_range_no_candidate(self):
        # -3% → 대상 아님
        self.assertEqual(self._eval([_holding(pl_amount=-3000)]), [])

    def test_exit_covered_symbol_in_range_is_not_a_review_candidate(self):
        out = self._eval([_holding(symbol="123450", pl_amount=-3000)],
                         exit_covered={"123450.KS"})
        self.assertEqual(out, [])

    def test_aggregate_stop_loss_not_suppressed_by_active_exit_level(self):
        # 최신 매수분 exit 레벨이 있어도 전체 Toss 포지션 손익이 -8% 이하라면
        # 보유 리스크 재평가 후보로 남겨야 한다. 아니면 평균단가 기준 손실이 방치된다.
        out = self._eval([_holding(symbol="123450", pl_amount=-9000)],
                         exit_covered={"123450.KS"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "stop_loss")
        self.assertTrue(out[0]["exit_covered"])

    def test_missing_purchase_amount_fail_safe(self):
        item = _holding(pl_amount=-9000)
        item["marketValue"] = {}
        self.assertEqual(self._eval([item]), [])

    def test_zero_quantity_or_price_skipped(self):
        self.assertEqual(self._eval([_holding(qty=0)]), [])
        self.assertEqual(self._eval([_holding(last_price=0)]), [])

    def test_us_symbol_kept_as_is(self):
        out = self._eval([_holding(symbol="NVDA", pl_amount=-9000, currency="USD")])
        self.assertEqual(out[0]["symbol"], "NVDA")
        self.assertEqual(out[0]["currency"], "USD")

    def test_env_thresholds(self):
        with patch.dict("os.environ", {"TOSS_REVIEW_STOP_LOSS_PCT": "-3"}):
            out = self._eval([_holding(pl_amount=-4000)])
        self.assertEqual(out[0]["action"], "stop_loss")

    def test_take_profit_min_one_share(self):
        out = self._eval([_holding(pl_amount=16000, qty=1)])
        self.assertEqual(out[0]["quantity"], 1)


    def test_income_position_takes_profit_at_one_point_five_pct_for_one_share(self):
        out = self._eval([
            _holding(symbol="555550", pl_amount=1600, purchase=100000, qty=1, last_price=101600)
        ], income_managed={"555550.KS"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "income_take_profit")
        self.assertEqual(out[0]["quantity"], 1)
        self.assertEqual(out[0]["review_reason"], "income_position_review")

    def test_income_position_early_stop_at_minus_two_point_five_pct(self):
        out = self._eval([
            _holding(symbol="555551", pl_amount=-2600, purchase=100000, qty=3, last_price=97400)
        ], income_managed={"555551.KS"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "income_early_stop_loss")
        self.assertEqual(out[0]["quantity"], 3)

    def test_manual_position_still_uses_old_fallback_thresholds(self):
        self.assertEqual(self._eval([_holding(symbol="555552", pl_amount=1600, purchase=100000, qty=1)]), [])



# ── 2. evaluate_sell_to_fund_candidates ──────────────────────────

def _plan_row(symbol, name="약한종목", qty=6, last=35450.0, pl_pct=-6.55,
              weakness=16.69, release=212700.0, eligible=True,
              adjustment=0.0, classification="sell_to_fund",
              currency="KRW") -> dict:
    return {
        "symbol": symbol,
        "side": "sell",
        "name": name,
        "quantity": qty,
        "last_price": last,
        "currency": currency,
        "estimated_release_krw": release,
        "pl_pct": pl_pct,
        "daily_pl_pct": -3.43,
        "weakness_score": weakness,
        "action": "sell_to_fund_candidate",
        "read_only": True,
        "ai_berkshire": {"classification": classification,
                         "berkshire_score": 8.0, "confidence": "medium"},
        "sell_to_fund_adjustment": adjustment,
        "adjusted_sell_priority": weakness + adjustment,
        "auto_sell_eligible": eligible,
        "auto_sell_block_reason": None if eligible else f"ai_berkshire_{classification}",
    }


def _plan(rows, required=True, funding=False, funding_currency=None,
          funding_target=None) -> dict:
    authority_rows = [
        row for row in rows
        if row.get("auto_sell_eligible") is True
        and row.get("funding_mode") == "currency_income_replacement"
        and row.get("covers_funding_target") is True
    ]
    if isinstance(funding_target, dict):
        target = dict(funding_target)
    elif funding_target:
        target = {
            "symbol": funding_target,
            "side": "buy",
            "currency": funding_currency,
            "income_pass": True,
            "decision_expected_pnl_krw": 10_000.0,
            "decision_income_edge_ratio": 0.03,
            "decision_expected_pnl_model": "income_exit_lifecycle_v1",
            "decision_expected_pnl_scope": "full_position_threshold_exit",
        }
    else:
        target = None
    return {
        "portfolio_rebalance_required": required,
        "sell_to_fund_candidates": rows,
        "funding_rebalance_required": funding,
        "funding_currency": funding_currency,
        "funding_target": target,
        "funding_source_symbol": (
            authority_rows[0].get("symbol")
            if funding and len(authority_rows) == 1 else None
        ),
        "funding_gap_native": (
            next((row.get("funding_gap_native") for row in rows
                  if row.get("funding_gap_native") is not None), None)
            if funding else None
        ),
    }


def _funding_row(symbol, target="MS", currency="USD", gap=221.28,
                 native=300.0, cumulative=None, covers=True, **kw):
    row = _plan_row(symbol, currency=currency, classification="trim", **kw)
    row["quantity"] = 1
    row["last_price"] = native
    row["estimated_release_usd"] = native if currency == "USD" else None
    row["estimated_release_krw"] = native * 1_418.0 if currency == "USD" else native
    row.update({
        "funding_mode": "currency_income_replacement",
        "funding_currency": currency,
        "funding_target_symbol": target,
        "funding_gap_native": gap,
        "estimated_release_native": native,
        "cumulative_release_native": native if cumulative is None else cumulative,
        "covers_funding_target": covers,
    })
    return row


_ACCOUNT_25 = {"holdings_count": 25, "holdings_items": []}
_WEAK_ROWS = [
    _plan_row("015760.KS", "한국전력"),
    _plan_row("035420.KS", "NAVER", weakness=12.0),
    _plan_row("035720.KS", "카카오", weakness=9.0),
    _plan_row("000270.KS", "기아", weakness=5.0),
]


class TestSellToFundCandidates(unittest.TestCase):
    def _eval(self, rows=None, account=None, required=True, policy=None,
              attempted=None, now=_NOW, **env):
        # _NOW = 목 11:00 KST → 한국장 열림 / 미국장 닫힘 (실제 market_hours 사용)
        with _rebalance_env(**env):
            return tpr.evaluate_sell_to_fund_candidates(
                account_summary=account if account is not None else dict(_ACCOUNT_25),
                rebalance_plan=_plan(rows if rows is not None else _WEAK_ROWS, required),
                policy=policy,
                attempted_map=attempted,
                now=now,
            )

    def test_returns_single_weakest_holding_by_default(self):
        out = self._eval()
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["symbol"], "015760.KS")
        self.assertEqual(c["action"], "sell_to_fund")
        self.assertEqual(c["quantity"], 6)
        self.assertEqual(c["held_quantity"], 6)
        self.assertEqual(c["pl_pct"], -6.55)
        self.assertEqual(c["process_reason"], "income_rebalance_sell_to_fund")
        self.assertEqual(c["review_reason"], "income_rebalance_sell_to_fund")
        self.assertEqual(c["estimated_release_krw"], 212700.0)
        self.assertEqual(c["weakness_score"], 16.69)

    def test_max_sells_per_run_two(self):
        out = self._eval(TOSS_REBALANCE_MAX_SELLS_PER_RUN=2)
        self.assertEqual([c["symbol"] for c in out], ["015760.KS", "035420.KS"])

    def test_max_sells_per_run_clamped_to_three(self):
        out = self._eval(TOSS_REBALANCE_MAX_SELLS_PER_RUN=9,
                         TOSS_REBALANCE_MAX_SELLS_PER_DAY=5)
        self.assertEqual(len(out), 3)

    def test_zero_max_sells_per_run_disables(self):
        self.assertEqual(self._eval(TOSS_REBALANCE_MAX_SELLS_PER_RUN=0), [])

    def test_holdings_at_or_below_min_returns_nothing(self):
        self.assertEqual(self._eval(account={"holdings_count": 20, "holdings_items": []}), [])

    def test_rebalance_not_required_returns_nothing(self):
        self.assertEqual(self._eval(required=False), [])

    def test_daily_cap_reached_returns_nothing(self):
        attempted = {
            "111111.KS": {"action": "sell_to_fund"},
            "222222.KS": {"process_reason": "income_rebalance_sell_to_fund"},
        }
        self.assertEqual(
            self._eval(attempted=attempted, TOSS_REBALANCE_MAX_SELLS_PER_DAY=2), [])

    def test_daily_remaining_caps_per_run(self):
        attempted = {"111111.KS": {"action": "sell_to_fund"}}
        out = self._eval(attempted=attempted, TOSS_REBALANCE_MAX_SELLS_PER_RUN=3,
                         TOSS_REBALANCE_MAX_SELLS_PER_DAY=2)
        self.assertEqual(len(out), 1)

    def test_risk_sell_attempts_do_not_consume_daily_cap(self):
        attempted = {"111111.KS": {"action": "stop_loss",
                                   "process_reason": "position_review_sell"}}
        self.assertEqual(len(self._eval(attempted=attempted)), 1)

    def test_already_attempted_symbol_excluded(self):
        out = self._eval(attempted={"015760.KS": {"action": "stop_loss"}})
        self.assertEqual(out[0]["symbol"], "035420.KS")

    def test_already_attempted_bare_code_excluded(self):
        out = self._eval(attempted={"015760": {"action": "stop_loss"}})
        self.assertEqual(out[0]["symbol"], "035420.KS")

    def test_mu_is_protected_by_default(self):
        out = self._eval(rows=[_plan_row("MU", "마이크론"), _plan_row("035420.KS", "NAVER")])
        self.assertEqual([c["symbol"] for c in out], ["035420.KS"])

    def test_env_protected_symbols(self):
        out = self._eval(TOSS_REBALANCE_PROTECTED_SYMBOLS="015760.KS, 035420")
        self.assertEqual(out[0]["symbol"], "035720.KS")

    def test_policy_preferred_symbols_protected(self):
        out = self._eval(rows=[_plan_row("069500.KS", "KODEX200"), _plan_row("035420.KS", "NAVER")],
                         policy={"preferred_symbols": ["069500.KS"]})
        self.assertEqual([c["symbol"] for c in out], ["035420.KS"])

    def test_zero_quantity_or_price_rows_skipped(self):
        out = self._eval(rows=[_plan_row("015760.KS", qty=0), _plan_row("035420.KS", last=0),
                               _plan_row("035720.KS", "카카오")])
        self.assertEqual([c["symbol"] for c in out], ["035720.KS"])

    def test_bare_code_row_normalized_to_ks(self):
        out = self._eval(rows=[_plan_row("015760")])
        self.assertEqual(out[0]["symbol"], "015760.KS")

    def test_no_network_fetch_when_inputs_provided(self):
        import core.dashboard_data as dd
        with patch.object(dd, "toss_account_summary") as acct, \
             patch.object(dd, "toss_buy_candidates_data") as buys:
            self._eval()
        acct.assert_not_called()
        buys.assert_not_called()

    # ── AI Berkshire eligibility (fail-closed) ───────────────────

    def test_hold_row_not_auto_sell_candidate(self):
        out = self._eval(rows=[
            _plan_row("035420.KS", "NAVER", eligible=False, classification="hold"),
            _plan_row("015760.KS", "한국전력"),
        ])
        self.assertEqual([c["symbol"] for c in out], ["015760.KS"])

    def test_row_without_eligibility_field_fail_closed(self):
        legacy = _plan_row("015760.KS")
        for key in ("ai_berkshire", "sell_to_fund_adjustment",
                    "adjusted_sell_priority", "auto_sell_eligible",
                    "auto_sell_block_reason"):
            legacy.pop(key, None)
        self.assertEqual(self._eval(rows=[legacy]), [])

    def test_all_hold_rows_returns_nothing(self):
        rows = [_plan_row(s, eligible=False, classification="hold")
                for s in ("035420.KS", "035720.KS", "068270.KS")]
        self.assertEqual(self._eval(rows=rows), [])

    def test_candidates_sorted_by_adjusted_priority(self):
        out = self._eval(rows=[
            _plan_row("015760.KS", "한국전력", weakness=9.8, adjustment=0.0),
            _plan_row("024110.KS", "기업은행", weakness=5.9, adjustment=5.0),
        ], TOSS_REBALANCE_MAX_SELLS_PER_RUN=2)
        # 기업은행 adjusted 10.9 > 한국전력 9.8
        self.assertEqual([c["symbol"] for c in out], ["024110.KS", "015760.KS"])
        self.assertEqual(out[0]["adjusted_sell_priority"], 10.9)
        self.assertEqual(out[0]["ai_berkshire_classification"], "sell_to_fund")

    # ── 시장 slot — 닫힌 시장 후보가 per_run slot을 점유하지 않음 ─

    def test_kr_open_closed_us_top_candidate_does_not_occupy_slot(self):
        # 우선순위 1위가 US 종목(ABBV)이어도 KR장 시간엔 KR 후보가 선택돼야 한다
        out = self._eval(rows=[
            _plan_row("ABBV", "AbbVie", weakness=20.0, currency="USD"),
            _plan_row("015760.KS", "한국전력", weakness=9.8),
        ], now=datetime(2026, 7, 2, 11, 0, tzinfo=KST))
        self.assertEqual([c["symbol"] for c in out], ["015760.KS"])

    def test_us_open_kr_candidate_does_not_occupy_slot(self):
        # 미국장 시간(KST 23:30)엔 KR 후보가 아니라 US trim 후보가 선택된다
        out = self._eval(rows=[
            _plan_row("015760.KS", "한국전력", weakness=20.0),
            _plan_row("XOM", "Exxon Mobil", weakness=8.0, adjustment=1.0,
                      classification="trim"),
        ], now=datetime(2026, 7, 2, 23, 30, tzinfo=KST))
        self.assertEqual([c["symbol"] for c in out], ["XOM"])

    def test_us_open_hold_abbv_excluded_trim_xom_selected(self):
        out = self._eval(rows=[
            _plan_row("ABBV", "AbbVie", weakness=20.0, eligible=False,
                      classification="hold"),
            _plan_row("XOM", "Exxon Mobil", weakness=8.0, adjustment=1.0,
                      classification="trim"),
        ], now=datetime(2026, 7, 2, 23, 30, tzinfo=KST))
        self.assertEqual([c["symbol"] for c in out], ["XOM"])

    def test_all_markets_closed_returns_nothing(self):
        out = self._eval(now=datetime(2026, 7, 2, 6, 0, tzinfo=KST))
        self.assertEqual(out, [])

    # ── 통화별 income 자금조달 (funding) 모드 ────────────────────

    _US_OPEN = datetime(2026, 7, 2, 23, 30, tzinfo=KST)

    def _eval_funding(self, rows, count=19, required=False, currency="USD",
                      now=None, **kw):
        return self._eval(
            rows=rows, required=required,
            account={"holdings_count": count, "holdings_items": []},
            now=now or self._US_OPEN, **kw)

    def test_funding_allows_candidates_below_min_holdings(self):
        plan = _plan([_funding_row("XOM")], required=False,
                     funding=True, funding_currency="USD", funding_target="MS")
        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, now=self._US_OPEN)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["symbol"], "XOM")
        self.assertEqual(c["action"], "sell_to_fund")
        self.assertEqual(c["funding_mode"], "currency_income_replacement")
        self.assertEqual(c["funding_target_symbol"], "MS")
        self.assertEqual(c["funding_currency"], "USD")

    def test_funding_owner_rejects_corrupt_plan_contracts(self):
        cases = [
            "flag_string", "target_missing", "side_missing", "legacy_missing",
            "partial_contract", "metric_string", "metric_nan", "metric_inf",
            "explicit_sell", "uppercase_buy", "whitespace_buy", "target_dict_subclass",
            "row_boolean_string", "row_target_mismatch",
            "row_side_buy", "target_symbol_non_string", "row_symbol_non_string",
            "row_near_overflow_price", "source_symbol_mismatch",
        ]
        for case in cases:
            with self.subTest(case=case):
                plan = _plan([_funding_row("XOM")], required=False,
                             funding=True, funding_currency="USD", funding_target="MS")
                target = plan["funding_target"]
                self.assertIsInstance(target, dict)
                if case == "flag_string":
                    plan["funding_rebalance_required"] = "true"
                elif case == "target_missing":
                    plan["funding_target"] = None
                elif case == "side_missing":
                    target.pop("side")
                elif case == "legacy_missing":
                    for key in list(target):
                        if key.startswith("decision_"):
                            target.pop(key)
                elif case == "partial_contract":
                    target.pop("decision_expected_pnl_scope")
                elif case == "metric_string":
                    target["decision_expected_pnl_krw"] = "10000"
                elif case == "metric_nan":
                    target["decision_expected_pnl_krw"] = float("nan")
                elif case == "metric_inf":
                    target["decision_income_edge_ratio"] = float("inf")
                elif case == "explicit_sell":
                    target["side"] = "sell"
                elif case == "uppercase_buy":
                    target["side"] = "BUY"
                elif case == "whitespace_buy":
                    target["side"] = " buy "
                elif case == "target_dict_subclass":
                    plan["funding_target"] = type("TargetDict", (dict,), {})(target)
                elif case == "row_boolean_string":
                    plan["sell_to_fund_candidates"][0]["covers_funding_target"] = "true"
                elif case == "row_target_mismatch":
                    plan["sell_to_fund_candidates"][0]["funding_target_symbol"] = "NVDA"
                elif case == "row_side_buy":
                    plan["sell_to_fund_candidates"][0]["side"] = "buy"
                elif case == "target_symbol_non_string":
                    target["symbol"] = 123
                    plan["sell_to_fund_candidates"][0]["funding_target_symbol"] = 123
                elif case == "row_symbol_non_string":
                    plan["sell_to_fund_candidates"][0]["symbol"] = ["XOM"]
                elif case == "row_near_overflow_price":
                    row = plan["sell_to_fund_candidates"][0]
                    row["last_price"] = 1e308
                    row["estimated_release_native"] = 1e308
                    row["cumulative_release_native"] = 1e308
                elif case == "source_symbol_mismatch":
                    plan["funding_source_symbol"] = "CVX"

                with _rebalance_env():
                    out = tpr.evaluate_sell_to_fund_candidates(
                        account_summary={"holdings_count": 19, "holdings_items": []},
                        rebalance_plan=plan, now=self._US_OPEN)
                self.assertEqual(out, [])

    def test_funding_owner_rejects_cumulative_multi_row_without_single_cover(self):
        rows = [
            _funding_row("XOM", gap=299.0, native=200.0,
                         cumulative=200.0, covers=False),
            _funding_row("CVX", gap=299.0, native=150.0,
                         cumulative=350.0, covers=True),
        ]
        plan = _plan(rows, required=False, funding=True,
                     funding_currency="USD", funding_target="MS")

        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, now=self._US_OPEN)

        self.assertEqual(out, [])

    def test_funding_owner_rejects_missing_currency_contract(self):
        row = _funding_row("XOM")
        row["currency"] = ""
        row["funding_currency"] = ""
        plan = _plan([row], required=False, funding=True,
                     funding_currency="", funding_target="MS")
        plan["funding_target"]["currency"] = ""

        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, now=self._US_OPEN)

        self.assertEqual(out, [])

    def test_funding_owner_rejects_declared_release_above_actual_sale_value(self):
        row = _funding_row(
            "XOM", gap=200.0, native=300.0, cumulative=300.0,
        )
        row["quantity"] = 1
        row["last_price"] = 100.0
        plan = _plan([row], required=False, funding=True,
                     funding_currency="USD", funding_target="MS")

        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, now=self._US_OPEN)

        self.assertEqual(out, [])

    def test_funding_owner_requires_exactly_one_sufficient_row(self):
        plan = _plan(
            [_funding_row("XOM"), _funding_row("CVX")],
            required=False, funding=True,
            funding_currency="USD", funding_target="MS",
        )

        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, now=self._US_OPEN)

        self.assertEqual(out, [])

    def test_no_funding_and_below_min_holdings_returns_nothing(self):
        plan = _plan([_plan_row("XOM", currency="USD")], required=False, funding=False)
        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, now=self._US_OPEN)
        self.assertEqual(out, [])

    def test_funding_mode_excludes_other_currency_rows(self):
        rows = [
            _plan_row("015760.KS", "한국전력", weakness=20.0),  # KR, funding 필드 없음
            _funding_row("XOM", weakness=8.0),
        ]
        plan = _plan(rows, required=False, funding=True,
                     funding_currency="USD", funding_target="MS")
        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, now=self._US_OPEN)
        self.assertEqual([c["symbol"] for c in out], ["XOM"])

    def test_funding_mode_excludes_hold_and_unscored_rows(self):
        hold_row = _funding_row("ABBV", weakness=20.0)
        hold_row["auto_sell_eligible"] = False
        hold_row["auto_sell_block_reason"] = "ai_berkshire_hold"
        unscored = _funding_row("MCD", weakness=15.0)
        unscored["auto_sell_eligible"] = False
        plan = _plan([hold_row, unscored, _funding_row("XOM", weakness=8.0)],
                     required=False, funding=True, funding_currency="USD",
                     funding_target="MS")
        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, now=self._US_OPEN)
        self.assertEqual([c["symbol"] for c in out], ["XOM"])

    def test_funding_mode_us_closed_returns_nothing(self):
        plan = _plan([_funding_row("XOM")], required=False, funding=True,
                     funding_currency="USD", funding_target="MS")
        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan,
                now=datetime(2026, 7, 2, 11, 0, tzinfo=KST))  # KR장만 열림
        self.assertEqual(out, [])

    def test_funding_mode_respects_daily_cap_with_single_row(self):
        rows = [_funding_row("XOM")]
        plan = _plan(rows, required=False, funding=True,
                     funding_currency="USD", funding_target="MS")
        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, now=self._US_OPEN)
        self.assertEqual(len(out), 1)  # 유효한 단일 funding row
        attempted = {
            "111111.KS": {"action": "sell_to_fund"},
            "222222.KS": {"process_reason": "income_rebalance_sell_to_fund"},
        }
        with _rebalance_env():
            out2 = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, attempted_map=attempted, now=self._US_OPEN)
        self.assertEqual(out2, [])  # per_day=2 소진

    def test_funding_mode_stop_loss_attempts_do_not_consume_cap(self):
        plan = _plan([_funding_row("XOM")], required=False, funding=True,
                     funding_currency="USD", funding_target="MS")
        attempted = {"005490.KS": {"action": "stop_loss",
                                   "process_reason": "position_review_sell"}}
        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, attempted_map=attempted, now=self._US_OPEN)
        self.assertEqual(len(out), 1)

    def test_funding_mode_protected_symbol_excluded(self):
        plan = _plan([_funding_row("MU")], required=False, funding=True,
                     funding_currency="USD", funding_target="MS")
        with _rebalance_env():
            out = tpr.evaluate_sell_to_fund_candidates(
                account_summary={"holdings_count": 19, "holdings_items": []},
                rebalance_plan=plan, now=self._US_OPEN)
        self.assertEqual(out, [])


# ── 3. execute_sell_candidates ───────────────────────────────────

_CAND = {
    "symbol": "123450.KS", "name": "테스트종목", "pl_pct": -9.0,
    "action": "stop_loss", "quantity": 10, "held_quantity": 10,
    "last_price": 9100, "currency": "KRW",
}

_STF_CAND = {
    "symbol": "015760.KS", "name": "한국전력", "pl_pct": -6.55,
    "action": "sell_to_fund", "quantity": 6, "held_quantity": 6,
    "last_price": 35450.0, "currency": "KRW",
    "review_reason": "income_rebalance_sell_to_fund",
    "process_reason": "income_rebalance_sell_to_fund",
    "estimated_release_krw": 212700.0, "weakness_score": 16.69,
}


class TestExecuteSell(unittest.TestCase):
    def _exec(self, policy=None, attempted=None, market_open=True,
              process_result=None, candidate=None):
        policy = policy if policy is not None else dict(_POLICY_SELL)
        attempted = attempted if attempted is not None else {}
        candidate = dict(candidate if candidate is not None else _CAND)
        process_result = process_result or {
            "symbol": candidate["symbol"], "stage": "verdict_recorded", "verdict": "PASS",
        }
        with patch.object(tpr, "_market_open_for_symbol", return_value=market_open), \
             patch("core.toss_autonomous_pipeline.process_candidate",
                   return_value=process_result) as mock_pc:
            results = tpr.execute_sell_candidates(
                [candidate], policy, _NOW, attempted)
        return results, mock_pc, attempted

    def test_executes_sell(self):
        results, mock_pc, attempted = self._exec()
        self.assertEqual(results[0]["verdict"], "PASS")
        self.assertEqual(results[0]["action"], "stop_loss")
        cand = mock_pc.call_args[0][0]
        self.assertEqual(cand["side"], "sell")
        self.assertEqual(cand["quantity"], 10)
        self.assertEqual(mock_pc.call_args.kwargs.get("reason"),
                         "position_review_sell")
        self.assertIn("123450.KS", attempted)

    def test_autonomous_off_skips(self):
        policy = dict(_POLICY_SELL, autonomous_mode=False)
        results, mock_pc, _ = self._exec(policy=policy)
        self.assertEqual(results[0]["reason"], "autonomous_mode_disabled")
        mock_pc.assert_not_called()

    def test_execution_policy_requires_exact_typed_authority(self):
        cases = [
            dict(_POLICY_SELL, autonomous_mode="true"),
            dict(_POLICY_SELL, autonomous_mode=1),
            dict(_POLICY_SELL, autonomous_kill_switch="false"),
            dict(_POLICY_SELL, autonomous_kill_switch=0),
            dict(_POLICY_SELL, mode="approval_only_live_pilot"),
            dict(_POLICY_SELL, requires_user_confirmation=True),
            dict(_POLICY_SELL, requires_user_confirmation="false"),
            dict(_POLICY_SELL, requires_second_confirmation=True),
            dict(_POLICY_SELL, live_order_allowed=False),
            dict(_POLICY_SELL, all_live_gates_open=False),
            dict(_POLICY_SELL, adapter_status="disabled"),
            dict(_POLICY_SELL, live_transport_status="not_configured"),
            dict(_POLICY_SELL, autonomous_allowed_sides="sell"),
            dict(_POLICY_SELL, autonomous_allowed_sides=["sell", 1]),
            dict(_POLICY_SELL, autonomous_allowed_sides=["sell", "hold"]),
            dict(_POLICY_SELL, autonomous_allowed_sides=["sell", ""]),
        ]
        for policy in cases:
            with self.subTest(policy=policy):
                _, mock_pc, _ = self._exec(policy=policy)
                mock_pc.assert_not_called()

    def test_kill_switch_skips(self):
        policy = dict(_POLICY_SELL, autonomous_kill_switch=True)
        results, _, _ = self._exec(policy=policy)
        self.assertEqual(results[0]["reason"], "kill_switch_active")

    def test_sell_env_off_skips(self):
        policy = dict(_POLICY_SELL, autonomous_allowed_sides=["buy"])
        results, mock_pc, _ = self._exec(policy=policy)
        self.assertEqual(results[0]["reason"], "sell_not_allowed_by_env")
        mock_pc.assert_not_called()

    def test_market_closed_skips(self):
        results, _, _ = self._exec(market_open=False)
        self.assertEqual(results[0]["reason"], "market_closed")

    def test_already_attempted_dedup(self):
        results, mock_pc, _ = self._exec(attempted={"123450.KS": {"at": "10:00"}})
        self.assertEqual(results[0]["reason"], "already_attempted_today")
        mock_pc.assert_not_called()

    def test_sell_to_fund_uses_rebalance_reason_and_note(self):
        results, mock_pc, attempted = self._exec(candidate=_STF_CAND)
        self.assertEqual(results[0]["verdict"], "PASS")
        self.assertEqual(mock_pc.call_args.kwargs.get("reason"),
                         "income_rebalance_sell_to_fund")
        cand = mock_pc.call_args[0][0]
        self.assertEqual(cand["side"], "sell")
        self.assertEqual(cand["quantity"], 6)
        note = mock_pc.call_args.kwargs.get("note")
        self.assertIn("review_action=sell_to_fund", note)
        self.assertIn("pl_pct=-6.55", note)
        self.assertIn("qty=6/6", note)
        self.assertIn("review_reason=income_rebalance_sell_to_fund", note)
        self.assertIn("estimated_release_krw=212700.0", note)
        self.assertIn("weakness_score=16.69", note)
        self.assertEqual(attempted["015760.KS"]["action"], "sell_to_fund")
        self.assertEqual(attempted["015760.KS"]["process_reason"],
                         "income_rebalance_sell_to_fund")

    def test_risk_sell_records_position_review_reason(self):
        _, mock_pc, attempted = self._exec()
        self.assertEqual(mock_pc.call_args.kwargs.get("reason"), "position_review_sell")
        self.assertEqual(attempted["123450.KS"]["process_reason"], "position_review_sell")

    def test_sell_to_fund_skipped_when_autonomous_off(self):
        policy = dict(_POLICY_SELL, autonomous_mode=False)
        results, mock_pc, _ = self._exec(policy=policy, candidate=_STF_CAND)
        self.assertEqual(results[0]["reason"], "autonomous_mode_disabled")
        mock_pc.assert_not_called()

    def test_sell_to_fund_skipped_when_kill_switch(self):
        policy = dict(_POLICY_SELL, autonomous_kill_switch=True)
        results, mock_pc, _ = self._exec(policy=policy, candidate=_STF_CAND)
        self.assertEqual(results[0]["reason"], "kill_switch_active")
        mock_pc.assert_not_called()

    def test_sell_to_fund_skipped_when_sell_not_allowed(self):
        policy = dict(_POLICY_SELL, autonomous_allowed_sides=["buy"])
        results, mock_pc, _ = self._exec(policy=policy, candidate=_STF_CAND)
        self.assertEqual(results[0]["reason"], "sell_not_allowed_by_env")
        mock_pc.assert_not_called()

    def test_sell_to_fund_skipped_when_market_closed(self):
        results, mock_pc, _ = self._exec(candidate=_STF_CAND, market_open=False)
        self.assertEqual(results[0]["reason"], "market_closed")
        mock_pc.assert_not_called()


# ── 4. run_toss_position_review ──────────────────────────────────

class TestRunReview(unittest.TestCase):
    def _run(self, tmp, now=_NOW, force=False, candidates=None, state=None,
             sell_to_fund=None, capture=None):
        candidates = candidates if candidates is not None else [dict(_CAND)]
        state_path = Path(tmp) / "state.json"
        if state is not None:
            state_path.write_text(json.dumps(state), encoding="utf-8")

        def _fake_execute(cands, policy, now_, attempted):
            if capture is not None:
                capture.extend(cands)
            return [{"symbol": c["symbol"], "stage": "verdict_recorded",
                     "verdict": "PASS"} for c in cands]

        with patch.object(tpr, "_state_path", return_value=state_path), \
             patch.object(tpr, "evaluate_holdings", return_value=list(candidates)), \
             patch.object(tpr, "evaluate_sell_to_fund_candidates",
                          return_value=list(sell_to_fund or [])), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=dict(_POLICY_SELL)), \
             patch.object(tpr, "execute_sell_candidates", side_effect=_fake_execute), \
             patch("core.telegram.send_simple_message", return_value=True):
            return tpr.run_toss_position_review(now=now, force=force)

    def test_risk_candidates_are_processed_before_sell_to_fund(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = []
            r = self._run(tmp, sell_to_fund=[dict(_STF_CAND)], capture=captured)
            self.assertEqual([c["action"] for c in captured],
                             ["stop_loss", "sell_to_fund"])
            self.assertEqual(r["candidate_count"], 2)

    def test_sell_to_fund_runs_when_no_risk_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = []
            r = self._run(tmp, candidates=[], sell_to_fund=[dict(_STF_CAND)],
                          capture=captured)
            self.assertEqual([c["symbol"] for c in captured], ["015760.KS"])
            self.assertEqual(r["candidate_count"], 1)
            self.assertTrue(r["sent"])

    def test_sell_to_fund_duplicate_of_risk_candidate_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = []
            dup = dict(_STF_CAND, symbol="123450.KS")
            self._run(tmp, sell_to_fund=[dup], capture=captured)
            self.assertEqual([c["action"] for c in captured], ["stop_loss"])

    def test_runs_and_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp)
            self.assertTrue(r["reviewed"])
            self.assertEqual(r["candidate_count"], 1)
            self.assertTrue(r["sent"])

    def test_immediate_second_review_is_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp)
            self.assertEqual(r2.get("skipped"), "throttled")

    def test_same_day_after_interval_reevaluates(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp, now=_NOW + timedelta(minutes=31))
            self.assertTrue(r2.get("reviewed"))

    def test_next_day_runs_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp, now=_NOW + timedelta(days=1))
            self.assertTrue(r2.get("reviewed"))

    def test_before_hour_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, now=datetime(2026, 7, 2, 9, 30, tzinfo=KST))
            self.assertEqual(r.get("skipped"), "before_review_hour")

    def test_weekend_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, now=datetime(2026, 7, 4, 11, 0, tzinfo=KST))
            self.assertEqual(r.get("skipped"), "weekend")

    def test_no_candidates_no_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, candidates=[])
            self.assertTrue(r["reviewed"])
            self.assertEqual(r["candidate_count"], 0)
            self.assertFalse(r["sent"])


# ── 5. 메시지 ────────────────────────────────────────────────────

class TestMessage(unittest.TestCase):
    def test_message_sell_to_fund(self):
        msg = tpr._format_review_message(
            [dict(_STF_CAND)],
            [{"symbol": "015760.KS", "stage": "verdict_recorded", "verdict": "PASS"}],
        )
        self.assertIn("리밸런싱 매도 후보", msg)
        self.assertIn("자동 매도 발동 (리밸런싱 매도 6주, 검증 PASS)", msg)
        self.assertNotIn("손절 기준 도달", msg)

    def test_message_pass(self):
        msg = tpr._format_review_message(
            [dict(_CAND)],
            [{"symbol": "123450.KS", "stage": "verdict_recorded", "verdict": "PASS"}],
        )
        self.assertIn("손절 기준 도달", msg)
        self.assertIn("자동 매도 발동", msg)
        self.assertIn("-9.0%", msg)

    def test_message_skipped(self):
        msg = tpr._format_review_message(
            [dict(_CAND)],
            [{"symbol": "123450.KS", "stage": "skipped",
              "reason": "sell_not_allowed_by_env"}],
        )
        self.assertIn("자동 매도 스킵: sell_not_allowed_by_env", msg)


if __name__ == "__main__":
    unittest.main()
