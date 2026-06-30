"""core/toss_quality_gate.py

Toss 자동매매 품질 게이트 — 다차원 점수화 + decision_bucket 결정.

[구조]
  score_candidate()  : 단일 후보 점수 계산 → QualityScore
  score_candidates_batch() : 배치 처리 (regime 1회 캐시)
  _decide_bucket()   : 점수+RR+국면 기반 실행 판정

[decision_bucket]
  PASS_EXECUTE    — 자동 주문 가능
  WAIT_PULLBACK   — 눌림목 대기 (RR 보통 또는 실적 임박)
  WATCH           — 관찰만 (약세장/점수 부족)
  CHASE_BLOCK     — 급등 추격 차단
  BLOCK           — 주문 불가 (손절 없음/RR 부족/위기)

[안전]
- 기존 자동주문 경로 변경 없음
- discovery_candidates._gate()/_score() 유지
- toss_autonomous_finalizer.py 변경 없음
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# ── decision buckets ─────────────────────────────────────────────
PASS_EXECUTE = "PASS_EXECUTE"
WAIT_PULLBACK = "WAIT_PULLBACK"
WATCH = "WATCH"
CHASE_BLOCK = "CHASE_BLOCK"
BLOCK = "BLOCK"

_ALL_BUCKETS = frozenset([PASS_EXECUTE, WAIT_PULLBACK, WATCH, CHASE_BLOCK, BLOCK])


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

    def to_dict(self) -> dict:
        return {
            "score_total": round(self.score_total, 1),
            "score_momentum": round(self.score_momentum, 1),
            "score_liquidity": round(self.score_liquidity, 1),
            "score_risk_reward": round(self.score_risk_reward, 1),
            "score_reliability": round(self.score_reliability, 1),
            "score_market_regime": round(self.score_market_regime, 1),
            "penalty_overheat": round(self.penalty_overheat, 1),
            "penalty_duplicate": round(self.penalty_duplicate, 1),
            "penalty_event_risk": round(self.penalty_event_risk, 1),
            "risk_flags": list(self.risk_flags),
            "decision_bucket": self.decision_bucket,
            "decision_reason": self.decision_reason,
            "rr_ratio": round(self.rr_ratio, 2),
            "regime": self.regime,
        }


# ── 점수 계산 ────────────────────────────────────────────────────

def _score_momentum(candidate: dict) -> float:
    """기술 지표 모멘텀 점수 (0-25). 빠른 fallback: candidate RSI."""
    ticker = candidate.get("symbol", "")
    try:
        from core.indicators import calculate_indicators
        result = calculate_indicators(ticker)
        if result:
            # confluence_score: -4 ~ +4 → 0 ~ 25
            return max(0.0, min(25.0, (result.confluence_score + 4) / 8 * 25))
    except Exception as e:
        log.debug("momentum score fallback for %s: %s", ticker, e)
    # fallback: candidate의 기존 점수 활용
    score = candidate.get("score", 0)
    return min(25.0, max(0.0, score / 4))


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
    except Exception as e:
        log.debug("event risk check failed for %s: %s", ticker, e)
        return 0.0, -1


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
    """점수+RR+국면 기반 실행 판정."""
    # 0. blocking risk flags → 무조건 차단
    if blocking_risk_flags:
        return BLOCK, f"리스크 차단: {blocking_risk_flags[0]}"

    # 1. 필수 조건
    if not has_stop or not has_target:
        return BLOCK, "손절/목표가 미설정"
    if rr < 1.2:
        return BLOCK, f"손익비 부족 ({rr:.1f}:1 < 1.2:1)"

    # 2. 시장 위기
    if regime == "위기":
        return BLOCK, "시장 위기 국면 — 신규 매수 차단"

    # 3. 급등 추격
    if abs(change_pct) >= 8.0:
        return CHASE_BLOCK, f"당일 급등 추격 차단 (+{change_pct:.1f}%)"

    # 4. 약세장 + RR 부족
    if regime == "약세장" and rr < 2.0:
        return WATCH, f"약세장 — RR {rr:.1f}:1 부족 (2.0+ 필요)"

    # 5. RR 보통 → 눌림목 대기
    if rr < 1.8:
        return WAIT_PULLBACK, f"손익비 보통 ({rr:.1f}:1) — 눌림목 대기"

    # 6. 총점 부족
    if score_total < 45:
        return WATCH, f"총점 부족 ({score_total:.0f}/100)"

    # 7. 실적 임박
    if 0 <= days_to_earnings <= 3:
        return WAIT_PULLBACK, f"실적 발표 {days_to_earnings}일 이내 — 대기"

    # 8. 통과
    return PASS_EXECUTE, "조건 충족"


# ── 메인 API ─────────────────────────────────────────────────────

def score_candidate(
    candidate: dict,
    regime_obj=None,
    accuracy_stats: dict | None = None,
) -> QualityScore:
    """단일 후보 품질 점수 계산."""
    ticker = candidate.get("symbol", "")
    rr = float(candidate.get("risk_reward", 0) or 0)
    change_pct = float(candidate.get("change_pct", 0) or 0)
    has_stop = bool(candidate.get("stop_loss"))
    has_target = bool(candidate.get("target_price"))

    # 각 sub-score
    s_momentum = _score_momentum(candidate)
    s_liquidity = _score_liquidity(candidate)
    s_rr = _score_risk_reward(candidate)
    s_reliability = _score_reliability(ticker, accuracy_stats)
    s_regime = _score_market_regime(regime_obj)

    p_overheat = _penalty_overheat(candidate)
    p_duplicate = _penalty_duplicate(candidate)

    # 이벤트 리스크: pre_score 계산 후 비용 관리
    pre_score = s_momentum + s_liquidity + s_rr + s_reliability + s_regime + p_overheat + p_duplicate
    p_event, days_to_earnings = _penalty_event_risk(ticker, pre_score)

    score_total = max(0.0, min(100.0,
        s_momentum + s_liquidity + s_rr + s_reliability + s_regime
        + p_overheat + p_duplicate + p_event
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
    )


def score_candidates_batch(
    items: list[dict],
    market: str = "KR",
) -> list[dict]:
    """배치 점수 계산. regime 1회 호출, accuracy_stats 1회 로드."""
    if not items:
        return items

    # regime: 1회 호출
    regime_obj = None
    try:
        from core.regime import detect_regime
        regime_obj = detect_regime(market="KR" if market == "KR" else "US")
    except Exception as e:
        log.warning("regime detection failed: %s", e)

    # accuracy_stats: 1회 로드
    accuracy_stats = None
    try:
        from core.memory import get_accuracy_summary
        accuracy_stats = get_accuracy_summary()
    except Exception as e:
        log.debug("accuracy stats load failed: %s", e)

    for item in items:
        try:
            qs = score_candidate(item, regime_obj=regime_obj, accuracy_stats=accuracy_stats)
            item["quality_score"] = qs.score_total
            item["quality_breakdown"] = qs.to_dict()
            item["decision_bucket"] = qs.decision_bucket
            item["decision_reason"] = qs.decision_reason
        except Exception as e:
            log.warning("quality gate scoring failed for %s: %s", item.get("symbol"), e)
            item["quality_score"] = 0.0
            item["quality_breakdown"] = {}
            item["decision_bucket"] = WATCH
            item["decision_reason"] = f"scoring_error: {e}"

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
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    if not _outcomes_schema_created:
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
                penalty_overheat REAL,
                penalty_duplicate REAL,
                penalty_event_risk REAL,
                rr_ratio REAL,
                regime TEXT,
                entry_price REAL,
                stop_loss REAL,
                target_price REAL,
                pilot_id TEXT,
                broker_order_id TEXT,
                outcome TEXT,
                return_1d REAL,
                return_3d REAL,
                return_5d REAL,
                outcome_evaluated_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_qg_ticker ON quality_gate_decisions(ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_qg_bucket ON quality_gate_decisions(decision_bucket)")
        conn.commit()
        _outcomes_schema_created = True
    return conn


def record_quality_decision(
    qs: QualityScore,
    entry_price: float,
    stop_loss: float,
    target_price: float,
    pilot_id: str = "",
) -> int:
    """PASS_EXECUTE 결정을 DB에 기록. 반환: row id."""
    with _outcomes_lock:
        conn = _outcomes_conn()
        try:
            cur = conn.execute(
                """INSERT INTO quality_gate_decisions
                   (ticker, decided_at, decision_bucket, decision_reason,
                    score_total, score_momentum, score_liquidity, score_risk_reward,
                    score_reliability, score_market_regime,
                    penalty_overheat, penalty_duplicate, penalty_event_risk,
                    rr_ratio, regime, entry_price, stop_loss, target_price, pilot_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    qs.ticker, qs.scored_at, qs.decision_bucket, qs.decision_reason,
                    qs.score_total, qs.score_momentum, qs.score_liquidity, qs.score_risk_reward,
                    qs.score_reliability, qs.score_market_regime,
                    qs.penalty_overheat, qs.penalty_duplicate, qs.penalty_event_risk,
                    qs.rr_ratio, qs.regime, entry_price, stop_loss, target_price, pilot_id,
                ),
            )
            conn.commit()
            return cur.lastrowid or 0
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
                "SELECT id, ticker, entry_price, stop_loss, target_price "
                "FROM quality_gate_decisions "
                "WHERE outcome IS NULL AND decided_at < ?",
                (cutoff,),
            ).fetchall()

            for row in rows:
                rid, ticker, entry, stop, target = row["id"], row["ticker"], row["entry_price"], row["stop_loss"], row["target_price"]
                try:
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
                        "UPDATE quality_gate_decisions SET outcome=?, return_5d=?, outcome_evaluated_at=? WHERE id=?",
                        (outcome, round(ret, 4), datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"), rid),
                    )
                    evaluated += 1
                except Exception as e:
                    log.debug("outcome eval failed for %s: %s", ticker, e)
                    errors += 1

            conn.commit()
        finally:
            conn.close()

    return {"evaluated": evaluated, "errors": errors}


def _get_current_price(ticker: str) -> float:
    """시세 조회 (기존 market.py 재사용)."""
    try:
        from core.market import get_price
        return float(get_price(ticker) or 0)
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
        if b == PASS_EXECUTE:
            pass_scores.append(r["score_total"])
            pass_rrs.append(r["rr_ratio"])
        else:
            if r["decision_reason"]:
                block_reasons.append(r["decision_reason"])

    wins = sum(1 for r in all_rows if r["outcome"] == "win")
    losses = sum(1 for r in all_rows if r["outcome"] == "loss")
    total_eval = wins + losses

    return {
        "date": date,
        "pass_count": buckets.get(PASS_EXECUTE, 0),
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


def _top_n(items: list[str], n: int) -> list[dict]:
    """빈도 상위 N개."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    sorted_items = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:n]
    return [{"reason": r, "count": c} for r, c in sorted_items]
