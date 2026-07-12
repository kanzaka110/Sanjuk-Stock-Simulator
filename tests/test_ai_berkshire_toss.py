"""tests/test_ai_berkshire_toss.py

AI Berkshire read-only score layer 테스트.

1. score 파일 로드 / 심볼 정규화
2. hold / protect / gray_zone 자동매도 제외 (fail-closed)
3. unscored / 파일 없음 / 깨진 파일 fail-closed
4. sell_to_fund / trim / avoid 자동매도 허용
5. adjusted_sell_priority 정렬
6. hard stop 후보는 이 레이어와 무관하게 유지 (position review 경로)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.ai_berkshire_toss as abt  # noqa: E402


# 테스트 결정성: valid_until은 먼 미래, 만료 검증은 as_of_date 주입으로만
_FRESH = {"as_of": "2026-07-10", "valid_until": "2099-12-31",
          "thesis": "test thesis", "red_lines": ["test red line"],
          "source_urls": ["https://example.com/ir"]}


def _item(classification, adjustment=0.0, **overrides):
    base = {"classification": classification,
            "sell_to_fund_adjustment": adjustment, **_FRESH}
    base.update(overrides)
    return base


_SCORES = {
    "version": "ai_berkshire_toss_v2",
    "read_only": True,
    "items": {
        "015760.KS": _item("sell_to_fund", 0.0, name="한국전력"),
        "XOM": _item("trim", 1.0, name="Exxon Mobil"),
        "ABBV": _item("hold", -3.5, name="AbbVie"),
        "035420.KS": _item("hold", -3.5, name="NAVER"),
        "069500.KS": _item("protect", -5.0, name="KODEX200"),
        "005930.KS": _item("gray_zone", 0.0, name="삼성전자"),
        "000660.KS": _item("avoid", 0.5, name="SK하이닉스"),
    },
}


def _row(symbol, weakness=10.0, **kw):
    base = {
        "symbol": symbol, "name": symbol, "quantity": 2, "last_price": 100.0,
        "currency": "KRW", "estimated_release_krw": 200.0,
        "pl_pct": -5.0, "weakness_score": weakness,
        "action": "sell_to_fund_candidate", "read_only": True,
    }
    base.update(kw)
    return base


# ── 1. 로드/정규화 ────────────────────────────────────────────────

class TestLoadAndLookup(unittest.TestCase):
    def test_load_scores_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "scores.json"
            p.write_text(json.dumps(_SCORES), encoding="utf-8")
            data = abt.load_ai_berkshire_scores(p)
        self.assertEqual(data["version"], "ai_berkshire_toss_v2")
        self.assertIn("ABBV", data["items"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(abt.load_ai_berkshire_scores("/nonexistent/x.json"), {})

    def test_broken_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "broken.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(abt.load_ai_berkshire_scores(p), {})

    def test_malformed_items_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(json.dumps({"items": ["not", "a", "dict"]}), encoding="utf-8")
            self.assertEqual(abt.load_ai_berkshire_scores(p), {})

    def test_score_for_symbol_direct_hit(self):
        item = abt.score_for_symbol("ABBV", _SCORES)
        self.assertEqual(item["classification"], "hold")
        self.assertEqual(item["sell_to_fund_adjustment"], -3.5)

    def test_score_for_symbol_bare_code_matches_ks_key(self):
        item = abt.score_for_symbol("015760", _SCORES)
        self.assertEqual(item["classification"], "sell_to_fund")

    def test_score_for_symbol_ks_matches_bare_key(self):
        scores = {"items": {"015760": _item("trim")}}
        item = abt.score_for_symbol("015760.KS", scores)
        self.assertEqual(item["classification"], "trim")

    def test_score_for_symbol_unknown_returns_none(self):
        self.assertIsNone(abt.score_for_symbol("ZZZZ", _SCORES))

    def test_compute_berkshire_score_clamped(self):
        self.assertEqual(abt.compute_berkshire_score(
            {"classification": "protect", "sell_to_fund_adjustment": -9.0}), 0.0)
        self.assertEqual(abt.compute_berkshire_score(
            {"classification": "sell_to_fund", "sell_to_fund_adjustment": 9.0}), 10.0)


# ── 2. eligibility (fail-closed) ─────────────────────────────────

class TestEligibility(unittest.TestCase):
    def _apply(self, rows, scores=_SCORES):
        return abt.apply_berkshire_to_sell_to_fund(rows, scores=scores)

    def test_sell_to_fund_and_trim_and_avoid_allowed(self):
        out = self._apply([_row("015760.KS"), _row("XOM"), _row("000660.KS")])
        self.assertTrue(all(r["auto_sell_eligible"] for r in out))
        self.assertTrue(all(r["auto_sell_block_reason"] is None for r in out))

    def test_hold_excluded(self):
        out = self._apply([_row("ABBV")])
        self.assertFalse(out[0]["auto_sell_eligible"])
        self.assertEqual(out[0]["auto_sell_block_reason"], "ai_berkshire_hold")
        self.assertEqual(out[0]["ai_berkshire"]["classification"], "hold")

    def test_protect_excluded(self):
        out = self._apply([_row("069500.KS")])
        self.assertFalse(out[0]["auto_sell_eligible"])
        self.assertEqual(out[0]["auto_sell_block_reason"], "ai_berkshire_protect")

    def test_gray_zone_excluded(self):
        out = self._apply([_row("005930.KS")])
        self.assertFalse(out[0]["auto_sell_eligible"])
        self.assertEqual(out[0]["auto_sell_block_reason"], "ai_berkshire_gray_zone")

    def test_unscored_fail_closed(self):
        out = self._apply([_row("ZZZZ")])
        self.assertFalse(out[0]["auto_sell_eligible"])
        self.assertEqual(out[0]["auto_sell_block_reason"], "ai_berkshire_unscored")
        self.assertIsNone(out[0]["ai_berkshire"]["classification"])

    def test_empty_scores_fail_closed(self):
        for scores in ({}, {"items": {}}, None):
            with patch.object(abt, "load_ai_berkshire_scores", return_value={}):
                out = abt.apply_berkshire_to_sell_to_fund([_row("015760.KS")], scores=scores)
            self.assertFalse(out[0]["auto_sell_eligible"], f"scores={scores!r}")

    def test_input_rows_not_mutated(self):
        row = _row("ABBV")
        before = dict(row)
        self._apply([row])
        self.assertEqual(row, before)


# ── 2.5 thesis freshness (만료/근거 누락 강등) ───────────────────

class TestThesisFreshness(unittest.TestCase):
    def _norm(self, as_of_date, **overrides):
        raw = _item("trim", 1.0)
        raw["valid_until"] = "2026-10-10"
        raw.update(overrides)
        return abt.normalize_ai_berkshire_item(raw, as_of_date=as_of_date)

    def test_before_valid_until_keeps_stored_classification(self):
        item = self._norm("2026-07-15")
        self.assertEqual(item["classification"], "trim")
        self.assertFalse(item["thesis_expired"])

    def test_on_valid_until_day_still_valid(self):
        item = self._norm("2026-10-10")
        self.assertEqual(item["classification"], "trim")
        self.assertFalse(item["thesis_expired"])

    def test_after_valid_until_downgrades_to_gray_zone(self):
        item = self._norm("2026-10-11")
        self.assertEqual(item["classification"], "gray_zone")
        self.assertTrue(item["thesis_expired"])

    def test_stored_and_effective_classification_are_separate(self):
        item = self._norm("2026-10-11")
        self.assertEqual(item["stored_classification"], "trim")
        self.assertEqual(item["classification"], "gray_zone")

    def test_invalid_date_format_downgrades(self):
        item = self._norm("2026-07-15", valid_until="10/10/2026")
        self.assertEqual(item["classification"], "gray_zone")
        self.assertFalse(item["thesis_expired"])  # 만료가 아니라 근거 불량

    def test_missing_valid_until_downgrades(self):
        raw = _item("trim", 1.0)
        raw.pop("valid_until")
        item = abt.normalize_ai_berkshire_item(raw, as_of_date="2026-07-15")
        self.assertEqual(item["classification"], "gray_zone")

    def test_missing_or_empty_source_urls_downgrades(self):
        for urls in (None, [], ["", "  "]):
            raw = _item("trim", 1.0, valid_until="2099-12-31")
            raw["source_urls"] = urls
            item = abt.normalize_ai_berkshire_item(raw, as_of_date="2026-07-15")
            self.assertEqual(item["classification"], "gray_zone", f"urls={urls!r}")

    def test_expired_row_not_auto_sell_eligible_with_exact_reason(self):
        scores = {"items": {"XOM": _item("trim", 1.0, valid_until="2026-10-10")}}
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("XOM")], scores=scores, as_of_date="2026-10-11")
        self.assertFalse(out[0]["auto_sell_eligible"])
        self.assertEqual(out[0]["auto_sell_block_reason"], "ai_berkshire_thesis_expired")
        ab = out[0]["ai_berkshire"]
        self.assertEqual(ab["stored_classification"], "trim")
        self.assertEqual(ab["classification"], "gray_zone")
        self.assertTrue(ab["thesis_expired"])
        self.assertEqual(ab["valid_until"], "2026-10-10")

    def test_fresh_eligible_classes_still_allowed(self):
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("015760.KS"), _row("XOM"), _row("000660.KS")],
            scores=_SCORES, as_of_date="2026-07-15")
        self.assertTrue(all(r["auto_sell_eligible"] for r in out))

    def test_explicit_false_blocks_trim_auto_sell(self):
        scores = {"items": {
            "096770.KS": _item(
                "trim", 1.0, auto_sell_eligible=False,
                buy_checklist_status="fail",
            )
        }}
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("096770.KS")], scores=scores, as_of_date="2026-07-15")
        self.assertFalse(out[0]["auto_sell_eligible"])
        self.assertEqual(
            out[0]["auto_sell_block_reason"],
            "ai_berkshire_auto_sell_disabled",
        )
        self.assertIs(out[0]["ai_berkshire"]["score_auto_sell_eligible"], False)

    def test_explicit_true_does_not_override_hold_classification(self):
        scores = {"items": {
            "AAA": _item("hold", auto_sell_eligible=True)
        }}
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("AAA")], scores=scores, as_of_date="2026-07-15")
        self.assertFalse(out[0]["auto_sell_eligible"])
        self.assertEqual(out[0]["auto_sell_block_reason"], "ai_berkshire_hold")

    def test_string_false_is_not_an_authoritative_override(self):
        scores = {"items": {
            "AAA": _item("trim", auto_sell_eligible="false")
        }}
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("AAA")], scores=scores, as_of_date="2026-07-15")
        self.assertTrue(out[0]["auto_sell_eligible"])
        self.assertIsNone(out[0]["ai_berkshire"]["score_auto_sell_eligible"])

    def test_fresh_hold_and_protect_still_blocked(self):
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("ABBV"), _row("069500.KS")],
            scores=_SCORES, as_of_date="2026-07-15")
        self.assertFalse(any(r["auto_sell_eligible"] for r in out))
        reasons = {r["auto_sell_block_reason"] for r in out}
        self.assertEqual(reasons, {"ai_berkshire_hold", "ai_berkshire_protect"})

    def test_merged_row_exposes_thesis_fields(self):
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("ABBV")], scores=_SCORES, as_of_date="2026-07-15")
        ab = out[0]["ai_berkshire"]
        for key in ("stored_classification", "classification", "thesis_expired",
                    "freshness_valid", "freshness_issues",
                    "as_of", "valid_until", "thesis", "red_lines",
                    "confidence", "source_urls", "buy_checklist_status",
                    "score_auto_sell_eligible"):
            self.assertIn(key, ab)
        self.assertEqual(ab["source_urls"], ["https://example.com/ir"])

    # ── 필수조건 전항목 fail-closed (thesis_invalid) ─────────────

    def test_full_valid_item_keeps_classification_and_freshness_valid(self):
        item = self._norm("2026-07-15")
        self.assertTrue(item["freshness_valid"])
        self.assertEqual(item["freshness_issues"], [])
        self.assertEqual(item["classification"], "trim")

    def test_missing_as_of_downgrades(self):
        raw = _item("trim", 1.0)
        raw.pop("as_of")
        item = abt.normalize_ai_berkshire_item(raw, as_of_date="2026-07-15")
        self.assertEqual(item["classification"], "gray_zone")
        self.assertIn("missing_as_of", item["freshness_issues"])
        raw["as_of"] = ""
        item = abt.normalize_ai_berkshire_item(raw, as_of_date="2026-07-15")
        self.assertIn("missing_as_of", item["freshness_issues"])

    def test_invalid_as_of_format_downgrades(self):
        item = self._norm("2026-07-15", as_of="07/10/2026")
        self.assertEqual(item["classification"], "gray_zone")
        self.assertIn("invalid_as_of", item["freshness_issues"])

    def test_as_of_after_valid_until_downgrades(self):
        item = self._norm("2026-07-15", as_of="2026-11-01")  # valid_until=2026-10-10
        self.assertEqual(item["classification"], "gray_zone")
        self.assertIn("invalid_date_range", item["freshness_issues"])

    def test_missing_or_blank_thesis_downgrades(self):
        for thesis in (None, "", "   "):
            item = self._norm("2026-07-15", thesis=thesis)
            self.assertEqual(item["classification"], "gray_zone", f"thesis={thesis!r}")
            self.assertIn("missing_thesis", item["freshness_issues"])

    def test_missing_or_blank_red_lines_downgrades(self):
        for red_lines in (None, [], ["", "  "], "문자열은 목록 아님"):
            item = self._norm("2026-07-15", red_lines=red_lines)
            self.assertEqual(item["classification"], "gray_zone", f"red_lines={red_lines!r}")
            self.assertIn("missing_red_lines", item["freshness_issues"])

    def test_source_urls_as_string_is_invalid(self):
        item = self._norm("2026-07-15", source_urls="https://example.com/ir")
        self.assertEqual(item["classification"], "gray_zone")
        self.assertIn("invalid_source_urls", item["freshness_issues"])

    def test_source_urls_without_http_scheme_is_invalid(self):
        item = self._norm("2026-07-15", source_urls=["ftp://x", "example.com", "메모"])
        self.assertEqual(item["classification"], "gray_zone")
        self.assertIn("invalid_source_urls", item["freshness_issues"])

    def test_expired_vs_structural_block_reasons_are_distinct(self):
        expired_scores = {"items": {"XOM": _item("trim", 1.0)}}
        expired_scores["items"]["XOM"]["valid_until"] = "2026-10-10"
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("XOM")], scores=expired_scores, as_of_date="2026-10-11")
        self.assertEqual(out[0]["auto_sell_block_reason"], "ai_berkshire_thesis_expired")

        broken_scores = {"items": {"XOM": _item("trim", 1.0, thesis="")}}
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("XOM")], scores=broken_scores, as_of_date="2026-07-15")
        self.assertEqual(out[0]["auto_sell_block_reason"], "ai_berkshire_thesis_invalid")
        self.assertFalse(out[0]["ai_berkshire"]["thesis_expired"])

    def test_stored_classification_preserved_on_structural_failure(self):
        item = self._norm("2026-07-15", thesis="")
        self.assertEqual(item["stored_classification"], "trim")
        self.assertEqual(item["classification"], "gray_zone")

    _STAGING_SYMBOLS = ("000270.KS", "096770.KS", "207940.KS")

    def _skip_unless_staging_merged(self, data):
        """3종목 staging이 운영 score에 병합되기 전에는 skip (병합 시 자동 활성화)."""
        items = data.get("items") or {}
        missing = [s for s in self._STAGING_SYMBOLS if s not in items]
        if missing:
            self.skipTest(f"staging 미병합 종목 {missing} — 운영 score 병합 후 활성화")

    def test_repo_scores_json_all_eleven_symbols_freshness_valid(self):
        data = abt.load_ai_berkshire_scores()
        self._skip_unless_staging_merged(data)
        self.assertEqual(len(data.get("items") or {}), 11)
        for sym in data["items"]:
            item = abt.score_for_symbol(sym, data, as_of_date="2026-07-11")
            self.assertTrue(item["freshness_valid"],
                            f"{sym}: {item['freshness_issues']}")

    def test_repo_new_staging_items_keep_buy_and_sell_authority_separate(self):
        data = abt.load_ai_berkshire_scores()
        self._skip_unless_staging_merged(data)
        expected = {
            "000270.KS": ("hold", "gray_zone"),
            "096770.KS": ("trim", "fail"),
            "207940.KS": ("hold", "fail"),
        }
        for symbol, (classification, checklist) in expected.items():
            item = abt.score_for_symbol(symbol, data, as_of_date="2026-07-11")
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(item["classification"], classification)
            self.assertEqual(item["buy_checklist_status"], checklist)
            self.assertIs(item["auto_sell_eligible"], False)
            raw = data["items"][symbol]
            for key in (
                "research_status", "proposed_classification",
                "classification_change_reason", "evidence_urls", "checked_at",
            ):
                self.assertIn(key, raw)
            self.assertEqual(raw["research_status"], "complete")
            self.assertEqual(raw["proposed_classification"], classification)
            self.assertEqual(raw["evidence_urls"], raw["source_urls"])


# ── 3. 정렬 ──────────────────────────────────────────────────────

class TestPrioritySort(unittest.TestCase):
    def test_adjusted_sell_priority_ordering(self):
        # XOM: 8.0 + 1.0 = 9.0 > 한국전력: 8.5 + 0.0 = 8.5 > NAVER: 11.0 - 3.5 = 7.5
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("035420.KS", weakness=11.0),
             _row("015760.KS", weakness=8.5),
             _row("XOM", weakness=8.0)],
            scores=_SCORES,
        )
        self.assertEqual([r["symbol"] for r in out], ["XOM", "015760.KS", "035420.KS"])
        self.assertEqual(out[0]["adjusted_sell_priority"], 9.0)
        self.assertEqual(out[2]["adjusted_sell_priority"], 7.5)


# ── 4. hard stop 후보는 AI Berkshire와 무관 ──────────────────────

class TestAuditMetadataPreserved(unittest.TestCase):
    """감사 필드(classification_change_reason/evidence_urls/checked_at) 보존 회귀."""

    def test_audit_fields_survive_normalizer_buy_and_sell_paths(self):
        raw = _item(
            "trim", 1.0, name="Exxon Mobil",
            buy_checklist_status="fail",
            auto_sell_eligible=False,
            classification_change_reason="  2026-07 10-K 재검토로 trim 유지  ",
            evidence_urls=[
                "https://www.sec.gov/Archives/edgar/data/xom-10k.htm",
                "  http://investor.exxonmobil.com/filing  ",
                "javascript:alert(1)",
                12345,
                "ftp://not-allowed.example.com",
            ],
            checked_at="  2026-07-12T04:00:00+09:00  ",
        )
        scores = {"version": "ai_berkshire_toss_v2", "read_only": True,
                  "items": {"XOM": raw}}
        source_snapshot = json.loads(json.dumps(raw))

        # 1) normalizer: 공백 정리 + HTTP(S) URL만 유지
        item = abt.normalize_ai_berkshire_item(raw, as_of_date="2026-07-12")
        self.assertEqual(item["classification_change_reason"],
                         "2026-07 10-K 재검토로 trim 유지")
        self.assertEqual(item["evidence_urls"], [
            "https://www.sec.gov/Archives/edgar/data/xom-10k.htm",
            "http://investor.exxonmobil.com/filing",
        ])
        self.assertEqual(item["checked_at"], "2026-07-12T04:00:00+09:00")

        # 2) BUY 결과: 세 필드 유지 + checklist fail 차단 불변
        buy = abt.evaluate_ai_berkshire_buy_gate(
            "XOM", scores, as_of_date="2026-07-12")
        self.assertTrue(buy["buy_block"])
        self.assertEqual(buy["buy_reason"], "ai_berkshire_buy_checklist_fail")
        self.assertEqual(buy["classification_change_reason"],
                         item["classification_change_reason"])
        self.assertEqual(buy["evidence_urls"], item["evidence_urls"])
        self.assertEqual(buy["checked_at"], item["checked_at"])

        # 3) SELL 결과: 세 필드 유지 + auto_sell false면 trim이어도 차단 불변
        out = abt.apply_berkshire_to_sell_to_fund(
            [_row("XOM")], scores=scores, as_of_date="2026-07-12")
        ai = out[0]["ai_berkshire"]
        self.assertFalse(out[0]["auto_sell_eligible"])
        self.assertEqual(out[0]["auto_sell_block_reason"],
                         "ai_berkshire_auto_sell_disabled")
        self.assertEqual(ai["classification_change_reason"],
                         item["classification_change_reason"])
        self.assertEqual(ai["evidence_urls"], item["evidence_urls"])
        self.assertEqual(ai["checked_at"], item["checked_at"])

        # 4) source object 불변
        self.assertEqual(raw, source_snapshot)

        # 5) 필드 부재 시 기본값 (None / [] / None)
        plain = abt.normalize_ai_berkshire_item(
            _item("hold"), as_of_date="2026-07-12")
        self.assertIsNone(plain["classification_change_reason"])
        self.assertEqual(plain["evidence_urls"], [])
        self.assertIsNone(plain["checked_at"])


class TestHardStopUnaffected(unittest.TestCase):
    def test_stop_loss_candidate_survives_hold_classification(self):
        """ABBV가 hold여도 -9% 손절 후보는 evaluate_holdings에서 그대로 나온다."""
        import core.toss_position_review as tpr
        holding = {
            "symbol": "ABBV", "name": "AbbVie", "quantity": 1,
            "lastPrice": 230.0, "currency": "USD",
            "profitLoss": {"amount": -9000, "amountAfterCost": -9000},
            "marketValue": {"purchaseAmount": 100000, "amount": 91000},
        }
        with patch.object(tpr, "_symbols_with_active_exit_levels", return_value=set()), \
             patch.object(tpr, "_income_managed_symbols", return_value=set()):
            out = tpr.evaluate_holdings([holding])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "stop_loss")


if __name__ == "__main__":
    unittest.main()
