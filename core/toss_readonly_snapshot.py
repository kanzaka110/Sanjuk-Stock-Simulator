"""Cross-process read-only Toss account snapshot.

During autonomous mode, only the long-running stock-bot/monitor process owns
Toss OAuth and broker GET calls. Dashboard and scheduled briefing processes
consume a sanitized atomic snapshot. The snapshot is decision context only: it
is never an order authorization and ``usable_for_orders`` is always false.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from config.settings import DB_DIR

log = logging.getLogger(__name__)

VERSION = "toss_readonly_snapshot_v2"
REFRESH_INTERVAL_SEC = 300
FRESH_TTL_SEC = 900
DISPLAY_STALE_TTL_SEC = 3600
MAX_FUTURE_SKEW_SEC = 120

ROLE_OWNER = "broker_owner"
ROLE_CONSUMER = "snapshot_consumer"
ROLE_ENV = "TOSS_PROCESS_ROLE"

_REFRESH_LOCK = threading.Lock()
_LAST_REFRESH_MONOTONIC = 0.0
_LONG_NUMBER_RE = re.compile(r"\b\d{8,}\b")
_BEARER_RE = re.compile(r"(?i)bearer\s+(?!\[REDACTED\])[A-Za-z0-9._~+\-/]+=*")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?ix)\b(?:"
    r"access[_-]?token|refresh[_-]?token|token|secret|password|authorization|"
    r"api[_-]?key|app[_-]?(?:key|secret)|client[_-]?secret|credential(?:s)?|"
    r"account[_-]?(?:no|number|id|seq)|connection[_-]?string"
    r")\b[\"']?\s*[:=]\s*[^\s,;]+"
)
_NAKED_SECRET_RE = re.compile(
    r"(?ix)(?:"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bhf_[A-Za-z0-9]{20,}\b|\bsk-(?:live-)?[A-Za-z0-9_-]{20,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b|\bAKIA[0-9A-Z]{16}\b|"
    r"\bAIza[0-9A-Za-z_-]{30,}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")"
)
_SENSITIVE_KEYS = {
    "access_token", "refresh_token", "token", "authorization",
    "account", "accountno", "accountnumber", "account_number", "account_id",
    "accountid", "account_seq", "accountseq", "appkey", "appsecret",
    "client_secret", "clientsecret", "credential", "credentials",
    "unknowncredential", "secret", "key", "password",
}
_HOLDING_TEXT_FIELDS = ("symbol", "stockCode", "name", "currency", "marketCountry")
_HOLDING_NUMBER_FIELDS = (
    "quantity", "sellableQuantity", "lastPrice", "currentPrice", "averagePrice",
)
_HOLDING_MONEY_FIELDS = ("marketValue", "profitLoss", "dailyProfitLoss")
_MONEY_NUMBER_FIELDS = (
    "amount", "amountAfterCost", "rate", "purchaseAmount", "krw", "krw_native",
    "usd", "usd_krw", "profitable_count", "loss_count",
)
_MONEY_TEXT_FIELDS = ("currency",)
_MONEY_BOOL_FIELDS = ("usd_included",)
_CALENDAR_TEXT_FIELDS = (
    "market", "date", "status", "openTime", "closeTime", "nextOpenDate", "nextOpenTime",
)
_CALENDAR_BOOL_FIELDS = ("open", "isOpen", "holiday")
_SAFE_CLIENT_ORDER_ID_RE = re.compile(r"^tlive_[A-Za-z0-9_-]{1,30}$")
_BROKER_ORDER_TEXT_FIELDS = (
    "broker_order_status", "symbol", "side", "ordered_at", "filled_at",
)
_BROKER_ORDER_NUMBER_FIELDS = (
    "quantity", "filled_quantity", "filled_price",
)


def _truthy(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on", "y"}


def _commands() -> set[str]:
    return {str(arg).strip().lower() for arg in sys.argv[1:]}


def process_role() -> str:
    """Resolve an explicit process role, with fail-closed command fallbacks."""
    configured = str(os.environ.get(ROLE_ENV, "")).strip().lower()
    if configured in {ROLE_OWNER, ROLE_CONSUMER}:
        return configured
    commands = _commands()
    if commands & {"bot", "monitor"}:
        return ROLE_OWNER
    return ROLE_CONSUMER


def is_broker_owner_process() -> bool:
    return process_role() == ROLE_OWNER


def should_consume_snapshot() -> bool:
    """비소유 프로세스는 env와 무관하게 항상 snapshot consumer다.

    (2026-07-15 계약 변경) 기존 'TOSS_AUTONOMOUS_MODE AND 비소유' 조건은
    env를 로드하지 않는 크론 브리핑·도구가 차단을 우회해 토큰을 발급하고
    bot 토큰을 무효화하는 401 경쟁을 낳았다. 토큰 단일 소유는 운영 모드와
    무관한 불변식이므로 role만으로 판정한다. 예외가 필요한 도구는
    TOSS_PROCESS_ROLE=broker_owner를 명시해야 한다.
    """
    return process_role() != ROLE_OWNER


def _snapshot_path() -> Path:
    override = os.environ.get("TOSS_READONLY_SNAPSHOT_PATH", "").strip()
    return Path(override) if override else Path(DB_DIR) / "toss_readonly_snapshot.json"


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _normalized_key(key: object) -> str:
    return str(key).lower().replace("-", "_")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: object, max_len: int = 200) -> str:
    raw = str(value or "")[:max_len]
    raw = _BEARER_RE.sub("Bearer [REDACTED]", raw)
    return _LONG_NUMBER_RE.sub("[NUM_REDACTED]", raw)


def _project_money(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _MONEY_NUMBER_FIELDS:
        if key in value and value.get(key) not in (None, ""):
            out[key] = _number(value.get(key))
    for key in _MONEY_TEXT_FIELDS:
        if key in value and value.get(key) not in (None, ""):
            out[key] = _text(value.get(key), 16)
    for key in _MONEY_BOOL_FIELDS:
        if key in value:
            out[key] = bool(value.get(key))
    return out


def _project_holding(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _HOLDING_TEXT_FIELDS:
        if key in value and value.get(key) not in (None, ""):
            out[key] = _text(value.get(key), 120)
    for key in _HOLDING_NUMBER_FIELDS:
        if key in value and value.get(key) not in (None, ""):
            out[key] = _number(value.get(key))
    for key in _HOLDING_MONEY_FIELDS:
        projected = _project_money(value.get(key))
        if projected:
            out[key] = projected
    return out


def _project_calendar(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _CALENDAR_TEXT_FIELDS:
        if key in value and value.get(key) not in (None, ""):
            out[key] = _text(value.get(key), 64)
    for key in _CALENDAR_BOOL_FIELDS:
        if key in value:
            out[key] = bool(value.get(key))
    today = value.get("today")
    if isinstance(today, dict):
        out["today"] = _project_calendar(today)
    return out


def _project_broker_order(value: object) -> dict:
    """Allowlist one read-only broker order; preserve only safe local client ID."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    client_order_id = str(value.get("client_order_id") or "")
    if _SAFE_CLIENT_ORDER_ID_RE.fullmatch(client_order_id):
        out["client_order_id"] = client_order_id
    for key in _BROKER_ORDER_TEXT_FIELDS:
        if value.get(key) not in (None, ""):
            out[key] = _text(value.get(key), 80)
    for key in _BROKER_ORDER_NUMBER_FIELDS:
        if value.get(key) not in (None, ""):
            out[key] = _number(value.get(key))
    return out if out.get("symbol") else {}


