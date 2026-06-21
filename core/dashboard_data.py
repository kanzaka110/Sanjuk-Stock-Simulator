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


# ─── /api/signals — 실시간 기술 신호 (브리핑과 무관, 라이브 계산) ──
def _fetch_live_signals_raw() -> dict:
    """보유+워치리스트 종목의 실시간 기술 지표 신호 (RSI/MACD/볼린저 합류).

    브리핑(스케줄)과 무관하게 매 조회 시 yfinance로 라이브 계산한다.
    confluence_score(-4~+4) 기준 강한 신호 우선 정렬. 읽기 전용·참고용(실행 주문 아님).
    held(보유)는 매도 단정 대신 '보유 관리 관찰'로 완화 표기(장기 보유 원칙 존중).
    """
    from config.settings import PORTFOLIO, WATCHLIST
    from core.indicators import calculate_all

    held = set(PORTFOLIO)
    tickers = {**PORTFOLIO, **WATCHLIST}
    results = calculate_all(tickers, period="3mo")

    items: list[dict] = []
    for tk, r in results.items():
        is_held = tk in held
        score = int(r.confluence_score)
        if score >= 2:
            direction = "buy"
            rec = "강세 신호 · 보유 유지/추가 검토" if is_held else "매수 신호 · 신규 검토"
        elif score <= -2:
            direction = "sell"
            rec = "과열·약세 · 보유 관리 관찰" if is_held else "약세 · 관망"
        else:
            direction = "neutral"
            rec = "중립"
        items.append({
            "ticker": tk,
            "name": r.name,
            "held": is_held,
            "rsi": round(float(r.rsi), 1),
            "confluence_score": score,
            "confluence_label": r.confluence_label,
            "rsi_signal": int(r.rsi_signal),
            "macd_signal": int(r.macd_signal),
            "bb_signal": int(r.bb_signal),
            "bb_position": round(float(r.bb_position), 2),
            "direction": direction,
            "rec": rec,
        })
    # 강한 신호 우선 (합류 절대값 → RSI 극단)
    items.sort(key=lambda x: (abs(x["confluence_score"]), abs(x["rsi"] - 50)), reverse=True)
    return {
        "items": items,
        "count": len(items),
        "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
    }


def live_signals() -> dict:
    """실시간 기술 신호 (5분 캐시 — yfinance 호출 비용 완화)."""
    out = _cached("live_signals", 300, _fetch_live_signals_raw)
    return out if isinstance(out, dict) and out else {"items": [], "count": 0, "generated_at": ""}


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


# ─── /api/calendar — 이벤트 캘린더 (경제·실적·배당 D-day) ──
def _fetch_calendar_raw() -> dict:
    """경제 일정(ECONOMIC_CALENDAR) + 보유 종목 실적/배당 + D-day.

    읽기 전용. fundamentals(yfinance) 호출은 보유 종목에만 적용(비용 완화).
    """
    today = datetime.now(KST).date()
    items: list[dict] = []

    def _dday(date_str: str) -> int | None:
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            return (d - today).days
        except Exception:
            return None

    # 1) 경제/매크로 일정
    try:
        from config.settings import ECONOMIC_CALENDAR
        for date_str, name, importance in ECONOMIC_CALENDAR:
            dd = _dday(date_str)
            if dd is None or dd < -1:
                continue
            cat = "earnings" if "실적" in name else "economic"
            items.append({
                "date": date_str, "name": name, "category": cat,
                "importance": importance, "d_day": dd, "ticker": "",
            })
    except Exception as e:
        log.warning("calendar economic load failed: %s", e)

    # 2) 보유 종목 실적/배당 (fundamentals)
    try:
        from config.settings import PORTFOLIO
        from core.fundamentals import fetch_financial_data
        # 같은 날 이미 실적 이벤트가 있으면(ECONOMIC_CALENDAR 수기 등록 등) 중복 방지
        earnings_dates = {it["date"] for it in items if it["category"] == "earnings"}
        for ticker, name in PORTFOLIO.items():
            try:
                fin = fetch_financial_data(ticker, name)
            except Exception:
                fin = None
            if not fin:
                continue
            if fin.earnings_date:
                dd = _dday(fin.earnings_date)
                if dd is not None and dd >= -1 and fin.earnings_date not in earnings_dates:
                    items.append({
                        "date": fin.earnings_date, "name": f"{name} 실적 발표",
                        "category": "earnings", "importance": "HIGH",
                        "d_day": dd, "ticker": ticker,
                    })
                    earnings_dates.add(fin.earnings_date)
            if fin.dividend_yield and fin.dividend_yield > 0:
                items.append({
                    "date": "", "name": f"{name} 배당 {fin.dividend_yield}%",
                    "category": "dividend", "importance": "LOW",
                    "d_day": None, "ticker": ticker,
                })
    except Exception as e:
        log.warning("calendar earnings load failed: %s", e)

    # 날짜 있는 이벤트 우선 정렬(D-day 오름차순), 배당(날짜 없음)은 뒤로
    dated = sorted([i for i in items if i["d_day"] is not None], key=lambda x: x["d_day"])
    undated = [i for i in items if i["d_day"] is None]
    return {
        "items": dated + undated,
        "count": len(dated) + len(undated),
        "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
    }


