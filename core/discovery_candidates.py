"""core/discovery_candidates.py

신규 종목 발굴을 보유/관심종목 관리와 코드 레벨에서 완전히 분리한다.

배경: 브리핑/추천이 계속 보유종목·WATCHLIST·RIA·과거 추천 주변만 반복하는 문제.
사용자 목적은 '아는 종목 관리'가 아니라 '모르는 신규 후보 발견 + 성공률 개선'.

분리 원칙 (프롬프트가 아니라 구조로 강제):
  1. 보유종목 관리   — 보유 종목의 리스크/익절/손절/홀딩만
  2. 기존 관심 재평가 — WATCHLIST / RIA_ALLOWED_TICKERS / 최근 추천만
  3. 신규 발굴       — 보유도 WATCHLIST도 RIA도 아닌 새 종목만

신규 발굴 섹션은 매 브리핑 필수. 통과 0개여도 탈락 상위 5개 + 사유를 출력한다.
read-only — 주문/POST/PUT/DELETE/실매매 경로 없음.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ── 발굴 게이트 상수 ───────────────────────────────────────────────
_CHASE_CHANGE_PCT = 8.0          # 이 이상은 즉시실행 금지 플래그(후보 제거 아님)
_HARD_CHASE_CHANGE_PCT = 30.0      # 이 이상만 발굴 후보에서 제외
_MIN_RISK_REWARD = 1.5           # 목표:손절 권장 1.5:1
_HARD_MIN_RISK_REWARD = 1.0      # 이 미만만 하드 탈락, 1.0~1.5는 관찰 후보
_STOP_PCT = 0.06                 # 손절 폭 (가격 대비)
_TARGET_BASE = 0.05              # 목표 폭 기본값
_MIN_KR_VALUE_KRW = 30_000_000_000   # 한국 거래대금 300억+ (유동성)
_MIN_US_MCAP = 2_000_000_000         # 미국 시총 $2B+
_TOP_NEW = 8                     # 신규 발굴 TOP N — 반복 후보 과소노출 방지
_TOP_REJECTED = 12               # 탈락 표시 상위 N
_ROTATION_MINUTES = 5              # 장중 후보 노출 순환 단위


@dataclass(frozen=True)
class NewCandidate:
    """신규 발굴 후보 (보유/관심 어디에도 없는 종목)."""

    ticker: str
    name: str
    market: str          # "KR" | "US"
    price: float
    score: float
    idea: str            # 투자 아이디어 한 줄 (계좌보다 먼저)
    reasons: tuple[str, ...]
    target_price: float
    stop_loss: float
    risk_reward: float
    change_pct: float = 0.0
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    intraday_drawdown_pct: float = 0.0  # 현재가가 장중 고점 대비 얼마나 밀렸는지(음수)
    intraday_range_pct: float = 0.0
    risk_flags: tuple[str, ...] = ()
    suggested_accounts: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    # Fast-universe quality inputs are separate from the display/ranking score.
    # Only observed quote/chart fields belong here; synthetic neutral defaults
    # must never become BUY authority.
    quality_inputs: dict = field(default_factory=dict)
    quality_input_provenance: dict = field(default_factory=dict)
    quality_data_starved: bool = False
    quality_data_starvation_reason: str = ""


@dataclass(frozen=True)
class RejectedCandidate:
    """신규 발굴 탈락 종목 + 사유."""

    ticker: str
    name: str
    reason: str


@dataclass(frozen=True)
class DiscoverySections:
    """브리핑 3섹션 분리 결과."""

    holdings_management: tuple[dict, ...] = ()
    watchlist_reeval: tuple[dict, ...] = ()
    new_discovery: tuple[NewCandidate, ...] = ()
    new_rejected: tuple[RejectedCandidate, ...] = ()
    market: str = ""
    scan_summary: dict = field(default_factory=dict)


# ── 컨텍스트 helpers ───────────────────────────────────────────────

def _known_sets() -> tuple[set[str], set[str], set[str], set[str]]:
    """settings 기반 (held, watchlist, ria, recent_reco) 집합 로드."""
    held: set[str] = set()
    watchlist: set[str] = set()
    ria: set[str] = set()
    recent: set[str] = set()
    try:
        from config.settings import (
            HOLDINGS_GENERAL, HOLDINGS_IRP, HOLDINGS_ISA,
            HOLDINGS_PENSION, HOLDINGS_RIA, WATCHLIST, RIA_ALLOWED_TICKERS,
        )
        for h in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA,
                  HOLDINGS_IRP, HOLDINGS_PENSION):
            held.update(h.keys())
        watchlist = set(WATCHLIST.keys())
        ria = set(RIA_ALLOWED_TICKERS.keys())
    except Exception as e:
        log.warning("known sets 로드 실패: %s", e)
    try:
        recent = recent_recommended_tickers()
    except Exception as e:
        log.debug("recent reco 로드 실패: %s", e)
    return held, watchlist, ria, recent


def recent_recommended_tickers(days: int = 30, limit: int = 80) -> set[str]:
    """최근 N일 추천된 티커 집합 (novelty 판정용)."""
    from datetime import datetime, timedelta, timezone
    try:
        from core.memory import get_recent_predictions
        kst = timezone(timedelta(hours=9))
        cutoff = (datetime.now(kst) - timedelta(days=days)).strftime("%Y-%m-%d")
        out: set[str] = set()
        for p in get_recent_predictions(limit=limit):
            created = str(getattr(p, "created_at", "") or "")[:10]
            if created >= cutoff and getattr(p, "ticker", ""):
                out.add(p.ticker)
        return out
    except Exception as e:
        log.debug("recent_recommended_tickers 실패: %s", e)
        return set()


def _name_for(ticker: str, fallback: str = "") -> str:
    try:
        from config.settings import (
            PORTFOLIO, WATCHLIST, RIA_ALLOWED_TICKERS,
            SCAN_UNIVERSE_KR, SCAN_UNIVERSE_US,
        )
        for m in (PORTFOLIO, WATCHLIST, RIA_ALLOWED_TICKERS,
                  SCAN_UNIVERSE_KR, SCAN_UNIVERSE_US):
            if ticker in m:
                return m[ticker]
    except Exception:
        pass
    return fallback or ticker


def _market_of(ticker: str, given: str = "") -> str:
    if given:
        return given
    return "KR" if ticker.endswith((".KS", ".KQ")) else "US"


# ── 손익비 / 점수 ──────────────────────────────────────────────────

def _risk_reward(c: dict) -> tuple[float, float, float]:
    """(target_price, stop_loss, risk_reward) 계산 — 결정론적 모델."""
    price = float(c.get("price") or 0)
    if price <= 0:
        return 0.0, 0.0, 0.0
    from_high = abs(float(c.get("pct_from_52w_high") or 0))
    ret_20 = max(float(c.get("ret_20d") or 0), 0.0)
    target_pct = min(0.30, from_high / 100 * 0.5 + ret_20 / 200 + _TARGET_BASE)
    # US fallback quotes currently do not provide chart/52w-high metrics. Treat that
    # as missing data, not as a genuine <1.0 RR failure; otherwise every US name is
    # hard-rejected before the US session pipeline can even evaluate it.
    if (
        _market_of(c.get("ticker", ""), c.get("market", "")) == "US"
        and from_high == 0
        and ret_20 == 0
        and str(c.get("source") or "").startswith("유니버스(fallback)")
    ):
        target_pct = max(target_pct, 0.10)
    stop = round(price * (1 - _STOP_PCT), 2)
    target = round(price * (1 + target_pct), 2)
    rr = round(target_pct / _STOP_PCT, 2) if _STOP_PCT else 0.0
    return target, stop, rr



def _intraday_metrics(c: dict) -> tuple[float, float, list[str]]:
    """장중 급등/급락/수급 약화를 실행 리스크 플래그로 계산한다.

    이 플래그는 후보를 숨기는 용도가 아니다. 발굴 후보로는 남기고,
    Toss/실행 채널에서 HOLD/BLOCK 판단에 사용한다.
    """
    price = float(c.get("price") or 0)
    high = float(c.get("high_price") or 0)
    low = float(c.get("low_price") or 0)
    change = float(c.get("change_pct") or 0)
    vol_surge = float(c.get("vol_surge") or 0)
    flags: list[str] = []
    drawdown = ((price / high - 1) * 100) if price > 0 and high > 0 else 0.0
    range_pct = ((high / low - 1) * 100) if high > 0 and low > 0 else 0.0
    if change >= _CHASE_CHANGE_PCT:
        flags.append(f"당일 급등 추격 주의 +{change:.1f}%")
    if drawdown <= -6.0:
        flags.append(f"장중 고점 대비 급락 {drawdown:.1f}%")
    if range_pct >= 10.0:
        flags.append(f"장중 변동성 과대 {range_pct:.1f}%")
    if 0 < vol_surge < 1.0 and not c.get("has_catalyst"):
        flags.append("수급 약함 — 즉시 실행보다 관찰")
    return round(drawdown, 1), round(range_pct, 1), flags

def _soft_observation_flags(flags: tuple[str, ...] | list[str]) -> list[str]:
    """즉시 HOLD가 아니라 소액 조건부/관찰매수로 남길 완화 리스크."""
    return [f for f in flags if str(f).startswith(("수급 약함", "손익비 보통"))]


def _blocking_risk_flags(flags: tuple[str, ...] | list[str]) -> list[str]:
    """실행을 차단해야 하는 장중 반전/과열 리스크만 분리한다."""
    soft = set(_soft_observation_flags(flags))
    return [f for f in flags if f not in soft]


def _intraday_rotation_bucket(now: datetime | None = None) -> int:
    """KST 기준 5분 단위 후보 순환 버킷. 상태 저장 없이 같은 후보 고정 노출을 완화한다."""
    kst = timezone(timedelta(hours=9))
    now = now.astimezone(kst) if now else datetime.now(kst)
    return (now.hour * 60 + now.minute) // _ROTATION_MINUTES


def _rotate_list(items: list, bucket: int | None = None) -> list:
    """리스트를 5분 버킷 기준으로 순환. 길이 0/1이면 그대로 반환."""
    if len(items) <= 1:
        return list(items)
    b = _intraday_rotation_bucket() if bucket is None else bucket
    n = b % len(items)
    return list(items[n:] + items[:n])


def _candidate_status_priority(item: dict) -> int:
    status = item.get("execution_status") or item.get("action_bias") or ""
    return {
        "executable": 0,
        "conditional_small_entry": 1,
        "CONDITIONAL_SMALL_ENTRY": 1,
        "ACCOUNT_REVIEW": 1,
        "limit_exceeded": 2,
        "WATCH": 3,
        "hold_risk_flags": 4,
        "HOLD_RISK_REVIEW": 4,
    }.get(str(status), 9)


def _rotate_candidate_groups(items: list[dict]) -> list[dict]:
    """상태 우선순위는 지키되 같은 상태 후보는 장중 순환 노출한다."""
    bucket = _intraday_rotation_bucket()
    out: list[dict] = []
    for priority in sorted({_candidate_status_priority(i) for i in items}):
        group = [i for i in items if _candidate_status_priority(i) == priority]
        group = _rotate_list(group, bucket)
        for rank, item in enumerate(group, 1):
            item["rotation_bucket"] = bucket
            item["rotation_rank"] = rank
        out.extend(group)
    return out


def _suggested_accounts(c: dict, price: float) -> tuple[str, ...]:
    """시장 발굴 후보를 계좌 제약 없이 먼저 찾은 뒤, 후단에서 계좌 후보를 붙인다."""
    market = _market_of(c.get("ticker", ""), c.get("market", ""))
    ticker = c.get("ticker", "")
    name = str(c.get("name", "")).upper()
    accounts: list[str] = []
    if market == "US":
        accounts.append("삼성 일반")
        if price <= 500_000:
            accounts.append("토스 AI")
    else:
        if ticker.endswith((".KS", ".KQ")):
            accounts.extend(["삼성 수동", "ISA"])
        if any(x in name for x in ("KODEX", "TIGER", "PLUS", "ACE")):
            accounts.append("RIA/연금 검토")
        if price <= 500_000:
            accounts.append("토스 AI")
    return tuple(dict.fromkeys(accounts))

def _gate(c: dict) -> str:
    """하드 탈락 사유 반환 (통과 시 빈 문자열).

    보수성 완화: 당일 급등/수급 약함은 후보 제거가 아니라 risk_flags로 이동한다.
    실제 실행은 Toss/계좌별 후단 게이트에서 차단한다.
    """
    price = float(c.get("price") or 0)
    if price <= 0:
        return "데이터 부족 (가격/시세 없음)"

    market = _market_of(c.get("ticker", ""), c.get("market", ""))
    value = float(c.get("volume_value") or 0)
    min_value = _MIN_KR_VALUE_KRW if market == "KR" else _MIN_US_MCAP
    fast_quality = c.get("quality_inputs") if type(c.get("quality_inputs")) is dict else None
    # Fast fallback may not expose volume/market-cap. Keep it visible for an
    # explicit downstream quality-data block; never forge the minimum value.
    if value < min_value and not (
        fast_quality is not None and "volume_value" not in fast_quality
    ):
        return "거래대금/유동성 부족"

    change = float(c.get("change_pct") or 0)
    if change >= _HARD_CHASE_CHANGE_PCT:
        return f"비정상 급등 과열 (+{change:.1f}%)"

    _, _, rr = _risk_reward(c)
    if rr < _HARD_MIN_RISK_REWARD:
        return f"손익비 부족 ({rr:.1f}:1 < 1.0:1)"

    return ""

def _score(c: dict, novel: bool, duplicate: bool) -> tuple[float, list[str]]:
    """통과 후보 점수 + 근거."""
    reasons: list[str] = []
    score = 0.0
    market = _market_of(c.get("ticker", ""), c.get("market", ""))

    value = float(c.get("volume_value") or 0)
    base = _MIN_KR_VALUE_KRW if market == "KR" else _MIN_US_MCAP
    liq = min(25.0, value / base * 12.5) if base else 0.0
    score += liq
    if liq >= 18:
        reasons.append("유동성 풍부")

    vol_surge = float(c.get("vol_surge") or 0)
    if vol_surge >= 2.0:
        score += 20; reasons.append(f"거래량 급증 x{vol_surge:.1f}")
    elif vol_surge >= 1.5:
        score += 12; reasons.append("수급 전환 조짐")
    elif vol_surge >= 1.0:
        score += 5

    rsi = float(c.get("rsi") or 0)
    from_high = float(c.get("pct_from_52w_high") or 0)
    if 45 <= rsi <= 70 and from_high >= -5:
        score += 15; reasons.append("돌파/신고가권 추세")
    elif 35 <= rsi < 45:
        score += 10; reasons.append("눌림목 반전 구간")

    if c.get("has_catalyst"):
        score += 15; reasons.append("뉴스/실적 촉매")

    _, _, rr = _risk_reward(c)
    score += min(20.0, max(0.0, (rr - _MIN_RISK_REWARD) * 15))
    reasons.append(f"손익비 {rr:.1f}:1")

    if novel:
        score += 15; reasons.append("신규(보유·관심·최근추천 이력 없음)")
    if duplicate:
        score -= 20; reasons.append("최근 추천 중복 감점")

    return round(score, 1), reasons


def _idea(c: dict, reasons: list[str]) -> str:
    """계좌보다 먼저 나오는 투자 아이디어 한 줄."""
    name = c.get("name") or c.get("ticker", "")
    ret_60 = float(c.get("ret_60d") or 0)
    tags = c.get("tags") or ()
    bits = []
    if tags:
        bits.append("/".join(tags))
    if ret_60:
        bits.append(f"60일 {ret_60:+.0f}%")
    head = ", ".join(bits) if bits else (reasons[0] if reasons else "신규 모멘텀")
    return f"{name} — {head}"


# ── 신규 발굴 핵심 ─────────────────────────────────────────────────

def build_new_discovery(
    candidates: list[dict],
    held: set[str] | None = None,
    watchlist: set[str] | None = None,
    ria: set[str] | None = None,
    recent_reco: set[str] | None = None,
) -> tuple[tuple[NewCandidate, ...], tuple[RejectedCandidate, ...]]:
    """신규 발굴 후보 평가.

    보유/WATCHLIST/RIA 종목은 신규 발굴 대상에서 완전 제외 (탈락도 아님 — 다른 섹션 소관).
    나머지는 게이트 통과 시 점수화, 탈락 시 사유 기록.

    Returns:
        (passed, rejected) — 둘 다 점수/근접도 순 정렬.
    """
    held = held or set()
    watchlist = watchlist or set()
    ria = ria or set()
    recent_reco = recent_reco or set()
    excluded_known = held | watchlist | ria

    passed: list[NewCandidate] = []
    rejected: list[RejectedCandidate] = []

    for c in candidates:
        ticker = c.get("ticker", "")
        if not ticker or ticker in excluded_known:
            continue  # 신규가 아님 → 신규 발굴에서 배제

        name = c.get("name") or _name_for(ticker)
        reason = _gate(c)
        if reason:
            rejected.append(RejectedCandidate(ticker=ticker, name=name, reason=reason))
            continue

        novel = ticker not in recent_reco
        duplicate = ticker in recent_reco
        score, reasons = _score(c, novel=novel, duplicate=duplicate)
        target, stop, rr = _risk_reward(c)
        price = float(c.get("price") or 0)
        intraday_dd, intraday_range, risk_flags = _intraday_metrics(c)
        if rr < _MIN_RISK_REWARD:
            risk_flags.append(f"손익비 보통 {rr:.1f}:1 — 즉시 실행보다 관찰")
        passed.append(NewCandidate(
            ticker=ticker, name=name,
            market=_market_of(ticker, c.get("market", "")),
            price=price,
            score=score, idea=_idea(c, reasons), reasons=tuple(reasons),
            target_price=target, stop_loss=stop, risk_reward=rr,
            change_pct=float(c.get("change_pct") or 0),
            open_price=float(c.get("open_price") or 0),
            high_price=float(c.get("high_price") or 0),
            low_price=float(c.get("low_price") or 0),
            intraday_drawdown_pct=intraday_dd,
            intraday_range_pct=intraday_range,
            risk_flags=tuple(risk_flags),
            suggested_accounts=_suggested_accounts(c, price),
            tags=tuple(c.get("tags") or ()),
            quality_inputs=dict(c.get("quality_inputs") or {}),
            quality_input_provenance=dict(c.get("quality_input_provenance") or {}),
            quality_data_starved=c.get("quality_data_starved") is True,
            quality_data_starvation_reason=str(
                c.get("quality_data_starvation_reason") or ""
            ),
        ))

    passed.sort(key=lambda x: x.score, reverse=True)
    rejected.sort(key=lambda x: x.ticker)
    return tuple(passed), tuple(rejected)


# ── 런타임 의존성 없는(requests-only) 모멘텀 계산 ─────────────────
# pandas/pykrx/yfinance 미설치 환경에서도 동작하는 순수 파이썬 지표.

def _ret_pct(closes: list[float], n: int) -> float:
    """n일 전 대비 수익률(%). 데이터 부족 시 0."""
    if len(closes) <= n or closes[-1 - n] <= 0:
        return 0.0
    return round((closes[-1] / closes[-1 - n] - 1) * 100, 2)


def _pct_from_high(closes: list[float]) -> float:
    """기간 내 최고가 대비 현재가 위치(%, 음수=하단)."""
    if not closes:
        return 0.0
    hi = max(closes)
    if hi <= 0:
        return 0.0
    return round((closes[-1] / hi - 1) * 100, 2)


def _rsi_from_closes(closes: list[float], period: int = 14) -> float:
    """순수 파이썬 RSI(14). 데이터 부족 시 중립값 50."""
    if len(closes) <= period:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return round(100 - 100 / (1 + rs), 1)


def _avg_turnover(closes: list[float], vols: list[int], lookback: int = 20) -> float:
    """최근 lookback일 평균 거래대금(종가×거래량)."""
    n = min(len(closes), len(vols), lookback)
    if n == 0:
        return 0.0
    total = sum(closes[-i] * vols[-i] for i in range(1, n + 1))
    return total / n


def _vol_surge(vols: list[int], lookback: int = 20) -> float:
    """최근 거래량 / 직전 평균 거래량 비율. 데이터 부족 시 1.0."""
    if len(vols) < 5:
        return 1.0
    recent = vols[-1]
    base = [v for v in vols[-(lookback + 1):-1] if v > 0]
    if not base:
        return 1.0
    avg = sum(base) / len(base)
    if avg <= 0:
        return 1.0
    return round(recent / avg, 2)


_FAST_QUALITY_MAX_AGE_SEC = 300.0
_FAST_QUALITY_FUTURE_SKEW_SEC = 30.0
_FAST_QUALITY_FIELDS = (
    "price", "change_pct", "high_price", "low_price", "volume_value",
    "ret_20d", "ret_60d", "rsi", "vol_surge", "pct_from_52w_high",
)


def _strict_quality_number(value) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _attach_fast_quality_inputs(candidate: dict) -> dict:
    """Bind observed fast-quote fields to source/time and classify starvation.

    The fallback must not manufacture neutral RSI/volume/market-cap values.  A
    field can affect quality only when its actual numeric observation and
    value-level source/fetch timestamp are both present.
    """
    out = dict(candidate)
    raw_source = out.get("quote_source") or out.get("source")
    source = raw_source.strip().lower() if type(raw_source) is str else ""
    as_of = _strict_quality_number(out.get("quote_as_of"))
    now_ts = datetime.now(timezone.utc).timestamp()
    fresh = bool(
        source
        and as_of is not None
        and as_of > 0
        and -_FAST_QUALITY_FUTURE_SKEW_SEC <= now_ts - as_of <= _FAST_QUALITY_MAX_AGE_SEC
    )

    inputs: dict[str, float] = {}
    provenance: dict[str, dict] = {}
    for field_name in _FAST_QUALITY_FIELDS:
        if field_name not in out:
            continue
        value = _strict_quality_number(out.get(field_name))
        if value is None:
            continue
        inputs[field_name] = value
        provenance[field_name] = {
            "source": source or "missing",
            "as_of": as_of if as_of is not None and as_of > 0 else None,
            "fresh": fresh,
        }

    price = inputs.get("price")
    high = inputs.get("high_price")
    low = inputs.get("low_price")
    groups: list[str] = []
    if fresh and "change_pct" in inputs:
        groups.append("quote_change")
    if (
        fresh
        and price is not None and price > 0
        and high is not None and low is not None
        and high >= price >= low > 0 and high > low
    ):
        groups.append("intraday_range")
    if fresh and inputs.get("volume_value", 0.0) > 0:
        groups.append("liquidity")
    if fresh and any(
        name in inputs
        for name in ("ret_20d", "ret_60d", "rsi", "vol_surge", "pct_from_52w_high")
    ):
        groups.append("indicator")

    missing_groups = [
        group for group in ("quote_change", "intraday_range", "liquidity", "indicator")
        if group not in groups
    ]
    if not source or as_of is None or as_of <= 0:
        starvation_reason = "quality_provenance_missing"
    elif not fresh:
        starvation_reason = "quality_provenance_stale"
    elif len(groups) < 2:
        starvation_reason = "quality_evidence_insufficient"
    else:
        starvation_reason = ""

    out["quality_inputs"] = inputs
    out["quality_input_provenance"] = provenance
    out["quality_evidence_groups"] = groups
    out["quality_missing_groups"] = missing_groups
    out["quality_data_starved"] = bool(starvation_reason)
    out["quality_data_starvation_reason"] = starvation_reason
    return out


def _light_quote_kr(ticker: str) -> dict | None:
    """KIS 일봉(requests-only)으로 KR 종목 시세+모멘텀. pandas 불필요."""
    from core.market_kis import get_domestic_chart, get_domestic_price

    chart = get_domestic_chart(ticker, period="3mo", interval="1d")
    closes: list[float] = []
    vols: list[int] = []
    if chart and chart.get("points"):
        for p in chart["points"]:
            c = float(p.get("close") or 0)
            if c > 0:
                closes.append(c)
                vols.append(int(p.get("volume") or 0))

    quote_source = ""
    quote_as_of = 0.0
    high_price = 0.0
    low_price = 0.0
    if closes:
        price = closes[-1]
        change_pct = float(chart.get("day_pct") or 0.0)
        quote_source = str(chart.get("source") or "").lower()
        quote_as_of = datetime.now(timezone.utc).timestamp()
        last_point = chart["points"][-1]
        high_price = float(last_point.get("high") or 0.0)
        low_price = float(last_point.get("low") or 0.0)
    else:
        q = get_domestic_price(ticker)
        if q is None:
            return None
        price = float(q.price)
        change_pct = float(q.pct)
        high_price = float(q.high)
        low_price = float(q.low)
        quote_source = str(q.source or "").lower()
        quote_as_of = float(q.as_of or 0.0)
    if price <= 0:
        return None

    out = {
        "ticker": ticker, "name": _name_for(ticker), "market": "KR",
        "price": price, "change_pct": change_pct,
        "high_price": high_price, "low_price": low_price,
        "source": "유니버스(fallback)", "quote_source": quote_source,
        "quote_as_of": quote_as_of, "tags": ("유니버스",),
        "has_catalyst": False,
    }
    turnover = _avg_turnover(closes, vols)
    if turnover > 0:
        out["volume_value"] = turnover
    if len(closes) > 20:
        out["ret_20d"] = _ret_pct(closes, 20)
    if len(closes) > 60:
        out["ret_60d"] = _ret_pct(closes, 60)
    if len(closes) > 14:
        out["rsi"] = _rsi_from_closes(closes)
    if len(vols) >= 5:
        out["vol_surge"] = _vol_surge(vols)
    if closes:
        out["pct_from_52w_high"] = _pct_from_high(closes)
    return out


def _light_quote_us(ticker: str) -> dict | None:
    """KIS 해외 현재가로 observed quote fields만 반환한다."""
    from core.market_kis import get_overseas_price

    q = get_overseas_price(ticker)
    if q is None:
        return None
    price = float(q.price)
    if price <= 0:
        return None
    out = {
        "ticker": ticker, "name": _name_for(ticker), "market": "US",
        "price": price, "change_pct": float(q.pct),
        "high_price": float(q.high), "low_price": float(q.low),
        "source": "유니버스(fallback)",
        "quote_source": str(q.source or "").lower(),
        "quote_as_of": float(q.as_of or 0.0),
        "tags": ("유니버스",), "has_catalyst": False,
    }
    previous_close = price - float(q.change or 0.0)
    turnover = (
        previous_close * float(q.previous_volume)
        if previous_close > 0 and float(q.previous_volume or 0.0) > 0
        else 0.0
    )
    if turnover <= 0:
        turnover = float(q.turnover or 0.0)
    if turnover <= 0 and float(q.volume or 0.0) > 0:
        turnover = price * float(q.volume)
    if turnover > 0:
        out["volume_value"] = turnover
    return out


def _light_quote(ticker: str, market: str) -> dict | None:
    """단일 종목 경량 시세 (requests-only). 실패 시 None — 테스트 seam."""
    try:
        if market == "KR":
            return _light_quote_kr(ticker)
        return _light_quote_us(ticker)
    except Exception as e:
        log.debug("light_quote 실패 [%s]: %s", ticker, e)
        return None


def _pandas_available() -> bool:
    """pandas 설치 여부 — 미설치 시 scanner 경로를 건너뛰고 fallback."""
    import importlib.util
    return importlib.util.find_spec("pandas") is not None


def _markets_for(briefing_type: str) -> list[str]:
    if briefing_type in ("KR_BEFORE", "KR_NIGHT", "KR_OPEN"):
        return ["KR"]
    if briefing_type in ("US_BEFORE", "US_NIGHT"):
        return ["US"]
    return ["KR", "US"]


def _universe_for(markets: list[str]) -> dict[str, tuple[str, str]]:
    """{ticker: (market, name)} — SCAN_UNIVERSE 기반 fallback 모집단."""
    uni: dict[str, tuple[str, str]] = {}
    try:
        from config.settings import SCAN_UNIVERSE_KR, SCAN_UNIVERSE_US
        if "KR" in markets:
            uni.update({t: ("KR", n) for t, n in SCAN_UNIVERSE_KR.items()})
        if "US" in markets:
            uni.update({t: ("US", n) for t, n in SCAN_UNIVERSE_US.items()})
    except Exception as e:
        log.warning("SCAN_UNIVERSE 로드 실패: %s", e)
    return uni


# fallback 유니버스 병렬 스캔 전체 예산 (초) — GET API 응답성 보호
_FALLBACK_SCAN_BUDGET_SEC = 15


def _fallback_universe_candidates(markets: list[str]) -> list[dict]:
    """pandas/pykrx 없이 SCAN_UNIVERSE를 직접 스캔 — 핵심 안전망.

    기존 구현은 50개 내외 종목을 순차 시세 조회해서 /api/toss/buy-candidates
    cold start가 40초 이상 걸렸다. GET 대시보드 경로가 막히지 않도록 경량
    quote는 병렬로 조회하되, 결과 순서는 기존 유니버스 순서를 유지한다.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as FuturesTimeout

    uni = _universe_for(markets)
    entries = list(uni.items())
    if not entries:
        return []

    def _load(idx_ticker_meta):
        idx, (ticker, (mkt, name)) = idx_ticker_meta
        q = _light_quote(ticker, mkt)
        if q is None:
            return idx, None
        q.setdefault("name", name)
        return idx, _attach_fast_quality_inputs(q)

    out_by_idx: dict[int, dict] = {}
    max_workers = min(12, max(1, len(entries)))
    # 전체 스캔 예산: KIS 장애 시 종목당 chart(10s)+price(10s) 직렬 폴백이
    # 워커 웨이브를 타고 25~50초로 번지는 것을 차단 (fail-fast).
    # 정상 시엔 병렬 조회가 수 초 내 끝나 예산에 걸리지 않는다.
    ex = ThreadPoolExecutor(max_workers=max_workers)
    futures = [ex.submit(_load, (idx, item)) for idx, item in enumerate(entries)]
    try:
        for fut in as_completed(futures, timeout=_FALLBACK_SCAN_BUDGET_SEC):
            try:
                idx, q = fut.result()
            except Exception as e:
                log.debug("fallback universe quote failed: %s", e)
                continue
            if q is not None:
                out_by_idx[idx] = q
    except FuturesTimeout:
        log.warning(
            "fallback universe 스캔 예산(%ds) 초과 — %d/%d 종목만 사용 (조회 장애 fail-fast)",
            _FALLBACK_SCAN_BUDGET_SEC, len(out_by_idx), len(entries),
        )
    finally:
        # 미시작 작업은 취소, 진행 중 스레드는 기다리지 않음 (응답성 우선)
        ex.shutdown(wait=False, cancel_futures=True)
    return [out_by_idx[i] for i in sorted(out_by_idx)]


