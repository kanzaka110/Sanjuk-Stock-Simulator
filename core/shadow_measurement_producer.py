"""Best-effort shadow producer for final Toss candidate cohorts.

Importing this module is side-effect free. Persistent stores are constructed lazily
only by persistence workers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence


FEATURE_SET_VERSION = "toss_final_candidate_v2_dual_income_ev"
SOURCE_NAME = "toss_final_candidate"
_MAX_COHORT_SIZE = 10
_MAX_TEXT_CHARS = 2048
_MAX_LIST_ITEMS = 64
_MAX_CANDIDATE_BYTES = 64 * 1024
_MAX_COHORT_BYTES = 256 * 1024
_MAX_INTEGER_BITS = 63
_SOURCE_SNAPSHOT_RE = re.compile(r"^srcobs_[0-9a-f]{64}$")
log = logging.getLogger(__name__)
_WORKER_LOCK = threading.Lock()


@dataclass(frozen=True)
class SanitizedFinalCandidate:
    """Immutable worker input containing no reference to a raw candidate."""

    payload_json: str
    candidate_snapshot_sha256: str


@dataclass(frozen=True)
class SanitizedFinalCandidateCohort:
    """Bounded immutable batch captured at the provider-return boundary."""

    candidates: tuple[SanitizedFinalCandidate, ...]
    captured_at_utc: datetime
    market_scope: str
    fallback_used: bool
    rejected_count: int = 0


_BUCKETS = frozenset(
    {
        "PASS_EXECUTE",
        "SMALL_PASS",
        "WAIT_PULLBACK",
        "WATCH",
        "CHASE_BLOCK",
        "BLOCK",
    }
)
_MARKETS = frozenset({"KR", "US"})
_MARKET_SCOPES = frozenset({"KR", "US", "ALL"})
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_KR_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(?:KS|KQ)$")
_BROKER_ACCOUNT_NUMBER_RE = re.compile(r"(?<![0-9])[0-9]{8}-[0-9]{2}(?![0-9])")
_STRING_BOOL_VALUES = frozenset({"true", "false"})

_SCORE_NUMERIC_FIELDS = (
    "score_total",
    "score_momentum",
    "score_liquidity",
    "score_risk_reward",
    "score_reliability",
    "score_market_regime",
    "score_supply_demand",
    "penalty_overheat",
    "penalty_duplicate",
    "penalty_event_risk",
    "rr_ratio",
    "decision_change_pct",
)
_SCORE_INTEGER_FIELDS = (
    "score_schema_version",
    "decision_days_to_earnings",
)
_SCORE_BOOL_FIELDS = (
    "decision_has_stop",
    "decision_has_target",
)
_SCORE_TEXT_FIELDS = (
    "decision_bucket",
    "decision_reason",
    "regime",
    "score_symbol",
    "score_side",
    "decision_origin_bucket",
    "decision_origin_reason",
    "weight_profile_hash",
    "score_breakdown_sha256",
    "candidate_snapshot_sha256",
)
_SCORE_LIST_FIELDS = (
    "risk_flags",
    "decision_blocking_risk_flags",
)
_FINAL_BOOL_FIELDS = (
    "stock_agent_ready",
    "executable_now",
    "limit_exceeded",
    "rebalance_required",
    "ai_berkshire_buy_block",
)
_FINAL_TEXT_FIELDS = (
    "execution_status",
    "decision_reason",
    "block_reason",
    "ai_berkshire_research_status",
)
_FINAL_LIST_FIELDS = ("missing_fields", "blocking_risk_flags", "observation_flags")
_INCOME_BOOL_FIELDS = (
    "income_pass",
    "decision_breakeven_reachable",
    "decision_residual_mark_to_market_included",
)
_INCOME_TEXT_FIELDS = (
    "income_grade",
    "income_block_reason",
    "income_block_label",
    "expected_pnl_model",
    "expected_pnl_scope",
    "decision_expected_pnl_model",
    "decision_expected_pnl_scope",
)
_INCOME_NUMERIC_FIELDS = (
    "expected_pnl_krw",
    "income_edge_ratio",
    "decision_expected_pnl_krw",
    "decision_income_edge_ratio",
    "decision_breakeven_win_rate",
    "decision_upside_krw",
    "decision_loss_krw",
    "decision_profit_exit_pct",
    "decision_profit_exit_quantity",
    "decision_loss_exit_pct",
    "decision_loss_exit_quantity",
    "decision_residual_quantity_after_profit",
)


def canonical_json(value: Any) -> str:
    """Serialize with the shared canonical JSON contract."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def candidate_snapshot_sha256(projection: Mapping[str, Any]) -> str:
    """Return the full SHA-256 of a sanitized candidate projection."""
    if not isinstance(projection, Mapping):
        raise ValueError("candidate_projection_invalid")
    return hashlib.sha256(canonical_json(projection).encode("utf-8")).hexdigest()


