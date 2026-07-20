"""
대시보드 데이터 수집 — 조회 전용 (읽기 전용)

웹 대시보드(web/app.py)와 헬스체크용. 실주문/DB 수정 일절 없음.
DB가 없거나 비어 있어도 절대 예외를 던지지 않고 빈 구조를 반환한다.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
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


# Portfolio quote refresh is intentionally decoupled from request latency.
# KIS/yfinance can hang for tens of seconds per ticker; /api/portfolio must
# keep returning broker/stale data instead of blocking mobile/dashboard loads.
_portfolio_quote_cache: dict[str, tuple[float, dict]] = {}
_portfolio_quote_refreshing: dict[str, threading.Event] = {}

_TOSS_READONLY_TIMEOUT_SEC = 3.0
_TOSS_ACCOUNT_SUMMARY_OK_TTL = 60
_TOSS_ACCOUNT_SUMMARY_STALE_TTL = 900
_TOSS_ACCOUNT_FAILURE_COOLDOWN_SEC = 300
_TOSS_BROKER_ORDERS_OK_TTL = 60
_TOSS_BROKER_ORDERS_STALE_TTL = 900
_TOSS_BROKER_ORDERS_FAILURE_COOLDOWN_SEC = 300
_TOSS_POLICY_OK_TTL = 60
_TOSS_POLICY_STALE_TTL = 900
_toss_account_summary_last_good: tuple[float, dict] | None = None
_toss_account_summary_cooldown_until = 0.0
_toss_broker_orders_last_good: tuple[float, dict] | None = None
_toss_broker_orders_cooldown_until = 0.0
_toss_policy_refreshing: threading.Event | None = None


def _set_toss_readonly_timeout(seconds: float = _TOSS_READONLY_TIMEOUT_SEC) -> None:
    """Bound Toss read-only dashboard calls so broker outages do not hang GET APIs."""
    try:
        from core import toss_client as tc
        current = float(getattr(tc, "TIMEOUT", 10) or 10)
        if current > seconds:
            tc.TIMEOUT = seconds
    except Exception:
        pass


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _dashboard_toss_broker_reads_isolated() -> bool:
    """Return True when this process must consume the stock-bot snapshot.

    Role-only invariant (운영 모드 무관): the long-running bot/monitor process
    (or explicit TOSS_PROCESS_ROLE=broker_owner) is the sole Toss GET owner.
    Dashboard, scheduled briefings and tools read a sanitized atomic snapshot
    so they cannot rotate the broker token under an in-flight order. 정책
    모듈 실패·비-bool 반환 시에도 role 계약으로 fail-closed한다.
    """
    try:
        from core.toss_readonly_snapshot import should_consume_snapshot
        decision = should_consume_snapshot()
        if type(decision) is not bool:
            # 비-bool 정책 반환은 신뢰 불가 — role fallback으로
            raise RuntimeError("invalid_snapshot_policy_decision")
        return decision
    except Exception:
        # (2026-07-15 Task 4.1A) 정책 모듈이 깨져도 fail-open 금지 —
        # role 계약(명시 role → argv bot/monitor → consumer)으로 판정.
        role = str(os.environ.get("TOSS_PROCESS_ROLE", "")).strip().lower()
        if role == "broker_owner":
            return False
        if role == "snapshot_consumer":
            return True
        args = {str(arg).strip().lower() for arg in sys.argv[1:]}
        if args & {"bot", "monitor"}:
            return False
        return True


def _csv_env(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def _toss_live_policy_fallback(reason: str = "policy_refresh_pending") -> dict:
    """Cheap dashboard-only live policy when full policy computation is slow."""
    live_pilot_enabled = _env_truthy("TOSS_LIVE_PILOT_ENABLED")
    live_order_env = _env_truthy("TOSS_LIVE_ORDER_ALLOWED")
    adapter_enabled = _env_truthy("TOSS_LIVE_ADAPTER_ENABLED")
    transport_armed = _env_truthy("TOSS_LIVE_TRANSPORT_ARMED")
    autonomous_mode = _env_truthy("TOSS_AUTONOMOUS_MODE")
    autonomous_kill_switch = _env_truthy("TOSS_AUTONOMOUS_KILL_SWITCH")
    transport_status = "configured" if transport_armed else "not_configured"
    adapter_status = "enabled" if adapter_enabled else "disabled"
    all_live_gates_open = bool(
        live_pilot_enabled
        and live_order_env
        and adapter_enabled
        and transport_armed
        and not autonomous_kill_switch
    )
    return {
        "mode": "autonomous_live_pilot" if autonomous_mode and all_live_gates_open else "approval_only_live_pilot",
        "live_pilot_enabled": live_pilot_enabled,
        "live_order_allowed": all_live_gates_open,
        "adapter_status": adapter_status,
        "live_transport_status": transport_status,
        "autonomous_mode": autonomous_mode,
        "autonomous_kill_switch": autonomous_kill_switch,
        "all_live_gates_open": all_live_gates_open,
        "allowed_asset_types": _csv_env("TOSS_AUTONOMOUS_ALLOWED_ASSET_TYPES"),
        "allowed_sides": _csv_env("TOSS_AUTONOMOUS_ALLOWED_SIDES"),
        "cache_status": "fallback",
        "policy_error": reason,
        "read_only_notice": "대시보드 timeout 방지를 위한 env 기반 임시 policy",
    }


def _refresh_toss_live_policy() -> None:
    global _toss_policy_refreshing
    try:
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        data = compute_toss_live_pilot_policy() or {}
        if isinstance(data, dict):
            data = dict(data)
            data["cache_status"] = "live"
            with _cache_lock:
                _cache["toss_live_policy_fast"] = (time.monotonic(), data)
    except Exception as exc:
        with _cache_lock:
            _cache["toss_live_policy_last_error"] = (
                time.monotonic(),
                f"policy_refresh_failed:{type(exc).__name__}",
            )
    finally:
        with _cache_lock:
            event = _toss_policy_refreshing
            _toss_policy_refreshing = None
            if event:
                event.set()


def _toss_live_policy_fast(timeout: float = 0.25) -> dict:
    """Return live-pilot policy without letting KIS/quality checks block dashboard APIs."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        return compute_toss_live_pilot_policy() or {}

    global _toss_policy_refreshing
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get("toss_live_policy_fast")
        if entry:
            ts, cached = entry
            if now - ts < _TOSS_POLICY_OK_TTL:
                return copy.deepcopy(cached)
        else:
            cached = None
            ts = 0.0

        if _toss_policy_refreshing is None:
            _toss_policy_refreshing = threading.Event()
            threading.Thread(target=_refresh_toss_live_policy, name="toss-policy-refresh", daemon=True).start()
        event = _toss_policy_refreshing

    if cached and now - ts <= _TOSS_POLICY_STALE_TTL:
        out = copy.deepcopy(cached)
        out["cache_status"] = "stale"
        out["cache_age_sec"] = round(max(0.0, now - ts))
        return out

    if event:
        event.wait(timeout)
    with _cache_lock:
        entry = _cache.get("toss_live_policy_fast")
        if entry:
            ts, cached = entry
            out = copy.deepcopy(cached)
            if time.monotonic() - ts >= _TOSS_POLICY_OK_TTL:
                out["cache_status"] = "stale"
                out["cache_age_sec"] = round(max(0.0, time.monotonic() - ts))
            return out
        err = _cache.get("toss_live_policy_last_error")
    return _toss_live_policy_fallback(err[1] if err else "policy_refresh_pending")


def _portfolio_quote_cache_key(ticker_map: dict[str, str]) -> str:
    return "|".join(sorted(ticker_map))


def _refresh_portfolio_quotes(cache_key: str, ticker_map: dict[str, str], fetch_fn) -> None:
    try:
        quotes = fetch_fn(dict(ticker_map))
        if isinstance(quotes, dict):
            with _cache_lock:
                _portfolio_quote_cache[cache_key] = (time.monotonic(), quotes)
    except Exception as e:
        log.warning("portfolio quote refresh failed: %s", e)
    finally:
        with _cache_lock:
            event = _portfolio_quote_refreshing.pop(cache_key, None)
            if event:
                event.set()


def _portfolio_quotes_fast(
    ticker_map: dict[str, str],
    fetch_fn,
    ttl: int = 300,
    timeout: float = 3.0,
) -> dict:
    """Return cached quotes quickly and refresh slow providers in background.

    Production dashboard endpoints must not wait on a flaky KIS/yfinance batch.
    If a stale quote snapshot exists, return it immediately while one daemon
    refresh runs. On cold start, wait only a tiny bounded window; broker Excel
    values remain the fallback. Tests keep the old synchronous path.
    """
    if not ticker_map:
        return {}

    if "PYTEST_CURRENT_TEST" in os.environ:
        return fetch_fn(ticker_map)

    cache_key = _portfolio_quote_cache_key(ticker_map)
    now = time.monotonic()

    with _cache_lock:
        entry = _portfolio_quote_cache.get(cache_key)
        if entry:
            ts, cached_quotes = entry
            if now - ts < ttl:
                return dict(cached_quotes)
        else:
            cached_quotes = {}

        event = _portfolio_quote_refreshing.get(cache_key)
        if event is None:
            event = threading.Event()
            _portfolio_quote_refreshing[cache_key] = event
            threading.Thread(
                target=_refresh_portfolio_quotes,
                args=(cache_key, dict(ticker_map), fetch_fn),
                name="portfolio-quote-refresh",
                daemon=True,
            ).start()

    # Stale-but-known quotes are better than blocking the request path.
    if cached_quotes:
        return dict(cached_quotes)

    # Cold start: give the provider a very short chance, then fall back to
    # broker snapshot/cost values. The background thread will populate cache.
    event.wait(timeout)
    with _cache_lock:
        entry = _portfolio_quote_cache.get(cache_key)
        return dict(entry[1]) if entry else {}


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


def _market_risk_context(
    indices: dict,
    vix_price: float,
    *,
    now_utc: datetime | None = None,
) -> dict:
    """VIX와 신뢰 가능한 국내 지수 급락을 반영한 read-only 위험 문맥."""
    import math

    trusted_sources = frozenset({"yf_batch", "yf_fast", "kis", "kis_domestic"})
    max_age_sec = 180.0
    future_skew_sec = 30.0
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_ts = now.timestamp()

    mode = "정상"
    if vix_price >= 35:
        mode = "위험"
    elif vix_price >= 25:
        mode = "주의"

    index_labels = ("KOSPI", "KOSDAQ")
    observed: dict[str, float] = {}
    missing_indices: list[str] = []
    untrusted_indices: list[str] = []
    stale_indices: list[str] = []
    index_provenance: dict[str, dict] = {}
    for label in index_labels:
        row = indices.get(label) if isinstance(indices, dict) else None
        if not isinstance(row, dict):
            missing_indices.append(label)
            continue
        raw_pct = row.get("pct")
        if raw_pct is None or isinstance(raw_pct, bool):
            missing_indices.append(label)
            continue
        try:
            pct = float(str(raw_pct))
        except (TypeError, ValueError):
            missing_indices.append(label)
            continue
        if not math.isfinite(pct):
            missing_indices.append(label)
            continue

        source = str(row.get("source") or "").lower().strip()
        raw_as_of = row.get("as_of")
        index_provenance[label] = {"source": source or "missing", "as_of": raw_as_of}
        if source not in trusted_sources:
            untrusted_indices.append(label)
            continue
        try:
            as_of = float(str(raw_as_of))
        except (TypeError, ValueError):
            stale_indices.append(label)
            continue
        if not math.isfinite(as_of):
            stale_indices.append(label)
            continue
        age_sec = now_ts - as_of
        index_provenance[label]["age_sec"] = round(age_sec, 1)
        if age_sec < -future_skew_sec or age_sec > max_age_sec:
            stale_indices.append(label)
            continue
        observed[label] = pct

    trigger_index = None
    trigger_pct = None
    local_level = "정상"
    if observed:
        trigger_index, trigger_pct = min(observed.items(), key=lambda item: item[1])
        if trigger_pct <= -5.0:
            local_level = "위험"
        elif trigger_pct <= -3.0:
            local_level = "주의"

    severity = {"정상": 0, "주의": 1, "위험": 2}
    if severity[local_level] > severity[mode]:
        mode = local_level
    provenance_incomplete = bool(missing_indices or untrusted_indices or stale_indices)
    if provenance_incomplete and mode == "정상":
        mode = "주의"
    local_market_shock = local_level != "정상"
    return {
        "mode": mode,
        "local_market_shock": local_market_shock,
        "local_market_shock_level": local_level,
        "trigger_index": trigger_index if local_market_shock else None,
        "trigger_pct": trigger_pct if local_market_shock else None,
        "shock_threshold_pct": -3.0,
        "indices": observed,
        "local_indices_complete": not provenance_incomplete,
        "local_indices_trusted": not provenance_incomplete,
        "missing_indices": missing_indices,
        "untrusted_indices": untrusted_indices,
        "stale_indices": stale_indices,
        "index_provenance": index_provenance,
        "trusted_sources": sorted(trusted_sources),
        "max_age_sec": max_age_sec,
        "future_skew_sec": future_skew_sec,
    }


def _fetch_market_raw() -> dict:
    """지수/매크로 시세 + 장 상태. 내부용(캐시 래핑)."""
    from config.settings import INDICES, MACRO
    from core.market import _batch_quotes
    from core.market_hours import get_market_session, market_status_text

    ticker_map = {**INDICES, **MACRO}
    quotes = _batch_quotes(ticker_map)

    def _q(q):
        import math

        raw_pct = getattr(q, "pct", None)
        try:
            pct = (
                float(str(raw_pct))
                if raw_pct is not None and not isinstance(raw_pct, bool)
                else None
            )
        except (TypeError, ValueError):
            pct = None
        if pct is not None and not math.isfinite(pct):
            pct = None
        return {
            "price": _safe(getattr(q, "price", 0.0)),
            "change": _safe(getattr(q, "change", 0.0)),
            "pct": round(pct, 2) if pct is not None else None,
            "high": _safe(getattr(q, "high", 0.0)),
            "low": _safe(getattr(q, "low", 0.0)),
            "source": str(getattr(q, "source", "") or ""),
            "as_of": _safe(getattr(q, "as_of", None)),
        }

    indices = {v: _q(quotes[k]) for k, v in INDICES.items() if k in quotes}
    macro = {v: _q(quotes[k]) for k, v in MACRO.items() if k in quotes}

    # VIX + 국내 지수 급락 기반 시장 모드
    vix_price = 0.0
    for k, v in MACRO.items():
        if v == "VIX" and k in quotes:
            vix_price = quotes[k].price
    risk_context = _market_risk_context(indices, float(vix_price or 0.0))

    session = get_market_session()
    from core.market_hours import market_reliability_context
    reliability = market_reliability_context()
    return {
        "indices": indices,
        "macro": macro,
        "session": session,
        "status_text": market_status_text(),
        "mode": risk_context["mode"],
        "market_risk": risk_context,
        "local_market_shock": risk_context["local_market_shock"],
        "vix": round(vix_price, 2),
        "now": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "market_reliability": reliability,
        "quote_trust_label": reliability["trust_label"],
        "market_status_summary": reliability["summary"],
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
        ACCOUNT_PRINCIPAL_KRW, TOTAL_PRINCIPAL_KRW,
    )
    from core.market import _batch_quotes

    from config.settings import HOLDINGS_AS_OF
    from core.portfolio_live import effective_holdings, pending_trades

    # USDKRW 먼저 조회 — USD 보유 평가 + 미반영 매매 현금 델타 환산에 필요.
    # FX quote providers occasionally return a 100x-scale or wrong cross value.
    # Keep portfolio valuation in a plausible USD/KRW band instead of exploding totals.
    usdkrw = 0.0
    fx_source = "quote"
    try:
        usd_q = _batch_quotes({"USDKRW=X": "원달러"})
        if "USDKRW=X" in usd_q:
            usdkrw = float(usd_q["USDKRW=X"].price or 0)
    except Exception:
        usdkrw = 0.0
    if not (800 <= usdkrw <= 2500):
        usdkrw = 1554.4
        fx_source = "fallback_const"

    # 라이브 합성: settings HOLDINGS/CASH(base) + 텔레그램 미반영 매매(applied=0).
    # "매매반영" 처리 후에는 델타 0 → settings 값 그대로 (core/portfolio_live.py).
    # 단위 테스트는 monkeypatch된 settings만으로 결정론적으로 검증 (실 DB 미접근).
    if "PYTEST_CURRENT_TEST" in os.environ:
        pending_by_account, pending_warnings = {}, []
    else:
        pending_by_account, pending_warnings = pending_trades(as_of=HOLDINGS_AS_OF)

    base_accounts = [
        ("일반", HOLDINGS_GENERAL, DEFAULT_CASH),
        ("RIA", HOLDINGS_RIA, RIA_CASH),
        ("IRP", HOLDINGS_IRP, IRP_CASH + IRP_DEFAULT_OPTION),
        ("연금저축", HOLDINGS_PENSION, PENSION_MMF),
        ("ISA", HOLDINGS_ISA, ISA_CASH),
    ]
    accounts = []
    account_pending_meta: dict[str, dict] = {}
    total_pending_trades = 0
    for _acct_name, _base_h, _base_c in base_accounts:
        _h, _c, _meta = effective_holdings(
            _acct_name, _base_h, _base_c, usdkrw,
            pending_by_account=pending_by_account,
        )
        accounts.append((_acct_name, _h, _c))
        account_pending_meta[_acct_name] = _meta
        total_pending_trades += int(_meta.get("pending_trade_count", 0))
        pending_warnings.extend(_meta.get("pending_notes", []))

    # 모든 티커 수집 → 배치 조회 (수량 × 라이브 시세 단일 경로)
    all_tickers: dict[str, str] = {}
    for _, holdings, _ in accounts:
        for t, info in holdings.items():
            all_tickers[t] = PORTFOLIO.get(t, info.get("name", t))
    quotes = _portfolio_quotes_fast(all_tickers, _batch_quotes) if all_tickers else {}
    _now_epoch = time.time()

    def _price_sanity_limit(ticker: str, avg_price: float, is_usd: bool) -> float:
        """Return max plausible quote/avg ratio for portfolio valuation.

        Dashboard quotes can occasionally arrive with split/currency scale errors
        (e.g. KR equity 5x, US equity 10x). Portfolio valuation should not let
        one bad quote inflate total assets. The limit is intentionally loose so
        real winners still show gains; extreme moves fall back to cost and are
        flagged on the item for reconciliation.
        """
        if avg_price <= 0:
            return 0.0
        # Long-term legacy Samsung Electronics has a very low historical cost.
        # Expected dashboard total currently assumes this quote is real,
        # so do not clamp it at the generic 3~4x guard.
        if ticker == "005930.KS":
            return 6.0
        # Most ETF/current holdings should not exceed 3x without a split/source issue.
        if is_usd:
            return 4.0
        return 3.5

    def _guard_portfolio_quote(ticker: str, cur_price: float, avg_price: float, is_usd: bool) -> tuple[float, dict]:
        """Clamp obviously bad quotes for portfolio totals, preserving diagnostics."""
        note = {"price_guard": "ok"}
        if not cur_price or cur_price <= 0:
            note.update({
                "price_guard": "missing",
                "raw_price": round(_safe(cur_price), 2),
                "valuation_price": round(_safe(avg_price), 2),
                "price_warning": "현재가 조회 실패 — 평가액은 평단 기준 보수 계산",
            })
            return avg_price or 0.0, note
        limit = _price_sanity_limit(ticker, avg_price, is_usd)
        ratio = (cur_price / avg_price) if avg_price else 0.0
        if limit and ratio > limit:
            note.update({
                "price_guard": "clamped_high",
                "raw_price": round(_safe(cur_price), 2),
                "valuation_price": round(_safe(avg_price), 2),
                "price_ratio": round(_safe(ratio), 2),
                "price_warning": f"가격 이상치 의심: 현재가/평단 {ratio:.1f}배 — 총평가액은 평단 기준 보수 계산",
            })
            return avg_price, note
        return cur_price, note

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
            raw_price = q.price if q else 0.0
            pct = q.pct if q else 0.0
            avg_price = avg_usd if is_usd else avg_krw
            cur_price, price_note = _guard_portfolio_quote(ticker, raw_price, avg_price, is_usd)

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
            name = info.get("name") or PORTFOLIO.get(ticker, ticker)

            items.append({
                "ticker": ticker,
                "name": name,
                "shares": shares,
                "avg_cost": _safe(avg_usd if is_usd else avg_krw),
                "currency": "USD" if is_usd else "KRW",
                "current_price": round(_safe(cur_price), 2),
                "raw_price": round(_safe(raw_price), 2),
                "day_pct": round(_safe(pct), 2),
                "day_pct_source": "quote" if q else "missing_quote",
                "price_source": (getattr(q, "source", "") or "quote") if q else "",
                "price_age_sec": (
                    round(max(0.0, _now_epoch - q.as_of))
                    if q and getattr(q, "as_of", 0) else None
                ),
                "pnl_pct": round(_safe(pnl_pct), 2),
                "eval_krw": round(_safe(eval_krw)),
                "horizon": strategy.get("horizon", ""),
                "thesis": strategy.get("thesis", ""),
                **price_note,
            })
            acct_eval += eval_krw
            acct_cost += cost_krw

        cash_krw = float(cash) if cash else 0
        principal = float(ACCOUNT_PRINCIPAL_KRW.get(acct_name, 0) or 0)
        acct_asset_total = acct_eval + cash_krw
        acct_pnl_krw = acct_eval - acct_cost
        acct_today_chg = 0.0
        acct_today_available = False
        for _it in items:
            _dp = _it.get("day_pct")
            if _dp is None:
                continue
            if _it.get("day_pct_source") == "missing_quote":
                continue
            try:
                _prev = _it.get("eval_krw", 0) / (1 + float(_dp) / 100)
                acct_today_chg += _it.get("eval_krw", 0) - _prev
                acct_today_available = True
            except Exception:
                pass
        result_accounts.append({
            "name": acct_name,
            "cash": round(cash_krw),
            "items": items,
            "eval_total": round(acct_eval),
            "asset_total": round(acct_asset_total),
            "cost_total": round(acct_cost),
            "principal": round(principal),
            "pnl_krw": round(acct_pnl_krw),
            "principal_pnl_krw": round(acct_asset_total - principal) if principal else None,
            "today_pnl_krw": round(acct_today_chg) if acct_today_available else None,
            "today_pnl_pct": round(acct_today_chg / acct_asset_total * 100, 2) if acct_today_available and acct_asset_total else None,
            "today_pnl_source": "live_quote_day_pct" if acct_today_available else "unavailable",
            "display_source": "live_settings_plus_pending_trades",
            "pending_trade_count": int(account_pending_meta.get(acct_name, {}).get("pending_trade_count", 0)),
            "pending_trades": account_pending_meta.get(acct_name, {}).get("pending_trades", []),
            "pnl_pct": round((acct_eval - acct_cost) / acct_cost * 100, 2) if acct_cost else 0,
        })
        total_eval += acct_eval + cash_krw
        total_cost += acct_cost + cash_krw

    raw_pnl = (total_eval - total_cost) / total_cost * 100 if total_cost else 0
    principal_pnl = (total_eval - TOTAL_PRINCIPAL_KRW) / TOTAL_PRINCIPAL_KRW * 100 if TOTAL_PRINCIPAL_KRW else 0

    # 비중 계산 (전체 평가금 대비)
    total_cash = sum(a["cash"] for a in result_accounts)
    grand_total = total_eval  # 이미 cash 포함
    total_today_available = any(a.get("today_pnl_krw") is not None for a in result_accounts)
    total_today_chg = sum(float(a.get("today_pnl_krw") or 0) for a in result_accounts)
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
        "total_asset": round(_safe(total_eval)),
        "total_holdings_eval": round(_safe(total_eval - total_cash)),
        "total_pnl_pct": round(_safe(raw_pnl), 2),
        "today_pnl_krw": round(total_today_chg) if total_today_available else None,
        "today_pnl_pct": round(total_today_chg / grand_total * 100, 2) if total_today_available and grand_total else None,
        "today_pnl_source": "live_quote_day_pct" if total_today_available else "unavailable",
        "total_cash": round(total_cash),
        "display_source": "live_settings_plus_pending_trades",
        "holdings_as_of": HOLDINGS_AS_OF,
        "pending_trade_count": total_pending_trades,
        "pending_warnings": pending_warnings,
        "fx_source": fx_source,
        "total_principal": round(float(TOTAL_PRINCIPAL_KRW)),
        "total_principal_pnl_pct": round(_safe(principal_pnl), 2),
        "cash_weight": cash_weight,
        "allocation": allocation,
        "usdkrw": round(_safe(usdkrw), 2),
        "now": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }


