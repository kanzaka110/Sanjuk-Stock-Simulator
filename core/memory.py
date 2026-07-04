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

        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT,
            alert_type TEXT,
            severity TEXT,
            title TEXT,
            message TEXT,
            price REAL DEFAULT 0,
            delivered INTEGER DEFAULT 0,
            suppress_reason TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_alert_history_created
            ON alert_history(created_at);
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
    # Phase 4 마이그레이션: 향후 축적용 컬럼 (과거 소급 없이 NULL 유지)
    _migrate_phase4(conn)


def _migrate_phase4(conn: sqlite3.Connection) -> None:
    """Phase 4 컬럼 안전 추가 — 향후 데이터부터 축적."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(predictions)").fetchall()}
    new_columns = [
        ("action_grade", "TEXT"),        # IMMEDIATE/CONDITIONAL/WATCH/BLOCKED
        ("action_type", "TEXT"),          # 즉시매수/조건부매수/즉시매도/조건부매도/관망
        ("account_type", "TEXT"),         # 일반/ISA/RIA/IRP/연금
        ("briefing_type", "TEXT"),        # KR_NIGHT/US_NIGHT/KR_BEFORE/US_BEFORE/MANUAL
        ("original_signal", "TEXT"),      # AI 원본 signal (정규화 전)
        ("data_quality", "TEXT"),         # good/suspect/error
        ("normalizer_version", "TEXT DEFAULT ''"),  # v1=normalizer 경유 / legacy=과거 / ''=미분류
    ]
    for col_name, col_def in new_columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col_name} {col_def}")
            log.info("Phase 4 마이그레이션: predictions.%s 추가", col_name)
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


def losing_streak_tickers(min_streak: int = 3) -> dict[str, int]:
    """종목별 최근 연속 손실(연패) 횟수 — min_streak 이상만 반환.

    가장 최근 closed(win/loss) 기록부터 거슬러 세어 연속 loss 횟수를 계산.
    neutral/invalid/data_error는 연패 판정에서 제외 (건너뜀 아님 — 분모 제외).
    """
    try:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT ticker, outcome FROM predictions
               WHERE status='closed' AND outcome IN ('win','loss')
               ORDER BY closed_at DESC, id DESC"""
        ).fetchall()
    except Exception as e:
        log.debug("연패 조회 실패: %s", e)
        return {}

    streaks: dict[str, int] = {}
    done: set[str] = set()
    for r in rows:
        t = normalize_ticker(r["ticker"])
        if t in done:
            continue
        if r["outcome"] == "loss":
            streaks[t] = streaks.get(t, 0) + 1
        else:
            done.add(t)  # 최근에 win이 나오면 연패 종료
    return {t: n for t, n in streaks.items() if n >= min_streak}


def calibrate_confidence(ticker: str, raw_confidence: int) -> int:
    """종목별 기대값(expectancy) + profit_factor + 연패 기반 확신도 보정.

    보정 기준 (2026-07 확대판 — 오염 통계 정리 후 실측 기반):
        - evaluated_count < 3 → 보정 없음 (샘플부족)
        - expectancy > 0 & profit_factor > 1.5 & evaluated >= 8 → +15
        - expectancy > 0 & profit_factor > 1.2 & evaluated >= 5 → +10
        - expectancy > 0 & profit_factor > 1.0 → +3~+5
        - 심각한 음수 expectancy & evaluated >= 8 → -30 (>= 5 → -20)
        - expectancy < 0 or profit_factor < 1.0 → -5~-15
        - 3연패 이상 → 추가 -10 + 결과 상한 40 (자동 회피 신호)
        - 결과: 10~95 범위로 클램프
    """
    ticker = normalize_ticker(ticker)
    stats = get_accuracy_summary()
    s = stats.get(ticker)
    if not s or s["total"] < 2:
        return max(10, min(95, raw_confidence))

    evaluated = s.get("evaluated_count", s.get("wins", 0) + s.get("losses", 0))
    if evaluated < 3:
        return max(10, min(95, raw_confidence))  # 샘플부족, 보정 스킵

    exp = s.get("expectancy", 0) or 0
    pf = s.get("profit_factor", 0) or 0
    adjustment = 0

    if exp > 0 and pf > 1.5 and evaluated >= 8:
        # 최고신뢰: 검증된 우위 + 대표본
        adjustment = 15
    elif exp > 0 and pf > 1.2 and evaluated >= 5:
        # 고신뢰: 양수 기대값 + 높은 profit_factor + 충분한 샘플
        adjustment = 10
    elif exp > 0 and pf > 1.0:
        # 양호: 양수 기대값 + profit_factor > 1
        adjustment = 5 if evaluated >= 5 else 3
    elif exp < -3 and evaluated >= 5:
        # 심각: 음수 기대값 + 충분한 샘플 → 강한 감점
        adjustment = -30 if evaluated >= 8 else -20
    elif exp < 0 or pf < 1.0:
        # 부진: 음수 기대값 또는 profit_factor < 1
        adjustment = -15 if evaluated >= 5 else -5

    streak = losing_streak_tickers(min_streak=3).get(ticker, 0)
    cap = 95
    if streak:
        adjustment -= 10
        cap = 40  # 3연패 종목은 어떤 근거로도 고확신 금지

    calibrated = max(10, min(cap, raw_confidence + adjustment))

    if adjustment != 0:
        log.info(
            "확신도 보정: %s %d%% → %d%% (조정 %+d, expectancy=%.1f%%, PF=%.2f, %d건, 연패=%d)",
            ticker, raw_confidence, calibrated, adjustment, exp, pf, evaluated, streak,
        )

    return calibrated


# ═══════════════════════════════════════════════════════
# 티커 정규화 — AI가 생성한 다양한 표기를 표준 yfinance 코드로 변환
# ═══════════════════════════════════════════════════════
_TICKER_ALIASES: dict[str, str] = {
    # KODEX 200
    "069500": "069500.KS",
    "KODEX 200": "069500.KS",
    "KODEX_200": "069500.KS",
    "KODEX200": "069500.KS",
    # KODEX 반도체
    "091160": "091160.KS",
    "KODEX 반도체": "091160.KS",
    "KODEX_반도체": "091160.KS",
    "KODEX반도체": "091160.KS",
    # KODEX 레버리지
    "122630": "122630.KS",
    "KODEX 레버리지": "122630.KS",
    "KODEX_레버리지": "122630.KS",
    "KODEX레버리지": "122630.KS",
    # KODEX 자동차
    "091180": "091180.KS",
    "KODEX 자동차": "091180.KS",
    "KODEX_자동차": "091180.KS",
    "KODEX자동차": "091180.KS",
    # KODEX 코스닥150
    "229200": "229200.KS",
    "KODEX 코스닥150": "229200.KS",
    "KODEX코스닥150": "229200.KS",
    # TIGER 200
    "102110": "102110.KS",
    "TIGER 200": "102110.KS",
    "TIGER200": "102110.KS",
    # TIGER 미국나스닥100
    "133690": "133690.KS",
    "TIGER 미국나스닥100": "133690.KS",
    "TIGER미국나스닥100": "133690.KS",
    "TIGER 나스닥100": "133690.KS",
    # TIGER 미국S&P500
    "360750": "360750.KS",
    "TIGER 미국S&P500": "360750.KS",
    "TIGER미국S&P500": "360750.KS",
    "TIGER S&P500": "360750.KS",
    # KODEX MSCI선진국
    "251350": "251350.KS",
    "KODEX MSCI선진국": "251350.KS",
    "KODEX MSCI 선진국": "251350.KS",
    # PLUS 고배당주
    "161510": "161510.KS",
    "PLUS 고배당주": "161510.KS",
    "PLUS고배당주": "161510.KS",
    # TIGER 리츠부동산인프라
    "329200": "329200.KS",
    "TIGER 리츠부동산인프라": "329200.KS",
    "TIGER리츠부동산인프라": "329200.KS",
    "TIGER 리츠": "329200.KS",
    # TIGER 차이나CSI300
    "192090": "192090.KS",
    "TIGER 차이나CSI300": "192090.KS",
    "TIGER차이나CSI300": "192090.KS",
}


