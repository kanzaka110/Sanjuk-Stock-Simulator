"""tests/test_ai_berkshire_research_queue.py

AI Berkshire 재리서치 큐 (read-only, GET 전용).

- 대상: Toss holdings + /api/toss/buy-candidates 후보 중 unscored/expired/invalid,
  그리고 score 파일 중 valid_until 30일 이내
- 중복 symbol merge (bare code ↔ .KS/.KQ)
- 계좌번호/token/order id/raw broker response 노출 금지
- GET 반복 호출에 preview/PASS/order/DB insert 부작용 0
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.ai_berkshire_toss as abt
from core import dashboard_data as dd
from core import discovery_candidates as disc
from core.discovery_candidates import DiscoverySections, NewCandidate


_AS_OF = "2026-07-15"

_FRESH = {
    "as_of": "2026-07-10",
    "thesis": "test thesis",
    "red_lines": ["test red line"],
    "source_urls": ["https://example.com/ir"],
}


def _item(classification, valid_until="2099-12-31", **overrides):
    base = {"classification": classification, "valid_until": valid_until, **_FRESH}
    base.update(overrides)
    return base


# 005930: 미채점 (score 없음)          → unscored
# ABBV:   valid_until 경과              → expired
# 035420: source_urls 없음 (근거 불량)  → invalid
# XOM:    fresh hold, D-17 만료 임박     → expiring_within_30d (holding + candidate + score)
# 015760.KS: score 파일만, D-26 만료 임박 → expiring_within_30d (score)
# 068270.KS: fresh hold, 2099 만료       → 큐 제외
_SCORES = {
    "version": "ai_berkshire_toss_v2",
    "read_only": True,
    "items": {
        "ABBV": _item("hold", valid_until="2026-07-01", name="AbbVie"),
        "035420.KS": _item("hold", source_urls=[], name="NAVER"),
        "XOM": _item("hold", valid_until="2026-08-01", name="Exxon Mobil"),
        "015760.KS": _item("sell_to_fund", valid_until="2026-08-10", name="한국전력"),
        "068270.KS": _item("hold", name="셀트리온"),
    },
}

_HOLDINGS = [
    {"symbol": "005930", "name": "삼성전자", "quantity": "90", "lastPrice": "71000"},
    {"symbol": "ABBV", "name": "AbbVie", "quantity": "3", "lastPrice": "230"},
    {"symbol": "035420", "name": "NAVER", "quantity": "5", "lastPrice": "180000"},
    {"symbol": "XOM", "name": "Exxon Mobil", "quantity": "4", "lastPrice": "115"},
    {"symbol": "068270.KS", "name": "셀트리온", "quantity": "2", "lastPrice": "170000"},
]

_CANDIDATES = {
    "items": [
        {"symbol": "005930.KS", "name": "삼성전자", "side": "buy"},
        {"symbol": "XOM", "name": "Exxon Mobil", "side": "buy"},
    ]
}


def _patch_sources(monkeypatch, scores=None, holdings=None, candidates=None):
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    monkeypatch.setattr(abt, "load_ai_berkshire_scores",
                        lambda *a, **k: _SCORES if scores is None else scores)
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "holdings_count": len(_HOLDINGS if holdings is None else holdings),
        "holdings_items": _HOLDINGS if holdings is None else holdings,
    })
    monkeypatch.setattr(dd, "toss_buy_candidates_data",
                        lambda *a, **k: _CANDIDATES if candidates is None else candidates)


def _by_symbol(payload):
    out = {}
    for item in payload["items"]:
        key = item["symbol"].split(".", 1)[0] if item["symbol"][:6].isdigit() else item["symbol"]
        out[key] = item
    return out


# ═══════════════════════════════════════════════════════════════
# 8. dedupe + counts
# ═══════════════════════════════════════════════════════════════

def test_queue_shape_and_read_only_flags(monkeypatch):
    _patch_sources(monkeypatch)
    payload = dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)

    assert payload["version"] == "ai_berkshire_research_queue_v1"
    assert payload["read_only"] is True
    assert payload["generated_at"]
    assert isinstance(payload["items"], list)


def test_queue_counts_are_exact(monkeypatch):
    _patch_sources(monkeypatch)
    counts = dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)["counts"]

    assert counts["unscored"] == 1
    assert counts["expired"] == 1
    assert counts["invalid"] == 1
    assert counts["expiring_within_30d"] == 2


def test_queue_merges_duplicate_symbol_across_sources(monkeypatch):
    """holding '005930' + candidate '005930.KS' → 1건, sources 병합."""
    _patch_sources(monkeypatch)
    payload = dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)

    samsung = [i for i in payload["items"] if i["symbol"].startswith("005930")]
    assert len(samsung) == 1
    assert set(samsung[0]["sources"]) == {"holding", "buy_candidate"}
    assert samsung[0]["reason"] == "unscored"


def test_queue_merges_holding_candidate_and_score_sources(monkeypatch):
    _patch_sources(monkeypatch)
    xom = _by_symbol(dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF))["XOM"]

    assert set(xom["sources"]) == {"holding", "buy_candidate", "score"}
    assert xom["reason"] == "expiring_within_30d"
    assert xom["valid_until"] == "2026-08-01"


def test_queue_reason_and_classification_fields(monkeypatch):
    _patch_sources(monkeypatch)
    items = _by_symbol(dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF))

    assert items["005930"]["reason"] == "unscored"
    assert items["005930"]["stored_classification"] is None
    assert items["005930"]["classification"] is None

    assert items["ABBV"]["reason"] == "expired"
    assert items["ABBV"]["stored_classification"] == "hold"
    assert items["ABBV"]["classification"] == "gray_zone"

    assert items["035420"]["reason"] == "invalid"
    assert "missing_source_urls" in items["035420"]["freshness_issues"]

    assert items["015760"]["reason"] == "expiring_within_30d"
    assert items["015760"]["sources"] == ["score"]


def test_queue_excludes_fresh_and_far_dated_scores(monkeypatch):
    _patch_sources(monkeypatch)
    symbols = {i["symbol"] for i in dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)["items"]}
    assert not any(s.startswith("068270") for s in symbols)


def test_queue_expiring_boundary_is_inclusive_at_30_days(monkeypatch):
    scores = {"items": {
        "AAA": _item("hold", valid_until="2026-08-14"),   # D+30 → 포함
        "BBB": _item("hold", valid_until="2026-08-15"),   # D+31 → 제외
    }}
    _patch_sources(monkeypatch, scores=scores, holdings=[], candidates={"items": []})
    payload = dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)

    symbols = {i["symbol"] for i in payload["items"]}
    assert symbols == {"AAA"}
    assert payload["counts"]["expiring_within_30d"] == 1


def test_queue_survives_missing_scores_file(monkeypatch):
    _patch_sources(monkeypatch, scores={})
    payload = dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)

    # score가 하나도 없으면 holdings/candidates 전부 unscored
    assert payload["counts"]["unscored"] == len(payload["items"]) > 0
    assert payload["counts"]["expired"] == 0


def test_queue_survives_source_failure(monkeypatch):
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    monkeypatch.setattr(abt, "load_ai_berkshire_scores", lambda *a, **k: _SCORES)
    monkeypatch.setattr(dd, "toss_account_summary", lambda: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(dd, "toss_buy_candidates_data",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("y")))

    payload = dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)
    assert payload["read_only"] is True
    assert payload["counts"]["expiring_within_30d"] == 2


# ═══════════════════════════════════════════════════════════════
# 9. secrets / 계좌 / 주문 식별자 미노출
# ═══════════════════════════════════════════════════════════════

_FORBIDDEN = (
    "accountSeq", "account_seq", "account_no", "accountNo", "cashBuyingPower",
    "token", "Bearer", "pilot_id", "verification_id", "order_id", "orderId",
    "app_key", "appSecret", "raw_response",
)

_ALLOWED_ITEM_KEYS = {
    "symbol", "name", "reason", "sources", "stored_classification",
    "classification", "as_of", "valid_until", "freshness_issues",
}


def test_queue_response_has_no_secrets_or_account_or_order_ids(monkeypatch):
    holdings = [
        {"symbol": "005930", "name": "삼성전자", "accountSeq": "12345678",
         "account_no": "1234567890", "cashBuyingPower": "9999999",
         "raw_response": {"token": "secret-token-value"}},
    ]
    _patch_sources(monkeypatch, holdings=holdings)
    payload = dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)
    blob = json.dumps(payload, ensure_ascii=False)

    for needle in _FORBIDDEN:
        assert needle not in blob, needle
    assert "secret-token-value" not in blob
    assert "1234567890" not in blob

    for item in payload["items"]:
        assert set(item) <= _ALLOWED_ITEM_KEYS, set(item) - _ALLOWED_ITEM_KEYS


# ═══════════════════════════════════════════════════════════════
# 10. GET 반복 호출 부작용 0
# ═══════════════════════════════════════════════════════════════

def _new_cand(ticker, name, price=50_000, score=88):
    return NewCandidate(
        ticker=ticker, name=name, market="KR", price=float(price), score=score,
        idea=f"{name} 아이디어", reasons=("거래대금 충분",),
        target_price=round(price * 1.12, 2), stop_loss=round(price * 0.96, 2),
        risk_reward=3.0, change_pct=2.0, tags=(),
    )


def _patch_buy_candidates(monkeypatch):
    sections = DiscoverySections(
        holdings_management=(), watchlist_reeval=(),
        new_discovery=(_new_cand("000777.KS", "강한수입후보"),),
        new_rejected=(), market="KR",
    )
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    monkeypatch.setattr(disc, "_fallback_universe_candidates", lambda markets: [])
    monkeypatch.setattr(disc, "build_discovery_sections", lambda *a, **k: sections)
    monkeypatch.setattr(disc, "recent_recommended_tickers", lambda *a, **k: set())

    from core import toss_client as tc
    from core import toss_live_pilot_policy as tlp
    monkeypatch.setattr(tlp, "compute_toss_live_pilot_policy",
                        lambda *a, **k: {"max_order_krw": 500_000})
    monkeypatch.setattr(dd, "_cross_check_price_quality",
                        lambda sym, cur=None: {"quality": "unknown", "checks": []})
    monkeypatch.setattr(tc, "get_exchange_rate", lambda base="USD", quote="KRW": {"rate": 1500.0})
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 10_000_000, "krw_native": 10_000_000, "usd": 10_000.0},
        "holdings_count": 0, "holdings_items": _HOLDINGS,
    })
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    monkeypatch.setattr(abt, "load_ai_berkshire_scores", lambda *a, **k: _SCORES)


def test_repeated_get_calls_create_no_previews_orders_or_verifications(monkeypatch):
    _patch_buy_candidates(monkeypatch)

    with patch("core.toss_live_pilot_preview.build_live_pilot_preview") as preview, \
            patch("core.toss_live_pilot_ledger.record_live_pilot_preview") as ledger, \
            patch("core.toss_live_pilot_verification.create_verification_request") as req, \
            patch("core.toss_live_pilot_verification.record_hermes_verification") as rec, \
            patch("core.toss_autonomous_finalizer.try_autonomous_finalize") as final:
        for _ in range(3):
            dd._cache.clear()
            queue = dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)
            cands = dd.toss_buy_candidates_data(range_="today")

    assert queue["read_only"] is True
    assert cands["items"]
    preview.assert_not_called()
    ledger.assert_not_called()
    req.assert_not_called()
    rec.assert_not_called()
    final.assert_not_called()


def test_repeated_queue_calls_are_stable(monkeypatch):
    _patch_sources(monkeypatch)
    first = dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)
    dd._cache.clear()
    second = dd.ai_berkshire_research_queue_data(as_of_date=_AS_OF)

    assert first["counts"] == second["counts"]
    assert [i["symbol"] for i in first["items"]] == [i["symbol"] for i in second["items"]]


def test_queue_route_is_get_only():
    from web import app as webapp

    routes = [r for r in webapp.app.routes
              if getattr(r, "path", "") == "/api/toss/ai-berkshire-research-queue"]
    assert routes, "research queue route missing"
    assert set(routes[0].methods) <= {"GET", "HEAD"}
