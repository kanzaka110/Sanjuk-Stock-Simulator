"""
읽기 전용 웹 대시보드 (FastAPI).

조회 전용 — 실주문/DB 수정 엔드포인트 없음. 기본 127.0.0.1:8787 바인드.
외부 공개 시 Basic Auth 필수 (DASHBOARD_USER + DASHBOARD_PASS 환경변수).

실행:
  python -m web.app
  python main.py dashboard
"""

from __future__ import annotations

import os
import secrets

import re

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from core import dashboard_data as dd

app = FastAPI(title="Sanjuk Dashboard", docs_url=None, redoc_url=None)
security = HTTPBasic()

_AUTH_USER = os.environ.get("DASHBOARD_USER", "")
_AUTH_PASS = os.environ.get("DASHBOARD_PASS", "")
_AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)


def _check_auth(creds: HTTPBasicCredentials = Depends(security)):
    """DASHBOARD_USER/DASHBOARD_PASS 설정 시 Basic Auth 검증."""
    ok = secrets.compare_digest(creds.username, _AUTH_USER) and secrets.compare_digest(
        creds.password, _AUTH_PASS
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


# 인증 활성화 시 모든 엔드포인트에 의존성 주입
if _AUTH_ENABLED:
    app = FastAPI(
        title="Sanjuk Dashboard",
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(_check_auth)],
    )


# ─── API (전부 읽기 전용 GET) ──────────────────────────
@app.get("/api/health")
def api_health():
    return JSONResponse(dd.health())


@app.get("/api/status")
def api_status():
    return JSONResponse(dd.system_status())


@app.get("/api/predictions")
def api_predictions():
    return JSONResponse({
        "recent": dd.recent_predictions(20),
        "open": dd.open_predictions(50),
        "closed": dd.closed_summary(30),
    })


@app.get("/api/accuracy")
def api_accuracy():
    return JSONResponse({"by_ticker": dd.accuracy_by_ticker()})


@app.get("/api/trades")
def api_trades(limit: int = 20):
    from core.trade_log import list_trades
    return JSONResponse(list_trades(limit=limit, pending_only=False))


@app.get("/api/trades/pending")
def api_trades_pending(limit: int = 20):
    from core.trade_log import list_trades
    return JSONResponse(list_trades(limit=limit, pending_only=True))


@app.get("/api/market")
def api_market():
    return JSONResponse(dd.market_data())


@app.get("/api/portfolio")
def api_portfolio():
    return JSONResponse(dd.portfolio_data())


@app.get("/api/performance")
def api_performance():
    return JSONResponse(dd.performance_data(30))


@app.get("/api/briefings")
def api_briefings(limit: int = 50, days: int = 90, type: str = "all"):
    from core.briefing_archive import list_briefing_archives
    items = list_briefing_archives(limit=min(limit, 100), days=min(days, 365),
                                   briefing_type=type)
    return JSONResponse({"items": items, "limit": limit, "days": days, "error": ""})


@app.get("/api/briefings/{archive_id}")
def api_briefing_detail(archive_id: str):
    from core.briefing_archive import get_briefing_archive, build_archive_tracking
    item = get_briefing_archive(archive_id)
    if item is None:
        return JSONResponse({"error": "not found", "id": archive_id})
    tracking = build_archive_tracking(item)
    return JSONResponse({**item, "tracking": tracking, "error": ""})


@app.get("/api/ticker/{ticker}/orderbook")
def api_ticker_orderbook(ticker: str):
    return JSONResponse(dd.ticker_orderbook(ticker))


@app.get("/api/ticker/{ticker}/chart")
def api_ticker_chart(ticker: str, range: str = "1d", interval: str = "5m"):
    return JSONResponse(dd.ticker_chart_data(ticker, range_=range, interval=interval))


@app.get("/api/ticker/{ticker:path}")
def api_ticker(ticker: str):
    return JSONResponse(dd.ticker_detail(ticker))


@app.get("/api/news")
def api_news():
    return JSONResponse(dd.news_data())


@app.get("/api/signals")
def api_signals():
    return JSONResponse(dd.live_signals())


@app.get("/api/calendar")
def api_calendar():
    return JSONResponse(dd.event_calendar())