def portfolio_data() -> dict:
    """포트폴리오 데이터 (60초 캐시)."""
    return _cached("portfolio", 60, _fetch_portfolio_raw)


def _fetch_portfolio_cluster_risk_raw() -> dict:
    """삼성 수동 포트폴리오의 결정론 군집 위험 (read-only).

    dashboard GET 요청에서 가격 이력 네트워크 호출을 하지 않는다. 따라서 기본
    응답의 correlation_status는 not_requested이며, 상관행렬은 별도 주기 작업이
    core.portfolio_cluster_risk 계산 함수에 명시적으로 주입한다.
    """
    from core.portfolio_cluster_risk import (
        calculate_portfolio_cluster_risk,
        hermes_interpretation_payload,
    )

    report = calculate_portfolio_cluster_risk(portfolio_data())
    report["generated_at"] = datetime.now(KST).isoformat()
    report["source"] = "dashboard_portfolio_read_only"
    report["scope"] = "samsung_manual_portfolio_only"
    report["interpretation_payload"] = hermes_interpretation_payload(report)
    return report


def portfolio_cluster_risk_data() -> dict:
    """포트폴리오 군집 위험 (5분 캐시, GET/read-only)."""
    value = _cached("portfolio_cluster_risk", 300, _fetch_portfolio_cluster_risk_raw)
    return value if isinstance(value, dict) else {}


# ─── /api/trade-outcome-attribution ─────────────────────
def _mark_hermes_verified_live_events(
    live_events: list[dict], verification_rows: list[dict]
) -> list[dict]:
    """Mark only exact verification_id + decision_ref PASS joins as direct."""
    by_verification_id = {
        str(row.get("verification_id") or ""): row
        for row in verification_rows
        if row.get("verification_id")
    }
    for event in live_events:
        event_ref = str(event.get("decision_ref") or "")
        verification = by_verification_id.get(
            str(event.get("verification_id") or "")
        ) or {}
        same_symbol = (
            str(event.get("symbol") or "").upper().strip()
            == str(verification.get("symbol") or "").upper().strip()
        )
        same_side = (
            str(event.get("side") or "").lower().strip()
            == str(verification.get("side") or "").lower().strip()
        )
        event["hermes_decision_verified"] = bool(
            event_ref
            and str(verification.get("decision_ref") or "") == event_ref
            and str(verification.get("status") or "") == "PASS"
            and same_symbol
            and same_side
        )
    return live_events


def _attach_direct_refs_to_broker_orders(
    broker_orders: list[dict], live_events: list[dict]
) -> list[dict]:
    """Join broker GET truth only by exact clientOrderId == pilot_id."""
    by_pilot_id: dict[str, dict] = {}
    for event in live_events:
        pilot_id = str(event.get("pilot_id") or "")
        if pilot_id and event.get("hermes_decision_verified"):
            by_pilot_id.setdefault(pilot_id, event)
    for order in broker_orders:
        client_order_id = str(order.get("client_order_id") or "")
        event = by_pilot_id.get(client_order_id) or {}
        same_symbol = (
            str(order.get("symbol") or "").upper().split(".", 1)[0]
            == str(event.get("symbol") or "").upper().split(".", 1)[0]
        )
        same_side = (
            str(order.get("side") or "").lower().strip()
            == str(event.get("side") or "").lower().strip()
        )
        if event and same_symbol and same_side:
            order["decision_ref"] = str(event.get("decision_ref") or "")
            order["verification_id"] = str(event.get("verification_id") or "")
            order["hermes_decision_verified"] = True
        else:
            order["decision_ref"] = ""
            order["verification_id"] = ""
            order["hermes_decision_verified"] = False
    return broker_orders


