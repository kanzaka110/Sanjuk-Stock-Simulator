"""
AI 메모리 시스템 — 과거 추천 기록 + 정확도 추적

FinMem 패턴 참고: 계층적 메모리로 AI가 자신의 과거 판단을 학습.
- 에피소드 메모리: 개별 추천 기록 (날짜, 종목, 판단, 가격)
- 성과 메모리: 추천 정확도 통계 (적중률, 평균 수익률)
"""

from __future__ import annotations

import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from config.settings import DB_DIR, KST

log = logging.getLogger(__name__)

DB_PATH = DB_DIR / "memory.db"

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_tables(_conn)
    return _conn


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            signal TEXT NOT NULL,
            entry_price REAL NOT NULL,
            target_price REAL,
            stop_loss REAL,
            confidence INTEGER DEFAULT 50,
            reasoning TEXT,
            persona TEXT,
            status TEXT DEFAULT 'open',
            closed_at TEXT,
            closed_price REAL,
            pnl_pct REAL,
            outcome TEXT
        );

        CREATE TABLE IF NOT EXISTS accuracy_stats (
            ticker TEXT PRIMARY KEY,
            total_predictions INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            wrong INTEGER DEFAULT 0,
            avg_pnl REAL DEFAULT 0,
            win_rate REAL DEFAULT 0,
            last_updated TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_predictions_ticker
            ON predictions(ticker);
        CREATE INDEX IF NOT EXISTS idx_predictions_status
            ON predictions(status);
    """)
    # Phase 2 마이그레이션: 기존 DB에 새 컬럼 추가
    _migrate_phase2(conn)


def _migrate_phase2(conn: sqlite3.Connection) -> None:
    """Phase 2 컬럼 안전 추가. 이미 있으면 무시."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(predictions)").fetchall()}
    new_columns = [
        ("strategy_type", "TEXT DEFAULT '일반'"),
        ("strategy_tags", "TEXT DEFAULT ''"),
        ("horizon_days", "INTEGER DEFAULT 7"),
        ("benchmark_ticker", "TEXT DEFAULT ''"),
        ("execution_condition", "TEXT DEFAULT ''"),
        ("invalidation_condition", "TEXT DEFAULT ''"),
        ("risk_reward", "REAL DEFAULT 0"),
        ("agreement_count", "INTEGER DEFAULT 0"),
    ]
    for col_name, col_def in new_columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col_name} {col_def}")
            log.info("Phase 2 마이그레이션: predictions.%s 추가", col_name)
    conn.commit()


# ═══════════════════════════════════════════════════════
# 추천 기록 저장
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class Prediction:
    """AI 추천 기록."""

    id: int = 0
    created_at: str = ""
    ticker: str = ""
    name: str = ""
    signal: str = ""  # 매수/매도/홀딩/관망
    entry_price: float = 0.0
    target_price: float = 0.0
    stop_loss: float = 0.0
    confidence: int = 50
    reasoning: str = ""
    persona: str = ""
    status: str = "open"  # open/closed
    closed_at: str = ""
    closed_price: float = 0.0
    pnl_pct: float = 0.0
    outcome: str = ""  # win/loss/neutral
    # Phase 2: 전략 메타데이터
    strategy_type: str = "일반"  # 단기매매/중기보유/리밸런싱/세금전략/관망
    strategy_tags: str = ""      # 콤마 구분: RSI반등,볼린저하단,...
    horizon_days: int = 7
    benchmark_ticker: str = ""
    execution_condition: str = ""
    invalidation_condition: str = ""
    risk_reward: float = 0.0
    agreement_count: int = 0     # 분석가 동의 수 (4명 중)