def event_calendar() -> dict:
    """이벤트 캘린더 (6시간 캐시 — fundamentals 호출 비용 완화)."""
    out = _cached("calendar", 21600, _fetch_calendar_raw)
    return out if isinstance(out, dict) and out else {"items": [], "count": 0, "generated_at": ""}


# ─── /api/portfolio/analytics — 성과 분석 (기여도·벤치마크·승률) ──
# 자산군 ETF 식별 키워드 (_fetch_portfolio_raw 분류와 동일)
_ETF_NAME_HINTS = ("TIGER", "KODEX", "PLUS", "나스닥", "S&P", "선진국", "고배당", "중국")
# 리스크 임계값 (하드코딩 회피 — 한 곳에 모음)
_RISK_LOSS_PCT = -10.0       # 종목 평가손실 경고선
_RISK_WEIGHT_PCT = 25.0      # 단일 종목 집중 경고선
_CASH_MIN_PCT = 5.0          # 현금 비중 하한
_CASH_MAX_PCT = 40.0         # 현금 비중 상한
_PROTECTED_LABEL = "보유 관리 · 실행 매도 아님"


def _asset_class(ticker: str, name: str) -> str:
    """종목을 ETF / 국내주식 / 해외주식으로 분류 (_fetch_portfolio_raw 로직과 동일)."""
    t = ticker or ""
    nm = name or ""
    if ".KS" in t and any(k in nm for k in _ETF_NAME_HINTS):
        return "ETF"
    if ".KS" in t or ".KQ" in t:
        return "국내주식"
    return "해외주식"


def _is_protected(ticker: str) -> bool:
    """보유 보호 종목(예: MU) 여부 — action_normalizer 판정 재사용. 실패 시 False."""
    try:
        from core.action_normalizer import _is_sell_protected
        return bool(_is_sell_protected(ticker))
    except Exception:
        return False