def normalize_ticker(ticker: str) -> str:
    """AI가 생성한 티커를 표준 yfinance 코드로 정규화.

    예: '069500' → '069500.KS', 'KODEX 200' → '069500.KS'
    이미 정규화된 코드는 그대로 반환.
    """
    if not ticker:
        return ticker
    stripped = ticker.strip()
    # 직접 매칭
    if stripped in _TICKER_ALIASES:
        return _TICKER_ALIASES[stripped]
    # 이미 .KS/.KQ 접미사가 있으면 그대로
    if stripped.endswith((".KS", ".KQ")):
        return stripped
    # 숫자 6자리 (한국 종목코드) → .KS 추가
    if stripped.isdigit() and len(stripped) == 6:
        return f"{stripped}.KS"
    return stripped


# 가격 괴리 검증 임계값 (10%)
PRICE_DIVERGENCE_THRESHOLD = 0.10


_VALID_STRATEGY_TYPES = {"신규진입", "장기적립", "단기매매", "중기보유", "리밸런싱", "세금전략", "관망", "일반"}


def _normalize_strategy_type(val) -> str:
    """strategy_type을 정규화. None/빈값/잘못된 값 → '일반'."""
    if not val or not isinstance(val, str) or not val.strip():
        return "일반"
    cleaned = val.strip()
    return cleaned if cleaned in _VALID_STRATEGY_TYPES else "일반"


_VALID_ACCOUNTS = {"일반", "ISA", "RIA", "IRP", "연금저축"}