def calibrate_confidence(ticker: str, raw_confidence: int) -> int:
    """종목별 과거 적중률 기반으로 확신도를 보정한다.

    보정 공식:
        최종 = raw + 종목 보정 + 샘플 보정
        - 승률 70%+ & 양수 수익 → +10%
        - 승률 30% 미만 → -30%
        - 샘플 4건 미만 → 보정폭 절반
        - 결과: 10~95 범위로 클램프
    """
    stats = get_accuracy_summary()
    s = stats.get(ticker)
    if not s or s["total"] < 2:
        return max(10, min(95, raw_confidence))  # 데이터 부족, 클램프만 적용

    adjustment = 0
    wr = s["win_rate"] or 0
    avg = s["avg_pnl"] or 0
    total = s["total"]

    # 위험 종목: 승률 30% 미만
    if wr < 30:
        adjustment = -30
    # 저신뢰: 승률 30~50%
    elif wr < 50:
        adjustment = -15
    # 보통: 승률 50~70%
    elif wr < 70:
        adjustment = 0
    # 고신뢰: 승률 70%+ & 양수 수익
    elif avg > 0:
        adjustment = 10
    else:
        adjustment = 5

    # 샘플 수 부족 시 과신 방지 (4건 미만이면 보정폭 절반)
    if total < 4:
        adjustment = adjustment // 2

    calibrated = raw_confidence + adjustment
    calibrated = max(10, min(95, calibrated))

    if adjustment != 0:
        log.info(
            "확신도 보정: %s %d%% → %d%% (조정 %+d, 승률 %.0f%%, %d건)",
            ticker, raw_confidence, calibrated, adjustment, wr, total,
        )

    return calibrated


_VALID_STRATEGY_TYPES = {"단기매매", "중기보유", "리밸런싱", "세금전략", "관망", "일반"}


def _normalize_strategy_type(val) -> str:
    """strategy_type을 정규화. None/빈값/잘못된 값 → '일반'."""
    if not val or not isinstance(val, str) or not val.strip():
        return "일반"
    cleaned = val.strip()
    return cleaned if cleaned in _VALID_STRATEGY_TYPES else "일반"


def _safe_int(val, default: int = 7) -> int:
    """LLM JSON에서 온 값을 안전하게 int로. '7일', null, '' 등 방어."""
    if val is None:
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = str(val).strip().rstrip("일days일 ")
    try:
        return int(float(s)) if s else default
    except (ValueError, TypeError):
        return default


def _safe_float(val, default: float = 0.0) -> float:
    """LLM JSON에서 온 값을 안전하게 float로. '2:1', null, '' 등 방어."""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return default
    # "2:1" → 2.0, "1:2" → 0.5 비율 해석
    if ":" in s:
        try:
            parts = s.split(":")
            return float(parts[0]) / float(parts[1])
        except (ValueError, ZeroDivisionError):
            return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _is_duplicate_prediction(ticker: str, signal: str, strategy_type: str = "일반") -> bool:
    """24시간 내 같은 종목+같은 방향+같은 전략유형의 미결(open) 추천이 있으면 중복.
    이미 closed된 추천은 중복으로 보지 않는다 (재추천 허용).
    같은 종목이라도 전략이 다르면 별도 저장 허용."""
    strategy_type = _normalize_strategy_type(strategy_type)
    conn = _get_conn()
    cutoff = (datetime.now(KST) - timedelta(hours=24)).isoformat()
    row = conn.execute(
        """SELECT COUNT(*) FROM predictions
           WHERE ticker = ? AND signal = ? AND COALESCE(strategy_type, '일반') = ?
           AND created_at > ? AND status = 'open'""",
        (ticker, signal, strategy_type, cutoff),
    ).fetchone()
    return (row[0] or 0) > 0


# ═══════════════════════════════════════════════════════
# 액션 등급
# ═══════════════════════════════════════════════════════
ACTION_IMMEDIATE = "IMMEDIATE_ACTION"
ACTION_CONDITIONAL = "CONDITIONAL_ACTION"
ACTION_WATCH = "WATCH"
ACTION_BLOCKED = "BLOCKED"