def _finite_number(name: str, value: Any) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name}_invalid")
    if type(value) is int and value.bit_length() > _MAX_INTEGER_BITS:
        raise ValueError(f"{name}_out_of_range")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name}_non_finite")
    return number


def _text(name: str, value: Any) -> str:
    if type(value) is not str:
        raise ValueError(f"{name}_invalid")
    if len(value) > _MAX_TEXT_CHARS:
        raise ValueError(f"{name}_too_long")
    if value.strip().lower() in _STRING_BOOL_VALUES:
        raise ValueError(f"{name}_string_boolean")
    return value


def _bool(name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name}_invalid")
    return value


def _string_list(name: str, value: Any) -> list[str]:
    if type(value) not in (list, tuple):
        raise ValueError(f"{name}_invalid")
    if len(value) > _MAX_LIST_ITEMS:
        raise ValueError(f"{name}_too_many_items")
    return [_text(name, item) for item in value]


def _validate_projection_contract(projection: dict[str, Any]) -> None:
    # This private dependency is intentional: the shadow store owns the 0A JSON
    # safety contract, so the producer must not drift by reimplementing it.
    # Importing the store module is side-effect free and constructs no database.
    from core.shadow_measurements import ShadowMeasurementStore

    ShadowMeasurementStore._validate_json_contract(projection)
    _reject_broker_account_numbers(projection)