def _normalize_account(val, ticker: str = "") -> str:
    """계좌 태그 정규화. '[ISA]'/'isa'/'ISA' → 'ISA'. 누락 시 시장 기반 추정.

    AI가 strategy_buy의 account 필드를 누락하던 버그로 account_type이 비던 문제 방어.
    """
    if isinstance(val, str) and val.strip():
        cleaned = val.strip().strip("[]").strip()
        upper = cleaned.upper()
        # 연금/연금저축 동의어
        if cleaned in ("연금", "연금저축") or upper == "CMA":
            return "연금저축"
        if upper in ("ISA", "RIA", "IRP"):
            return upper
        if cleaned == "일반":
            return "일반"
    # 누락 시 시장 기반 기본값 추정 (RIA는 국내 ETF 한정이라 자동 추정 제외)
    if ticker:
        norm = normalize_ticker(ticker)
        if norm.endswith((".KS", ".KQ")):
            return "ISA"  # 국내주식 → ISA 우선 (세제 혜택)
        return "일반"     # 미국주식 → 일반
    return ""


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
    current_price: float | None = None,
    target_price: float = 0.0,
) -> tuple[str, int, str]:
    """추천 품질 게이트. (action_grade, adjusted_confidence, gate_reason) 반환.

    룰:
    1. 확신도 40 미만 → WATCH (매수만. 매도는 확신도 무관하게 통과)
    2. 위험 종목(승률 30% 미만 & evaluated_count >= 5) → BLOCKED
       (evaluated < 5면 샘플부족으로 차단 안 함)
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

    # 위험 종목 체크 — evaluated_count 5건 이상일 때만 적용 (샘플부족 차단 방지)
    stats = get_accuracy_summary()
    s = stats.get(ticker)
    evaluated = s.get("evaluated_count", 0) if s else 0
    if s and evaluated >= 5 and (s["win_rate"] or 0) < 30:
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

    # 확신도 기준 — 매수만 적용 (매도는 확신도 무관하게 통과, 손절 놓치면 안 됨)
    if adj_conf < 40 and signal == "매수":
        grade = max_grade(grade, ACTION_WATCH)
        reasons.append(f"확신도{adj_conf}<40→관망")

    # 장기 적립식 매수: 손절가/손익비 대신 무효화 조건으로 평가
    # (장기 코어 적립은 단기 손절 개념이 부적합 — 논지 훼손 조건이 핵심)
    is_long_term = strategy_type == "장기적립"
    has_invalidation = bool(invalidation_condition and invalidation_condition.strip())

    # 메타데이터 누락 감점 — 채점 인프라(벤치마크 알파/무효화 추적) 가동률 확보
    # (과거 기록 85%가 누락 → 정확도 측정 불가였음. 누락 시 confidence -5씩)
    if signal in ("매수", "매도"):
        if not has_invalidation:
            adj_conf -= 5
            reasons.append("무효화조건누락(-5)")

    # 손절가 필수 (매수) — 장기적립은 무효화 조건으로 대체 가능
    if signal == "매수" and stop_loss <= 0:
        if is_long_term and has_invalidation:
            grade = max_grade(grade, ACTION_CONDITIONAL)
            reasons.append("장기적립: 손절가 대신 무효화조건 인정")
        else:
            grade = max_grade(grade, ACTION_BLOCKED)
            reasons.append("손절가없음→저장금지")

    # 손익비 기준 (0 또는 누락도 차단) — 장기적립은 면제
    if signal == "매수" and risk_reward < 1.5 and not is_long_term:
        grade = max_grade(grade, ACTION_BLOCKED)
        reasons.append(f"손익비{risk_reward:.1f}<1.5→차단")

    # 진입가 0 → 차단 (실전 추천에 진입가 없으면 의미 없음)
    if entry_price <= 0 and signal in ("매수", "매도"):
        grade = max_grade(grade, ACTION_BLOCKED)
        reasons.append("진입가0→저장금지")

    # 레벨 정합성 — 목표/손절이 진입가와 모순이면 차단
    # (모순 레벨은 평가 시점에 즉시 win/loss 0%로 종료돼 승률 통계를 오염시킴)
    if entry_price > 0:
        if signal == "매수":
            if target_price > 0 and target_price <= entry_price:
                grade = max_grade(grade, ACTION_BLOCKED)
                reasons.append(
                    f"레벨모순: 매수 목표가({target_price:,.0f})≤진입가({entry_price:,.0f})")
            if stop_loss > 0 and stop_loss >= entry_price:
                grade = max_grade(grade, ACTION_BLOCKED)
                reasons.append(
                    f"레벨모순: 매수 손절가({stop_loss:,.0f})≥진입가({entry_price:,.0f})")
        elif signal == "매도":
            # 평가 로직은 매도를 숏 관점(진입가 대비 하락=수익)으로 채점하므로
            # 목표가는 진입가 아래, 손절가는 진입가 위여야 한다
            if target_price > 0 and target_price >= entry_price:
                grade = max_grade(grade, ACTION_BLOCKED)
                reasons.append(
                    f"레벨모순: 매도 목표가({target_price:,.0f})≥진입가({entry_price:,.0f})")
            if stop_loss > 0 and stop_loss <= entry_price:
                grade = max_grade(grade, ACTION_BLOCKED)
                reasons.append(
                    f"레벨모순: 매도 손절가({stop_loss:,.0f})≤진입가({entry_price:,.0f})")

    # 가격 괴리 검증 (current_price가 명시적으로 전달된 경우만 적용)
    # 단 신규진입 눌림목 예약(매수): 진입가가 현재가보다 낮은 건 의도된 것 — 차단 예외.
    # (발굴주가 급등 후 눌림목 대기인데 괴리 게이트에 막혀 매수로 못 가던 숨은 버그)
    is_pullback_buy = signal == "매수" and strategy_type == "신규진입"
    if current_price is not None and signal in ("매수", "매도"):
        if current_price > 0 and entry_price > 0:
            divergence = abs(entry_price - current_price) / current_price
            # 눌림목 예약은 "현재가보다 낮은 진입가"를 더 넓게 허용 (최대 -30%)
            pullback_below = is_pullback_buy and entry_price < current_price
            threshold = 0.30 if pullback_below else PRICE_DIVERGENCE_THRESHOLD
            if divergence > threshold:
                grade = max_grade(grade, ACTION_BLOCKED)
                reasons.append(
                    f"진입가-현재가 괴리 초과(현재가={current_price:,.0f}, "
                    f"진입가={entry_price:,.0f}, 괴리율={divergence:.1%})"
                )
            elif pullback_below:
                # 눌림목 예약은 매수로 보존하되 '조건부(예약 대기)' 등급으로 표시
                grade = max_grade(grade, ACTION_CONDITIONAL)
                reasons.append(f"눌림목 예약(진입가 {entry_price:,.0f}, 현재 {current_price:,.0f}) → 예약 대기")
        elif current_price == 0 and entry_price > 0:
            # 현재가 없는 신규 매수/매도 → 차단 (시세 미수집 종목)
            grade = max_grade(grade, ACTION_BLOCKED)
            reasons.append("현재가없음→시세미수집종목저장차단")

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
    current_price: float | None = None,
    # Phase 4: 향후 축적용
    action_grade: str = "",
    action_type: str = "",
    account_type: str = "",
    briefing_type: str = "",
    data_quality: str = "good",
    normalizer_version: str = "",
) -> int:
    """새 추천 기록 저장. 품질 게이트 + 확신도 보정 + 중복 방지. Returns prediction ID."""
    # 티커 정규화 (AI 할루시네이션 방지)
    ticker = normalize_ticker(ticker)
    # 전략 정규화
    strategy_type = _normalize_strategy_type(strategy_type)
    # original_signal은 WATCH 변환(관망) 전 원본을 보존 (이후 signal이 바뀌어도 유지)
    original_signal = signal

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
        agreement_count, current_price, target_price=target_price,
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
    # action_grade를 품질 게이트 결과로 자동 설정 (명시 지정 없으면)
    if not action_grade:
        action_grade = grade
    cursor = conn.execute(
        """INSERT INTO predictions
           (created_at, ticker, name, signal, entry_price, target_price,
            stop_loss, confidence, reasoning, persona,
            strategy_type, strategy_tags, horizon_days, benchmark_ticker,
            execution_condition, invalidation_condition, risk_reward, agreement_count,
            action_grade, action_type, account_type, briefing_type, original_signal, data_quality,
            normalizer_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, ticker, name, signal, entry_price, target_price,
         stop_loss, gated_conf, reasoning + f" [게이트:{grade}|{gate_reason}]", persona,
         strategy_type, strategy_tags, horizon_days, benchmark_ticker,
         execution_condition, invalidation_condition, risk_reward, agreement_count,
         action_grade, action_type, account_type, briefing_type, original_signal, data_quality,
         normalizer_version),
    )
    conn.commit()

    if grade != ACTION_IMMEDIATE:
        log.info("품질 게이트 통과(%s): %s %s conf=%d — %s", grade, ticker, signal, gated_conf, gate_reason)

    return cursor.lastrowid or 0


# action_type → 저장 signal 매핑 (CANCEL/HOLD는 매도 실행으로 저장하지 않음)
_ACTION_TYPE_SIGNAL = {
    "AI_NEW_BUY": "매수",
    "AI_ADD_BUY": "매수",
    "CONDITIONAL_NEW_BUY": "매수",
    "AI_SELL_MANAGEMENT": "매도",
    "CANCEL_SELL": "관망",
    "HOLD_REVIEW": "관망",
    "WATCH_ONLY": "관망",
}