def scan_discovery_candidates(briefing_type: str = "MANUAL") -> tuple[list[dict], dict]:
    """후보 모집단 + 스캔 메타 반환.

    1순위: scanner.py(유니버스+전시장 발굴, pandas 필요).
    pandas 미설치 또는 결과 0건이면 SCAN_UNIVERSE 기반 fallback을 반드시 수행한다.
    단순 예외 로그 후 빈 결과를 반환하지 않는다.
    """
    markets = _markets_for(briefing_type)
    universe_count = len(_universe_for(markets))
    pandas_ok = _pandas_available()
    fallback_used = False
    candidates: list[dict] = []

    if pandas_ok:
        try:
            candidates = _default_scan_candidates(briefing_type)
            # scanner.py는 카테고리별 상위만 반환해서 같은 후보가 반복되기 쉽다.
            # 단, 이미 충분한 후보가 있으면 GET API 응답성을 위해 전체 유니버스 보강은 생략한다.
            # 부족할 때만 병렬 fallback으로 보강한다.
            if len(candidates) < 30:
                seen = {str(c.get("ticker") or "") for c in candidates}
                for q in _fallback_universe_candidates(markets):
                    if q.get("ticker") not in seen:
                        candidates.append(q)
                        seen.add(str(q.get("ticker") or ""))
        except Exception as e:
            log.warning("scanner 경로 실패 → universe fallback: %s", e)
            candidates = []

    if not candidates:
        fallback_used = True
        candidates = _fallback_universe_candidates(markets)

    meta = {
        "universe_count": universe_count,
        "scanned_count": len(candidates),
        "dependency_fallback_used": fallback_used,
        "pandas_available": pandas_ok,
        "source": "universe_fallback" if fallback_used else "scanner",
    }
    return candidates, meta