def _quality_gate(
    ticker: str,
    signal: str,
    confidence: int,
    entry_price: float,
    stop_loss: float,
    risk_reward: float,
    invalidation_condition: str,
    strategy_type: str,
    data_failures: int = 0,
    agreement_count: int = 0,
) -> tuple[str, int, str]:
    """추천 품질 게이트. (action_grade, adjusted_confidence, gate_reason) 반환.

    룰:
    1. 확신도 55 미만 → WATCH (매수/매도 관망 처리)
    2. 위험 종목(승률 30% 미만) → BLOCKED (예외: 손절+무효화+손익비2.0++동의3/4)
    3. 손절가 없으면 → BLOCKED
    4. 손익비 1.5 미만 (0 포함) → BLOCKED
    5. 진입가 0 → BLOCKED (매수/매도)
    6. 데이터 실패 1개당 confidence -10, 2개 이상이면 신규 매수 BLOCKED
    """
    reasons = []
    grade = ACTION_IMMEDIATE
    adj_conf = confidence

    # 데이터 품질 감점
    if data_failures > 0:
        penalty = data_failures * 10
        adj_conf -= penalty
        reasons.append(f"데이터실패{data_failures}건(-{penalty})")
        if data_failures >= 2 and signal == "매수":
            grade = ACTION_BLOCKED
            reasons.append("데이터2+실패→신규매수금지")

    # 위험 종목 체크
    stats = get_accuracy_summary()
    s = stats.get(ticker)
    if s and s["total"] >= 2 and (s["win_rate"] or 0) < 30:
        # 예외 조건: 4개 모두 충족해야 허용
        has_stoploss = stop_loss > 0
        has_invalidation = bool(invalidation_condition and invalidation_condition.strip())
        good_rr = risk_reward >= 2.0
        enough_agreement = agreement_count >= 3  # 분석가 4명 중 3명 이상 동의
        if has_stoploss and has_invalidation and good_rr and enough_agreement:
            grade = max_grade(grade, ACTION_CONDITIONAL)
            reasons.append(f"위험종목(승률{s['win_rate']:.0f}%)→예외허용(손절+무효화+손익비+동의{agreement_count}/4)")
        else:
            missing = []
            if not has_stoploss:
                missing.append("손절가")
            if not has_invalidation:
                missing.append("무효화조건")
            if not good_rr:
                missing.append(f"손익비{risk_reward:.1f}<2.0")
            if not enough_agreement:
                missing.append(f"동의{agreement_count}/4<3")
            grade = ACTION_BLOCKED
            reasons.append(f"위험종목(승률{s['win_rate']:.0f}%)→차단(미충족:{','.join(missing)})")

    # 확신도 기준
    if adj_conf < 55 and signal in ("매수", "매도"):
        grade = max_grade(grade, ACTION_WATCH)
        reasons.append(f"확신도{adj_conf}<55→관망")

    # 손절가 필수 (매수)
    if signal == "매수" and stop_loss <= 0:
        grade = max_grade(grade, ACTION_BLOCKED)
        reasons.append("손절가없음→저장금지")

    # 손익비 기준 (0 또는 누락도 차단)
    if signal == "매수" and risk_reward < 1.5:
        grade = max_grade(grade, ACTION_BLOCKED)
        reasons.append(f"손익비{risk_reward:.1f}<1.5→차단")

    # 진입가 0 → 차단 (실전 추천에 진입가 없으면 의미 없음)
    if entry_price <= 0 and signal in ("매수", "매도"):
        grade = max_grade(grade, ACTION_BLOCKED)
        reasons.append("진입가0→저장금지")

    reason_text = "; ".join(reasons) if reasons else "통과"
    return grade, max(10, min(95, adj_conf)), reason_text


def max_grade(current: str, new: str) -> str:
    """더 제한적인 등급 반환. BLOCKED > WATCH > CONDITIONAL > IMMEDIATE."""
    order = {ACTION_IMMEDIATE: 0, ACTION_CONDITIONAL: 1, ACTION_WATCH: 2, ACTION_BLOCKED: 3}
    return current if order.get(current, 0) >= order.get(new, 0) else new