def save_predictions_from_briefing(
    raw_json: dict,
    data_failures: int = 0,
    current_prices: dict[str, float] | None = None,
    briefing_type: str = "",
    normalized: dict | None = None,
) -> int:
    """브리핑 결과를 정규화 분류(normalized) 기반으로 저장. Returns 저장 건수.

    normalized가 있으면 그 분류(executable/conditional/cancelled)를 신뢰해 각 액션을
    action_type/briefing_type과 함께 저장한다. CANCEL_SELL/HOLD_REVIEW는 signal='관망'으로
    저장되어 'signal=매도인데 매도 취소 reason' 모순이 구조적으로 발생하지 않는다.
    normalized가 없으면(데일리 리뷰 등) 저장하지 않는다.
    """
    count = 0
    prices = current_prices

    def _extract_tags(row: dict) -> str:
        tags = row.get("strategy_tags", [])
        if isinstance(tags, list):
            return ",".join(tags)
        return str(tags) if tags else ""

    def _resolve_current_price(ticker: str) -> float | None:
        if prices is None:
            return None
        norm = normalize_ticker(ticker)
        return prices.get(norm, prices.get(ticker, 0.0))

    def _default_benchmark(ticker: str, provided: str) -> str:
        if provided and provided.strip():
            return provided.strip()
        return "^KS11" if normalize_ticker(ticker).endswith((".KS", ".KQ")) else "^GSPC"

    if normalized is None:
        return 0  # 정규화 결과 없는 브리핑(데일리 리뷰)은 추천 저장 안 함

    # 실행/조건부/취소 액션을 한 묶음으로 저장 (각자의 action_type 보존)
    all_actions = (
        list(normalized.get("executable_actions", []))
        + list(normalized.get("conditional_buy_candidates", []))
        + list(normalized.get("cancelled_sells", []))
    )

    # action_type → action_grade 강제 (결정론적 — DB 모순 방지)
    # 조건부 매수는 IMMEDIATE 금지, 취소/홀딩은 실행 등급 금지
    _grade_override = {
        "CONDITIONAL_NEW_BUY": ACTION_CONDITIONAL,
        "CANCEL_SELL": ACTION_WATCH,
        "HOLD_REVIEW": ACTION_WATCH,
        "WATCH_ONLY": ACTION_WATCH,
    }

    for act in all_actions:
        try:
            row = act.get("_raw", {})
            action_type = act.get("action_type", "")
            signal = _ACTION_TYPE_SIGNAL.get(action_type, "관망")
            raw_ticker = act.get("ticker", "") or row.get("ticker", "")

            if act.get("side") == "sell":
                entry = _parse_price(row.get("current_price", "0"))
                target = _parse_price(row.get("take_profit", "0"))
            else:
                entry = _parse_price(row.get("entry_price", "0"))
                target = _parse_price(row.get("target_price", "0"))
            stop = _parse_price(row.get("stop_loss", "0"))
            cur_price = _resolve_current_price(raw_ticker)

            pid = save_prediction(
                ticker=raw_ticker,
                name=act.get("name", "") or row.get("name", ""),
                signal=signal,
                entry_price=entry,
                target_price=target,
                stop_loss=stop,
                reasoning=str(row.get("reason", ""))[:200],
                strategy_type=_normalize_strategy_type(row.get("strategy_type")),
                strategy_tags=_extract_tags(row),
                horizon_days=_safe_int(row.get("horizon_days"), 7),
                benchmark_ticker=_default_benchmark(raw_ticker, str(row.get("benchmark_ticker", "") or "")),
                execution_condition=str(row.get("execution_condition", "") or "")[:200],
                invalidation_condition=str(row.get("invalidation_condition", "") or "")[:200],
                risk_reward=_safe_float(row.get("risk_reward"), 0),
                data_failures=data_failures,
                agreement_count=_safe_int(row.get("agreement_count") or row.get("consensus_count"), 0),
                current_price=cur_price,
                account_type=_normalize_account(act.get("account", "") or row.get("account", ""), raw_ticker),
                action_type=action_type,
                briefing_type=briefing_type,
                action_grade=_grade_override.get(action_type, ""),
                normalizer_version="v1",
            )
            if pid > 0:
                count += 1
        except Exception as e:
            log.debug(f"추천 저장 실패({act.get('action_type','?')}): {e}")

    return count


def _parse_price(val: str | float) -> float:
    """가격 문자열 파싱.

    프롬프트가 강제하는 형식까지 모두 처리:
      '₩201,000' → 201000.0
      '₩58,000 이하' → 58000.0  (수식어 무시)
      '$185.00 이하 분할' → 185.0
      '198,000~202,000' → 200000.0  (범위 → 중간값)
      '시세 확인 필요' → 0.0
    """
    if isinstance(val, (int, float)):
        return float(val)

    import re

    cleaned = str(val).replace("₩", "").replace("$", "").replace(",", "").replace("원", "").strip()

    # 범위 표기 (예: "198000~202000", "58000 ~ 60000 이하") → 중간값
    if "~" in cleaned:
        nums = re.findall(r"\d+(?:\.\d+)?", cleaned)
        if len(nums) >= 2:
            try:
                return (float(nums[0]) + float(nums[1])) / 2
            except ValueError:
                pass

    # 단일 가격 + 수식어 ("58000 이하", "1차 58000 분할") → 최대 숫자 채택
    # ("1차", "2회" 같은 서수가 섞여도 가격이 가장 큰 숫자)
    nums = re.findall(r"\d+(?:\.\d+)?", cleaned)
    if nums:
        try:
            return max(float(n) for n in nums)
        except ValueError:
            pass
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

    # 14일 이상 된 미결 추천 자동 정리 (stale prediction 방지)
    # 장기적립 추천은 90일 유예 (장기 시계를 14일에 만료시키면 평가 왜곡)
    now = datetime.now(KST).isoformat()
    cutoff_14d = (datetime.now(KST) - timedelta(days=14)).isoformat()
    cutoff_90d = (datetime.now(KST) - timedelta(days=90)).isoformat()
    stale = conn.execute(
        """UPDATE predictions SET status = 'closed', closed_at = ?, outcome = 'expired'
           WHERE status = 'open' AND (
               (created_at < ? AND COALESCE(strategy_type, '') != '장기적립')
               OR created_at < ?
           )""",
        (now, cutoff_14d, cutoff_90d),
    ).rowcount
    if stale > 0:
        conn.commit()
        log.info(f"미결 추천 {stale}건 자동 만료 (14일 초과)")

    # 좀비 레코드 정리: entry_price=0 AND stop_loss=0 → invalid
    zombie = conn.execute(
        """UPDATE predictions SET status = 'closed', closed_at = ?, outcome = 'invalid'
           WHERE status = 'open' AND entry_price <= 0 AND stop_loss <= 0""",
        (now,),
    ).rowcount
    if zombie > 0:
        conn.commit()
        log.info(f"좀비 레코드 {zombie}건 정리 (entry_price=0, stop_loss=0)")

    rows = conn.execute(
        "SELECT * FROM predictions WHERE status = 'open'"
    ).fetchall()

    # 크로스마켓 폴백: 이번 브리핑 스냅샷에 없는 미결 종목은 yfinance로 시세 보충
    # (예: KR 브리핑 중 미국 종목 추천 평가 — 보충 실패 시 다음 기회로 이월)
    prices = dict(current_prices)
    for row in rows:
        tk = row["ticker"]
        if tk not in prices:
            p = _fetch_price_fallback(tk)
            if p is not None:
                prices[tk] = p

    closed_count = 0

    for row in rows:
        ticker = row["ticker"]
        if ticker not in prices:
            continue

        current = prices[ticker]
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
            # horizon_days 경과 시 자동 평가 (벤치마크 대비 알파 기준, 대칭 ±3% 밴드)
            elif _days_since(row["created_at"]) >= max(_row_get(row, "horizon_days", 7) or 7, 7):
                should_close = True
                outcome = _grade_horizon_pnl(pnl, row)
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
                # 매도는 절대 수익률 기준 대칭 밴드 (벤치마크 알파 부적합)
                outcome = "win" if pnl > 3 else "loss" if pnl < -3 else "neutral"
        else:
            continue

        if should_close:
            pnl_pct = (current - entry) / entry * 100 if signal == "매수" else (entry - current) / entry * 100
            # 레벨 모순 가드: 생성 당일 0% win/loss는 목표/손절이 진입가와 모순이었다는 뜻
            # (저장 게이트 도입 전 레거시 레코드 보호 — 승률 통계 오염 방지)
            if outcome in ("win", "loss") and abs(pnl_pct) < 0.01 and _days_since(row["created_at"]) < 1:
                outcome = "invalid"
                log.info("레벨 모순 감지: %s %s 즉시 0%% 종료 → invalid", ticker, signal)
            # 비현실 수익률 → data_error 처리
            threshold = _unrealistic_pnl_threshold(ticker)
            if abs(pnl_pct) > threshold:
                outcome = "data_error"
                log.info("비현실 수익률 감지: %s pnl=%.1f%% (임계 %.0f%%) → data_error",
                         ticker, pnl_pct, threshold)
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


