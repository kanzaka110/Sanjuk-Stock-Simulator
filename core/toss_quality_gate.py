"""core/toss_quality_gate.py

Toss 자동매매 품질 게이트 — 다차원 점수화 + decision_bucket 결정.

[구조]
  score_candidate()  : 단일 후보 점수 계산 → QualityScore
  score_candidates_batch() : 배치 처리 (regime 1회 캐시)
  _decide_bucket()   : 점수+RR+국면 기반 실행 판정

[decision_bucket]
  PASS_EXECUTE    — 자동 주문 가능
  SMALL_PASS      — 소액 자동 주문 (1주/최소 금액, 위기장·점수 보통 허용)
  WAIT_PULLBACK   — 눌림목 대기 (RR 보통 또는 실적 임박)
  WATCH           — 관찰만 (약세장/점수 부족)
  CHASE_BLOCK     — 급등 추격 차단
  BLOCK           — 주문 불가 (손절 없음/RR 부족/데이터 이상)

[안전]
- 기존 자동주문 경로 변경 없음
- discovery_candidates._gate()/_score() 유지
- toss_autonomous_finalizer.py 변경 없음
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_CLIENT_ORDER_ID_RE = re.compile(r"^tlive_[A-Za-z0-9_-]{1,30}$")
_DECISION_REF_RE = re.compile(r"^(?:prediction|execution_decision):[A-Za-z0-9._:-]{1,140}$")

# ── decision buckets ─────────────────────────────────────────────
PASS_EXECUTE = "PASS_EXECUTE"
SMALL_PASS = "SMALL_PASS"
WAIT_PULLBACK = "WAIT_PULLBACK"
WATCH = "WATCH"
CHASE_BLOCK = "CHASE_BLOCK"
BLOCK = "BLOCK"

# PASS_EXECUTE + SMALL_PASS → 자동주문 가능
EXECUTABLE_BUCKETS = frozenset([PASS_EXECUTE, SMALL_PASS])

_ALL_BUCKETS = frozenset([PASS_EXECUTE, SMALL_PASS, WAIT_PULLBACK, WATCH, CHASE_BLOCK, BLOCK])


# ── QualityScore ─────────────────────────────────────────────────

@dataclass(frozen=True)
class QualityScore:
    ticker: str
    score_total: float
    score_momentum: float
    score_liquidity: float
    score_risk_reward: float
    score_reliability: float
    score_market_regime: float
    penalty_overheat: float
    penalty_duplicate: float
    penalty_event_risk: float
    risk_flags: tuple
    decision_bucket: str
    decision_reason: str
    rr_ratio: float
    regime: str
    scored_at: str
    score_supply_demand: float = 0.0  # KRX 기관/외국인 수급 보정 (-10 ~ +10)

    def to_dict(self) -> dict:
        return {
            "score_total": round(self.score_total, 1),
            "score_momentum": round(self.score_momentum, 1),
            "score_liquidity": round(self.score_liquidity, 1),
            "score_risk_reward": round(self.score_risk_reward, 1),
            "score_reliability": round(self.score_reliability, 1),
            "score_market_regime": round(self.score_market_regime, 1),
            "score_supply_demand": round(self.score_supply_demand, 1),
            "penalty_overheat": round(self.penalty_overheat, 1),
            "penalty_duplicate": round(self.penalty_duplicate, 1),
            "penalty_event_risk": round(self.penalty_event_risk, 1),
            "risk_flags": list(self.risk_flags),
            "decision_bucket": self.decision_bucket,
            "decision_reason": self.decision_reason,
            "rr_ratio": round(self.rr_ratio, 2),
            "regime": self.regime,
        }


# ── 점수 가중치 (자동 캘리브레이션 구조, P3-4) ────────────────────
#
# 기본 1.0. db/data/quality_gate_weights.json이 있으면 override (0.5~1.5 clamp).
# suggest_weight_calibration()은 outcome 30건+ 누적 시 제안 파일만 생성 —
# 자동 적용하지 않음 (승호가 검토 후 weights 파일로 복사해야 반영).

_DEFAULT_WEIGHTS: dict[str, float] = {
    "momentum": 1.0,
    "liquidity": 1.0,
    "risk_reward": 1.0,
    "reliability": 1.0,
    "market_regime": 1.0,
    "supply_demand": 1.0,
}

_WEIGHT_MIN, _WEIGHT_MAX = 0.5, 1.5

# 각 sub-score 만점 (캘리브레이션 정규화용)
_DIM_MAX: dict[str, float] = {
    "momentum": 25.0,
    "liquidity": 25.0,
    "risk_reward": 20.0,
    "reliability": 15.0,
    "market_regime": 15.0,
}

_weights_cache: dict = {"mtime": None, "weights": dict(_DEFAULT_WEIGHTS)}


def _weights_path() -> Path:
    return _outcomes_db_path().parent / "quality_gate_weights.json"


def get_score_weights() -> dict[str, float]:
    """현재 점수 가중치. 파일 없으면 기본 1.0, 값은 0.5~1.5로 clamp."""
    p = _weights_path()
    try:
        if not p.exists():
            return dict(_DEFAULT_WEIGHTS)
        mtime = p.stat().st_mtime
        if _weights_cache["mtime"] == mtime:
            return dict(_weights_cache["weights"])
        import json
        raw = json.loads(p.read_text(encoding="utf-8"))
        weights = dict(_DEFAULT_WEIGHTS)
        for k in weights:
            try:
                v = float(raw.get(k, 1.0))
            except (TypeError, ValueError):
                v = 1.0
            weights[k] = max(_WEIGHT_MIN, min(_WEIGHT_MAX, v))
        _weights_cache["mtime"] = mtime
        _weights_cache["weights"] = dict(weights)
        return weights
    except Exception as exc:
        log.debug("weights load failed: error_type=%s", type(exc).__name__)
        return dict(_DEFAULT_WEIGHTS)


def suggest_weight_calibration(min_outcomes: int = 30) -> dict:
    """outcome 누적 기반 가중치 제안 (자동 적용 안 함).

    win/loss 그룹의 sub-score 평균 차이를 만점으로 정규화해
    1.0 ± 0.5 범위 제안. 제안은 quality_gate_weights_suggestion.json에 기록.
    """
    with _outcomes_lock:
        conn = _outcomes_conn()
        try:
            rows = conn.execute(
                "SELECT outcome, score_momentum, score_liquidity, "
                "score_risk_reward, score_reliability, score_market_regime "
                "FROM quality_gate_decisions WHERE outcome IN ('win','loss')"
            ).fetchall()
        finally:
            conn.close()

    if len(rows) < min_outcomes:
        return {"ok": False, "reason": "insufficient_outcomes",
                "evaluated": len(rows), "required": min_outcomes}

    cols = {
        "momentum": "score_momentum",
        "liquidity": "score_liquidity",
        "risk_reward": "score_risk_reward",
        "reliability": "score_reliability",
        "market_regime": "score_market_regime",
    }
    wins = [r for r in rows if r["outcome"] == "win"]
    losses = [r for r in rows if r["outcome"] == "loss"]
    if not wins or not losses:
        return {"ok": False, "reason": "need_both_win_and_loss",
                "wins": len(wins), "losses": len(losses)}

    suggested = dict(_DEFAULT_WEIGHTS)
    detail: dict[str, dict] = {}
    for dim, col in cols.items():
        win_avg = sum(float(r[col] or 0) for r in wins) / len(wins)
        loss_avg = sum(float(r[col] or 0) for r in losses) / len(losses)
        diff_norm = (win_avg - loss_avg) / _DIM_MAX[dim]
        w = max(_WEIGHT_MIN, min(_WEIGHT_MAX, 1.0 + diff_norm * 0.5))
        suggested[dim] = round(w, 3)
        detail[dim] = {"win_avg": round(win_avg, 2),
                       "loss_avg": round(loss_avg, 2),
                       "suggested_weight": round(w, 3)}

    result = {
        "ok": True,
        "evaluated": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "suggested_weights": suggested,
        "detail": detail,
        "generated_at": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "note": "자동 적용 안 됨 — 검토 후 quality_gate_weights.json으로 복사 시 반영",
    }
    try:
        import json
        p = _weights_path().parent / "quality_gate_weights_suggestion.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as exc:
        log.debug(
            "weight suggestion save failed: error_type=%s",
            type(exc).__name__,
        )
    return result


# ── 점수 계산 ────────────────────────────────────────────────────

def _score_momentum(candidate: dict) -> float:
    """기술 지표 모멘텀 점수 (0-25).

    대시보드 GET 경로에서는 신규 발굴 스캐너가 이미 계산한 candidate.score를
    우선 사용한다. 종목별 calculate_indicators()는 yfinance/pykrx 조회가 섞여
    /api/toss/buy-candidates를 45초 이상 막을 수 있으므로 score가 없을 때만
    보조 경로로 호출한다.
    """
    score = candidate.get("score", 0)
    try:
        score_f = float(score or 0)
    except Exception:
        score_f = 0.0
    if score_f > 0:
        return min(25.0, max(0.0, score_f / 4))

    ticker = candidate.get("symbol", "")
    try:
        from core.indicators import calculate_indicators
        result = calculate_indicators(ticker)
        if result:
            # confluence_score: -4 ~ +4 → 0 ~ 25
            return max(0.0, min(25.0, (result.confluence_score + 4) / 8 * 25))
    except Exception as exc:
        log.debug("momentum score fallback: error_type=%s", type(exc).__name__)
    return 0.0


def _score_liquidity(candidate: dict) -> float:
    """유동성 점수 (0-25)."""
    market = candidate.get("market", "KR")
    if market == "KR":
        base = 30_000_000_000  # 300억
    else:
        base = 2_000_000_000  # $2B
    volume_value = float(candidate.get("volume_value", 0) or 0)
    if volume_value <= 0:
        # volume_value 없으면 price * volume 추정
        price = float(candidate.get("price", 0) or 0)
        volume = float(candidate.get("volume", 0) or 0)
        volume_value = price * volume
    if volume_value <= 0:
        return 5.0  # 데이터 없으면 최소 점수
    return min(25.0, volume_value / base * 12.5)


def _score_risk_reward(candidate: dict) -> float:
    """손익비 점수 (0-20)."""
    rr = float(candidate.get("risk_reward", 0) or 0)
    return min(20.0, max(0.0, (rr - 1.0) * 15))


def _score_reliability(ticker: str, accuracy_stats: dict | None = None) -> float:
    """신뢰도 점수 (0-15). memory.py accuracy_stats 기반."""
    if accuracy_stats is None:
        try:
            from core.memory import get_accuracy_summary
            accuracy_stats = get_accuracy_summary()
        except Exception:
            return 7.5  # 조회 실패 → 중립

    stats = accuracy_stats.get(ticker, {})
    evaluated = int(stats.get("evaluated_count", 0))
    if evaluated < 5:
        return 7.5  # 표본 부족 → 중립 (감점 금지)

    win_rate = float(stats.get("win_rate", 50))
    expectancy = float(stats.get("expectancy", 0))

    # win_rate 0-100% → 0-10
    wr_score = min(10.0, win_rate / 10)
    # expectancy > 0 → +5, < 0 → 0
    exp_score = 5.0 if expectancy > 0 else 0.0

    return min(15.0, wr_score + exp_score)


def _score_market_regime(regime_obj) -> float:
    """시장 국면 점수 (0-15)."""
    if regime_obj is None:
        return 10.0  # 조회 실패 → 중립
    regime = getattr(regime_obj, "regime", "")
    risk_adj = getattr(regime_obj, "risk_adjustment", "")
    if regime == "강세장":
        return 15.0
    if regime == "횡보장":
        return 10.0
    if regime == "약세장":
        return 5.0
    if regime == "위기":
        return 0.0
    return 10.0  # 판단불가 → 중립


def _penalty_overheat(candidate: dict) -> float:
    """과열 감점 (≤0)."""
    change_pct = abs(float(candidate.get("change_pct", 0) or 0))
    range_pct = float(candidate.get("intraday_range_pct", 0) or 0)
    penalty = 0.0
    if change_pct >= 8:
        penalty -= min(15.0, (change_pct - 8) * 2)
    if range_pct >= 10:
        penalty -= 5.0
    return penalty


def _penalty_duplicate(candidate: dict) -> float:
    """중복 추천 감점 (≤0). candidate에 is_duplicate 정보 있으면 사용."""
    if candidate.get("is_duplicate") or candidate.get("penalty_duplicate"):
        return -20.0
    return 0.0


def _penalty_event_risk(ticker: str, pre_score: float) -> tuple[float, int]:
    """이벤트 리스크 감점 + days_to_earnings 반환."""
    if pre_score < 40:
        return 0.0, -1  # 저점수 후보는 API 호출 스킵

    try:
        from core.fundamentals import fetch_financial_data
        fd = fetch_financial_data(ticker)
        if fd is None:
            return 0.0, -1
        days = fd.days_to_earnings
        if 0 <= days <= 3:
            return -15.0, days
        if 4 <= days <= 7:
            return -5.0, days
        return 0.0, days
    except Exception as exc:
        log.debug("event risk check failed: error_type=%s", type(exc).__name__)
        return 0.0, -1


_SUPPLY_DEMAND_DAYS = 5


def _is_kr_ticker(ticker: str) -> bool:
    base = ticker.split(".")[0]
    return ticker.endswith((".KS", ".KQ")) or (base.isdigit() and len(base) == 6)


def _score_supply_demand(
    ticker: str,
    pre_score: float,
    fetch_budget: dict | None = None,
) -> float:
    """KRX 기관/외국인 수급 보정 (-10 ~ +10).

    최근 5일 순매매 금액(주식수×종가) 기준:
      외국인 순매수 → +5 / 순매도 → -5
      기관 순매수 → +5 / 순매도 → -5

    비용 관리:
    - 한국 종목(.KS/.KQ)만 조회
    - pre_score < 40 저점수 후보는 스킵 (event_risk와 동일 패턴)
    - fetch_budget 지정 시 미캐시 심볼 네트워크 조회 횟수 제한
      (대시보드 GET 배치 경로의 지연 방지 — _FRGN_CACHE는 프로세스 수명 캐시)
    - 조회 실패/데이터 없음 → 0.0 중립 (fail-safe)
    """
    if not _is_kr_ticker(ticker):
        return 0.0
    if pre_score < 40:
        return 0.0

    code = ticker.split(".")[0]
    try:
        from core.kr_market import _FRGN_CACHE, _fetch_naver_frgn

        if code not in _FRGN_CACHE and fetch_budget is not None:
            if fetch_budget.get("remaining", 0) <= 0:
                return 0.0
            fetch_budget["remaining"] -= 1

        rows = _fetch_naver_frgn(code)
    except Exception as exc:
        log.debug("supply/demand check failed: error_type=%s", type(exc).__name__)
        return 0.0

    if not rows:
        return 0.0

    recent = rows[:_SUPPLY_DEMAND_DAYS]
    try:
        frgn_net = sum(float(r["foreign_shares"]) * float(r["close"]) for r in recent)
        inst_net = sum(float(r["inst_shares"]) * float(r["close"]) for r in recent)
    except Exception:
        return 0.0

    score = 0.0
    if frgn_net > 0:
        score += 5.0
    elif frgn_net < 0:
        score -= 5.0
    if inst_net > 0:
        score += 5.0
    elif inst_net < 0:
        score -= 5.0
    return score


# ── Decision 엔진 ────────────────────────────────────────────────

def _decide_bucket(
    score_total: float,
    rr: float,
    regime: str,
    change_pct: float,
    has_stop: bool,
    has_target: bool,
    days_to_earnings: int,
    blocking_risk_flags: list | None = None,
) -> tuple[str, str]:
    """점수+RR+국면 기반 실행 판정.

    SMALL_PASS: 조건이 완벽하지는 않지만 1주/소액이면 손실 제한 가능한 후보.
    위기장에서도 RR 2.5+ / 손절 명확 → SMALL_PASS 허용 (전면 BLOCK 방지).
    """
    # 0. blocking risk flags → 무조건 차단
    if blocking_risk_flags:
        return BLOCK, f"리스크 차단: {blocking_risk_flags[0]}"

    # 1. 필수 조건 (완화 불가)
    if not has_stop or not has_target:
        return BLOCK, "손절/목표가 미설정"
    if rr < 1.2:
        return BLOCK, f"손익비 부족 ({rr:.1f}:1 < 1.2:1)"

    # 2. 급등 추격 (완화 불가)
    if abs(change_pct) >= 8.0:
        return CHASE_BLOCK, f"당일 급등 추격 차단 (+{change_pct:.1f}%)"

    # 3. 시장 위기 — 전면 BLOCK 대신 조건부 SMALL_PASS
    if regime == "위기":
        if rr >= 2.5:
            return SMALL_PASS, f"위기장 소액 허용 (RR {rr:.1f}:1 ≥ 2.5, 손절 명확)"
        return WATCH, f"위기장 — RR {rr:.1f}:1 부족 (2.5+ 필요)"

    # 4. 약세장 — 기준 상향, SMALL_PASS 가능
    if regime == "약세장":
        if rr >= 2.0:
            return SMALL_PASS, f"약세장 소액 허용 (RR {rr:.1f}:1 ≥ 2.0)"
        return WATCH, f"약세장 — RR {rr:.1f}:1 부족 (2.0+ 필요)"

    # 5. 실적 임박
    if 0 <= days_to_earnings <= 3:
        return WAIT_PULLBACK, f"실적 발표 {days_to_earnings}일 이내 — 대기"

    # 6. 총점 부족 → SMALL_PASS (RR 충분하면)
    if score_total < 45:
        if rr >= 1.8:
            return SMALL_PASS, f"총점 보통 ({score_total:.0f}/100) · 소액 허용 (RR {rr:.1f}:1)"
        return WATCH, f"총점 부족 ({score_total:.0f}/100)"

    # 7. RR 보통 → SMALL_PASS (눌림목 대기 대신 소액)
    if rr < 1.8:
        return SMALL_PASS, f"손익비 보통 ({rr:.1f}:1) — 소액 허용"

    # 8. 완전 통과
    return PASS_EXECUTE, "조건 충족"


# ── 메인 API ─────────────────────────────────────────────────────

def score_candidate(
    candidate: dict,
    regime_obj=None,
    accuracy_stats: dict | None = None,
    expensive_checks: bool = True,
    fetch_budget: dict | None = None,
) -> QualityScore:
    """단일 후보 품질 점수 계산."""
    ticker = candidate.get("symbol", "")
    rr = float(candidate.get("risk_reward", 0) or 0)
    change_pct = float(candidate.get("change_pct", 0) or 0)
    has_stop = bool(candidate.get("stop_loss"))
    has_target = bool(candidate.get("target_price"))

    # 각 sub-score (가중치: 기본 1.0, 캘리브레이션 파일 있으면 override)
    w = get_score_weights()
    s_momentum = _score_momentum(candidate) * w["momentum"]
    s_liquidity = _score_liquidity(candidate) * w["liquidity"]
    s_rr = _score_risk_reward(candidate) * w["risk_reward"]
    s_reliability = _score_reliability(ticker, accuracy_stats) * w["reliability"]
    s_regime = _score_market_regime(regime_obj) * w["market_regime"]

    p_overheat = _penalty_overheat(candidate)
    p_duplicate = _penalty_duplicate(candidate)

    # 이벤트 리스크: pre_score 계산 후 비용 관리.
    # GET 대시보드 배치 경로는 expensive_checks=False로 외부/느린 조회를 건너뛴다.
    pre_score = s_momentum + s_liquidity + s_rr + s_reliability + s_regime + p_overheat + p_duplicate
    if expensive_checks:
        p_event, days_to_earnings = _penalty_event_risk(ticker, pre_score)
    else:
        p_event, days_to_earnings = 0.0, -1

    # KRX 수급 보정: pre_score 게이트 + fetch_budget으로 비용 제한
    s_supply = _score_supply_demand(ticker, pre_score, fetch_budget=fetch_budget) \
        * w["supply_demand"]

    score_total = max(0.0, min(100.0,
        s_momentum + s_liquidity + s_rr + s_reliability + s_regime
        + s_supply + p_overheat + p_duplicate + p_event
    ))

    regime_str = getattr(regime_obj, "regime", "판단불가") if regime_obj else "판단불가"

    # risk_flags 집계
    flags = list(candidate.get("risk_flags", []))
    if candidate.get("blocking_risk_flags"):
        flags.extend(candidate["blocking_risk_flags"])

    # blocking_risk_flags
    blocking_flags = candidate.get("blocking_risk_flags") or []

    # decision
    bucket, reason = _decide_bucket(
        score_total, rr, regime_str, change_pct,
        has_stop, has_target, days_to_earnings,
        blocking_risk_flags=blocking_flags,
    )

    return QualityScore(
        ticker=ticker,
        score_total=score_total,
        score_momentum=s_momentum,
        score_liquidity=s_liquidity,
        score_risk_reward=s_rr,
        score_reliability=s_reliability,
        score_market_regime=s_regime,
        penalty_overheat=p_overheat,
        penalty_duplicate=p_duplicate,
        penalty_event_risk=p_event,
        risk_flags=tuple(flags),
        decision_bucket=bucket,
        decision_reason=reason,
        rr_ratio=rr,
        regime=regime_str,
        scored_at=datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        score_supply_demand=s_supply,
    )


def score_candidates_batch(
    items: list[dict],
    market: str = "KR",
    *,
    persist_decisions: bool = False,
    expensive_checks: bool = False,
) -> list[dict]:
    """배치 점수 계산. regime 1회 호출, accuracy_stats 1회 로드.

    기본값은 GET/read-only 대시보드용이다.
    - persist_decisions=False: 후보 조회만으로 quality DB가 중복 증가하지 않게 함
    - expensive_checks=False: 종목별 재무/지표 네트워크 조회를 생략해 API 타임아웃 방지
    실제 preview/order 생성 경로에서 기록이 필요하면 persist_decisions=True로 호출한다.
    """
    if not items:
        return items

    # regime: 1회 호출
    regime_obj = None
    try:
        from core.regime import detect_regime
        regime_obj = detect_regime(market="KR" if market == "KR" else "US")
    except Exception as exc:
        log.warning("regime detection failed: error_type=%s", type(exc).__name__)

    # accuracy_stats: 1회 로드
    accuracy_stats = None
    try:
        from core.memory import get_accuracy_summary
        accuracy_stats = get_accuracy_summary()
    except Exception as exc:
        log.debug("accuracy stats load failed: error_type=%s", type(exc).__name__)

    # 수급 조회 예산: expensive_checks=False(GET 배치)는 미캐시 심볼 최대 3건만
    # 네트워크 조회 (캐시된 심볼은 예산 소모 없이 항상 반영)
    fetch_budget = None if expensive_checks else {"remaining": 3}

    for item in items:
        try:
            qs = score_candidate(
                item,
                regime_obj=regime_obj,
                accuracy_stats=accuracy_stats,
                expensive_checks=expensive_checks,
                fetch_budget=fetch_budget,
            )
            item["quality_score"] = qs.score_total
            item["quality_breakdown"] = qs.to_dict()
            # 계보 증명 (B6): 이 breakdown이 현재 가중치 프로필과 이 후보에서
            # 산출됐음을 바인딩 — record는 이 증명 없이는 기록을 거부한다.
            attach_quality_proof(item)
            item["decision_bucket"] = qs.decision_bucket
            item["decision_reason"] = qs.decision_reason

            # PASS_EXECUTE / SMALL_PASS → quality DB 기록
            if persist_decisions and qs.decision_bucket in EXECUTABLE_BUCKETS:
                try:
                    record_quality_decision(
                        qs,
                        entry_price=float(item.get("price") or item.get("limit_price") or 0),
                        stop_loss=float(item.get("stop_loss") or 0),
                        target_price=float(item.get("target_price") or 0),
                        quantity=float(item.get("quantity") or 0),
                    )
                except Exception as exc:
                    log.debug(
                        "quality decision record failed: error_type=%s",
                        type(exc).__name__,
                    )

        except Exception as exc:
            log.warning(
                "quality gate scoring failed: error_type=%s",
                type(exc).__name__,
            )
            item["quality_score"] = 0.0
            item["quality_breakdown"] = {}
            item["decision_bucket"] = WATCH
            item["decision_reason"] = "scoring_error"

    # score 내림차순 정렬
    items.sort(key=lambda x: x.get("quality_score", 0), reverse=True)
    return items


# ── 결과 추적 DB ─────────────────────────────────────────────────

_outcomes_lock = threading.Lock()
_outcomes_schema_created = False


def _outcomes_db_path() -> Path:
    try:
        from db.store import DB_DIR
        return DB_DIR / "toss_quality_gate.db"
    except Exception:
        return Path("db/data/toss_quality_gate.db")


def _outcomes_conn() -> sqlite3.Connection:
    global _outcomes_schema_created
    p = _outcomes_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if _outcomes_schema_created:
        return conn
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quality_gate_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                decision_bucket TEXT NOT NULL,
                decision_reason TEXT,
                score_total REAL,
                score_momentum REAL,
                score_liquidity REAL,
                score_risk_reward REAL,
                score_reliability REAL,
                score_market_regime REAL,
                score_supply_demand REAL DEFAULT 0,
                penalty_overheat REAL,
                penalty_duplicate REAL,
                penalty_event_risk REAL,
                rr_ratio REAL,
                regime TEXT,
                entry_price REAL,
                stop_loss REAL,
                target_price REAL,
                quantity REAL DEFAULT 0,
                side TEXT DEFAULT '',
                score_schema_version REAL DEFAULT 0,
                weight_profile_hash TEXT DEFAULT '',
                candidate_snapshot_sha256 TEXT DEFAULT '',
                pilot_id TEXT DEFAULT '',
                decision_ref TEXT DEFAULT '',
                broker_order_id TEXT,
                outcome TEXT,
                return_1d REAL,
                return_3d REAL,
                return_5d REAL,
                outcome_evaluated_at TEXT
            )
        """)
        existing_columns = {
            str(row[1]) for row in conn.execute(
                "PRAGMA table_info(quality_gate_decisions)"
            ).fetchall()
        }
        migrations = {
            "ticker": "TEXT NOT NULL DEFAULT ''",
            "decided_at": "TEXT NOT NULL DEFAULT ''",
            "decision_bucket": "TEXT NOT NULL DEFAULT ''",
            "decision_reason": "TEXT DEFAULT ''",
            "score_total": "REAL DEFAULT 0",
            "score_momentum": "REAL DEFAULT 0",
            "score_liquidity": "REAL DEFAULT 0",
            "score_risk_reward": "REAL DEFAULT 0",
            "score_reliability": "REAL DEFAULT 0",
            "score_market_regime": "REAL DEFAULT 0",
            "score_supply_demand": "REAL DEFAULT 0",
            "penalty_overheat": "REAL DEFAULT 0",
            "penalty_duplicate": "REAL DEFAULT 0",
            "penalty_event_risk": "REAL DEFAULT 0",
            "rr_ratio": "REAL DEFAULT 0",
            "regime": "TEXT DEFAULT ''",
            "entry_price": "REAL DEFAULT 0",
            "stop_loss": "REAL DEFAULT 0",
            "target_price": "REAL DEFAULT 0",
            "quantity": "REAL DEFAULT 0",
            "side": "TEXT DEFAULT ''",
            "score_schema_version": "REAL DEFAULT 0",
            "weight_profile_hash": "TEXT DEFAULT ''",
            "candidate_snapshot_sha256": "TEXT DEFAULT ''",
            "pilot_id": "TEXT DEFAULT ''",
            "decision_ref": "TEXT DEFAULT ''",
            "broker_order_id": "TEXT DEFAULT ''",
            "outcome": "TEXT DEFAULT ''",
            "return_1d": "REAL",
            "return_3d": "REAL",
            "return_5d": "REAL",
            "outcome_evaluated_at": "TEXT DEFAULT ''",
        }
        for column, ddl in migrations.items():
            if column in existing_columns:
                continue
            try:
                conn.execute(
                    f"ALTER TABLE quality_gate_decisions ADD COLUMN {column} {ddl}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

        conn.execute("CREATE INDEX IF NOT EXISTS idx_qg_ticker ON quality_gate_decisions(ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_qg_bucket ON quality_gate_decisions(decision_bucket)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_qg_decision_ref_exact "
            "ON quality_gate_decisions(decision_ref) WHERE decision_ref <> ''"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_qg_pilot_id_exact "
            "ON quality_gate_decisions(pilot_id) WHERE pilot_id <> ''"
        )
        conn.commit()

        actual_columns = {
            str(row[1]) for row in conn.execute(
                "PRAGMA table_info(quality_gate_decisions)"
            ).fetchall()
        }
        actual_indexes = {
            str(row[1]) for row in conn.execute(
                "PRAGMA index_list(quality_gate_decisions)"
            ).fetchall()
        }
        missing_columns = ({"id"} | set(migrations)) - actual_columns
        missing_indexes = {
            "idx_qg_decision_ref_exact", "idx_qg_pilot_id_exact",
        } - actual_indexes
        if missing_columns or missing_indexes:
            raise RuntimeError(
                "quality_schema_incomplete:"
                f"columns={sorted(missing_columns)},indexes={sorted(missing_indexes)}"
            )
        _outcomes_schema_created = True
        return conn
    except Exception:
        conn.rollback()
        conn.close()
        _outcomes_schema_created = False
        raise


def record_quality_decision(
    qs: QualityScore,
    entry_price: float,
    stop_loss: float,
    target_price: float,
    pilot_id: str = "",
    quantity: float = 0,
) -> int:
    """PASS_EXECUTE 결정을 DB에 기록. 반환: row id."""
    with _outcomes_lock:
        conn = _outcomes_conn()
        try:
            cur = conn.execute(
                """INSERT INTO quality_gate_decisions
                   (ticker, decided_at, decision_bucket, decision_reason,
                    score_total, score_momentum, score_liquidity, score_risk_reward,
                    score_reliability, score_market_regime, score_supply_demand,
                    penalty_overheat, penalty_duplicate, penalty_event_risk,
                    rr_ratio, regime, entry_price, stop_loss, target_price, quantity, pilot_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    qs.ticker, qs.scored_at, qs.decision_bucket, qs.decision_reason,
                    qs.score_total, qs.score_momentum, qs.score_liquidity, qs.score_risk_reward,
                    qs.score_reliability, qs.score_market_regime, qs.score_supply_demand,
                    qs.penalty_overheat, qs.penalty_duplicate, qs.penalty_event_risk,
                    qs.rr_ratio, qs.regime, entry_price, stop_loss, target_price, quantity, pilot_id,
                ),
            )
            conn.commit()
            return cur.lastrowid or 0
        finally:
            conn.close()


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def quality_decision_for_ref(decision_ref: str) -> dict:
    ref = str(decision_ref or "")
    if not _DECISION_REF_RE.fullmatch(ref):
        return {}
    with _outcomes_lock:
        conn = _outcomes_conn()
        try:
            row = conn.execute(
                "SELECT * FROM quality_gate_decisions WHERE decision_ref=?",
                (ref,),
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()


def validate_execution_quality_decision(rec: dict, *, pilot_id: str) -> dict:
    """BUY dispatch 직전 exact quality row와 ledger sizing을 대조한다."""
    if not isinstance(rec, dict):
        return {"ok": False, "reason": "quality_execution_contract_invalid"}

    # side 누락/비문자열은 non-BUY의 증거가 아니다 (B3) — 계약 위반으로 차단.
    raw_side = rec.get("side")
    if not isinstance(raw_side, str) or not raw_side.strip():
        return {"ok": False, "reason": "quality_execution_contract_invalid"}
    side = raw_side.lower().strip()
    if side != "buy":
        # 명시적 non-BUY(sell 등)만 품질 검증 생략 대상
        return {"ok": True, "reason": "quality_not_required_for_non_buy", "skipped": True}

    pid = str(pilot_id or "")
    ref = str(rec.get("decision_ref") or "")
    symbol = str(rec.get("symbol") or "").upper().strip()
    quantity = _finite_number(rec.get("quantity"))
    entry = _finite_number(rec.get("limit_price"))
    stop = _finite_number(rec.get("stop_loss"))
    target = _finite_number(rec.get("target_price"))
    if (
        not _CLIENT_ORDER_ID_RE.fullmatch(pid)
        or str(rec.get("pilot_id") or "") != pid
        or not _DECISION_REF_RE.fullmatch(ref)
        or not symbol
        or quantity is None or quantity <= 0
        or entry is None or entry <= 0
        or stop is None or stop <= 0
        or target is None or target <= 0
    ):
        return {"ok": False, "reason": "quality_execution_contract_invalid"}

    with _outcomes_lock:
        conn = _outcomes_conn()
        try:
            row = conn.execute(
                "SELECT * FROM quality_gate_decisions WHERE decision_ref=?",
                (ref,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return {"ok": False, "reason": "quality_decision_missing"}

    exact_numbers = {
        "quantity": float(quantity),
        "entry_price": float(entry),
        "stop_loss": float(stop),
        "target_price": float(target),
    }
    if (
        str(row["pilot_id"] or "") != pid
        or str(row["decision_ref"] or "") != ref
        or str(row["ticker"] or "").upper().strip() != symbol
        or str(row["decision_bucket"] or "") not in EXECUTABLE_BUCKETS
    ):
        return {"ok": False, "reason": "quality_decision_mismatch"}
    for key, expected in exact_numbers.items():
        actual = _finite_number(row[key])
        if actual is None or float(actual) != expected:
            return {"ok": False, "reason": "quality_decision_mismatch"}
    for key in (
        "score_total", "score_momentum", "score_liquidity", "score_risk_reward",
        "score_reliability", "score_market_regime", "score_supply_demand", "rr_ratio",
    ):
        if _finite_number(row[key]) is None:
            return {"ok": False, "reason": "quality_decision_mismatch"}
    # side 증명: legacy/migration row(side 미기록)는 buy 증명 전까지 fail-closed
    try:
        row_side = str(row["side"] or "").lower().strip()
    except (KeyError, IndexError):
        row_side = ""
    if row_side != "buy":
        return {"ok": False, "reason": "quality_decision_side_unverified"}
    # 계보 증명 (B6): scorer 산출 증거가 없는 row는 dispatch 근거가 될 수 없다
    try:
        row_version = _finite_number(row["score_schema_version"])
        row_weights = str(row["weight_profile_hash"] or "")
        row_snapshot = str(row["candidate_snapshot_sha256"] or "")
    except (KeyError, IndexError):
        row_version, row_weights, row_snapshot = None, "", ""
    if (
        row_version is None
        or int(row_version) != QUALITY_SCORE_SCHEMA_VERSION
        or not row_weights or not row_snapshot
    ):
        return {"ok": False, "reason": "quality_decision_proof_missing"}
    if row_weights != _weight_profile_hash():
        # 기록 이후 가중치 프로필 변경 — 낡은 결정으로 dispatch 금지
        return {"ok": False, "reason": "quality_decision_weights_changed"}
    dispatch_snapshot = candidate_snapshot_hash({
        "symbol": symbol, "side": side, "quantity": quantity,
        "limit_price": entry, "stop_loss": stop, "target_price": target,
    })
    if not dispatch_snapshot or dispatch_snapshot != row_snapshot:
        # dispatch 시점 대상과 기록 시점 후보가 다른 객체 — 바인딩 실패
        return {"ok": False, "reason": "quality_decision_snapshot_mismatch"}
    # 컴포넌트 정규 범위 (B5) — 저장 후 변조 방어 심층
    if _component_bounds_violations(row):
        return {"ok": False, "reason": "quality_decision_component_out_of_range"}
    # RR도 저장값을 신뢰하지 않는다 — 가격 3종으로 재계산·대조 (fail-closed)
    stored_rr = _finite_number(row["rr_ratio"])
    computed_rr = _recompute_rr_ratio(
        row["entry_price"], row["stop_loss"], row["target_price"])
    if (
        stored_rr is None or computed_rr is None
        or abs(computed_rr - float(stored_rr)) > _RR_RECOMPUTE_TOLERANCE
    ):
        return {"ok": False, "reason": "quality_decision_rr_mismatch"}
    # 저장된 총점/PASS도 신뢰하지 않는다 — dispatch 직전 재계산 대조 (fail-closed)
    if not _quality_scores_verified(
            row, row["score_total"], str(row["decision_bucket"] or ""),
            row["rr_ratio"]):
        return {"ok": False, "reason": "quality_decision_recompute_mismatch"}
    return {"ok": True, "reason": "quality_decision_exact", "id": int(row["id"])}


_RECOMPUTE_COMPONENT_KEYS = (
    "score_momentum", "score_liquidity", "score_risk_reward",
    "score_reliability", "score_market_regime", "score_supply_demand",
    "penalty_overheat", "penalty_duplicate", "penalty_event_risk",
)
# 저장 시 컴포넌트별 round(…,1) 누적 오차 허용치 (9필드 × 0.05 + 여유)
_RECOMPUTE_TOLERANCE = 0.75
# RR 재계산 허용치 — 호출자 1소수 반올림(±0.05) + float 오차 여유
_RR_RECOMPUTE_TOLERANCE = 0.08

# ── 계보 증명 (B6): scorer가 어떤 가중치·후보로 이 점수를 만들었는지 바인딩 ──
QUALITY_SCORE_SCHEMA_VERSION = 2

# 컴포넌트 정규 범위 (B5): scorer 절대 최대 × 최대 가중치 1.5 기준.
# (min, max) 연속 범위 또는 frozenset 이산 집합. 저장 반올림(0.1) 여유 0.06.
_COMPONENT_BOUNDS: dict[str, tuple[float, float] | frozenset] = {
    "score_momentum": (0.0, 37.5),
    "score_liquidity": (0.0, 37.5),
    "score_risk_reward": (0.0, 30.0),
    "score_reliability": (0.0, 22.5),
    "score_market_regime": (0.0, 22.5),
    "score_supply_demand": (-15.0, 15.0),
    "penalty_overheat": (-20.0, 0.0),
    "penalty_duplicate": frozenset({-20.0, 0.0}),
    "penalty_event_risk": frozenset({-15.0, -5.0, 0.0}),
}
_BOUNDS_EPS = 0.06


def _component_bounds_violations(source) -> list[str]:
    """컴포넌트/페널티가 scorer 정규 범위를 벗어나면 필드명 목록 반환."""
    bad: list[str] = []
    for name, bounds in _COMPONENT_BOUNDS.items():
        try:
            value = _finite_number(source[name])
        except (KeyError, IndexError, TypeError):
            value = None
        if value is None:
            bad.append(name)
            continue
        v = float(value)
        if isinstance(bounds, frozenset):
            if not any(abs(v - allowed) <= _BOUNDS_EPS for allowed in bounds):
                bad.append(name)
        else:
            lo, hi = bounds
            if v < lo - _BOUNDS_EPS or v > hi + _BOUNDS_EPS:
                bad.append(name)
    return bad


def _weight_profile_hash() -> str:
    """현재 가중치 프로필의 결정론 해시 — 점수 산출 시점 프로필 바인딩용."""
    weights = {k: round(float(v), 6) for k, v in sorted(get_score_weights().items())}
    payload = json.dumps(weights, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_SNAPSHOT_FIELDS = ("symbol", "side", "quantity", "limit_price", "stop_loss", "target_price")


def candidate_snapshot_hash(candidate) -> str | None:
    """실행 결정 대상 후보의 불변 스냅샷 해시.

    record 시점 candidate와 dispatch 시점 rec이 같은 대상임을 증명하는 키.
    필수 필드가 하나라도 비정상이면 None (fail-closed).
    """
    if not isinstance(candidate, Mapping):
        return None
    snap: dict[str, object] = {}
    for field in _SNAPSHOT_FIELDS:
        raw = candidate.get(field)
        if field in ("symbol", "side"):
            text = str(raw or "").strip()
            if not text:
                return None
            snap[field] = text.upper() if field == "symbol" else text.lower()
        else:
            value = _finite_number(raw)
            if value is None or float(value) <= 0:
                return None
            snap[field] = round(float(value), 6)
    payload = json.dumps(snap, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def attach_quality_proof(candidate: dict) -> None:
    """scorer 산출 직후 breakdown에 계보 증명 3필드를 주입한다.

    - score_schema_version: 점수 스키마 버전
    - weight_profile_hash: 산출에 사용된 가중치 프로필 해시
    - candidate_snapshot_sha256: 이 후보(심볼·side·수량·가격 3종)의 스냅샷 해시
    record_execution_quality_decision은 이 증명 없이는 기록을 거부한다.
    """
    breakdown = candidate.get("quality_breakdown")
    if not isinstance(breakdown, dict):
        return
    breakdown["score_schema_version"] = QUALITY_SCORE_SCHEMA_VERSION
    breakdown["weight_profile_hash"] = _weight_profile_hash()
    snapshot = candidate_snapshot_hash(candidate)
    if snapshot:
        breakdown["candidate_snapshot_sha256"] = snapshot


def _recompute_rr_ratio(entry, stop, target) -> float | None:
    """BUY 기준 RR = (target-entry)/(entry-stop). caller rr_ratio를 신뢰하지 않는다.

    entry<=stop(위험 0 이하)·target<=entry(보상 0 이하)·비유한 값은 None (fail-closed).
    """
    e = _finite_number(entry)
    s = _finite_number(stop)
    t = _finite_number(target)
    if e is None or s is None or t is None:
        return None
    risk = float(e) - float(s)
    reward = float(t) - float(e)
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def _recompute_score_total(source) -> float | None:
    """저장된 컴포넌트(가중치 적용 완료)+페널티 합으로 총점을 재계산한다.

    호출자가 주장한 score_total을 신뢰하지 않기 위한 fail-closed 기준값.
    필드가 하나라도 비유한(non-finite)이면 None (→ 검증 실패 처리).
    """
    total = 0.0
    for key in _RECOMPUTE_COMPONENT_KEYS:
        try:
            value = _finite_number(source[key])
        except (KeyError, IndexError, TypeError):
            value = None
        if value is None:
            return None
        total += float(value)
    return max(0.0, min(100.0, total))


def _bucket_consistent_with_scores(bucket: str, score_total: float, rr: float) -> bool:
    """실행 bucket이 재계산 총점·RR과 모순되지 않는지 검사.

    _decide_bucket에서 역산 가능한 하한 불변식만 강제한다 (fail-closed):
    - PASS_EXECUTE ⇒ 총점 ≥ 45 그리고 RR ≥ 1.8
    - SMALL_PASS   ⇒ RR ≥ 1.2 그리고 (RR ≥ 1.8 또는 총점 ≥ 45)
    """
    if bucket == PASS_EXECUTE:
        return score_total >= 45.0 and rr >= 1.8
    if bucket == SMALL_PASS:
        return rr >= 1.2 and (rr >= 1.8 or score_total >= 45.0)
    return False


def _quality_scores_verified(source, claimed_total, bucket: str, rr) -> bool:
    """호출자/저장소가 주장한 총점·bucket을 내부 재계산과 대조한다."""
    claimed = _finite_number(claimed_total)
    rr_value = _finite_number(rr)
    if claimed is None or rr_value is None:
        return False
    recomputed = _recompute_score_total(source)
    if recomputed is None:
        return False
    if abs(recomputed - float(claimed)) > _RECOMPUTE_TOLERANCE:
        return False
    return _bucket_consistent_with_scores(bucket, recomputed, float(rr_value))


def record_execution_quality_decision(
    candidate: dict,
    *,
    pilot_id: str,
    decision_ref: str,
) -> dict:
    """실행 직전 BUY 품질 결정을 exact pilot/ref에 1회 기록."""
    if not isinstance(candidate, dict):
        return {"ok": False, "reason": "candidate_invalid"}
    pid = str(pilot_id or "")
    ref = str(decision_ref or "")
    symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").upper().strip()
    # side는 명시 필수 — 기본값 'buy' 금지 (B4). 실행 경계에서 추정은 위조와 동급.
    raw_side = candidate.get("side")
    if not isinstance(raw_side, str) or not raw_side.strip():
        return {"ok": False, "reason": "quality_side_missing"}
    side = raw_side.lower().strip()
    bucket = str(candidate.get("decision_bucket") or "")
    breakdown = candidate.get("quality_breakdown")
    if (
        not _CLIENT_ORDER_ID_RE.fullmatch(pid)
        or not _DECISION_REF_RE.fullmatch(ref)
        or not symbol
        or side != "buy"
        or bucket not in EXECUTABLE_BUCKETS
        or not isinstance(breakdown, dict)
    ):
        return {"ok": False, "reason": "quality_execution_contract_invalid"}

    entry = _finite_number(candidate.get("limit_price") or candidate.get("price"))
    stop = _finite_number(candidate.get("stop_loss"))
    target = _finite_number(candidate.get("target_price"))
    quantity = _finite_number(candidate.get("quantity"))
    rr = _finite_number(breakdown.get("rr_ratio") or candidate.get("risk_reward"))
    score_total = _finite_number(
        breakdown.get("score_total") if "score_total" in breakdown
        else candidate.get("quality_score")
    )
    if (
        entry is None or entry <= 0
        or stop is None or stop <= 0
        or target is None or target <= 0
        or quantity is None or quantity <= 0
        or rr is None or rr < 1.2
        or score_total is None
    ):
        return {"ok": False, "reason": "quality_execution_values_invalid"}

    # 컴포넌트 6종+페널티 3종 전부 present+finite 필수 — 누락을 0.0으로
    # 대체하던 관대한 계약 제거 (누락 = 산출 증거 부재 = 기록 거부)
    component_values: dict[str, float] = {}
    for name in _RECOMPUTE_COMPONENT_KEYS:
        value = _finite_number(breakdown.get(name))
        if value is None:
            return {"ok": False, "reason": "quality_components_missing"}
        component_values[name] = float(value)

    # RR도 caller 주장을 신뢰하지 않는다 — 가격 3종으로 재계산·대조
    computed_rr = _recompute_rr_ratio(entry, stop, target)
    if computed_rr is None or abs(computed_rr - float(rr)) > _RR_RECOMPUTE_TOLERANCE:
        return {"ok": False, "reason": "quality_rr_recompute_mismatch"}

    # 컴포넌트 정규 범위 검증 (B5) — 산술 정합만으로는 부족, scorer 한계 강제
    if _component_bounds_violations(component_values):
        return {"ok": False, "reason": "quality_component_out_of_range"}

    # 계보 증명 (B6): scorer가 이 후보·현재 가중치로 산출했음을 검증
    proof_version = breakdown.get("score_schema_version")
    proof_weights = str(breakdown.get("weight_profile_hash") or "")
    proof_snapshot = str(breakdown.get("candidate_snapshot_sha256") or "")
    if proof_version != QUALITY_SCORE_SCHEMA_VERSION or not proof_weights or not proof_snapshot:
        return {"ok": False, "reason": "quality_proof_missing"}
    if proof_weights != _weight_profile_hash():
        return {"ok": False, "reason": "quality_proof_weight_mismatch"}
    if proof_snapshot != (candidate_snapshot_hash(candidate) or ""):
        return {"ok": False, "reason": "quality_proof_candidate_mismatch"}

    def metric(name: str) -> float:
        return component_values[name]

    immutable_payload = {
        "ticker": symbol,
        "decision_bucket": bucket,
        "decision_reason": str(
            candidate.get("decision_reason")
            or breakdown.get("decision_reason")
            or ""
        )[:500],
        "score_total": float(score_total),
        "score_momentum": metric("score_momentum"),
        "score_liquidity": metric("score_liquidity"),
        "score_risk_reward": metric("score_risk_reward"),
        "score_reliability": metric("score_reliability"),
        "score_market_regime": metric("score_market_regime"),
        "score_supply_demand": metric("score_supply_demand"),
        "penalty_overheat": metric("penalty_overheat"),
        "penalty_duplicate": metric("penalty_duplicate"),
        "penalty_event_risk": metric("penalty_event_risk"),
        "rr_ratio": float(rr),
        "regime": str(breakdown.get("regime") or "")[:80],
        "entry_price": float(entry),
        "stop_loss": float(stop),
        "target_price": float(target),
        "quantity": float(quantity),
        "side": side,
        "score_schema_version": float(QUALITY_SCORE_SCHEMA_VERSION),
        "weight_profile_hash": proof_weights,
        "candidate_snapshot_sha256": proof_snapshot,
    }

    # 호출자가 주장한 총점/PASS를 신뢰하지 않는다 — 기록 시점 재계산 대조 (fail-closed)
    if not _quality_scores_verified(
            immutable_payload, immutable_payload["score_total"], bucket, rr):
        return {"ok": False, "reason": "quality_recompute_mismatch"}

    def payload_matches(row: sqlite3.Row) -> bool:
        for key, expected in immutable_payload.items():
            actual = row[key]
            if isinstance(expected, float):
                actual_number = _finite_number(actual)
                if actual_number is None or float(actual_number) != expected:
                    return False
            elif str(actual or "") != expected:
                return False
        return True

    with _outcomes_lock:
        conn = _outcomes_conn()
        try:
            select_columns = (
                "id, ticker, pilot_id, decision_ref, decision_bucket, decision_reason, "
                "score_total, score_momentum, score_liquidity, score_risk_reward, "
                "score_reliability, score_market_regime, score_supply_demand, penalty_overheat, "
                "penalty_duplicate, penalty_event_risk, rr_ratio, regime, "
                "entry_price, stop_loss, target_price, quantity, side, "
                "score_schema_version, weight_profile_hash, candidate_snapshot_sha256"
            )
            existing_ref = conn.execute(
                f"SELECT {select_columns} FROM quality_gate_decisions WHERE decision_ref=?",
                (ref,),
            ).fetchone()
            existing_pilot = conn.execute(
                f"SELECT {select_columns} FROM quality_gate_decisions WHERE pilot_id=?",
                (pid,),
            ).fetchone()
            if existing_pilot and str(existing_pilot["decision_ref"] or "") != ref:
                return {"ok": False, "reason": "quality_pilot_id_conflict"}
            if existing_ref and str(existing_ref["pilot_id"] or "") != pid:
                return {"ok": False, "reason": "quality_decision_ref_conflict"}
            existing = existing_ref or existing_pilot
            if existing:
                if not payload_matches(existing):
                    return {
                        "ok": False,
                        "reason": "quality_decision_payload_conflict",
                    }
                return {"ok": True, "id": int(existing["id"]), "created": False}
            cur = conn.execute(
                """INSERT INTO quality_gate_decisions
                   (ticker, decided_at, decision_bucket, decision_reason,
                    score_total, score_momentum, score_liquidity, score_risk_reward,
                    score_reliability, score_market_regime, score_supply_demand,
                    penalty_overheat, penalty_duplicate, penalty_event_risk,
                    rr_ratio, regime, entry_price, stop_loss, target_price, quantity,
                    side, score_schema_version, weight_profile_hash,
                    candidate_snapshot_sha256, pilot_id, decision_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    symbol,
                    datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                    bucket,
                    immutable_payload["decision_reason"],
                    immutable_payload["score_total"],
                    immutable_payload["score_momentum"],
                    immutable_payload["score_liquidity"],
                    immutable_payload["score_risk_reward"],
                    immutable_payload["score_reliability"],
                    immutable_payload["score_market_regime"],
                    immutable_payload["score_supply_demand"],
                    immutable_payload["penalty_overheat"],
                    immutable_payload["penalty_duplicate"],
                    immutable_payload["penalty_event_risk"],
                    immutable_payload["rr_ratio"],
                    immutable_payload["regime"],
                    immutable_payload["entry_price"],
                    immutable_payload["stop_loss"],
                    immutable_payload["target_price"],
                    immutable_payload["quantity"],
                    immutable_payload["side"],
                    immutable_payload["score_schema_version"],
                    immutable_payload["weight_profile_hash"],
                    immutable_payload["candidate_snapshot_sha256"],
                    pid,
                    ref,
                ),
            )
            conn.commit()
            return {"ok": True, "id": int(cur.lastrowid or 0), "created": True}
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            reason = (
                "quality_pilot_id_conflict"
                if "pilot_id" in str(exc).lower()
                else "quality_decision_ref_conflict"
            )
            return {"ok": False, "reason": reason}
        finally:
            conn.close()


def evaluate_outcomes() -> dict:
    """5일 경과 PASS 결정의 outcome을 자동 평가."""
    cutoff = (datetime.now(KST) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    evaluated = 0
    errors = 0

    with _outcomes_lock:
        conn = _outcomes_conn()
        try:
            rows = conn.execute(
                "SELECT id, ticker, entry_price, stop_loss, target_price, pilot_id "
                "FROM quality_gate_decisions "
                "WHERE outcome IS NULL AND decided_at < ?",
                (cutoff,),
            ).fetchall()

            for row in rows:
                rid, ticker, entry, stop, target = row["id"], row["ticker"], row["entry_price"], row["stop_loss"], row["target_price"]
                try:
                    # 실제 체결가가 있으면 entry로 사용 (B: 체결가 연동)
                    fill_price = _get_fill_price(row["pilot_id"] if "pilot_id" in row.keys() else "")
                    if fill_price > 0:
                        entry = fill_price
                    price = _get_current_price(ticker)
                    if price <= 0 or entry <= 0:
                        continue
                    ret = (price - entry) / entry
                    if target and price >= target:
                        outcome = "win"
                    elif stop and price <= stop:
                        outcome = "loss"
                    else:
                        outcome = "expired"
                    conn.execute(
                        "UPDATE quality_gate_decisions SET outcome=?, return_5d=?, "
                        "entry_price=?, outcome_evaluated_at=? WHERE id=?",
                        (outcome, round(ret, 4), entry,
                         datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"), rid),
                    )
                    evaluated += 1
                except Exception as exc:
                    log.debug(
                        "outcome eval failed: error_type=%s",
                        type(exc).__name__,
                    )
                    errors += 1

            conn.commit()
        finally:
            conn.close()

    return {"evaluated": evaluated, "errors": errors}


def _get_current_price(ticker: str) -> float:
    """시세 조회 (기존 market.py 재사용).

    주의: core.market에 get_price()는 없음 — _get_quote_realtime 사용.
    (기존 코드가 존재하지 않는 get_price를 import해 항상 0 반환하던 버그 수정)
    """
    try:
        from core.market import _get_quote_realtime
        q = _get_quote_realtime(ticker)
        return float(q.price) if q else 0.0
    except Exception:
        return 0.0


def _get_fill_price(pilot_id: str) -> float:
    """pilot_id의 실제 체결가 조회 (없으면 0)."""
    if not pilot_id:
        return 0.0
    try:
        from core.toss_live_pilot_events import latest_fill_for_pilot
        return float(latest_fill_for_pilot(pilot_id).get("filled_price", 0) or 0)
    except Exception:
        return 0.0


# ── 일간 리포트 ──────────────────────────────────────────────────

def generate_daily_quality_report(date: str | None = None) -> dict:
    """일간 품질 리포트."""
    if date is None:
        date = datetime.now(KST).strftime("%Y-%m-%d")

    with _outcomes_lock:
        conn = _outcomes_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM quality_gate_decisions WHERE decided_at LIKE ?",
                (f"{date}%",),
            ).fetchall()

            # 전체 outcome 통계
            all_rows = conn.execute(
                "SELECT outcome, return_5d FROM quality_gate_decisions WHERE outcome IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()

    buckets: dict[str, int] = {}
    pass_scores = []
    pass_rrs = []
    block_reasons: list[str] = []

    for r in rows:
        b = r["decision_bucket"]
        buckets[b] = buckets.get(b, 0) + 1
        if b in (PASS_EXECUTE, SMALL_PASS):
            pass_scores.append(r["score_total"])
            pass_rrs.append(r["rr_ratio"])
        elif b in (BLOCK, CHASE_BLOCK, WATCH):
            if r["decision_reason"]:
                block_reasons.append(r["decision_reason"])

    wins = sum(1 for r in all_rows if r["outcome"] == "win")
    losses = sum(1 for r in all_rows if r["outcome"] == "loss")
    total_eval = wins + losses

    return {
        "date": date,
        "pass_count": buckets.get(PASS_EXECUTE, 0),
        "small_pass_count": buckets.get(SMALL_PASS, 0),
        "wait_count": buckets.get(WAIT_PULLBACK, 0),
        "watch_count": buckets.get(WATCH, 0),
        "chase_block_count": buckets.get(CHASE_BLOCK, 0),
        "block_count": buckets.get(BLOCK, 0),
        "avg_pass_score": round(sum(pass_scores) / len(pass_scores), 1) if pass_scores else 0,
        "avg_pass_rr": round(sum(pass_rrs) / len(pass_rrs), 2) if pass_rrs else 0,
        "outcome_hit_rate": round(wins / total_eval, 3) if total_eval > 0 else None,
        "outcome_evaluated": total_eval,
        "top_block_reasons": _top_n(block_reasons, 5),
    }


def no_action_diagnosis(items: list[dict]) -> dict | None:
    """후보가 있는데 실행 가능 버킷 0개면 원인과 완화 후보를 반환."""
    if not items:
        return None

    bucket_counts: dict[str, int] = {}
    for item in items:
        b = item.get("decision_bucket", WATCH)
        bucket_counts[b] = bucket_counts.get(b, 0) + 1

    executable = bucket_counts.get(PASS_EXECUTE, 0) + bucket_counts.get(SMALL_PASS, 0)
    if executable > 0:
        return None  # 실행 가능 후보 있음 → 진단 불필요

    # 완화 가능 후보 탐색 (WATCH/WAIT_PULLBACK 중 조건 완화하면 SMALL_PASS 가능)
    relaxable: list[dict] = []
    for item in items:
        bucket = item.get("decision_bucket", "")
        if bucket in (WATCH, WAIT_PULLBACK):
            hints = []
            rr = float(item.get("risk_reward") or 0)
            has_stop = bool(item.get("stop_loss"))
            has_target = bool(item.get("target_price"))
            if not has_stop:
                hints.append("손절 자동 산정 필요 (6% 기본)")
            if not has_target:
                hints.append("목표가 자동 산정 필요")
            if rr < 1.2 and has_stop and has_target:
                hints.append(f"RR {rr:.1f} → 지정가 하향 또는 목표가 상향으로 RR 개선")
            if rr >= 1.2:
                hints.append("수량 1주로 축소하면 SMALL_PASS 가능")
            relaxable.append({
                "symbol": item.get("symbol", ""),
                "name": item.get("name", ""),
                "bucket": bucket,
                "reason": item.get("decision_reason", ""),
                "score": item.get("quality_score", 0),
                "rr": rr,
                "relaxation_hints": hints,
            })

    relaxable.sort(key=lambda x: x["score"], reverse=True)

    block_reasons = [
        item.get("decision_reason", "")
        for item in items
        if item.get("decision_bucket") in (BLOCK, CHASE_BLOCK)
    ]

    return {
        "total_candidates": len(items),
        "bucket_counts": bucket_counts,
        "executable_count": 0,
        "top_block_reasons": _top_n(block_reasons, 5),
        "relaxable_candidates": relaxable[:3],
        "diagnosis": (
            f"후보 {len(items)}건 중 실행 가능 0건. "
            f"BLOCK {bucket_counts.get(BLOCK, 0)}, "
            f"CHASE_BLOCK {bucket_counts.get(CHASE_BLOCK, 0)}, "
            f"WATCH {bucket_counts.get(WATCH, 0)}, "
            f"WAIT {bucket_counts.get(WAIT_PULLBACK, 0)}."
        ),
    }


def _top_n(items: list[str], n: int) -> list[dict]:
    """빈도 상위 N개."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    sorted_items = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:n]
    return [{"reason": r, "count": c} for r, c in sorted_items]
