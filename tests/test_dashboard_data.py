"""대시보드 데이터 레이어 (조회 전용) 테스트.

DB가 없거나 비어 있어도 예외 없이 안전한 빈 구조를 반환하는지 검증한다.
"""

import sqlite3

import pytest

from core import dashboard_data as dd


@pytest.fixture
def no_db(monkeypatch, tmp_path):
    """DB 파일이 존재하지 않는 상황."""
    monkeypatch.setattr(dd, "_db_path", lambda: tmp_path / "nope.db")
    return tmp_path


@pytest.fixture
def empty_db(monkeypatch, tmp_path):
    """predictions/accuracy_stats 테이블이 비어 있는 DB."""
    p = tmp_path / "memory.db"
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE predictions (
            created_at TEXT, closed_at TEXT, ticker TEXT, name TEXT,
            signal TEXT, original_signal TEXT, action_type TEXT,
            action_grade TEXT, account_type TEXT, briefing_type TEXT,
            entry_price REAL, target_price REAL, stop_loss REAL,
            confidence REAL, status TEXT, outcome TEXT, pnl_pct REAL,
            normalizer_version TEXT
        );
        CREATE TABLE accuracy_stats (
            ticker TEXT, total_predictions INTEGER, evaluated_count INTEGER,
            wins INTEGER, losses INTEGER, win_rate REAL, avg_pnl REAL,
            profit_factor REAL, expectancy REAL
        );
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(dd, "_db_path", lambda: p)
    return p


# ─── health: DB 유무와 무관하게 항상 ok ────────────────
def test_health_without_db(no_db):
    h = dd.health()
    assert h["status"] == "ok"
    assert h["db_available"] is False
    assert "now" in h


def test_health_with_db(empty_db):
    h = dd.health()
    assert h["status"] == "ok"
    assert h["db_available"] is True


# ─── DB 없음: 모든 조회가 빈 구조 반환 (예외 없음) ─────
def test_queries_safe_without_db(no_db):
    assert dd.recent_predictions() == []
    assert dd.open_predictions() == []
    assert dd.accuracy_by_ticker() == []

    closed = dd.closed_summary()
    assert closed["total"] == 0
    assert closed["recent"] == []

    lb = dd.latest_briefing_actions()
    assert lb["day"] == ""
    assert lb["by_type"] == {}
    assert lb["rows"] == []

    stats = dd.db_stats()
    assert stats["db_exists"] is False
    assert stats["predictions"] == 0


# ─── 빈 DB: 테이블은 있으나 행이 없음 ──────────────────
def test_queries_safe_with_empty_db(empty_db):
    assert dd.recent_predictions() == []
    assert dd.open_predictions() == []
    assert dd.accuracy_by_ticker() == []

    closed = dd.closed_summary()
    assert closed["total"] == 0
    assert closed["win"] == 0
    assert closed["avg_pnl"] == 0.0

    lb = dd.latest_briefing_actions()
    assert lb["day"] == ""

    stats = dd.db_stats()
    assert stats["db_exists"] is True
    assert stats["predictions"] == 0
    assert stats["open"] == 0


# ─── system_status: 전체 구조가 항상 채워짐 ────────────
def test_system_status_structure(empty_db):
    st = dd.system_status()
    assert "now" in st
    assert "db" in st and st["db"]["db_exists"] is True
    assert "service" in st and "active" in st["service"]
    assert "latest_briefing" in st


# ─── 행이 있는 DB: 집계가 정상 동작 ────────────────────
def test_with_sample_rows(empty_db):
    conn = sqlite3.connect(empty_db)
    conn.execute(
        """INSERT INTO predictions
           (created_at, closed_at, ticker, name, signal, action_type,
            account_type, entry_price, target_price, status, outcome,
            pnl_pct, normalizer_version, briefing_type)
           VALUES
           ('2026-06-10T08:30:00', '', '005930.KS', '삼성전자', '매수',
            'AI_NEW_BUY', '일반', 70000, 80000, 'open', '', 0, 'v1', 'KR_BEFORE')""",
    )
    conn.execute(
        """INSERT INTO predictions
           (created_at, closed_at, ticker, name, signal, action_type,
            account_type, entry_price, target_price, status, outcome,
            pnl_pct, normalizer_version, briefing_type)
           VALUES
           ('2026-06-09T08:30:00', '2026-06-10T15:30:00', '012450.KS', '한화에어로',
            '매도', 'AI_SELL_MANAGEMENT', '일반', 300000, 330000, 'closed', 'win',
            10.0, 'v1', 'KR_BEFORE')""",
    )
    conn.commit()
    conn.close()

    recent = dd.recent_predictions()
    assert len(recent) == 2
    assert recent[0]["ticker"] == "005930.KS"  # 최신순

    opens = dd.open_predictions()
    assert len(opens) == 1
    assert opens[0]["ticker"] == "005930.KS"

    closed = dd.closed_summary()
    assert closed["total"] == 1
    assert closed["win"] == 1

    stats = dd.db_stats()
    assert stats["predictions"] == 2
    assert stats["open"] == 1
    assert stats["closed"] == 1
    assert stats["v1"] == 2

    lb = dd.latest_briefing_actions()
    assert lb["day"] == "2026-06-10"
    assert lb["by_type"].get("AI_NEW_BUY") == 1