def _row_get(row, key, default=""):
    """sqlite3.Row에서 컬럼 안전 조회 (마이그레이션 미적용 DB 호환)."""
    try:
        return row[key] if row[key] is not None else default
    except (IndexError, KeyError):
        return default


def _grade_horizon_pnl(pnl: float, row) -> str:
    """7일 경과 자동 평가 채점.

    벤치마크 수익률을 차감한 알파 기준으로 대칭 ±3% 밴드 적용.
    벤치마크 조회 실패 시 절대 수익률 기준으로 폴백.
    """
    benchmark = _row_get(row, "benchmark_ticker", "") or ""
    bench_return = _benchmark_return_since(benchmark, row["created_at"])
    ref = pnl - bench_return if bench_return is not None else pnl
    return "win" if ref > 3 else "loss" if ref < -3 else "neutral"


def _fetch_price_fallback(ticker: str) -> float | None:
    """yfinance로 단일 종목 현재가 보충 조회. 실패 시 None."""
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        try:
            price = float(t.fast_info["last_price"])
            if price > 0:
                return price
        except Exception:
            pass
        hist = t.history(period="2d")
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
            return price if price > 0 else None
    except Exception as e:
        log.debug("시세 폴백 조회 실패 (%s): %s", ticker, e)
    return None


def _benchmark_return_since(benchmark: str, created_at: str) -> float | None:
    """벤치마크 지수의 created_at 이후 수익률(%). 실패 시 None."""
    if not benchmark:
        return None
    try:
        import yfinance as yf

        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=KST)
        days = max((datetime.now(KST) - created).days + 5, 10)
        hist = yf.Ticker(benchmark).history(period=f"{days}d")
        if hist.empty:
            return None
        close = hist["Close"]
        created_date = created.date()
        base = close[[d.date() >= created_date for d in close.index]]
        if len(base) < 2:
            return None
        return float((base.iloc[-1] / base.iloc[0] - 1) * 100)
    except Exception as e:
        log.debug("벤치마크 수익률 조회 실패 (%s): %s", benchmark, e)
        return None


def _days_since(iso_date: str) -> int:
    try:
        created = datetime.fromisoformat(iso_date)
        now = datetime.now(KST)
        return (now - created).days
    except Exception:
        return 0


def _is_korean_ticker(ticker: str) -> bool:
    """한국 종목 여부 판별."""
    return ticker.endswith((".KS", ".KQ"))


def _unrealistic_pnl_threshold(ticker: str) -> float:
    """비현실 수익률 임계값. 한국 100%, 미국 300%."""
    return 100.0 if _is_korean_ticker(ticker) else 300.0


def _update_accuracy_stats() -> None:
    """정확도 통계 업데이트 — 티커 정규화 + neutral 제외 + 비현실 수익률 필터."""
    conn = _get_conn()
    now = datetime.now(KST).isoformat()

    # 기존 accuracy_stats 테이블 재생성 (캐시 테이블)
    conn.execute("DROP TABLE IF EXISTS accuracy_stats")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accuracy_stats (
            ticker TEXT PRIMARY KEY,
            total_predictions INTEGER DEFAULT 0,
            evaluated_count INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            neutral_count INTEGER DEFAULT 0,
            invalid_count INTEGER DEFAULT 0,
            data_error_count INTEGER DEFAULT 0,
            expired_count INTEGER DEFAULT 0,
            avg_pnl REAL DEFAULT 0,
            avg_win REAL DEFAULT 0,
            avg_loss REAL DEFAULT 0,
            profit_factor REAL DEFAULT 0,
            expectancy REAL DEFAULT 0,
            win_rate REAL DEFAULT 0,
            last_updated TEXT
        )
    """)

    # 성과 집계 대상: 실행성 추천만. CANCEL_SELL/HOLD_REVIEW/WATCH_ONLY는
    # 사용자가 실제 실행하지 않은 건이라 승률·정확도에서 제외 (요청 3).
    rows = conn.execute(
        """SELECT ticker, outcome, pnl_pct
           FROM predictions
           WHERE status='closed'
             AND COALESCE(action_type,'') NOT IN ('CANCEL_SELL','HOLD_REVIEW','WATCH_ONLY')"""
    ).fetchall()

    # 정규화된 티커별로 집계
    from collections import defaultdict
    stats: dict[str, dict] = defaultdict(lambda: {
        "total": 0, "wins": 0, "losses": 0, "neutral": 0,
        "invalid": 0, "data_error": 0, "expired": 0,
        "win_pnls": [], "loss_pnls": [],
    })

    for row in rows:
        norm_ticker = normalize_ticker(row["ticker"])
        outcome = row["outcome"] or ""
        pnl = row["pnl_pct"] or 0.0
        s = stats[norm_ticker]
        s["total"] += 1

        # 비현실 수익률 필터
        threshold = _unrealistic_pnl_threshold(norm_ticker)
        is_realistic = abs(pnl) <= threshold

        if outcome == "win":
            s["wins"] += 1
            if is_realistic:
                s["win_pnls"].append(pnl)
            else:
                log.info("비현실 수익률 제외: %s pnl=%.1f%%", norm_ticker, pnl)
        elif outcome == "loss":
            s["losses"] += 1
            if is_realistic:
                s["loss_pnls"].append(pnl)
            else:
                log.info("비현실 수익률 제외: %s pnl=%.1f%%", norm_ticker, pnl)
        elif outcome == "invalid":
            s["invalid"] += 1
        elif outcome == "data_error":
            s["data_error"] += 1
        elif outcome == "expired":
            s["expired"] += 1
        else:  # neutral (목표/손절 미도달, 엣지 부족, 기간 내 미확정)
            s["neutral"] += 1

    for ticker, s in stats.items():
        evaluated = s["wins"] + s["losses"]
        win_rate = (s["wins"] / evaluated * 100) if evaluated > 0 else 0

        avg_win = (sum(s["win_pnls"]) / len(s["win_pnls"])) if s["win_pnls"] else 0
        avg_loss = (sum(s["loss_pnls"]) / len(s["loss_pnls"])) if s["loss_pnls"] else 0
        all_pnls = s["win_pnls"] + s["loss_pnls"]
        avg_pnl = (sum(all_pnls) / len(all_pnls)) if all_pnls else 0

        # profit_factor = gross_profit / abs(gross_loss)
        gross_profit = sum(p for p in s["win_pnls"])
        gross_loss = sum(abs(p) for p in s["loss_pnls"])
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0)

        # expectancy = (win_rate * avg_win) - (loss_rate * abs(avg_loss))
        if evaluated > 0:
            wr_frac = s["wins"] / evaluated
            lr_frac = s["losses"] / evaluated
            expectancy = (wr_frac * avg_win) - (lr_frac * abs(avg_loss))
        else:
            expectancy = 0

        conn.execute(
            """INSERT OR REPLACE INTO accuracy_stats
               (ticker, total_predictions, evaluated_count, wins, losses,
                neutral_count, invalid_count, data_error_count, expired_count,
                avg_pnl, avg_win, avg_loss,
                profit_factor, expectancy, win_rate, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, s["total"], evaluated, s["wins"], s["losses"],
             s["neutral"], s["invalid"], s["data_error"], s["expired"],
             round(avg_pnl, 2),
             round(avg_win, 2), round(avg_loss, 2),
             round(profit_factor, 2), round(expectancy, 2),
             round(win_rate, 1), now),
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
    """종목별 정확도 통계 — 정규화된 티커로 반환."""
    conn = _get_conn()
    # 컬럼 존재 여부 확인 (마이그레이션 전 호환)
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(accuracy_stats)").fetchall()}
    has_new_schema = "evaluated_count" in col_names
    rows = conn.execute("SELECT * FROM accuracy_stats").fetchall()
    result = {}
    col_names_set = {row[1] for row in conn.execute("PRAGMA table_info(accuracy_stats)").fetchall()}
    has_expectancy = "expectancy" in col_names_set

    # 원인 버킷 컬럼 존재 여부 (구 캐시 호환 — 없으면 0)
    has_data_error = "data_error_count" in col_names_set
    has_expired = "expired_count" in col_names_set

    for r in rows:
        norm = normalize_ticker(r["ticker"])
        if has_new_schema and has_expectancy:
            result[norm] = {
                "total": r["total_predictions"],
                "evaluated_count": r["evaluated_count"],
                "wins": r["wins"],
                "losses": r["losses"],
                "neutral_count": r["neutral_count"],
                "invalid_count": r["invalid_count"] if "invalid_count" in col_names_set else 0,
                "data_error_count": r["data_error_count"] if has_data_error else 0,
                "expired_count": r["expired_count"] if has_expired else 0,
                "avg_pnl": r["avg_pnl"],
                "avg_win": r["avg_win"] if "avg_win" in col_names_set else 0,
                "avg_loss": r["avg_loss"] if "avg_loss" in col_names_set else 0,
                "profit_factor": r["profit_factor"] if "profit_factor" in col_names_set else 0,
                "expectancy": r["expectancy"] if "expectancy" in col_names_set else 0,
                "win_rate": r["win_rate"],
            }
        elif has_new_schema:
            result[norm] = {
                "total": r["total_predictions"],
                "evaluated_count": r["evaluated_count"],
                "wins": r["wins"],
                "losses": r["losses"],
                "neutral_count": r["neutral_count"],
                "invalid_count": r["invalid_count"] if "invalid_count" in col_names_set else 0,
                "data_error_count": r["data_error_count"] if has_data_error else 0,
                "expired_count": r["expired_count"] if has_expired else 0,
                "avg_pnl": r["avg_pnl"],
                "avg_win": 0, "avg_loss": 0, "profit_factor": 0, "expectancy": 0,
                "win_rate": r["win_rate"],
            }
        else:
            # 구 스키마 호환
            result[norm] = {
                "total": r["total_predictions"],
                "evaluated_count": (r["correct"] or 0) + (r["wrong"] or 0),
                "wins": r["correct"],
                "losses": r["wrong"],
                "neutral_count": 0,
                "invalid_count": 0,
                "data_error_count": 0,
                "expired_count": 0,
                "avg_pnl": r["avg_pnl"],
                "avg_win": 0, "avg_loss": 0, "profit_factor": 0, "expectancy": 0,
                "win_rate": r["win_rate"],
            }
    return result


