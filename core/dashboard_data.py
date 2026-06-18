"""
대시보드 데이터 수집 — 조회 전용 (읽기 전용)

웹 대시보드(web/app.py)와 헬스체크용. 주문 실행/DB 수정 일절 없음.
DB가 없거나 비어 있어도 절대 예외를 던지지 않고 빈 구조를 반환한다.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
log = logging.getLogger(__name__)

# ─── TTL 캐시 (스레드 안전, 읽기 전용) ───────────────────
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl: int, fn):
    """fn() 결과를 ttl초 캐시. 실패 시 빈 dict 반환."""
    with _cache_lock:
        if key in _cache:
            ts, val = _cache[key]
            if time.monotonic() - ts < ttl:
                return val
    try:
        val = fn()
    except Exception as e:
        log.warning("cache fn %s failed: %s", key, e)
        val = {}
    with _cache_lock:
        _cache[key] = (time.monotonic(), val)
    return val


def _db_path():
    try:
        from config.settings import DB_DIR
        return DB_DIR / "memory.db"
    except Exception:
        from pathlib import Path
        return Path("db/data/memory.db")


def _conn() -> sqlite3.Connection | None:
    """읽기 전용 연결. DB 없으면 None (예외 없음)."""
    p = _db_path()
    try:
        if not p.exists():
            return None
        # 읽기 전용 URI — 실수로도 쓰기 불가
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _rows(conn, sql, params=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _scalar(conn, sql, params=(), default=0):
    try:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r and r[0] is not None else default
    except Exception:
        return default


# ─── 추천(predictions) ─────────────────────────────────
def recent_predictions(limit: int = 20) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    rows = _rows(
        conn,
        """SELECT created_at, ticker, name, signal, original_signal, action_type,
                  action_grade, account_type, briefing_type, entry_price, target_price,
                  stop_loss, confidence, status, outcome, pnl_pct, normalizer_version
           FROM predictions ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    )
    conn.close()
    return rows