def save_prediction(
    ticker: str,
    name: str,
    signal: str,
    entry_price: float,
    target_price: float = 0.0,
    stop_loss: float = 0.0,
    confidence: int = 50,
    reasoning: str = "",
    persona: str = "종합",
    strategy_type: str = "일반",
    strategy_tags: str = "",
    horizon_days: int = 7,
    benchmark_ticker: str = "",
    execution_condition: str = "",
    invalidation_condition: str = "",
    risk_reward: float = 0.0,
    data_failures: int = 0,
    agreement_count: int = 0,
) -> int:
    """새 추천 기록 저장. 품질 게이트 + 확신도 보정 + 중복 방지. Returns prediction ID."""
    # 정규화
    strategy_type = _normalize_strategy_type(strategy_type)

    # 중복 예측 방지 (원래 signal + 관망 변환 모두 체크)
    if _is_duplicate_prediction(ticker, signal, strategy_type):
        log.info("중복 예측 스킵: %s %s %s", ticker, signal, strategy_type)
        return 0
    if signal in ("매수", "매도") and _is_duplicate_prediction(ticker, "관망", strategy_type):
        log.info("중복 예측 스킵(관망 변환 중복): %s %s→관망 %s", ticker, signal, strategy_type)
        return 0

    # 확신도 보정 (종목별 과거 적중률)
    calibrated = calibrate_confidence(ticker, confidence)

    # 품질 게이트
    grade, gated_conf, gate_reason = _quality_gate(
        ticker, signal, calibrated, entry_price, stop_loss,
        risk_reward, invalidation_condition, strategy_type, data_failures,
        agreement_count,
    )

    if grade == ACTION_BLOCKED:
        log.info("품질 게이트 차단: %s %s %s — %s", ticker, signal, name, gate_reason)
        return 0

    # WATCH 등급은 저장하되 signal을 "관망"으로 변경
    if grade == ACTION_WATCH and signal in ("매수", "매도"):
        log.info("품질 게이트 관망: %s %s→관망 — %s", ticker, signal, gate_reason)
        signal = "관망"

    conn = _get_conn()
    now = datetime.now(KST).isoformat()
    cursor = conn.execute(
        """INSERT INTO predictions
           (created_at, ticker, name, signal, entry_price, target_price,
            stop_loss, confidence, reasoning, persona,
            strategy_type, strategy_tags, horizon_days, benchmark_ticker,
            execution_condition, invalidation_condition, risk_reward, agreement_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, ticker, name, signal, entry_price, target_price,
         stop_loss, gated_conf, reasoning + f" [게이트:{grade}|{gate_reason}]", persona,
         strategy_type, strategy_tags, horizon_days, benchmark_ticker,
         execution_condition, invalidation_condition, risk_reward, agreement_count),
    )
    conn.commit()

    if grade != ACTION_IMMEDIATE:
        log.info("품질 게이트 통과(%s): %s %s conf=%d — %s", grade, ticker, signal, gated_conf, gate_reason)

    return cursor.lastrowid or 0


def save_predictions_from_briefing(raw_json: dict, data_failures: int = 0) -> int:
    """브리핑 결과에서 추천 기록 자동 저장. 품질 게이트 적용. Returns 저장된 건수."""
    count = 0

    def _extract_tags(row: dict) -> str:
        tags = row.get("strategy_tags", [])
        if isinstance(tags, list):
            return ",".join(tags)
        return str(tags) if tags else ""

    for row in raw_json.get("strategy_buy", []):
        try:
            entry = _parse_price(row.get("entry_price", "0"))
            target = _parse_price(row.get("target_price", "0"))
            stop = _parse_price(row.get("stop_loss", "0"))

            pid = save_prediction(
                ticker=row.get("ticker", ""),
                name=row.get("name", ""),
                signal="매수",
                entry_price=entry,
                target_price=target,
                stop_loss=stop,
                reasoning=row.get("reason", "")[:200],
                strategy_type=_normalize_strategy_type(row.get("strategy_type")),
                strategy_tags=_extract_tags(row),
                horizon_days=_safe_int(row.get("horizon_days"), 7),
                benchmark_ticker=str(row.get("benchmark_ticker", "") or ""),
                execution_condition=str(row.get("execution_condition", "") or "")[:200],
                invalidation_condition=str(row.get("invalidation_condition", "") or "")[:200],
                risk_reward=_safe_float(row.get("risk_reward"), 0),
                data_failures=data_failures,
                agreement_count=_safe_int(row.get("agreement_count") or row.get("consensus_count"), 0),
            )
            if pid > 0:
                count += 1
        except Exception as e:
            log.debug(f"매수 추천 저장 실패: {e}")

    for row in raw_json.get("strategy_sell", []):
        try:
            entry = _parse_price(row.get("current_price", "0"))
            target = _parse_price(row.get("take_profit", "0"))
            stop = _parse_price(row.get("stop_loss", "0"))

            pid = save_prediction(
                ticker=row.get("ticker", ""),
                name=row.get("name", ""),
                signal="매도",
                entry_price=entry,
                target_price=target,
                stop_loss=stop,
                reasoning=row.get("reason", "")[:200],
                strategy_type=_normalize_strategy_type(row.get("strategy_type")),
                strategy_tags=_extract_tags(row),
                horizon_days=_safe_int(row.get("horizon_days"), 7),
                benchmark_ticker=str(row.get("benchmark_ticker", "") or ""),
                execution_condition=str(row.get("execution_condition", "") or "")[:200],
                invalidation_condition=str(row.get("invalidation_condition", "") or "")[:200],
                risk_reward=_safe_float(row.get("risk_reward"), 0),
                data_failures=data_failures,
                agreement_count=_safe_int(row.get("agreement_count") or row.get("consensus_count"), 0),
            )
            if pid > 0:
                count += 1
        except Exception as e:
            log.debug(f"매도 추천 저장 실패: {e}")

    return count


def _parse_price(val: str | float) -> float:
    """가격 문자열 파싱. '₩201,000' → 201000.0"""
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = str(val).replace("₩", "").replace("$", "").replace(",", "").replace("원", "").strip()
    # 범위 표기 (예: "198,000~202,000") → 중간값
    if "~" in cleaned:
        parts = cleaned.split("~")
        try:
            return (float(parts[0]) + float(parts[1])) / 2
        except (ValueError, IndexError):
            pass
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


# ═══════════════════════════════════════════════════════
# 추천 결과 평가 (자동)
# ═══════════════════════════════════════════════════════
def evaluate_open_predictions(current_prices: dict[str, float]) -> int:
    """미결 추천을 현재가로 평가하여 종료 처리.

    Args:
        current_prices: {ticker: current_price}

    Returns:
        종료된 건수
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM predictions WHERE status = 'open'"
    ).fetchall()

    closed_count = 0
    now = datetime.now(KST).isoformat()

    for row in rows:
        ticker = row["ticker"]
        if ticker not in current_prices:
            continue

        current = current_prices[ticker]
        entry = row["entry_price"]
        target = row["target_price"]
        stop = row["stop_loss"]
        signal = row["signal"]

        if entry <= 0:
            continue

        should_close = False
        outcome = "neutral"

        if signal == "매수":
            pnl = (current - entry) / entry * 100
            if target > 0 and current >= target:
                should_close = True
                outcome = "win"
            elif stop > 0 and current <= stop:
                should_close = True
                outcome = "loss"
            # 7일 이상 경과 시 자동 평가
            elif _days_since(row["created_at"]) >= 7:
                should_close = True
                outcome = "win" if pnl > 0 else "loss" if pnl < -3 else "neutral"
        elif signal == "매도":
            pnl = (entry - current) / entry * 100
            if target > 0 and current <= target:
                should_close = True
                outcome = "win"
            elif stop > 0 and current >= stop:
                should_close = True
                outcome = "loss"
            elif _days_since(row["created_at"]) >= 7:
                should_close = True
                outcome = "win" if pnl > 0 else "loss" if pnl < -3 else "neutral"
        else:
            continue

        if should_close:
            pnl_pct = (current - entry) / entry * 100 if signal == "매수" else (entry - current) / entry * 100
            conn.execute(
                """UPDATE predictions
                   SET status='closed', closed_at=?, closed_price=?,
                       pnl_pct=?, outcome=?
                   WHERE id=?""",
                (now, current, round(pnl_pct, 2), outcome, row["id"]),
            )
            closed_count += 1

    if closed_count > 0:
        conn.commit()
        _update_accuracy_stats()

    return closed_count