# 위험 종목 판정 최소 평가 표본 (이 미만은 표본부족 — 승률·위험 경고 산정 안 함)
RELIABILITY_MIN_EVAL = 5
# 데이터 품질 점검 임계 (무효/오류/중립 비율이 이 이상이면 데이터 품질 분류)
DATA_QUALITY_RATIO = 0.5

_CAUSE_LABELS: tuple[tuple[str, str], ...] = (
    ("neutral_count", "중립"),
    ("invalid_count", "무효"),
    ("data_error_count", "데이터오류"),
    ("expired_count", "만료"),
)


def _cause_breakdown_text(stats: dict) -> str:
    """원인별 count를 '중립 1·무효 6' 형태로 (0인 항목 생략)."""
    parts = [f"{label} {stats.get(key, 0)}"
             for key, label in _CAUSE_LABELS if stats.get(key, 0)]
    return "·".join(parts)


def classify_reliability(stats: dict) -> tuple[str, str, str]:
    """종목 통계를 신뢰도 카테고리로 분류 + 리포트 문구 생성.

    Returns: (category, headline, detail)
      category ∈ {"evaluated", "low_sample", "data_quality"}
      - evaluated:    평가 완료 (evaluated_count >= 5) — 승률 기준 판단. 위험 경고는 여기서만.
      - low_sample:   표본부족 (evaluated_count < 5) — 승률 미산정, 위험 경고 아님.
      - data_quality: 무효/데이터오류/중립 비율이 높음 — 데이터 품질 점검 필요.

    invalid/data_error/neutral/expired는 승률 분모(evaluated)에 들어가지 않는다.
    """
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    evaluated = stats.get("evaluated_count", wins + losses)
    total = stats.get("total", 0) or (
        evaluated + stats.get("neutral_count", 0) + stats.get("invalid_count", 0)
        + stats.get("data_error_count", 0) + stats.get("expired_count", 0))
    win_rate = stats.get("win_rate", 0) or 0
    breakdown = _cause_breakdown_text(stats)

    bad = (stats.get("invalid_count", 0) + stats.get("data_error_count", 0)
           + stats.get("neutral_count", 0))

    # 평가 완료: 승률 기준 판단. '위험 종목' 문구는 evaluated_count>=5 & win_rate<30%에서만.
    if evaluated >= RELIABILITY_MIN_EVAL:
        if win_rate < 30:
            headline = f"평가 {evaluated}건 승률 {win_rate:.0f}% — 위험 종목"
        else:
            headline = f"평가 {evaluated}건 승률 {win_rate:.0f}%"
        return "evaluated", headline, breakdown

    # 표본부족인데 무효/오류/중립 비율이 높으면 데이터 품질 점검으로 분리 (위험 종목 아님)
    if total >= 3 and bad >= total * DATA_QUALITY_RATIO and bad > evaluated:
        bad_cnt = stats.get("invalid_count", 0) + stats.get("data_error_count", 0)
        headline = f"무효/오류 {bad_cnt}건 — 가격/무효화 조건/벤치마크 데이터 점검 필요"
        return "data_quality", headline, breakdown

    # 표본부족: 승률 미산정 (위험 종목 아님)
    headline = f"평가 {evaluated}/{total}건, 승률 미산정"
    return "low_sample", headline, breakdown


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
          AND COALESCE(action_type,'') NOT IN ('CANCEL_SELL','HOLD_REVIEW','WATCH_ONLY')
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
          AND COALESCE(action_type,'') NOT IN ('CANCEL_SELL','HOLD_REVIEW','WATCH_ONLY')
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
        # 원인별로 분리 리포트 — invalid/neutral/data_error/expired가 '위험 종목'으로
        # 오해되지 않게 평가완료/표본부족/데이터품질 점검으로 나눠 표시한다.
        cat_icon = {"evaluated": "📊", "low_sample": "🔎", "data_quality": "🧪"}
        for ticker, stats in accuracy.items():
            category, headline, detail = classify_reliability(stats)
            if category == "evaluated":
                # 위험 종목 문구는 evaluated_count>=5 & win_rate<30%일 때만
                risk = " — 위험 종목" if (stats.get("win_rate", 0) or 0) < 30 else ""
                lines.append(
                    f"  📊 {ticker}: {stats['total']}건 중 {stats['wins']}적중 "
                    f"(승률 {stats['win_rate']:.0f}%, 평균 {stats['avg_pnl']:+.1f}%, "
                    f"평가 {stats.get('evaluated_count', 0)}건){risk}"
                    + (f" · 원인: {detail}" if detail else "")
                )
            else:
                # 표본부족·데이터품질은 위험 종목이 아님 — 원인별 count + '위험 경고 아님' 명시
                line = f"  {cat_icon[category]} {ticker}: {headline}"
                if detail:
                    line += f" · 원인: {detail}"
                line += " · 위험 경고 아님"
                lines.append(line)

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
                tag_suffix = ""
                if p.signal == "매도" and p.strategy_tags:
                    tag_suffix = f" ({p.strategy_tags})"
                lines.append(
                    f"  {icon} {p.name} {p.signal}{tag_suffix}: {p.pnl_pct:+.1f}% [{p.outcome}]"
                )

    # 신뢰도 기반 피드백 주입 — 평가 표본(evaluated_count) 기준 (total은 invalid/neutral 포함이라 부적합)
    # 게이트(평가≥5 차단)·보정(평가≥3)과 일관. 평가 0건짜리 "0% 승률"을 위험으로 낙인찍던 버그 수정.
    def _eval_cnt(s: dict) -> int:
        return s.get("evaluated_count", s["wins"] + s["losses"])

    if accuracy:
        high = [t for t, s in accuracy.items() if _eval_cnt(s) >= 5 and s["win_rate"] >= 70 and s["avg_pnl"] > 0]
        danger = [t for t, s in accuracy.items() if _eval_cnt(s) >= 5 and s["win_rate"] < 30]
        if high or danger:
            lines.append("\n  [⚡ 신뢰도 기반 판단 보정]")
        if danger:
            conn = _get_conn()
            for t in danger:
                s = accuracy[t]
                penalty = -30 if _eval_cnt(s) >= 8 else -15
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
                bonus = 10 if _eval_cnt(s) >= 8 else 5
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

    # 주간 리뷰 요약 (최근 7일 종료된 추천이 있으면 추가)
    weekly = generate_weekly_review()
    if weekly:
        lines.append("")
        lines.append(weekly)

    if len(lines) == 1:
        lines.append("  (기록 없음 — 첫 브리핑 후 축적됩니다)")

    return "\n".join(lines)


