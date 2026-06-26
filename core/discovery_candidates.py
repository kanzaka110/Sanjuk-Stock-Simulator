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
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ── 발굴 게이트 상수 ───────────────────────────────────────────────
_CHASE_CHANGE_PCT = 8.0          # 당일 등락률 초과 시 추격 금지
_MIN_RISK_REWARD = 1.5           # 목표:손절 최소 1.5:1
_STOP_PCT = 0.06                 # 손절 폭 (가격 대비)
_TARGET_BASE = 0.05              # 목표 폭 기본값
_MIN_KR_VALUE_KRW = 30_000_000_000   # 한국 거래대금 300억+ (유동성)
_MIN_US_MCAP = 2_000_000_000         # 미국 시총 $2B+
_TOP_NEW = 3                     # 신규 발굴 TOP N
_TOP_REJECTED = 5                # 탈락 표시 상위 N


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
    tags: tuple[str, ...] = ()


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
    stop = round(price * (1 - _STOP_PCT), 2)
    target = round(price * (1 + target_pct), 2)
    rr = round(target_pct / _STOP_PCT, 2) if _STOP_PCT else 0.0
    return target, stop, rr


def _gate(c: dict) -> str:
    """탈락 사유 반환 (통과 시 빈 문자열)."""
    price = float(c.get("price") or 0)
    if price <= 0:
        return "데이터 부족 (가격/시세 없음)"

    market = _market_of(c.get("ticker", ""), c.get("market", ""))
    value = float(c.get("volume_value") or 0)
    min_value = _MIN_KR_VALUE_KRW if market == "KR" else _MIN_US_MCAP
    if value < min_value:
        return "거래대금/유동성 부족"

    change = float(c.get("change_pct") or 0)
    if change > _CHASE_CHANGE_PCT:
        return f"당일 급등 추격 위험 (+{change:.1f}%)"

    vol_surge = float(c.get("vol_surge") or 0)
    if vol_surge < 1.0 and not c.get("has_catalyst"):
        return "수급 미확인 (거래량/촉매 없음)"

    _, _, rr = _risk_reward(c)
    if rr < _MIN_RISK_REWARD:
        return f"손익비 부족 ({rr:.1f}:1 < 1.5:1)"

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
        passed.append(NewCandidate(
            ticker=ticker, name=name,
            market=_market_of(ticker, c.get("market", "")),
            price=float(c.get("price") or 0),
            score=score, idea=_idea(c, reasons), reasons=tuple(reasons),
            target_price=target, stop_loss=stop, risk_reward=rr,
            change_pct=float(c.get("change_pct") or 0),
            tags=tuple(c.get("tags") or ()),
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

    if closes:
        price = closes[-1]
        change_pct = float(chart.get("day_pct") or 0.0)
    else:
        q = get_domestic_price(ticker)
        if q is None:
            return None
        price = float(q.price)
        change_pct = float(q.pct)
    if price <= 0:
        return None

    turnover = _avg_turnover(closes, vols)
    return {
        "ticker": ticker, "name": _name_for(ticker), "market": "KR",
        "price": price, "change_pct": change_pct,
        "ret_20d": _ret_pct(closes, 20), "ret_60d": _ret_pct(closes, 60),
        "rsi": _rsi_from_closes(closes), "vol_surge": _vol_surge(vols),
        "pct_from_52w_high": _pct_from_high(closes),
        # 거래대금 미산출 시 curated 유니버스이므로 최소 기준값으로 보정
        "volume_value": turnover if turnover > 0 else _MIN_KR_VALUE_KRW,
        "source": "유니버스(fallback)", "tags": ("유니버스",),
        "has_catalyst": False,
    }


def _light_quote_us(ticker: str) -> dict | None:
    """KIS 해외 현재가(requests-only)로 US 종목. 모멘텀은 중립(차트 미수집)."""
    from core.market_kis import get_overseas_price

    q = get_overseas_price(ticker)
    if q is None:
        return None
    price = float(q.price)
    if price <= 0:
        return None
    return {
        "ticker": ticker, "name": _name_for(ticker), "market": "US",
        "price": price, "change_pct": float(q.pct),
        "ret_20d": 0.0, "ret_60d": 0.0, "rsi": 50.0, "vol_surge": 1.0,
        "pct_from_52w_high": 0.0, "volume_value": _MIN_US_MCAP,
        "source": "유니버스(fallback)", "tags": ("유니버스",),
        "has_catalyst": False,
    }


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


def _fallback_universe_candidates(markets: list[str]) -> list[dict]:
    """pandas/pykrx 없이 SCAN_UNIVERSE를 직접 스캔 — 핵심 안전망."""
    uni = _universe_for(markets)
    out: list[dict] = []
    for ticker, (mkt, name) in uni.items():
        q = _light_quote(ticker, mkt)
        if q is None:
            continue
        q.setdefault("name", name)
        out.append(q)
    return out


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
            for h in scan_market(m).hits:
                out.append({
                    "ticker": h.ticker, "name": h.name, "market": m, "price": h.price,
                    "change_pct": 0.0, "ret_20d": h.ret_20d, "ret_60d": h.ret_60d,
                    "rsi": h.rsi, "vol_surge": h.vol_surge,
                    "pct_from_52w_high": h.pct_from_52w_high,
                    "volume_value": (_MIN_KR_VALUE_KRW if m == "KR" else _MIN_US_MCAP),
                    "source": "유니버스", "tags": h.tags, "has_catalyst": False,
                })
        except Exception as e:
            log.warning("유니버스 스캔 정규화 실패 (%s): %s", m, e)
        try:
            disc = discover_kr() if m == "KR" else discover_us()
            for d in disc:
                out.append({
                    "ticker": d.ticker, "name": d.name, "market": m, "price": d.price,
                    "change_pct": d.change_pct, "ret_20d": 0.0, "ret_60d": d.ret_60d,
                    "rsi": d.rsi, "vol_surge": 1.0, "pct_from_52w_high": 0.0,
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
            toss_ok = c.market == "KR" and c.price <= 100_000
            lines.append(
                f"     적합 계좌: 일반/ISA 검토 · 토스 소액 {'가능' if toss_ok else '불가(고가/해외)'}")
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
    """신규 발굴 후보 중 토스 소액 조건(KR/1주 ≤ 한도/BUY)을 통과한 후보만 items.

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

    limit_exceeded_count = 0
    for c in sections.new_discovery:
        if c.market != "KR":
            excluded.append({
                "ticker": c.ticker, "name": c.name,
                "reason": "토스 소액 조건 미충족: 해외 종목 (KRW 소액 대상 아님)",
                "scope": "toss_soak",
            })
            continue
        est = c.price  # 1주 기준
        over_limit = est > max_order_krw
        item = {
            "symbol": c.ticker, "name": c.name, "side": "buy", "quantity": 1,
            "price": c.price,
            "limit_price": c.price, "estimated_amount_krw": round(c.price, 2),
            "market": c.market, "idea": c.idea, "score": c.score,
            "target_price": c.target_price, "stop_loss": c.stop_loss,
            "risk_reward": c.risk_reward,
            "candidate_scope": "new_discovery", "read_only": True,
            # 한도는 실주문 gate 전용 — 발굴/표시 단계에서는 후보를 배제하지 않음
            "executable_now": not over_limit,
            "limit_exceeded": over_limit,
        }
        if over_limit:
            limit_exceeded_count += 1
            item["execution_status"] = "limit_exceeded"
            item["block_reason"] = (
                f"1주 {est:,.0f}원 > 현재 1회 한도 {max_order_krw:,.0f}원"
            )
            item["suggested_action"] = "한도 상향 또는 수동 승인 필요"
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

    executable_count = sum(1 for i in items if i.get("executable_now"))
    scan_summary["pass_count"] = len(items)
    scan_summary["executable_count"] = executable_count
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
            "execution_status=limit_exceeded로 즉시 실행 불가 처리."
        ),
    }