def open_predictions(limit: int = 50) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    rows = _rows(
        conn,
        """SELECT created_at, ticker, name, signal, action_type, account_type,
                  entry_price, target_price, stop_loss, confidence, briefing_type
           FROM predictions WHERE status='open' ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    )
    conn.close()
    return rows


def closed_summary(days: int = 30) -> dict:
    conn = _conn()
    if conn is None:
        return {"total": 0, "win": 0, "loss": 0, "neutral": 0, "avg_pnl": 0.0, "recent": []}
    cutoff = (datetime.now(KST) - timedelta(days=days)).isoformat()
    base = "WHERE status='closed' AND closed_at >= ?"
    total = _scalar(conn, f"SELECT COUNT(*) FROM predictions {base}", (cutoff,))
    win = _scalar(conn, f"SELECT COUNT(*) FROM predictions {base} AND outcome='win'", (cutoff,))
    loss = _scalar(conn, f"SELECT COUNT(*) FROM predictions {base} AND outcome='loss'", (cutoff,))
    neutral = _scalar(conn, f"SELECT COUNT(*) FROM predictions {base} AND outcome='neutral'", (cutoff,))
    avg_pnl = _scalar(
        conn,
        f"SELECT AVG(pnl_pct) FROM predictions {base} AND outcome IN ('win','loss','neutral')",
        (cutoff,), default=0.0,
    )
    recent = _rows(
        conn,
        """SELECT closed_at, name, ticker, signal, outcome, pnl_pct
           FROM predictions WHERE status='closed' AND outcome IN ('win','loss','neutral')
           ORDER BY closed_at DESC LIMIT 10""",
    )
    conn.close()
    return {
        "total": total, "win": win, "loss": loss, "neutral": neutral,
        "avg_pnl": round(float(avg_pnl or 0), 2), "recent": recent,
    }


def latest_briefing_actions() -> dict:
    """가장 최근 브리핑(같은 날)의 분류별 카운트 + 행."""
    conn = _conn()
    if conn is None:
        return {"day": "", "by_type": {}, "rows": []}
    latest = _scalar(conn, "SELECT MAX(created_at) FROM predictions", default="")
    if not latest:
        conn.close()
        return {"day": "", "by_type": {}, "rows": []}
    day = str(latest)[:10]
    rows = _rows(
        conn,
        """SELECT created_at, name, ticker, signal, action_type, account_type,
                  entry_price, target_price, briefing_type, normalizer_version
           FROM predictions WHERE created_at LIKE ? ORDER BY created_at DESC""",
        (f"{day}%",),
    )
    conn.close()
    by_type: dict[str, int] = {}
    for r in rows:
        k = r.get("action_type") or "(미분류)"
        by_type[k] = by_type.get(k, 0) + 1
    return {"day": day, "by_type": by_type, "rows": rows}


# ─── 적중률(accuracy_stats) ────────────────────────────
def accuracy_by_ticker() -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    rows = _rows(
        conn,
        """SELECT ticker, total_predictions, evaluated_count, wins, losses,
                  win_rate, avg_pnl, profit_factor, expectancy
           FROM accuracy_stats WHERE evaluated_count >= 1
           ORDER BY evaluated_count DESC, win_rate DESC""",
    )
    conn.close()
    return rows


# ─── 시스템 상태 ───────────────────────────────────────
def db_stats() -> dict:
    conn = _conn()
    if conn is None:
        return {"db_exists": False, "predictions": 0, "open": 0, "closed": 0,
                "v1": 0, "last_created": "", "last_closed": ""}
    out = {
        "db_exists": True,
        "predictions": _scalar(conn, "SELECT COUNT(*) FROM predictions"),
        "open": _scalar(conn, "SELECT COUNT(*) FROM predictions WHERE status='open'"),
        "closed": _scalar(conn, "SELECT COUNT(*) FROM predictions WHERE status='closed'"),
        "v1": _scalar(conn, "SELECT COUNT(*) FROM predictions WHERE normalizer_version='v1'"),
        "last_created": _scalar(conn, "SELECT MAX(created_at) FROM predictions", default=""),
        "last_closed": _scalar(conn, "SELECT MAX(closed_at) FROM predictions WHERE closed_at != ''", default=""),
    }
    conn.close()
    return out


def service_status(service: str = "stock-bot") -> dict:
    """systemctl show 기반 읽기 전용 서비스 상태. 실패 시 unknown."""
    out = {"service": service, "active": "unknown", "sub": "", "since": ""}
    try:
        r = subprocess.run(
            ["systemctl", "show", service,
             "--property=ActiveState,SubState,ActiveEnterTimestamp", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("ActiveState="):
                    out["active"] = line.split("=", 1)[1]
                elif line.startswith("SubState="):
                    out["sub"] = line.split("=", 1)[1]
                elif line.startswith("ActiveEnterTimestamp="):
                    raw = line.split("=", 1)[1].strip()
                    # UTC → KST 변환
                    if raw:
                        try:
                            from datetime import datetime as _dt
                            dt = _dt.strptime(raw, "%a %Y-%m-%d %H:%M:%S %Z")
                            dt_kst = dt.replace(tzinfo=timezone.utc).astimezone(KST)
                            out["since"] = dt_kst.strftime("%Y-%m-%d %H:%M KST")
                        except Exception:
                            out["since"] = raw
                    else:
                        out["since"] = raw
    except Exception:
        pass
    return out


def system_status() -> dict:
    """대시보드 상단 종합 상태."""
    return {
        "now": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "db": db_stats(),
        "service": service_status(),
        "latest_briefing": latest_briefing_actions(),
    }


def health() -> dict:
    """DB 유무와 무관하게 항상 정상 응답."""
    db = _conn()
    db_ok = db is not None
    if db:
        db.close()
    return {
        "status": "ok",
        "db_available": db_ok,
        "now": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }


# ═══════════════════════════════════════════════════════════
# 2차 확장 API — 모두 읽기 전용, 예외 안전
# ═══════════════════════════════════════════════════════════


# ─── /api/market ──────────────────────────────────────────
def _safe(v, default=0.0):
    """NaN/Inf를 default로 치환 — JSON 직렬화 안전."""
    import math
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return default
    return v


def _fetch_market_raw() -> dict:
    """지수/매크로 시세 + 장 상태. 내부용(캐시 래핑)."""
    from config.settings import INDICES, MACRO
    from core.market import _batch_quotes
    from core.market_hours import get_market_session, market_status_text

    ticker_map = {**INDICES, **MACRO}
    quotes = _batch_quotes(ticker_map)

    def _q(q):
        return {"price": _safe(q.price), "change": _safe(q.change),
                "pct": round(_safe(q.pct), 2),
                "high": _safe(q.high), "low": _safe(q.low)}

    indices = {v: _q(quotes[k]) for k, v in INDICES.items() if k in quotes}
    macro = {v: _q(quotes[k]) for k, v in MACRO.items() if k in quotes}

    # VIX 기반 시장 모드
    vix_price = 0.0
    for k, v in MACRO.items():
        if v == "VIX" and k in quotes:
            vix_price = quotes[k].price
    mode = "정상"
    if vix_price >= 35:
        mode = "위험"
    elif vix_price >= 25:
        mode = "주의"

    session = get_market_session()
    return {
        "indices": indices,
        "macro": macro,
        "session": session,
        "status_text": market_status_text(),
        "mode": mode,
        "vix": round(vix_price, 2),
        "now": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }


def market_data() -> dict:
    """시장 데이터 (60초 캐시)."""
    return _cached("market", 60, _fetch_market_raw)


# ─── /api/portfolio ───────────────────────────────────────
def _fetch_portfolio_raw() -> dict:
    """전 계좌 보유종목 + 현재가 + 수익률. 내부용(캐시 래핑)."""
    from config.settings import (
        HOLDINGS_GENERAL, HOLDINGS_RIA, HOLDINGS_IRP,
        HOLDINGS_PENSION, HOLDINGS_ISA,
        DEFAULT_CASH, RIA_CASH, IRP_CASH, PENSION_MMF, ISA_CASH,
        IRP_DEFAULT_OPTION,
        PORTFOLIO, HOLDING_STRATEGY,
    )
    from core.market import _batch_quotes

    accounts = [
        ("일반", HOLDINGS_GENERAL, DEFAULT_CASH),
        ("RIA", HOLDINGS_RIA, RIA_CASH),
        # IRP 현금 = 예수금 + 디폴트옵션(안정투자형) 잔고
        ("IRP", HOLDINGS_IRP, IRP_CASH + IRP_DEFAULT_OPTION),
        ("연금저축", HOLDINGS_PENSION, PENSION_MMF),
        ("ISA", HOLDINGS_ISA, ISA_CASH),
    ]

    # 모든 티커 수집 → 배치 조회
    all_tickers: dict[str, str] = {}
    for _, holdings, _ in accounts:
        for t in holdings:
            all_tickers[t] = PORTFOLIO.get(t, t)
    quotes = _batch_quotes(all_tickers) if all_tickers else {}

    # USDKRW
    usdkrw = 1.0
    try:
        usd_q = _batch_quotes({"USDKRW=X": "원달러"})
        if "USDKRW=X" in usd_q:
            usdkrw = usd_q["USDKRW=X"].price or 1.0
    except Exception:
        usdkrw = 1400.0

    result_accounts = []
    total_eval = 0.0
    total_cost = 0.0

    for acct_name, holdings, cash in accounts:
        items = []
        acct_eval = 0.0
        acct_cost = 0.0
        for ticker, info in holdings.items():
            shares = info.get("shares", 0)
            avg_krw = info.get("avg_cost_krw", 0)
            avg_usd = info.get("avg_cost_usd", 0)
            is_usd = avg_usd > 0

            q = quotes.get(ticker)
            cur_price = q.price if q else 0.0
            pct = q.pct if q else 0.0

            if is_usd:
                cost_total = avg_usd * shares
                eval_total = cur_price * shares
                pnl_pct = ((cur_price - avg_usd) / avg_usd * 100) if avg_usd else 0
                eval_krw = eval_total * usdkrw
                cost_krw = cost_total * usdkrw
            else:
                cost_total = avg_krw * shares
                eval_total = cur_price * shares
                pnl_pct = ((cur_price - avg_krw) / avg_krw * 100) if avg_krw else 0
                eval_krw = eval_total
                cost_krw = cost_total

            strategy = HOLDING_STRATEGY.get(ticker, {})
            name = PORTFOLIO.get(ticker, ticker)

            items.append({
                "ticker": ticker,
                "name": name,
                "shares": shares,
                "avg_cost": _safe(avg_usd if is_usd else avg_krw),
                "currency": "USD" if is_usd else "KRW",
                "current_price": round(_safe(cur_price), 2),
                "day_pct": round(_safe(pct), 2),
                "pnl_pct": round(_safe(pnl_pct), 2),
                "eval_krw": round(_safe(eval_krw)),
                "horizon": strategy.get("horizon", ""),
                "thesis": strategy.get("thesis", ""),
            })
            acct_eval += eval_krw
            acct_cost += cost_krw

        cash_krw = float(cash) if cash else 0
        result_accounts.append({
            "name": acct_name,
            "cash": round(cash_krw),
            "items": items,
            "eval_total": round(acct_eval),
            "cost_total": round(acct_cost),
            "pnl_pct": round((acct_eval - acct_cost) / acct_cost * 100, 2) if acct_cost else 0,
        })
        total_eval += acct_eval + cash_krw
        total_cost += acct_cost + cash_krw

    raw_pnl = (total_eval - total_cost) / total_cost * 100 if total_cost else 0

    # 비중 계산 (전체 평가금 대비)
    total_cash = sum(a["cash"] for a in result_accounts)
    grand_total = total_eval  # 이미 cash 포함
    allocation = []
    for acct in result_accounts:
        for it in acct["items"]:
            it["weight"] = round(it["eval_krw"] / grand_total * 100, 1) if grand_total else 0
        acct["weight"] = round((acct["eval_total"] + acct["cash"]) / grand_total * 100, 1) if grand_total else 0
    cash_weight = round(total_cash / grand_total * 100, 1) if grand_total else 0

    # 자산군 분류 (도넛 차트용)
    cat = {"ETF": 0, "국내주식": 0, "해외주식": 0, "현금": total_cash}
    for acct in result_accounts:
        for it in acct["items"]:
            t = it["ticker"]
            if ".KS" in t and any(k in it["name"] for k in ("TIGER", "KODEX", "PLUS", "나스닥", "S&P", "선진국", "고배당", "중국")):
                cat["ETF"] += it["eval_krw"]
            elif ".KS" in t or ".KQ" in t:
                cat["국내주식"] += it["eval_krw"]
            else:
                cat["해외주식"] += it["eval_krw"]
    allocation = [{"name": k, "value": round(v), "pct": round(v / grand_total * 100, 1) if grand_total else 0}
                  for k, v in cat.items() if v > 0]

    return {
        "accounts": result_accounts,
        "total_eval": round(_safe(total_eval)),
        "total_pnl_pct": round(_safe(raw_pnl), 2),
        "total_cash": round(total_cash),
        "cash_weight": cash_weight,
        "allocation": allocation,
        "usdkrw": round(_safe(usdkrw), 2),
        "now": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }


def portfolio_data() -> dict:
    """포트폴리오 데이터 (60초 캐시)."""
    return _cached("portfolio", 60, _fetch_portfolio_raw)


# ─── /api/performance ────────────────────────────────────
def performance_data(days: int = 30) -> dict:
    """action_type / briefing_type / ticker 별 성과 집계."""
    conn = _conn()
    if conn is None:
        return {"by_action_type": [], "by_briefing_type": [], "by_ticker": [],
                "summary": {}}
    cutoff = (datetime.now(KST) - timedelta(days=days)).isoformat()
    base = "status='closed' AND closed_at >= ? AND outcome IN ('win','loss','neutral')"

    def _group_stats(group_col: str) -> list[dict]:
        sql = f"""SELECT {group_col} as grp,
                         COUNT(*) as total,
                         SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                         SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                         ROUND(AVG(pnl_pct),2) as avg_pnl,
                         ROUND(100.0*SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END)/COUNT(*),1) as win_rate
                  FROM predictions WHERE {base}
                  GROUP BY {group_col} ORDER BY total DESC"""
        return _rows(conn, sql, (cutoff,))

    by_action = _group_stats("action_type")
    by_briefing = _group_stats("briefing_type")
    by_ticker = _group_stats("ticker")

    total = _scalar(conn, f"SELECT COUNT(*) FROM predictions WHERE {base}", (cutoff,))
    wins = _scalar(conn, f"SELECT COUNT(*) FROM predictions WHERE {base} AND outcome='win'", (cutoff,))
    avg = _scalar(conn, f"SELECT AVG(pnl_pct) FROM predictions WHERE {base}", (cutoff,), 0.0)
    conn.close()

    return {
        "days": days,
        "summary": {
            "total": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "avg_pnl": round(float(avg or 0), 2),
        },
        "by_action_type": by_action,
        "by_briefing_type": by_briefing,
        "by_ticker": by_ticker,
    }


# ─── /api/ticker/{ticker} ────────────────────────────────
def ticker_detail(ticker: str) -> dict:
    """특정 종목의 추천 이력 + 미결 + 종료 + 적중률 + 현재가."""
    conn = _conn()
    recent = []
    opens = []
    closed = []
    acc = {}
    if conn is not None:
        recent = _rows(conn,
            """SELECT created_at, signal, action_type, action_grade, account_type,
                      entry_price, target_price, stop_loss, confidence, status, outcome,
                      pnl_pct, briefing_type, normalizer_version, reasoning
               FROM predictions WHERE ticker=? ORDER BY created_at DESC LIMIT 20""",
            (ticker,))
        opens = _rows(conn,
            """SELECT created_at, signal, action_type, account_type, entry_price,
                      target_price, stop_loss, confidence, invalidation_condition
               FROM predictions WHERE ticker=? AND status='open'
               ORDER BY created_at DESC""",
            (ticker,))
        closed = _rows(conn,
            """SELECT closed_at, signal, outcome, pnl_pct, action_type
               FROM predictions WHERE ticker=? AND status='closed'
               AND outcome IN ('win','loss','neutral')
               ORDER BY closed_at DESC LIMIT 10""",
            (ticker,))
        acc_rows = _rows(conn,
            """SELECT * FROM accuracy_stats WHERE ticker=?""",
            (ticker,))
        if acc_rows:
            acc = acc_rows[0]
        conn.close()

    # 현재가
    cur_price = 0.0
    day_pct = 0.0
    try:
        from core.market import _get_quote_realtime
        q = _get_quote_realtime(ticker)
        if q:
            cur_price = q.price
            day_pct = q.pct
    except Exception:
        pass

    # settings 정보
    name = ticker
    strategy = {}
    try:
        from config.settings import PORTFOLIO, HOLDING_STRATEGY
        name = PORTFOLIO.get(ticker, ticker)
        strategy = HOLDING_STRATEGY.get(ticker, {})
    except Exception:
        pass

    return {
        "ticker": ticker,
        "name": name,
        "current_price": round(cur_price, 2),
        "day_pct": round(day_pct, 2),
        "horizon": strategy.get("horizon", ""),
        "thesis": strategy.get("thesis", ""),
        "recent": recent,
        "open": opens,
        "closed": closed,
        "accuracy": acc,
    }


# ─── /api/recommendations/timeline ───────────────────────
_ACTION_LABELS = {
    "AI_NEW_BUY": "신규 매수",
    "CONDITIONAL_NEW_BUY": "조건부 매수",
    "AI_ADD_BUY": "추가 매수",
    "AI_SELL_MANAGEMENT": "보유 관리",
    "CANCEL_SELL": "매도 취소",
    "HOLD_REVIEW": "보유 점검",
    "WATCH_ONLY": "관망",
}


def recommendations_timeline(
    range_: str = "today",
    ticker: str | None = None,
    action_type: str | None = None,
    order: str = "desc",
) -> dict:
    """추천 타임라인 — DB predictions read-only 조회."""
    conn = _conn()
    if conn is None:
        return {"items": [], "count": 0, "range": range_}

    now = datetime.now(KST)
    if range_ == "today":
        cutoff = now.strftime("%Y-%m-%d")
    elif range_ == "7d":
        cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    elif range_ == "30d":
        cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    else:
        cutoff = now.strftime("%Y-%m-%d")

    where = ["created_at >= ?"]
    params: list = [cutoff]
    if ticker:
        where.append("ticker = ?")
        params.append(ticker)
    if action_type:
        where.append("action_type = ?")
        params.append(action_type)

    direction = "ASC" if order == "asc" else "DESC"
    sql = f"""SELECT created_at, ticker, name, signal, action_type,
                     account_type, entry_price, target_price, stop_loss,
                     confidence, status, outcome, pnl_pct, briefing_type,
                     normalizer_version
              FROM predictions
              WHERE {' AND '.join(where)}
              ORDER BY created_at {direction}
              LIMIT 100"""
    rows = _rows(conn, sql, tuple(params))
    conn.close()

    # action_label 추가
    for r in rows:
        r["action_label"] = _ACTION_LABELS.get(r.get("action_type", ""), r.get("action_type", ""))

    return {"items": rows, "count": len(rows), "range": range_}


# ─── /api/news ────────────────────────────────────────────
def _fetch_news_raw() -> dict:
    """뉴스 수집 — 기존 캐시/로그 우선, RSS 폴백. AI 호출 없음."""
    articles: list[dict] = []
    error = ""

    # 1순위: 기존 브리핑 뉴스 캐시 (core/news.py의 캐시 파일)
    try:
        from pathlib import Path
        cache_dir = Path("db/data")
        cache_file = cache_dir / "news_cache.json"
        if cache_file.exists():
            import json
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "articles" in raw:
                articles = raw["articles"]
            elif isinstance(raw, list):
                articles = raw
    except Exception as e:
        log.debug("news cache read failed: %s", e)

    # 2순위: RSS 공개 소스 (비용 $0)
    if not articles:
        articles = _fetch_rss_news()

    # 카테고리/중요도 없으면 기본값 부여
    for a in articles:
        a.setdefault("category", "market")
        a.setdefault("sentiment", "neutral")
        a.setdefault("importance", 3)
        a.setdefault("tickers", [])
        a.setdefault("summary", a.get("title", ""))

    return {
        "articles": articles[:30],
        "count": len(articles[:30]),
        "cached_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "error": error,
    }


_TICKER_KEYWORDS = {
    "MU": ["micron", "마이크론", "hbm"],
    "NVDA": ["nvidia", "엔비디아"],
    "005930.KS": ["삼성전자", "samsung", "삼전"],
    "LMT": ["lockheed", "록히드"],
    "000660.KS": ["하이닉스", "hynix"],
    "462870.KS": ["시프트업", "shiftup", "스텔라"],
}
_NEG_WORDS = ["crash", "plunge", "drop", "fall", "급락", "폭락", "하락", "위기", "매도", "공포", "침체"]
_POS_WORDS = ["surge", "rally", "jump", "record", "급등", "상승", "최고", "매수", "반등", "호재"]


def _translate_en_to_kr(text: str) -> str:
    """Google Translate 무료 엔드포인트로 영→한 번역. 실패 시 원문 반환."""
    import json
    from urllib.request import urlopen, Request
    if not text or not any(ord(c) < 128 for c in text[:20]):
        return text  # 이미 한글이면 스킵
    # 영어 비율이 낮으면 스킵
    ascii_ratio = sum(1 for c in text if ord(c) < 128) / max(len(text), 1)
    if ascii_ratio < 0.5:
        return text
    try:
        from urllib.parse import quote
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ko&dt=t&q={quote(text[:300])}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(seg[0] for seg in data[0] if seg[0])
    except Exception:
        return text


def _detect_sentiment(text: str) -> str:
    combined = text.lower()
    if any(w in combined for w in _NEG_WORDS):
        return "negative"
    if any(w in combined for w in _POS_WORDS):
        return "positive"
    return "neutral"


def _detect_tickers(text: str) -> list[str]:
    combined = text.lower()
    return [tk for tk, kws in _TICKER_KEYWORDS.items()
            if any(kw in combined for kw in kws)]


def _fetch_rss_news() -> list[dict]:
    """한국어 RSS (한경/매경/연합) 우선 + Yahoo Finance 영어(번역). 비용 $0."""
    import xml.etree.ElementTree as ET
    from urllib.request import urlopen, Request
    from urllib.error import URLError

    feeds = [
        # 한국어 RSS (1순위)
        ("https://www.hankyung.com/feed/finance", "korea", "한경 증권"),
        ("https://www.hankyung.com/feed/international", "us", "한경 글로벌"),
        ("https://www.mk.co.kr/rss/30100041/", "korea", "매경 증권"),
        ("https://www.yna.co.kr/rss/economy.xml", "korea", "연합 경제"),
        # 영어 RSS (폴백, 번역 처리)
        ("https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US", "us", "Yahoo Finance"),
    ]
    articles = []
    is_en_source = {"Yahoo Finance"}

    for url, cat, source in feeds:
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 SanjukDashboard/1.0"})
            with urlopen(req, timeout=8) as resp:
                raw = resp.read()
            root = ET.fromstring(raw)
            items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")

            for item in items[:8]:
                title = (item.findtext("title") or
                         item.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if not link:
                    link_el = item.find("{http://www.w3.org/2005/Atom}link")
                    if link_el is not None:
                        link = link_el.get("href", "")
                pub = (item.findtext("pubDate") or
                       item.findtext("{http://www.w3.org/2005/Atom}published") or
                       item.findtext("{http://purl.org/dc/elements/1.1/}date") or "").strip()
                desc = (item.findtext("description") or
                        item.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()

                if not title:
                    continue

                # 영어 소스 → 번역
                translated = False
                if source in is_en_source:
                    orig_title = title
                    title = _translate_en_to_kr(title)
                    if desc:
                        desc = _translate_en_to_kr(desc[:200])
                    translated = (title != orig_title)

                tickers = _detect_tickers(title + " " + desc)
                sentiment = _detect_sentiment(title + " " + desc)

                articles.append({
                    "title": title,
                    "source": source + (" 번역" if translated else ""),
                    "url": link,
                    "published_at": pub,
                    "category": cat,
                    "tickers": tickers,
                    "sentiment": sentiment,
                    "summary": desc[:200] if desc else title,
                    "importance": 4 if tickers else 3,
                })
        except (URLError, ET.ParseError, OSError) as e:
            log.warning("RSS fetch failed %s: %s", url, e)

    return articles


def news_data() -> dict:
    """뉴스 데이터 (10분 캐시)."""
    return _cached("news", 600, _fetch_news_raw)