def confidence_calibration_text(min_bucket_n: int = 5) -> str:
    """확신도 구간별 실측 승률 캘리브레이션 테이블 (프롬프트 병기용).

    "확신도 70"이 실제로 70% 맞는지 검증 — 과신/과소 구간을 CIO에게 알려
    확신도 산출을 실측으로 교정한다. win/loss만 분모 (neutral/invalid 제외).
    구간 표본 < min_bucket_n 이면 미표시 (노이즈 방지).
    """
    try:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT confidence, outcome FROM predictions
               WHERE status='closed' AND outcome IN ('win','loss')
                 AND confidence IS NOT NULL"""
        ).fetchall()
    except Exception as e:
        log.debug("캘리브레이션 조회 실패: %s", e)
        return ""
    if not rows:
        return ""

    buckets = [(0, 40, "≤40"), (41, 55, "41-55"), (56, 70, "56-70"),
               (71, 85, "71-85"), (86, 100, "86+")]
    lines = []
    for lo, hi, label in buckets:
        sample = [r for r in rows if lo <= (r["confidence"] or 0) <= hi]
        n = len(sample)
        if n < min_bucket_n:
            continue
        wins = sum(1 for r in sample if r["outcome"] == "win")
        actual = wins / n * 100
        mid = (lo + hi) / 2 if lo else hi
        gap = actual - mid
        if gap <= -15:
            verdict = f"과신 — 이 구간 확신도는 실측보다 {abs(gap):.0f}%p 높음, 하향하라"
        elif gap >= 15:
            verdict = f"과소 — 실측이 {gap:.0f}%p 더 좋음, 조건 충족 시 상향 가능"
        else:
            verdict = "실측 부합"
        lines.append(f"  확신도 {label}: {n}건 실측 승률 {actual:.0f}% → {verdict}")
    return "\n".join(lines)


def reliability_directives_text() -> str:
    """CIO system 프롬프트용 종목별 실측 신뢰도 보정 테이블 (간결판).

    memory_to_text()는 user 프롬프트 깊숙이 들어가 CIO가 놓치기 쉬움 —
    system 프롬프트의 보정 규칙(-15~-30/+5~+10) 바로 아래에 실측 데이터를 박는다.
    - 표시: evaluated_count >= 3 종목만
    - 판정: evaluated_count >= 5 에서만 위험(승률<30%)/고신뢰(승률>=70% & 평균수익+) — 게이트 기준과 동일
    """
    accuracy = get_accuracy_summary()
    if not accuracy:
        return ""

    def _eval_cnt(s: dict) -> int:
        return s.get("evaluated_count", s.get("wins", 0) + s.get("losses", 0))

    streaks = losing_streak_tickers(min_streak=3)
    lines = []
    for ticker, s in sorted(accuracy.items(), key=lambda kv: -_eval_cnt(kv[1])):
        n = _eval_cnt(s)
        if n < 3:
            continue
        wr = s.get("win_rate", 0) or 0
        avg = s.get("avg_pnl", 0) or 0
        if ticker in streaks:
            tag = (f"⛔ {streaks[ticker]}연패 — 신규 매수 금지 (자동 회피). "
                   "승리 기록이 나오기 전까지 이 종목은 관망만 허용")
        elif n >= 5 and wr < 30:
            adj = -30 if n >= 8 else -15
            tag = f"🔴 확신도 {adj:+d}% 감점 필수"
        elif n >= 5 and wr >= 70 and avg > 0:
            adj = 10 if n >= 8 else 5
            tag = f"🟢 확신도 +{adj}% 가중 허용"
        else:
            tag = "⚪ 보정 없음 (참고만 — 표본부족 종목을 위험 근거로 쓰지 마라)"
        lines.append(f"  {ticker}: 평가 {n}건 · 승률 {wr:.0f}% · 평균 {avg:+.1f}% → {tag}")
    return "\n".join(lines)


def generate_open_positions_review(current_prices: dict[str, float]) -> str:
    """미결 추천 상세 점검 — 실제 보유 종목만. "이 포지션을 유지할 근거가 있는가?" 강제 점검.

    Returns:
        프롬프트 삽입용 텍스트. 미결 추천이 없으면 빈 문자열.
    """
    # 실제 보유 종목만 필터 (워치리스트 추천 제외)
    from config.settings import (
        HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA,
        HOLDINGS_IRP, HOLDINGS_PENSION,
    )
    held_tickers: set[str] = set()
    for holdings in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
        held_tickers.update(holdings.keys())

    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM predictions WHERE status = 'open' ORDER BY created_at DESC"
    ).fetchall()

    # 보유 종목만 필터
    rows = [r for r in rows if r["ticker"] in held_tickers]

    if not rows:
        return ""

    lines = ["━━━ 📋 미결 포지션 점검 (반드시 각 포지션에 대해 유지/매도 판단 필수) ━━━"]
    lines.append("아래는 이전 브리핑에서 추천한 미결 포지션입니다. **각 포지션마다 유지/매도 판단을 명시**하세요.\n")

    for row in rows:
        ticker = row["ticker"]
        name = row["name"]
        signal = row["signal"]
        entry = row["entry_price"]
        target = row["target_price"] or 0
        stop = row["stop_loss"] or 0
        reasoning = (row["reasoning"] or "")[:150]
        created = row["created_at"][:10]
        try:
            invalidation = row["invalidation_condition"] or ""
        except (IndexError, KeyError):
            invalidation = ""

        cur = current_prices.get(ticker, 0)
        if cur and entry:
            pnl = (cur - entry) / entry * 100
            pnl_str = f"현재가 기준 {pnl:+.1f}%"
        else:
            pnl = 0
            pnl_str = "현재가 미수집"

        # 경고 레벨
        if pnl <= -10:
            alert = "🚨 손절 검토 필요"
        elif pnl <= -5:
            alert = "⚠️ 주의 구간"
        elif target and cur >= target:
            alert = "🎯 목표가 도달 — 익절 검토"
        elif stop and cur <= stop:
            alert = "🚨 손절가 이탈 — 즉시 매도 검토"
        else:
            alert = "✅ 정상"

        lines.append(f"▸ {name} ({ticker}) — {signal} [{created}]")
        lines.append(f"  진입: {entry:,.0f} | 목표: {target:,.0f} | 손절: {stop:,.0f} | {pnl_str} | {alert}")
        if reasoning:
            lines.append(f"  매수 근거: {reasoning}")
        if invalidation:
            lines.append(f"  무효화 조건: {invalidation}")
        lines.append(f"  → **이 포지션 유지? 매도? 판단 필수.**\n")

    lines.append("위 모든 미결 포지션에 대해 strategy_sell 또는 분석에 유지/매도 판단을 반드시 포함하세요.")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
# 긴급 알림 이력 — 모니터가 전송/억제한 알림을 대시보드에서 조회
# ═══════════════════════════════════════════════════════
def save_alert(alert: dict) -> int:
    """모니터 알림 기록 (전송/억제 모두). Returns row id, 실패 시 0.

    alert keys: ticker, name, alert_type, severity, title, message,
                price, delivered(bool), suppress_reason
    """
    try:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO alert_history
               (created_at, ticker, name, alert_type, severity, title,
                message, price, delivered, suppress_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(KST).isoformat(),
                str(alert.get("ticker", "")),
                str(alert.get("name", "")),
                str(alert.get("alert_type", "")),
                str(alert.get("severity", "")),
                str(alert.get("title", ""))[:200],
                str(alert.get("message", ""))[:2000],
                float(alert.get("price", 0) or 0),
                1 if alert.get("delivered") else 0,
                str(alert.get("suppress_reason", ""))[:200],
            ),
        )
        conn.commit()
        return cur.lastrowid or 0
    except sqlite3.Error as e:
        log.warning("알림 이력 저장 실패: %s", e)
        return 0


def recent_alerts(hours: int = 48, limit: int = 100) -> list[dict]:
    """최근 N시간 알림 이력 (전송+억제) — 대시보드 read-only API용."""
    try:
        conn = _get_conn()
        cutoff = (datetime.now(KST) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            """SELECT * FROM alert_history WHERE created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (cutoff, max(1, min(int(limit), 500))),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        log.warning("알림 이력 조회 실패: %s", e)
        return []


