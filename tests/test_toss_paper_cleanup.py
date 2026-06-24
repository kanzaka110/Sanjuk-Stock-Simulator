"""
cleanup_toss_paper_ledger.py 테스트

- --dry-run: 변경 없음, 출력만
- --expire-preview-minutes N: previewed → expired 전환
- --source 필터 동작
- 승인 자동 취소 없음
- 삭제 없음
- 금지 문구/함수명 부재
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.toss_paper_ledger as ledger

KST = timezone(timedelta(hours=9))


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path):
    db = tmp_path / "test_cleanup.db"
    with patch.object(ledger, "_DB_PATH", db):
        yield db


def _ctx(**kw) -> dict:
    base = {"cash_krw": 5_000_000, "usdkrw": 1530.0}
    base.update(kw)
    return base


def _insert_previewed(symbol: str, minutes_ago: int, source: str = "telegram_paper_preview") -> None:
    """지정 분 전에 생성된 previewed 레코드 삽입."""
    import sqlite3
    ts = (datetime.now(KST) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    db_path = ledger._DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_ledger (
        paper_id TEXT PRIMARY KEY, preview_id TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL, quantity INTEGER DEFAULT 0, limit_price REAL DEFAULT 0,
        estimated_amount_krw REAL DEFAULT 0, status TEXT NOT NULL DEFAULT 'previewed',
        source TEXT DEFAULT '', account_label TEXT DEFAULT 'Toss 실전 AI 자동거래 계좌',
        live_order_allowed INTEGER NOT NULL DEFAULT 0, dry_run INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, approved_at TEXT, cancelled_at TEXT,
        reason TEXT DEFAULT '', confidence REAL DEFAULT 0,
        blocks TEXT DEFAULT '[]', warnings TEXT DEFAULT '[]', metadata TEXT DEFAULT '{}'
    )""")
    conn.execute(
        "INSERT INTO paper_ledger (paper_id, preview_id, symbol, side, quantity, "
        "limit_price, estimated_amount_krw, status, source, live_order_allowed, dry_run, "
        "created_at, reason, confidence, blocks, warnings, metadata) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,1,?,?,?,?,?,?)",
        (f"paper_cleanup_{symbol}", f"prev_{symbol}", symbol, "buy", 1, 1000, 1000,
         "previewed", source, ts, "", 0.0, "[]", "[]", "{}")
    )
    conn.commit()
    conn.close()


# ─── expire_stale_previews 직접 동작 ────────────────────────

class TestExpireStaleCore:
    def test_old_previewed_expires(self):
        _insert_previewed("A.KS", minutes_ago=90)
        result = ledger.expire_stale_previews(older_than_minutes=60)
        assert result["ok"] is True
        assert result["expired_count"] == 1

    def test_recent_previewed_kept(self):
        _insert_previewed("B.KS", minutes_ago=10)
        result = ledger.expire_stale_previews(older_than_minutes=60)
        assert result["expired_count"] == 0
        assert result["kept_count"] == 1

    def test_source_filter_targets_only_matching(self):
        _insert_previewed("C.KS", minutes_ago=90, source="telegram_paper_preview")
        _insert_previewed("D.KS", minutes_ago=90, source="other_source")
        result = ledger.expire_stale_previews(
            older_than_minutes=60,
            source_filter="telegram_paper_preview",
        )
        assert result["expired_count"] == 1
        assert result["kept_count"] == 1

    def test_no_delete_only_status_change(self):
        _insert_previewed("E.KS", minutes_ago=90)
        ledger.expire_stale_previews(older_than_minutes=60)
        orders = ledger.list_paper_orders()
        assert len(orders) == 1
        assert orders[0]["status"] == "expired"

    def test_approved_not_touched(self):
        cands = [{"symbol": "F.KS", "side": "buy", "quantity": 1, "limit_price": 1000,
                  "estimated_amount_krw": 1000, "confidence": 0.0, "reason": ""}]
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "paper_only",
                "live_order_allowed": False, "score_adjustments": []}]
        ledger.create_paper_preview_records("prev_app_keep", cands, ccs, _ctx())
        ledger.approve_paper_order("prev_app_keep")
        # expire with 0-minute threshold (everything old)
        ledger.expire_stale_previews(older_than_minutes=0)
        orders = ledger.list_paper_orders()
        approved = [o for o in orders if o["status"] == "approved"]
        assert len(approved) == 1

    def test_blocked_not_expired_by_stale(self):
        _insert_previewed("G.KS", minutes_ago=90, source="x")
        cands = [{"symbol": "G.KS", "side": "buy", "quantity": 1, "limit_price": 1000,
                  "estimated_amount_krw": 1000, "confidence": 0.0, "reason": ""}]
        ccs = [{"blocks": ["blacklisted"], "warnings": [], "toss_readiness": "blocked",
                "live_order_allowed": False, "score_adjustments": []}]
        ledger.create_paper_preview_records("prev_blocked", cands, ccs, _ctx())
        ledger.expire_stale_previews(older_than_minutes=0)
        orders = ledger.list_paper_orders()
        blocked = [o for o in orders if o["status"] == "blocked"]
        assert len(blocked) == 1


