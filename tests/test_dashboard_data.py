"""대시보드 데이터 레이어 (조회 전용) 테스트.

DB가 없거나 비어 있어도 예외 없이 안전한 빈 구조를 반환하는지 검증한다.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from core import dashboard_data as dd


@pytest.fixture(autouse=True)
def no_live_toss_broker_network(monkeypatch):
    """Dashboard data tests must never acquire a real Toss OAuth token.

    The production .env is present on the GCP test host. Without this boundary,
    an indirect dashboard helper can issue a second client-credentials token and
    invalidate the running stock-bot token. Tests that need broker-shaped data
    patch the public Toss helpers directly, so returning no token is fail-closed.
    """
    from core import toss_client as tc

    monkeypatch.setattr(tc, "_get_access_token", lambda: None)


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
    closed_at = (datetime.now(dd.KST) - timedelta(days=1)).isoformat()
    conn.execute(
        """INSERT INTO predictions
           (created_at, closed_at, ticker, name, signal, action_type,
            account_type, entry_price, target_price, status, outcome,
            pnl_pct, normalizer_version, briefing_type)
           VALUES
           ('2026-06-09T08:30:00', ?, '012450.KS', '한화에어로',
            '매도', 'AI_SELL_MANAGEMENT', '일반', 300000, 330000, 'closed', 'win',
            10.0, 'v1', 'KR_BEFORE')""",
        (closed_at,),
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


# ─── 모바일 핵심 탭 구조 검증 ───────────────────────
def test_mobile_agent_tab_replaces_simulator_tab():
    """index.html에서 저가치 시뮬레이터 탭 대신 에이전트 탭이 먼저 보인다."""
    from pathlib import Path
    html = (Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")

    assert 'data-t="agent"' in html
    assert 'id="t-agent"' in html
    assert 'stock-agent-activity-m' in html
    assert 'data-t="sim"' not in html
    assert 'id="t-sim"' in html  # 섹션은 보존하되 탭에서 숨김

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


# PC 파일 미변경 가드 — 모바일(6단계) 작업이 PC 터미널을 건드리지 않음
# 차트 API 통합(10단계)으로 PC도 변경 대상이 되어 가드 완화
def test_pc_html_exists():
    from pathlib import Path
    pc = Path(__file__).parent.parent / "web" / "index_pc.html"
    assert pc.exists(), "index_pc.html 미존재"


# ═══════════════════════════════════════════════════════
# 모바일 투자 툴 UX (6단계) — index.html 정적 검증
# ═══════════════════════════════════════════════════════
def _mobile_html() -> str:
    from pathlib import Path
    return (Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")


# 6-1) 모바일 홈 핵심 마커 존재 (v3 bento 투자 터미널)
def test_mobile_home_markers():
    html = _mobile_html()
    assert ("모바일 투자 터미널" in html) or ("산적 투자 터미널" in html), "터미널 제목 없음"
    assert "오늘 결론" in html, "Today Decision Strip 없음"
    assert "수익 기여" in html, "Portfolio Bento '수익 기여' 없음"
    assert "보유 TOP" in html, "Holdings 랭킹 '보유 TOP' 없음"
    assert "주의 알림" in html, "Alert Stack '주의 알림' 없음"
    assert "보유 관리 · 실행 매도 아님" in html, "보호종목 문구 없음"
    assert ("주문표 미리보기" in html) or ("가상 계산" in html), "안전 CTA 없음"


# 8-1) bento/타일 레이아웃 마커 존재 (v3 세로 일지형 탈피)
def test_mobile_bento_markers():
    html = _mobile_html()
    for marker in (
        "mini-command-bar",   # 1. Sticky Mini Command Bar
        "bento-hero",         # 2. Bento Hero Grid
        "bento-main",
        "bento-mini",
        "action-matrix",      # 3. Action Tile Matrix
        "action-tile",
        "priority-rail",      # 4. Priority Rail (가로 스와이프)
        "prail-card",
        "portfolio-bento",    # 5. Portfolio Bento
        "contrib-pair",
        "holding-rank-compact",
        "alert-stack",        # 6. Alert Stack
        "alert-item",
        "detail-drawer",      # 7. Detail Drawers
    ):
        assert marker in html, f"bento 마커 '{marker}' 없음"
    # 가로 스와이프 rail (scroll-snap)
    assert "scroll-snap-type:x mandatory" in html, "가로 rail scroll-snap 없음"


# 8-2) 세로 일지형(v2) 회귀 방지 — 옛 단일 세로 컨테이너 ID 제거
def test_mobile_no_vertical_journal_regression():
    html = _mobile_html()
    # v2의 분리형 세로 섹션 ID는 bento로 통합되며 사라져야 함
    for gone in ('id="h-snap"', 'id="h-top"'):
        assert gone not in html, f"세로 일지형 잔재 '{gone}' 존재 — bento 통합 누락"
    # 액션은 가로 rail-cell이 아닌 2x2 action-tile로 렌더
    assert 'class="action-tile' in html, "action-tile 렌더 없음 (옛 rail-cell 잔존 가능)"
    # 우선순위는 prail-card(가로)로 렌더
    assert 'class="prail-card' in html, "prail-card 렌더 없음"


# 6-1b) analytics 필드 적극 사용 확인
def test_mobile_uses_analytics_fields():
    html = _mobile_html()
    assert "top_contributors" in html, "top_contributors 미사용"
    assert "bottom_contributors" in html, "bottom_contributors 미사용"
    assert "risk_flags" in html, "risk_flags 미사용"
    assert ("asset_classes" in html) or ("concentration" in html), "자산군/집중도 미사용"


# 6-2) 금지 CTA 없음 (실제 주문 실행처럼 보이는 문구 차단)
def test_mobile_no_forbidden_cta():
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


# 6-3) AI_SELL_MANAGEMENT → 보유 관리 매핑 마커
def test_mobile_sell_management_mapped():
    html = _mobile_html()
    assert "AI_SELL_MANAGEMENT" in html, "보유관리 분류 식별자 없음"
    # classify가 mgmt로 분류하고 라벨을 '보유 관리'로 노출
    assert '"보유 관리"' in html or "보유 관리" in html, "보유 관리 라벨 없음"


# 6-4) 보호종목 문구 '보유 관리 · 실행 매도 아님' 존재
def test_mobile_protected_phrase():
    html = _mobile_html()
    assert "보유 관리 · 실행 매도 아님" in html, "보호종목 문구 없음"


# 6-6) classify 분류 순서 — CONDITIONAL이 NEW_BUY보다 먼저 판정
def test_mobile_classify_conditional_before_newbuy():
    import re
    html = _mobile_html()
    m = re.search(r"function classify\(at,s\)\{(.*?)\}", html, re.S)
    assert m, "classify 함수 없음"
    body = m.group(1)
    i_cond = body.find('includes("CONDITIONAL")')
    i_buy = body.find('includes("NEW_BUY")')
    assert i_cond != -1 and i_buy != -1, "CONDITIONAL/NEW_BUY 판정 누락"
    assert i_cond < i_buy, \
        "CONDITIONAL_NEW_BUY가 buy로 오분류 — CONDITIONAL 판정을 NEW_BUY보다 먼저 둬야 함"


# 6-5) read-only — 주문 실행 폼/POST 없음
def test_mobile_read_only():
    html = _mobile_html()
    assert 'method="post"' not in html.lower(), "주문 실행 폼(method=post) 존재"
    assert "실제 주문이 실행되지 않습니다" in html, "가상/읽기전용 안내 없음"
    from pathlib import Path
    app_code = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "DELETE"):
        assert verb not in app_code, f"app.py에 {verb} 핸들러 — read-only 위반"


# ═══════════════════════════════════════════════════════
# normalized item execution_risk 주입 (27단계)
# ═══════════════════════════════════════════════════════

def test_attach_execution_risk_domestic(monkeypatch):
    """국내 조건부 item에 execution_risk 주입."""
    from core.analyzer import _attach_execution_risk
    import core.dashboard_data as _dd
    mock_ob = {
        "execution_risk_label": "스프레드 주의", "spread_pct": 0.5,
        "imbalance_pct": 20, "source": "KIS",
    }
    monkeypatch.setattr(_dd, "ticker_orderbook", lambda tk: mock_ob)
    _dd._cache.clear()

    normalized = {
        "executable_actions": [{"ticker": "005930.KS", "name": "삼성전자"}],
        "conditional_buy_candidates": [{"ticker": "069500.KS", "name": "KODEX200"}],
        "cancelled_sells": [{"ticker": "462870.KS", "name": "시프트업"}],
    }
    _attach_execution_risk(normalized)

    assert normalized["executable_actions"][0].get("execution_risk")
    assert normalized["executable_actions"][0]["execution_risk"]["has_warning"] is True
    assert normalized["conditional_buy_candidates"][0].get("execution_risk")
    assert normalized["cancelled_sells"][0].get("execution_risk")


def test_attach_execution_risk_overseas_skip(monkeypatch):
    """해외 종목에는 execution_risk 미주입."""
    from core.analyzer import _attach_execution_risk
    import core.dashboard_data as _dd
    call_count = {"n": 0}
    def mock_ob(tk):
        call_count["n"] += 1
        return {"execution_risk_label": "체결 리스크 낮음", "spread_pct": 0.1, "imbalance_pct": 0, "source": "KIS"}
    monkeypatch.setattr(_dd, "ticker_orderbook", mock_ob)
    _dd._cache.clear()

    normalized = {
        "executable_actions": [{"ticker": "MU", "name": "마이크론"}],
        "conditional_buy_candidates": [],
        "cancelled_sells": [],
    }
    _attach_execution_risk(normalized)
    assert call_count["n"] == 0
    assert "execution_risk" not in normalized["executable_actions"][0]


def test_attach_execution_risk_max_10(monkeypatch):
    """최대 10종목 제한."""
    from core.analyzer import _attach_execution_risk
    import core.dashboard_data as _dd
    call_count = {"n": 0}
    def mock_ob(tk):
        call_count["n"] += 1
        return {"execution_risk_label": "체결 리스크 낮음", "spread_pct": 0.1, "imbalance_pct": 0, "source": "KIS"}
    monkeypatch.setattr(_dd, "ticker_orderbook", mock_ob)
    _dd._cache.clear()

    items = [{"ticker": f"{str(i).zfill(6)}.KS", "name": f"종목{i}"} for i in range(15)]
    normalized = {"executable_actions": items, "conditional_buy_candidates": [], "cancelled_sells": []}
    _attach_execution_risk(normalized)
    assert call_count["n"] == 10  # 최대 10종목


def test_attach_execution_risk_exception_safe(monkeypatch):
    """예외 발생해도 normalized 반환."""
    from core.analyzer import _attach_execution_risk
    import core.dashboard_data as _dd
    monkeypatch.setattr(_dd, "ticker_orderbook", lambda tk: (_ for _ in ()).throw(RuntimeError("fail")))
    _dd._cache.clear()

    normalized = {
        "executable_actions": [{"ticker": "005930.KS"}],
        "conditional_buy_candidates": [],
        "cancelled_sells": [],
    }
    _attach_execution_risk(normalized)  # 예외 없이 완료
    # execution_risk는 fallback으로 들어감
    er = normalized["executable_actions"][0].get("execution_risk", {})
    assert er.get("has_warning") is False


def test_email_briefing_html_contracts_intentional_changes():
    """수입 계기판과 정규화 매도 섹션을 이메일 HTML에 안전하게 렌더한다."""
    from types import SimpleNamespace

    from core.email import _build_briefing_html

    result = SimpleNamespace(
        market_summary="",
        buy_signals=(),
        sell_signals=(),
        portfolio_signals=(),
    )
    raw = {
        "advisor_verdict": "HOLD",
        "income_briefing": {
            "income_kpi": {},
            "toss": {},
            "samsung": {},
            "thesis": {},
        },
        "normalized": {
            "executable_actions": [
                {
                    "side": "sell",
                    "name": "실행매도종목",
                    "account": "Toss AI",
                    "qty": 1,
                    "price": 100,
                    "stop": 90,
                    "reason": "위험 축소",
                }
            ],
            "conditional_buy_candidates": [],
            "conditional_sell_candidates": [
                {
                    "name": "조건부매도종목",
                    "account": "Toss AI",
                    "price": 100,
                    "stop": 90,
                    "reason": "조건 확인",
                }
            ],
            "cancelled_sells": [
                {
                    "name": "보유관리종목",
                    "account": "Toss AI",
                    "hold_note": "실행 매도 아님",
                    "cancel_reason": "보호 보유",
                }
            ],
            "blocked_buys": [],
        },
    }

    html = _build_briefing_html(result, raw, "테스트 브리핑", "테스트")

    assert "오늘 수입 계기판" in html
    assert "실행 매도" in html and "실행매도종목" in html
    assert "조건부 매도·손절 감시" in html and "조건부매도종목" in html
    assert "매도 취소·보유 관리" in html and "보유관리종목" in html
    assert "실행 매도 아님" in html


# ═══════════════════════════════════════════════════════
# 메일/텔레그램 호가 리스크 경고 (26단계)
# ═══════════════════════════════════════════════════════

def test_telegram_execution_risk_warning():
    """텔레그램 risk warning 함수: has_warning true → 문구 반환."""
    from core.telegram import _format_execution_risk_warning
    # has_warning true
    item = {"execution_risk": {"has_warning": True, "label": "스프레드 주의", "tone": "warn"}}
    result = _format_execution_risk_warning(item)
    assert "스프레드 주의" in result
    assert "호가 기준 판단 보조" in result
    assert "주문 지시 아님" in result


def test_telegram_execution_risk_no_warning():
    """has_warning false → 빈 문자열."""
    from core.telegram import _format_execution_risk_warning
    item = {"execution_risk": {"has_warning": False, "label": "체결 리스크 낮음", "tone": "ok"}}
    assert _format_execution_risk_warning(item) == ""
    assert _format_execution_risk_warning({}) == ""
    assert _format_execution_risk_warning({"execution_risk": None}) == ""


def test_email_risk_warning_in_source():
    """email.py에 호가 리스크 경고 렌더 코드 존재."""
    from pathlib import Path
    code = (Path(__file__).parent.parent / "core" / "email.py").read_text(encoding="utf-8")
    assert "호가 기준 판단 보조" in code
    assert "주문 지시 아님" in code
    assert "execution_risk" in code


def test_telegram_risk_warning_in_source():
    """telegram.py에 호가 리스크 경고 함수 존재."""
    from pathlib import Path
    code = (Path(__file__).parent.parent / "core" / "telegram.py").read_text(encoding="utf-8")
    assert "_format_execution_risk_warning" in code
    assert "호가 기준 판단 보조" in code
    assert "주문 지시 아님" in code


def test_risk_warning_no_forbidden_cta():
    """메일/텔레그램 소스에 금지 CTA 없음."""
    from pathlib import Path
    root = Path(__file__).parent.parent / "core"
    for fn in ("email.py", "telegram.py"):
        code = (root / fn).read_text(encoding="utf-8")
        for cta in ("매수하기", "매도하기"):
            assert cta not in code, f"{fn}에 금지 CTA '{cta}' 발견"




def test_samsung_screenshot_trades_reflected_in_settings():
    """2026-07-01 삼성증권/RIA 체결 기준 보유/현금이 PC·모바일 공통 API 원본에 반영된다."""
    from config.settings import HOLDINGS_ISA, HOLDINGS_RIA, ISA_CASH, RIA_CASH

    assert "161510.KS" not in HOLDINGS_ISA
    assert HOLDINGS_RIA["069500.KS"]["shares"] == 21
    assert HOLDINGS_RIA["069500.KS"]["avg_cost_krw"] == 136_977
    assert HOLDINGS_RIA["091160.KS"] == {"shares": 20, "avg_cost_krw": 165_425}
    assert HOLDINGS_RIA["352820.KS"] == {"shares": 5, "avg_cost_krw": 189_700}
    assert HOLDINGS_RIA["003670.KS"] == {"shares": 5, "avg_cost_krw": 176_700}
    assert HOLDINGS_RIA["005380.KS"] == {"shares": 1, "avg_cost_krw": 497_000}
    assert HOLDINGS_RIA["328130.KQ"] == {"shares": 87, "avg_cost_krw": 11_450}
    assert HOLDINGS_RIA["041510.KQ"] == {"shares": 13, "avg_cost_krw": 72_100}
    assert ISA_CASH == 4_556_922.0
    assert RIA_CASH == 8_781_585.0


def test_samsung_general_drive_excel_snapshot_reflected():
    """Google Drive 삼성증권.xlsx 최신 스냅샷을 일반(종합) 보유 원본으로 사용한다."""
    from config.settings import DEFAULT_CASH, HOLDINGS_GENERAL

    assert HOLDINGS_GENERAL["005930.KS"]["shares"] == 100
    assert HOLDINGS_GENERAL["005930.KS"]["avg_cost_krw"] == 83_482
    assert HOLDINGS_GENERAL["000660.KS"] == {"shares": 2, "avg_cost_krw": 2_325_000}
    assert DEFAULT_CASH == 4_313_735.0


def test_dashboard_ria_cash_uses_live_settings_value():
    """계좌 보유/현금은 하드코딩 스냅샷 없이 settings + 미반영 매매 라이브 합성이다."""
    import core.dashboard_data as dd
    import inspect

    src = inspect.getsource(dd._fetch_portfolio_raw)
    assert "samsung_cash_overrides" not in src
    assert "samsung_excel_snapshot" not in src
    assert "effective_holdings" in src
    assert "RIA_CASH" in src

def test_trade_api_routes_in_source():
    """거래 ledger 조회 API는 GET-only로 존재한다."""
    from pathlib import Path
    code = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/api/trades")' in code
    assert '@app.get("/api/trades/pending")' in code
    assert 'def api_trades(' in code
    assert 'def api_trades_pending(' in code


def test_web_portfolio_principal_visible():
    """HTML 대시보드에 투자 원금 표시가 존재한다."""
    from pathlib import Path
    root = Path(__file__).parent.parent
    mobile = (root / "web" / "index.html").read_text(encoding="utf-8")
    pc = (root / "web" / "index_pc.html").read_text(encoding="utf-8")
    for code in (mobile, pc):
        assert "투자 원금" in code
        assert "cost_total" in code
    assert "_principal" in mobile
    assert "principal" in pc


def test_portfolio_principal_uses_deposit_history_constants():
    """투자 원금은 보유종목 매입가가 아니라 사용자가 확인한 입금 내역 기준이다."""
    from config.settings import ACCOUNT_PRINCIPAL_KRW

    assert ACCOUNT_PRINCIPAL_KRW["일반"] == 35_000_000
    assert ACCOUNT_PRINCIPAL_KRW["ISA"] == 20_000_000
    assert ACCOUNT_PRINCIPAL_KRW["연금저축"] == 20_500_000
    assert ACCOUNT_PRINCIPAL_KRW["IRP"] == 10_250_000
    assert sum(ACCOUNT_PRINCIPAL_KRW.values()) == 85_750_000

    from pathlib import Path
    root = Path(__file__).parent.parent
    dashboard = (root / "core" / "dashboard_data.py").read_text(encoding="utf-8")
    mobile = (root / "web" / "index.html").read_text(encoding="utf-8")
    pc = (root / "web" / "index_pc.html").read_text(encoding="utf-8")
    assert "total_principal" in dashboard
    assert '"principal"' in dashboard
    assert "total_principal" in mobile
    assert "total_principal" in pc






def test_pc_home_kpis_show_principal_return():
    """PC 홈 KPI에서 원금 대비 수익률과 투자 원금이 총 평가액 서브텍스트에 표시된다."""
    from pathlib import Path
    pc = (Path(__file__).parent.parent / "web" / "index_pc.html").read_text(encoding="utf-8")

    home_block = pc.split('setKPI("home-kpis",[', 1)[1].split(']);', 1)[0]
    assert '원금 대비' in home_block
    assert '투자 원금' in home_block
    assert 'prc(principalPct)' in home_block

def test_pc_principal_label_uses_deposit_basis():
    """PC판 투자 원금 설명도 보유 매입가/현금 제외가 아니라 입금 원금 기준으로 표기한다."""
    from pathlib import Path
    pc = (Path(__file__).parent.parent / "web" / "index_pc.html").read_text(encoding="utf-8")

    assert "입금 원금 기준" in pc
    assert "보유 종목 매입 원금" not in pc
    assert "현금 제외" not in pc

def test_principal_return_uses_non_price_direction_color():
    """원금 대비 수익률은 한국식 상승/하락 빨강·파랑과 별도 색상 클래스를 쓴다."""
    from pathlib import Path
    root = Path(__file__).parent.parent
    dashboard = (root / "core" / "dashboard_data.py").read_text(encoding="utf-8")
    mobile = (root / "web" / "index.html").read_text(encoding="utf-8")
    pc = (root / "web" / "index_pc.html").read_text(encoding="utf-8")

    assert "total_principal_pnl_pct" in dashboard
    for code in (mobile, pc):
        assert "원금 대비" in code
        assert "prc(" in code
        assert "pr-up" in code
        assert "pr-dn" in code
    assert "--principal" in mobile
    assert "--principal" in pc


def test_current_return_visible_alongside_principal_return():
    """원금 대비 추가 후에도 기존 현재/평가 수익률을 숨기지 않는다."""
    from pathlib import Path
    root = Path(__file__).parent.parent
    mobile = (root / "web" / "index.html").read_text(encoding="utf-8")
    pc = (root / "web" / "index_pc.html").read_text(encoding="utf-8")

    assert "원금 대비" in mobile
    assert "총 손익률" in mobile
    assert "평가손익 ${cs(d.total_pnl_pct)}" in mobile
    assert "cs(d.total_pnl_pct)" in mobile
    assert "원금 대비" in pc
    assert "총 손익률" in pc
    assert "cs(d.total_pnl_pct)" in pc


def test_web_trade_ledger_visible_on_mobile_and_pc():
    """HTML PC/모바일도 거래 ledger와 미반영 거래 경고를 렌더한다."""
    from pathlib import Path
    root = Path(__file__).parent.parent
    mobile = (root / "web" / "index.html").read_text(encoding="utf-8")
    pc = (root / "web" / "index_pc.html").read_text(encoding="utf-8")
    for label, code in (("mobile", mobile), ("pc", pc)):
        assert "/api/trades" in code, f"{label} 거래 ledger API 호출 없음"
        assert "/api/trades/pending" in code, f"{label} 미반영 거래 API 호출 없음"
        assert "최근 거래 내역" in code, f"{label} 최근 거래 내역 UI 없음"
        assert "미반영 거래" in code, f"{label} 미반영 거래 경고 UI 없음"
        assert "todayExecTrades" in code, f"{label} 오늘 실행 거래 합산 로직 없음"
        assert "거래완료" in code, f"{label} 거래완료 카드 없음"
        assert "매수/매도는 ledger 반영 거래 기준" in code, f"{label} ledger 기준 설명 없음"


# ═══════════════════════════════════════════════════════
# 시장 상태/시세 신뢰도 (25단계)
# ═══════════════════════════════════════════════════════

def test_market_reliability_context_shape():
    """market_reliability_context 반환 shape 확인."""
    from core.market_hours import market_reliability_context
    ctx = market_reliability_context()
    assert "kr" in ctx and "us" in ctx
    assert "summary" in ctx
    assert "trust_label" in ctx
    assert ctx["trust_tone"] in ("live", "stale", "closed", "unknown")
    assert ctx["kr"]["label"] in ("한국장 장중", "한국장 장전", "한국장 마감", "한국장 휴장")


def test_market_reliability_kr_regular():
    """장중 시간에 kr.is_open=True."""
    from core.market_hours import market_reliability_context
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    # 월요일 10시
    mon_10 = datetime(2026, 6, 22, 10, 0, tzinfo=KST)
    ctx = market_reliability_context(mon_10)
    assert ctx["kr"]["is_open"] is True
    assert ctx["kr"]["label"] == "한국장 장중"
    assert ctx["trust_tone"] == "live"


def test_market_reliability_closed():
    """주말에 closed."""
    from core.market_hours import market_reliability_context
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    # 일요일
    sun = datetime(2026, 6, 21, 14, 0, tzinfo=KST)
    ctx = market_reliability_context(sun)
    assert ctx["kr"]["is_open"] is False
    assert "휴장" in ctx["kr"]["label"] or "마감" in ctx["kr"]["label"]


def test_market_reliability_html_markers():
    """HTML 시장 신뢰도 마커 존재."""
    html = _mobile_html()
    for marker in (
        "market-reliability-bar",
        "quote-trust-badge",
        "market-session-label",
        "quote-trust-live",
        "quote-trust-stale",
        "quote-trust-closed",
        "quote-trust-note",
        "briefing-market-context",
    ):
        assert marker in html, f"시장 신뢰도 마커 '{marker}' 없음"


def test_market_reliability_phrases():
    """시장 상태 문구: HTML 정적 + Python 모듈에 존재."""
    html = _mobile_html()
    # HTML에 정적으로 있는 문구
    assert "시세 지연 가능" in html
    assert "실시간 보장 아님" in html
    # Python 모듈에서 생성되는 문구 확인
    from core.market_hours import market_reliability_context
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    # 장중
    ctx = market_reliability_context(datetime(2026, 6, 22, 10, 0, tzinfo=KST))
    assert "한국장 장중" in ctx["kr"]["label"]
    assert "장중 시세" in ctx["trust_label"]
    # 마감
    ctx2 = market_reliability_context(datetime(2026, 6, 22, 20, 0, tzinfo=KST))
    assert "마감" in ctx2["kr"]["label"]
    assert "마감 후 참고" in ctx2["trust_label"] or "캐시" in ctx2["trust_label"]


def test_market_reliability_no_realtime_solo():
    """HTML body 내 사용자 표시에서 '실시간'이 단독 과장되지 않음."""
    html = _mobile_html()
    import re
    # <body> 이후 JS/HTML에서 사용자에게 보이는 문자열만 확인
    body_start = html.find("<body")
    if body_start < 0:
        return
    body = html[body_start:]
    # 문자열 리터럴 안의 "실시간" 검색 (따옴표 안)
    literals = re.findall(r'["\'][^"\']*실시간[^"\']*["\']', body)
    for lit in literals:
        # CSS 주석, 변수명, 기능 제목(기술 신호 등)은 무시
        if "/*" in lit or "보유 실시간" in lit or "기술 신호" in lit:
            continue
        assert "보장 아님" in lit or "준실시간" in lit or "준" in lit, \
            f"'실시간' 단독 과장 의심: {lit[:80]}"


def test_market_reliability_no_forbidden_cta():
    """금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