def _days_since(iso_date: str) -> int:
    try:
        created = datetime.fromisoformat(iso_date)
        now = datetime.now(KST)
        return (now - created).days
    except Exception:
        return 0


def _update_accuracy_stats() -> None:
    """정확도 통계 업데이트."""
    conn = _get_conn()
    now = datetime.now(KST).isoformat()

    rows = conn.execute(
        """SELECT ticker,
                  COUNT(*) as total,
                  SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                  SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                  AVG(pnl_pct) as avg_pnl
           FROM predictions
           WHERE status='closed'
           GROUP BY ticker"""
    ).fetchall()

    for row in rows:
        total = row["total"]
        wins = row["wins"]
        win_rate = (wins / total * 100) if total > 0 else 0

        conn.execute(
            """INSERT OR REPLACE INTO accuracy_stats
               (ticker, total_predictions, correct, wrong, avg_pnl, win_rate, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (row["ticker"], total, wins, row["losses"],
             round(row["avg_pnl"] or 0, 2), round(win_rate, 1), now),
        )
    conn.commit()


# ═══════════════════════════════════════════════════════
# 메모리 조회 (프롬프트용)
# ═══════════════════════════════════════════════════════
def get_recent_predictions(limit: int = 20) -> list[Prediction]:
    """최근 추천 기록 조회."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM predictions
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    def _row_get(row, key, default=""):
        try:
            return row[key] if row[key] is not None else default
        except (IndexError, KeyError):
            return default

    return [
        Prediction(
            id=r["id"],
            created_at=r["created_at"],
            ticker=r["ticker"],
            name=r["name"],
            signal=r["signal"],
            entry_price=r["entry_price"],
            target_price=r["target_price"] or 0,
            stop_loss=r["stop_loss"] or 0,
            confidence=r["confidence"],
            reasoning=r["reasoning"] or "",
            persona=r["persona"] or "",
            status=r["status"],
            closed_at=r["closed_at"] or "",
            closed_price=r["closed_price"] or 0,
            pnl_pct=r["pnl_pct"] or 0,
            outcome=r["outcome"] or "",
            # Phase 2 필드
            strategy_type=_row_get(r, "strategy_type", "일반"),
            strategy_tags=_row_get(r, "strategy_tags", ""),
            horizon_days=_row_get(r, "horizon_days", 7) or 7,
            benchmark_ticker=_row_get(r, "benchmark_ticker", ""),
            execution_condition=_row_get(r, "execution_condition", ""),
            invalidation_condition=_row_get(r, "invalidation_condition", ""),
            risk_reward=_row_get(r, "risk_reward", 0) or 0,
            agreement_count=_row_get(r, "agreement_count", 0) or 0,
        )
        for r in rows
    ]


def get_accuracy_summary() -> dict[str, dict]:
    """종목별 정확도 통계."""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM accuracy_stats").fetchall()
    return {
        r["ticker"]: {
            "total": r["total_predictions"],
            "wins": r["correct"],
            "losses": r["wrong"],
            "avg_pnl": r["avg_pnl"],
            "win_rate": r["win_rate"],
        }
        for r in rows
    }


def get_strategy_accuracy_summary() -> dict[str, dict]:
    """전략 유형별 정확도 통계. Phase 2."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT strategy_type,
               COUNT(*) as total,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
               AVG(pnl_pct) as avg_pnl
        FROM predictions
        WHERE status = 'closed' AND strategy_type != ''
        GROUP BY strategy_type
        HAVING total >= 2
        ORDER BY total DESC
    """).fetchall()

    result = {}
    for r in rows:
        total = r["total"]
        wins = r["wins"] or 0
        result[r["strategy_type"]] = {
            "total": total,
            "wins": wins,
            "losses": r["losses"] or 0,
            "avg_pnl": r["avg_pnl"] or 0,
            "win_rate": (wins / total * 100) if total > 0 else 0,
        }
    return result