def _reject_broker_account_numbers(value: Any, path: str = "$") -> None:
    """Reject the broker's naked 8-digit/2-digit account form in free text."""
    if isinstance(value, dict):
        for key, nested in value.items():
            _reject_broker_account_numbers(nested, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_broker_account_numbers(nested, f"{path}[{index}]")
        return
    if isinstance(value, str) and _BROKER_ACCOUNT_NUMBER_RE.search(value):
        raise ValueError(f"broker_account_number:{path}")


def _score_proof(candidate: Mapping[str, Any]) -> dict[str, Any]:
    raw = candidate.get("quality_breakdown")
    if raw is None:
        return {}
    if type(raw) is not dict:
        raise ValueError("quality_breakdown_invalid")
    proof: dict[str, Any] = {}
    for name in _SCORE_NUMERIC_FIELDS:
        if name in raw:
            proof[name] = _finite_number(name, raw[name])
    for name in _SCORE_INTEGER_FIELDS:
        if name in raw:
            value = raw[name]
            if type(value) is not int or value.bit_length() > _MAX_INTEGER_BITS:
                raise ValueError(f"{name}_invalid")
            proof[name] = value
    for name in _SCORE_BOOL_FIELDS:
        if name in raw:
            proof[name] = _bool(name, raw[name])
    for name in _SCORE_TEXT_FIELDS:
        if name in raw:
            proof[name] = _text(name, raw[name])
    for name in _SCORE_LIST_FIELDS:
        if name in raw:
            proof[name] = _string_list(name, raw[name])
    return proof


def _final_state(candidate: Mapping[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for name in _FINAL_BOOL_FIELDS:
        if name in candidate:
            state[name] = _bool(name, candidate[name])
    for name in _FINAL_TEXT_FIELDS:
        if name in candidate:
            value = candidate[name]
            state[name] = None if value is None else _text(name, value)
    for name in _FINAL_LIST_FIELDS:
        if name in candidate:
            state[name] = _string_list(name, candidate[name])

    raw_income = candidate.get("income_strategy")
    if raw_income is not None:
        if type(raw_income) is not dict:
            raise ValueError("income_strategy_invalid")
        income: dict[str, Any] = {}
        for name in _INCOME_BOOL_FIELDS:
            if name in raw_income:
                income[name] = _bool(name, raw_income[name])
        for name in _INCOME_TEXT_FIELDS:
            if name in raw_income:
                value = raw_income[name]
                income[name] = None if value is None else _text(name, value)
        for name in _INCOME_NUMERIC_FIELDS:
            if name in raw_income:
                value = raw_income[name]
                income[name] = None if value is None else _finite_number(name, value)
        if income:
            state["income_strategy"] = income
    return state


def project_final_candidate(
    candidate: Mapping[str, Any],
    *,
    cohort_position: int,
    cohort_size: int,
    market_scope: str,
    fallback_used: bool = False,
) -> dict[str, Any]:
    """Return the deterministic, allowlisted projection of one final candidate."""
    if type(candidate) is not dict:
        raise ValueError("candidate_invalid")
    if type(cohort_size) is not int or not 1 <= cohort_size <= 10:
        raise ValueError("cohort_size_invalid")
    if (
        type(cohort_position) is not int
        or cohort_position < 0
        or cohort_position >= cohort_size
    ):
        raise ValueError("cohort_position_invalid")
    if (
        type(market_scope) is not str
        or len(market_scope) > 3
        or market_scope not in _MARKET_SCOPES
    ):
        raise ValueError("market_scope_invalid")
    fallback = _bool("fallback_used", fallback_used)

    raw_symbol = candidate.get("symbol")
    if type(raw_symbol) is not str or len(raw_symbol) > 32:
        raise ValueError("symbol_invalid")
    symbol = raw_symbol.strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol_invalid")

    raw_side = candidate.get("side")
    if (
        type(raw_side) is not str
        or len(raw_side) > 16
        or raw_side.strip().lower() not in {"buy", "sell"}
    ):
        raise ValueError("side_invalid")
    side = raw_side.strip().upper()

    raw_market = candidate.get("market")
    if type(raw_market) is not str or len(raw_market) > 3 or raw_market not in _MARKETS:
        raise ValueError("market_invalid")
    market = raw_market
    if market_scope != "ALL" and market != market_scope:
        raise ValueError("market_scope_mismatch")

    raw_currency = candidate.get("currency")
    if (
        type(raw_currency) is not str
        or len(raw_currency) > 3
        or raw_currency not in {"KRW", "USD"}
    ):
        raise ValueError("currency_invalid")
    currency = raw_currency
    if currency != {"KR": "KRW", "US": "USD"}[market]:
        raise ValueError("market_currency_mismatch")
    kr_symbol = _KR_SYMBOL_RE.fullmatch(symbol) is not None
    if (market == "KR" and not kr_symbol) or (
        market == "US" and (kr_symbol or (symbol.isdigit() and len(symbol) == 6))
    ):
        raise ValueError("symbol_market_mismatch")

    bucket = candidate.get("decision_bucket")
    if type(bucket) is not str or len(bucket) > 32 or bucket not in _BUCKETS:
        raise ValueError("decision_bucket_invalid")

    proof = _score_proof(candidate)
    raw_score = candidate.get("quality_score")
    if raw_score is None:
        raw_score = proof.get("score_total")
    score = _finite_number("quality_score", raw_score)

    projection = {
        "feature_set_version": FEATURE_SET_VERSION,
        "symbol": symbol,
        "side": side,
        "market": market,
        "currency": currency,
        "production_bucket": bucket,
        "production_score": score,
        "score_proof": proof,
        "final_state": _final_state(candidate),
        "cohort_position": cohort_position,
        "cohort_size": cohort_size,
        "market_scope": market_scope,
        "fallback_used": fallback,
    }
    _validate_projection_contract(projection)
    return projection


def _utc_datetime(name: str, value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name}_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def sanitize_final_candidate_cohort(
    candidates: Sequence[Mapping[str, Any]],
    *,
    market_scope: str,
    captured_at_utc: datetime,
    fallback_used: bool,
) -> SanitizedFinalCandidateCohort:
    """Project at most ten candidates into immutable canonical-JSON records."""
    if type(candidates) not in (list, tuple):
        raise ValueError("candidates_invalid")
    captured = _utc_datetime("captured_at_utc", captured_at_utc)
    if (
        type(market_scope) is not str
        or len(market_scope) > 3
        or market_scope not in _MARKET_SCOPES
    ):
        raise ValueError("market_scope_invalid")
    fallback = _bool("fallback_used", fallback_used)

    bounded = list(candidates[:_MAX_COHORT_SIZE])
    if not bounded:
        return SanitizedFinalCandidateCohort((), captured, market_scope, fallback)

    sanitized: list[SanitizedFinalCandidate] = []
    rejected = 0
    cohort_bytes = 0
    for position, candidate in enumerate(bounded):
        try:
            projection = project_final_candidate(
                candidate,
                cohort_position=position,
                cohort_size=len(bounded),
                market_scope=market_scope,
                fallback_used=fallback,
            )
            payload_json = canonical_json(projection)
            payload_bytes = payload_json.encode("utf-8")
            if len(payload_bytes) > _MAX_CANDIDATE_BYTES:
                raise ValueError("candidate_payload_too_large")
            if cohort_bytes + len(payload_bytes) > _MAX_COHORT_BYTES:
                raise ValueError("cohort_payload_too_large")
            payload_hash = hashlib.sha256(payload_bytes).hexdigest()
            sanitized.append(
                SanitizedFinalCandidate(
                    payload_json=payload_json,
                    candidate_snapshot_sha256=payload_hash,
                )
            )
            cohort_bytes += len(payload_bytes)
        except Exception as exc:
            rejected += 1
            log.warning(
                "final cohort projection failed: error_type=%s failed_count=%d",
                type(exc).__name__,
                rejected,
            )
    return SanitizedFinalCandidateCohort(
        candidates=tuple(sanitized),
        captured_at_utc=captured,
        market_scope=market_scope,
        fallback_used=fallback,
        rejected_count=rejected,
    )


def decision_id_for_source_snapshot(source_snapshot_id: str) -> str:
    """Bind a shadow decision identity to one exact source snapshot."""
    if (
        type(source_snapshot_id) is not str
        or not _SOURCE_SNAPSHOT_RE.fullmatch(source_snapshot_id)
    ):
        raise ValueError("source_snapshot_id_invalid")
    identity = {
        "feature_set_version": FEATURE_SET_VERSION,
        "source_snapshot_id": source_snapshot_id,
    }
    return "tossq_" + hashlib.sha256(
        canonical_json(identity).encode("utf-8")
    ).hexdigest()


def _default_source_store():
    from config.settings import DB_DIR
    from core.source_observations import SourceObservationStore

    return SourceObservationStore(DB_DIR / "source_observations.db")


def _default_shadow_store():
    from config.settings import DB_DIR
    from core.shadow_measurements import ShadowMeasurementStore

    return ShadowMeasurementStore(DB_DIR / "shadow_measurements.db")


def _decoded_projection(candidate: SanitizedFinalCandidate) -> dict[str, Any]:
    if not isinstance(candidate, SanitizedFinalCandidate):
        raise ValueError("sanitized_candidate_invalid")
    payload = json.loads(candidate.payload_json)
    if not isinstance(payload, dict):
        raise ValueError("sanitized_payload_invalid")
    if canonical_json(payload) != candidate.payload_json:
        raise ValueError("sanitized_payload_not_canonical")
    observed_hash = hashlib.sha256(candidate.payload_json.encode("utf-8")).hexdigest()
    if observed_hash != candidate.candidate_snapshot_sha256:
        raise ValueError("sanitized_payload_hash_mismatch")
    return payload


def persist_final_candidate_cohort(
    batch: SanitizedFinalCandidateCohort,
    *,
    source_store=None,
    shadow_store=None,
) -> dict[str, int]:
    """Append source first, then a shadow decision for that exact snapshot."""
    if not isinstance(batch, SanitizedFinalCandidateCohort):
        raise ValueError("sanitized_cohort_invalid")
    captured = _utc_datetime("captured_at_utc", batch.captured_at_utc)
    fallback = _bool("fallback_used", batch.fallback_used)
    seen = len(batch.candidates)
    if not seen:
        return {"seen": 0, "persisted": 0, "failed": 0}

    resolved_source = source_store
    if resolved_source is None:
        try:
            resolved_source = _default_source_store()
        except Exception as exc:
            log.warning(
                "final cohort source store failed: error_type=%s failed_count=%d",
                type(exc).__name__,
                seen,
            )
            return {"seen": seen, "persisted": 0, "failed": seen}

    resolved_shadow = shadow_store
    persisted = 0
    failed = 0
    for sanitized in batch.candidates:
        try:
            payload = _decoded_projection(sanitized)
            observation = resolved_source.append(
                source=SOURCE_NAME,
                source_record_id=sanitized.candidate_snapshot_sha256,
                symbol=payload["symbol"],
                market=payload["market"],
                currency=payload["currency"],
                source_as_of=captured,
                ingested_at=captured,
                schema_version=1,
                fallback_used=fallback,
                payload=payload,
            )
        except Exception as exc:
            failed += 1
            log.warning(
                "final cohort source append failed: error_type=%s failed_count=%d",
                type(exc).__name__,
                failed,
            )
            continue

        try:
            if resolved_shadow is None:
                resolved_shadow = _default_shadow_store()
            decision_id = decision_id_for_source_snapshot(observation.snapshot_id)
            resolved_shadow.append_decision(
                decision_id=decision_id,
                decision_ref=f"shadow:{decision_id}",
                symbol=payload["symbol"],
                side=payload["side"],
                decided_at_utc=captured,
                production_bucket=payload["production_bucket"],
                production_score=payload["production_score"],
                feature_set_version=FEATURE_SET_VERSION,
                features=payload,
                source_snapshots=[
                    {
                        "snapshot_id": observation.snapshot_id,
                        "source": SOURCE_NAME,
                        "ingested_at_utc": captured,
                        "payload_sha256": observation.payload_sha256,
                    }
                ],
                candidate_snapshot_sha256=sanitized.candidate_snapshot_sha256,
            )
            persisted += 1
        except Exception as exc:
            failed += 1
            log.warning(
                "final cohort shadow append failed: error_type=%s failed_count=%d",
                type(exc).__name__,
                failed,
            )
    return {"seen": seen, "persisted": persisted, "failed": failed}


def _persist_worker(
    batch: SanitizedFinalCandidateCohort,
    source_store,
    shadow_store,
    observation_consumer: Callable[[tuple[str, ...]], Any] | None,
) -> None:
    try:
        try:
            persist_final_candidate_cohort(
                batch,
                source_store=source_store,
                shadow_store=shadow_store,
            )
        except Exception as exc:
            log.warning(
                "final cohort worker failed: error_type=%s failed_count=%d",
                type(exc).__name__,
                len(batch.candidates),
            )
        if observation_consumer is not None:
            try:
                symbols = tuple(
                    dict.fromkeys(
                        payload["symbol"]
                        for candidate in batch.candidates
                        for payload in (json.loads(candidate.payload_json),)
                        if payload.get("market") == "KR"
                    )
                )
                if symbols:
                    observation_consumer(symbols)
            except Exception as exc:
                log.warning(
                    "market observation consumer failed: error_type=%s failed_count=%d",
                    type(exc).__name__,
                    len(batch.candidates),
                )
    finally:
        _WORKER_LOCK.release()


def enqueue_sanitized_final_candidate_cohort(
    batch: SanitizedFinalCandidateCohort,
    *,
    source_store=None,
    shadow_store=None,
    observation_consumer: Callable[[tuple[str, ...]], Any] | None = None,
) -> bool:
    """Enqueue an already bounded immutable batch without touching raw candidates."""
    if type(batch) is not SanitizedFinalCandidateCohort:
        raise ValueError("sanitized_cohort_invalid")
    if not batch.candidates:
        return False
    if not _WORKER_LOCK.acquire(blocking=False):
        return False
    try:
        worker = threading.Thread(
            target=_persist_worker,
            args=(batch, source_store, shadow_store, observation_consumer),
            daemon=True,
            name="toss-final-cohort-shadow",
        )
        worker.start()
    except Exception as exc:
        _WORKER_LOCK.release()
        log.warning(
            "final cohort worker start failed: error_type=%s failed_count=%d",
            type(exc).__name__,
            len(batch.candidates),
        )
        return False
    return True


def enqueue_final_candidate_cohort(
    candidates: Sequence[Mapping[str, Any]],
    *,
    market_scope: str,
    captured_at_utc: datetime,
    fallback_used: bool,
    source_store=None,
    shadow_store=None,
    observation_consumer: Callable[[tuple[str, ...]], Any] | None = None,
) -> bool:
    """Compatibility wrapper that sanitizes raw input before enqueueing."""
    batch = sanitize_final_candidate_cohort(
        candidates,
        market_scope=market_scope,
        captured_at_utc=captured_at_utc,
        fallback_used=fallback_used,
    )
    return enqueue_sanitized_final_candidate_cohort(
        batch,
        source_store=source_store,
        shadow_store=shadow_store,
        observation_consumer=observation_consumer,
    )


def _market_observation_consumer():
    try:
        from core.kr_market_observation_collector import (
            enqueue_candidate_observation_cycle,
        )

        return enqueue_candidate_observation_cycle
    except Exception as exc:
        log.warning(
            "market observation consumer import failed: error_type=%s",
            type(exc).__name__,
        )
        return None


def enqueue_sanitized_final_candidate_cohort_with_market_observations(
    batch: SanitizedFinalCandidateCohort,
    *,
    source_store=None,
    shadow_store=None,
) -> bool:
    """Production adapter for a provider-boundary sanitized immutable batch."""
    return enqueue_sanitized_final_candidate_cohort(
        batch,
        source_store=source_store,
        shadow_store=shadow_store,
        observation_consumer=_market_observation_consumer(),
    )


def enqueue_final_candidate_cohort_with_market_observations(
    candidates: Sequence[Mapping[str, Any]],
    *,
    market_scope: str,
    captured_at_utc: datetime,
    fallback_used: bool,
    source_store=None,
    shadow_store=None,
) -> bool:
    """Compatibility adapter for callers that still supply raw candidates."""
    return enqueue_final_candidate_cohort(
        candidates,
        market_scope=market_scope,
        captured_at_utc=captured_at_utc,
        fallback_used=fallback_used,
        source_store=source_store,
        shadow_store=shadow_store,
        observation_consumer=_market_observation_consumer(),
    )
