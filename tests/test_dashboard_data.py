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


# ═══════════════════════════════════════════════════════
# 포트폴리오 기여도/성과 분석 (3단계) — 시세 호출 모킹
# ═══════════════════════════════════════════════════════
def _sample_pf() -> dict:
    """결정론적 샘플 포트폴리오 (계좌·자산군·보호종목 포함).

    TIGER S&P500(ETF, +2,000,000) / MU 마이크론(해외주식·보호, +500,000)
    시프트업(국내주식, -1,000,000). 전체 평가손익 = +1,500,000.
    """
    return {
        "accounts": [
            {
                "name": "일반",
                "cash": 2_000_000,
                "eval_total": 15_500_000,
                "cost_total": 13_000_000,
                "pnl_pct": 19.23,
                "weight": 81.4,
                "items": [
                    {"ticker": "360750.KS", "name": "TIGER 미국S&P500",
                     "eval_krw": 10_000_000, "pnl_pct": 25.0, "day_pct": 2.0},
                    {"ticker": "MU", "name": "마이크론",
                     "eval_krw": 5_500_000, "pnl_pct": 10.0, "day_pct": 1.0},
                ],
            },
            {
                "name": "ISA",
                "cash": 0,
                "eval_total": 4_000_000,
                "cost_total": 5_000_000,
                "pnl_pct": -20.0,
                "weight": 18.6,
                "items": [
                    {"ticker": "462870.KS", "name": "시프트업",
                     "eval_krw": 4_000_000, "pnl_pct": -20.0, "day_pct": -3.0},
                ],
            },
        ],
        "total_eval": 21_500_000,
        "total_cash": 2_000_000,
        "cash_weight": 9.3,
        "total_pnl_pct": 7.5,
    }


def _patch_sources(monkeypatch, pf: dict):
    """analytics가 재사용하는 portfolio/market/performance 데이터 모킹 + 캐시 초기화."""
    monkeypatch.setattr(dd, "portfolio_data", lambda: pf)
    monkeypatch.setattr(
        dd, "market_data",
        lambda: {"indices": {"KOSPI": {"pct": 0.5},
                             "S&P500": {"pct": 0.8},
                             "NASDAQ": {"pct": 1.0}}},
    )
    monkeypatch.setattr(
        dd, "performance_data",
        lambda days=30: {"summary": {"win_rate": 60, "avg_pnl": 1.2,
                                     "total": 10, "wins": 6, "losses": 4}},
    )
    dd._cache.clear()


def _row(rows, ticker):
    return next(r for r in rows if r["ticker"] == ticker)


# 1) 종목별 pnl_krw / weight / contribution_pct 계산 검증
def test_contribution_per_holding(monkeypatch):
    _patch_sources(monkeypatch, _sample_pf())
    a = dd._fetch_portfolio_analytics_raw()

    assert a["total_pnl_krw"] == 1_500_000

    tiger = _row(a["contributors"], "360750.KS")
    assert tiger["pnl_krw"] == 2_000_000
    assert tiger["cost_krw"] == 8_000_000
    assert abs(tiger["weight"] - 46.5) < 0.2          # 10M / 21.5M
    assert abs(tiger["contribution_pct"] - 133.3) < 0.2  # 2M / 1.5M

    shift = _row(a["contributors"], "462870.KS")
    assert shift["pnl_krw"] == -1_000_000
    assert abs(shift["contribution_pct"] - (-66.7)) < 0.2

    # 일간 기여도 = weight * day_pct / 100
    assert abs(tiger["day_contribution_pct"] - (tiger["weight"] * 2.0 / 100)) < 0.01


# 2) top_contributors / bottom_contributors 정렬 검증
def test_top_bottom_contributors_sorted(monkeypatch):
    _patch_sources(monkeypatch, _sample_pf())
    a = dd._fetch_portfolio_analytics_raw()

    top = a["top_contributors"]
    bottom = a["bottom_contributors"]
    assert top[0]["ticker"] == "360750.KS"   # +2,000,000 최상위
    assert bottom[0]["ticker"] == "462870.KS"  # -1,000,000 최하위

    assert [r["pnl_krw"] for r in top] == sorted([r["pnl_krw"] for r in top], reverse=True)
    assert [r["pnl_krw"] for r in bottom] == sorted([r["pnl_krw"] for r in bottom])