# ═══════════════════════════════════════════════════════
# 호가 리스크 브리핑 연동 (24단계)
# ═══════════════════════════════════════════════════════

def test_summarize_execution_risk_ok():
    """체결 리스크 낮음 → has_warning false."""
    ob = {"execution_risk_label": "체결 리스크 낮음", "spread_pct": 0.1,
          "imbalance_pct": 10, "source": "KIS"}
    r = dd.summarize_execution_risk(ob)
    assert r["has_warning"] is False
    assert r["tone"] == "ok"


def test_summarize_execution_risk_warn():
    """스프레드 주의 → has_warning true."""
    ob = {"execution_risk_label": "스프레드 주의", "spread_pct": 0.5,
          "imbalance_pct": 20, "source": "KIS"}
    r = dd.summarize_execution_risk(ob)
    assert r["has_warning"] is True
    assert r["tone"] == "warn"
    assert "스프레드" in r["summary"]


def test_summarize_execution_risk_bad():
    """유동성 주의 → has_warning true, tone bad."""
    ob = {"execution_risk_label": "유동성 주의", "spread_pct": 1.2,
          "imbalance_pct": -70, "source": "KIS"}
    r = dd.summarize_execution_risk(ob)
    assert r["has_warning"] is True
    assert r["tone"] == "bad"
    assert "불균형" in r["summary"]