def _project_broker_orders(values: object) -> list[dict]:
    if not isinstance(values, list):
        return []
    return [row for row in (_project_broker_order(item) for item in values[:200]) if row]


def _project_account_summary(value: object) -> dict:
    """Allowlist projection: unknown broker fields are dropped, never persisted."""
    if not isinstance(value, dict):
        return {}
    if value.get("error"):
        return {}
    account_count = int(_number(value.get("account_count"), 0))
    holdings = [row for row in (_project_holding(item) for item in value.get("holdings_items") or []) if row]
    accounts: list[dict] = []
    for account in value.get("accounts") or []:
        if isinstance(account, dict) and account.get("account_type"):
            accounts.append({"account_type": _text(account.get("account_type"), 40)})
    summary = {
        "enabled": bool(value.get("enabled", True)),
        "label": "Toss 실전 AI 자동거래 계좌",
        "separate_from_portfolio": True,
        "included_in_total_portfolio": False,
        "trading_enabled": False,
        "automation_status": "snapshot_read_only",
        "account_count": account_count,
        "accounts": accounts,
        "holdings_count": len(holdings),
        "holdings_items": holdings,
        "market_value": _project_money(value.get("market_value")),
        "cash": _project_money(value.get("cash")),
        "total_account_value": _project_money(value.get("total_account_value")),
        "exchange_rate": _project_money(value.get("exchange_rate")),
        "profit_loss": _project_money(value.get("profit_loss")),
        "today_profit_loss": _project_money(value.get("today_profit_loss")),
        "realized_profit_loss": {
            "krw": None,
            "source": "not_available_from_current_readonly_summary",
        },
        "pnl_scope": {
            "profit_loss": "open_positions_unrealized_after_cost",
            "today_profit_loss": "open_positions_daily_change_excludes_closed_realized",
            "realized_profit_loss": "unavailable",
            "true_daily_account_pnl_available": False,
            "warning": "오늘 손익은 현재 보유만 합산하며 매도 종목의 실현손익은 포함하지 않음",
        },
        "warnings": [
            "기존 삼성증권/수동 포트폴리오에 합산하지 않음",
            "stock-bot snapshot: 주문 직접 사용 금지",
            "오늘 손익은 현재 보유만 합산하며 매도 종목의 실현손익은 포함하지 않음",
        ],
        "error": "",
    }
    return summary if account_count > 0 else {}