_REJECT_CATEGORIES = (
    ("데이터 부족", "데이터 부족"),
    ("거래대금", "거래대금/유동성 부족"),
    ("급등 추격", "당일 급등 추격"),
    ("수급 미확인", "수급 미확인"),
    ("손익비", "손익비 부족"),
    ("스캔 결과 없음", "스캔 결과 없음"),
)


def _reject_category(reason: str) -> str:
    for key, label in _REJECT_CATEGORIES:
        if key in reason:
            return label
    return reason


# ── 스캐너 → 후보 정규화 (프로덕션 기본 소스) ───────────────────────

def _default_scan_candidates(briefing_type: str) -> list[dict]:
    """scanner.py 유니버스 스캔 + 전시장 발굴 결과를 후보 dict로 정규화."""
    from core.scanner import scan_market, discover_kr, discover_us
    if briefing_type in ("KR_BEFORE", "KR_NIGHT", "KR_OPEN"):
        markets = ["KR"]
    elif briefing_type in ("US_BEFORE", "US_NIGHT"):
        markets = ["US"]
    else:
        markets = ["KR", "US"]

    out: list[dict] = []
    for m in markets:
        try:
            for h in scan_market(m, top_n=18).hits:
                out.append({
                    "ticker": h.ticker, "name": h.name, "market": m, "price": h.price,
                    "change_pct": h.change_pct, "ret_20d": h.ret_20d, "ret_60d": h.ret_60d,
                    "rsi": h.rsi, "vol_surge": h.vol_surge,
                    "pct_from_52w_high": h.pct_from_52w_high,
                    "open_price": h.open_price, "high_price": h.high_price, "low_price": h.low_price,
                    "volume_value": (_MIN_KR_VALUE_KRW if m == "KR" else _MIN_US_MCAP),
                    "source": "유니버스", "tags": h.tags, "has_catalyst": False,
                })
        except Exception as e:
            log.warning("유니버스 스캔 정규화 실패 (%s): %s", m, e)
        try:
            disc = discover_kr(top_n=25) if m == "KR" else discover_us(top_n=25)
            for d in disc:
                out.append({
                    "ticker": d.ticker, "name": d.name, "market": m, "price": d.price,
                    "change_pct": d.change_pct, "ret_20d": 0.0, "ret_60d": d.ret_60d,
                    "rsi": d.rsi, "vol_surge": 1.0, "pct_from_52w_high": 0.0,
                    "open_price": d.open_price, "high_price": d.high_price, "low_price": d.low_price,
                    "volume_value": d.volume_value, "source": d.source,
                    "tags": (d.source,), "has_catalyst": True,
                })
        except Exception as e:
            log.warning("전시장 발굴 정규화 실패 (%s): %s", m, e)
    return out