# 3) 계좌별 합계가 종목+현금과 일치
def test_account_totals_match_holdings(monkeypatch):
    pf = _sample_pf()
    _patch_sources(monkeypatch, pf)
    a = dd._fetch_portfolio_analytics_raw()

    by_name = {acc["name"]: acc for acc in a["accounts"]}
    for src in pf["accounts"]:
        acc = by_name[src["name"]]
        items_eval = sum(it["eval_krw"] for it in src["items"])
        assert acc["eval_total"] == items_eval
        assert acc["pnl_krw"] == acc["eval_total"] - acc["cost_total"]
        assert acc["cash"] == src["cash"]


# 4) 현금 비중 계산 검증
def test_cash_weight(monkeypatch):
    _patch_sources(monkeypatch, _sample_pf())
    a = dd._fetch_portfolio_analytics_raw()

    assert a["total_cash"] == 2_000_000
    assert abs(a["cash_weight"] - 9.3) < 0.2
    assert abs(a["concentration"]["cash_weight"] - 9.3) < 0.2
    cash_cls = next(c for c in a["asset_classes"] if c["name"] == "현금")
    assert cash_cls["value"] == 2_000_000
    assert cash_cls["pnl_krw"] == 0  # 현금은 평가손익 없음


# 5) 전체 손익이 0일 때 contribution_pct 0 처리
def test_zero_total_pnl_contribution(monkeypatch):
    pf = {
        "accounts": [{
            "name": "일반", "cash": 0,
            "eval_total": 9_000_000, "cost_total": 9_000_000,
            "pnl_pct": 0.0, "weight": 100.0,
            "items": [
                {"ticker": "AAA", "name": "에이", "eval_krw": 5_000_000,
                 "pnl_pct": 25.0, "day_pct": 0.0},   # +1,000,000
                {"ticker": "BBB", "name": "비", "eval_krw": 4_000_000,
                 "pnl_pct": -20.0, "day_pct": 0.0},  # -1,000,000
            ],
        }],
        "total_eval": 9_000_000, "total_cash": 0,
        "cash_weight": 0.0, "total_pnl_pct": 0.0,
    }
    _patch_sources(monkeypatch, pf)
    a = dd._fetch_portfolio_analytics_raw()

    assert a["total_pnl_krw"] == 0
    assert all(r["contribution_pct"] == 0.0 for r in a["contributors"])


# 6) MU 보호 라벨 — 보유 관리 · 실행 매도 아님
def test_protected_stock_label(monkeypatch):
    _patch_sources(monkeypatch, _sample_pf())
    a = dd._fetch_portfolio_analytics_raw()

    protected = [f for f in a["risk_flags"] if f.get("type") == "protected"]
    assert any(f["ticker"] == "MU" for f in protected)
    mu_flag = next(f for f in protected if f["ticker"] == "MU")
    assert "실행 매도 아님" in mu_flag["message"]

    # 보호 종목은 비중 25%↑여도 집중 경고로 매도 압박하지 않음
    conc_flags = [f for f in a["risk_flags"]
                  if f.get("type") == "concentration" and f.get("ticker") == "MU"]
    assert conc_flags == []

    # 기여도 표에는 그대로 표시(보유 관리 성격 유지)
    mu_row = _row(a["contributors"], "MU")
    assert mu_row["protected"] is True

    # 요약 텍스트에도 보호 종목 명시
    monkeypatch.setattr(dd, "portfolio_analytics", dd._fetch_portfolio_analytics_raw)
    s = dd.portfolio_contribution_summary()
    assert "보호 종목" in s["text"]
    assert "실행 매도 아님" in s["text"]


