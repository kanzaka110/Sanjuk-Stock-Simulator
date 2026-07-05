"""신규 데이터 소스 단위 테스트 — 공매도/FRED/EDGAR/F&G (전부 오프라인)."""

from __future__ import annotations

from datetime import datetime, timedelta

import core.edgar_monitor as em
from core.edgar_monitor import KST, WATCH_FORMS, _format_alert_message, fetch_recent_filings
from core.fear_greed import fear_greed_to_text
from core.kr_market import kr_market_to_text
from core.macro_fred import macro_to_text


# ── Fear & Greed ─────────────────────────────────────────────

def test_fear_greed_text_empty():
    assert fear_greed_to_text({}) == ""


def test_fear_greed_text_format():
    snap = {"score": 32.0, "rating_kr": "공포", "prev_week": 25.0, "prev_month": 53.0}
    text = fear_greed_to_text(snap)
    assert "32/100" in text
    assert "공포" in text
    assert "+7" in text
    assert "-21" in text


# ── FRED ─────────────────────────────────────────────────────

def test_macro_text_empty():
    assert macro_to_text({}) == ""


def test_macro_text_format():
    snap = {
        "t10y2y": {"value": -0.5, "date": "2026-07-01", "label": "역전(침체 신호)"},
        "dgs10": {"value": 4.48, "date": "2026-07-01", "chg_20d": 0.1},
        "cpi_yoy": {"value": 4.3, "date": "2026-06-01"},
    }
    text = macro_to_text(snap)
    assert "역전" in text
    assert "4.48%" in text
    assert "4.3%" in text


# ── EDGAR ────────────────────────────────────────────────────

def _fake_submissions(forms, dates, accessions):
    class R:
        status_code = 200

        def json(self):
            return {"filings": {"recent": {
                "form": forms,
                "filingDate": dates,
                "accessionNumber": accessions,
                "primaryDocDescription": [""] * len(forms),
            }}}
    return R()


def test_edgar_filters_watch_forms(monkeypatch):
    today = datetime.now(KST).strftime("%Y-%m-%d")
    monkeypatch.setattr(em.requests, "get", lambda *a, **k: _fake_submissions(
        ["8-K", "4", "10-Q"], [today, today, today], ["a1", "a2", "a3"]))
    hits = fetch_recent_filings("NVDA", 1045810)
    assert [h["form"] for h in hits] == ["8-K", "10-Q"]
    assert hits[0]["severity"] == "high"
    assert "4" not in WATCH_FORMS


def test_edgar_cutoff_stops_old_filings(monkeypatch):
    today = datetime.now(KST).strftime("%Y-%m-%d")
    old = (datetime.now(KST) - timedelta(days=30)).strftime("%Y-%m-%d")
    monkeypatch.setattr(em.requests, "get", lambda *a, **k: _fake_submissions(
        ["8-K", "8-K"], [today, old], ["a1", "a2"]))
    hits = fetch_recent_filings("MU", 723125)
    assert len(hits) == 1
    assert hits[0]["accession"] == "a1"


def test_edgar_fetch_failure_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(em.requests, "get", boom)
    assert fetch_recent_filings("LMT", 936468) == []


def test_edgar_alert_message_format():
    msg = _format_alert_message([{
        "ticker": "NVDA", "form": "8-K", "severity": "high",
        "filing_date": "2026-07-02", "accession": "x", "description": "", "url": "http://u",
    }])
    assert "NVDA" in msg
    assert "8-K" in msg
    assert "자동 매도는 발동하지 않음" in msg


def test_edgar_dedup_and_state(monkeypatch, tmp_path):
    today = datetime.now(KST).strftime("%Y-%m-%d")
    monkeypatch.setattr(em, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(em, "_us_holding_tickers", lambda: ["NVDA"])
    monkeypatch.setattr(em, "_cik_map", lambda: {"NVDA": 1045810})
    monkeypatch.setattr(em.requests, "get", lambda *a, **k: _fake_submissions(
        ["8-K"], [today], ["acc-1"]))
    monkeypatch.setattr(em.time, "sleep", lambda *_: None)

    sent = []
    import core.telegram as tg
    monkeypatch.setattr(tg, "send_simple_message", lambda m: sent.append(m) or True)

    r1 = em.run_edgar_monitor(force=True)
    assert r1["new_hit_count"] == 1 and r1["sent"] is True
    r2 = em.run_edgar_monitor(force=True)
    assert r2["new_hit_count"] == 0 and r2["sent"] is False
    assert len(sent) == 1


# ── 공매도 텍스트 ────────────────────────────────────────────

def test_kr_market_text_includes_short_selling():
    ss = {"462870.KS": {"name": "시프트업", "short_ratio_pct": 6.3,
                        "avg5_ratio_pct": 16.0, "trend": "감소", "date": "2026-07-03"}}
    text = kr_market_to_text({}, {}, None, ss)
    assert "공매도" in text
    assert "시프트업" in text
    assert "6.3" in text