def build_discovery_sections(
    scan_candidates: list[dict] | None = None,
    briefing_type: str = "MANUAL",
    held: set[str] | None = None,
    watchlist: set[str] | None = None,
    ria: set[str] | None = None,
    recent_reco: set[str] | None = None,
) -> DiscoverySections:
    """3섹션 분리 결과 생성."""
    if held is None or watchlist is None or ria is None or recent_reco is None:
        d_held, d_wl, d_ria, d_recent = _known_sets()
        held = d_held if held is None else held
        watchlist = d_wl if watchlist is None else watchlist
        ria = d_ria if ria is None else ria
        recent_reco = d_recent if recent_reco is None else recent_reco

    if scan_candidates is None:
        scan_candidates, scan_meta = scan_discovery_candidates(briefing_type)
    else:
        scan_meta = {
            "universe_count": len(scan_candidates),
            "scanned_count": len(scan_candidates),
            "dependency_fallback_used": False,
            "pandas_available": _pandas_available(),
            "source": "injected",
        }

    passed, rejected = build_new_discovery(
        scan_candidates, held=held, watchlist=watchlist, ria=ria, recent_reco=recent_reco,
    )
    rejected_list = list(rejected)

    # 스캔 자체가 비어있으면(런타임 시세/의존성 수집 실패) 사유를 명시 — 빈 결과 금지
    if scan_meta.get("scanned_count", 0) == 0:
        rejected_list.insert(0, RejectedCandidate(
            ticker="*", name="신규 스캔",
            reason="신규 스캔 결과 없음 — 런타임 시세/의존성 수집 실패 (universe fallback도 비어있음)",
        ))

    cat_counts: dict[str, int] = {}
    for r in rejected_list:
        cat = _reject_category(r.reason)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    top_reasons = [
        {"reason": k, "count": v}
        for k, v in sorted(cat_counts.items(), key=lambda x: -x[1])[:_TOP_REJECTED]
    ]
    scan_summary = {
        **scan_meta,
        "pass_count": len(passed),
        "reject_count": len(rejected_list),
        "top_reject_reasons": top_reasons,
    }

    holdings = tuple(
        {"ticker": t, "name": _name_for(t), "section": "보유종목 관리"}
        for t in sorted(held)
    )
    reeval = tuple(
        {"ticker": t, "name": _name_for(t), "section": "기존 관심종목 재평가"}
        for t in sorted((watchlist | ria | recent_reco) - held)
    )
    market = "KR" if briefing_type in ("KR_BEFORE", "KR_NIGHT", "KR_OPEN") else (
        "US" if briefing_type in ("US_BEFORE", "US_NIGHT") else "KR/US")

    return DiscoverySections(
        holdings_management=holdings,
        watchlist_reeval=reeval,
        new_discovery=passed,
        new_rejected=tuple(rejected_list[:_TOP_REJECTED]),
        market=market,
        scan_summary=scan_summary,
    )