# ─── 추천 타임라인 구조 검증 ──────────────────────────
def test_timeline_structure(empty_db):
    tl = dd.recommendations_timeline(range_="today")
    assert "items" in tl
    assert "count" in tl
    assert isinstance(tl["items"], list)
    assert tl["count"] == 0


def test_timeline_with_data(empty_db):
    """타임라인에 데이터가 있을 때 action_label 필드 포함."""
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT09:00:00")

    conn = sqlite3.connect(empty_db)
    conn.execute(
        """INSERT INTO predictions
           (created_at, closed_at, ticker, name, signal, action_type,
            account_type, entry_price, target_price, status, outcome,
            pnl_pct, normalizer_version, briefing_type)
           VALUES
           (?, '', 'NVDA', 'NVIDIA', '매수',
            'AI_NEW_BUY', '일반', 130, 150, 'open', '', 0, 'v1', 'US_BEFORE')""",
        (today,),
    )
    conn.commit()
    conn.close()

    tl = dd.recommendations_timeline(range_="today")
    assert tl["count"] == 1
    assert tl["items"][0]["ticker"] == "NVDA"
    assert "action_label" in tl["items"][0]


# ─── ticker_detail 구조 검증 ─────────────────────────
def test_ticker_detail_structure(empty_db):
    """ticker_detail이 필수 키를 항상 반환."""
    td = dd.ticker_detail("UNKNOWN")
    for key in ("ticker", "name", "current_price", "day_pct",
                "recent", "open", "closed", "accuracy"):
        assert key in td, f"missing key: {key}"
    assert isinstance(td["recent"], list)
    assert isinstance(td["open"], list)


# ─── 시뮬레이터 HTML 구조 검증 ───────────────────────
def test_simulator_tab_in_html():
    """index.html에 시뮬레이터 탭과 주요 요소가 존재."""
    from pathlib import Path
    html = (Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")

    # 탭 버튼
    assert 'data-t="sim"' in html
    assert 'id="t-sim"' in html

    # 3패널 구조
    assert 'sim-left' in html
    assert 'sim-center' in html
    assert 'sim-right' in html

    # 주요 요소 ID
    assert 'id="sim-list"' in html
    assert 'id="sim-hero"' in html
    assert 'id="sim-order"' in html

    # 안전 장치: POST/실제 주문 없음
    assert "주문표 미리보기" in html
    assert "실제 주문이 실행되지 않습니다" in html

    # HTML 기본 구조
    assert html.count("<html") == 1
    assert html.count("</html>") == 1
    assert html.count("<body") == 1
    assert html.count("</body>") == 1


def test_pc_html_exists_and_valid():
    """index_pc.html이 존재하고 주요 구조를 갖춤."""
    from pathlib import Path
    pc = Path(__file__).parent.parent / "web" / "index_pc.html"
    assert pc.exists(), "index_pc.html 미존재"
    html = pc.read_text(encoding="utf-8")
    assert 'class="gnb"' in html, "글로벌 내비 없음"
    assert 'id="p-home"' in html, "홈 페이지 없음"
    assert 'id="p-portfolio"' in html, "포트폴리오 페이지 없음"
    assert "POST" not in html, "POST 금지 위반"
    assert "실제 주문" not in html or "실행되지 않습니다" not in html, \
        "PC에서 주문 실행 버튼 존재"
    assert html.count("<html") == 1


def test_view_query_routing():
    """app.py의 / 라우트가 view 파라미터를 지원."""
    from pathlib import Path
    app_code = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    assert "view" in app_code, "view 파라미터 미지원"
    assert "index_pc.html" in app_code, "PC HTML 참조 없음"
    assert "Mobile" in app_code or "mobile" in app_code, "모바일 UA 감지 없음"