def _fetch_portfolio_analytics_raw() -> dict:
    """종목별 수익 기여도 + 계좌별·자산군별 손익 + 집중도/리스크 + 벤치마크 + 승률.

    기존 portfolio_data / market_data / performance_data 재사용(추가 시세 호출 최소화).
    전부 읽기 전용 계산 — DB write 없음. 보호 종목(MU)은 기여도에 표시하되
    '보유 관리 · 실행 매도 아님'으로 라벨해 실행 매도처럼 보이지 않게 한다.
    """
    pf = portfolio_data()
    mk = market_data()
    perf = performance_data(30)

    # 전 종목 펼치기 (계좌 무관 합산)
    holdings: list[dict] = []
    for acct in pf.get("accounts", []):
        for it in acct.get("items", []):
            holdings.append({**it, "account": acct.get("name", "")})

    grand_eval = float(pf.get("total_eval", 0) or 0)
    total_cash = float(pf.get("total_cash", 0) or 0)
    cash_weight = float(pf.get("cash_weight", 0) or 0)

    # 1차 패스: 종목별 평가손익·비중·일간기여 (전체 평가손익 합계 산출용)
    rows: list[dict] = []
    weighted_day = 0.0
    total_pnl_krw = 0.0
    worst = None
    # 자산군 집계 (현금 포함)
    asset_val: dict[str, float] = {"ETF": 0.0, "국내주식": 0.0, "해외주식": 0.0}
    asset_pnl: dict[str, float] = {"ETF": 0.0, "국내주식": 0.0, "해외주식": 0.0}

    for it in holdings:
        ticker = it.get("ticker", "")
        name = it.get("name", "")
        eval_krw = float(it.get("eval_krw", 0) or 0)
        pnl_pct = float(it.get("pnl_pct", 0) or 0)
        day_pct = float(it.get("day_pct", 0) or 0)
        # cost_krw 역산 → 평가손익(원화)
        cost_krw = eval_krw / (1 + pnl_pct / 100) if pnl_pct != -100 else 0.0
        pnl_krw = eval_krw - cost_krw
        weight = (eval_krw / grand_eval * 100) if grand_eval else 0.0
        day_contribution = weight * day_pct / 100
        weighted_day += day_contribution
        total_pnl_krw += pnl_krw

        cls = _asset_class(ticker, name)
        asset_val[cls] += eval_krw
        asset_pnl[cls] += pnl_krw

        row = {
            "ticker": ticker, "name": name, "account": it.get("account", ""),
            "eval_krw": round(eval_krw), "cost_krw": round(cost_krw),
            "pnl_krw": round(pnl_krw), "pnl_pct": round(pnl_pct, 2),
            "day_pct": round(day_pct, 2), "weight": round(weight, 1),
            "day_contribution_pct": round(day_contribution, 3),
            "protected": _is_protected(ticker),
        }
        rows.append(row)
        if worst is None or pnl_pct < worst["pnl_pct"]:
            worst = row

    # 2차 패스: 전체 손익 대비 기여도 (전체 손익 0이면 0 처리)
    for row in rows:
        row["contribution_pct"] = (
            round(row["pnl_krw"] / total_pnl_krw * 100, 1) if total_pnl_krw else 0.0
        )

    contrib = sorted(rows, key=lambda x: x["pnl_krw"], reverse=True)
    top_contributors = contrib[:5]
    bottom_contributors = sorted(rows, key=lambda x: x["pnl_krw"])[:5]

    # 계좌별 요약 (eval/cost/cash/pnl_krw/pnl_pct/weight)
    accounts_summary: list[dict] = []
    for acct in pf.get("accounts", []):
        eval_total = float(acct.get("eval_total", 0) or 0)
        cost_total = float(acct.get("cost_total", 0) or 0)
        accounts_summary.append({
            "name": acct.get("name", ""),
            "eval_total": round(eval_total),
            "cost_total": round(cost_total),
            "cash": round(float(acct.get("cash", 0) or 0)),
            "pnl_krw": round(eval_total - cost_total),
            "pnl_pct": acct.get("pnl_pct", 0),
            "weight": acct.get("weight", 0),
        })

    # 자산군별 (현금 포함). 현금은 평가손익 0.
    asset_classes: list[dict] = []
    for cls in ("ETF", "국내주식", "해외주식"):
        val = asset_val[cls]
        if val <= 0:
            continue
        asset_classes.append({
            "name": cls, "value": round(val),
            "pct": round(val / grand_eval * 100, 1) if grand_eval else 0.0,
            "pnl_krw": round(asset_pnl[cls]),
        })
    if total_cash > 0:
        asset_classes.append({
            "name": "현금", "value": round(total_cash),
            "pct": round(total_cash / grand_eval * 100, 1) if grand_eval else 0.0,
            "pnl_krw": 0,  # 현금은 평가손익 없음
        })

    # 집중도
    by_weight = sorted(rows, key=lambda x: x["weight"], reverse=True)
    largest = by_weight[0] if by_weight else None
    concentration = {
        "top1_weight": round(by_weight[0]["weight"], 1) if by_weight else 0.0,
        "top3_weight": round(sum(r["weight"] for r in by_weight[:3]), 1),
        "largest_holding": (
            {"ticker": largest["ticker"], "name": largest["name"],
             "weight": largest["weight"]} if largest else None
        ),
        "cash_weight": round(cash_weight, 1),
    }

    # 리스크 플래그
    risk_flags: list[dict] = []
    for row in rows:
        if row["protected"]:
            # 보호 종목: 기여도엔 표시하되 실행 매도 아님을 명시
            risk_flags.append({
                "type": "protected", "ticker": row["ticker"], "name": row["name"],
                "message": f"{row['name']} {_PROTECTED_LABEL}",
            })
            continue  # 보호 종목은 손실/집중 경고로 매도 압박하지 않음
        if row["pnl_pct"] <= _RISK_LOSS_PCT:
            risk_flags.append({
                "type": "loss", "ticker": row["ticker"], "name": row["name"],
                "pnl_pct": row["pnl_pct"],
                "message": f"{row['name']} 평가손실 {row['pnl_pct']:.1f}% ({_RISK_LOSS_PCT:.0f}% 이하)",
            })
        if row["weight"] >= _RISK_WEIGHT_PCT:
            risk_flags.append({
                "type": "concentration", "ticker": row["ticker"], "name": row["name"],
                "weight": row["weight"],
                "message": f"{row['name']} 비중 {row['weight']:.0f}% ({_RISK_WEIGHT_PCT:.0f}% 이상 집중)",
            })
    if cash_weight < _CASH_MIN_PCT:
        risk_flags.append({
            "type": "cash_low", "message": f"현금 비중 {cash_weight:.0f}% ({_CASH_MIN_PCT:.0f}% 미만)",
        })
    elif cash_weight > _CASH_MAX_PCT:
        risk_flags.append({
            "type": "cash_high", "message": f"현금 비중 {cash_weight:.0f}% ({_CASH_MAX_PCT:.0f}% 초과)",
        })

    # 벤치마크: 시장 지수 일간 등락률 (KOSPI / S&P500 / NASDAQ)
    indices = mk.get("indices", {})
    bench = []
    for label in ("KOSPI", "S&P500", "NASDAQ"):
        q = indices.get(label)
        if q:
            bench.append({
                "name": label, "day_pct": round(float(q.get("pct", 0) or 0), 2),
                "vs_port": round(weighted_day - float(q.get("pct", 0) or 0), 2),
            })

    summary = perf.get("summary", {})
    return {
        "weighted_day_pct": round(weighted_day, 2),
        "total_eval": round(grand_eval),
        "total_pnl_pct": pf.get("total_pnl_pct", 0),
        "total_pnl_krw": round(total_pnl_krw),
        "total_cash": round(total_cash),
        "cash_weight": round(cash_weight, 1),
        "contributors": contrib,
        "top_contributors": top_contributors,
        "bottom_contributors": bottom_contributors,
        "top_winner": contrib[0] if contrib else None,
        "top_loser": contrib[-1] if contrib else None,
        "worst_holding": worst,
        "accounts": accounts_summary,
        "asset_classes": asset_classes,
        "concentration": concentration,
        "risk_flags": risk_flags,
        "benchmarks": bench,
        "realized": {
            "win_rate": summary.get("win_rate", 0),
            "avg_pnl": summary.get("avg_pnl", 0),
            "total": summary.get("total", 0),
            "wins": summary.get("wins", 0),
            "losses": summary.get("losses", 0),
        },
        "now": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }


def portfolio_analytics() -> dict:
    """포트폴리오 성과 분석 (60초 캐시)."""
    out = _cached("portfolio_analytics", 60, _fetch_portfolio_analytics_raw)
    return out if isinstance(out, dict) and out else {}


# ─── 브리핑/텔레그램용 기여도 요약 (읽기 전용 텍스트) ──────
def _fmt_man(krw: float) -> str:
    """원화 → '만원' 표기. 예: 1_200_000 → '+120만원'."""
    return f"{krw / 10000:+,.0f}만원"


def portfolio_contribution_summary() -> dict:
    """포트폴리오 기여도를 브리핑/텔레그램에 바로 넣을 짧은 요약(dict + text).

    실행 주문 아님 — 참고용. 보호 종목(MU)은 '보유 관리, 실행 매도 아님'으로 명시.
    """
    a = portfolio_analytics()
    if not a:
        return {"text": "포트폴리오 데이터 없음", "lines": [], "empty": True}

    total_eval = float(a.get("total_eval", 0) or 0)
    total_pnl_pct = a.get("total_pnl_pct", 0)
    top = a.get("top_contributors") or []
    bottom = a.get("bottom_contributors") or []
    conc = a.get("concentration") or {}

    lines: list[str] = [
        f"전체 평가액: {total_eval / 10000:,.0f}만원 / 손익 {total_pnl_pct:+.1f}%"
    ]

    if top:
        w = top[0]
        lines.append(
            f"수익 기여 1위: {w['name']} {_fmt_man(w['pnl_krw'])} "
            f"({w.get('contribution_pct', 0):+.0f}% 기여)"
        )
    if bottom:
        l = bottom[0]
        if l["pnl_krw"] < 0:  # 실제 손실 종목이 있을 때만
            lines.append(
                f"손실 기여 1위: {l['name']} {_fmt_man(l['pnl_krw'])} "
                f"({l.get('contribution_pct', 0):+.0f}% 기여)"
            )

    lines.append(
        f"집중도: 상위 3종목 {conc.get('top3_weight', 0):.0f}%, "
        f"현금 {conc.get('cash_weight', 0):.0f}%"
    )

    # 보호 종목 경고 (실행 매도 아님)
    protected = [f["name"] for f in a.get("risk_flags", []) if f.get("type") == "protected"]
    if protected:
        names = ", ".join(protected)
        lines.append(f"주의: {names}는 보호 종목 — 보유 관리, 실행 매도 아님")

    return {"text": "\n".join(lines), "lines": lines, "empty": False}