def _decision_context_from_summary(summary: dict, calendars: dict | None = None) -> dict:
    cash = summary.get("cash") or {}
    market_value = summary.get("market_value") or {}
    total = summary.get("total_account_value") or {}
    fx = summary.get("exchange_rate") or {}
    holdings = summary.get("holdings_items") or []
    calendars = calendars or {}
    cash_krw = _number(cash.get("krw"))
    market_value_krw = _number(market_value.get("krw"))
    total_krw = _number(total.get("krw"), cash_krw + market_value_krw)
    fx_rate = _number(fx.get("rate"))
    projected_calendars = {
        "KR": _project_calendar(calendars.get("KR")),
        "US": _project_calendar(calendars.get("US")),
    }
    return {
        "enabled": bool(summary.get("enabled", True)),
        "account_label": "Toss 실전 AI 자동거래 계좌",
        "included_in_total_portfolio": False,
        "cash_krw": cash_krw,
        "cash_usd": cash.get("usd"),
        "market_value_krw": market_value_krw,
        "total_account_value_krw": total_krw,
        "holdings_count": int(summary.get("holdings_count") or len(holdings)),
        "holdings": holdings,
        "usdkrw": fx_rate,
        "market_calendar": projected_calendars,
        "automation": {
            "enabled": False,
            "mode": "snapshot_read_only",
            "dry_run": True,
            "live_orders_allowed": False,
            "kill_switch": True,
        },
        "data_quality": {
            "toss_available": True,
            "cash_available": bool(cash),
            "fx_available": bool(fx_rate),
            "calendar_available": bool(projected_calendars["KR"] or projected_calendars["US"]),
            "stale": False,
            "warnings": [],
            "source": "stock_bot_snapshot",
        },
    }