# ── 브리핑 텍스트 (idea-first, 신규 섹션 항상 출력) ────────────────

def render_discovery_text(sections: DiscoverySections) -> str:
    """프롬프트/브리핑 주입용 3섹션 텍스트."""
    lines: list[str] = []

    lines.append("━━━ 1) 보유종목 관리 (리스크/익절/손절/홀딩만) ━━━")
    if sections.holdings_management:
        lines.append("  " + ", ".join(
            f"{h['name']}({h['ticker']})" for h in sections.holdings_management))
    else:
        lines.append("  (보유 종목 없음)")

    lines.append("")
    lines.append("━━━ 2) 기존 관심종목 재평가 (WATCHLIST/RIA/과거추천만) ━━━")
    if sections.watchlist_reeval:
        lines.append("  " + ", ".join(
            f"{w['name']}({w['ticker']})" for w in sections.watchlist_reeval))
    else:
        lines.append("  (관심 종목 없음)")

    lines.append("")
    lines.append("━━━ 3) 🆕 신규 발굴 (보유·관심·RIA 밖 새 종목만) ━━━")
    lines.append("오늘 신규 발굴 TOP 3:")
    if sections.new_discovery:
        for i, c in enumerate(sections.new_discovery[:_TOP_NEW], 1):
            lines.append(f"  {i}. 💡 {c.idea}")
            lines.append(
                f"     근거: {', '.join(c.reasons)} | 점수 {c.score}")
            unit = "₩" if c.market == "KR" else "$"
            lines.append(
                f"     진입 {unit}{c.price:,.0f} · 목표 {unit}{c.target_price:,.0f}"
                f" · 손절 {unit}{c.stop_loss:,.0f} · 손익비 {c.risk_reward:.1f}:1")
            if c.risk_flags:
                lines.append(f"     위험 플래그: {', '.join(c.risk_flags)}")
            accounts = ", ".join(c.suggested_accounts) if c.suggested_accounts else "계좌 배정 필요"
            lines.append(f"     계좌 후보: {accounts}")
    else:
        lines.append("  신규 후보 없음 — 통과 후보 0개 (탈락 상위 사유):")
        if sections.new_rejected:
            for r in sections.new_rejected:
                lines.append(f"    - {r.name}({r.ticker}): {r.reason}")
        else:
            lines.append("    - 스캔 결과 없음 (데이터 부족)")

    return "\n".join(lines)