# ─── 액션 현재가/조건거리 계산 (read-only) ─────────────
def calc_price_context(
    current_price: float | None,
    entry_price: float | None,
    target_price: float | None,
    stop_loss: float | None,
    action_type: str | None = None,
) -> dict:
    """현재가 기준 조건 거리/도달 상태를 계산. read-only 참고용."""
    ctx: dict = {
        "current_price": current_price or 0,
        "entry_price": entry_price,
        "target_price": target_price,
        "stop_loss": stop_loss,
        "distance_to_entry_pct": None,
        "distance_to_target_pct": None,
        "distance_to_stop_pct": None,
        "condition_status": "unknown",
        "condition_label": "데이터 부족",
        "risk_label": "데이터 부족",
        "summary": "",
    }
    cur = current_price or 0
    if cur <= 0:
        return ctx

    parts = []

    # entry distance
    if entry_price and entry_price > 0:
        d = round((cur - entry_price) / entry_price * 100, 2)
        ctx["distance_to_entry_pct"] = d
        parts.append(f"조건가까지 {d:+.2f}%")
        # condition status (주로 조건부 매수에 사용)
        is_cond = action_type in ("CONDITIONAL_NEW_BUY",)
        if is_cond or action_type is None:
            if cur <= entry_price:
                ctx["condition_status"] = "reached"
                ctx["condition_label"] = "조건 도달"
            elif d <= 1.0:
                ctx["condition_status"] = "near"
                ctx["condition_label"] = "조건 근접"
            else:
                ctx["condition_status"] = "waiting"
                ctx["condition_label"] = "조건 대기"

    # target distance
    if target_price and target_price > 0:
        d = round((target_price - cur) / cur * 100, 2)
        ctx["distance_to_target_pct"] = d
        parts.append(f"목표까지 {d:+.2f}%")

    # stop distance
    if stop_loss and stop_loss > 0:
        d = round((stop_loss - cur) / cur * 100, 2)
        ctx["distance_to_stop_pct"] = d
        parts.append(f"손절까지 {d:+.2f}%")

    # risk label
    stop_d = ctx["distance_to_stop_pct"]
    target_d = ctx["distance_to_target_pct"]
    if stop_d is not None and target_d is not None:
        if abs(stop_d) <= 2.0:
            ctx["risk_label"] = "손절 근접"
        elif target_d is not None and target_d <= 2.0:
            ctx["risk_label"] = "목표 근접"
        else:
            ctx["risk_label"] = "손절 여유"
    # override for sell management
    if action_type == "AI_SELL_MANAGEMENT":
        ctx["condition_label"] = "보유 관리 · 실행 매도 아님"

    ctx["summary"] = " · ".join(parts) if parts else "데이터 부족"
    return ctx


# ─── /api/decision-brief — 의사결정 브리핑 카드 ───────────
_BUY_TYPES = ("AI_NEW_BUY", "AI_ADD_BUY", "CONDITIONAL_NEW_BUY")
_SELL_TYPES = ("AI_SELL_MANAGEMENT",)
_HOLD_TYPES = ("CANCEL_SELL", "HOLD_REVIEW")