def _contains_sensitive(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in _SENSITIVE_KEYS:
                return True
            if (
                normalized == "client_order_id"
                and _SAFE_CLIENT_ORDER_ID_RE.fullmatch(str(item or ""))
            ):
                continue
            if _contains_sensitive(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_sensitive(item) for item in value)
    if isinstance(value, str):
        return bool(
            _BEARER_RE.search(value)
            or _LONG_NUMBER_RE.search(value)
            or _SENSITIVE_ASSIGNMENT_RE.search(value)
            or _NAKED_SECRET_RE.search(value)
        )
    return False


def _read_existing_generated_at(path: Path) -> float:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(current, dict) and current.get("version") == VERSION:
            return float(current.get("generated_at") or 0)
    except Exception:
        pass
    return 0.0


def _fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_snapshot(
    account_summary: dict,
    calendars: dict | None = None,
    broker_orders: list[dict] | None = None,
    *,
    now: float | None = None,
) -> dict:
    """Project safe fields and atomically publish a mode-0600 snapshot."""
    summary = _project_account_summary(account_summary)
    if not summary:
        return {"ok": False, "reason": "account_summary_unavailable"}
    generated_at = float(time.time() if now is None else now)
    decision_context = _decision_context_from_summary(summary, calendars)
    envelope = {
        "version": VERSION,
        "generated_at": generated_at,
        "producer": "stock_bot",
        "read_only": True,
        "order_side_effects": False,
        "usable_for_orders": False,
        "account_summary": summary,
        "decision_context": decision_context,
        "broker_orders": _project_broker_orders(broker_orders),
    }
    if _contains_sensitive(envelope):
        return {"ok": False, "reason": "sensitive_data_detected"}

    path = _snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(path)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existing_generated_at = _read_existing_generated_at(path)
        if existing_generated_at > generated_at:
            return {
                "ok": True,
                "skipped": True,
                "reason": "newer_snapshot_exists",
                "generated_at": existing_generated_at,
            }
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            os.chmod(path, 0o600)
            _fsync_directory(path.parent)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    return {"ok": True, "path": str(path), "generated_at": generated_at}


def load_snapshot(*, now: float | None = None) -> dict:
    """Validate freshness and contracts; expired or sensitive payloads are withheld."""
    path = _snapshot_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": False, "status": "missing", "reason": "snapshot_missing"}
    except Exception as exc:
        return {"ok": False, "status": "invalid", "reason": f"snapshot_invalid:{type(exc).__name__}"}
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return {"ok": False, "status": "invalid", "reason": "snapshot_schema_mismatch"}
    if raw.get("read_only") is not True or raw.get("order_side_effects") is not False:
        return {"ok": False, "status": "invalid", "reason": "snapshot_contract_violation"}
    if raw.get("usable_for_orders") is not False:
        return {"ok": False, "status": "invalid", "reason": "snapshot_order_contract_violation"}
    if _contains_sensitive(raw):
        return {"ok": False, "status": "invalid", "reason": "snapshot_sensitive_data"}
    try:
        generated_at = float(raw.get("generated_at") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "status": "invalid", "reason": "snapshot_timestamp_invalid"}
    current = float(time.time() if now is None else now)
    if generated_at > current + MAX_FUTURE_SKEW_SEC:
        return {"ok": False, "status": "invalid", "reason": "snapshot_from_future"}
    age_sec = max(0.0, current - generated_at)
    if age_sec > DISPLAY_STALE_TTL_SEC:
        return {"ok": False, "status": "expired", "reason": "snapshot_expired", "age_sec": round(age_sec, 1)}
    status = "fresh" if age_sec <= FRESH_TTL_SEC else "stale"
    return {
        "ok": True,
        "status": status,
        "age_sec": round(age_sec, 1),
        "usable_for_decisions": status == "fresh",
        "usable_for_orders": False,
        "account_summary": raw.get("account_summary") or {},
        "decision_context": raw.get("decision_context") or {},
        "broker_orders": _project_broker_orders(raw.get("broker_orders") or []),
        "generated_at": generated_at,
    }


def _copy_payload(payload: Any) -> Any:
    return json.loads(json.dumps(payload, ensure_ascii=False))


def _mark_snapshot_quality(payload: dict, snapshot: dict) -> dict:
    out = _copy_payload(payload)
    status = str(snapshot.get("status") or "invalid")
    age_sec = float(snapshot.get("age_sec") or 0)
    out["snapshot_status"] = status
    out["snapshot_age_sec"] = age_sec
    out["snapshot_usable_for_decisions"] = bool(snapshot.get("usable_for_decisions"))
    out["snapshot_usable_for_orders"] = False
    out["stale"] = status != "fresh"
    warnings = list(out.get("warnings") or [])
    if status == "stale":
        warnings.append("Toss snapshot이 15분을 초과해 화면 참고용으로만 사용")
    out["warnings"] = warnings
    dq = dict(out.get("data_quality") or {})
    dq.update({
        "source": "stock_bot_snapshot",
        "snapshot_status": status,
        "snapshot_age_sec": age_sec,
        "usable_for_decisions": status == "fresh",
        "usable_for_orders": False,
        "stale": status != "fresh",
    })
    out["data_quality"] = dq
    return out


def account_summary_for_consumer() -> dict | None:
    snapshot = load_snapshot()
    if not snapshot.get("ok"):
        return None
    return _mark_snapshot_quality(snapshot.get("account_summary") or {}, snapshot)


def broker_orders_for_consumer() -> dict:
    """Return sanitized broker GET truth from snapshot; never authorize orders."""
    snapshot = load_snapshot()
    if not snapshot.get("ok"):
        return {
            "ok": False,
            "orders": [],
            "error": snapshot.get("reason") or "snapshot_unavailable",
            "source": "stock_bot_snapshot",
            "usable_for_orders": False,
        }
    return {
        "ok": True,
        "orders": _copy_payload(snapshot.get("broker_orders") or []),
        "snapshot_status": snapshot.get("status"),
        "snapshot_age_sec": snapshot.get("age_sec"),
        "source": "stock_bot_snapshot",
        "usable_for_orders": False,
    }


def decision_context_for_consumer() -> dict | None:
    snapshot = load_snapshot()
    if not snapshot.get("ok"):
        return None
    out = _mark_snapshot_quality(snapshot.get("decision_context") or {}, snapshot)
    dq = dict(out.get("data_quality") or {})
    if snapshot.get("status") != "fresh":
        dq.update({
            "toss_available": False,
            "cash_available": False,
            "fx_available": False,
            "calendar_available": False,
            "usable_for_decisions": False,
            "usable_for_orders": False,
        })
        warnings = list(dq.get("warnings") or [])
        warnings.append("오래된 Toss snapshot은 후보 sizing/readiness와 주문 판단에 사용 금지")
        dq["warnings"] = warnings
    out["data_quality"] = dq
    return out


def _raw_account_summary_from_broker() -> tuple[dict, dict, list[dict]]:
    """Broker-owner GET projection source. No order/write endpoint is called."""
    from core import toss_client as tc

    accounts = tc.get_accounts()
    if not accounts:
        return {}, {}, []
    first = accounts[0] if isinstance(accounts[0], dict) else {}
    seq = str(first.get("accountSeq") or "")
    if not seq:
        return {}, {}, []
    holdings = tc.get_holdings(seq)
    items = holdings.get("items") or [] if isinstance(holdings, dict) else []
    market_value = holdings.get("marketValue") or {} if isinstance(holdings, dict) else {}
    market_amount = market_value.get("amount") or {} if isinstance(market_value, dict) else {}
    bp_krw = tc.get_buying_power(seq, "KRW") or {}
    bp_usd = tc.get_buying_power(seq, "USD") or {}
    fx = tc.get_exchange_rate("USD", "KRW") or {}
    fx_rate = _number(fx.get("rate"))
    mv_krw = _number(market_amount.get("krw"))
    mv_usd = _number(market_amount.get("usd"))
    cash_krw = _number(bp_krw.get("cashBuyingPower"))
    cash_usd = _number(bp_usd.get("cashBuyingPower"))
    mv_usd_krw = mv_usd * fx_rate if fx_rate else 0.0
    cash_usd_krw = cash_usd * fx_rate if fx_rate else 0.0

    def _row_krw(amount: Any, currency: Any) -> float:
        value = _number(amount)
        return value * fx_rate if str(currency or "KRW").upper() == "USD" and fx_rate else value

    unrealized_krw = 0.0
    unrealized_before_cost_krw = 0.0
    daily_krw = 0.0
    cost_basis_krw = 0.0
    daily_basis_krw = 0.0
    profitable_count = 0
    loss_count = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        currency = item.get("currency") or "KRW"
        profit = item.get("profitLoss") or {}
        daily = item.get("dailyProfitLoss") or {}
        row_market = item.get("marketValue") or {}
        amount_after_cost = _number(profit.get("amountAfterCost", profit.get("amount")))
        unrealized_krw += _row_krw(amount_after_cost, currency)
        unrealized_before_cost_krw += _row_krw(profit.get("amount"), currency)
        daily_krw += _row_krw(daily.get("amount"), currency)
        cost_basis_krw += _row_krw(row_market.get("purchaseAmount"), currency)
        daily_basis_krw += _row_krw(row_market.get("amount"), currency)
        if amount_after_cost > 0:
            profitable_count += 1
        elif amount_after_cost < 0:
            loss_count += 1

    summary = {
        "enabled": tc.is_configured(),
        "account_count": len(accounts),
        "accounts": [{"account_type": first.get("accountType", "")}],
        "holdings_items": items,
        "market_value": {
            "krw": mv_krw + mv_usd_krw,
            "krw_native": mv_krw,
            "usd": mv_usd,
            "usd_krw": mv_usd_krw,
        },
        "cash": {
            "krw": cash_krw + cash_usd_krw,
            "krw_native": cash_krw,
            "usd": cash_usd,
            "usd_krw": cash_usd_krw,
        },
        "total_account_value": {
            "krw": mv_krw + cash_krw + mv_usd_krw + cash_usd_krw,
            "krw_native": mv_krw + cash_krw,
            "usd": mv_usd + cash_usd,
            "usd_krw": mv_usd_krw + cash_usd_krw,
            "usd_included": bool(fx_rate and (mv_usd or cash_usd)),
        },
        "exchange_rate": {"rate": fx_rate},
        "profit_loss": {
            "krw": unrealized_krw,
            "amount": unrealized_before_cost_krw,
            "rate": (unrealized_krw / cost_basis_krw) if cost_basis_krw else 0.0,
            "profitable_count": profitable_count,
            "loss_count": loss_count,
        },
        "today_profit_loss": {
            "krw": daily_krw,
            "rate": (daily_krw / daily_basis_krw) if daily_basis_krw else 0.0,
        },
        "error": "",
    }
    calendars = {"KR": tc.get_market_calendar("KR"), "US": tc.get_market_calendar("US")}
    broker_orders: list[dict] = []
    from core.toss_live_order_http import list_orders
    for status in ("OPEN", "CLOSED"):
        result = list_orders(status, account_seq=seq)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError(f"broker_orders_incomplete:{status}")
        rows = result.get("orders")
        if not isinstance(rows, list):
            raise RuntimeError(f"broker_orders_invalid:{status}")
        broker_orders.extend(row for row in rows if isinstance(row, dict))
    return summary, calendars, broker_orders


def refresh_snapshot_if_due(*, force: bool = False) -> dict:
    """Refresh from the explicit broker-owner process; never call order endpoints."""
    global _LAST_REFRESH_MONOTONIC
    if not is_broker_owner_process():
        return {"ok": False, "skipped": True, "reason": "not_broker_owner_process"}
    now_mono = time.monotonic()
    with _REFRESH_LOCK:
        if not force and now_mono - _LAST_REFRESH_MONOTONIC < REFRESH_INTERVAL_SEC:
            return {"ok": True, "skipped": True, "reason": "refresh_throttled"}
        _LAST_REFRESH_MONOTONIC = now_mono
    try:
        summary, calendars, broker_orders = _raw_account_summary_from_broker()
        if not summary:
            return {"ok": False, "reason": "account_summary_unavailable", "order_side_effects": False}
        fill_sync: dict = {"updated": 0, "ambiguous": 0, "rejected": 0}
        try:
            from core.toss_live_pilot_events import sync_live_event_fills_from_broker_orders
            fill_sync = sync_live_event_fills_from_broker_orders(broker_orders)
        except Exception as exc:
            log.warning("exact broker fill sync failed: %s", type(exc).__name__)
            fill_sync = {"updated": 0, "ambiguous": 0, "rejected": 0, "error": type(exc).__name__}
        result = write_snapshot(summary, calendars, broker_orders)
        result["fill_sync"] = fill_sync
        result["order_side_effects"] = False
        return result
    except Exception as exc:
        return {"ok": False, "reason": f"snapshot_refresh_failed:{type(exc).__name__}", "order_side_effects": False}