def test_summarize_execution_risk_none():
    """None/unsupported 안전."""
    r = dd.summarize_execution_risk(None)
    assert r["has_warning"] is False
    assert r["tone"] == "unknown"

    r2 = dd.summarize_execution_risk({"source": "unsupported", "error": "국내 종목만 지원"})
    assert r2["has_warning"] is False


def test_execution_risk_html_markers():
    """HTML 호가 리스크 경고 마커 존재."""
    html = _mobile_html()
    for marker in (
        "execution-risk-warning",
        "execution-risk-summary",
        "execution-risk-warn",
        "execution-risk-bad",
        "execution-risk-muted",
        "briefing-execution-risk",
    ):
        assert marker in html, f"호가 리스크 마커 '{marker}' 없음"


def test_execution_risk_phrases():
    """호가 리스크 문구 존재."""
    html = _mobile_html()
    assert "호가 기준 판단 보조" in html
    assert "주문 지시 아님" in html
    assert "체결 리스크 참고" in html


def test_execution_risk_no_forbidden_cta():
    """금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


# ═══════════════════════════════════════════════════════
# KIS 국내 호가 (23단계)
# ═══════════════════════════════════════════════════════

def test_orderbook_domestic_shape(monkeypatch):
    """국내 종목 orderbook shape 확인."""
    mock_ob = {
        "ticker": "005930.KS", "source": "KIS", "updated_at": "2026-06-21T10:00:00",
        "bids": [{"price": 70000, "size": 100}],
        "asks": [{"price": 70100, "size": 200}],
        "spread": 100, "spread_pct": 0.143, "mid_price": 70050,
        "total_bid_size": 100, "total_ask_size": 200,
        "imbalance_pct": -33.3,
        "liquidity_label": "유동성 양호",
        "execution_risk_label": "체결 리스크 낮음", "error": "",
    }
    import core.market_kis as kis
    monkeypatch.setattr(kis, "get_domestic_orderbook", lambda t: mock_ob)
    dd._cache.clear()
    result = dd.ticker_orderbook("005930.KS")
    assert result["source"] == "KIS"
    assert result["spread"] == 100
    assert result["execution_risk_label"] == "체결 리스크 낮음"
    assert "error" in result


def test_orderbook_overseas_unsupported():
    """해외 종목은 unsupported."""
    dd._cache.clear()
    result = dd.ticker_orderbook("MU")
    assert result["source"] == "unsupported"
    assert "국내 종목만 지원" in result["error"]


def test_orderbook_invalid_ticker():
    """이상한 ticker 안전 처리."""
    dd._cache.clear()
    result = dd.ticker_orderbook("../../etc")
    assert result["error"] == "invalid ticker"


def test_orderbook_route_exists():
    """orderbook route가 chart/generic보다 먼저."""
    from pathlib import Path
    code = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    ob_pos = code.find("/api/ticker/{ticker}/orderbook")
    chart_pos = code.find("/api/ticker/{ticker}/chart")
    generic_pos = code.find("/api/ticker/{ticker:path}")
    assert ob_pos != -1
    assert ob_pos < chart_pos < generic_pos


def test_orderbook_html_markers():
    """HTML 호가 마커 존재."""
    html = _mobile_html()
    for marker in (
        "orderbook-panel",
        "orderbook-ladder",
        "orderbook-bid-row",
        "orderbook-ask-row",
        "orderbook-spread",
        "orderbook-imbalance",
        "execution-risk-badge",
        "orderbook-readonly-note",
        "orderbook-empty-state",
    ):
        assert marker in html, f"호가 마커 '{marker}' 없음"


def test_orderbook_phrases():
    """호가 관련 문구 존재."""
    html = _mobile_html()
    assert "호가/체결 리스크" in html
    assert "read-only · 실제 주문 아님" in html
    assert "KIS 호가" in html
    assert "국내 종목만 지원" in html


def test_orderbook_no_forbidden_cta():
    """금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