def _fetch_decision_brief_raw() -> dict:
    """최근 브리핑을 6블록으로 구조화: 무슨일/왜중요/지금할일/하지말것/리스크/보호규칙.

    DB predictions(최근 같은 날) read-only. 실행 주문 아님 — 참고용 정리.
    """
    conn = _conn()
    if conn is None:
        return {"day": "", "blocks": {}, "empty": True}
    latest = _scalar(conn, "SELECT MAX(created_at) FROM predictions", default="")
    if not latest:
        conn.close()
        return {"day": "", "blocks": {}, "empty": True}
    day = str(latest)[:10]
    rows = _rows(
        conn,
        """SELECT created_at, name, ticker, signal, action_type, account_type,
                  entry_price, target_price, stop_loss, confidence,
                  invalidation_condition, briefing_type, reasoning
           FROM predictions WHERE created_at LIKE ? ORDER BY confidence DESC""",
        (f"{day}%",),
    )
    conn.close()

    # 현재가 일괄 조회 (캐시 재사용)
    tickers_in_rows = list({r.get("ticker", "") for r in rows if r.get("ticker")})
    cur_prices: dict[str, float] = {}
    try:
        from core.market import _get_quote_realtime
        for tk in tickers_in_rows[:20]:
            q = _get_quote_realtime(tk)
            if q and q.price:
                cur_prices[tk] = q.price
    except Exception:
        pass

    do_now, conditionals, dont, risks = [], [], [], []
    for r in rows:
        at = r.get("action_type", "")
        label = _ACTION_LABELS.get(at, at)
        acct = r.get("account_type", "")
        name = r.get("name") or r.get("ticker", "")
        entry = r.get("entry_price")
        ticker = r.get("ticker", "")
        cur_price = cur_prices.get(ticker, 0.0)
        pctx = calc_price_context(cur_price, entry, r.get("target_price"),
                                   r.get("stop_loss"), at)
        item = {
            "ticker": ticker, "name": name, "account": acct,
            "action_type": at, "label": label, "signal": r.get("signal", ""),
            "entry_price": entry, "target_price": r.get("target_price"),
            "stop_loss": r.get("stop_loss"), "confidence": r.get("confidence"),
            "current_price": cur_price,
            "price_context": pctx,
            "condition_label": pctx["condition_label"],
            "distance_summary": pctx["summary"],
        }
        if at in ("AI_NEW_BUY", "AI_ADD_BUY"):
            do_now.append(item)
        elif at == "CONDITIONAL_NEW_BUY":
            conditionals.append(item)
        elif at in _SELL_TYPES:
            do_now.append({**item, "side": "sell"})
        elif at in _HOLD_TYPES:
            dont.append({**item, "note": "보유 관리 · 실행 매도 아님"})
        elif at == "WATCH_ONLY":
            dont.append({**item, "note": "관망 — 신규 진입 보류"})
        inv = r.get("invalidation_condition")
        if inv:
            risks.append({"ticker": r.get("ticker", ""), "name": name, "invalidation": inv})

    briefing_type = rows[0].get("briefing_type", "") if rows else ""
    blocks = {
        # 무슨 일: 브리핑 종류 + 액션 수
        "what": {
            "briefing_type": briefing_type,
            "total": len(rows),
            "do_now": len(do_now),
            "conditional": len(conditionals),
        },
        # 왜 중요: 가장 신뢰도 높은 액션의 근거 일부
        "why": (rows[0].get("reasoning", "")[:300] if rows else ""),
        "do_now": do_now,           # 지금 할 일
        "conditional": conditionals,  # 조건 충족 시
        "dont": dont,               # 하지 말 것
        "risks": risks[:6],         # 리스크/무효화 조건
        # 사용자 보호 규칙 (고정)
        "guardrails": [
            "표시된 수치는 참고용이며 실제 주문이 아닙니다.",
            "장기 보유 종목은 단기 변동으로 매도하지 않습니다.",
            "조건부 매수는 조건 충족 전 즉시 체결하지 않습니다.",
        ],
    }
    return {"day": day, "blocks": blocks, "empty": len(rows) == 0}


def decision_brief() -> dict:
    """의사결정 브리핑 (60초 캐시)."""
    out = _cached("decision_brief", 60, _fetch_decision_brief_raw)
    return out if isinstance(out, dict) and out else {"day": "", "blocks": {}, "empty": True}


# ─── /api/ticker/{ticker}/chart — OHLCV 차트 데이터 ──────
_CHART_RANGE_MAP: dict[str, tuple[str, str]] = {
    "1d":  ("1d",  "5m"),
    "5d":  ("5d",  "15m"),
    "1mo": ("1mo", "1d"),
    "3mo": ("3mo", "1d"),
}

# ticker 경로 안전 패턴 (영숫자 + . + - + = 만 허용)
_TICKER_SAFE = __import__("re").compile(r"^[A-Za-z0-9.\-=^]{1,20}$")