# 7) read-only 보장 — DB write/POST 없음
def test_read_only_guarantees():
    from pathlib import Path
    root = Path(__file__).parent.parent

    # web/app.py에 POST/PUT/DELETE 핸들러 추가 없음
    app_code = (root / "web" / "app.py").read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "DELETE"):
        assert verb not in app_code, f"{verb} 핸들러 발견 — read-only 위반"

    # dashboard_data.py에 쓰기 SQL 없음
    dd_code = (root / "core" / "dashboard_data.py").read_text(encoding="utf-8")
    for kw in ("INSERT", "UPDATE ", "DELETE FROM", "DROP ", "CREATE TABLE"):
        assert kw not in dd_code, f"쓰기 SQL '{kw}' 발견 — read-only 위반"


def test_analytics_does_not_write_db(empty_db, monkeypatch):
    """portfolio analytics 호출 후 predictions 행 수가 변하지 않음(DB write 없음)."""
    import sqlite3 as _sq
    before = _sq.connect(empty_db).execute(
        "SELECT COUNT(*) FROM predictions").fetchone()[0]

    _patch_sources(monkeypatch, _sample_pf())
    dd._fetch_portfolio_analytics_raw()
    monkeypatch.setattr(dd, "portfolio_analytics", dd._fetch_portfolio_analytics_raw)
    dd.portfolio_contribution_summary()

    after = _sq.connect(empty_db).execute(
        "SELECT COUNT(*) FROM predictions").fetchone()[0]
    assert before == after == 0


# ═══════════════════════════════════════════════════════
# PC 다크 투자 터미널 (4단계) — index_pc.html 정적 검증
# ═══════════════════════════════════════════════════════
def _pc_html() -> str:
    from pathlib import Path
    return (Path(__file__).parent.parent / "web" / "index_pc.html").read_text(encoding="utf-8")


# 4-1) 다크 테마 마커 유지
def test_pc_dark_theme_markers():
    html = _pc_html()
    assert "다크 PC 투자 터미널" in html, "다크 터미널 주석/식별자 없음"
    assert ("#0a0e17" in html or "#070b12" in html), "다크 배경 변수 없음"
    assert ("#111a2b" in html or "#101826" in html), "다크 패널 변수 없음"


# 4-2) 라이트 회귀 방지 — 라이트 배경이 주요 변수로 쓰이지 않음
def test_pc_no_light_regression():
    html = _pc_html()
    assert "#f5f6f8" not in html, "라이트 배경 #f5f6f8 회귀"
    # #ffffff가 배경 변수(--bg/--panel)로 선언되지 않았는지 확인
    import re
    bad = re.findall(r"--(?:bg|bg2|panel)\s*:\s*#ffffff", html, re.IGNORECASE)
    assert not bad, f"라이트 배경 변수 회귀: {bad}"


# 4-3) PC 홈에 portfolio analytics 필드 사용
def test_pc_uses_analytics_fields():
    html = _pc_html()
    for field in ("top_contributors", "bottom_contributors",
                  "risk_flags", "concentration", "asset_classes"):
        assert field in html, f"analytics 필드 미사용: {field}"


# 4-4) MU/보호종목 문구 확인
def test_pc_protected_phrasing():
    html = _pc_html()
    assert "보유 관리" in html, "'보유 관리' 문구 없음"
    assert "실행 매도 아님" in html, "'실행 매도 아님' 문구 없음"


# 4-5) read-only — 주문 버튼/POST 없음
def test_pc_read_only():
    html = _pc_html()
    assert "POST" not in html, "PC HTML에 POST 존재 — read-only 위반"
    from pathlib import Path
    app_code = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "DELETE"):
        assert verb not in app_code, f"app.py에 {verb} 핸들러 — read-only 위반"


# 4-6) 모바일 index.html 미변경(불필요한 대규모 변경 없음)
def test_mobile_html_untouched():
    import subprocess
    from pathlib import Path
    root = Path(__file__).parent.parent
    diff = subprocess.run(
        ["git", "diff", "--stat", "HEAD", "--", "web/index.html"],
        cwd=root, capture_output=True, text=True,
    ).stdout
    assert diff.strip() == "", f"모바일 index.html 변경 감지:\n{diff}"