# ── 토스 적격 후보 (신규 발굴 기반만) ──────────────────────────────

def toss_eligible_new_candidates(
    sections: DiscoverySections,
    max_order_krw: int = 100_000,
) -> dict:
    """신규 발굴 후보 중 토스 조건(KR/US, 1주 ≤ 한도/BUY)을 통과한 후보만 items.

    기존 삼성/RIA 추천을 재사용하지 않는다. items 0이면 excluded에
    '기존 후보 제외' + '신규 스캔 탈락 이유'를 함께 담는다.
    """
    items: list[dict] = []
    excluded: list[dict] = []
    scan_summary = dict(getattr(sections, "scan_summary", {}) or {})

    # 기존 후보 재사용 금지 명시
    excluded.append({
        "ticker": "*",
        "reason": "기존 삼성/RIA/관심 후보는 토스 후보로 재사용 안 함 (신규 발굴 기반만 사용)",
        "scope": "reuse_blocked",
    })

    # 스캔 자체가 0건이면 런타임 사유를 명시 (reuse_blocked 단독 노출 방지)
    if scan_summary.get("scanned_count", 1) == 0:
        excluded.append({
            "ticker": "*",
            "reason": "신규 스캔 불가 — 런타임 시세/의존성 수집 실패 (universe fallback도 비어있음)",
            "scope": "scan_unavailable",
        })

    def _usdkrw_rate() -> float:
        try:
            from core import toss_client as tc
            fx = tc.get_exchange_rate("USD", "KRW") or {}
            return float(fx.get("rate") or fx.get("midRate") or 1500.0)
        except Exception:
            return 1500.0

    usdkrw = _usdkrw_rate()
    limit_exceeded_count = 0
    for c in sections.new_discovery:
        if c.market not in ("KR", "US"):
            excluded.append({
                "ticker": c.ticker, "name": c.name,
                "reason": "토스 조건 미충족: 지원하지 않는 시장",
                "scope": "toss_market_unsupported",
            })
            continue
        est = c.price if c.market == "KR" else c.price * usdkrw  # 1주 기준 KRW 환산
        over_limit = est > max_order_krw
        blocking_flags = _blocking_risk_flags(c.risk_flags)
        observation_flags = _soft_observation_flags(c.risk_flags)
        if c.quality_data_starved and "quality_data_starvation" not in blocking_flags:
            blocking_flags.append("quality_data_starvation")
        item = {
            "symbol": c.ticker, "name": c.name, "side": "buy", "quantity": 1,
            "price": c.price,
            "limit_price": c.price,
            "currency": "KRW" if c.market == "KR" else "USD",
            "asset_type": "KR_STOCK" if c.market == "KR" else "US_STOCK",
            "estimated_amount_krw": round(est, 2),
            "estimated_amount_usd": round(c.price, 2) if c.market == "US" else None,
            "fx_usdkrw": round(usdkrw, 4) if c.market == "US" else None,
            "market": c.market, "idea": c.idea, "score": c.score,
            "target_price": c.target_price, "stop_loss": c.stop_loss,
            "risk_reward": c.risk_reward,
            "open_price": c.open_price, "high_price": c.high_price, "low_price": c.low_price,
            "intraday_drawdown_pct": c.intraday_drawdown_pct,
            "intraday_range_pct": c.intraday_range_pct,
            "risk_flags": list(c.risk_flags),
            "blocking_risk_flags": blocking_flags,
            "observation_flags": observation_flags,
            "suggested_accounts": list(c.suggested_accounts),
            "candidate_scope": "new_discovery", "read_only": True,
            # 한도는 실주문 gate 전용 — 발굴/표시 단계에서는 후보를 배제하지 않음.
            # 단, 수급 약함 단독은 HOLD 차단이 아니라 소액 조건부 후보로 완화한다.
            "executable_now": not over_limit and not blocking_flags,
            "limit_exceeded": over_limit,
        }
        if c.quality_input_provenance or c.quality_data_starved:
            item.update({
                "quality_inputs": dict(c.quality_inputs),
                "quality_input_provenance": dict(c.quality_input_provenance),
                "quality_data_starved": c.quality_data_starved,
                "quality_data_starvation_reason": c.quality_data_starvation_reason,
                "quality_score_authority": "quality_breakdown.score_total",
            })
        if c.quality_data_starved:
            item["upstream_input_validation_error"] = "quality_data_starvation"
        if blocking_flags:
            item["execution_status"] = "hold_risk_flags"
            item["block_reason"] = " / ".join(blocking_flags + observation_flags)
            item["suggested_action"] = "토스 즉시 실행 금지 · 삼성/ISA 포함 감시 후보로 재검토"
        elif over_limit:
            limit_exceeded_count += 1
            item["execution_status"] = "limit_exceeded"
            item["block_reason"] = (
                f"1주 원화환산 {est:,.0f}원 > 현재 1회 한도 {max_order_krw:,.0f}원"
            )
            item["suggested_action"] = "한도 상향 또는 수동 승인 필요"
        elif observation_flags:
            item["execution_status"] = "conditional_small_entry"
            item["observation_reason"] = " / ".join(observation_flags)
            item["suggested_action"] = "소액 조건부 관찰매수 후보 · Hermes PASS 후 결정론 안전 게이트 자동 진행"
        else:
            item["execution_status"] = "executable"
        items.append(item)

    # 신규 스캔 탈락 사유도 excluded에 포함
    for r in sections.new_rejected:
        excluded.append({
            "ticker": r.ticker, "name": r.name,
            "reason": f"신규 스캔 탈락: {r.reason}",
            "scope": "scan_rejected",
        })

    # 품질 게이트 점수화 (자동매매 PASS 품질 강화)
    try:
        from core.toss_quality_gate import score_candidates_batch
        score_market = getattr(sections, "market", "KR")
        if score_market == "KR/US":
            score_market = "ALL"
        items = score_candidates_batch(items, market=score_market)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("quality gate scoring failed: %s", e)

    items = _rotate_candidate_groups(items)

    executable_count = sum(1 for i in items if i.get("executable_now") is True)
    conditional_count = sum(1 for i in items if i.get("execution_status") == "conditional_small_entry")
    scan_summary["pass_count"] = len(items)
    scan_summary["executable_count"] = executable_count
    scan_summary["conditional_small_entry_count"] = conditional_count
    scan_summary["limit_exceeded_count"] = limit_exceeded_count

    return {
        "items": items,
        "excluded": excluded,
        "count": len(items),
        "excluded_count": len(excluded),
        "scan_summary": scan_summary,
        "note": (
            "신규 발굴 기반 토스 후보 표시 (기존 삼성/RIA 재사용 안 함). "
            "1주 가격이 1회 한도 초과인 종목도 후보로 표시하되 "
            "execution_status=limit_exceeded/hold_risk_flags로 즉시 실행 불가 처리. "
            "수급 약함/손익비 보통 단독은 conditional_small_entry로 완화 표시. "
            "같은 상태 후보는 5분 단위로 노출 순환."
        ),
    }