def generate_weekly_review() -> str:
    """지난 7일간 마감된 추천 분석 — 왜 맞았고 왜 틀렸는지."""
    conn = _get_conn()
    cutoff = (datetime.now(KST) - timedelta(days=7)).isoformat()
    rows = conn.execute(
        """SELECT * FROM predictions
           WHERE status = 'closed' AND closed_at > ?
           ORDER BY closed_at DESC""",
        (cutoff,),
    ).fetchall()

    if not rows:
        return ""

    wins = [r for r in rows if r["outcome"] == "win"]
    losses = [r for r in rows if r["outcome"] == "loss"]
    neutrals = [r for r in rows if r["outcome"] == "neutral"]

    total = len(rows)
    win_rate = (len(wins) / total * 100) if total > 0 else 0
    pnl_values = [r["pnl_pct"] or 0 for r in rows]
    avg_pnl = sum(pnl_values) / len(pnl_values) if pnl_values else 0
    best = max(pnl_values) if pnl_values else 0
    worst = min(pnl_values) if pnl_values else 0

    lines = ["  [📅 주간 리뷰 (최근 7일)]"]
    lines.append(
        f"  총 {total}건: ✅{len(wins)}승 ❌{len(losses)}패 ➖{len(neutrals)}중립 "
        f"| 승률 {win_rate:.0f}% | 평균 {avg_pnl:+.1f}%"
    )
    lines.append(f"  최고 {best:+.1f}% | 최저 {worst:+.1f}%")

    # 승리 요인 분석
    if wins:
        lines.append("\n  [✅ 성공 요인]")
        for w in wins[:3]:
            tags = w["strategy_tags"] or ""
            name = w["name"] or w["ticker"]
            lines.append(
                f"  {name} {w['signal']} {w['pnl_pct']:+.1f}%"
                f" — 태그: {tags or '없음'}"
            )

    # 실패 요인 분석
    if losses:
        lines.append("\n  [❌ 실패 원인]")
        for l_row in losses[:3]:
            tags = l_row["strategy_tags"] or ""
            name = l_row["name"] or l_row["ticker"]
            reasoning = (l_row["reasoning"] or "")[:80]
            invalidation = ""
            try:
                invalidation = l_row["invalidation_condition"] or ""
            except (KeyError, IndexError):
                pass
            reason_label = "무효화조건 미달" if invalidation else "분석 오류"
            lines.append(
                f"  {name} {l_row['signal']} {l_row['pnl_pct']:+.1f}%"
                f" — {reason_label} | 태그: {tags or '없음'}"
            )
            if reasoning:
                lines.append(f"    근거: {reasoning}")

    return "\n".join(lines)