def _fetch_chart_raw(ticker: str, period: str, interval: str) -> dict:
    """yfinance history로 OHLCV 조회. 내부용(캐시 래핑).

    현재가/일간등락률은 기존 시세 체인(KIS→yfinance)에서 가져와
    전일종가 대비 정확한 day_pct를 제공한다.
    """
    import yfinance as yf

    tk = yf.Ticker(ticker)
    df = tk.history(period=period, interval=interval)
    if df is None or df.empty:
        return {"points": [], "current_price": 0.0, "day_pct": 0.0,
                "source": "yfinance"}

    points: list[dict] = []
    for idx, row in df.iterrows():
        t = idx.strftime("%H:%M") if interval in ("5m", "15m") else idx.strftime("%m-%d")
        points.append({
            "time": t,
            "open": round(float(row.get("Open", 0)), 2),
            "high": round(float(row.get("High", 0)), 2),
            "low": round(float(row.get("Low", 0)), 2),
            "close": round(float(row.get("Close", 0)), 2),
            "volume": int(row.get("Volume", 0)),
        })

    last_close = points[-1]["close"] if points else 0.0

    # 현재가/day_pct: 기존 시세 체인(KIS 우선)에서 가져오기
    cur_price = last_close
    day_pct = 0.0
    source = "yfinance"
    try:
        from core.market import _get_quote_realtime
        q = _get_quote_realtime(ticker)
        if q and q.price:
            cur_price = q.price
            day_pct = round(q.pct, 2)
            # KIS 경유 판별: 국내 종목이고 KIS가 활성화되어 있으면 KIS
            is_kr = ticker.endswith(".KS") or ticker.endswith(".KQ")
            try:
                from core.market_kis import _is_kis_configured
                if is_kr and _is_kis_configured():
                    source = "KIS+yfinance"
            except Exception:
                pass
    except Exception:
        # 폴백: 차트 데이터에서 계산
        first_open = points[0]["open"] if points else 0.0
        day_pct = round(((last_close - first_open) / first_open * 100), 2) if first_open else 0.0

    return {
        "points": points,
        "current_price": cur_price,
        "day_pct": day_pct,
        "source": source,
    }


def ticker_chart_data(ticker: str, range_: str, interval: str) -> dict:
    """종목 차트 데이터 (60초 캐시). 실패해도 200 + error 필드."""
    now_str = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S")
    base = {
        "ticker": ticker, "name": ticker, "range": range_,
        "interval": interval, "source": "yfinance",
        "updated_at": now_str, "cache_age_sec": 0,
        "current_price": 0.0, "day_pct": 0.0,
        "points": [], "error": "",
    }

    # ticker 안전 검증
    if not _TICKER_SAFE.match(ticker):
        base["error"] = "invalid ticker format"
        return base

    # range/interval 매핑 (허용 외 → 안전 fallback)
    period, iv = _CHART_RANGE_MAP.get(range_, ("1d", "5m"))
    base["range"] = range_ if range_ in _CHART_RANGE_MAP else "1d"
    base["interval"] = iv

    # 이름 조회
    try:
        from config.settings import PORTFOLIO
        base["name"] = PORTFOLIO.get(ticker, ticker)
    except Exception:
        pass

    cache_key = f"chart:{ticker}:{base['range']}:{iv}"

    def _fetch():
        return _fetch_chart_raw(ticker, period, iv)

    # 정상 데이터는 60초 캐시, 빈 결과는 10초만 (빠른 재시도 허용)
    cached_result = _cached(cache_key, 60, _fetch)

    if not cached_result or not isinstance(cached_result, dict):
        base["error"] = "no data available"
        return base

    has_points = bool(cached_result.get("points"))

    # 빈 결과가 캐시됐으면 TTL을 10초로 줄여 재시도 허용
    if not has_points:
        with _cache_lock:
            entry = _cache.get(cache_key)
            if entry and time.monotonic() - entry[0] > 10:
                _cache.pop(cache_key, None)

    # cache_age_sec 계산
    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry:
            base["cache_age_sec"] = round(time.monotonic() - entry[0])

    base["points"] = cached_result.get("points", [])
    base["current_price"] = cached_result.get("current_price", 0.0)
    base["day_pct"] = cached_result.get("day_pct", 0.0)
    base["source"] = cached_result.get("source", "yfinance")
    base["updated_at"] = now_str

    if not base["points"]:
        base["error"] = "no data points"

    return base