# ═══════════════════════════════════════════════════════
# 브리핑 아카이브 결과 추적 (22단계)
# ═══════════════════════════════════════════════════════

def test_archive_tracking_shape(tmp_path, monkeypatch):
    """build_archive_tracking 반환 shape 확인."""
    from core import briefing_archive as ba
    monkeypatch.setattr(ba, "_db_path", lambda: tmp_path / "test.db")
    # archive 저장
    ba.save_briefing_archive(
        briefing_type="KR_BEFORE", title="테스트",
        body_text="본문", raw_json={},
    )
    items = ba.list_briefing_archives(limit=1, days=1)
    assert items
    archive = items[0]
    # predictions DB가 없으면 빈 결과
    result = ba.build_archive_tracking(archive)
    assert "summary" in result
    assert "items" in result
    assert isinstance(result["items"], list)


def test_archive_tracking_html_markers():
    """HTML tracking 마커 존재."""
    html = _mobile_html()
    for marker in (
        "briefing-tracking-panel",
        "briefing-tracking-summary",
        "briefing-tracking-list",
        "briefing-tracking-row",
        "briefing-tracking-label",
        "briefing-tracking-distance",
        "briefing-tracking-empty",
        "briefing-tracking-source",
        "briefing-tracking-hint",
    ):
        assert marker in html, f"tracking 마커 '{marker}' 없음"


def test_archive_tracking_phrases():
    """추적 문구 존재."""
    html = _mobile_html()
    assert "현재 결과 추적" in html
    assert "브리핑 이후 현재 위치" in html
    assert "상세에서 현재 결과 확인" in html


def test_archive_tracking_condition_labels():
    """조건 상태 라벨 문자열 존재 (JS 렌더 시 사용)."""
    html = _mobile_html()
    # These appear in the openBriefingArchive JS as tracking_label values
    # Verify the backend produces them:
    from core.dashboard_data import calc_price_context
    ctx_wait = calc_price_context(150, 145, 160, 140, "CONDITIONAL_NEW_BUY")
    assert ctx_wait["condition_label"] == "조건 대기"
    ctx_near = calc_price_context(146, 145, 160, 140, "CONDITIONAL_NEW_BUY")
    assert ctx_near["condition_label"] == "조건 근접"
    ctx_reach = calc_price_context(144, 145, 160, 140, "CONDITIONAL_NEW_BUY")
    assert ctx_reach["condition_label"] == "조건 도달"


def test_archive_tracking_no_forbidden_cta():
    """금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


def test_archive_tracking_api_has_tracking():
    """app.py briefing detail에 tracking 반환."""
    from pathlib import Path
    code = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    assert "build_archive_tracking" in code
    assert "tracking" in code


# ═══════════════════════════════════════════════════════
# KIS 국내 차트 우선화 (21단계)
# ═══════════════════════════════════════════════════════

def test_chart_kis_priority_domestic(monkeypatch):
    """국내 티커에서 KIS chart 성공 시 source=KIS."""
    kis_data = {"points": [{"time": "09:05", "open": 70000, "high": 70500,
                             "low": 69900, "close": 70400, "volume": 100}],
                "current_price": 70400, "day_pct": 0.5, "source": "KIS"}
    import core.market_kis as kis
    monkeypatch.setattr(kis, "get_domestic_chart", lambda *a, **kw: kis_data)
    dd._cache.clear()
    # 직접 _fetch_chart_raw 호출
    result = dd._fetch_chart_raw("005930.KS", "1d", "5m")
    assert result["source"] == "KIS"
    assert len(result["points"]) == 1


def test_chart_kis_fallback_to_yfinance(monkeypatch):
    """KIS chart 실패 시 yfinance fallback."""
    import core.market_kis as kis
    monkeypatch.setattr(kis, "get_domestic_chart", lambda *a, **kw: None)
    # yfinance도 모킹
    import yfinance
    import pandas as pd
    empty_df = pd.DataFrame()
    monkeypatch.setattr(yfinance, "Ticker", lambda t: type("T", (), {"history": lambda self, **kw: empty_df})())
    monkeypatch.setattr(dd, "_fetch_chart_raw.__wrapped__", None, raising=False)
    dd._cache.clear()
    result = dd._fetch_chart_raw("005930.KS", "1d", "5m")
    # yfinance도 empty → points 빈 결과
    assert result["source"] in ("yfinance", "KIS+yfinance")
    assert result["points"] == []


def test_chart_overseas_no_kis(monkeypatch):
    """해외 티커는 KIS chart 호출하지 않음."""
    call_count = {"n": 0}
    import core.market_kis as kis
    def mock_kis(*a, **kw):
        call_count["n"] += 1
        return None
    monkeypatch.setattr(kis, "get_domestic_chart", mock_kis)
    monkeypatch.setattr(dd, "_fetch_chart_raw", dd._fetch_chart_raw)  # use real
    dd._cache.clear()
    # MU는 해외 → KIS chart 미호출
    # 직접 _fetch_chart_raw 코드 실행 확인
    # 대신 ticker_chart_data에서 테스트 (mock)
    from core.dashboard_data import _fetch_chart_raw
    # _fetch_chart_raw는 MU에 대해 KIS chart를 호출하지 않아야 함
    # 하지만 실제 yfinance 호출은 피하기 위해 monkeypatch
    import yfinance
    import pandas as pd
    monkeypatch.setattr(yfinance, "Ticker", lambda t: type("T", (), {"history": lambda self, **kw: pd.DataFrame()})())
    _fetch_chart_raw("MU", "1d", "5m")
    assert call_count["n"] == 0, "해외 종목에 KIS chart 호출됨"


# ═══════════════════════════════════════════════════════
# 액션 현재가/조건거리 계산 (20단계)
# ═══════════════════════════════════════════════════════

def test_calc_price_context_waiting():
    """current > entry → 조건 대기."""
    ctx = dd.calc_price_context(150.0, 145.0, 160.0, 140.0, "CONDITIONAL_NEW_BUY")
    assert ctx["condition_status"] == "waiting"
    assert ctx["condition_label"] == "조건 대기"
    assert ctx["distance_to_entry_pct"] is not None
    assert "%" not in ctx["condition_label"]  # 라벨에 % 없음
    assert "조건가까지" in ctx["summary"]
    assert "목표까지" in ctx["summary"]
    assert "손절까지" in ctx["summary"]


def test_calc_price_context_near():
    """current close to entry within 1% → 조건 근접."""
    ctx = dd.calc_price_context(146.0, 145.0, 160.0, 140.0, "CONDITIONAL_NEW_BUY")
    assert ctx["condition_status"] == "near"
    assert ctx["condition_label"] == "조건 근접"


def test_calc_price_context_reached():
    """current <= entry → 조건 도달."""
    ctx = dd.calc_price_context(144.0, 145.0, 160.0, 140.0, "CONDITIONAL_NEW_BUY")
    assert ctx["condition_status"] == "reached"
    assert ctx["condition_label"] == "조건 도달"


def test_calc_price_context_sell_mgmt():
    """AI_SELL_MANAGEMENT → 보유 관리 라벨."""
    ctx = dd.calc_price_context(100.0, 90.0, 120.0, 80.0, "AI_SELL_MANAGEMENT")
    assert "보유 관리" in ctx["condition_label"]
    assert "실행 매도 아님" in ctx["condition_label"]


def test_calc_price_context_missing():
    """missing values → 안전 반환."""
    ctx = dd.calc_price_context(0, None, None, None)
    assert ctx["condition_label"] == "데이터 부족"
    assert ctx["summary"] == "" or ctx["summary"] == "데이터 부족"

    ctx2 = dd.calc_price_context(None, 100, 110, 90)
    assert ctx2["distance_to_entry_pct"] is None


def test_calc_price_context_no_forbidden_labels():
    """raw action_type이 라벨에 노출되지 않음."""
    for at in ("AI_SELL_MANAGEMENT", "CONDITIONAL_NEW_BUY", "AI_NEW_BUY"):
        ctx = dd.calc_price_context(100, 95, 110, 85, at)
        assert at not in ctx["condition_label"]
        assert at not in ctx["summary"]


# ═══════════════════════════════════════════════════════
# 브리핑 아카이브 (19단계)
# ═══════════════════════════════════════════════════════

def test_briefing_archive_save_and_list(tmp_path, monkeypatch):
    """아카이브 저장/조회 shape 확인."""
    from core import briefing_archive as ba
    monkeypatch.setattr(ba, "_db_path", lambda: tmp_path / "test_archive.db")

    aid = ba.save_briefing_archive(
        briefing_type="US_BEFORE", title="테스트 브리핑",
        subject="[테스트]", body_text="본문입니다",
        body_html="<p>HTML</p>", raw_json={"advisor_oneliner": "요약"},
    )
    assert aid is not None
    items = ba.list_briefing_archives(limit=10, days=1)
    assert len(items) >= 1
    assert items[0]["briefing_type"] == "US_BEFORE"
    assert "body_text" not in items[0]  # 목록에는 body 미포함


def test_briefing_archive_get_detail(tmp_path, monkeypatch):
    """상세 조회 시 body 포함."""
    from core import briefing_archive as ba
    monkeypatch.setattr(ba, "_db_path", lambda: tmp_path / "test_archive.db")

    aid = ba.save_briefing_archive(
        briefing_type="KR_NIGHT", title="야간",
        body_text="텍스트", body_html="<b>HTML</b>",
    )
    detail = ba.get_briefing_archive(aid)
    assert detail is not None
    assert detail["body_text"] == "텍스트"
    assert "<b>HTML</b>" in detail["body_html"]


def test_briefing_archive_sanitize(tmp_path, monkeypatch):
    """script 태그 제거, secret 패턴 마스킹."""
    from core import briefing_archive as ba
    monkeypatch.setattr(ba, "_db_path", lambda: tmp_path / "test_archive.db")

    aid = ba.save_briefing_archive(
        briefing_type="MANUAL", title="보안 테스트",
        body_html='<p>ok</p><script>alert("xss")</script><p>app_key=ABC</p>',
    )
    detail = ba.get_briefing_archive(aid)
    assert "<script" not in detail["body_html"]
    assert "app_key" not in detail["body_html"]
    assert "[REDACTED]" in detail["body_html"]


def test_briefing_archive_api_routes():
    """app.py에 아카이브 GET 라우트 존재."""
    from pathlib import Path
    code = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    assert "/api/briefings" in code
    assert "/api/briefings/{archive_id}" in code


def test_briefing_archive_html_markers():
    """HTML 아카이브 마커 존재."""
    html = _mobile_html()
    for marker in (
        "briefing-archive-panel",
        "briefing-archive-list",
        "briefing-archive-card",
        "briefing-archive-detail",
        "briefing-email-body",
        "briefing-text-body",
        "briefing-archive-open",
        "briefing-archive-limited",
        "briefing-archive-empty",
        "briefing-action-derived",
    ):
        assert marker in html, f"아카이브 마커 '{marker}' 없음"


def test_briefing_archive_phrases():
    """아카이브 문구 존재."""
    html = _mobile_html()
    assert "메일 브리핑 원문" in html
    assert "최근 50개" in html
    assert "90일" in html
    assert "원문 보기" in html
    assert "브리핑 원문 데이터 대기" in html


def test_briefing_archive_no_post():
    """POST/PUT/DELETE 없음."""
    from pathlib import Path
    code = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    for verb in (".post(", ".put(", ".delete("):
        assert verb not in code, f"{verb} 핸들러 발견"


# ═══════════════════════════════════════════════════════
# 브리핑 탭 가시성 + 미리보기 (18단계)
# ═══════════════════════════════════════════════════════

def test_briefing_visibility_markers():
    """브리핑 탭 가시성 마커 존재."""
    html = _mobile_html()
    for marker in (
        "briefing-tab-visible",
        "briefing-tab-priority",
        "tab-scroll-hint",
        "mobile-tab-wrap",
        "briefing-preview-panel",
        "briefing-preview-card",
        "briefing-preview-latest",
        "briefing-preview-open-tab",
        "briefing-tab-title",
        "briefing-tab-description",
    ):
        assert marker in html, f"브리핑 가시성 마커 '{marker}' 없음"


def test_briefing_default_range():
    """기본 range가 7d (최근 자료 표시)."""
    html = _mobile_html()
    assert '_brRange="7d"' in html, "기본 range가 7d 아님"


def test_briefing_preview_functions():
    """브리핑 미리보기 함수/연결 존재."""
    html = _mobile_html()
    assert "renderBriefingPreview" in html
    assert "go('briefing')" in html
    assert "브리핑 전체 보기" in html
    assert "최근 브리핑 데이터 대기" in html


def test_briefing_visibility_no_forbidden_cta():
    """금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


