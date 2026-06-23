"""
Toss paper 성과 채점/추적

- approved 상태만 평가 (win/loss 분모)
- cancelled/blocked/previewed 제외 (승률 분모 미포함)
- expired/data_error 별도 카운트
- 실제 주문 0건 — read-only 가격 조회만 사용
- 기존 포트폴리오(/api/portfolio)에 합산 금지
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
logger = logging.getLogger(__name__)

# 평가용 기본값 (paper 평가 가정, 실제 투자 조언 아님)
_DEFAULT_TARGET_PCT = 0.03   # +3%
_DEFAULT_STOP_PCT = 0.03     # -3%
_DEFAULT_EXPIRE_DAYS = 7     # 7 calendar days

# win/loss 분모에서 제외할 상태
_EXCLUDE_FROM_DENOMINATOR = {"cancelled", "blocked", "previewed"}


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _classify_error_type(source_chain: list[dict], cur_price: float | None) -> dict:
    """source_chain 분석으로 data_error 세부 유형을 반환한다.

    Returns:
        {
            "error_type": "price_unavailable" | "consensus_anomaly" | "price_anomaly",
            "consensus_price": float | None,
            "consensus_sources": list[str],
        }
    """
    # price가 있는 rejected 항목만
    anomaly_entries = [
        e for e in source_chain
        if e.get("price") and e.get("price", 0) > 0 and not e.get("accepted") and "이상치" in e.get("reason", "")
    ]

    if not anomaly_entries:
        # 가격 자체를 못 얻음
        return {"error_type": "price_unavailable", "consensus_price": None, "consensus_sources": []}

    if len(anomaly_entries) < 2:
        # 단일 소스만 이상치
        return {"error_type": "price_anomaly", "consensus_price": None, "consensus_sources": []}

    # 여러 소스 이상치 — 가격이 거의 동일한지 확인 (max/min 1% 이내)
    prices = [e["price"] for e in anomaly_entries]
    price_min, price_max = min(prices), max(prices)
    if price_min > 0 and (price_max - price_min) / price_min <= 0.01:
        return {
            "error_type": "consensus_anomaly",
            "consensus_price": round(price_max, 2),
            "consensus_sources": [e["source"] for e in anomaly_entries],
        }

    # 여러 소스지만 가격이 서로 다름
    return {"error_type": "price_anomaly", "consensus_price": None, "consensus_sources": []}


def _get_quote(symbol: str) -> tuple[float | None, str]:
    """read-only 가격 조회. 실패해도 전체 실패 없음."""
    result = _get_quote_for_paper(symbol)
    return result["price"], result["source"]


def _get_quote_for_paper(symbol: str, entry_price: float | None = None) -> dict:
    """각 소스를 순서대로 시도하며 source_chain을 기록한다.

    국내 종목 순서: KIS → naver_current → yfinance_live → yfinance_daily
    해외 종목 순서: KIS → yfinance_live → yfinance_daily

    entry_price 제공 시 소스별로 ±50% 이상치 체크를 수행한다.
    이상치인 소스는 건너뛰고 다음 소스를 시도한다.

    Returns:
        {
            "price": float | None,
            "source": str,
            "accepted_price_source": str | None,
            "source_chain": list[dict],
        }
    """
    from core.market import _get_quote_kis, _get_quote_yf_live, _get_quote_daily
    from core.kr_price_fallback import is_kr_ticker, get_kr_stock_price_fallback

    def _step_market(name: str, fn) -> tuple[str, object]:
        """market 함수 래핑 — Quote 객체 반환."""
        return (name, fn)

    steps_market = [
        ("KIS", _get_quote_kis),
        ("yfinance_live", _get_quote_yf_live),
        ("yfinance_daily", _get_quote_daily),
    ]

    source_chain: list[dict] = []
    accepted_price: float | None = None
    accepted_source: str | None = None
    _is_kr = is_kr_ticker(symbol)

    def _try_price(source_name: str, raw_price: float) -> bool:
        """가격 이상치 체크 후 accepted 여부 반환. source_chain에 항목 추가."""
        nonlocal accepted_price, accepted_source
        entry: dict = {
            "source": source_name,
            "price": raw_price,
            "accepted": False,
            "reason": "",
        }
        if entry_price and entry_price > 0:
            ratio = raw_price / entry_price
            entry["ratio_to_entry"] = round(ratio, 4)
            if ratio > 1.5 or ratio < 0.5:
                entry["reason"] = f"이상치(ratio={ratio:.4f})"
                source_chain.append(entry)
                logger.debug(
                    "paper price anomaly from %s [%s]: %.0f / %.0f = %.4f",
                    source_name, symbol, raw_price, entry_price, ratio,
                )
                return False
        entry["accepted"] = True
        entry["reason"] = "정상" if (entry_price and entry_price > 0) else "entry 미제공(이상치 미검사)"
        source_chain.append(entry)
        accepted_price = raw_price
        accepted_source = source_name
        return True

    # ─── market 함수 기반 소스 (KIS / yfinance) ─────────────────
    # 국내: KIS → (naver 삽입) → yfinance_live → yfinance_daily
    # 해외: KIS → yfinance_live → yfinance_daily
    for source_name, fn in steps_market:
        # 국내 종목 한정: KIS reject 후 naver_current 먼저 시도
        if _is_kr and source_name == "yfinance_live" and accepted_price is None:
            try:
                fb = get_kr_stock_price_fallback(symbol)
                if fb["ok"] and fb["price"] and fb["price"] > 0:
                    if _try_price(fb["source"], float(fb["price"])):
                        break
                else:
                    source_chain.append({
                        "source": fb["source"],
                        "price": None,
                        "accepted": False,
                        "reason": fb.get("warning") or "가격 없음",
                    })
            except Exception as exc:
                source_chain.append({
                    "source": "naver_current",
                    "price": None,
                    "accepted": False,
                    "reason": f"오류: {exc}",
                })
                logger.debug("naver fallback error [%s]: %s", symbol, exc)

        entry: dict = {"source": source_name, "price": None, "accepted": False, "reason": ""}
        try:
            q = fn(symbol)
            if q and q.price and q.price > 0:
                raw_price = float(q.price)
                entry["price"] = raw_price
                if _try_price(source_name, raw_price):
                    break
                # _try_price가 False면 이미 source_chain에 추가됨
            else:
                entry["reason"] = "가격 없음"
                source_chain.append(entry)
        except Exception as exc:
            entry["reason"] = f"오류: {exc}"
            source_chain.append(entry)
            logger.debug("paper quote error from %s [%s]: %s", source_name, symbol, exc)

    if accepted_price is None:
        return {
            "price": None,
            "source": "unavailable",
            "accepted_price_source": None,
            "source_chain": source_chain,
        }

    return {
        "price": accepted_price,
        "source": accepted_source,
        "accepted_price_source": accepted_source,
        "source_chain": source_chain,
    }


def _parse_kst(ts: str | None) -> datetime | None:
    if not ts:
        return None
    # 전체 문자열 또는 앞부분으로 순서대로 시도
    candidates = [
        (ts, "%Y-%m-%dT%H:%M:%S+09:00"),
        (ts, "%Y-%m-%dT%H:%M:%S"),
        (ts, "%Y-%m-%d %H:%M:%S"),
        (ts[:10], "%Y-%m-%d"),
    ]
    for raw, fmt in candidates:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def evaluate_paper_order(order: dict, quote: dict | None = None) -> dict:
    """
    paper ledger row 하나를 평가해 성과 필드를 반환한다.
    quote: {"price": float, "source": str} — 외부 주입 가능 (테스트용).
    실제 주문 없음. read-only.
    """
    paper_id = order.get("paper_id", "")
    symbol = order.get("symbol", "")
    side = order.get("side", "buy")
    status = order.get("status", "")
    entry_price = float(order.get("limit_price") or 0)
    quantity = int(order.get("quantity") or 0)
    created_at = order.get("created_at", "")

    result: dict = {
        "paper_id": paper_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "entry_price": entry_price,
        "quantity": quantity,
        "entry_amount_krw": round(entry_price * quantity, 2),
        "current_price": None,
        "current_value_krw": None,
        "unrealized_pnl_krw": None,
        "unrealized_pnl_pct": None,
        "target_price": None,
        "stop_price": None,
        "expires_at": None,
        "outcome": "open",
        "evaluated_at": _now_kst(),
        "data_source": "unavailable",
        "accepted_price_source": None,
        "source_chain": [],
        "error_type": None,
        "consensus_price": None,
        "consensus_sources": [],
        "warnings": [],
        "_note": "Paper 성과 · 실제 주문 아님 · 기존 포트폴리오 미합산 · 실주문 비활성",
    }

    # cancelled/blocked/previewed → 성과 평가 제외, 승률 분모 제외
    if status in _EXCLUDE_FROM_DENOMINATOR:
        result["outcome"] = status
        return result

    # target/stop 기본 규칙 (평가용 가정 — 실제 투자 조언 아님)
    if entry_price > 0:
        result["target_price"] = round(entry_price * (1 + _DEFAULT_TARGET_PCT), 2)
        result["stop_price"] = round(entry_price * (1 - _DEFAULT_STOP_PCT), 2)
        result["_target_stop_note"] = "평가용 기본값(+3%/-3%) — 실제 투자 조언 아님"

    # 만료 기준
    created_dt = _parse_kst(created_at)
    is_expired = False
    if created_dt:
        expires_dt = created_dt + timedelta(days=_DEFAULT_EXPIRE_DAYS)
        result["expires_at"] = expires_dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        is_expired = datetime.now(KST) > expires_dt

    # 가격 조회 (read-only) — source_chain으로 어느 소스가 틀렸는지 추적
    if quote is not None:
        cur_price = quote.get("price")
        data_source = quote.get("source", "injected")
        source_chain: list[dict] = []
        accepted_price_source: str | None = data_source if cur_price else None
    else:
        _q = _get_quote_for_paper(symbol, entry_price if entry_price > 0 else None)
        cur_price = _q["price"]
        data_source = _q["source"]
        source_chain = _q["source_chain"]
        accepted_price_source = _q["accepted_price_source"]

    result["data_source"] = data_source
    result["accepted_price_source"] = accepted_price_source
    result["source_chain"] = source_chain

    if cur_price is None or cur_price <= 0:
        result["outcome"] = "data_error"
        classification = _classify_error_type(source_chain, cur_price)
        result["error_type"] = classification["error_type"]
        result["consensus_price"] = classification["consensus_price"]
        result["consensus_sources"] = classification["consensus_sources"]
        if classification["error_type"] == "consensus_anomaly":
            result["warnings"].append(
                "여러 가격 소스가 entry 대비 이상치 가격에 합의 — 기업행동/entry_price 재확인 필요"
            )
        else:
            result["warnings"].append("가격 조회 실패")
        result["price_anomaly"] = False
        result["price_ratio"] = None
        return result

    # 가격 이상치 guard — entry 대비 ±50% 초과 시 평가 보류
    # _get_quote_for_paper가 소스별 이상치를 걸러주지만, 외부 quote 주입 시에도 방어
    price_ratio: float | None = None
    price_anomaly = False
    if entry_price > 0:
        _raw_ratio = cur_price / entry_price
        price_ratio = round(_raw_ratio, 4)
        if _raw_ratio > 1.5 or _raw_ratio < 0.5:
            price_anomaly = True
            # 외부 quote 주입 시에는 단일 소스 이상치
            result["price_anomaly"] = True
            result["price_ratio"] = price_ratio
            result["current_price"] = cur_price
            result["outcome"] = "data_error"
            result["error_type"] = "price_anomaly"
            result["warnings"].append(
                f"가격 이상치로 평가 보류 (entry={entry_price:,.0f}, current={cur_price:,.0f}, ratio={price_ratio:.2f})"
            )
            return result

    result["price_anomaly"] = price_anomaly
    result["price_ratio"] = price_ratio
    result["current_price"] = cur_price
    result["current_value_krw"] = round(cur_price * quantity, 2)
    result["unrealized_pnl_krw"] = round((cur_price - entry_price) * quantity, 2)
    result["unrealized_pnl_pct"] = (
        round((cur_price - entry_price) / entry_price * 100, 2) if entry_price else 0.0
    )

    # 결과 판정 (buy 기준)
    target = result["target_price"]
    stop = result["stop_price"]

    if target and cur_price >= target:
        result["outcome"] = "win"
    elif stop and cur_price <= stop:
        result["outcome"] = "loss"
    elif is_expired:
        result["outcome"] = "expired"
    else:
        result["outcome"] = "open"

    return result


def evaluate_open_paper_orders(limit: int = 100) -> dict:
    """
    approved 상태 paper orders 일괄 평가.
    cancelled/blocked/previewed 제외. 실제 주문 0건.
    """
    from core.toss_paper_ledger import list_paper_orders

    orders = list_paper_orders(status="approved", limit=limit)
    evaluated = [evaluate_paper_order(o) for o in orders]

    return {
        "evaluated": evaluated,
        "count": len(evaluated),
        "evaluated_at": _now_kst(),
        "_note": "Paper 성과 평가 · 실제 주문 아님 · 기존 포트폴리오 미합산",
    }


def get_paper_performance_summary() -> dict:
    """
    paper ledger 전체 성과 요약.
    - win_rate 분모 = win + loss만
    - cancelled/blocked/previewed 제외
    - expired/data_error 별도 카운트
    - 표본부족은 위험으로 표시하지 않음
    - 실제 주문 0건
    """
    from core.toss_paper_ledger import list_paper_orders, paper_ledger_summary

    summary_base = paper_ledger_summary()
    counts = summary_base.get("counts", {})
    total = summary_base.get("total", 0)

    orders = list_paper_orders(status="approved", limit=200)
    evaluated = [evaluate_paper_order(o) for o in orders]

    wins = [e for e in evaluated if e["outcome"] == "win"]
    losses = [e for e in evaluated if e["outcome"] == "loss"]
    open_orders = [e for e in evaluated if e["outcome"] == "open"]
    expired_orders = [e for e in evaluated if e["outcome"] == "expired"]
    data_errors = [e for e in evaluated if e["outcome"] == "data_error"]
    consensus_anomalies = [e for e in data_errors if e.get("error_type") == "consensus_anomaly"]

    # win_rate 분모 = win + loss만 (cancelled/blocked/previewed/expired/data_error 제외)
    denominator = len(wins) + len(losses)
    win_rate = round(len(wins) / denominator * 100, 1) if denominator else 0.0

    # avg_pnl_pct: win + loss 결과만 (pnl 있는 것)
    pnl_list = [
        e["unrealized_pnl_pct"]
        for e in evaluated
        if e["outcome"] in ("win", "loss") and e["unrealized_pnl_pct"] is not None
    ]
    avg_pnl_pct = round(sum(pnl_list) / len(pnl_list), 2) if pnl_list else 0.0

    # recent: 최근 10건
    recent = list(reversed(evaluated[-10:])) if evaluated else []

    return {
        "summary": {
            "total": total,
            "open": len(open_orders),
            "wins": len(wins),
            "losses": len(losses),
            "cancelled": counts.get("cancelled", 0),
            "blocked": counts.get("blocked", 0),
            "previewed": counts.get("previewed", 0),
            "expired": len(expired_orders),
            "data_error": len(data_errors),
            "consensus_anomaly": len(consensus_anomalies),
            "evaluated_count": denominator,
            "win_rate": win_rate,
            "avg_pnl_pct": avg_pnl_pct,
        },
        "recent": recent,
        "evaluated_at": _now_kst(),
        "_note": "Paper 성과 · 실제 주문 아님 · 기존 포트폴리오 미합산 · 실주문 비활성",
    }


def format_toss_paper_performance_briefing(summary: dict | None = None) -> str:
    """브리핑/LLM 입력용 Toss Paper 성과 요약 텍스트.

    - evaluated_count == 0 → '표본부족 / 평가 대기' (0.0%로 오해 방지)
    - 항상 '실제 주문 아님', '실주문 비활성', '기존 포트폴리오 미합산' 포함
    - blocked/cancelled/previewed는 실패로 표현하지 않음
    - 기존 주식 예측 DB 승률과 합산 금지
    """
    if summary is None:
        try:
            summary = get_paper_performance_summary()
        except Exception:
            summary = {}

    s = (summary or {}).get("summary", {})
    evaluated = s.get("evaluated_count", 0)
    wins = s.get("wins", 0)
    losses = s.get("losses", 0)
    open_ = s.get("open", 0)
    win_rate = s.get("win_rate", 0.0)
    avg_pnl = s.get("avg_pnl_pct", 0.0)
    blocked = s.get("blocked", 0)
    cancelled = s.get("cancelled", 0)
    previewed = s.get("previewed", 0)
    expired = s.get("expired", 0)
    data_error = s.get("data_error", 0)
    consensus_anomaly = s.get("consensus_anomaly", 0)

    # consensus_anomaly 발생 종목 이름 수집 (recent에서 추출)
    recent = (summary or {}).get("recent", [])
    consensus_symbols = [
        r.get("symbol", "") for r in recent
        if r.get("error_type") == "consensus_anomaly"
    ]

    lines = ["[Toss Paper 성과 — 실제 주문 아님]"]
    lines.append(f"- 평가 완료: {evaluated}건")
    lines.append(f"- 진행 중: {open_}건")

    if evaluated == 0:
        lines.append("- 승률: 표본부족 / 평가 대기")
        lines.append("- 평균손익: -")
    else:
        pnl_sign = "+" if avg_pnl > 0 else ""
        lines.append(f"- 승률: {win_rate}%")
        lines.append(f"- 평균손익: {pnl_sign}{avg_pnl}%")
        lines.append(f"- 결과: win {wins} · loss {losses} · expired {expired} · data_error {data_error}")

    lines.append(f"- 상태: previewed {previewed} · blocked {blocked} · cancelled {cancelled}")

    if consensus_anomaly > 0:
        symbols_str = " · ".join(consensus_symbols) if consensus_symbols else f"{consensus_anomaly}건"
        lines.append(f"- ⚠️ 기업행동 의심/entry_price 재확인 필요: {symbols_str}")

    lines.append("- 실주문: 비활성")
    lines.append("- 기존 포트폴리오 미합산")
    return "\n".join(lines)