# ─── cleanup script 소스 검사 ────────────────────────────────

class TestCleanupScriptSource:
    def _src(self) -> str:
        return (ROOT / "scripts" / "cleanup_toss_paper_ledger.py").read_text(encoding="utf-8")

    def test_no_delete_sql(self):
        src = self._src()
        assert "DELETE FROM" not in src.upper()

    def test_no_live_order_true(self):
        src = self._src()
        assert "live_order_allowed=True" not in src
        assert "live_orders_allowed=True" not in src

    def test_no_forbidden_fn(self):
        src = self._src()
        for fn in ("place_order", "submit_order", "execute_order"):
            assert fn not in src

    def test_no_forbidden_cta(self):
        src = self._src()
        for word in ("주문 실행", "매수하기", "매도하기", "자동매매 시작", "자동거래 시작"):
            assert word not in src

    def test_no_auto_cancel_approved(self):
        """cleanup script에 approve 취소 자동화 없음."""
        src = self._src()
        # cancel_paper_order with approved status auto-trigger 없어야 함
        assert "cancel_paper_order(" not in src

    def test_dry_run_flag_present(self):
        src = self._src()
        assert "--dry-run" in src

    def test_expire_preview_minutes_flag_present(self):
        src = self._src()
        assert "--expire-preview-minutes" in src

    def test_no_sensitive_data(self):
        src = self._src()
        long_nums = re.findall(r"\b\d{8,}\b", src)
        assert long_nums == []
        assert "access_token" not in src
        assert "Bearer " not in src
        assert "TOSS_APP_SECRET" not in src


# ─── dashboard_data stale/expired fields ────────────────────

class TestDashboardDataLedger:
    def test_stale_preview_count_in_ledger_data(self):
        """toss_paper_ledger_data()에 stale_preview_count 포함."""
        from core.dashboard_data import toss_paper_ledger_data
        cands = [{"symbol": "X.KS", "side": "buy", "quantity": 1, "limit_price": 1000,
                  "estimated_amount_krw": 1000, "confidence": 0.0, "reason": ""}]
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "paper_only",
                "live_order_allowed": False, "score_adjustments": []}]
        ledger.create_paper_preview_records("prev_dd_001", cands, ccs, _ctx())
        data = toss_paper_ledger_data()
        assert "stale_preview_count" in data
        assert data["stale_preview_count"] == 1

    def test_expired_count_in_ledger_data(self):
        """toss_paper_ledger_data()에 expired_count 포함."""
        from core.dashboard_data import toss_paper_ledger_data
        _insert_previewed("Y.KS", minutes_ago=90)
        ledger.expire_stale_previews(older_than_minutes=60)
        data = toss_paper_ledger_data()
        assert "expired_count" in data
        assert data["expired_count"] >= 1