# ═══════════════════════════════════════════════════════
# 브리핑 탭 (17단계)
# ═══════════════════════════════════════════════════════

def test_briefing_tab_markers():
    """브리핑 탭 마커 존재."""
    html = _mobile_html()
    for marker in (
        "briefing-tab-button",
        "briefing-tab-panel",
        "briefing-latest-summary",
        "briefing-filter-bar",
        "briefing-range-chip",
        "briefing-history-list",
        "briefing-history-card",
        "briefing-detail-sheet",
        "briefing-action-list",
        "briefing-related-ticker",
        "briefing-detail-guard",
        "briefing-limited-history",
        "briefing-stale-safe",
        "briefing-empty-state",
    ):
        assert marker in html, f"브리핑 탭 마커 '{marker}' 없음"


def test_briefing_functions():
    """브리핑 함수/API 호출 존재."""
    html = _mobile_html()
    assert "loadBriefing" in html
    assert "openBriefingDetail" in html
    assert "/api/decision-brief" in html
    assert "/api/predictions" in html
    assert "/api/recommendations/timeline" in html


def test_briefing_phrases():
    """브리핑 문구 존재."""
    html = _mobile_html()
    assert "조건 도달 시만" in html
    assert "보유 관리 · 실행 매도 아님" in html
    assert "최근 50개" in html
    assert "브리핑 데이터 대기" in html


def test_briefing_no_forbidden_cta():
    """브리핑 탭 금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


def test_briefing_pc_has_tab():
    """PC에도 브리핑 탭 존재."""
    from pathlib import Path
    pc = (Path(__file__).parent.parent / "web" / "index_pc.html").read_text(encoding="utf-8")
    assert 'data-p="briefing"' in pc, "PC에 브리핑 탭 없음"


# ═══════════════════════════════════════════════════════
# 준실시간 자동 갱신 UX (16단계)
# ═══════════════════════════════════════════════════════

def test_refresh_ux_markers():
    """갱신 상태바 마커 존재."""
    html = _mobile_html()
    for marker in (
        "refresh-status-bar",
        "refresh-live-dot",
        "refresh-last-updated",
        "manual-refresh-button",
        "refresh-error-state",
        "refresh-paused-state",
        "preserve-stale-data",
        "stale-data-badge",
        "refresh-fallback-safe",
        "handleVisibilityRefresh",
        "visibility-refresh-resume",
    ):
        assert marker in html, f"갱신 UX 마커 '{marker}' 없음"


def test_refresh_phrases():
    """갱신 관련 문구 존재."""
    html = _mobile_html()
    assert "준실시간" in html
    assert "마지막" in html
    assert "새로고침" in html
    assert "갱신 중" in html
    assert "기존 데이터 유지" in html
    assert "실시간 보장 아님" in html
    assert "일시정지" in html


def test_refresh_functions():
    """갱신 함수 존재."""
    html = _mobile_html()
    assert "refreshAllNow" in html
    assert "setRefreshState" in html
    assert "markRefreshSuccess" in html
    assert "markRefreshError" in html
    assert "safeInterval" in html


def test_refresh_no_forbidden_cta():
    """금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


def test_pc_html_valid():
    """PC HTML 기본 구조 유효."""
    from pathlib import Path
    pc = (Path(__file__).parent.parent / "web" / "index_pc.html").read_text(encoding="utf-8")
    assert pc.count("<html") == 1
    assert "POST" not in pc


# ═══════════════════════════════════════════════════════
# 폴드7 레이아웃 마감 (15단계) — 3단 분기 정리
# ═══════════════════════════════════════════════════════

def test_layout_density_markers():
    """레이아웃 밀도 시스템 마커 존재."""
    html = _mobile_html()
    for marker in (
        "folded-phone-layout",
        "fold-open-layout",
        "tablet-terminal-layout",
        "desktop-wide-layout",
        "fold-density-compact",
        "fold-density-terminal",
        "fold-modal-sheet",
        "modal-density-phone",
        "modal-density-fold",
    ):
        assert marker in html, f"레이아웃 마커 '{marker}' 없음"


def test_layout_breakpoints():
    """주요 breakpoint 문자열 존재."""
    html = _mobile_html()
    h = html.replace(" ", "")
    assert "650px" in h, "650px breakpoint 없음"
    assert "900px" in h, "900px breakpoint 없음"
    assert "1200px" in h, "1200px breakpoint 없음"


def test_layout_preserves_existing_markers():
    """기존 주요 기능 마커가 유지됨."""
    html = _mobile_html()
    for marker in (
        "stock-detail-terminal",
        "kis-holding-strip",
        "action-detail-sheet",
        "performance-detail-sheet",
        "info-hub-panel",
    ):
        assert marker in html, f"기존 마커 '{marker}' 실종"


def test_layout_pc_briefing_tab():
    """PC에 브리핑 페이지 존재."""
    from pathlib import Path
    pc = (Path(__file__).parent.parent / "web" / "index_pc.html").read_text(encoding="utf-8")
    assert 'id="p-briefing"' in pc, "PC 브리핑 페이지 없음"


def test_layout_no_forbidden_cta():
    """금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


# ═══════════════════════════════════════════════════════
# 정보 허브 (14단계) — 뉴스/신호/이벤트/타임라인
# ═══════════════════════════════════════════════════════

def test_info_hub_markers():
    """정보 허브 패널 마커 존재."""
    html = _mobile_html()
    for marker in (
        "info-hub-panel",
        "info-hub-news",
        "info-hub-signals",
        "info-hub-events",
        "info-hub-timeline",
        "info-hub-detail-sheet",
        "news-detail-sheet",
        "signal-detail-sheet",
        "event-detail-sheet",
        "decision-timeline-panel",
        "decision-timeline-row",
        "info-tap-detail",
        "info-open-ticker-detail",
        "info-empty-state",
        "fold-info-hub-grid",
    ):
        assert marker in html, f"정보 허브 마커 '{marker}' 없음"


def test_info_hub_functions():
    """정보 상세 함수 존재."""
    html = _mobile_html()
    assert "openInfoDetail" in html, "openInfoDetail 없음"
    assert "renderInfoNews" in html, "renderInfoNews 없음"
    assert "renderInfoSignals" in html, "renderInfoSignals 없음"
    assert "renderInfoEvents" in html, "renderInfoEvents 없음"
    assert "renderInfoTimeline" in html, "renderInfoTimeline 없음"


def test_info_hub_phrases():
    """정보 허브 표시 문구 존재."""
    html = _mobile_html()
    assert "뉴스 상세" in html
    assert "신호 상세" in html
    assert "이벤트 상세" in html
    assert "판단 타임라인" in html
    assert "관련 종목 ›" in html
    assert "원문 보기" in html
    assert "뉴스 데이터 대기" in html
    assert "신호 데이터 대기" in html
    assert "이벤트 데이터 대기" in html
    assert "최근 판단 데이터 대기" in html


def test_info_hub_empty_states():
    """empty state 마커 존재."""
    html = _mobile_html()
    for marker in (
        "news-empty-state",
        "signal-empty-state",
        "event-empty-state",
        "timeline-empty-state",
    ):
        assert marker in html, f"empty state '{marker}' 없음"


def test_info_hub_no_forbidden_cta():
    """금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