def _read_trade_outcome_inputs(
    days: int,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """추천·수동 매매·production live event·broker GET을 read-only로 읽는다."""
    cutoff = (datetime.now(KST) - timedelta(days=days)).isoformat()
    predictions: list[dict] = []
    manual_trades: list[dict] = []
    live_events: list[dict] = []
    broker_orders: list[dict] = []

    conn = _conn()
    if conn is not None:
        predictions = _rows(
            conn,
            """SELECT id, created_at, closed_at, ticker, name, signal,
                      original_signal, action_type, action_grade, account_type,
                      briefing_type, entry_price, closed_price, pnl_pct, outcome,
                      status, persona, strategy_type, strategy_tags,
                      agreement_count, confidence, benchmark_ticker, data_quality,
                      normalizer_version
               FROM predictions
               WHERE created_at >= ?
               ORDER BY created_at DESC LIMIT 2000""",
            (cutoff,),
        )
        manual_trades = _rows(
            conn,
            """SELECT id, created_at, ticker, name, side, shares, price,
                      account, applied
               FROM trades WHERE created_at >= ?
               ORDER BY created_at DESC LIMIT 1000""",
            (cutoff,),
        )
        conn.close()

    event_path = _db_path().parent / "toss_live_pilot_events.db"
    if event_path.exists():
        event_conn = None
        try:
            event_conn = sqlite3.connect(f"file:{event_path}?mode=ro", uri=True)
            event_conn.row_factory = sqlite3.Row
            event_columns = {
                str(row[1]) for row in event_conn.execute(
                    "PRAGMA table_info(live_pilot_events)"
                ).fetchall()
            }
            decision_expr = "decision_ref" if "decision_ref" in event_columns else "'' AS decision_ref"
            verification_expr = (
                "verification_id" if "verification_id" in event_columns else "'' AS verification_id"
            )
            live_events = _rows(
                event_conn,
                f"""SELECT event_id, pilot_id, event_type, {verification_expr}, {decision_expr},
                          symbol, side, filled_price, filled_quantity, broker_order_status,
                          live_order_sent, adapter_status,
                          live_order_allowed, created_at
                   FROM live_pilot_events
                   WHERE created_at >= ? AND event_type IN ('live_sent', 'autonomous_live_sent')
                   ORDER BY created_at DESC LIMIT 1000""",
                (cutoff,),
            )
        except Exception:
            live_events = []
        finally:
            if event_conn is not None:
                event_conn.close()

    # A Hermes link is direct only when both immutable keys agree: the event's
    # verification_id resolves to PASS and its decision_ref equals the stored
    # verification decision_ref. No symbol/time proximity inference is allowed.
    verification_path = _db_path().parent / "toss_live_pilot_verification.db"
    if live_events and verification_path.exists():
        verification_conn = None
        try:
            verification_conn = sqlite3.connect(
                f"file:{verification_path}?mode=ro", uri=True
            )
            verification_conn.row_factory = sqlite3.Row
            verification_columns = {
                str(row[1]) for row in verification_conn.execute(
                    "PRAGMA table_info(live_pilot_verification)"
                ).fetchall()
            }
            if "decision_ref" in verification_columns:
                verification_rows = _rows(
                    verification_conn,
                    """SELECT verification_id, decision_ref, status, symbol, side
                       FROM live_pilot_verification
                       ORDER BY requested_at DESC LIMIT 2000""",
                )
                _mark_hermes_verified_live_events(live_events, verification_rows)
        except Exception:
            pass
        finally:
            if verification_conn is not None:
                verification_conn.close()

    try:
        from core.toss_readonly_snapshot import broker_orders_for_consumer
        broker_snapshot = broker_orders_for_consumer()
        broker_orders = [
            dict(row) for row in broker_snapshot.get("orders") or []
            if isinstance(row, dict)
        ]
        _attach_direct_refs_to_broker_orders(broker_orders, live_events)
    except Exception:
        broker_orders = []
    return predictions, manual_trades, live_events, broker_orders


def _fetch_trade_outcome_attribution_raw(days: int) -> dict:
    from core.trade_outcome_attribution import (
        calculate_trade_outcome_attribution,
        hermes_interpretation_payload,
        normalize_execution_records,
    )

    predictions, manual_trades, live_events, broker_orders = _read_trade_outcome_inputs(days)
    executions = normalize_execution_records(
        manual_trades=manual_trades,
        live_events=live_events,
        broker_orders=broker_orders,
    )
    report = calculate_trade_outcome_attribution(
        predictions,
        executions=executions,
    )
    report["generated_at"] = datetime.now(KST).isoformat()
    report["source"] = "memory_db_and_local_execution_logs_read_only"
    report["scope"] = f"recent_{days}_days"
    window_end = datetime.now(KST)
    report["window"] = {
        "mode": "rolling_days",
        "days": days,
        "as_of": window_end.isoformat(),
        "cutoff": (window_end - timedelta(days=days)).isoformat(),
        "rule": "dashboard SQL: prediction created_at; execution created_at",
        "filter_applied": True,
    }
    report["benchmark_attribution"]["status"] = "not_requested"
    report["interpretation_payload"] = hermes_interpretation_payload(report)
    return report


def trade_outcome_attribution_data(days: int = 90) -> dict:
    """추천·체결 사후 귀속 보고서 (5분 캐시, GET/read-only)."""
    days = max(1, min(int(days or 90), 365))
    value = _cached(
        f"trade_outcome_attribution:{days}", 300,
        lambda: _fetch_trade_outcome_attribution_raw(days),
    )
    return value if isinstance(value, dict) else {}


# ─── /api/toss/execution-red-team — staging 조회 전용 ─────
def execution_red_team_staging_data(limit: int = 50, symbol: str | None = None) -> dict:
    """실행 후보 Red Team staging을 읽기만 한다.

    이 GET 경로는 Claude/WebSearch/주문 preview/ledger/finalizer/transport를 호출하지
    않는다. CLI가 별도로 생성한 advisory JSON만 검증해 최신순으로 반환한다.
    """
    import json
    from pathlib import Path

    from core.execution_candidate_red_team import VERSION, validate_staging_record

    capped_limit = min(max(int(limit or 50), 1), 200)
    wanted = str(symbol or "").upper().strip()
    configured = os.environ.get("EXECUTION_RED_TEAM_STAGING_DIR", "").strip()
    root = Path(configured).expanduser() if configured else _db_path().parent / "execution-red-team-staging"

    items: list[dict] = []
    invalid_count = 0
    if root.exists() and root.is_dir():
        try:
            paths = sorted(
                root.glob("*/*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:500]
        except OSError:
            paths = []
        for path in paths:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                invalid_count += 1
                continue
            if not isinstance(record, dict) or validate_staging_record(record):
                invalid_count += 1
                continue
            if wanted and str(record.get("symbol") or "").upper().strip() != wanted:
                continue
            items.append(record)
            if len(items) >= capped_limit:
                break

    return {
        "version": VERSION,
        "read_only": True,
        "review_only": True,
        "operational_decision_unchanged": True,
        "advisory_only": True,
        "order_side_effects": False,
        "order_signal": False,
        "source": "execution_red_team_staging_json",
        "count": len(items),
        "invalid_count": invalid_count,
        "items": items,
    }


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


# ─── /api/macro — FRED 매크로 + Fear&Greed (홈 카드용) ──────
def macro_data() -> dict:
    """미국 매크로(FRED) + CNN Fear&Greed 스냅샷 (30분 캐시 — 원천은 자체 파일캐시)."""
    def _fetch() -> dict:
        from core.fear_greed import fetch_fear_greed
        from core.macro_fred import fetch_macro_snapshot
        return {"fred": fetch_macro_snapshot(), "fear_greed": fetch_fear_greed()}
    return _cached("macro", 1800, _fetch)


# ─── /api/short-selling — 보유 KR 종목 공매도 거래비중 ──────
def short_selling_data() -> dict:
    """KIS 공매도 일별추이 (30분 캐시 — 종목당 1 API콜이라 짧은 TTL 금지)."""
    def _fetch() -> dict:
        from core.kr_market import fetch_short_selling
        return {"items": fetch_short_selling()}
    return _cached("short_selling", 1800, _fetch)


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

    # 국내 종목 호가 리스크 (최대 10종목, 캐시 재사용)
    ob_risks: dict[str, dict] = {}
    kr_tickers = [t for t in tickers_in_rows if t.endswith(".KS") or t.endswith(".KQ")][:10]
    for tk in kr_tickers:
        try:
            ob = ticker_orderbook(tk)
            ob_risks[tk] = summarize_execution_risk(ob)
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
            "execution_risk": ob_risks.get(ticker, {"has_warning": False, "label": "", "summary": "", "tone": "unknown"}),
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
    from core.market_hours import market_reliability_context
    mkt_rel = market_reliability_context()
    blocks = {
        # 무슨 일: 브리핑 종류 + 액션 수
        "what": {
            "briefing_type": briefing_type,
            "total": len(rows),
            "do_now": len(do_now),
            "conditional": len(conditionals),
        },
        "market_reliability": mkt_rel,
        "market_status_summary": mkt_rel["summary"],
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


# ─── 호가 리스크 요약 유�� (공통) ──────────────────────
def summarize_execution_risk(orderbook: dict | None) -> dict:
    """orderbook 결과에서 warning 수준만 요약. 정상이면 has_warning=False."""
    base = {"has_warning": False, "label": "데이터 대기",
            "summary": "", "spread_pct": 0, "imbalance_pct": 0, "tone": "unknown"}
    if not orderbook or not isinstance(orderbook, dict):
        return base
    if orderbook.get("error") or orderbook.get("source") == "unsupported":
        base["label"] = orderbook.get("error") or "국내 종목만 지원"
        return base

    risk = orderbook.get("execution_risk_label", "")
    spread = orderbook.get("spread_pct", 0) or 0
    imbalance = orderbook.get("imbalance_pct", 0) or 0

    base["spread_pct"] = spread
    base["imbalance_pct"] = imbalance

    if risk == "체결 리스크 낮음":
        base["has_warning"] = False
        base["label"] = "체결 리스크 낮음"
        base["tone"] = "ok"
        base["summary"] = f"스프레드 {spread:.2f}%"
    elif risk == "스프레드 주의":
        base["has_warning"] = True
        base["label"] = "스프레드 주의"
        base["tone"] = "warn"
        base["summary"] = f"스프레드 {spread:.2f}% · 호가 기준 판단 보조"
    elif risk == "유동성 주의":
        base["has_warning"] = True
        base["label"] = "유동성 주의"
        base["tone"] = "bad"
        base["summary"] = f"스프레드 {spread:.2f}% · 유동성 주의 · 호가 기준 판단 보조"
    else:
        base["label"] = risk or "데이터 대기"
        base["tone"] = "unknown"

    # imbalance 보조
    if abs(imbalance) > 60:
        base["has_warning"] = True
        base["summary"] += f" · 불균형 {imbalance:+.0f}%"
        if base["tone"] == "ok":
            base["tone"] = "warn"

    return base


# ─── /api/ticker/{ticker}/orderbook — 호가/체결 리스크 ──────
def ticker_orderbook(ticker: str) -> dict:
    """국내 종목 호가 조회 (30초 캐시). 해외는 미지원."""
    now_str = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S")
    base = {
        "ticker": ticker, "source": "unsupported", "updated_at": now_str,
        "cache_age_sec": 0, "bids": [], "asks": [],
        "spread": 0, "spread_pct": 0, "mid_price": 0,
        "total_bid_size": 0, "total_ask_size": 0, "imbalance_pct": 0,
        "liquidity_label": "데이터 대기",
        "execution_risk_label": "데이터 대기", "error": "",
    }
    if not _TICKER_SAFE.match(ticker):
        base["error"] = "invalid ticker"
        return base
    is_kr = ticker.endswith(".KS") or ticker.endswith(".KQ")
    if not is_kr:
        base["error"] = "국내 종목만 지원"
        return base

    cache_key = f"orderbook:{ticker}"

    def _fetch():
        try:
            from core.market_kis import get_domestic_orderbook
            return get_domestic_orderbook(ticker)
        except Exception:
            return None

    result = _cached(cache_key, 30, _fetch)
    if not result or not isinstance(result, dict):
        base["error"] = "호가 데이터 없음"
        return base

    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry:
            base["cache_age_sec"] = round(time.monotonic() - entry[0])

    base.update(result)
    base["updated_at"] = now_str
    return base


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
    """OHLCV 차트 조회. 국내 종목은 KIS 우선, 실패 시 yfinance fallback.

    현재가/일간등락률은 기존 시세 체인(KIS→yfinance)에서 가져와
    전일종가 대비 정확한 day_pct를 제공한다.
    """
    # 국내 종목: KIS 차트 우선 시도
    is_kr = ticker.endswith(".KS") or ticker.endswith(".KQ")
    if is_kr:
        try:
            from core.market_kis import get_domestic_chart
            kis_data = get_domestic_chart(ticker, period, interval)
            if kis_data and kis_data.get("points"):
                return kis_data
        except Exception as e:
            log.debug("KIS chart fallback for %s: %s", ticker, e)

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


# ─── /api/toss/account-summary (읽기 전용, 기존 포트폴리오 미합산) ──
def _toss_pnl_scope_metadata() -> dict:
    """현재 Toss 보유 응답으로 계산 가능한 손익 범위를 명시한다."""
    return {
        "profit_loss": "open_positions_unrealized_after_cost",
        "today_profit_loss": "open_positions_daily_change_excludes_closed_realized",
        "realized_profit_loss": "unavailable",
        "true_daily_account_pnl_available": False,
        "warning": "오늘 손익은 현재 보유만 합산하며 매도 종목의 실현손익은 포함하지 않음",
    }


def _fetch_toss_account_summary_raw(*, bound_dashboard_timeout: bool = True) -> dict:
    """Toss 실전 AI 자동거래 계좌 요약. 기존 포트폴리오에 절대 합산하지 않음."""
    if _dashboard_toss_broker_reads_isolated():
        try:
            from core.toss_readonly_snapshot import account_summary_for_consumer
            snapshot = account_summary_for_consumer()
        except Exception:
            snapshot = None
        if isinstance(snapshot, dict) and snapshot:
            snapshot["read_only_notice"] = (
                "OAuth 충돌 방지: stock-bot이 생성한 sanitized snapshot을 표시"
            )
            return snapshot
        data = _toss_account_summary_unavailable("stock_bot_snapshot_unavailable")
        data["read_only_notice"] = (
            "OAuth 충돌 방지: 이 프로세스는 Toss 계좌 API를 직접 조회하지 않음"
        )
        return data

    from core import toss_client as tc
    if bound_dashboard_timeout:
        _set_toss_readonly_timeout()

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    live_policy = _toss_live_policy_fast(timeout=0.2)
    pnl_scope = _toss_pnl_scope_metadata()
    effective_live = bool(
        live_policy.get("autonomous_mode")
        and not live_policy.get("autonomous_kill_switch")
        and live_policy.get("all_live_gates_open")
        and live_policy.get("live_transport_status") == "configured"
    )

    base = {
        "enabled": tc.is_configured(),
        "label": "Toss 실전 AI 자동거래 계좌",
        "separate_from_portfolio": True,
        "included_in_total_portfolio": False,
        "trading_enabled": effective_live,
        "automation_status": "autonomous_live_pilot" if effective_live else "disabled",
        "live_policy": live_policy,
        "account_count": 0,
        "accounts": [],
        "holdings_count": 0,
        "holdings_items": [],
        "market_value": {"krw": 0, "usd": None},
        "cash": {"krw": 0, "usd": None, "source": "Toss"},
        "total_account_value": {"krw": 0, "usd": None},
        "profit_loss": {"krw": None, "source": "unavailable"},
        "today_profit_loss": {"krw": None, "source": "unavailable"},
        "realized_profit_loss": {
            "krw": None,
            "source": "not_available_from_current_readonly_summary",
        },
        "pnl_scope": pnl_scope,
        "exchange_rate": None,
        "warnings": [
            "기존 삼성증권/수동 포트폴리오에 합산하지 않음",
            "실전 계좌 · 별도 성과 추적",
            "자율 live pilot 활성" if effective_live else "실주문 기능 없음",
            "Hermes PASS 후 자동주문" if effective_live else "자동거래 비활성",
            pnl_scope["warning"],
        ],
        "updated_at": now_str,
        "error": "",
    }

    if not tc.is_configured():
        base["error"] = "Toss API 미설정"
        return base

    # 계좌 목록. Dashboard 프로세스에서 Toss가 간헐적으로 401을 돌려주면
    # 메모리 토큰을 비우고 1회 재시도한다. 실패값 0원을 캐시해 화면을
    # 오도하지 않기 위한 read-only 복구 루트다.
    accounts = tc.get_accounts()
    if not accounts:
        try:
            tc._mem_token = ""
            tc._mem_expires = 0.0
        except Exception:
            pass
        accounts = tc.get_accounts()
    if not accounts:
        base["error"] = "Toss account unavailable"
        base["data_quality"] = {
            "account_summary": "unavailable",
            "reason": "accounts_empty_after_retry",
            "cooldown_eligible": True,
        }
        base["warnings"].append(
            "Toss 계좌 조회 실패 — 마지막 정상값 또는 쿨다운 응답 사용"
        )
        return base
    base["account_count"] = len(accounts)
    base["accounts"] = [
        {
            "account_seq": a.get("accountSeq"),
            "account_type": a.get("accountType", ""),
            "account_no_masked": "[REDACTED]",
        }
        for a in accounts
    ]

    mv_krw = 0.0
    mv_usd = None
    cash_krw = 0.0
    cash_usd = None

    if accounts:
        seq = str(accounts[0].get("accountSeq", ""))

        # 보유종목
        holdings = tc.get_holdings(seq)
        items = holdings.get("items", [])
        base["holdings_count"] = len(items)
        base["holdings_items"] = tc.sanitize_dict(items)

        mv = holdings.get("marketValue", {})
        mv_amt = mv.get("amount", {}) if isinstance(mv, dict) else {}
        krw_val = mv_amt.get("krw", "0") if isinstance(mv_amt, dict) else "0"
        usd_val = mv_amt.get("usd") if isinstance(mv_amt, dict) else None
        try:
            mv_krw = float(krw_val) if krw_val else 0
        except (ValueError, TypeError):
            pass
        mv_usd = float(usd_val) if usd_val else None

        # 현금/예수금 (KRW)
        bp_krw = tc.get_buying_power(seq, "KRW")
        if bp_krw:
            try:
                if str(bp_krw.get("currency", "KRW")).upper() == "KRW":
                    cash_krw = float(bp_krw.get("cashBuyingPower", "0"))
            except (ValueError, TypeError):
                pass

        # 현금/예수금 (USD)
        bp_usd = tc.get_buying_power(seq, "USD")
        if bp_usd:
            try:
                if str(bp_usd.get("currency", "USD")).upper() == "USD":
                    v = bp_usd.get("cashBuyingPower", "0")
                    cash_usd = float(v) if v and float(v) > 0 else None
            except (ValueError, TypeError):
                pass

    # 환율
    fx = tc.get_exchange_rate("USD", "KRW")
    if not fx:
        try:
            tc._mem_token = ""
            tc._mem_expires = 0.0
        except Exception:
            pass
        fx = tc.get_exchange_rate("USD", "KRW")
    fx_rate = 0.0
    if fx:
        try:
            fx_rate = float(fx.get("rate", 0) or 0)
            base["exchange_rate"] = {
                "base": fx.get("baseCurrency", "USD"),
                "quote": fx.get("quoteCurrency", "KRW"),
                "rate": fx_rate,
                "source": "Toss",
            }
        except (ValueError, TypeError):
            fx_rate = 0.0

    mv_usd_krw = (mv_usd or 0) * fx_rate if fx_rate else 0.0
    cash_usd_krw = (cash_usd or 0) * fx_rate if fx_rate else 0.0
    # Toss 응답은 KRW와 USD buying power/market value를 분리해서 준다.
    # HTML 총자산/손익 표시는 원화 환산 총액이어야 하므로 USD 현금·미국주 평가를 합산한다.
    market_value_krw_total = mv_krw + mv_usd_krw
    cash_krw_total = cash_krw + cash_usd_krw
    total_usd = ((mv_usd or 0) + (cash_usd or 0)) or None

    def _toss_num(v, default=0.0):
        try:
            if v is None or v == "":
                return default
            return float(v)
        except (TypeError, ValueError):
            return default

    def _toss_money_to_krw(amount, currency):
        val = _toss_num(amount)
        if str(currency or "KRW").upper() == "USD" and fx_rate:
            return val * fx_rate
        return val

    # Toss 앱의 수입/손익 표시는 보유종목별 profitLoss / dailyProfitLoss가
    # 원본에 들어있다. 기존 대시보드는 총자산만 합산하고 이 필드를 버려서
    # 앱에서는 수익인데 툴은 손익 없음/0처럼 보였다. read-only로 원본 손익을
    # 합산해 화면에 그대로 노출한다. 실현손익은 별도 체결/정산 API가 없으면
    # 추정하지 않고 None으로 둔다.
    unrealized_krw = 0.0
    unrealized_after_cost_krw = 0.0
    daily_krw = 0.0
    cost_basis_krw = 0.0
    daily_basis_krw = 0.0
    profitable_count = 0
    loss_count = 0
    for item in base.get("holdings_items", []) or []:
        cur = str(item.get("currency") or "KRW").upper()
        pl = item.get("profitLoss") or {}
        dpl = item.get("dailyProfitLoss") or {}
        mv_row = item.get("marketValue") or {}
        unrealized_krw += _toss_money_to_krw(pl.get("amount"), cur)
        unrealized_after_cost_krw += _toss_money_to_krw(pl.get("amountAfterCost", pl.get("amount")), cur)
        daily_krw += _toss_money_to_krw(dpl.get("amount"), cur)
        cost_basis_krw += _toss_money_to_krw(mv_row.get("purchaseAmount"), cur)
        daily_basis_krw += _toss_money_to_krw(mv_row.get("amount"), cur)
        amt = _toss_num(pl.get("amountAfterCost", pl.get("amount")))
        if amt > 0:
            profitable_count += 1
        elif amt < 0:
            loss_count += 1

    base["profit_loss"] = {
        "krw": unrealized_after_cost_krw,
        "before_cost_krw": unrealized_krw,
        "rate": (unrealized_after_cost_krw / cost_basis_krw) if cost_basis_krw else None,
        "source": "Toss holdings.profitLoss.amountAfterCost",
        "profitable_count": profitable_count,
        "loss_count": loss_count,
    }
    base["today_profit_loss"] = {
        "krw": daily_krw,
        "rate": (daily_krw / daily_basis_krw) if daily_basis_krw else None,
        "source": "Toss holdings.dailyProfitLoss.amount",
    }
    base["realized_profit_loss"] = {
        "krw": None,
        "source": "not_available_from_current_readonly_summary",
        "note": "매도 종목의 실현손익은 현재 read-only 보유 응답으로 계산할 수 없음",
    }
    base["pnl_scope"] = _toss_pnl_scope_metadata()
    if base["pnl_scope"]["warning"] not in base["warnings"]:
        base["warnings"].append(base["pnl_scope"]["warning"])

    base["market_value"] = {
        "krw": market_value_krw_total,
        "krw_native": mv_krw,
        "usd": mv_usd,
        "usd_krw": mv_usd_krw,
    }
    base["cash"] = {
        "krw": cash_krw_total,
        "krw_native": cash_krw,
        "usd": cash_usd,
        "usd_krw": cash_usd_krw,
        "source": "Toss",
    }
    base["total_account_value"] = {
        "krw": market_value_krw_total + cash_krw_total,
        "krw_native": mv_krw + cash_krw,
        "usd": total_usd,
        "usd_krw": mv_usd_krw + cash_usd_krw,
        "usd_included": bool(fx_rate and total_usd),
    }

    return base


def _toss_account_summary_is_live_good(data: dict) -> bool:
    """Return True when the summary came from a usable live Toss account read."""
    if not isinstance(data, dict):
        return False
    if data.get("error"):
        return False
    if not data.get("enabled"):
        return True  # configured-off is cheap/static, not a transient Toss outage
    try:
        return int(data.get("account_count") or 0) > 0
    except Exception:
        return False


def _toss_account_summary_unavailable(reason: str) -> dict:
    """Minimal read-only response used while Toss account calls are cooling down."""
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    pnl_scope = _toss_pnl_scope_metadata()
    return {
        "enabled": True,
        "label": "Toss 실전 AI 자동거래 계좌",
        "separate_from_portfolio": True,
        "included_in_total_portfolio": False,
        "trading_enabled": False,
        "automation_status": "unknown",
        "live_policy": {},
        "account_count": 0,
        "accounts": [],
        "holdings_count": 0,
        "holdings_items": [],
        "market_value": {"krw": 0, "usd": None},
        "cash": {"krw": 0, "usd": None, "source": "Toss"},
        "total_account_value": {"krw": 0, "usd": None},
        "profit_loss": {"krw": None, "source": "unavailable"},
        "today_profit_loss": {"krw": None, "source": "unavailable"},
        "realized_profit_loss": {
            "krw": None,
            "source": "not_available_from_current_readonly_summary",
        },
        "pnl_scope": pnl_scope,
        "exchange_rate": None,
        "warnings": [
            "기존 삼성증권/수동 포트폴리오에 합산하지 않음",
            "Toss API 일시 오류 — 계좌 조회 쿨다운 중",
            pnl_scope["warning"],
        ],
        "updated_at": now_str,
        "error": reason,
        "cache_status": "cooldown",
        "stale": False,
        "data_quality": {
            "account_summary": "unavailable",
            "reason": reason,
            "cooldown_eligible": True,
        },
    }


def _toss_account_summary_mark(data: dict, status: str, reason: str = "", source_ts: float | None = None) -> dict:
    """Copy a Toss account summary and annotate cache/freshness state."""
    out = copy.deepcopy(data) if isinstance(data, dict) else {}
    out["cache_status"] = status
    out["stale"] = status == "stale"
    if source_ts is not None:
        out["cache_age_sec"] = round(max(0.0, time.monotonic() - source_ts))
    if reason:
        out["stale_reason"] = reason
        out["error"] = reason if status != "live" else out.get("error", "")
        warnings = list(out.get("warnings") or [])
        msg = "Toss API 일시 오류 — 마지막 정상값 표시"
        if msg not in warnings:
            warnings.append(msg)
        out["warnings"] = warnings
        dq = dict(out.get("data_quality") or {})
        dq.update({"account_summary": status, "reason": reason})
        out["data_quality"] = dq
    return out


def toss_account_summary() -> dict:
    """Toss 실전 AI 자동거래 계좌 요약.

    Live Toss account calls can return repeated 401/429 and hammer the broker API.
    Keep the dashboard read-only and responsive by returning a fresh cache, then
    stale last-known-good data during a short cooldown, instead of caching a false
    zero-account summary.
    """
    global _toss_account_summary_last_good, _toss_account_summary_cooldown_until

    key = "toss_account_summary"
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry:
            ts, val = entry
            if now - ts < _TOSS_ACCOUNT_SUMMARY_OK_TTL:
                return copy.deepcopy(val)
        else:
            val = None
        last_good = _toss_account_summary_last_good
        cooldown_until = _toss_account_summary_cooldown_until

    if now < cooldown_until:
        if last_good and now - last_good[0] <= _TOSS_ACCOUNT_SUMMARY_STALE_TTL:
            stale = _toss_account_summary_mark(
                last_good[1], "stale", "toss_api_cooldown", last_good[0]
            )
            with _cache_lock:
                _cache[key] = (now, stale)
            return stale
        if isinstance(val, dict):
            cooldown_val = _toss_account_summary_mark(val, "cooldown", "toss_api_cooldown")
        else:
            cooldown_val = _toss_account_summary_unavailable("toss_api_cooldown")
        with _cache_lock:
            _cache[key] = (now, cooldown_val)
        return cooldown_val

    data = _fetch_toss_account_summary_raw()
    if _toss_account_summary_is_live_good(data):
        live = _toss_account_summary_mark(data, "live")
        with _cache_lock:
            if live.get("enabled") and int(live.get("account_count") or 0) > 0:
                _toss_account_summary_last_good = (now, live)
            _toss_account_summary_cooldown_until = 0.0
            _cache[key] = (now, live)
        return live

    reason = str((data or {}).get("error") or "toss_account_unavailable")
    with _cache_lock:
        _toss_account_summary_cooldown_until = now + _TOSS_ACCOUNT_FAILURE_COOLDOWN_SEC
        last_good = _toss_account_summary_last_good
    if last_good and now - last_good[0] <= _TOSS_ACCOUNT_SUMMARY_STALE_TTL:
        stale = _toss_account_summary_mark(last_good[1], "stale", reason, last_good[0])
        with _cache_lock:
            _cache[key] = (now, stale)
        return stale

    unavailable = _toss_account_summary_mark(data or _toss_account_summary_unavailable(reason), "cooldown", reason)
    with _cache_lock:
        _cache[key] = (now, unavailable)
    return unavailable


def _fetch_toss_automation_status_raw() -> dict:
    """Toss 자동거래 상태 + 가드레일 목록."""
    from config import toss_automation as cfg
    from core.toss_paper_trading import today_paper_stats

    stats = today_paper_stats()
    guards = []
    if cfg.TOSS_KILL_SWITCH:
        guards.append({"name": "킬스위치", "status": "ON", "ok": False})
    else:
        guards.append({"name": "킬스위치", "status": "OFF", "ok": True})
    guards.append({"name": "실주문 허용", "status": str(cfg.TOSS_ALLOW_LIVE_ORDERS), "ok": False})
    guards.append({"name": "Telegram 승인", "status": "필수" if cfg.TOSS_REQUIRE_TELEGRAM_APPROVAL else "불필요", "ok": True})
    guards.append({"name": "1회 한도", "status": f"₩{cfg.TOSS_MAX_ORDER_KRW:,}", "ok": True})
    guards.append({"name": "일일 한도", "status": f"₩{cfg.TOSS_MAX_DAILY_ORDER_KRW:,}", "ok": True})
    guards.append({"name": "현금 하한", "status": f"₩{cfg.TOSS_MIN_CASH_BUFFER_KRW:,}", "ok": True})
    guards.append({"name": "최대 포지션", "status": str(cfg.TOSS_MAX_POSITIONS), "ok": True})
    guards.append({"name": "블랙리스트", "status": ", ".join(cfg.TOSS_SYMBOL_BLACKLIST) or "없음", "ok": True})

    # Legacy paper-trading config can disagree with the live-pilot/autonomous
    # env gates used by the real Toss order path. Surface the effective live
    # status here so the dashboard does not show a false kill-switch while
    # /api/toss/live-pilot-policy is armed.
    try:
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        live_policy = compute_toss_live_pilot_policy()
    except Exception:
        live_policy = {}

    effective_live = bool(
        live_policy.get("autonomous_mode")
        and not live_policy.get("autonomous_kill_switch")
        and live_policy.get("all_live_gates_open")
        and live_policy.get("live_transport_status") == "configured"
    )
    if effective_live:
        guards = [g for g in guards if g.get("name") not in ("킬스위치", "실주문 허용", "Telegram 승인")]
        guards.insert(0, {"name": "자율 live pilot", "status": "ON", "ok": True})
        guards.insert(1, {"name": "실주문 게이트", "status": "OPEN", "ok": True})
        guards.insert(2, {"name": "Telegram 승인", "status": "불필요", "ok": True})

    return {
        "automation_enabled": effective_live or cfg.TOSS_AUTOMATION_ENABLED,
        "mode": "autonomous_live_pilot" if effective_live else cfg.TOSS_AUTOMATION_MODE,
        "dry_run": False if effective_live else cfg.TOSS_DRY_RUN,
        "live_orders_allowed": effective_live or cfg.TOSS_ALLOW_LIVE_ORDERS,
        "kill_switch": False if effective_live else cfg.TOSS_KILL_SWITCH,
        "telegram_approval_required": False if effective_live else cfg.TOSS_REQUIRE_TELEGRAM_APPROVAL,
        "paper_trades_count_today": stats.get("count", 0),
        "daily_budget_used_krw": stats.get("daily_amount_krw", 0),
        "daily_budget_max_krw": cfg.TOSS_MAX_DAILY_ORDER_KRW,
        "live_policy": live_policy,
        "guards": guards,
    }


def toss_automation_status() -> dict:
    """Toss 자동거래 상태 (30초 캐시)."""
    return _cached("toss_automation_status", 30, _fetch_toss_automation_status_raw)


def _to_float(value, default: float | None = None) -> float | None:
    """Best-effort numeric conversion for read-only cross checks."""
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def _kr_symbol_candidates(symbol: str) -> list[str]:
    """Return safe KIS lookup variants for Toss/KR symbols."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return []
    if sym.endswith((".KS", ".KQ")):
        return [sym]
    if sym.isdigit() and len(sym) == 6:
        return [f"{sym}.KS", f"{sym}.KQ", sym]
    return [sym]


def _toss_holding_price_map() -> dict[str, dict]:
    """Toss holdings keyed by raw and suffix-normalized symbols. Read-only."""
    out: dict[str, dict] = {}
    try:
        acct = toss_account_summary() or {}
        for h in acct.get("holdings_items") or []:
            raw = str(h.get("symbol") or "").upper().strip()
            if not raw:
                continue
            row = {
                "symbol": raw,
                "name": h.get("name") or raw,
                "last_price": _to_float(h.get("lastPrice")),
                "quantity": _to_float(h.get("quantity")),
                "currency": h.get("currency") or "",
                "market_country": h.get("marketCountry") or "",
            }
            out[raw] = row
            if raw.isdigit() and len(raw) == 6:
                out[f"{raw}.KS"] = row
                out[f"{raw}.KQ"] = row
    except Exception as e:
        log.debug("Toss holding map unavailable: %s", e)
    return out


def _recent_toss_risk_sell_symbols(limit: int = 100) -> dict[str, dict]:
    """최근 리스크 기반 SELL 심볼 맵. 신규 매수 재진입 cooldown용. Read-only."""
    out: dict[str, dict] = {}
    try:
        from core.toss_live_pilot_ledger import list_live_pilot_records
        records = list_live_pilot_records(limit=limit)
    except Exception as e:
        log.debug("recent risk sell lookup unavailable: %s", e)
        records = []
    risk_reasons = {"position_review_sell", "auto_exit_sell", "income_rebalance_sell_to_fund"}
    for r in records:
        side = str(r.get("side") or "").lower()
        symbol = str(r.get("symbol") or "").upper().strip()
        reason = str(r.get("reason") or "")
        if side != "sell" or not symbol:
            continue
        if reason not in risk_reasons:
            continue
        out[symbol] = {
            "reason": reason,
            "created_at": r.get("created_at") or r.get("sent_at") or "",
            "status": r.get("status") or "",
        }
        if symbol.endswith((".KS", ".KQ")):
            out[symbol.split(".", 1)[0]] = out[symbol]
    return out


_PREVIEW_TTL_MINUTES = 60   # verification 없는 preview의 차단 유효시간


def _pending_toss_order_symbols(limit: int = 150) -> dict[str, dict] | None:
    """신규 BUY 차단용 same-symbol pending 주문 맵. Read-only.

    stale 이력(만료 verification·종결된 broker 주문)이 신규 후보를 영구
    차단하지 않도록, 심볼별 최신 행만 보고 상태별로 살아있는 것만 차단한다.

    - preview류(previewed/reviewed/payload_validated/confirmed_but_not_sent):
      연결된 최신 verification이 fresh PENDING 또는 미만료 PASS일 때만 차단.
      expired PASS/EXPIRED/HOLD/BLOCK/ERROR는 제외. verification이 없으면
      preview 생성 후 _PREVIEW_TTL_MINUTES 이내에만 차단.
    - live_sent(및 live_send_retryable): stock-bot sanitized snapshot의
      broker OPEN 주문과 symbol+side가 일치할 때만 차단. FILLED/CANCELLED/
      REJECTED거나 OPEN 매칭이 없으면 제외. broker truth(fresh snapshot)
      unavailable이면 fail-closed(차단 유지).
    - Toss OAuth/API 직접 호출 없음(스냅샷만 소비). ledger/verification DB
      행은 읽기만 하며 삭제·수정하지 않는다.
    - 조회 실패 시 None 반환 — '모름'을 빈 맵('없음')으로 위장하지 않는다.
    """
    try:
        from core.toss_live_pilot_ledger import list_live_pilot_records
        records = list_live_pilot_records(limit=limit)   # created_at DESC
    except Exception as e:
        log.warning("pending order lookup unavailable (fail-closed): %s", e)
        return None

    # broker truth: fresh snapshot의 broker_orders만 신뢰
    broker_open: set | None = None
    try:
        from core.toss_readonly_snapshot import load_snapshot
        snap = load_snapshot()
        if (
            snap.get("ok") is True
            and snap.get("status") == "fresh"
            and snap.get("usable_for_decisions") is True
        ):
            terminal_broker = {
                "FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED",
            }
            broker_open = {
                (str(o.get("symbol") or "").upper().strip(),
                 str(o.get("side") or "").lower().strip())
                for o in (snap.get("broker_orders") or [])
                if str(o.get("broker_order_status") or "").upper().strip()
                not in terminal_broker
            }
    except Exception as e:
        log.debug("broker snapshot unavailable for pending check: %s", e)
        broker_open = None   # unavailable → live_sent fail-closed

    def _parse_ts(text) -> datetime | None:
        try:
            dt = datetime.fromisoformat(str(text))
            return dt if dt.tzinfo else dt.replace(tzinfo=KST)
        except (TypeError, ValueError):
            return None

    now = datetime.now(KST)
    preview_statuses = {
        "previewed", "reviewed", "payload_validated", "confirmed_but_not_sent",
    }
    sent_statuses = {"live_sent", "live_send_retryable"}

    def _verification_blocks(pilot_id: str) -> bool | None:
        """True=차단, False=제외, None=verification 없음."""
        try:
            from core.toss_live_pilot_verification import get_verification_for_pilot
            v = get_verification_for_pilot(pilot_id)
        except Exception as e:
            log.debug("verification lookup failed (fail-closed): %s", e)
            return True   # 조회 실패는 '모름' — 중복 방지 우선
        if not isinstance(v, dict):
            return None
        status = str(v.get("status") or "").upper().strip()
        if status not in ("PENDING", "PASS"):
            return False   # EXPIRED/HOLD/BLOCK/ERROR — 죽은 이력은 차단 안 함
        expires = _parse_ts(v.get("expires_at"))
        if expires is None:
            return True    # 만료시각 불명 — fail-closed
        return expires > now   # fresh PENDING / 미만료 PASS만 차단

    out: dict[str, dict] = {}
    seen: set[str] = set()
    for r in records:
        side = str(r.get("side") or "buy").lower()
        status = str(r.get("status") or "").lower()
        symbol = str(r.get("symbol") or "").upper().strip()
        if side != "buy" or not symbol:
            continue
        if symbol in seen:
            continue   # DESC 조회 — 최신 행이 그 심볼의 진실 (newest wins)
        seen.add(symbol)

        blocks = False
        if status in preview_statuses:
            verdict = _verification_blocks(str(r.get("pilot_id") or ""))
            if verdict is None:
                created = _parse_ts(r.get("created_at") or r.get("sent_at"))
                blocks = (
                    created is None   # 시각 불명 — fail-closed
                    or (now - created).total_seconds() < _PREVIEW_TTL_MINUTES * 60
                )
            else:
                blocks = verdict
        elif status in sent_statuses:
            if broker_open is None:
                blocks = True   # broker truth unavailable — fail-closed
            else:
                blocks = (symbol, side) in broker_open
        # terminal(cancelled/filled/blocked/…)·기타 상태는 차단하지 않음

        if not blocks:
            continue
        entry = {
            "side": side,
            "status": status or "pending",
            "created_at": r.get("created_at") or r.get("sent_at") or "",
            "pilot_id": r.get("pilot_id") or "",
            # provenance: 이 pending 판단의 출처
            "source": (
                "internal_ledger+broker_snapshot" if status in sent_statuses
                else "internal_ledger+verification"
            ),
        }
        out[symbol] = entry
        if symbol.endswith((".KS", ".KQ")):
            out[symbol.split(".", 1)[0]] = entry
    return out


def _kis_price_for_symbol(symbol: str) -> dict:
    """Read KIS price when available. No writes, no order path."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {"ok": False, "source": "KIS", "reason": "missing_symbol"}
    try:
        from core.market_kis import get_domestic_price, get_overseas_price
        if sym.endswith((".KS", ".KQ")) or (sym.isdigit() and len(sym) == 6):
            for cand in _kr_symbol_candidates(sym):
                q = get_domestic_price(cand)
                if q:
                    price = _to_float(q.get("price") if isinstance(q, dict) else getattr(q, "price", None))
                    if price and price > 0:
                        return {"ok": True, "source": "KIS", "symbol": cand, "price": price}
            return {"ok": False, "source": "KIS", "reason": "domestic_price_unavailable"}
        q = get_overseas_price(sym)
        if q:
            price = _to_float(q.get("price") if isinstance(q, dict) else getattr(q, "price", None))
            if price and price > 0:
                return {"ok": True, "source": "KIS", "symbol": sym, "price": price}
        return {"ok": False, "source": "KIS", "reason": "overseas_price_unavailable"}
    except Exception as e:
        return {"ok": False, "source": "KIS", "reason": str(e)[-120:]}


def _price_gap_pct(a: float | None, b: float | None) -> float | None:
    if not a or not b or a <= 0 or b <= 0:
        return None
    return round((a / b - 1.0) * 100.0, 2)


def _quality_tone_from_gap(abs_gap: float | None, warn_pct: float = 0.7, block_pct: float = 2.0) -> str:
    if abs_gap is None:
        return "unknown"
    if abs_gap >= block_pct:
        return "block"
    if abs_gap >= warn_pct:
        return "warn"
    return "ok"


def _cross_check_price_quality(symbol: str, current_price: float | None = None) -> dict:
    """Cross-check every overlapping Toss/KIS price field available for a symbol."""
    sym = str(symbol or "").upper().strip()
    cur = _to_float(current_price)
    kis = _kis_price_for_symbol(sym)
    holdings = _toss_holding_price_map()
    toss_row = holdings.get(sym)
    if not toss_row and sym.endswith((".KS", ".KQ")):
        toss_row = holdings.get(sym.split(".", 1)[0])
    toss_price = _to_float((toss_row or {}).get("last_price"))

    checks: list[dict] = []
    tones: list[str] = []

    if kis.get("ok") and cur:
        gap = _price_gap_pct(cur, kis.get("price"))
        tone = _quality_tone_from_gap(abs(gap) if gap is not None else None)
        checks.append({"name": "KIS vs 후보 현재가", "tone": tone, "source_a": "candidate.current_price", "price_a": cur, "source_b": "KIS", "price_b": kis.get("price"), "gap_pct": gap})
        tones.append(tone)
    elif cur:
        checks.append({"name": "KIS vs 후보 현재가", "tone": "unknown", "reason": kis.get("reason") or "kis_unavailable"})

    if kis.get("ok") and toss_price:
        gap = _price_gap_pct(toss_price, kis.get("price"))
        tone = _quality_tone_from_gap(abs(gap) if gap is not None else None)
        checks.append({"name": "Toss 보유평가가 vs KIS 현재가", "tone": tone, "source_a": "Toss.holdings.lastPrice", "price_a": toss_price, "source_b": "KIS", "price_b": kis.get("price"), "gap_pct": gap})
        tones.append(tone)

    if toss_price and cur:
        gap = _price_gap_pct(toss_price, cur)
        tone = _quality_tone_from_gap(abs(gap) if gap is not None else None)
        checks.append({"name": "Toss 보유평가가 vs 후보 현재가", "tone": tone, "source_a": "Toss.holdings.lastPrice", "price_a": toss_price, "source_b": "candidate.current_price", "price_b": cur, "gap_pct": gap})
        tones.append(tone)

    # Only true Toss↔KIS overlap may hard-block. Candidate-vs-KIS alone is
    # a scanner freshness warning because it is not a broker-side Toss price.
    has_toss_kis_overlap = bool(toss_price and kis.get("ok"))
    if "block" in tones and has_toss_kis_overlap:
        quality = "low"; action = "BLOCK_DATA_MISMATCH"
    elif "block" in tones or "warn" in tones:
        quality = "medium"; action = "SMALL_PASS_OR_REDUCE_SIZE"
    elif "ok" in tones:
        quality = "high"; action = "PASS_CONFIDENCE_UP"
    else:
        quality = "unknown"; action = "NO_OVERLAP_AVAILABLE"

    return {"schema": "toss_kis_price_cross_check.v1", "symbol": sym, "quality": quality, "action_hint": action, "has_toss_holding_price": bool(toss_price), "has_kis_price": bool(kis.get("ok")), "checks": checks}


def _build_toss_kis_cross_check_summary(sample_items: list[dict] | None = None) -> dict:
    """Aggregate read-only Toss+KIS cross-check status for dashboard/agent use."""
    sample_items = sample_items or []
    rows: list[dict] = []
    for sym, row in list(_toss_holding_price_map().items()):
        if "." in sym:
            continue
        rows.append(_cross_check_price_quality(sym, row.get("last_price")))
    seen = {r.get("symbol") for r in rows}
    for item in sample_items[:10]:
        sym = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
        if not sym or sym in seen:
            continue
        rows.append(_cross_check_price_quality(sym, item.get("current_price") or item.get("price") or item.get("limit_price")))
        seen.add(sym)
    counts = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
    for r in rows:
        q = r.get("quality") or "unknown"
        counts[q] = counts.get(q, 0) + 1
    overall = "low" if counts.get("low") else ("medium" if counts.get("medium") else ("high" if counts.get("high") else "unknown"))
    return {"schema": "toss_kis_cross_check.v1.read_only", "overall_quality": overall, "counts": counts, "rows": rows[:20], "rule": "Toss와 KIS에서 겹치는 정보는 모두 대조. 일치=신뢰도 상승, 중간 괴리=수량 축소, 큰 괴리=BLOCK."}


def toss_paper_trades(limit: int = 50) -> dict:
    """Toss paper trade 목록."""
    from core.toss_paper_trading import list_paper_trades
    trades = list_paper_trades(limit=limit)
    return {"trades": trades, "count": len(trades)}


def _fetch_toss_decision_context_raw() -> dict:
    """Toss 판단 컨텍스트 (dashboard 표시용)."""
    from core.toss_decision_context import get_toss_decision_context
    return get_toss_decision_context()


def toss_decision_context() -> dict:
    """Toss 판단 컨텍스트 (60초 캐시)."""
    return _cached("toss_decision_context", 60, _fetch_toss_decision_context_raw)


def _fetch_toss_cross_check_raw() -> dict:
    """Toss/KIS 교차 검증 요약."""
    from core.toss_decision_context import get_toss_decision_context
    from core.toss_cross_check import cross_check_summary
    ctx = get_toss_decision_context()
    out = cross_check_summary(ctx)
    if not isinstance(out, dict):
        out = {}
    quality = _build_toss_kis_cross_check_summary()
    out["data_quality"] = quality
    out["data_quality_summary"] = {
        "overall_quality": quality.get("overall_quality"),
        "counts": quality.get("counts", {}),
        "rule": quality.get("rule"),
    }
    if quality.get("overall_quality") == "low":
        out.setdefault("warnings", []).append("Toss/KIS 가격 교차검증 큰 괴리 — 자동 실행 BLOCK 대상")
        out["all_ok"] = False
    return out


def toss_cross_check() -> dict:
    """Toss/KIS 교차 검증 (30초 캐시)."""
    return _cached("toss_cross_check", 30, _fetch_toss_cross_check_raw)


def toss_paper_ledger_data(limit: int = 50) -> dict:
    """Toss paper ledger 조회 (dashboard용). expired/stale 카운트 포함."""
    from core.toss_paper_ledger import paper_ledger_summary
    summary = paper_ledger_summary()
    counts = summary.get("counts", {})
    summary["stale_preview_count"] = counts.get("previewed", 0)
    summary["expired_count"] = counts.get("expired", 0)
    return summary


def toss_paper_performance_data() -> dict:
    """Toss paper 성과 요약 (120초 캐시). 실제 주문 0건. 기존 포트폴리오 미합산."""
    def _fetch():
        from core.toss_paper_performance import get_paper_performance_summary
        return get_paper_performance_summary()
    return _cached("toss_paper_performance", 120, _fetch)


def toss_paper_policy_data() -> dict:
    """Toss paper sizing/risk policy (120초 캐시). 실제 주문 0건."""
    def _fetch():
        from core.toss_paper_policy import compute_toss_paper_policy
        return compute_toss_paper_policy()
    return _cached("toss_paper_policy", 120, _fetch)




# ─── /api/market/discovery — 계좌 비의존 광역 시장 레이더 ─────────────
def market_discovery_data(range_: str = "today", limit: int = 50) -> dict:
    """삼성/ISA/RIA/IRP/토스 공용 광역 후보 레이더 (read-only)."""
    def _fetch():
        from core.discovery_candidates import build_discovery_sections, market_discovery_radar
        sections = build_discovery_sections(briefing_type="KR_OPEN")
        return market_discovery_radar(sections, limit=limit)
    return _cached(f"market_discovery:{range_}:{limit}", 120, _fetch)



def toss_rebalance_plan_data(limit: int = 80, market: str = "ALL") -> dict:
    """Toss income 리밸런싱 계획 (GET-only). 주문/취소/정정 없음."""
    try:
        account = toss_account_summary() or {}
    except Exception as e:
        account = {"error": str(e)[:180], "holdings_items": [], "holdings_count": 0}
    try:
        candidates = toss_buy_candidates_data(range_="today", limit=limit, market=market) or {}
        items = candidates.get("items") or []
    except Exception as e:
        candidates = {"error": str(e)[:180]}
        items = []
    try:
        from core.toss_income_strategy import build_rebalance_plan
        plan = build_rebalance_plan(account, items)
    except Exception as e:
        plan = {"version": "income_rebalance_v1", "read_only": True, "error": str(e)[:180]}
    plan["source"] = "toss_rebalance_plan_data"
    plan["candidate_error"] = candidates.get("error") if isinstance(candidates, dict) else None
    return plan

# ─── /api/toss/buy-candidates — 토스 전용 매수 후보 (신규 발굴 기반) ─────────────
_AI_BERKSHIRE_BUY_GATE_VERSION = "ai_berkshire_buy_gate_strict_v3"
_TOSS_BUY_CANDIDATE_CACHE_LIMIT = 100


def _apply_ai_berkshire_buy_gate(out: dict, scores: dict | None) -> dict:
    """신규 BUY 후보에 AI Berkshire 질적 게이트를 적용 (in-place).

    근거가 살아있는 avoid 또는 strict BUY 게이트 거부를 stock_agent_ready에서
    하드 차단한다. marker 없는 legacy score는 avoid_only 동작을 유지한다.
    score unavailable·게이트 오류는 fail-closed하고, 정상 score의 unscored symbol만
    진단 필드를 남긴 채 기존 판정을 유지한다. SELL 후보는 영향을 받지 않는다.
    """
    from core.ai_berkshire_toss import evaluate_ai_berkshire_buy_gate

    symbol = str(out.get("symbol") or out.get("ticker") or "")
    if str(out.get("side") or "buy").lower() != "buy":
        return out
    try:
        gate = evaluate_ai_berkshire_buy_gate(symbol, scores=scores or {})
    except Exception as e:
        log.warning("ai_berkshire buy gate failed (%s): %s", symbol, e)
        gate = {
            "buy_block": True,
            "buy_reason": "ai_berkshire_gate_error",
            "research_status": "needs_research",
            "stored_classification": None,
            "classification": None,
            "freshness_valid": False,
            "thesis_expired": False,
            "freshness_issues": ["gate_error"],
            "as_of": None,
            "valid_until": None,
            "confidence": None,
            "source_urls": [],
            "buy_checklist_status": None,
        }
    if gate.get("buy_reason") == "ai_berkshire_scores_unavailable":
        gate["buy_block"] = True

    out["ai_berkshire_buy_block"] = gate["buy_block"]
    out["ai_berkshire_buy_reason"] = gate["buy_reason"]
    out["ai_berkshire_research_status"] = gate["research_status"]
    out["ai_berkshire_buy_gate"] = {
        "version": _AI_BERKSHIRE_BUY_GATE_VERSION,
        "stored_classification": gate["stored_classification"],
        "classification": gate["classification"],
        "classification_valid": gate.get("classification_valid", False),
        "strict_buy_gate": gate.get("strict_buy_gate", False),
        "freshness_valid": gate["freshness_valid"],
        "thesis_expired": gate["thesis_expired"],
        "freshness_issues": gate["freshness_issues"],
        "as_of": gate["as_of"],
        "valid_until": gate["valid_until"],
        "confidence": gate["confidence"],
        "source_urls": gate["source_urls"],
        "buy_checklist_status": gate["buy_checklist_status"],
    }
    if not gate["buy_block"]:
        return out

    reason = str(gate.get("buy_reason") or "ai_berkshire_buy_blocked")
    if reason == "ai_berkshire_avoid":
        block_reason = "AI Berkshire avoid 판정 — 신규 BUY 차단 (기존 보유/매도 판단은 불변)"
        execution_status = "hold_ai_berkshire_avoid"
    elif reason in {"ai_berkshire_scores_unavailable", "ai_berkshire_gate_error"}:
        block_reason = "AI Berkshire score/게이트 확인 불가 — 신규 BUY fail-closed"
        execution_status = "hold_ai_berkshire_unavailable"
    else:
        checklist = str(gate.get("buy_checklist_status") or "blocked")
        block_reason = (
            f"AI Berkshire BUY 체크리스트 {checklist} — 신규 BUY 차단 "
            "(기존 보유/리스크 매도 판단은 불변)"
        )
        execution_status = "hold_ai_berkshire_buy_checklist"
    out["stock_agent_ready"] = False
    out["executable_now"] = False
    out["execution_status"] = execution_status
    out["block_reason"] = block_reason
    out.setdefault("risk_notes", []).append(block_reason)
    return out


def toss_buy_candidates_data(range_: str = "today", limit: int = 20, market: str = "KR") -> dict:
    """토스 전용 매수 후보 조회 (read-only) — 신규 발굴 기반.

    삼성/RIA/ISA/IRP 등 기존 계좌 추천(predictions DB)을 재사용하지 않는다.
    market은 "KR" | "US" | "ALL". 기존 KR 고정 후보 공급 때문에 USD 예수금이
    남아도 미장 자동매매가 돌지 않던 문제를 막기 위해 시장별 후보를 분리한다.
    주문 생성/승인/전송은 하지 않는다.
    """
    market_norm = str(market or "KR").strip().upper()
    if market_norm in ("BOTH", "KRUS", "KR_US"):
        market_norm = "ALL"
    if market_norm not in {"KR", "US", "ALL"}:
        market_norm = "KR"
    markets = ["KR", "US"] if market_norm == "ALL" else [market_norm]
    briefing_type = "US_BEFORE" if market_norm == "US" else ("MANUAL" if market_norm == "ALL" else "KR_BEFORE")
    calibration_fields = frozenset({
        "schema", "status", "mode", "decision_usable", "decision_block_reason",
        "attribution_model", "attribution_verified", "cost_model", "reason",
        "error_type", "completed_count", "wins", "losses", "flats", "win_rate",
        "avg_win_pct", "avg_loss_pct", "mean_net_return_pct",
        "minimum_sample_reached", "sample_sufficient", "evidence_sufficient",
        "min_samples", "lineage_status", "lineage_reasons",
        "unmatched_sell_fill_count", "unmatched_sell_quantity",
        "symbol_alias_conflict_count", "ambiguous_fill_count",
        "holdings_reconciliation_status", "open_quantity_exceeds_holdings",
        "open_lot_count", "open_quantity", "ignored_count",
        "quarantined_fill_count", "invalid_fill_count", "conflict_count",
        "source", "source_window_truncated", "source_row_limit",
        "source_rows_loaded", "ledger_reason_conflict_count",
        "ledger_reason_missing_count", "ledger_reason_invalid_count",
        "holdings_symbol_alias_conflict_count",
    })
    calibration_forbidden_fields = frozenset({"outcomes", "open_positions"})
    calibration_count_fields = frozenset({
        "completed_count", "wins", "losses", "flats", "min_samples",
        "unmatched_sell_fill_count", "unmatched_sell_quantity",
        "symbol_alias_conflict_count", "ambiguous_fill_count",
        "holdings_symbol_alias_conflict_count", "open_quantity_exceeds_holdings",
        "open_lot_count", "open_quantity", "ignored_count",
        "quarantined_fill_count", "invalid_fill_count", "conflict_count",
        "source_row_limit", "source_rows_loaded", "ledger_reason_conflict_count",
        "ledger_reason_missing_count", "ledger_reason_invalid_count",
    })
    calibration_required_fields = frozenset({
        "schema", "status", "mode", "decision_usable", "decision_block_reason",
        "attribution_model", "attribution_verified", "cost_model",
        "completed_count", "wins", "losses", "flats", "win_rate",
        "avg_win_pct", "avg_loss_pct", "mean_net_return_pct",
        "minimum_sample_reached", "sample_sufficient", "evidence_sufficient",
        "min_samples", "lineage_status", "lineage_reasons",
        "unmatched_sell_fill_count", "unmatched_sell_quantity",
        "symbol_alias_conflict_count", "ambiguous_fill_count",
        "holdings_reconciliation_status", "holdings_symbol_alias_conflict_count",
        "open_quantity_exceeds_holdings", "open_lot_count", "open_quantity",
        "ignored_count", "quarantined_fill_count", "invalid_fill_count",
        "conflict_count", "source", "source_window_truncated",
        "source_row_limit", "source_rows_loaded", "ledger_reason_conflict_count",
        "ledger_reason_missing_count", "ledger_reason_invalid_count",
    })
    calibration_lineage_reasons = frozenset({
        "pilot_payload_conflict", "krx_symbol_alias_conflict",
        "fill_contract_invalid", "fill_order_ambiguous", "unmatched_sell_fill",
        "holdings_reconciliation_unavailable", "open_lots_exceed_holdings",
        "holdings_symbol_alias_conflict", "source_window_truncated",
        "ledger_reason_conflict", "ledger_reason_missing", "ledger_reason_invalid",
        "execution_calibration_source_unavailable",
    })

    def _unavailable_execution_calibration(
        reason: str,
        error_type: str | None = None,
    ) -> dict:
        payload = {
            "schema": "toss_execution_calibration.v1",
            "status": "unavailable",
            "mode": "observability_only",
            "decision_usable": False,
            "decision_block_reason": "lifecycle_transition_model_unvalidated",
            "attribution_model": "symbol_fifo_v1",
            "attribution_verified": False,
            "cost_model": "decision_buffer_v1_not_broker_statement",
            "reason": reason,
            "completed_count": 0,
            "wins": 0,
            "losses": 0,
            "flats": 0,
            "win_rate": None,
            "avg_win_pct": None,
            "avg_loss_pct": None,
            "mean_net_return_pct": None,
            "minimum_sample_reached": False,
            "sample_sufficient": False,
            "evidence_sufficient": False,
            "min_samples": 20,
            "lineage_status": "incomplete",
            "lineage_reasons": [
                "execution_calibration_source_unavailable",
                "holdings_reconciliation_unavailable",
            ],
            "unmatched_sell_fill_count": 0,
            "unmatched_sell_quantity": 0,
            "symbol_alias_conflict_count": 0,
            "ambiguous_fill_count": 0,
            "holdings_reconciliation_status": "unavailable",
            "holdings_symbol_alias_conflict_count": 0,
            "open_quantity_exceeds_holdings": 0,
            "open_lot_count": 0,
            "open_quantity": 0,
            "ignored_count": 0,
            "quarantined_fill_count": 0,
            "invalid_fill_count": 0,
            "conflict_count": 0,
            "source": "read_only_live_pilot_event_ledger",
            "source_window_truncated": False,
            "source_row_limit": 5_000,
            "source_rows_loaded": 0,
            "ledger_reason_conflict_count": 0,
            "ledger_reason_missing_count": 0,
            "ledger_reason_invalid_count": 0,
        }
        if error_type is not None:
            payload["error_type"] = error_type
        return payload

    def _exact_text(value: object, expected: str | frozenset[str]) -> bool:
        if type(value) is not str:
            return False
        if type(expected) is str:
            return value == expected
        return value in expected

    def _bounded_number(
        value: object,
        minimum: float,
        maximum: float,
    ) -> bool:
        if type(value) is int:
            return minimum <= value <= maximum
        if type(value) is float:
            return math.isfinite(value) and minimum <= value <= maximum
        return False

    def _bounded_int(value: object, minimum: int, maximum: int) -> bool:
        return type(value) is int and minimum <= value <= maximum

    def _exact_number(value: object) -> float | None:
        if type(value) is int:
            return float(value)
        if type(value) is float and math.isfinite(value):
            return value
        return None

    def _safe_execution_calibration(value: object) -> dict | None:
        if type(value) is not dict:
            return None
        if (
            any(type(key) is not str for key in value)
            or calibration_forbidden_fields.intersection(value)
            or set(value).difference(calibration_fields)
            or not calibration_required_fields.issubset(value)
        ):
            return None
        if (
            not _exact_text(value.get("schema"), "toss_execution_calibration.v1")
            or not _exact_text(
                value.get("status"), frozenset({"ok", "partial", "unavailable"})
            )
            or not _exact_text(value.get("mode"), "observability_only")
            or value.get("decision_usable") is not False
            or not _exact_text(
                value.get("decision_block_reason"),
                "lifecycle_transition_model_unvalidated",
            )
            or not _exact_text(value.get("attribution_model"), "symbol_fifo_v1")
            or value.get("attribution_verified") is not False
            or not _exact_text(
                value.get("cost_model"),
                "decision_buffer_v1_not_broker_statement",
            )
            or value.get("evidence_sufficient") is not False
            or not _exact_text(
                value.get("source"), "read_only_live_pilot_event_ledger"
            )
            or not _exact_text(
                value.get("holdings_reconciliation_status"),
                frozenset({"complete", "incomplete", "unavailable"}),
            )
        ):
            return None
        if any(
            not _bounded_int(value.get(field), 0, 10_000_000)
            for field in calibration_count_fields
        ):
            return None
        source_rows_loaded = value["source_rows_loaded"]
        row_based_count_fields = (
            "completed_count",
            "wins",
            "losses",
            "flats",
            "unmatched_sell_fill_count",
            "open_lot_count",
            "ignored_count",
            "quarantined_fill_count",
            "invalid_fill_count",
            "conflict_count",
            "symbol_alias_conflict_count",
            "ambiguous_fill_count",
            "ledger_reason_missing_count",
            "ledger_reason_conflict_count",
            "ledger_reason_invalid_count",
        )
        if (
            any(value[field] > source_rows_loaded for field in row_based_count_fields)
            or value["invalid_fill_count"] > value["quarantined_fill_count"]
            or value["quarantined_fill_count"] > value["ignored_count"]
        ):
            return None
        has_quarantine_cause = bool(
            value["invalid_fill_count"]
            or value["conflict_count"]
            or value["symbol_alias_conflict_count"]
        )
        if (value["quarantined_fill_count"] > 0) != has_quarantine_cause:
            return None
        minimum_quarantine = (
            value["invalid_fill_count"]
            + (2 * value["conflict_count"])
            + (2 * value["symbol_alias_conflict_count"])
        )
        minimum_lifecycle_rows = (
            value["ignored_count"]
            + value["open_lot_count"]
            + value["completed_count"]
            + (
                1
                if (
                    value["completed_count"]
                    and value["unmatched_sell_fill_count"] == 0
                )
                else 0
            )
        )
        if (
            value["quarantined_fill_count"] < minimum_quarantine
            or value["invalid_fill_count"] < value["ambiguous_fill_count"]
            or value["ambiguous_fill_count"] == 1
            or source_rows_loaded < minimum_lifecycle_rows
            or (
                value["source_window_truncated"]
                and source_rows_loaded != value["source_row_limit"]
            )
        ):
            return None
        if (
            not 1 <= value["min_samples"] <= 1_000_000
            or not 1 <= value["source_row_limit"] <= 10_000
            or value["source_rows_loaded"] > value["source_row_limit"]
            or value["wins"] + value["losses"] + value["flats"]
            != value["completed_count"]
            or value["completed_count"] > value["source_rows_loaded"]
        ):
            return None
        for key in ("minimum_sample_reached", "sample_sufficient", "source_window_truncated"):
            if type(value.get(key)) is not bool:
                return None
        sample_reached = value["completed_count"] >= value["min_samples"]
        if (
            value["minimum_sample_reached"] is not sample_reached
            or value["sample_sufficient"] is not sample_reached
        ):
            return None
        lineage_reasons = value.get("lineage_reasons")
        if (
            type(lineage_reasons) is not list
            or len(lineage_reasons) > 20
            or any(
                type(reason) is not str
                or reason not in calibration_lineage_reasons
                for reason in lineage_reasons
            )
            or len(set(lineage_reasons)) != len(lineage_reasons)
        ):
            return None
        lineage_status = value.get("lineage_status")
        if (
            not _exact_text(
                lineage_status, frozenset({"complete", "incomplete"})
            )
            or (lineage_status == "complete") != (not lineage_reasons)
            or (value["status"] == "ok") != (lineage_status == "complete")
        ):
            return None
        if (
            (value["unmatched_sell_fill_count"] > 0)
            != (value["unmatched_sell_quantity"] > 0)
            or value["quarantined_fill_count"] < value["invalid_fill_count"]
        ):
            return None
        expected_lineage_reasons = {
            "pilot_payload_conflict": value["conflict_count"] > 0,
            "krx_symbol_alias_conflict": value["symbol_alias_conflict_count"] > 0,
            "fill_contract_invalid": value["invalid_fill_count"] > 0,
            "unmatched_sell_fill": value["unmatched_sell_fill_count"] > 0,
            "holdings_reconciliation_unavailable": (
                value["holdings_reconciliation_status"] == "unavailable"
            ),
            "open_lots_exceed_holdings": value["open_quantity_exceeds_holdings"] > 0,
            "holdings_symbol_alias_conflict": (
                value["holdings_symbol_alias_conflict_count"] > 0
            ),
            "source_window_truncated": value["source_window_truncated"],
            "ledger_reason_conflict": value["ledger_reason_conflict_count"] > 0,
            "ledger_reason_missing": value["ledger_reason_missing_count"] > 0,
            "ledger_reason_invalid": value["ledger_reason_invalid_count"] > 0,
            "fill_order_ambiguous": value["ambiguous_fill_count"] > 0,
            "execution_calibration_source_unavailable": value["status"] == "unavailable",
        }
        reason_set = set(lineage_reasons)
        if any(
            (reason in reason_set) is not expected
            for reason, expected in expected_lineage_reasons.items()
        ):
            return None
        holdings_issue = (
            value["open_quantity_exceeds_holdings"] > 0
            or value["holdings_symbol_alias_conflict_count"] > 0
        )
        if (
            (value["holdings_reconciliation_status"] == "incomplete")
            != holdings_issue
        ):
            return None
        win_rate = value.get("win_rate")
        expected_win_rate = (
            value["wins"] / value["completed_count"]
            if value["completed_count"] else None
        )
        if expected_win_rate is None:
            if win_rate is not None:
                return None
        elif (
            not _bounded_number(win_rate, 0, 1)
            or abs(win_rate - expected_win_rate) > 0.0001
        ):
            return None
        avg_win = value.get("avg_win_pct")
        avg_loss = value.get("avg_loss_pct")
        mean_return = value.get("mean_net_return_pct")
        avg_win_number = _exact_number(avg_win)
        avg_loss_number = _exact_number(avg_loss)
        if (
            (value["wins"] == 0 and avg_win is not None)
            or (
                value["wins"] > 0
                and (
                    not _bounded_number(avg_win, 0, 100_000)
                    or avg_win_number is None
                    or avg_win_number <= 0
                )
            )
            or (value["losses"] == 0 and avg_loss is not None)
            or (
                value["losses"] > 0
                and (
                    not _bounded_number(avg_loss, -100_000, 0)
                    or avg_loss_number is None
                    or avg_loss_number >= 0
                )
            )
            or (
                value["completed_count"] == 0
                and mean_return is not None
            )
            or (
                value["completed_count"] > 0
                and not _bounded_number(mean_return, -100_000, 100_000)
            )
        ):
            return None
        if value["completed_count"]:
            mean_number = _exact_number(mean_return)
            if mean_number is None:
                return None
            expected_mean = (
                ((avg_win_number or 0) * value["wins"])
                + ((avg_loss_number or 0) * value["losses"])
            ) / value["completed_count"]
            if abs(mean_number - expected_mean) > 0.0002:
                return None
        for key in ("reason", "error_type"):
            text = value.get(key)
            if text is not None and (type(text) is not str or len(text) > 200):
                return None
        if value["status"] == "unavailable":
            unavailable_zero_fields = calibration_count_fields.difference({
                "min_samples",
                "source_row_limit",
            })
            if (
                type(value.get("reason")) is not str
                or any(value[field] != 0 for field in unavailable_zero_fields)
                or value["source_window_truncated"] is not False
                or value["minimum_sample_reached"] is not False
                or value["sample_sufficient"] is not False
                or value["win_rate"] is not None
                or value["avg_win_pct"] is not None
                or value["avg_loss_pct"] is not None
                or value["mean_net_return_pct"] is not None
                or value["holdings_reconciliation_status"] != "unavailable"
            ):
                return None
        safe = {}
        for key, field_value in value.items():
            safe[key] = list(field_value) if key == "lineage_reasons" else field_value
        return safe

    cache_invalid = object()
    cache_top_level_fields = frozenset({
        "schema", "scan_summary", "items", "excluded", "count",
        "excluded_count", "range", "max_order_krw", "note",
    })
    cache_required_summary_fields = frozenset({
        "income_gate_version", "income_liveness_version",
        "income_liveness_status", "raw_income_pass_count",
        "income_pass_count", "income_block_count",
        "income_gate_eligible_count", "upstream_executable_count",
        "income_ready_count", "income_liveness_diagnosis",
        "execution_calibration",
    })
    cache_summary_fields = frozenset({
        "universe_count", "scanned_count", "dependency_fallback_used",
        "pandas_available", "source", "pass_count", "reject_count",
        "top_reject_reasons", "executable_count",
        "conditional_small_entry_count", "limit_exceeded_count",
        "toss_held_excluded_count", "toss_held_excluded_symbols",
        "recent_risk_sell_excluded_count", "recent_risk_sell_excluded_symbols",
        "market", "markets", "user_blocked_buy_symbols", "user_blocked_count",
        "configured_max_order_krw", "candidate_affordability_limit_krw",
        "snapshot_candidate_blocked", "snapshot_status", "snapshot_block_count",
        "portfolio_rebalance_required", "portfolio_income_ready_cap",
        "holdings_count_for_income_gate", "portfolio_cap_block_count",
        "rebalance_plan", "rebalance_plan_error", "raw_income_pass_count",
        "income_pass_count", "income_block_count", "income_gate_eligible_count",
        "upstream_executable_count", "income_ready_count",
        "income_liveness_status", "income_liveness_diagnosis",
        "income_liveness_version", "execution_calibration", "income_gate_version",
        "ai_berkshire_gate_version", "ai_berkshire_buy_block_count",
        "ai_berkshire_needs_research_count",
    })
    liveness_diagnosis_fields = frozenset({
        "reason", "upstream_executable_count", "income_pass_count",
        "income_ready_count", "top_income_block_reasons",
    })

    def _safe_json_value(value: object, depth: int = 0) -> object:
        if depth > 8:
            return cache_invalid
        if value is None or type(value) is bool:
            return value
        if type(value) is int:
            return value if abs(value) <= 10**18 else cache_invalid
        if type(value) is float:
            return value if math.isfinite(value) and abs(value) <= 10**18 else cache_invalid
        if type(value) is str:
            return value if len(value) <= 50_000 else cache_invalid
        if type(value) is list:
            if len(value) > 1_000:
                return cache_invalid
            projected_list = []
            for item in value:
                projected = _safe_json_value(item, depth + 1)
                if projected is cache_invalid:
                    return cache_invalid
                projected_list.append(projected)
            return projected_list
        if type(value) is dict:
            if len(value) > 250:
                return cache_invalid
            projected_dict = {}
            for key, item in value.items():
                if (
                    type(key) is not str
                    or len(key) > 200
                    or key in calibration_forbidden_fields
                ):
                    return cache_invalid
                projected = _safe_json_value(item, depth + 1)
                if projected is cache_invalid:
                    return cache_invalid
                projected_dict[key] = projected
            return projected_dict
        return cache_invalid

    def _safe_liveness_diagnosis(
        value: object,
        status: str,
    ) -> object:
        if status in {"healthy", "idle"}:
            return None if value is None else cache_invalid
        if (
            type(value) is not dict
            or any(type(key) is not str for key in value)
            or set(value) != liveness_diagnosis_fields
        ):
            return cache_invalid
        expected_reason = {
            "degraded": "upstream_executable_but_no_income_ready",
            "downstream_blocked": "income_pass_but_no_final_ready",
            "no_signal": "no_income_gate_eligible_candidates",
        }.get(status)
        if not _exact_text(value.get("reason"), expected_reason or ""):
            return cache_invalid
        for key in (
            "upstream_executable_count", "income_pass_count", "income_ready_count"
        ):
            if type(value.get(key)) is not int or not 0 <= value[key] <= 10_000_000:
                return cache_invalid
        reasons = value.get("top_income_block_reasons")
        if type(reasons) is not list or len(reasons) > 5:
            return cache_invalid
        safe_reasons = []
        for row in reasons:
            if (
                type(row) is not dict
                or any(type(key) is not str for key in row)
                or set(row) != {"reason", "count"}
            ):
                return cache_invalid
            if (
                type(row.get("reason")) is not str
                or not row["reason"].strip()
                or len(row["reason"]) > 200
                or type(row.get("count")) is not int
                or not 1 <= row["count"] <= 10_000_000
            ):
                return cache_invalid
            safe_reasons.append({"reason": row["reason"], "count": row["count"]})
        return {
            "reason": value["reason"],
            "upstream_executable_count": value["upstream_executable_count"],
            "income_pass_count": value["income_pass_count"],
            "income_ready_count": value["income_ready_count"],
            "top_income_block_reasons": safe_reasons,
        }

    pre_income_block_statuses = frozenset({
        "hold_risk_flags",
        "chase_block",
        "data_quality_block",
        "cash_unavailable",
        "quality_finalization_failed",
        "toss_snapshot_stale",
    })

    def _income_gate_eligible(item: dict) -> bool:
        if str(item.get("decision_bucket") or "") not in {
            "PASS_EXECUTE", "SMALL_PASS"
        }:
            return False
        if item.get("missing_fields"):
            return False
        if item.get("limit_exceeded") is True:
            return False
        if item.get("blocking_risk_flags"):
            return False
        return str(item.get("execution_status") or "") not in (
            pre_income_block_statuses
        )

    def _item_income_pass(item: dict) -> bool:
        income = item.get("income_strategy")
        return type(income) is dict and income.get("income_pass") is True

    def _safe_candidate_cache(value: object) -> dict | None:
        if (
            type(value) is not dict
            or any(type(key) is not str for key in value)
            or set(value).difference(cache_top_level_fields)
            or not {"schema", "scan_summary", "items", "excluded", "count", "excluded_count"}.issubset(value)
            or not _exact_text(value.get("schema"), "toss_buy_candidates.v3.dual_income_ev")
        ):
            return None
        summary = value.get("scan_summary")
        if (
            type(summary) is not dict
            or any(type(key) is not str for key in summary)
            or set(summary).difference(cache_summary_fields)
            or calibration_forbidden_fields.intersection(summary)
            or not cache_required_summary_fields.issubset(summary)
            or not _exact_text(summary.get("income_gate_version"), "income_v2_dual_ev")
            or not _exact_text(summary.get("income_liveness_version"), "income_liveness_v1")
            or not _exact_text(
                summary.get("income_liveness_status"),
                frozenset({"healthy", "degraded", "downstream_blocked", "no_signal", "idle"}),
            )
        ):
            return None
        raw_items = value.get("items")
        raw_excluded = value.get("excluded")
        if type(raw_items) is not list or type(raw_excluded) is not list:
            return None
        items = _safe_json_value(raw_items)
        excluded = _safe_json_value(raw_excluded)
        if (
            type(items) is not list
            or any(type(item) is not dict for item in items)
            or type(excluded) is not list
            or any(type(item) is not dict for item in excluded)
            or type(value.get("count")) is not int
            or value["count"] != len(items)
            or type(value.get("excluded_count")) is not int
            or value["excluded_count"] != len(excluded)
        ):
            return None
        for item in items:
            ready = item.get("stock_agent_ready")
            if type(ready) is not bool:
                return None
            if ready is True:
                from core.toss_quality_gate import validate_ready_candidate_contract
                ready_ok, _ = validate_ready_candidate_contract(item)
                if not ready_ok:
                    return None
        calibration = _safe_execution_calibration(summary.get("execution_calibration"))
        diagnosis = _safe_liveness_diagnosis(
            summary.get("income_liveness_diagnosis"),
            summary["income_liveness_status"],
        )
        if calibration is None or diagnosis is cache_invalid:
            return None
        for key in (
            "raw_income_pass_count", "income_pass_count", "income_block_count",
            "income_gate_eligible_count", "upstream_executable_count",
            "income_ready_count",
        ):
            if type(summary.get(key)) is not int or not 0 <= summary[key] <= 10_000_000:
                return None
        derived_eligible_items = [
            item for item in items if _income_gate_eligible(item)
        ]
        derived_counts = {
            "raw_income_pass_count": sum(
                1 for item in items if _item_income_pass(item)
            ),
            "income_pass_count": sum(
                1 for item in derived_eligible_items if _item_income_pass(item)
            ),
            "income_block_count": sum(
                1 for item in derived_eligible_items if not _item_income_pass(item)
            ),
            "income_gate_eligible_count": len(derived_eligible_items),
            "upstream_executable_count": len(derived_eligible_items),
            "income_ready_count": sum(
                1 for item in items if item["stock_agent_ready"] is True
            ),
        }
        if any(summary[key] != count for key, count in derived_counts.items()):
            return None
        expected_liveness_status = (
            "healthy"
            if summary["income_ready_count"] > 0
            else "degraded"
            if summary["upstream_executable_count"] > 0
            and summary["income_pass_count"] == 0
            else "downstream_blocked"
            if summary["income_pass_count"] > 0
            else "no_signal"
            if len(items) > 0
            else "idle"
        )
        if (
            summary["income_gate_eligible_count"]
            != summary["upstream_executable_count"]
            or summary["income_block_count"]
            != summary["upstream_executable_count"] - summary["income_pass_count"]
            or summary["raw_income_pass_count"] < summary["income_pass_count"]
            or summary["income_ready_count"] > summary["income_pass_count"]
            or summary["income_liveness_status"] != expected_liveness_status
        ):
            return None
        if type(diagnosis) is dict:
            reason_rows = diagnosis["top_income_block_reasons"]
            if (
                diagnosis["upstream_executable_count"]
                != summary["upstream_executable_count"]
                or diagnosis["income_pass_count"] != summary["income_pass_count"]
                or diagnosis["income_ready_count"] != summary["income_ready_count"]
                or len({row["reason"] for row in reason_rows}) != len(reason_rows)
                or sum(row["count"] for row in reason_rows)
                > summary["income_block_count"]
            ):
                return None
        projected_summary = _safe_json_value(summary)
        if type(projected_summary) is not dict:
            return None
        projected_summary["execution_calibration"] = calibration
        projected_summary["income_liveness_diagnosis"] = diagnosis

        safe = {}
        for key, field_value in value.items():
            if key in {"scan_summary", "items", "excluded"}:
                continue
            projected = _safe_json_value(field_value)
            if projected is cache_invalid:
                return None
            safe[key] = projected
        safe.update({
            "scan_summary": projected_summary,
            "items": items,
            "excluded": excluded,
        })
        return safe

    def _fetch():
        from core.discovery_candidates import (
            build_discovery_sections,
            toss_eligible_new_candidates,
            _fallback_universe_candidates,
        )
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy

        # 정책의 max_order_krw=None/0은 명시적인 "고정 한도 없음"이다.
        # 후보 API에서 int(None) 예외를 50만원으로 되살리지 않는다. 고정 한도가
        # 없을 때 1주 적격성은 실제 원화 예수금으로 확인하고, 최종 수량은 아래의
        # 계좌 위험/집중도 sizing에서 별도로 제한한다.
        try:
            account_for_cash_gate = toss_account_summary() or {}
        except Exception:
            account_for_cash_gate = {}
        snapshot_status = str(account_for_cash_gate.get("snapshot_status") or "")
        snapshot_blocks_candidates = False
        if _dashboard_toss_broker_reads_isolated():
            # Read freshness from the canonical file again instead of trusting
            # dashboard's short-lived account-summary cache. Missing, corrupt,
            # expired, or stale snapshots all fail closed for candidate use.
            try:
                from core.toss_readonly_snapshot import load_snapshot
                snapshot_state = load_snapshot()
            except Exception as exc:
                snapshot_state = {
                    "ok": False,
                    "status": "invalid",
                    "reason": f"snapshot_load_failed:{type(exc).__name__}",
                }
            snapshot_status = str(snapshot_state.get("status") or "invalid")
            snapshot_blocks_candidates = not (
                snapshot_status == "fresh"
                and snapshot_state.get("ok") is True
                and snapshot_state.get("usable_for_decisions") is True
            )
        elif snapshot_status:
            snapshot_blocks_candidates = not (
                snapshot_status == "fresh"
                and account_for_cash_gate.get("snapshot_usable_for_decisions") is True
            )
        if snapshot_blocks_candidates:
            # Do not let stale totals, cash, holdings, or FX participate in
            # sizing/income/rebalance calculations. Items are hard-blocked below.
            account_for_cash_gate = {}
        cash_for_candidate = {} if snapshot_blocks_candidates else (account_for_cash_gate.get("cash") or {})
        try:
            available_native_krw = int(float(
                cash_for_candidate.get("krw_native", cash_for_candidate.get("krw")) or 0
            ))
        except (TypeError, ValueError):
            available_native_krw = 0

        try:
            raw_max_order_krw = compute_toss_live_pilot_policy().get("max_order_krw")
            if raw_max_order_krw in (None, "", 0, "0"):
                max_order_krw = None
            else:
                max_order_krw = max(0, int(raw_max_order_krw))
        except Exception:
            # 정책 자체를 읽지 못한 경우만 기존 fail-safe 50만원을 유지한다.
            max_order_krw = 500_000
        candidate_affordability_limit_krw = (
            max_order_krw if max_order_krw is not None else available_native_krw
        )

        # AI Berkshire 판정 1회 로드 → 후보별 BUY 게이트에서 재사용.
        # 파일 누락/파손은 빈 dict → 전 후보 needs_research 진단 (하드 차단 없음).
        from core.ai_berkshire_toss import load_ai_berkshire_scores
        try:
            berkshire_scores = load_ai_berkshire_scores() or {}
        except Exception as e:
            log.warning("ai_berkshire scores unavailable for buy gate: %s", e)
            berkshire_scores = {}

        # 대시보드 GET은 응답성이 우선이다. 전체 scanner/discover 경로는
        # cold start에서 30~60초 걸릴 수 있으므로, 토스 후보 API는 병렬 경량
        # 유니버스 quote를 주 소스로 사용한다. 주문 전송 경로의 최종 gate는
        # 별도로 유지된다.
        scan_candidates = _fallback_universe_candidates(markets)
        sections = build_discovery_sections(scan_candidates=scan_candidates, briefing_type=briefing_type)
        result = toss_eligible_new_candidates(
            sections,
            max_order_krw=candidate_affordability_limit_krw,
        )
        from core.toss_income_strategy import (
            detect_explicit_toss_input_error,
            quarantine_explicit_toss_input,
        )
        for raw_item in result.get("items") or []:
            raw_error = detect_explicit_toss_input_error(raw_item)
            if raw_error:
                quarantine_explicit_toss_input(raw_item, raw_error)

        # 승호 명시 제외: 크래프톤은 토스/신규 매수 후보에서 노출하지 않는다.
        # config/settings.py 원본 유니버스는 건드리지 않고, 주문/후보 API 직전에서
        # fail-closed로 제거해 자동 finalizer까지 도달하지 못하게 한다.
        user_blocked_buy_symbols = {"259960.KS"}
        try:
            toss_held_map = _toss_holding_price_map()
        except Exception:
            toss_held_map = {}
        try:
            recent_risk_sells = _recent_toss_risk_sell_symbols()
        except Exception:
            recent_risk_sells = {}
        try:
            pending_order_symbols = _pending_toss_order_symbols()
        except Exception:
            # '모름'을 '없음'으로 위장하지 않는다 — income gate가 fail-closed 처리
            pending_order_symbols = None
        blocked_items = []
        wrong_market_items = []
        already_held_items = []
        recent_risk_sell_items = []
        kept_items = []
        for item in result.get("items") or []:
            sym = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
            inferred_market = "KR" if sym.endswith((".KS", ".KQ")) or sym.isdigit() else "US"
            item_market = (
                inferred_market
                if item.get("upstream_input_validation_error") == "symbol_market_mismatch"
                else str(item.get("market") or inferred_market).upper()
            )
            held_row = toss_held_map.get(sym)
            if not held_row and sym.endswith((".KS", ".KQ")):
                held_row = toss_held_map.get(sym.split(".", 1)[0])
            recent_risk_sell = recent_risk_sells.get(sym)
            if not recent_risk_sell and sym.endswith((".KS", ".KQ")):
                recent_risk_sell = recent_risk_sells.get(sym.split(".", 1)[0])
            if market_norm != "ALL" and item_market != market_norm:
                wrong_market_items.append(item)
                continue
            if sym in user_blocked_buy_symbols:
                blocked_items.append(item)
                continue
            if held_row:
                held_item = dict(item)
                held_item["_held_row"] = held_row
                already_held_items.append(held_item)
                continue
            if recent_risk_sell:
                risk_item = dict(item)
                risk_item["_risk_sell_row"] = recent_risk_sell
                recent_risk_sell_items.append(risk_item)
                continue
            kept_items.append(item)
        if blocked_items or wrong_market_items or already_held_items or recent_risk_sell_items:
            result["items"] = kept_items
            excluded = list(result.get("excluded") or [])
            for item in wrong_market_items:
                excluded.append({
                    "ticker": item.get("symbol") or item.get("ticker"),
                    "symbol": item.get("symbol") or item.get("ticker"),
                    "name": item.get("name") or item.get("symbol") or item.get("ticker"),
                    "reason": f"요청 시장({market_norm})과 다른 후보 제외",
                    "scope": "market_scope_excluded",
                })
            for item in already_held_items:
                held_row = item.get("_held_row") or {}
                excluded.append({
                    "ticker": item.get("symbol") or item.get("ticker"),
                    "symbol": item.get("symbol") or item.get("ticker"),
                    "name": item.get("name") or held_row.get("name") or item.get("symbol") or item.get("ticker"),
                    "reason": "이미 Toss 보유 중 — 신규 매수 후보 제외, 보유/매도 관리 루프로 판단",
                    "scope": "already_held_toss_position",
                    "quantity": held_row.get("quantity"),
                    "last_price": held_row.get("last_price"),
                })
            for item in recent_risk_sell_items:
                risk_row = item.get("_risk_sell_row") or {}
                excluded.append({
                    "ticker": item.get("symbol") or item.get("ticker"),
                    "symbol": item.get("symbol") or item.get("ticker"),
                    "name": item.get("name") or item.get("symbol") or item.get("ticker"),
                    "reason": "최근 리스크 매도 종목 — 재진입 cooldown, 다음 검토 주기까지 신규 매수 제외",
                    "scope": "recent_risk_sell_cooldown",
                    "risk_sell_reason": risk_row.get("reason"),
                    "risk_sell_at": risk_row.get("created_at"),
                    "risk_sell_status": risk_row.get("status"),
                })
            for item in blocked_items:
                excluded.append({
                    "ticker": item.get("symbol") or item.get("ticker"),
                    "symbol": item.get("symbol") or item.get("ticker"),
                    "name": item.get("name") or "크래프톤",
                    "reason": "사용자 제외: 크래프톤은 매수 후보에서 제외",
                    "scope": "user_blocked_buy_symbol",
                })
            result["excluded"] = excluded
            result["count"] = len(kept_items)
            result["excluded_count"] = len(excluded)

        scan_summary = result.setdefault("scan_summary", {})
        if already_held_items:
            scan_summary["toss_held_excluded_count"] = len(already_held_items)
            scan_summary["toss_held_excluded_symbols"] = [
                str(i.get("symbol") or i.get("ticker") or "") for i in already_held_items[:20]
            ]
        else:
            scan_summary.setdefault("toss_held_excluded_count", 0)
        if recent_risk_sell_items:
            scan_summary["recent_risk_sell_excluded_count"] = len(recent_risk_sell_items)
            scan_summary["recent_risk_sell_excluded_symbols"] = [
                str(i.get("symbol") or i.get("ticker") or "") for i in recent_risk_sell_items[:20]
            ]
        else:
            scan_summary.setdefault("recent_risk_sell_excluded_count", 0)
        scan_summary["dependency_fallback_used"] = True
        scan_summary["source"] = "fast_universe_fallback"
        scan_summary["market"] = market_norm
        scan_summary["markets"] = list(markets)
        scan_summary["user_blocked_buy_symbols"] = sorted(user_blocked_buy_symbols)
        if blocked_items:
            scan_summary["user_blocked_count"] = len(blocked_items)

        scan_summary["configured_max_order_krw"] = max_order_krw
        scan_summary["candidate_affordability_limit_krw"] = candidate_affordability_limit_krw

        # 품질 점수는 ready 판정 전에 반드시 채운다. GET 경로는 DB 기록/고비용 조회 없이
        # 후보 객체만 결정론적으로 보강한다. 실패·누락은 아래 ready gate에서 차단된다.
        try:
            from core.toss_quality_gate import score_candidates_batch
            quality_items = result.get("items") or []
            for quality_market in ("KR", "US"):
                market_items = [
                    item for item in quality_items
                    if str(item.get("market") or (
                        "KR" if str(item.get("symbol") or "").endswith((".KS", ".KQ"))
                        else "US"
                    )).upper() == quality_market
                ]
                score_candidates_batch(
                    market_items,
                    market=quality_market,
                    persist_decisions=False,
                    expensive_checks=False,
                )
        except Exception as exc:
            log.warning("Toss candidate quality scoring failed: %s", type(exc).__name__)

        def _enrich_for_stock_agent(item: dict) -> dict:
            """Add complete read-only order-review fields for Hermes stock-agent.

            These fields are display/review metadata only. They do not create, approve,
            or send an order. missing_fields is explicit so the agent can HOLD/BLOCK
            instead of guessing when data is absent.
            """
            out = dict(item)
            validation_error = str(out.get("upstream_input_validation_error") or "")
            if validation_error:
                from core.toss_income_strategy import (
                    compute_income_edge,
                    quarantine_explicit_toss_input,
                )
                quarantine_explicit_toss_input(out, validation_error)
                market = str(out.get("market") or "KR").upper()
                safe_price = 1_000.0 if market == "US" else 10_000.0
                safe_candidate = {
                    "symbol": out.get("symbol") or out.get("ticker"),
                    "market": market,
                    "side": "buy",
                    "quantity": 1,
                    "limit_price": safe_price,
                    "price": safe_price,
                    "target_price": safe_price * 1.02,
                    "stop_loss": safe_price * 0.98,
                    "risk_reward": 2.0,
                    "score": 75,
                    "estimated_amount_krw": (
                        safe_price * 1_400.0 if market == "US" else safe_price
                    ),
                    "fx_usdkrw": 1_400.0,
                    "income_exit_model": "toss_position_review_v2",
                    "upstream_input_validation_error": validation_error,
                }
                out["income_strategy"] = compute_income_edge(
                    safe_candidate,
                    account=account_for_cash_gate,
                    pending_orders=pending_order_symbols,
                    recent_risk_sells=recent_risk_sells,
                )
                out["decision_bucket"] = "BLOCK"
                out["income_execution_contract_valid"] = False
                out["quality_finalized"] = False
                out["quantity_source"] = "blocked_invalid_input"
                return out
            price = out.get("price") or out.get("limit_price") or out.get("entry_price")
            limit_price = out.get("limit_price") or price
            try:
                current_price = float(out.get("current_price") or price or 0)
            except Exception:
                current_price = 0.0
            try:
                limit_f = float(limit_price or 0)
            except Exception:
                limit_f = 0.0
            stop = out.get("stop_loss")
            target = out.get("target_price")

            def _float_or_zero(value) -> float:
                try:
                    return float(value or 0)
                except Exception:
                    return 0.0

            def _sizing_multiplier(score: float, rr: float, stop_risk_pct: float) -> float:
                # 보수적 병목 방식: 확신도/손익비/손절폭 중 가장 약한 축이 수량을 제한한다.
                if score >= 85:
                    score_mult = 1.0
                elif score >= 75:
                    score_mult = 0.75
                elif score >= 65:
                    score_mult = 0.50
                else:
                    score_mult = 0.33

                if rr >= 2.5:
                    rr_mult = 1.0
                elif rr >= 1.8:
                    rr_mult = 0.75
                elif rr >= 1.2:
                    rr_mult = 0.50
                else:
                    rr_mult = 0.33

                if 0 < stop_risk_pct <= 4.0:
                    risk_mult = 1.0
                elif stop_risk_pct <= 6.0:
                    risk_mult = 0.75
                elif stop_risk_pct <= 8.0:
                    risk_mult = 0.50
                else:
                    risk_mult = 0.33
                return min(score_mult, rr_mult, risk_mult)

            score = _float_or_zero(out.get("score"))
            rr = _float_or_zero(out.get("risk_reward"))
            stop_f = _float_or_zero(stop)
            stop_risk_pct = round(max((limit_f - stop_f) / limit_f * 100.0, 0.0), 2) if limit_f and stop_f else None
            market_for_size = str(out.get("market") or "KR").upper()
            asset_type = "KR_STOCK" if market_for_size == "KR" else "US_STOCK"
            currency = "KRW" if asset_type == "KR_STOCK" else "USD"
            out["asset_type"] = asset_type
            out["currency"] = currency
            quantity_source = "provided"
            quantity = int(out.get("quantity") or 0)
            position_budget_krw = None
            sizing_method = "confidence_rr_stop_sizing"
            sizing_details: dict = {}
            if asset_type == "US_STOCK":
                # US limit_price is USD. Never divide a KRW budget by a USD price;
                # keep the upstream 1-share/default USD sizing and preserve KRW conversion.
                quantity = max(1, quantity)
                quantity_source = "provided_usd" if out.get("quantity") else "default_1_us_share"
                estimated_usd = quantity * limit_f if quantity and limit_f else _float_or_zero(out.get("estimated_amount_usd"))
                out["estimated_amount_usd"] = round(estimated_usd, 2) if estimated_usd else out.get("estimated_amount_usd")
                existing_krw = _float_or_zero(out.get("estimated_amount_krw"))
                fx = _float_or_zero(out.get("fx_usdkrw"))
                if not fx and existing_krw and estimated_usd:
                    fx = existing_krw / estimated_usd
                    out["fx_usdkrw"] = round(fx, 4)
                estimated = existing_krw or (estimated_usd * fx if estimated_usd and fx else estimated_usd)
                position_budget_krw = estimated if estimated else None
            else:
                if limit_f and not out.get("limit_exceeded"):
                    multiplier = _sizing_multiplier(score, rr, stop_risk_pct or 99.0)
                    if max_order_krw is None:
                        # 고정 금액 cap이 없을 때는 무제한 수량이 아니라
                        # 계좌 1% 손절 위험 + 단일 포지션 15% + 실제 현금으로 제한한다.
                        total_value = _float_or_zero(
                            (account_for_cash_gate.get("total_account_value") or {}).get("krw")
                        )
                        available_cash = _float_or_zero(
                            (account_for_cash_gate.get("cash") or {}).get(
                                "krw_native",
                                (account_for_cash_gate.get("cash") or {}).get("krw"),
                            )
                        )
                        account_risk_budget = total_value * 0.01
                        concentration_budget = total_value * 0.15
                        per_share_risk = max(limit_f - stop_f, 0.0)
                        risk_qty = int(account_risk_budget // per_share_risk) if per_share_risk > 0 else 0
                        concentration_qty = int(concentration_budget // limit_f) if concentration_budget > 0 else 0
                        cash_qty = int(available_cash // limit_f) if available_cash > 0 else 0
                        base_qty = min(risk_qty, concentration_qty, cash_qty)
                        sizing_method = "account_risk_concentration_sizing"
                        sizing_details = {
                            "account_risk_budget_pct": 1.0,
                            "max_position_pct": 15.0,
                            "account_risk_budget_krw": round(account_risk_budget, 2),
                            "concentration_budget_krw": round(concentration_budget, 2),
                            "available_native_krw": round(available_cash, 2),
                            "risk_quantity_cap": risk_qty,
                            "concentration_quantity_cap": concentration_qty,
                            "cash_quantity_cap": cash_qty,
                        }
                        if base_qty <= 0:
                            quantity = max(1, quantity)
                            out["limit_exceeded"] = True
                            out["execution_status"] = "dynamic_risk_limit"
                            out["executable_now"] = False
                            out["block_reason"] = "1주가 계좌 위험 1%·단일 포지션 15%·가용 현금 한도 중 하나를 초과"
                            position_budget_krw = quantity * limit_f
                            quantity_source = "dynamic_risk_limit_blocked"
                        else:
                            quantity = base_qty
                            if quantity > 1:
                                quantity = max(1, int(quantity * multiplier))
                            position_budget_krw = quantity * limit_f
                            quantity_source = "account_risk_concentration_sizing"
                    else:
                        position_budget_krw = max(limit_f, max_order_krw * multiplier)
                        position_budget_krw = min(position_budget_krw, max_order_krw)
                        quantity = max(1, int(position_budget_krw // limit_f))
                        quantity_source = "confidence_rr_stop_sizing"
                elif limit_f:
                    quantity = max(1, quantity)
                estimated = quantity * limit_f if quantity and limit_f else _float_or_zero(out.get("estimated_amount_krw"))

            out.setdefault("account", "토스 AI")
            out.setdefault("account_type", "토스 AI")
            out.setdefault("order_type", "LIMIT")
            out.setdefault("entry_price", limit_f or None)
            out.setdefault("current_price", current_price or None)
            out.setdefault("current_price_source", "discovery_candidates.price")
            out.setdefault("current_price_age_sec", None)
            out["quantity"] = quantity
            out["estimated_amount_krw"] = round(estimated, 2) if estimated else None
            out["quantity_source"] = quantity_source
            out["position_budget_krw"] = round(position_budget_krw, 2) if position_budget_krw else None

            cash_info = account_for_cash_gate.get("cash") or {}
            cash_check = {"checked": bool(cash_info), "asset_type": asset_type}
            if str(out.get("side") or "buy").lower() == "buy" and cash_info:
                if asset_type == "KR_STOCK":
                    native_present = "krw_native" in cash_info
                    available = _float_or_zero(cash_info.get("krw_native") if native_present else cash_info.get("krw"))
                    required = _float_or_zero(out.get("estimated_amount_krw"))
                    cash_check.update({
                        "currency": "KRW",
                        "available": available,
                        "required": required,
                        "native_cash_used": native_present,
                    })
                    if native_present and required and available < required:
                        out["execution_status"] = "cash_unavailable"
                        out["executable_now"] = False
                        out["block_reason"] = f"KRW 예수금 부족: 필요 {required:,.0f}원 > 가용 {available:,.0f}원"
                else:
                    available = _float_or_zero(cash_info.get("usd"))
                    required = _float_or_zero(out.get("estimated_amount_usd")) or (limit_f * quantity if limit_f and quantity else 0.0)
                    cash_check.update({
                        "currency": "USD",
                        "available": available,
                        "required": required,
                    })
                    if required and available < required:
                        out["execution_status"] = "cash_unavailable"
                        out["executable_now"] = False
                        out["block_reason"] = f"USD 예수금 부족: 필요 ${required:,.2f} > 가용 ${available:,.2f}"
            out["cash_check"] = cash_check

            out["position_sizing"] = {
                "method": sizing_method,
                "max_order_krw": max_order_krw,
                "score": score or None,
                "risk_reward": rr or None,
                "stop_risk_pct": stop_risk_pct,
                "note": (
                    "계좌 손절위험 1%·단일 포지션 15%·가용 현금으로 수량 제한"
                    if sizing_method == "account_risk_concentration_sizing"
                    else "확신도·손익비·손절폭 중 가장 약한 축으로 수량 제한"
                ),
                **sizing_details,
            }

            try:
                from core.toss_income_strategy import prepare_income_buy_plan
                out = prepare_income_buy_plan(out)
            except Exception as e:
                out.setdefault("income_exit_plan_error", str(e)[:180])

            out.setdefault("condition", "지정가 이하에서만 검토 · Hermes PASS 후 결정론 안전 게이트")
            out.setdefault("execution_gate", "Hermes PASS + deterministic safety gates")
            out.setdefault("broker_execution", "Toss AI autonomous live pilot")
            out.setdefault("read_only_notice", "GET-only 후보 표시 · 이 응답은 주문 생성/승인/전송을 하지 않음")

            buy_limit_above_current = False
            if current_price and limit_f:
                gap = round((limit_f / current_price - 1.0) * 100.0, 2)
                out.setdefault("current_vs_limit_gap_pct", gap)
                side = str(out.get("side") or "").lower()
                buy_limit_above_current = side == "buy" and limit_f > current_price
                if buy_limit_above_current:
                    fill_note = "지정가가 현재가보다 높음 — 자동매수 차단(추격/즉시체결 위험)"
                    out["execution_status"] = "chase_block"
                    out["executable_now"] = False
                    out["block_reason"] = (
                        f"매수 지정가 {limit_f:,.0f}원 > 현재가 {current_price:,.0f}원"
                    )
                elif gap >= -0.3:
                    fill_note = "현재가 근접 — 즉시체결 가능성 있음"
                else:
                    fill_note = "현재가보다 낮은 지정가 — 미체결 가능성 있음"
                out.setdefault("fill_risk_note", fill_note)
            else:
                out.setdefault("current_vs_limit_gap_pct", None)
                out.setdefault("fill_risk_note", "현재가 또는 지정가 부족 — 체결 가능성 판단 불가")

            # This endpoint deliberately builds candidates from
            # `_fallback_universe_candidates`, whose price is already sourced from
            # KIS. Calling KIS again for every item only compares KIS with itself and
            # made a cold GET take ~55 seconds. Preserve honest provenance instead;
            # independent Toss↔KIS checks remain in the holdings/cross-check and
            # final execution gates.
            data_quality = {
                "schema": "toss_kis_price_cross_check.v1",
                "symbol": str(out.get("symbol") or "").upper().strip(),
                "quality": "unknown",
                "action_hint": "SAME_SOURCE_ONLY",
                "has_toss_holding_price": False,
                "has_kis_price": bool(out.get("current_price")),
                "same_source_only": True,
                "checks": [{
                    "name": "후보 현재가 원본",
                    "tone": "unknown",
                    "source_a": "candidate.current_price",
                    "source_b": "KIS fallback quote",
                    "reason": "same_source_recheck_skipped",
                }],
            }
            out["data_quality"] = data_quality
            if data_quality.get("quality") == "low":
                out["execution_status"] = "data_quality_block"
                out["executable_now"] = False
                out["block_reason"] = "Toss/KIS 가격 교차검증 큰 괴리"
            elif data_quality.get("quality") == "medium":
                out["data_quality_note"] = "Toss/KIS 가격 괴리 주의 — 소액/수량 축소 우선"

            risk_notes = list(out.get("risk_notes") or [])
            if stop:
                risk_notes.append(f"손절 기준 {stop:,.0f}원" if isinstance(stop, (int, float)) else f"손절 기준 {stop}")
            if target:
                risk_notes.append(f"목표 기준 {target:,.0f}원" if isinstance(target, (int, float)) else f"목표 기준 {target}")
            if out.get("limit_exceeded"):
                risk_notes.append(str(out.get("block_reason") or "1회 주문 한도 초과"))
            if buy_limit_above_current:
                risk_notes.append(str(out.get("block_reason") or "매수 지정가가 현재가보다 높음"))
            if out.get("blocking_risk_flags"):
                risk_notes.extend(str(f) for f in out.get("blocking_risk_flags") or [])
            if out.get("observation_flags"):
                risk_notes.extend(str(f) for f in out.get("observation_flags") or [])
            out["risk_notes"] = risk_notes

            # income plan이 stop/target을 조정한 최종 가격으로 RR·total·bucket을
            # 같은 scorer weight profile에서 다시 계산하고 실행 snapshot을 결합한다.
            try:
                from core.toss_quality_gate import finalize_quality_proof
                quality_finalized = finalize_quality_proof(out)
            except Exception as exc:
                quality_finalized = False
                log.debug("quality proof finalization failed: %s", type(exc).__name__)
            out["quality_finalized"] = quality_finalized
            if not quality_finalized:
                out["decision_bucket"] = "BLOCK"
                out["decision_reason"] = "quality_finalization_failed"
                out["executable_now"] = False
                out["execution_status"] = "quality_finalization_failed"
                if not out.get("block_reason"):
                    out["block_reason"] = "최종 품질 증명 생성 실패"

            # EV 입력인 decision_bucket/RR/score는 quality proof 최종화 뒤에만
            # 확정된다. 사전 bucket으로 계산한 PASS를 재사용하지 않는다.
            try:
                from core.toss_income_strategy import compute_income_edge
                income_strategy = compute_income_edge(
                    out,
                    account=account_for_cash_gate,
                    pending_orders=pending_order_symbols,
                    recent_risk_sells=recent_risk_sells,
                    exit_model="toss_position_review_v2",
                )
            except Exception as e:
                income_strategy = {
                    "version": "income_v2_dual_ev",
                    "income_pass": False,
                    "income_grade": "BLOCK",
                    "income_block_reason": "income_gate_error",
                    "income_block_label": f"수입 게이트 계산 실패: {e}"[:180],
                }
            income_strategy.update({
                "planned_entry_price": out.get("limit_price"),
                "planned_stop_loss": out.get("stop_loss"),
                "planned_target_price": out.get("target_price"),
                "planned_quantity": out.get("quantity"),
            })
            out["income_strategy"] = income_strategy
            if income_strategy.get("income_pass") is not True:
                income_note = "수입 기대값 gate 차단: " + str(
                    income_strategy.get("income_block_label")
                    or income_strategy.get("income_block_reason")
                    or "income_pass=false"
                )
                if not out.get("block_reason"):
                    out["block_reason"] = income_note
                out.setdefault("risk_notes", []).append(income_note)

            missing = []
            required = {
                "account": out.get("account") or out.get("account_type"),
                "symbol": out.get("symbol"),
                "name": out.get("name"),
                "side": out.get("side"),
                "current_price": out.get("current_price"),
                "limit_price": out.get("limit_price"),
                "quantity": out.get("quantity"),
                "estimated_amount_krw": out.get("estimated_amount_krw"),
                "stop_loss": out.get("stop_loss"),
                "condition": out.get("condition"),
            }
            for key, value in required.items():
                if value in (None, "", 0, 0.0, []):
                    missing.append(key)
            out["missing_fields"] = missing
            hard_blocked = out.get("execution_status") in {
                "hold_risk_flags", "chase_block", "data_quality_block",
                "cash_unavailable", "quality_finalization_failed",
            } or bool(out.get("blocking_risk_flags"))
            from core.toss_income_strategy import validate_executable_income_contract
            income_pass, income_contract_reason = validate_executable_income_contract(
                out.get("income_strategy")
            )
            out["income_execution_contract_valid"] = income_pass
            if not income_pass and (out.get("income_strategy") or {}).get("income_pass") is True:
                out["execution_status"] = "income_contract_blocked"
                out["executable_now"] = False
                if not out.get("block_reason"):
                    out["block_reason"] = income_contract_reason
                hard_blocked = True

            # 품질 게이트 + 수입 기대값 gate decision 반영
            bucket = str(out.get("decision_bucket") or "")
            _exec_buckets = ("PASS_EXECUTE", "SMALL_PASS")
            out["stock_agent_ready"] = (
                bucket in _exec_buckets
                and income_pass
                and not missing
                and not out.get("limit_exceeded")
                and not hard_blocked
            )
            if bucket not in _exec_buckets:
                out["executable_now"] = False
                if not hard_blocked:
                    out["execution_status"] = (
                        "quality_gate_blocked" if bucket else "quality_gate_missing"
                    )
                if not out.get("block_reason"):
                    out["block_reason"] = (
                        str(out.get("decision_reason") or bucket)
                        if bucket else "quality_gate_decision_missing"
                    )

            _apply_ai_berkshire_buy_gate(out, berkshire_scores)
            return out

        items = [
            _enrich_for_stock_agent(i)
            for i in result["items"][:_TOSS_BUY_CANDIDATE_CACHE_LIMIT]
        ]

        # A stale snapshot may be shown for reference, but must never size or
        # ready a candidate. The broker-owner pipeline re-fetches live account
        # state; consumer processes fail closed here.
        snapshot_block_count = 0
        if snapshot_blocks_candidates:
            for item in items:
                item["stock_agent_ready"] = False
                item["executable_now"] = False
                item["execution_status"] = "toss_snapshot_stale"
                item["block_reason"] = "Toss 계좌 snapshot 만료/비정상 — 신규 BUY 판단 차단"
                item.setdefault("risk_notes", []).append(item["block_reason"])
                snapshot_block_count += 1
            scan_summary["snapshot_candidate_blocked"] = True
            scan_summary["snapshot_status"] = snapshot_status
            scan_summary["snapshot_block_count"] = snapshot_block_count
        else:
            scan_summary["snapshot_candidate_blocked"] = False
            scan_summary["snapshot_block_count"] = 0

        def _income_expected(item: dict) -> float:
            try:
                income = item.get("income_strategy") or {}
                value = income.get("decision_expected_pnl_krw")
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return 0.0
                number = float(value)
                return number if number > 0 and number == number and number != float("inf") else 0.0
            except Exception:
                return 0.0

        # 포트폴리오 상태를 신규 BUY gate에 연결한다.
        # 보유가 이미 많으면 후보 품질이 좋아도 무조건 추가 매수하지 않는다.
        try:
            holdings_count_for_cap = int(account_for_cash_gate.get("holdings_count") or 0)
        except Exception:
            holdings_count_for_cap = 0
        ready_items = [i for i in items if i.get("stock_agent_ready") is True]
        portfolio_cap_block_count = 0
        if holdings_count_for_cap > 20:
            for item in ready_items:
                item["stock_agent_ready"] = False
                item["executable_now"] = False
                item["execution_status"] = "portfolio_rebalance_required"
                item["block_reason"] = "보유 20개 초과 — 신규 BUY 전 보유 리밸런싱/매도 루프 필요"
                item.setdefault("risk_notes", []).append(item["block_reason"])
                portfolio_cap_block_count += 1
            scan_summary["portfolio_rebalance_required"] = True
            scan_summary["portfolio_income_ready_cap"] = 0
        elif holdings_count_for_cap > 12 and len(ready_items) > 3:
            ranked_ready = sorted(ready_items, key=_income_expected, reverse=True)
            allowed_ids = {id(i) for i in ranked_ready[:3]}
            for item in ready_items:
                if id(item) in allowed_ids:
                    continue
                item["stock_agent_ready"] = False
                item["executable_now"] = False
                item["execution_status"] = "portfolio_income_cap"
                item["block_reason"] = "보유 12개 초과 — income expected 상위 3개만 신규 BUY 허용"
                item.setdefault("risk_notes", []).append(item["block_reason"])
                portfolio_cap_block_count += 1
            scan_summary["portfolio_rebalance_required"] = False
            scan_summary["portfolio_income_ready_cap"] = 3
        else:
            scan_summary["portfolio_rebalance_required"] = False
            scan_summary["portfolio_income_ready_cap"] = None

        scan_summary["holdings_count_for_income_gate"] = holdings_count_for_cap
        scan_summary["portfolio_cap_block_count"] = portfolio_cap_block_count
        if holdings_count_for_cap > 20:
            for item in items:
                if (item.get("income_strategy") or {}).get("income_pass"):
                    item["rebalance_required"] = True
                    item.setdefault("risk_notes", []).append("보유 20개 초과 — 신규 BUY 전 리밸런싱 계획 확인")
        try:
            from core.toss_income_strategy import build_rebalance_plan
            scan_summary["rebalance_plan"] = build_rebalance_plan(account_for_cash_gate, items)
        except Exception as e:
            scan_summary["rebalance_plan_error"] = str(e)[:180]
        income_gate_eligible_items = [
            item for item in items if _income_gate_eligible(item)
        ]
        raw_income_pass_count = sum(
            1 for item in items if _item_income_pass(item)
        )
        income_pass_count = sum(
            1 for item in income_gate_eligible_items if _item_income_pass(item)
        )
        income_block_count = len(income_gate_eligible_items) - income_pass_count
        upstream_executable_count = len(income_gate_eligible_items)
        income_ready_count = sum(
            1 for item in items if item.get("stock_agent_ready") is True
        )
        income_block_reasons: dict[str, int] = {}
        for item in income_gate_eligible_items:
            reason = str(
                (item.get("income_strategy") or {}).get("income_block_reason") or ""
            ).strip()
            if reason:
                income_block_reasons[reason] = income_block_reasons.get(reason, 0) + 1
        top_income_block_reasons = [
            {"reason": reason, "count": count}
            for reason, count in sorted(
                income_block_reasons.items(), key=lambda row: (-row[1], row[0])
            )[:5]
        ]
        if income_ready_count > 0:
            income_liveness_status = "healthy"
            income_liveness_diagnosis = None
        elif upstream_executable_count > 0 and income_pass_count == 0:
            income_liveness_status = "degraded"
            income_liveness_diagnosis = {
                "reason": "upstream_executable_but_no_income_ready",
                "upstream_executable_count": upstream_executable_count,
                "income_pass_count": 0,
                "income_ready_count": 0,
                "top_income_block_reasons": top_income_block_reasons,
            }
        elif income_pass_count > 0:
            income_liveness_status = "downstream_blocked"
            income_liveness_diagnosis = {
                "reason": "income_pass_but_no_final_ready",
                "upstream_executable_count": upstream_executable_count,
                "income_pass_count": income_pass_count,
                "income_ready_count": 0,
                "top_income_block_reasons": top_income_block_reasons,
            }
        elif items:
            income_liveness_status = "no_signal"
            income_liveness_diagnosis = {
                "reason": "no_income_gate_eligible_candidates",
                "upstream_executable_count": 0,
                "income_pass_count": 0,
                "income_ready_count": 0,
                "top_income_block_reasons": top_income_block_reasons,
            }
        else:
            income_liveness_status = "idle"
            income_liveness_diagnosis = None
        scan_summary["raw_income_pass_count"] = raw_income_pass_count
        scan_summary["income_pass_count"] = income_pass_count
        scan_summary["income_block_count"] = income_block_count
        scan_summary["income_gate_eligible_count"] = upstream_executable_count
        scan_summary["upstream_executable_count"] = upstream_executable_count
        scan_summary["income_ready_count"] = income_ready_count
        scan_summary["income_liveness_status"] = income_liveness_status
        scan_summary["income_liveness_diagnosis"] = income_liveness_diagnosis
        scan_summary["income_liveness_version"] = "income_liveness_v1"
        try:
            from src.toss_execution_calibration import (
                load_execution_calibration,
                reconcile_calibration_with_holdings,
            )

            raw_calibration = load_execution_calibration()
            holdings_for_reconciliation = account_for_cash_gate.get("holdings_items")
            if (
                snapshot_status == "fresh"
                and account_for_cash_gate.get("snapshot_usable_for_decisions") is True
                and type(holdings_for_reconciliation) is list
            ):
                raw_calibration = reconcile_calibration_with_holdings(
                    raw_calibration,
                    holdings_for_reconciliation,
                )
            else:
                raw_calibration = dict(raw_calibration)
                raw_lineage_reasons = raw_calibration.get("lineage_reasons")
                lineage_reasons = (
                    list(raw_lineage_reasons)
                    if type(raw_lineage_reasons) is list else []
                )
                if "holdings_reconciliation_unavailable" not in lineage_reasons:
                    lineage_reasons.append("holdings_reconciliation_unavailable")
                raw_calibration.update({
                    "status": (
                        "unavailable"
                        if raw_calibration.get("status") == "unavailable"
                        else "partial"
                    ),
                    "holdings_reconciliation_status": "unavailable",
                    "holdings_symbol_alias_conflict_count": 0,
                    "open_quantity_exceeds_holdings": 0,
                    "lineage_status": "incomplete",
                    "lineage_reasons": lineage_reasons,
                    "evidence_sufficient": False,
                })
            fresh_projection = {
                key: value
                for key, value in raw_calibration.items()
                if key in calibration_fields
            }
            fresh_projection.update({
                "schema": "toss_execution_calibration.v1",
                "mode": "observability_only",
                "decision_usable": False,
                "decision_block_reason": "lifecycle_transition_model_unvalidated",
                "attribution_verified": False,
            })
            execution_calibration = _safe_execution_calibration(fresh_projection)
            if execution_calibration is None:
                raise ValueError("execution_calibration_contract_invalid")
        except Exception as exc:
            execution_calibration = _unavailable_execution_calibration(
                "execution_calibration_load_failed",
                type(exc).__name__,
            )
        scan_summary["execution_calibration"] = execution_calibration
        scan_summary["income_gate_version"] = "income_v2_dual_ev"
        scan_summary["ai_berkshire_gate_version"] = _AI_BERKSHIRE_BUY_GATE_VERSION
        scan_summary["ai_berkshire_buy_block_count"] = sum(
            1 for i in items if i.get("ai_berkshire_buy_block"))
        scan_summary["ai_berkshire_needs_research_count"] = sum(
            1 for i in items if i.get("ai_berkshire_research_status") != "ok")
        return {
            "items": items,
            "excluded": result["excluded"][:_TOSS_BUY_CANDIDATE_CACHE_LIMIT],
            "count": result["count"],
            "excluded_count": result["excluded_count"],
            "scan_summary": result.get("scan_summary", {}),
            "range": range_,
            "max_order_krw": max_order_krw,
            "schema": "toss_buy_candidates.v3.dual_income_ev",
            "note": result["note"],
        }

    try:
        cached = _cached(f"toss_buy_candidates:{range_}:{market_norm}", 120, _fetch)
    except Exception as e:
        log.warning("toss buy candidates cache failed: error_type=%s", type(e).__name__)
        cached = None
    try:
        safe_cached = _safe_candidate_cache(cached)
    except Exception as exc:
        log.warning(
            "toss buy candidates cache contract rejected: error_type=%s",
            type(exc).__name__,
        )
        safe_cached = None
    cache_valid = safe_cached is not None
    if safe_cached is None:
        safe_cached = {
            "schema": "toss_buy_candidates.v3.dual_income_ev",
            "scan_summary": {"income_gate_version": "income_v2_dual_ev"},
            "items": [],
            "excluded": [],
            "count": 0,
            "excluded_count": 0,
            "range": range_,
            "note": "cache_payload_invalid_fail_closed",
        }
    requested_limit = max(0, int(limit))
    out = dict(safe_cached)
    out["scan_summary"] = dict(safe_cached.get("scan_summary") or {})
    out["items"] = list(safe_cached.get("items") or [])
    out["excluded"] = list(safe_cached.get("excluded") or [])
    out["schema"] = "toss_buy_candidates.v3.dual_income_ev"
    scan_summary = out.get("scan_summary")
    if not isinstance(scan_summary, dict):
        scan_summary = {}
        out["scan_summary"] = scan_summary
    scan_summary["income_gate_version"] = "income_v2_dual_ev"
    scan_summary["income_liveness_version"] = "income_liveness_v1"
    scan_summary["cache_contract_valid"] = cache_valid
    if not cache_valid:
        scan_summary.update({
            "raw_income_pass_count": 0,
            "income_pass_count": 0,
            "income_block_count": 0,
            "income_gate_eligible_count": 0,
            "upstream_executable_count": 0,
            "income_ready_count": 0,
            "income_liveness_status": "unavailable",
            "income_liveness_diagnosis": {
                "reason": "candidate_cache_payload_unavailable",
                "upstream_executable_count": 0,
                "income_pass_count": 0,
                "income_ready_count": 0,
                "top_income_block_reasons": [],
            },
            "execution_calibration": _unavailable_execution_calibration(
                "candidate_cache_payload_unavailable"
            ),
        })
    out["items"] = list(out.get("items") or [])[:requested_limit]
    out["excluded"] = list(out.get("excluded") or [])[:requested_limit]
    scan_summary["returned_candidate_count"] = len(out["items"])
    scan_summary["returned_income_ready_count"] = sum(
        1 for item in out["items"] if item.get("stock_agent_ready") is True
    )
    out["requested_limit"] = requested_limit
    return out


# ─── /api/toss/ai-berkshire-research-queue — 재리서치 대상 (GET-only) ──────
_RESEARCH_QUEUE_VERSION = "ai_berkshire_research_queue_v1"
_RESEARCH_EXPIRY_WINDOW_DAYS = 30

# 한 심볼에 사유가 여럿이면 급한 쪽을 남긴다 (숫자가 작을수록 우선).
_RESEARCH_REASON_PRIORITY = {
    "expired": 0,
    "expiring_within_30d": 1,
    "legacy_checklist_missing": 2,
    "invalid": 3,
    "unscored": 4,
}
_RESEARCH_STRICT_COVERAGE_FIELDS = (
    "research_status",
    "proposed_classification",
    "classification_change_reason",
    "evidence_urls",
    "checked_at",
    "buy_checklist_status",
    "auto_sell_eligible",
)
_RESEARCH_STATUS_TO_REASON = {
    "needs_research": "unscored",
    "expired": "expired",
    "invalid": "invalid",
}


def _research_queue_key(symbol) -> str:
    """중복 merge용 정규화 키 — 6자리 코드는 .KS/.KQ 접미사를 제거한다."""
    sym = str(symbol or "").upper().strip()
    if sym.endswith((".KS", ".KQ")) and sym.split(".", 1)[0].isdigit():
        return sym.split(".", 1)[0]
    return sym


def ai_berkshire_research_queue_data(limit: int = 100, as_of_date=None) -> dict:
    """AI Berkshire 재리서치 큐 (read-only).

    Hermes가 자동 리서치할 대상을 한 화면에 모은다. 대상은
      - 현재 Toss holdings 중 unscored/expired/invalid
      - 현재 buy candidates 중 unscored/expired/invalid
      - score 파일 중 valid_until이 30일 이내로 임박한 종목
    이며 심볼 기준으로 merge한다. 계좌번호/토큰/주문 식별자/브로커 원본 응답은
    노출하지 않는다. 주문 생성/승인/전송/DB 기록 부작용이 없다.
    """
    def _fetch():
        from core.ai_berkshire_toss import (
            evaluate_ai_berkshire_buy_gate,
            load_ai_berkshire_scores,
            normalize_ai_berkshire_item,
        )

        try:
            scores = load_ai_berkshire_scores() or {}
        except Exception as e:
            log.warning("research queue scores load failed: %s", e)
            scores = {}
        score_items = scores.get("items") if isinstance(scores, dict) else None
        score_items = score_items if isinstance(score_items, dict) else {}

        today = _coerce_research_date(as_of_date)
        entries: dict[str, dict] = {}

        def _touch(symbol: str, name: str, source: str) -> dict:
            key = _research_queue_key(symbol)
            entry = entries.setdefault(key, {
                "symbol": str(symbol or "").upper().strip(),
                "name": "", "sources": [], "reasons": set(),
                "stored_classification": None, "classification": None,
                "as_of": None, "valid_until": None, "freshness_issues": [],
                "coverage_gaps": [],
            })
            # 표시 심볼은 접미사가 붙은 완전형을 선호한다 (005930 < 005930.KS).
            sym = str(symbol or "").upper().strip()
            if len(sym) > len(entry["symbol"]):
                entry["symbol"] = sym
            if name and not entry["name"]:
                entry["name"] = str(name)
            if source not in entry["sources"]:
                entry["sources"].append(source)
            return entry

        def _absorb_gate(entry: dict, gate: dict) -> None:
            entry["stored_classification"] = gate["stored_classification"]
            entry["classification"] = gate["classification"]
            entry["as_of"] = gate["as_of"]
            entry["valid_until"] = gate["valid_until"]
            entry["freshness_issues"] = list(gate["freshness_issues"])
            if not entry["name"] and gate.get("name"):
                entry["name"] = gate["name"]

        def _scan(rows, source: str) -> None:
            for row in rows or []:
                symbol = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
                if not symbol:
                    continue
                entry = _touch(symbol, row.get("name") or "", source)
                gate = evaluate_ai_berkshire_buy_gate(symbol, scores=scores,
                                                      as_of_date=today)
                _absorb_gate(entry, gate)
                reason = _RESEARCH_STATUS_TO_REASON.get(gate["research_status"])
                if reason:
                    entry["reasons"].add(reason)

        # 1) 현재 Toss holdings
        try:
            holdings = (toss_account_summary() or {}).get("holdings_items") or []
        except Exception as e:
            log.warning("research queue holdings unavailable: %s", e)
            holdings = []
        _scan(holdings, "holding")

        # 2) 현재 buy candidates (기존 read-only 캐시 함수 재사용)
        try:
            candidates = (toss_buy_candidates_data(limit=limit, market="ALL")
                          or {}).get("items") or []
        except Exception as e:
            log.warning("research queue candidates unavailable: %s", e)
            candidates = []
        _scan(candidates, "buy_candidate")

        # 3) score 파일 중 만료 임박 또는 strict checklist migration 누락
        for key, raw in score_items.items():
            try:
                item = normalize_ai_berkshire_item(raw, as_of_date=today)
            except Exception:
                continue
            raw_item = raw if isinstance(raw, dict) else {}
            coverage_gaps = [
                field for field in _RESEARCH_STRICT_COVERAGE_FIELDS
                if field not in raw_item or raw_item.get(field) in (None, "", [])
            ]
            valid_until = _parse_iso_date(item.get("valid_until"))
            days_left = (valid_until - today).days if valid_until else None
            expiring = days_left is not None and 0 <= days_left <= _RESEARCH_EXPIRY_WINDOW_DAYS
            if not expiring and not coverage_gaps:
                continue
            entry = _touch(key, item.get("name") or "", "score")
            entry["stored_classification"] = item["stored_classification"]
            entry["classification"] = item["classification"]
            entry["as_of"] = item["as_of"]
            entry["valid_until"] = item["valid_until"]
            entry["freshness_issues"] = list(item["freshness_issues"])
            if expiring:
                entry["reasons"].add("expiring_within_30d")
            if coverage_gaps:
                entry["reasons"].add("legacy_checklist_missing")
                entry["coverage_gaps"] = coverage_gaps

        items: list[dict] = []
        for entry in entries.values():
            if not entry["reasons"]:
                continue
            reason = min(entry["reasons"], key=lambda r: _RESEARCH_REASON_PRIORITY[r])
            items.append({
                "symbol": entry["symbol"],
                "name": entry["name"],
                "reason": reason,
                "sources": entry["sources"],
                "stored_classification": entry["stored_classification"],
                "classification": entry["classification"],
                "as_of": entry["as_of"],
                "valid_until": entry["valid_until"],
                "freshness_issues": entry["freshness_issues"],
                "coverage_gaps": entry["coverage_gaps"],
            })
        items.sort(key=lambda i: (_RESEARCH_REASON_PRIORITY[i["reason"]], i["symbol"]))

        counts = {reason: sum(1 for i in items if i["reason"] == reason)
                  for reason in _RESEARCH_REASON_PRIORITY}
        return {
            "version": _RESEARCH_QUEUE_VERSION,
            "read_only": True,
            "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
            "as_of": today.isoformat(),
            "count": len(items),
            "counts": counts,
            "items": items[:limit],
            "note": "GET-only 재리서치 대상 표시 · 주문 생성/승인/전송 없음",
        }

    cache_key = f"ai_berkshire_research_queue:{limit}:{as_of_date or ''}"
    return _cached(cache_key, 120, _fetch)


def _parse_iso_date(value):
    """ISO 날짜 문자열/날짜 → date. 누락/형식 오류는 None."""
    from datetime import date as _date

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    try:
        return _date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _coerce_research_date(value):
    """as_of 문자열/날짜 → date. 값이 없거나 형식이 틀리면 오늘(KST)."""
    return _parse_iso_date(value) or datetime.now(KST).date()


# ─── /api/stock-agent/activity — Hermes Stock-Agent 분석 활동 조회 (GET-only) ──
def stock_agent_activity_data(limit: int = 20) -> dict:
    """Stock-Agent 분석/감시 활동 요약.

    읽기 전용 대시보드 표시용이다. 후보 스캔, Hermes 검증 ledger, live-pilot 이벤트,
    저장된 리뷰 아티팩트가 있으면 한 화면에 모아 보여준다. 주문 생성/승인/전송 없음.
    """
    def _fetch():
        from pathlib import Path

        now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
        out = {
            "schema": "stock_agent_activity.v1.read_only",
            "updated_at": now_str,
            "read_only_notice": "GET-only 분석 활동 표시 · 주문 생성/승인/전송 없음",
            "live_order_allowed": False,
            "summary": {
                "candidate_count": 0,
                "ready_count": 0,
                "missing_count": 0,
                "pending_verifications": 0,
                "recent_events": 0,
                "reviews_saved": False,
            },
            "artifacts": {},
            "activities": [],
            "error": "",
        }

        activities: list[dict] = []

        # 1) 현재 후보 스캔 상태
        try:
            cands = toss_buy_candidates_data(limit=min(limit, 50)) or {}
            items = cands.get("items") or []
            excluded = cands.get("excluded") or []
            scan = cands.get("scan_summary") or {}
            ready = [i for i in items if i.get("stock_agent_ready") is True]
            missing = [i for i in items if i.get("missing_fields")]
            out["summary"].update({
                "candidate_count": len(items),
                "ready_count": len(ready),
                "missing_count": len(missing),
                "universe_count": scan.get("universe_count"),
                "scanned_count": scan.get("scanned_count"),
                "pass_count": scan.get("pass_count"),
                "reject_count": scan.get("reject_count"),
                "excluded_count": cands.get("excluded_count", len(excluded)),
            })
            out["candidates"] = items[:10]
            out["excluded"] = excluded[:10]
            out["scan_summary"] = scan
            activities.append({
                "kind": "scan",
                "title": "신규 후보 스캔",
                "status": f"후보 {len(items)} · PASS-ready {len(ready)} · 정보부족 {len(missing)}",
                "time": now_str,
                "detail": cands.get("note") or "토스 AI 계좌 전용 신규 발굴 후보를 읽기 전용으로 스캔",
            })
            for item in items[:5]:
                name = item.get("name") or item.get("symbol") or item.get("ticker") or "—"
                symbol = item.get("symbol") or item.get("ticker") or ""
                missing_fields = item.get("missing_fields") or []
                status = "PASS-ready" if item.get("stock_agent_ready") is True else ("HOLD · 정보부족" if missing_fields else item.get("execution_status", "검토"))
                gap = item.get("current_vs_limit_gap_pct")
                detail_bits = [item.get("fill_risk_note") or "체결위험 미확인"]
                if gap is not None:
                    detail_bits.append(f"현재가 대비 {gap:+.2f}%")
                if missing_fields:
                    detail_bits.append("부족: " + ", ".join(missing_fields[:4]))
                activities.append({
                    "kind": "candidate",
                    "title": f"{name} ({symbol})" if symbol else name,
                    "status": status,
                    "time": now_str,
                    "detail": " · ".join(detail_bits),
                })
        except Exception as e:
            activities.append({"kind": "scan", "title": "신규 후보 스캔", "status": "오류", "time": now_str, "detail": str(e)})

        # 2) Hermes 검증/이벤트 ledger
        try:
            ver = toss_live_pilot_verifications_data(limit=min(limit, 50)) or {}
            v_records = ver.get("records") or []
            out["summary"]["pending_verifications"] = ver.get("pending_count", 0)
            for r in v_records[:5]:
                symbol = r.get("symbol") or r.get("ticker") or ""
                name = r.get("symbol_name") or r.get("name") or symbol or "검증 요청"
                activities.append({
                    "kind": "verification",
                    "title": f"Hermes 검증 · {name} ({symbol})" if symbol else f"Hermes 검증 · {name}",
                    "status": r.get("status") or r.get("decision") or "검토",
                    "time": r.get("created_at") or r.get("updated_at") or "",
                    "detail": r.get("reason") or r.get("summary") or "PASS/HOLD/BLOCK 검증 기록",
                })
        except Exception as e:
            activities.append({"kind": "verification", "title": "Hermes 검증 ledger", "status": "오류", "time": now_str, "detail": str(e)})

        try:
            ev = toss_live_pilot_events_data(limit=min(limit, 50)) or {}
            e_records = ev.get("records") or []
            broker_orders = ev.get("broker_orders") or []
            out["summary"]["recent_events"] = len(e_records)
            out["summary"]["broker_order_count"] = ev.get("broker_order_count", len(broker_orders))
            out["summary"]["broker_open_count"] = ev.get("broker_open_count", 0)
            out["summary"]["broker_closed_count"] = ev.get("broker_closed_count", 0)
            for r in broker_orders[:5]:
                symbol = r.get("symbol") or r.get("ticker") or ""
                name = r.get("symbol_name") or r.get("name") or symbol or "브로커 주문"
                qty = r.get("filled_quantity") or r.get("quantity") or "-"
                price = r.get("filled_price") or "-"
                activities.append({
                    "kind": "broker_order",
                    "title": f"브로커 체결/주문 · {name} ({symbol})" if symbol else f"브로커 체결/주문 · {name}",
                    "status": r.get("broker_order_status") or r.get("status") or r.get("list_status") or "order",
                    "time": r.get("filled_at") or r.get("ordered_at") or r.get("created_at") or "",
                    "detail": f"{r.get('side') or '-'} · {qty}주 · 체결가 {price} · {r.get('read_only_source')}",
                })
            for r in e_records[:5]:
                symbol = r.get("symbol") or r.get("ticker") or ""
                name = r.get("symbol_name") or r.get("name") or symbol or "이벤트"
                activities.append({
                    "kind": "event",
                    "title": f"Live Pilot 이벤트 · {name} ({symbol})" if symbol else f"Live Pilot 이벤트 · {name}",
                    "status": r.get("event_type") or r.get("status") or "event",
                    "time": r.get("created_at") or r.get("event_time") or "",
                    "detail": r.get("message") or r.get("reason") or r.get("result") or "이벤트 기록",
                })
        except Exception as e:
            activities.append({"kind": "event", "title": "Live Pilot 이벤트", "status": "오류", "time": now_str, "detail": str(e)})

        # 3) Stock-Agent 리뷰 아티팩트 — 있으면 표시, 없으면 configured-but-empty로 표시
        artifact_paths = [
            Path("/root/.hermes/stock-agent/reviews/recent_reviews.md"),
            Path("/home/kanzaka110/.hermes/stock-agent/reviews/recent_reviews.md"),
            Path(".hermes/stock-agent/reviews/recent_reviews.md"),
        ]
        snapshot_paths = [
            Path("/root/.hermes/stock-agent/reviews/latest_snapshot.json"),
            Path("/home/kanzaka110/.hermes/stock-agent/reviews/latest_snapshot.json"),
            Path(".hermes/stock-agent/reviews/latest_snapshot.json"),
        ]
        def _safe_existing(paths):
            for x in paths:
                try:
                    if x.exists():
                        return x
                except OSError:
                    continue
            return None

        review_path = _safe_existing(artifact_paths)
        snapshot_path = _safe_existing(snapshot_paths)
        out["artifacts"] = {
            "recent_reviews_exists": bool(review_path),
            "latest_snapshot_exists": bool(snapshot_path),
            "recent_reviews_path": str(review_path) if review_path else "",
            "latest_snapshot_path": str(snapshot_path) if snapshot_path else "",
        }
        if review_path:
            try:
                body = review_path.read_text(encoding="utf-8", errors="replace")
                excerpt = body[-2000:].strip()
                mtime = datetime.fromtimestamp(review_path.stat().st_mtime, KST).strftime("%Y-%m-%d %H:%M KST")
                out["artifacts"].update({"recent_reviews_mtime": mtime, "recent_reviews_excerpt": excerpt})
                out["summary"]["reviews_saved"] = True
                activities.append({
                    "kind": "review",
                    "title": "LLM 리뷰 아티팩트 저장됨",
                    "status": "recent_reviews.md",
                    "time": mtime,
                    "detail": excerpt.splitlines()[-1] if excerpt else "저장된 리뷰 있음",
                })
            except Exception as e:
                out["artifacts"]["recent_reviews_error"] = str(e)
        else:
            activities.append({
                "kind": "review",
                "title": "LLM 리뷰 아티팩트",
                "status": "아직 없음",
                "time": now_str,
                "detail": "이벤트 발생 후 Stock-Agent 리뷰가 저장되면 recent_reviews.md/latest_snapshot.json 상태가 여기에 표시됨",
            })

        out["activities"] = activities[:limit]
        return out

    return _cached(f"stock_agent_activity:{limit}", 60, _fetch)

def toss_live_pilot_policy_data() -> dict:
    """승인형 live pilot 정책. 실제 주문 없음; dashboard path는 bounded."""
    data = dict(_toss_live_policy_fast(timeout=1.0))
    # transport dry-run schema 상태 표시 (token/account 등 민감정보 미노출)
    transport_configured = data.get("live_transport_status") == "configured"
    live_order_sent_possible = bool(
        data.get("live_order_allowed")
        and data.get("adapter_status") == "enabled"
        and transport_configured
    )
    data["transport"] = {
        "live_transport_status": data.get("live_transport_status", "not_configured"),
        "dry_run_schema_ready": True,
        "order_endpoint_confirmed": True,
        "order_endpoint": "POST /api/v1/orders",
        # read-only 표시값: env gate + adapter + transport가 모두 열린 경우에만 true.
        # 실제 주문은 Hermes PASS + 정책별 confirmation + transport dispatch guard 필요.
        "live_order_sent_possible": live_order_sent_possible,
    }
    return data


def _stock_display_name(symbol: str) -> str:
    """회사명/종목명 우선 표시용 이름 조회. 미등록일 때만 코드 반환."""
    sym = str(symbol or "").strip()
    if not sym:
        return ""
    try:
        from config.settings import PORTFOLIO, WATCHLIST, SCAN_UNIVERSE_KR, RIA_ALLOWED_TICKERS
        for mapping in (PORTFOLIO, WATCHLIST, SCAN_UNIVERSE_KR, RIA_ALLOWED_TICKERS):
            name = mapping.get(sym)
            if name:
                return str(name)
    except Exception:
        pass
    try:
        from core.toss_live_pilot_hermes_bridge import SYMBOL_NAMES
        name = SYMBOL_NAMES.get(sym)
        if name:
            return str(name)
    except Exception:
        pass
    return sym




def _mask_broker_order_id(order_id: object) -> str:
    """Dashboard-safe broker order id display. Keeps enough for matching, no full id exposure."""
    raw = str(order_id or "")
    if not raw:
        return ""
    if len(raw) <= 18:
        return raw
    return f"{raw[:8]}…{raw[-6:]}"


def _normalize_broker_symbol(symbol: object) -> str:
    """Toss broker may return domestic symbols without suffix; normalize for display only."""
    sym = str(symbol or "").strip()
    if sym.isdigit() and len(sym) == 6:
        return f"{sym}.KS"
    return sym


def _recent_toss_broker_orders(limit: int = 20) -> dict:
    """Read-only broker order truth from Toss GET order lists."""
    global _toss_broker_orders_last_good, _toss_broker_orders_cooldown_until

    if _dashboard_toss_broker_reads_isolated():
        try:
            from core.toss_readonly_snapshot import broker_orders_for_consumer
            snapshot = broker_orders_for_consumer()
        except Exception as exc:
            snapshot = {"ok": False, "orders": [], "error": type(exc).__name__}
        orders: list[dict] = []
        for row in snapshot.get("orders") or []:
            if not isinstance(row, dict):
                continue
            out = dict(row)
            broker_symbol = str(out.get("symbol") or "")
            out["broker_symbol"] = broker_symbol
            out["symbol"] = _normalize_broker_symbol(broker_symbol) or broker_symbol
            out["ticker"] = out["symbol"]
            out["event_type"] = "broker_order_truth"
            out["status"] = out.get("broker_order_status") or ""
            out["created_at"] = out.get("filled_at") or out.get("ordered_at") or ""
            out["read_only_source"] = "stock_bot_snapshot"
            orders.append(_decorate_stock_display(out))
        orders.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        open_count = sum(
            1 for row in orders
            if str(row.get("broker_order_status") or "").upper() in {"OPEN", "PENDING", "ACCEPTED"}
        )
        snapshot_error = str(snapshot.get("error") or "")
        safe_snapshot_error = (
            snapshot_error
            if re.fullmatch(
                r"snapshot_(?:missing|stale|invalid|unavailable)"
                r"(?::[A-Za-z][A-Za-z0-9_]*)?",
                snapshot_error,
            )
            else "broker_snapshot_unavailable"
        )
        return {
            "ok": bool(snapshot.get("ok")),
            "error": "" if snapshot.get("ok") else safe_snapshot_error,
            "orders": orders[:limit],
            "open_count": open_count,
            "closed_count": max(0, len(orders) - open_count),
            "cache_status": snapshot.get("snapshot_status") or "unavailable",
            "source": "stock_bot_snapshot",
            "snapshot_age_sec": snapshot.get("snapshot_age_sec"),
            "usable_for_orders": False,
            "read_only_notice": (
                "자율매매 중 OAuth 충돌 방지: dashboard는 stock-bot broker-order snapshot만 소비"
            ),
        }

    _set_toss_readonly_timeout()
    cache_key = f"toss_broker_orders:{int(limit or 20)}"
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry:
            ts, val = entry
            if now - ts < _TOSS_BROKER_ORDERS_OK_TTL:
                return copy.deepcopy(val)
        last_good = _toss_broker_orders_last_good
        cooldown_until = _toss_broker_orders_cooldown_until

    if now < cooldown_until:
        if last_good and now - last_good[0] <= _TOSS_BROKER_ORDERS_STALE_TTL:
            stale = copy.deepcopy(last_good[1])
            stale["ok"] = False
            stale["cache_status"] = "stale"
            stale["stale_reason"] = "toss_broker_orders_cooldown"
            stale["cache_age_sec"] = round(max(0.0, now - last_good[0]))
            stale["error"] = stale.get("error") or "toss_broker_orders_cooldown"
            with _cache_lock:
                _cache[cache_key] = (now, stale)
            return stale
        return {
            "ok": False, "error": "toss_broker_orders_cooldown",
            "orders": [], "open_count": 0, "closed_count": 0,
            "cache_status": "cooldown", "source": "GET /api/v1/orders OPEN+CLOSED",
            "read_only_notice": "브로커 주문 조회 전용 · 주문 생성/취소/수정 없음",
        }

    try:
        from core.toss_live_order_http import list_orders
    except Exception:
        return {
            "ok": False,
            "error": "broker_order_source_unavailable",
            "orders": [],
            "open_count": 0,
            "closed_count": 0,
        }

    orders: list[dict] = []
    errors: list[str] = []
    counts = {"OPEN": 0, "CLOSED": 0}
    for status in ("OPEN", "CLOSED"):
        try:
            res = list_orders(status)
        except Exception:
            errors.append(f"{status}:broker_orders_unavailable")
            continue
        if not res.get("ok"):
            raw_reason = str(res.get("reason") or "")
            safe_reason = (
                raw_reason
                if re.fullmatch(r"http_[1-5][0-9]{2}", raw_reason)
                else "broker_orders_unavailable"
            )
            errors.append(f"{status}:{safe_reason}")
            continue
        rows = res.get("orders") or []
        counts[status] = len(rows)
        for row in rows:
            if not isinstance(row, dict):
                continue
            out = dict(row)
            broker_symbol = str(out.get("symbol") or "")
            symbol = _normalize_broker_symbol(broker_symbol)
            out["broker_symbol"] = broker_symbol
            out["symbol"] = symbol or broker_symbol
            out["ticker"] = out["symbol"]
            out["list_status"] = status
            out["event_type"] = "broker_order_truth"
            out["status"] = out.get("broker_order_status") or status
            out["created_at"] = out.get("filled_at") or out.get("ordered_at") or ""
            out["read_only_source"] = "toss_broker_orders_get"
            out["broker_order_id_masked"] = _mask_broker_order_id(out.get("broker_order_id"))
            out.pop("broker_order_id", None)
            orders.append(_decorate_stock_display(out))
    orders.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    result = {
        "ok": not errors,
        "error": "; ".join(errors),
        "orders": orders[:limit],
        "open_count": counts["OPEN"],
        "closed_count": counts["CLOSED"],
        "source": "GET /api/v1/orders OPEN+CLOSED",
        "read_only_notice": "브로커 주문 조회 전용 · 주문 생성/취소/수정 없음",
        "cache_status": "live" if not errors else "error",
    }
    with _cache_lock:
        if not errors:
            _toss_broker_orders_last_good = (now, result)
            _toss_broker_orders_cooldown_until = 0.0
        else:
            _toss_broker_orders_cooldown_until = now + _TOSS_BROKER_ORDERS_FAILURE_COOLDOWN_SEC
        _cache[cache_key] = (now, result)
    return result

def _decorate_stock_display(record: dict) -> dict:
    """대시보드/API 응답에서 종목코드 단독 표기를 피한다."""
    out = dict(record or {})
    sym = str(out.get("symbol") or out.get("ticker") or "").strip()
    if not sym:
        return out
    name = str(out.get("symbol_name") or out.get("name") or "").strip()
    if not name or name == sym:
        name = _stock_display_name(sym)
    if name and name != sym:
        out["symbol_name"] = name
        out.setdefault("name", name)
        out["symbol_label"] = f"{name} ({sym})"
        out["display_name"] = name
    else:
        out["symbol_label"] = sym
        out["display_name"] = sym
    return out


def _current_toss_live_policy_for_dashboard() -> dict:
    """Return current runtime live-pilot policy for read-only display.

    Ledger/verification rows intentionally keep their historical gate fields
    (often false/disabled). Dashboard summaries need the *current* runtime
    policy separately so old rows do not make an armed autonomous runtime look
    disabled.
    """
    return _toss_live_policy_fast(timeout=0.2)


def _merge_current_live_policy(summary: dict | None) -> dict:
    out = dict(summary or {})
    policy = _current_toss_live_policy_for_dashboard()
    if policy:
        historical_allowed = out.get("live_order_allowed")
        historical_adapter = out.get("adapter_status")
        out["historical_live_order_allowed"] = bool(historical_allowed)
        out["historical_adapter_status"] = historical_adapter or "unknown"
        out["live_order_allowed"] = bool(policy.get("live_order_allowed", False))
        out["adapter_status"] = policy.get("adapter_status", "disabled")
        out["live_transport_status"] = policy.get("live_transport_status", "not_configured")
        out["policy_live_order_allowed"] = bool(policy.get("live_order_allowed", False))
        out["policy_adapter_status"] = policy.get("adapter_status", "disabled")
        out["policy_live_transport_status"] = policy.get("live_transport_status", "not_configured")
        out["autonomous_mode"] = bool(policy.get("autonomous_mode", False))
        out["autonomous_kill_switch"] = bool(policy.get("autonomous_kill_switch", False))
    return out


def toss_live_pilot_previews_data(limit: int = 20) -> dict:
    """최근 live pilot 미리보기 기록 (read-only). 실제 주문 0건."""
    try:
        from core.toss_live_pilot_ledger import (
            list_live_pilot_records,
            live_pilot_ledger_summary,
        )
        records = [_decorate_stock_display(r) for r in list_live_pilot_records(limit=limit)]
        return {
            "summary": _merge_current_live_policy(live_pilot_ledger_summary()),
            "records": records,
        }
    except Exception as e:
        return {"error": str(e), "summary": {}, "records": []}


def toss_live_pilot_events_data(limit: int = 50) -> dict:
    """최근 live pilot callback 이벤트 (read-only). Hermes polling용."""
    try:
        from core.toss_live_pilot_events import list_events, event_summary
        summ = event_summary()
        policy = _toss_live_policy_fast(timeout=0.2)
        records = [_decorate_stock_display(r) for r in list_events(limit=limit)]
        broker_truth = _recent_toss_broker_orders(limit=min(limit, 50))
        broker_orders = broker_truth.get("orders") or []
        live_sent_real = int(summ.get("live_sent_real", 0))
        warnings = []
        if not broker_truth.get("ok", False):
            warnings.append("브로커 주문 조회 실패/제한: broker_orders_unavailable")
        if broker_orders and not records:
            warnings.append("브로커 주문은 있으나 live-pilot 이벤트 ledger가 비어 있음 — 표시/기록 경로 점검 필요")
        allowed_assets = set(policy.get("allowed_asset_types") or [])
        broker_assets = set()
        for o in broker_orders:
            sym = str(o.get("symbol") or "")
            if sym.endswith((".KS", ".KQ")):
                broker_assets.add("KR_STOCK")
            elif sym:
                broker_assets.add("US_STOCK")
        missing_assets = sorted(a for a in broker_assets if allowed_assets and a not in allowed_assets)
        if missing_assets:
            warnings.append("브로커 주문 자산군이 현재 policy.allowed_asset_types와 불일치: " + ", ".join(missing_assets))
        return {
            "summary": summ.get("summary", {}),
            "live_sent_real": live_sent_real,
            "live_sent_mock_or_artifact": summ.get("live_sent_mock_or_artifact", 0),
            "blocked_policy": summ.get("blocked_policy", 0),
            "blocked_transport": summ.get("blocked_transport", 0),
            "blocked_guard": summ.get("blocked_guard", 0),
            "live_order_sent_total": live_sent_real,
            "live_order_allowed": bool(policy.get("live_order_allowed", False)),
            "adapter_status": policy.get("adapter_status", "disabled"),
            "live_transport_status": policy.get("live_transport_status", "not_configured"),
            "records": records,
            "broker_orders": broker_orders,
            "broker_order_count": len(broker_orders),
            "broker_open_count": broker_truth.get("open_count", 0),
            "broker_closed_count": broker_truth.get("closed_count", 0),
            "broker_truth_ok": broker_truth.get("ok", False),
            "broker_truth_error": (
                "" if broker_truth.get("ok", False) else "broker_orders_unavailable"
            ),
            "warnings": warnings,
            "read_only_broker_source": broker_truth.get("source"),
        }
    except Exception:
        return {
            "error": "events_data_unavailable",
            "summary": {},
            "live_sent_real": 0,
            "live_sent_mock_or_artifact": 0,
            "blocked_policy": 0,
            "blocked_transport": 0,
            "blocked_guard": 0,
            "live_order_sent_total": 0,
            "live_order_allowed": False,
            "records": [],
        }


def toss_live_pilot_verifications_data(limit: int = 20) -> dict:
    """최근 Hermes 교차검증 기록 (read-only). live_order_allowed 항상 false."""
    try:
        from core.toss_live_pilot_verification import (
            list_verifications,
            verification_summary,
        )
        summ = verification_summary()
        counts = summ.get("summary", {})

        # mirror 설정 상태 포함 (비밀 미포함)
        mirror_enabled = False
        mirror_target_configured = False
        try:
            from core.toss_live_pilot_hermes_bridge import get_mirror_status
            mirror_cfg = get_mirror_status()
            mirror_enabled = mirror_cfg.get("mirror_enabled", False)
            mirror_target_configured = mirror_cfg.get("mirror_target_configured", False)
        except Exception:
            pass

        policy = _toss_live_policy_fast(timeout=0.2)

        summary = dict(summ or {})
        # verification 자체는 gate-only라 live_order_allowed=false가 맞다.
        # 대신 현재 runtime policy를 nested summary/top-level에 별도 표시해서
        # 대시보드가 오래된 gate-only false를 시스템 disabled로 오판하지 않게 한다.
        summary["gate_live_order_allowed"] = bool(summary.get("live_order_allowed", False))
        summary["policy_live_order_allowed"] = bool(policy.get("live_order_allowed", False))
        summary["policy_adapter_status"] = policy.get("adapter_status", "disabled")
        summary["policy_live_transport_status"] = policy.get("live_transport_status", "not_configured")
        summary["autonomous_mode"] = bool(policy.get("autonomous_mode", False))
        summary["autonomous_kill_switch"] = bool(policy.get("autonomous_kill_switch", False))

        return {
            "summary": summary,
            "records": [_decorate_stock_display(r) for r in list_verifications(limit=limit)],
            # 검증 레코드의 live_order_allowed는 PASS여도 false가 맞다(검증은 gate only).
            # 별도로 현재 운영 policy 상태를 노출해 대시보드/콜백이 false로 오판하지 않게 한다.
            "live_order_allowed": False,
            "policy_live_order_allowed": bool(policy.get("live_order_allowed", False)),
            "policy_adapter_status": policy.get("adapter_status", "disabled"),
            "policy_live_transport_status": policy.get("live_transport_status", "not_configured"),
            "mirror_enabled": mirror_enabled,
            "mirror_target_configured": mirror_target_configured,
            "pending_count": counts.get("PENDING", 0),
            "expired_count": counts.get("EXPIRED", 0),
        }
    except Exception as e:
        return {
            "error": str(e),
            "summary": {},
            "records": [],
            "live_order_allowed": False,
            "mirror_enabled": False,
            "mirror_target_configured": False,
            "pending_count": 0,
            "expired_count": 0,
        }


# ─── 액션센터 (실시간 주문 판단) ─────────────────────────
# read-only: 미결 추천(memory) + PRICE_ALERTS 를 라이브 시세로 판정.
# state: HIT(레벨 도달) / NEAR(±2% 이내) / FAR

_NEAR_PCT = 2.0
_STATE_RANK = {"HIT": 0, "NEAR": 1, "FAR": 2}


def _open_predictions_slim() -> list[dict]:
    """미결 추천(open predictions) 슬림 조회 — 실패 시 빈 리스트."""
    try:
        from core.memory import _get_conn
        conn = _get_conn()
        rows = conn.execute(
            """SELECT created_at, ticker, name, signal, entry_price,
                      target_price, stop_loss, confidence, reasoning,
                      COALESCE(account_type, '') AS account,
                      COALESCE(strategy_type, '') AS strategy_type
               FROM predictions WHERE status = 'open'
               ORDER BY created_at DESC LIMIT 40"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("미결 추천 조회 실패: %s", e)
        return []


def _level_state(price: float, level: float, direction: str) -> tuple[str, float]:
    """레벨 판정. direction: 'below'=가격이 레벨 이하면 HIT, 'above'=이상이면 HIT.

    Returns (state, dist_pct) — 레벨까지 남은 거리 % (양수=미도달, 0 이하=도달).
    """
    if price <= 0 or level <= 0:
        return "FAR", 999.0
    if direction == "below":
        dist = (price - level) / level * 100
    else:
        dist = (level - price) / level * 100
    if dist <= 0:
        return "HIT", round(dist, 2)
    if dist <= _NEAR_PCT:
        return "NEAR", round(dist, 2)
    return "FAR", round(dist, 2)


def _quote_meta(q, now_epoch: float) -> dict:
    return {
        "price": float(q.price) if q else 0.0,
        "price_source": (getattr(q, "source", "") or "quote") if q else "",
        "price_age_sec": (
            round(max(0.0, now_epoch - q.as_of))
            if q and getattr(q, "as_of", 0) else None),
    }


def _fetch_action_center_raw() -> dict:
    from config import settings
    from core.market import _batch_quotes

    preds = _open_predictions_slim()
    alerts = getattr(settings, "PRICE_ALERTS", {}) or {}
    portfolio = getattr(settings, "PORTFOLIO", {}) or {}

    ticker_map: dict[str, str] = {}
    for p in preds:
        t = p.get("ticker", "")
        if t:
            ticker_map[t] = p.get("name") or portfolio.get(t, t)
    for t, cfg in alerts.items():
        ticker_map[t] = cfg.get("name", t)

    quotes = _portfolio_quotes_fast(ticker_map, _batch_quotes) if ticker_map else {}
    now_epoch = time.time()

    items: list[dict] = []

    for p in preds:
        t = p.get("ticker", "")
        q = quotes.get(t)
        meta = _quote_meta(q, now_epoch)
        signal = (p.get("signal") or "").strip()
        is_buy = "매수" in signal or "BUY" in signal.upper()
        levels: list[dict] = []
        for key, label, direction in (
            ("entry_price", "진입가", "below" if is_buy else "above"),
            ("target_price", "목표가", "above"),
            ("stop_loss", "손절가", "below"),
        ):
            lv = float(p.get(key) or 0)
            if lv <= 0:
                continue
            state, dist = _level_state(meta["price"], lv, direction)
            levels.append({"type": label, "level": lv, "direction": direction,
                           "state": state, "dist_pct": dist})
        if not levels:
            continue
        best = min(levels, key=lambda l: _STATE_RANK[l["state"]])
        items.append({
            "kind": "recommendation",
            "ticker": t,
            "name": p.get("name") or ticker_map.get(t, t),
            "signal": signal,
            "account": p.get("account", ""),
            "strategy_type": p.get("strategy_type", ""),
            "created_at": p.get("created_at", ""),
            "confidence": p.get("confidence", 0),
            "reason": (p.get("reasoning") or "").strip()[:200],
            **meta,
            "levels": levels,
            "state": best["state"],
            "nearest_level": best,
        })

    for t, cfg in alerts.items():
        q = quotes.get(t)
        meta = _quote_meta(q, now_epoch)
        levels = []
        for direction in ("below", "above"):
            lv = float(cfg.get(direction) or 0)
            if lv <= 0:
                continue
            state, dist = _level_state(meta["price"], lv, direction)
            label = "하락 알림가" if direction == "below" else "상승 알림가"
            levels.append({"type": label, "level": lv, "direction": direction,
                           "state": state, "dist_pct": dist})
        if not levels:
            continue
        best = min(levels, key=lambda l: _STATE_RANK[l["state"]])
        items.append({
            "kind": "price_alert",
            "ticker": t,
            "name": cfg.get("name", t),
            "signal": "가격 알림",
            "account": "",
            "strategy_type": "",
            "created_at": "",
            "confidence": 0,
            "reason": (cfg.get("reason") or "").strip()[:200],
            **meta,
            "levels": levels,
            "state": best["state"],
            "nearest_level": best,
        })

    urgent = [it for it in items if it["state"] in ("HIT", "NEAR")]
    watching = [it for it in items if it["state"] == "FAR"]
    urgent.sort(key=lambda it: (0 if it["state"] == "HIT" else 1,
                                abs(it["nearest_level"]["dist_pct"])))
    watching.sort(key=lambda it: it["nearest_level"]["dist_pct"])
    return {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "urgent_count": len(urgent),
        "urgent": [_decorate_stock_display(it) for it in urgent],
        "watching": [_decorate_stock_display(it) for it in watching],
        "open_recommendation_count": len(preds),
        "near_threshold_pct": _NEAR_PCT,
    }


def action_center_data() -> dict:
    return _cached("action_center", 30, _fetch_action_center_raw)


def alerts_history_data(hours: int = 48) -> dict:
    def _fetch() -> dict:
        try:
            from core.memory import recent_alerts
            items = recent_alerts(hours=hours)
        except Exception as e:
            log.debug("알림 이력 조회 실패: %s", e)
            items = []
        return {
            "hours": hours,
            "count": len(items),
            "items": [_decorate_stock_display(it) for it in items],
        }
    return _cached(f"alerts_history_{hours}", 20, _fetch)


def dart_disclosures() -> dict:
    def _fetch() -> dict:
        from core.briefing_enrichment import dart_disclosures_data
        return dart_disclosures_data(days=3)
    return _cached("dart_disclosures", 600, _fetch)


def orderbook_summary() -> dict:
    def _fetch() -> dict:
        from core.briefing_enrichment import orderbook_summary_data
        return orderbook_summary_data()
    return _cached("orderbook_summary", 60, _fetch)