@app.get("/api/portfolio/analytics")
def api_portfolio_analytics():
    return JSONResponse(dd.portfolio_analytics())


@app.get("/api/decision-brief")
def api_decision_brief():
    return JSONResponse(dd.decision_brief())


@app.get("/api/toss/account-summary")
def api_toss_account_summary():
    return JSONResponse(dd.toss_account_summary())


@app.get("/api/toss/automation-status")
def api_toss_automation_status():
    return JSONResponse(dd.toss_automation_status())


@app.get("/api/toss/paper-trades")
def api_toss_paper_trades(limit: int = 50):
    return JSONResponse(dd.toss_paper_trades(min(limit, 200)))


@app.get("/api/toss/decision-context")
def api_toss_decision_context():
    return JSONResponse(dd.toss_decision_context())


@app.get("/api/toss/cross-check")
def api_toss_cross_check():
    return JSONResponse(dd.toss_cross_check())


@app.get("/api/toss/paper-ledger")
def api_toss_paper_ledger():
    return JSONResponse(dd.toss_paper_ledger_data())


@app.get("/api/toss/paper-performance")
def api_toss_paper_performance():
    return JSONResponse(dd.toss_paper_performance_data())


@app.get("/api/toss/paper-policy")
def api_toss_paper_policy():
    return JSONResponse(dd.toss_paper_policy_data())




@app.get("/api/market/discovery")
def api_market_discovery(range: str = "today", limit: int = 50):
    return JSONResponse(dd.market_discovery_data(range_=range, limit=min(limit, 100)))


@app.get("/api/toss/buy-candidates")
def api_toss_buy_candidates(range: str = "today", limit: int = 20):
    return JSONResponse(dd.toss_buy_candidates_data(range_=range, limit=min(limit, 100)))


@app.get("/api/toss/live-pilot-policy")
def api_toss_live_pilot_policy():
    return JSONResponse(dd.toss_live_pilot_policy_data())


@app.get("/api/toss/live-pilot-previews")
def api_toss_live_pilot_previews():
    return JSONResponse(dd.toss_live_pilot_previews_data())


@app.get("/api/toss/live-pilot-verifications")
def api_toss_live_pilot_verifications(limit: int = 20):
    return JSONResponse(dd.toss_live_pilot_verifications_data(min(limit, 100)))


@app.get("/api/toss/live-pilot-events")
def api_toss_live_pilot_events(limit: int = 50):
    return JSONResponse(dd.toss_live_pilot_events_data(min(limit, 200)))


@app.get("/api/stock-agent/activity")
def api_stock_agent_activity(limit: int = 20):
    return JSONResponse(dd.stock_agent_activity_data(min(limit, 100)))


@app.get("/api/quality-report")
def api_quality_report(date: str | None = None):
    from core.toss_quality_gate import generate_daily_quality_report
    return JSONResponse(generate_daily_quality_report(date))


@app.get("/api/recommendations/timeline")
def api_timeline(
    range: str = "today",
    ticker: str | None = None,
    action_type: str | None = None,
    order: str = "desc",
):
    return JSONResponse(
        dd.recommendations_timeline(range_=range, ticker=ticker,
                                    action_type=action_type, order=order)
    )


# ─── HTML 대시보드 (모바일/PC 자동 분기) ─────────────
_MOBILE_RE = re.compile(r"iPhone|Android|Mobile|iPod|Opera Mini|IEMobile", re.I)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, view: str | None = Query(None)):
    from pathlib import Path

    # 강제 전환: ?view=pc / ?view=mobile
    if view == "pc":
        use_pc = True
    elif view == "mobile":
        use_pc = False
    else:
        ua = request.headers.get("user-agent", "")
        use_pc = not _MOBILE_RE.search(ua)

    filename = "index_pc.html" if use_pc else "index.html"
    html_path = Path(__file__).parent / filename
    if not html_path.exists():
        html_path = Path(__file__).parent / "index.html"
    html = html_path.read_text(encoding="utf-8")
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def run():
    """대시보드 서버 실행 (main.py dashboard / python -m web.app)."""
    import uvicorn
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8787"))
    print(f"📊 대시보드 (읽기 전용): http://{host}:{port}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"⚠️ 외부 바인드({host}) — SSH 터널 권장, 방화벽 확인 필요")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