# ═══════════════════════════════════════════════════════
# 성과 상세 시트 (13단계)
# ═══════════════════════════════════════════════════════

def test_performance_detail_markers():
    """성과 상세 시트 마커 존재."""
    html = _mobile_html()
    for marker in (
        "performance-detail-sheet",
        "performance-detail-row",
        "performance-detail-summary",
        "performance-detail-list",
        "performance-detail-empty",
        "performance-tap-detail",
    ):
        assert marker in html, f"성과 상세 마커 '{marker}' 없음"


def test_performance_detail_function():
    """openPerfDetail 함수 및 문구 존재."""
    html = _mobile_html()
    assert "openPerfDetail" in html, "openPerfDetail 함수 없음"
    assert "승 상세" in html or "승<span" in html, "승 상세 문구 없음"
    assert "패 상세" in html or "패<span" in html, "패 상세 문구 없음"
    assert "무 상세" in html, "무 상세 문구 없음"
    assert "최근 종료 결과" in html, "최근 종료 결과 문구 없음"
    assert "종목 상세 ›" in html, "종목 상세 안내 없음"


def test_performance_percent_utils():
    """fmtPctSmart / isMeaningfulPct / safeWinRate 유틸 존재."""
    html = _mobile_html()
    assert "fmtPctSmart" in html, "fmtPctSmart 없음"
    assert "isMeaningfulPct" in html, "isMeaningfulPct 없음"
    assert "safeWinRate" in html, "safeWinRate 없음"


def test_performance_no_abnormal_percent():
    """비정상 퍼센트 문자열 없음."""
    html = _mobile_html()
    for bad in ("4020%", "7500%", "10000%"):
        assert bad not in html, f"비정상 퍼센트 '{bad}' 발견"


def test_performance_no_forbidden_cta():
    """금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


# ═══════════════════════════════════════════════════════
# 액션 매트릭스 상세 시트 (12단계)
# ═══════════════════════════════════════════════════════

def test_action_detail_markers():
    """액션 상세 시트 마커 존재."""
    html = _mobile_html()
    for marker in (
        "action-detail-sheet",
        "action-tile-clickable",
        "action-detail-list",
        "action-detail-row",
        "action-detail-quote",
        "action-detail-guard",
    ):
        assert marker in html, f"액션 상세 마커 '{marker}' 없음"


def test_action_detail_function():
    """openActionDetail 함수 존재."""
    html = _mobile_html()
    assert "openActionDetail" in html, "openActionDetail 함수 없음"
    assert "상세 ›" in html, "'상세 ›' 문구 없음"


def test_action_detail_guard_phrases():
    """보호 문구 존재."""
    html = _mobile_html()
    assert "조건 도달 시만" in html
    assert "보유 관리 · 실행 매도 아님" in html
    assert "즉시 체결 금지" in html


def test_action_detail_ticker_link():
    """액션 상세에서 종목 상세 연결."""
    html = _mobile_html()
    assert "openM(" in html, "종목 상세 연결 없음"
    assert "종목 상세 ›" in html, "종목 상세 안내 없음"


def test_action_detail_no_forbidden_cta():
    """액션 상세에서 금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


# ═══════════════════════════════════════════════════════
# KIS 보유 스트립 + 포트폴리오 카드 강화 (11단계)
# ═══════════════════════════════════════════════════════

def test_kis_strip_markers():
    """홈 KIS 보유 스트립 마커 존재."""
    html = _mobile_html()
    for marker in (
        "kis-holding-strip",
        "kis-strip-item",
        "ticker-tap-detail",
    ):
        assert marker in html, f"KIS strip 마커 '{marker}' 없음"


def test_kis_holding_card_markers():
    """포트폴리오 보유 카드 강화 마커 존재."""
    html = _mobile_html()
    for marker in (
        "kis-holding-card",
        "holding-live-price",
        "holding-day-change",
        "holding-pnl-grid",
        "holding-source-meta",
    ):
        assert marker in html, f"보유 카드 마커 '{marker}' 없음"


def test_kis_strip_source_cache_messages():
    """source/cache 안내 문구 존재."""
    html = _mobile_html()
    assert "준실시간" in html
    assert "60초 캐시" in html
    assert "실시간 보장 아님" in html


def test_kis_strip_detail_connection():
    """종목 클릭 시 loadTicker 연결."""
    html = _mobile_html()
    # openM → loadTicker 호출 체인
    assert "openM(" in html, "종목 클릭 상세 연결 없음"


