"""Durable cross-owner idempotency for Toss autonomous exit SELLs."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

KST = timezone(timedelta(hours=9))
US_EASTERN = ZoneInfo("America/New_York")
_PREFIX = "execution_decision:exit."
_SYMBOL_PATTERN = r"(?:[0-9]{6}|[A-Z][A-Z0-9.-]{0,31})"
_REF_RE = re.compile(
    rf"^execution_decision:exit\.{_SYMBOL_PATTERN}\.[0-9]{{8}}\.[a-z_]{{1,24}}$"
)
_INTENT_CLASSES = frozenset({"full_exit", "partial_exit", "rebalance"})
_LEASE = timedelta(minutes=5)


def _state_path() -> Path:
    override = os.environ.get("TOSS_EXIT_INTENT_STATE_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "db" / "data" / "toss_exit_execution_intents.json"


def _market_day(symbol: str, value: datetime):
    normalized = str(symbol or "").upper().strip()
    is_kr = re.fullmatch(r"[0-9]{6}(?:\.(?:KS|KQ))?", normalized) is not None
    return value.astimezone(KST if is_kr else US_EASTERN).date()


def _canonical_symbol(symbol: str) -> str:
    normalized = str(symbol or "").upper().strip()
    kr_match = re.fullmatch(r"([0-9]{6})(?:\.(?:KS|KQ))?", normalized)
    return kr_match.group(1) if kr_match else normalized


def build_exit_decision_ref(symbol: str, intent_class: str, now: datetime) -> str:
    normalized = _canonical_symbol(symbol)
    intent = str(intent_class or "").lower().strip()
    if not normalized or intent not in _INTENT_CLASSES:
        raise ValueError("invalid_exit_intent")
    if not re.fullmatch(_SYMBOL_PATTERN, normalized):
        raise ValueError("invalid_exit_symbol")
    day = _market_day(normalized, now).strftime("%Y%m%d")
    ref = f"{_PREFIX}{normalized}.{day}.{intent}"
    if not _REF_RE.fullmatch(ref):
        raise ValueError("invalid_exit_decision_ref")
    return ref


def is_exit_decision_ref(value: object) -> bool:
    return (
        type(value) is str
        and _REF_RE.fullmatch(value) is not None
        and value.rsplit(".", 1)[-1] in _INTENT_CLASSES
    )


def exit_decision_ref_matches(
    decision_ref: object,
    symbol: object,
    now: datetime | None = None,
) -> bool:
    """Bind a managed SELL ref to the record symbol and current market day."""
    if type(decision_ref) is not str or not is_exit_decision_ref(decision_ref):
        return False
    if type(symbol) is not str:
        return False
    normalized = _canonical_symbol(symbol)
    if not re.fullmatch(_SYMBOL_PATTERN, normalized):
        return False
    current = now or datetime.now(KST)
    if type(current) is not datetime or current.tzinfo is None:
        return False
    payload = decision_ref[len(_PREFIX):]
    ref_symbol, ref_day, _intent = payload.rsplit(".", 2)
    return (
        ref_symbol == normalized
        and ref_day == _market_day(normalized, current).strftime("%Y%m%d")
    )


def _intent_scope(decision_ref: str) -> str:
    """All SELL dispositions for one symbol/market-day share one fence."""
    if not is_exit_decision_ref(decision_ref):
        return ""
    return decision_ref.rsplit(".", 1)[0]


def acquire_exit_dispatch_lock(decision_ref: str) -> dict:
    """Intent별 irreversible transport 구간을 한 프로세스만 소유하게 한다."""
    if not is_exit_decision_ref(decision_ref):
        return {"ok": False, "reason": "invalid_exit_dispatch_lock"}
    path = _state_path()
    fd: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(_intent_scope(decision_ref).encode("utf-8")).hexdigest()[:24]
        lock_path = path.with_name(f".{path.name}.{digest}.dispatch.lock")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return {"ok": True, "reason": "exit_intent_dispatch_locked", "_fd": fd}
    except OSError as exc:
        reason = (
            "exit_intent_inflight"
            if exc.errno in {errno.EACCES, errno.EAGAIN}
            else "exit_intent_state_unavailable"
        )
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return {"ok": False, "reason": reason}
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return {"ok": False, "reason": "exit_intent_state_unavailable"}


def release_exit_dispatch_lock(lock: object) -> None:
    fd = lock.get("_fd") if type(lock) is dict else None
    if type(fd) is not int:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _iso(now: datetime) -> str:
    return now.astimezone(KST).isoformat(timespec="seconds")


def _parse_ts(value: object) -> datetime | None:
    if type(value) is not str or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(KST)


def _load_state(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "intents": {}}
    except Exception:
        return None
    if (
        type(raw) is not dict
        or type(raw.get("version")) is not int
        or raw.get("version") != 1
    ):
        return None
    intents = raw.get("intents")
    if type(intents) is not dict:
        return None
    scopes: set[str] = set()
    for key, row in intents.items():
        if not is_exit_decision_ref(key) or type(row) is not dict:
            return None
        scope = _intent_scope(key)
        if not scope or scope in scopes:
            return None
        scopes.add(scope)
        if row.get("status") not in {"reserved", "sent"}:
            return None
        if type(row.get("pilot_id")) is not str or not row["pilot_id"]:
            return None
        if _parse_ts(row.get("updated_at")) is None:
            return None
    return raw


def _write_state(path: Path, state: dict) -> bool:
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
        )
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
        dir_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return True
    except Exception:
        return False
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _mutate(fn: Callable[[dict], tuple[dict, bool]]) -> dict:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except Exception:
        return {"ok": False, "reason": "exit_intent_state_unavailable"}
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = _load_state(path)
        if state is None:
            return {"ok": False, "reason": "exit_intent_state_invalid"}
        result, changed = fn(state)
        if changed and not _write_state(path, state):
            return {"ok": False, "reason": "exit_intent_state_unavailable"}
        return result
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def claim_exit_intent(decision_ref: str, pilot_id: str, *, now: datetime | None = None) -> dict:
    current = now or datetime.now(KST)
    if not is_exit_decision_ref(decision_ref) or type(pilot_id) is not str or not pilot_id:
        return {"ok": False, "reason": "invalid_exit_intent_claim"}

    def mutate(state: dict) -> tuple[dict, bool]:
        intents = state["intents"]
        scope = _intent_scope(decision_ref)
        matching = [(key, value) for key, value in intents.items() if _intent_scope(key) == scope]
        if len(matching) > 1:
            return {"ok": False, "reason": "exit_intent_state_invalid"}, False
        prior_ref, row = matching[0] if matching else (decision_ref, None)
        if row is None:
            intents[decision_ref] = {
                "status": "reserved", "pilot_id": pilot_id, "updated_at": _iso(current),
            }
            return {"ok": True, "reason": "exit_intent_claimed"}, True
        if row["status"] == "sent":
            return {
                "ok": False, "reason": "exit_intent_already_sent",
                "prior_pilot_id": row["pilot_id"],
            }, False
        updated_at = _parse_ts(row["updated_at"])
        if updated_at is None:
            return {"ok": False, "reason": "exit_intent_state_invalid"}, False
        age = current.astimezone(KST) - updated_at
        if age < _LEASE:
            return {
                "ok": False, "reason": "exit_intent_reserved",
                "prior_pilot_id": row["pilot_id"],
            }, False
        return {
            "ok": False, "reason": "exit_intent_reconcile_required",
            "prior_pilot_id": row["pilot_id"],
            "prior_decision_ref": prior_ref,
            "prior_updated_at": row["updated_at"],
        }, False

    return _mutate(mutate)


def takeover_exit_intent(
    decision_ref: str,
    expected_pilot_id: str,
    pilot_id: str,
    *,
    expected_decision_ref: str,
    expected_updated_at: str,
    now: datetime | None = None,
) -> dict:
    current = now or datetime.now(KST)

    def mutate(state: dict) -> tuple[dict, bool]:
        if (
            not is_exit_decision_ref(decision_ref)
            or not is_exit_decision_ref(expected_decision_ref)
            or _intent_scope(decision_ref) != _intent_scope(expected_decision_ref)
        ):
            return {"ok": False, "reason": "exit_intent_takeover_conflict"}, False
        intents = state["intents"]
        row = intents.get(expected_decision_ref)
        if type(row) is not dict or row.get("status") != "reserved":
            return {"ok": False, "reason": "exit_intent_takeover_conflict"}, False
        if (
            row.get("pilot_id") != expected_pilot_id
            or row.get("updated_at") != expected_updated_at
        ):
            return {"ok": False, "reason": "exit_intent_takeover_conflict"}, False
        updated_at = _parse_ts(row.get("updated_at"))
        if updated_at is None or current.astimezone(KST) - updated_at < _LEASE:
            return {"ok": False, "reason": "exit_intent_takeover_conflict"}, False
        if expected_decision_ref != decision_ref:
            del intents[expected_decision_ref]
            intents[decision_ref] = row
        row.update({"pilot_id": pilot_id, "updated_at": _iso(current)})
        return {"ok": True, "reason": "exit_intent_taken_over"}, True

    return _mutate(mutate)


def mark_exit_intent_sent(
    decision_ref: str,
    pilot_id: str,
    *,
    now: datetime | None = None,
) -> dict:
    current = now or datetime.now(KST)

    def mutate(state: dict) -> tuple[dict, bool]:
        row = state["intents"].get(decision_ref)
        if type(row) is not dict or row.get("pilot_id") != pilot_id:
            return {"ok": False, "reason": "exit_intent_owner_mismatch"}, False
        row.update({"status": "sent", "updated_at": _iso(current)})
        return {"ok": True, "reason": "exit_intent_sent"}, True

    return _mutate(mutate)


def release_exit_intent(decision_ref: str, pilot_id: str) -> dict:
    def mutate(state: dict) -> tuple[dict, bool]:
        row = state["intents"].get(decision_ref)
        if type(row) is not dict or row.get("pilot_id") != pilot_id:
            return {"ok": False, "reason": "exit_intent_owner_mismatch"}, False
        del state["intents"][decision_ref]
        return {"ok": True, "reason": "exit_intent_released"}, True

    return _mutate(mutate)