# ── 계좌 비의존 광역 시장 레이더 ───────────────────────────────────
def market_discovery_radar(sections: DiscoverySections, limit: int = 50) -> dict:
    """Toss 전용이 아닌 삼성/ISA/RIA/IRP/토스 공용 광역 후보 레이더.

    발굴은 넓게 유지하고, 계좌/실행 제약은 후단에서 분리한다.
    이 함수는 read-only 데이터 구조만 반환한다.
    """
    items: list[dict] = []
    for c in list(sections.new_discovery)[:limit]:
        action_bias = "WATCH"
        blocking_flags = _blocking_risk_flags(c.risk_flags)
        observation_flags = _soft_observation_flags(c.risk_flags)
        if not c.risk_flags and c.risk_reward >= 1.5:
            action_bias = "ACCOUNT_REVIEW"
        if observation_flags and not blocking_flags:
            action_bias = "CONDITIONAL_SMALL_ENTRY"
        if blocking_flags:
            action_bias = "HOLD_RISK_REVIEW"
        items.append({
            "symbol": c.ticker, "name": c.name, "market": c.market,
            "price": c.price, "change_pct": c.change_pct,
            "score": c.score, "idea": c.idea, "reasons": list(c.reasons),
            "target_price": c.target_price, "stop_loss": c.stop_loss, "risk_reward": c.risk_reward,
            "open_price": c.open_price, "high_price": c.high_price, "low_price": c.low_price,
            "intraday_drawdown_pct": c.intraday_drawdown_pct,
            "intraday_range_pct": c.intraday_range_pct,
            "risk_flags": list(c.risk_flags),
            "blocking_risk_flags": blocking_flags,
            "observation_flags": observation_flags,
            "suggested_accounts": list(c.suggested_accounts),
            "action_bias": action_bias,
            "read_only": True,
        })
    items = _rotate_candidate_groups(items)

    account_buckets: dict[str, list[dict]] = {}
    for item in items:
        for acc in item.get("suggested_accounts") or ["계좌 배정 필요"]:
            account_buckets.setdefault(acc, []).append(item)
    return {
        "items": items,
        "count": len(items),
        "account_buckets": {k: v[:10] for k, v in account_buckets.items()},
        "rejected": [
            {"ticker": r.ticker, "name": r.name, "reason": r.reason}
            for r in sections.new_rejected
        ],
        "scan_summary": sections.scan_summary,
        "schema": "market_discovery_radar.v1.account_agnostic",
        "note": "광역 후보 발굴은 계좌/한도 제약 없이 수행하고, 삼성/ISA/RIA/IRP/토스 배정은 후단에서 분리한다. 같은 상태 후보는 5분 단위로 순환 노출한다.",
    }