def get_tag_accuracy_summary() -> dict[str, dict]:
    """전략 태그별 정확도 통계. Phase 2."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT strategy_tags, outcome, pnl_pct
        FROM predictions
        WHERE status = 'closed' AND strategy_tags != ''
    """).fetchall()

    # 태그별 집계 (콤마 구분 태그를 개별로 분리)
    tag_stats: dict[str, list] = {}
    for r in rows:
        tags = [t.strip() for t in (r["strategy_tags"] or "").split(",") if t.strip()]
        for tag in tags:
            if tag not in tag_stats:
                tag_stats[tag] = []
            tag_stats[tag].append({"outcome": r["outcome"], "pnl": r["pnl_pct"] or 0})

    result = {}
    for tag, entries in tag_stats.items():
        if len(entries) < 2:
            continue
        total = len(entries)
        wins = sum(1 for e in entries if e["outcome"] == "win")
        avg_pnl = sum(e["pnl"] for e in entries) / total
        result[tag] = {
            "total": total,
            "wins": wins,
            "avg_pnl": avg_pnl,
            "win_rate": (wins / total * 100) if total > 0 else 0,
        }
    return result


def memory_to_text() -> str:
    """메모리를 텍스트로 변환 (프롬프트 삽입용)."""
    predictions = get_recent_predictions(10)
    accuracy = get_accuracy_summary()

    lines = ["【AI 메모리 — 과거 추천 기록】"]

    if accuracy:
        lines.append("\n  [정확도 통계]")
        for ticker, stats in accuracy.items():
            lines.append(
                f"  {ticker}: {stats['total']}건 중 {stats['wins']}적중 "
                f"(승률 {stats['win_rate']:.0f}%, 평균 {stats['avg_pnl']:+.1f}%)"
            )

    if predictions:
        open_preds = [p for p in predictions if p.status == "open"]
        closed_preds = [p for p in predictions if p.status == "closed"]

        if open_preds:
            lines.append("\n  [미결 추천]")
            for p in open_preds[:5]:
                lines.append(
                    f"  {p.created_at[:10]} {p.name} {p.signal} "
                    f"진입 {p.entry_price:,.0f} → 목표 {p.target_price:,.0f}"
                )

        if closed_preds:
            lines.append("\n  [최근 종료]")
            for p in closed_preds[:5]:
                icon = "✅" if p.outcome == "win" else "❌" if p.outcome == "loss" else "➖"
                lines.append(
                    f"  {icon} {p.name} {p.signal}: {p.pnl_pct:+.1f}% [{p.outcome}]"
                )

    # 신뢰도 기반 피드백 주입
    if accuracy:
        high = [t for t, s in accuracy.items() if s["total"] >= 2 and s["win_rate"] >= 70 and s["avg_pnl"] > 0]
        danger = [t for t, s in accuracy.items() if s["total"] >= 2 and s["win_rate"] < 30]
        if high or danger:
            lines.append("\n  [⚡ 신뢰도 기반 판단 보정]")
        if danger:
            conn = _get_conn()
            for t in danger:
                s = accuracy[t]
                penalty = -30 if s["total"] >= 4 else -15
                lines.append(
                    f"  🔴 {t}: 승률 {s['win_rate']:.0f}% 평균 {s['avg_pnl']:+.1f}% ({s['total']}건) "
                    f"→ 확신도 {penalty:+d}% 자동 보정됨."
                )
                # 최근 실패 이유 추가
                fails = conn.execute(
                    """SELECT signal, substr(reasoning, 1, 120) as r FROM predictions
                       WHERE ticker = ? AND outcome = 'loss'
                       ORDER BY closed_at DESC LIMIT 2""",
                    (t,),
                ).fetchall()
                for f in fails:
                    lines.append(f"    실패 기록: {f[0]} | {f[1]}")
        if high:
            for t in high:
                s = accuracy[t]
                bonus = 10 if s["total"] >= 4 else 5
                lines.append(
                    f"  🟢 {t}: 승률 {s['win_rate']:.0f}% 평균 {s['avg_pnl']:+.1f}% ({s['total']}건) "
                    f"→ 확신도 +{bonus}% 자동 가중됨."
                )

    # Phase 2: 전략 유형별 성과
    strat_stats = get_strategy_accuracy_summary()
    if strat_stats:
        lines.append("\n  [📊 전략 유형별 성과]")
        for stype, s in strat_stats.items():
            icon = "✅" if s["win_rate"] >= 60 else "⚠️" if s["win_rate"] >= 40 else "❌"
            lines.append(
                f"  {icon} {stype}: {s['total']}건 승률 {s['win_rate']:.0f}% 평균 {s['avg_pnl']:+.1f}%"
            )

    # Phase 2: 전략 태그별 성과
    tag_stats = get_tag_accuracy_summary()
    if tag_stats:
        lines.append("\n  [🏷️ 전략 태그별 성과]")
        sorted_tags = sorted(tag_stats.items(), key=lambda x: x[1]["total"], reverse=True)
        for tag, s in sorted_tags[:8]:
            icon = "✅" if s["win_rate"] >= 60 else "⚠️" if s["win_rate"] >= 40 else "❌"
            lines.append(
                f"  {icon} {tag}: {s['total']}건 승률 {s['win_rate']:.0f}% 평균 {s['avg_pnl']:+.1f}%"
            )

    if len(lines) == 1:
        lines.append("  (기록 없음 — 첫 브리핑 후 축적됩니다)")

    return "\n".join(lines)