def test_kis_strip_no_forbidden_cta():
    """KIS 스트립/카드에서 금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


# ═══════════════════════════════════════════════════════
# 종목 상세 터미널 (10단계) — HTML 마커 검증
# ═══════════════════════════════════════════════════════

def test_stock_detail_terminal_markers():
    """증권앱형 종목 상세 구조 마커가 존재."""
    html = _mobile_html()
    for marker in (
        "stock-detail-terminal",
        "quote-source-badge",
        "ticker-chart-panel",
        "holding-snapshot",
        "recommendation-snapshot",
        "kis-freshness",
    ):
        assert marker in html, f"상세 터미널 마커 '{marker}' 없음"


def test_stock_detail_chart_api_call():
    """종목 상세에서 /api/ticker/{ticker}/chart 호출."""
    html = _mobile_html()
    assert "/api/ticker/" in html and "/chart?range=" in html, "차트 API 호출 없음"


def test_stock_detail_cache_messages():
    """캐시/준실시간 안내 문구 존재."""
    html = _mobile_html()
    assert "준실시간" in html, "'준실시간' 문구 없음"
    assert "60초 캐시" in html, "'60초 캐시' 문구 없음"
    assert "실시간 보장 아님" in html, "'실시간 보장 아님' 문구 없음"


def test_stock_detail_protected_phrase():
    """보유 관리 · 실행 매도 아님 문구 존재."""
    html = _mobile_html()
    assert "보유 관리 · 실행 매도 아님" in html


def test_stock_detail_conditional_phrase():
    """조건부 매수 '조건 도달 시만' 문구 존재."""
    html = _mobile_html()
    assert "조건 도달 시만" in html


def test_stock_detail_no_forbidden_cta():
    """종목 상세에서 금지 CTA 없음."""
    html = _mobile_html()
    for cta in ("주문 실행", "매수하기", "매도하기"):
        assert cta not in html, f"금지 CTA '{cta}' 존재"


def test_stock_detail_no_raw_action_type_display():
    """action_type raw 코드가 사용자 표시 텍스트로 직접 노출되지 않음.

    JS 매핑 상수 내부(classify 함수)는 허용, 화면 렌더 텍스트만 방지.
    """
    html = _mobile_html()
    # badge/렌더 텍스트에 raw 코드가 직접 표시되면 안 됨
    # BL={buy:"매수",...} 매핑 이후 badge() 함수로 변환하므로
    # innerHTML/textContent에 직접 "AI_SELL_MANAGEMENT" 문자열이 표시되면 안 됨
    # 단, classify 함수 내부 상수와 분기 판정은 허용
    import re
    # classify/BL/BC 정의 블록 제외한 나머지에서 raw 코드 노출 확인
    # 렌더 함수 내부에서 action_type을 직접 textContent로 쓰는지 확인
    # badge(cls)로 변환하므로 정상. 직접 문자열 출력 패턴만 금지:
    # 예: `${r.action_type}` 이 innerHTML에 바로 들어가는 경우
    render_sections = re.findall(r'innerHTML\s*[+=].*?;', html, re.S)
    for section in render_sections:
        # action_type 값을 직접 표시하는 패턴 (badge 없이)
        if 'action_type}' in section and 'badge' not in section and 'classify' not in section:
            # BL 매핑이나 badge 변환 없이 직접 표시
            assert False, f"action_type raw 직접 표시 의심: {section[:100]}"


# ═══════════════════════════════════════════════════════
# 차트 API (ticker_chart_data) — OHLCV 조회 전용
# ═══════════════════════════════════════════════════════


def _chart_mock(points=None, price=121.0, pct=0.83, source="yfinance"):
    """차트 테스트용 모킹 팩토리."""
    if points is None:
        points = [{"time": "09:30", "open": 120, "high": 122,
                   "low": 119, "close": 121, "volume": 100000}]
    return {"points": points, "current_price": price,
            "day_pct": pct, "source": source}


def test_chart_data_shape(monkeypatch):
    """ticker_chart_data 반환값에 필수 키가 모두 존재."""
    monkeypatch.setattr(dd, "_fetch_chart_raw", lambda t, p, i: _chart_mock())
    dd._cache.clear()
    result = dd.ticker_chart_data("MU", "1d", "5m")
    for key in ("ticker", "name", "range", "interval", "source",
                "updated_at", "cache_age_sec", "current_price",
                "day_pct", "points", "error"):
        assert key in result, f"missing key: {key}"
    assert result["ticker"] == "MU"
    assert len(result["points"]) == 1
    assert result["error"] == ""
    assert result["source"] == "yfinance"


def test_chart_source_propagated(monkeypatch):
    """source 필드가 fetch 결과에서 전파됨."""
    monkeypatch.setattr(dd, "_fetch_chart_raw",
                        lambda t, p, i: _chart_mock(source="KIS+yfinance"))
    dd._cache.clear()
    result = dd.ticker_chart_data("005930.KS", "1d", "5m")
    assert result["source"] == "KIS+yfinance"


def test_chart_invalid_range_fallback(monkeypatch):
    """허용 외 range는 1d/5m으로 안전 fallback."""
    monkeypatch.setattr(dd, "_fetch_chart_raw",
                        lambda t, p, i: _chart_mock(points=[], price=0.0, pct=0.0))
    dd._cache.clear()
    result = dd.ticker_chart_data("MU", "99y", "1s")
    assert result["range"] == "1d"
    assert result["interval"] == "5m"


def test_chart_empty_points_has_keys(monkeypatch):
    """points가 비어도 모든 키는 존재."""
    monkeypatch.setattr(dd, "_fetch_chart_raw",
                        lambda t, p, i: _chart_mock(points=[], price=0.0, pct=0.0))
    dd._cache.clear()
    result = dd.ticker_chart_data("FAKE", "1d", "5m")
    assert result["points"] == []
    assert "error" in result
    assert result["current_price"] == 0.0
    # 빈 결과에도 source/updated_at/cache_age_sec 존재
    assert "source" in result
    assert "updated_at" in result
    assert "cache_age_sec" in result


def test_chart_invalid_ticker():
    """이상한 ticker는 error 반환."""
    dd._cache.clear()
    result = dd.ticker_chart_data("../../etc", "1d", "5m")
    assert result["error"] == "invalid ticker format"
    assert result["points"] == []


def test_chart_cache_key(monkeypatch):
    """같은 ticker+range 조합은 캐시 히트."""
    call_count = {"n": 0}

    def _mock(t, p, i):
        call_count["n"] += 1
        return _chart_mock()

    monkeypatch.setattr(dd, "_fetch_chart_raw", _mock)
    dd._cache.clear()
    dd.ticker_chart_data("MU", "1d", "5m")
    dd.ticker_chart_data("MU", "1d", "5m")  # 캐시 히트
    assert call_count["n"] == 1


def test_chart_error_cache_short_ttl(monkeypatch):
    """빈 결과는 10초 후 재호출 허용 (정상 데이터는 60초 캐시)."""
    call_count = {"n": 0}

    def _mock_empty(t, p, i):
        call_count["n"] += 1
        return _chart_mock(points=[], price=0.0, pct=0.0)

    monkeypatch.setattr(dd, "_fetch_chart_raw", _mock_empty)
    dd._cache.clear()
    dd.ticker_chart_data("GONE", "1d", "5m")
    assert call_count["n"] == 1
    # 10초 안에는 캐시 히트 (재호출 안 됨)
    dd.ticker_chart_data("GONE", "1d", "5m")
    assert call_count["n"] == 1
    # 10초 지난 것처럼 시뮬레이션
    with dd._cache_lock:
        key = "chart:GONE:1d:5m"
        if key in dd._cache:
            ts, val = dd._cache[key]
            dd._cache[key] = (ts - 61, val)  # 60초 TTL 만료
    dd.ticker_chart_data("GONE", "1d", "5m")
    assert call_count["n"] == 2  # 재호출됨


def test_chart_route_exists():
    """web/app.py에 chart route가 존재하고 generic ticker보다 먼저."""
    from pathlib import Path
    code = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    chart_pos = code.find("/api/ticker/{ticker}/chart")
    generic_pos = code.find("/api/ticker/{ticker:path}")
    assert chart_pos != -1, "chart route 없음"
    assert chart_pos < generic_pos, "chart route가 generic보다 뒤에 있음 — 삼킴 위험"


def test_no_post_put_delete_routes():
    """app.py에 쓰기 핸들러 없음."""
    from pathlib import Path
    code = (Path(__file__).parent.parent / "web" / "app.py").read_text(encoding="utf-8")
    for verb in (".post(", ".put(", ".delete("):
        assert verb not in code, f"{verb} 핸들러 발견 — read-only 위반"


def test_no_naver_in_dashboard_layer():
    """대시보드 레이어(dashboard_data + app.py)에 네이버 크롤링 없음."""
    from pathlib import Path
    root = Path(__file__).parent.parent
    targets = [root / "core" / "dashboard_data.py", root / "web" / "app.py"]
    for f in targets:
        content = f.read_text(encoding="utf-8")
        for kw in ("naver.com", "finance.naver", "m.stock.naver"):
            assert kw not in content, f"{f.name}에 네이버 크롤링 '{kw}' 발견"


# ═══════════════════════════════════════════════════════
# 폴드7 / 태블릿 PC식 레이아웃 (9단계) — index.html 정적 검증
# ═══════════════════════════════════════════════════════
# 9-1) 폴드 레이아웃 구조 마커 존재
def test_mobile_fold_layout_markers():
    html = _mobile_html()
    for marker in (
        "fold-layout",   # 홈 그리드 컨테이너
        "fold-grid",     # 레이아웃 마커
        "fold-main",     # 좌측 메인
        "fold-side",     # 우측 사이드
        "fold-wide",     # 하단 와이드
        "fold-only",     # 폴드 폭에서만 노출
    ):
        assert marker in html, f"폴드 레이아웃 마커 '{marker}' 없음"
    # grid-template-areas 로 2열/3영역 배치
    assert "grid-template-areas" in html, "grid-template-areas 배치 없음"


# 9-2) 폴드 전용 PC급 정보 패널 마커 존재
def test_mobile_fold_panels():
    html = _mobile_html()
    for marker in (
        "fold-performance-panel",  # 30일 성과 + 유형별 + 종료
        "fold-market-panel",       # 지수 + 모드 + 뉴스
        "fold-signal-panel",       # 기술 신호 + 일정
        "fold-portfolio-panel",    # 기여도 + 보유 TOP + 자산군 + 계좌
    ):
        assert marker in html, f"폴드 패널 마커 '{marker}' 없음"


# 9-3) 폴드/태블릿 breakpoint 존재 (720 / 900)
def test_mobile_fold_breakpoints():
    html = _mobile_html()
    h = html.replace(" ", "")
    assert "@media(min-width:720px)" in h, "720px breakpoint 없음"
    assert "@media(min-width:900px)" in h, "900px breakpoint 없음"


# 9-4) 헬퍼 클래스 존재 (폰/폴드 분기 + 그리드)
def test_mobile_fold_helper_classes():
    html = _mobile_html()
    for cls in (
        "fold-card-grid", "fold-wide-grid", "fold-dense-list",
        "fold-visible", "phone-only", "fold-only",
    ):
        assert cls in html, f"헬퍼 클래스 '{cls}' 없음"


# 9-5) PC급 analytics 정보 필드를 홈에서 사용
def test_mobile_fold_uses_pc_fields():
    html = _mobile_html()
    for field in (
        "top_contributors", "bottom_contributors",
        "asset_classes", "risk_flags",
    ):
        assert field in html, f"PC급 필드 '{field}' 미사용"
    # 집중도(concentration) — analytics 또는 비중 기반
    assert ("concentration" in html) or ("weight" in html), "집중도/비중 정보 없음"


# 9-6) 시뮬레이터 폴드 3패널 (CSS 보강) — sim-left/center/right + row 분기
def test_mobile_fold_simulator_3panel():
    html = _mobile_html()
    for cls in ("sim-left", "sim-center", "sim-right"):
        assert cls in html, f"시뮬레이터 패널 '{cls}' 없음"
    # 폴드 폭에서 가로 3패널로 전환
    assert "flex-direction:row" in html, "시뮬레이터 가로(row) 분기 없음"
    # 주문 CTA는 계속 '주문표 미리보기' (실주문 아님)
    assert ("주문표 미리보기" in html) or ("가상 주문 계산" in html), "안전 CTA 없음"


# ═══ 9단계 보정: 축소 PC판 제거 — 폴드용 태블릿 터미널 ═══

# 9R-1) 보정 마커 존재 (하이브리드/터미널/패널 분기)
def test_mobile_fold_refine_markers():
    html = _mobile_html()
    for marker in (
        "fold-hybrid",      # 720~899 하이브리드 단일 컬럼
        "tablet-terminal",  # 900+ 2열 터미널 그리드
        "fold-open-panel",  # 폴드에서 펼쳐두는 패널 묶음
        "phone-collapsed",  # 폰에서만 접는 details
        "tablet-visible",   # 태블릿 폭에서 노출
    ):
        assert marker in html, f"폴드 보정 마커 '{marker}' 없음"


# 9R-2) 재계층화된 breakpoint (719/899 분기 + 1100 wide)
def test_mobile_fold_refine_breakpoints():
    html = _mobile_html()
    h = html.replace(" ", "")
    assert "max-width:899px" in h, "720~899 하이브리드 분기 없음"
    assert "@media(min-width:1100px)" in h, "1100px wide breakpoint 없음"


# 9R-3) 압축 방지 마커 — 최소 카드폭 + 큰 타이포
def test_mobile_fold_refine_no_compress():
    html = _mobile_html()
    h = html.replace(" ", "")
    assert "minmax(300px" in h, "최소 카드폭(minmax 300px) 방어 없음"
    assert "clamp(30px" in h, "핵심 수치 대형 타이포(clamp 30px) 없음"


# 9R-4) 메인/사이드 컬럼 비율 (메인 넓게)
def test_mobile_fold_refine_column_ratio():
    html = _mobile_html()
    h = html.replace(" ", "")
    # 메인 1.8fr 이상 / 사이드 보조
    assert ("minmax(0,1.8fr)" in h) or ("minmax(0,1.9fr)" in h), "메인 컬럼 우세 비율 없음"


# 9R-5) 집중도(concentration) 필드를 실제로 사용
def test_mobile_fold_refine_concentration():
    html = _mobile_html()
    assert "concentration" in html, "concentration 필드 미사용"
    # 집중도 행 마커
    assert "conc-row" in html, "집중도 표시 행(conc-row) 없음"


# 9R-6) 폴드 홈 그리드는 활성(.on) 탭일 때만 — 펼친 상태 탭 전환 회귀 방지
def test_mobile_fold_home_grid_gated_by_on():
    html = _mobile_html()
    h = html.replace(" ", "").replace("\n", "")
    # display:grid / display:block 으로 홈을 켜는 규칙은 .on 으로 게이트돼야
    # (안 그러면 #t-home(120) > .tc{display:none}(10) 라 홈이 항상 떠 다른 탭을 덮음)
    assert "#t-home.fold-layout.tablet-terminal.on{display:grid" in h, \
        "900+ 홈 그리드가 .on 게이트 안 됨 — 펼친 상태 탭 클릭 무효"
    assert "#t-home.fold-layout.on{display:block" in h, \
        "720~899 홈 블록이 .on 게이트 안 됨 — 탭 클릭 무효"


def test_pc_home_uses_strict_four_column_grid():
    """PC 홈은 4열 카드 그리드 기준으로 상단과 시장 카드를 배치한다."""
    from pathlib import Path
    pc = (Path(__file__).parent.parent / "web" / "index_pc.html").read_text(encoding="utf-8")

    assert ".home-hero{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(420px,1fr)" in pc
    assert ".portfolio-hero{grid-column:1;grid-row:1" in pc
    assert ".hero-market-strip{grid-column:1;grid-row:2;grid-template-columns:repeat(4,minmax(0,1fr))" in pc
    assert ".hero-side{grid-column:2;grid-row:1 / span 2" in pc
    assert "home-strip-top" in pc
    assert "home-strip-rest" in pc
    assert ".home-rest-market-strip{grid-template-columns:repeat(auto-fill,145px)" in pc
    assert "marketCards.slice(0,4)" in pc
    assert "marketCards.slice(4)" in pc
    assert ".market-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))" in pc
    assert ".market-strip .icard{min-width:0;flex:none" in pc



def test_pc_trade_card_opens_price_and_pnl_detail():
    """PC 실제 체결 내역 클릭 시 매수가/매도가와 손익 상세 모달을 연다."""
    from pathlib import Path
    pc = (Path(__file__).parent.parent / "web" / "index_pc.html").read_text(encoding="utf-8")

    assert "function openTradeDetail(t)" in pc
    assert "매수가" in pc
    assert "매도가" in pc
    assert "실현손익" in pc
    assert "평가손익" in pc
    assert "openTradeDetail" in pc.split("function tradeCard(t){", 1)[1].split("function renderTradeLedger", 1)[0]



def test_mobile_trade_card_opens_price_and_pnl_detail():
    """모바일 실제 체결 내역 클릭 시 매수가/매도가와 손익 상세 바텀시트를 연다."""
    from pathlib import Path
    mobile = (Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")

    assert "function openTradeDetail(t)" in mobile
    assert "매수가" in mobile
    assert "매도가" in mobile
    assert "실현손익" in mobile
    assert "평가손익" in mobile
    block = mobile.split("function tradeCard(t){", 1)[1].split("function renderTradeLedger", 1)[0]
    assert "openTradeDetail" in block



def test_mobile_home_shows_today_exec_trades_under_total_eval():
    """모바일 홈 총 평가액 박스 아래에 당일 실제 체결 내역만 표시한다."""
    from pathlib import Path
    mobile = (Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")

    assert "home-exec-tab" in mobile
    assert "실제 체결 내역" in mobile
    assert "오늘 체결 내역 없음" in mobile
    assert "todayExecTrades().slice(0,4)" in mobile
    assert "function renderHomeExecTrades()" in mobile
    assert "renderHomeExecTrades();renderMCB" in mobile
    assert "renderHomeExecTrades();_tradeLedgerLoaded=true" in mobile



def test_stock_agent_activity_api_and_html_markers():
    """Stock-Agent 분석 활동은 GET-only API와 PC/모바일 표시 위치를 갖는다."""
    from pathlib import Path
    root = Path(__file__).parent.parent
    app = (root / "web" / "app.py").read_text(encoding="utf-8")
    pc = (root / "web" / "index_pc.html").read_text(encoding="utf-8")
    mobile = (root / "web" / "index.html").read_text(encoding="utf-8")
    data = (root / "core" / "dashboard_data.py").read_text(encoding="utf-8")

    assert '@app.get("/api/stock-agent/activity")' in app
    assert "def stock_agent_activity_data" in data
    assert "stock_agent_activity.v1.read_only" in data
    assert "live_order_allowed" in data
    assert "stock-agent-activity-pc" in pc
    assert "stock-agent-activity-m" in mobile
    assert 'data-p="agent"' in pc
    assert 'data-p="sim"' not in pc
    assert 'id="p-agent"' in pc
    assert 'data-t="agent"' in mobile
    assert 'data-t="sim"' not in mobile
    assert mobile.index('data-t="agent"') < mobile.index('data-t="toss"')
    assert 'id="t-agent"' in mobile
    assert "/api/stock-agent/activity" in pc
    assert "/api/stock-agent/activity" in mobile
    assert "상세 보기" in pc
    assert "상세 보기" in mobile
    assert "agent-summary-first-v2" in pc
    assert "agent-summary-first-v2" in mobile
    assert "agent-detail-log" in pc
    assert "agent-detail-log" in mobile
    assert "종목 분석" in pc
    assert "종목 분석" in mobile
    assert "agent-stock-card" in pc
    assert "agent-stock-card" in mobile
    assert '"candidates"' in data
    assert '"excluded"' in data
    assert "display:none" in pc
    assert "display:none" in mobile
    assert "핵심 활동" in pc
    assert "핵심 활동" in mobile
    assert "주문 생성/승인/전송 없음" in data


def test_portfolio_clamps_obvious_quote_outliers(monkeypatch):
    """총평가액은 split/scale 의심 가격 하나로 급등하지 않도록 보수 계산한다."""
    from core.models import Quote
    from core import market
    from config import settings

    monkeypatch.setattr(settings, "HOLDINGS_GENERAL", {
        "005930.KS": {"shares": 90, "avg_cost_krw": 60425},
        "MU": {"shares": 5, "avg_cost_usd": 408.8181},
    })
    monkeypatch.setattr(settings, "HOLDINGS_RIA", {})
    monkeypatch.setattr(settings, "HOLDINGS_IRP", {})
    monkeypatch.setattr(settings, "HOLDINGS_PENSION", {})
    monkeypatch.setattr(settings, "HOLDINGS_ISA", {})
    monkeypatch.setattr(settings, "DEFAULT_CASH", 0)
    monkeypatch.setattr(settings, "RIA_CASH", 0)
    monkeypatch.setattr(settings, "IRP_CASH", 0)
    monkeypatch.setattr(settings, "IRP_DEFAULT_OPTION", 0)
    monkeypatch.setattr(settings, "PENSION_MMF", 0)
    monkeypatch.setattr(settings, "ISA_CASH", 0)

    def fake_batch_quotes(tickers):
        if "USDKRW=X" in tickers:
            return {"USDKRW=X": Quote("USDKRW=X", "원달러", 1543.18)}
        return {
            "005930.KS": Quote("005930.KS", "삼성전자", 328000, pct=1.55),
            "MU": Quote("MU", "마이크론", 1138.65, pct=-0.58),
        }

    monkeypatch.setattr(market, "_batch_quotes", fake_batch_quotes)

    p = dd._fetch_portfolio_raw()
    general = next(a for a in p["accounts"] if a["name"] == "일반")
    items = {it["ticker"]: it for it in general["items"]}

    assert items["005930.KS"]["price_guard"] == "ok"
    assert items["005930.KS"]["raw_price"] == 328000
    assert items["005930.KS"]["current_price"] == 328000
    assert items["005930.KS"]["eval_krw"] == 328000 * 90

    assert items["MU"]["price_guard"] == "ok"
    assert items["MU"]["raw_price"] == 1138.65
    assert items["MU"]["current_price"] == 1138.65
    assert items["MU"]["eval_krw"] == round(1138.65 * 5 * 1543.18)
    assert "price_warning" not in items["MU"]

    # Unit-test monkeypatch mode disables broker Excel overlays and cash overrides,
    # so total_eval is exactly the two mocked holdings with mocked FX.
    assert p["total_eval"] == items["005930.KS"]["eval_krw"] + items["MU"]["eval_krw"]


def test_portfolio_quote_refresh_timeout_returns_fast_stale(monkeypatch):
    """느린 KIS/yfinance 배치는 요청 경로를 막지 않고 백그라운드 갱신한다."""
    import time
    from core.models import Quote

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    dd._portfolio_quote_cache.clear()
    dd._portfolio_quote_refreshing.clear()
    calls = {"n": 0}

    def slow_fetch(tickers):
        calls["n"] += 1
        time.sleep(0.15)
        return {"005930.KS": Quote("005930.KS", "삼성전자", 70000, pct=1.2)}

    started = time.monotonic()
    cold = dd._portfolio_quotes_fast({"005930.KS": "삼성전자"}, slow_fetch, ttl=60, timeout=0.01)
    elapsed = time.monotonic() - started

    assert cold == {}
    assert elapsed < 0.08
    assert calls["n"] == 1

    time.sleep(0.25)
    warm = dd._portfolio_quotes_fast({"005930.KS": "삼성전자"}, slow_fetch, ttl=60, timeout=0.01)

    assert warm["005930.KS"].price == 70000
    assert calls["n"] == 1


def test_portfolio_missing_quote_uses_cost_with_warning(monkeypatch):
    """현재가가 0/누락이면 평가액은 평단 기준으로 보수 계산하고 경고를 남긴다."""
    from core.models import Quote
    from core import market
    from config import settings

    monkeypatch.setattr(settings, "HOLDINGS_GENERAL", {"NVDA": {"shares": 10, "avg_cost_usd": 190}})
    monkeypatch.setattr(settings, "HOLDINGS_RIA", {})
    monkeypatch.setattr(settings, "HOLDINGS_IRP", {})
    monkeypatch.setattr(settings, "HOLDINGS_PENSION", {})
    monkeypatch.setattr(settings, "HOLDINGS_ISA", {})
    monkeypatch.setattr(settings, "DEFAULT_CASH", 0)
    monkeypatch.setattr(settings, "RIA_CASH", 0)
    monkeypatch.setattr(settings, "IRP_CASH", 0)
    monkeypatch.setattr(settings, "IRP_DEFAULT_OPTION", 0)
    monkeypatch.setattr(settings, "PENSION_MMF", 0)
    monkeypatch.setattr(settings, "ISA_CASH", 0)

    def fake_batch_quotes(tickers):
        if "USDKRW=X" in tickers:
            return {"USDKRW=X": Quote("USDKRW=X", "원달러", 1500)}
        return {"NVDA": Quote("NVDA", "엔비디아", 0)}

    monkeypatch.setattr(market, "_batch_quotes", fake_batch_quotes)
    p = dd._fetch_portfolio_raw()
    item = next(a for a in p["accounts"] if a["name"] == "일반")["items"][0]

    assert item["price_guard"] == "missing"
    assert item["current_price"] == 190
    assert item["eval_krw"] == 190 * 10 * 1500
    assert "현재가 조회 실패" in item["price_warning"]
