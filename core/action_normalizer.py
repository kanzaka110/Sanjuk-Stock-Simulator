"""
액션 정규화 — LLM raw JSON을 결정론적으로 분류

배경 (2026-06-16, Hermes 검증):
프롬프트로 LLM 행동을 통제하려는 시도가 반복 실패했다. LLM은 strategy_buy에
"추격 금지/FOMC 후/조건 미충족" 같은 비실행 reason을 담으면서 동시에 즉시 실행처럼
보이게 하거나, strategy_sell에 "매도 취소/홀딩 전환"을 담아 모순을 만든다.

해결: 프롬프트 추가가 아니라 이 모듈이 raw JSON을 **결정론적으로** 분류한다.
LLM 텍스트의 신호어를 규칙 기반으로 읽어 실행 가능 여부를 판정한다.

분류 결과 (normalize_actions 반환):
  - executable_actions:          지금 실행할 매수/매도 (AI_NEW_BUY/AI_ADD_BUY/AI_SELL_MANAGEMENT)
  - conditional_buy_candidates:  조건 충족 시 매수 (CONDITIONAL_NEW_BUY — 눌림목/대기/FOMC 후)
  - watch_only:                  관찰만 (WATCH_ONLY)
  - cancelled_sells:             매도 취소/홀딩 전환 (CANCEL_SELL/HOLD_REVIEW)
  - no_buy_reason:               실행 매수가 0건일 때 그 사유

action_type 7종 (predictions.action_type에 그대로 저장):
  AI_NEW_BUY / AI_ADD_BUY / CONDITIONAL_NEW_BUY /
  AI_SELL_MANAGEMENT / CANCEL_SELL / HOLD_REVIEW / WATCH_ONLY
"""

from __future__ import annotations

# ─── action_type 상수 ──────────────────────────────────
AI_NEW_BUY = "AI_NEW_BUY"
AI_ADD_BUY = "AI_ADD_BUY"
CONDITIONAL_NEW_BUY = "CONDITIONAL_NEW_BUY"
AI_SELL_MANAGEMENT = "AI_SELL_MANAGEMENT"
CANCEL_SELL = "CANCEL_SELL"
HOLD_REVIEW = "HOLD_REVIEW"
WATCH_ONLY = "WATCH_ONLY"

# ─── 신호어 ────────────────────────────────────────────
# 매수 reason/execution_condition/timing에 있으면 즉시매수 금지 → 조건부로 분류
BUY_NOT_NOW_PHRASES: tuple[str, ...] = (
    "추격 금지", "추격금지", "대기", "조건 미충족", "조건미충족",
    "FOMC 후", "FOMC후", "눌림목", "현재 진입 조건 미충족",
    "즉시 진입은 부적절", "즉시진입 부적절", "검토",
)

# 매도 reason에 있으면 실행 매도가 아님 → 취소/홀딩으로 분류
SELL_CANCEL_PHRASES: tuple[str, ...] = (
    "매도 취소", "매도취소", "홀딩 전환", "홀딩전환", "매도 보류", "매도보류",
    "홀딩 유지", "홀딩유지", "무효화 조건 충족", "무효화조건 충족",
    "전량 매도 부적절", "전량매도 부적절", "잔여 보유", "잔여보유",
)


def _row_text(row: dict, *fields: str) -> str:
    """row의 지정 필드들을 합쳐 소문자 무시 검색용 텍스트로."""
    parts = []
    for f in fields:
        v = row.get(f, "")
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.append(" ".join(str(x) for x in v))
    return " ".join(parts)


def _has_phrase(text: str, phrases: tuple[str, ...]) -> str:
    """text에 포함된 첫 신호어 반환 (없으면 빈 문자열)."""
    for p in phrases:
        if p in text:
            return p
    return ""


def _is_held(ticker: str, name: str, holdings: dict | None) -> bool:
    if not holdings:
        return False
    if ticker and ticker in holdings:
        return True
    # 이름 매칭 (보유 dict가 {ticker: {...}} 형태일 때 name으로도 점검)
    return False


def _num(val) -> float:
    """가격 문자열에서 숫자 추출. '₩138,000 이하' → 138000.0, 실패 시 0."""
    if isinstance(val, (int, float)):
        return float(val)
    import re
    s = str(val).replace(",", "")
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    return float(nums[0]) if nums else 0.0


def _price_gap_fields(entry: float, cur: float) -> dict:
    """예약가-현재가 괴리율 + 안내 단계 산출.

    단계: pullback(현재가보다 낮음·미체결 가능) / chase(같거나 높음·추격 재검토) /
          wide(괴리 3%+·데이터 확인). cur/entry 없으면 빈 dict.
    """
    if entry <= 0 or cur <= 0:
        return {}
    gap_pct = (entry - cur) / cur * 100
    abs_gap = abs(gap_pct)
    if abs_gap >= 3.0:
        stage = "wide"
        note = f"가격 괴리 큼({gap_pct:+.1f}%) — 데이터/가격 단위 확인 필요"
    elif gap_pct < 0:
        stage = "pullback"
        note = f"현재가 대비 {gap_pct:.1f}% 눌림목 지정가 — 미체결 가능"
    else:
        stage = "chase"
        note = f"현재가 대비 {gap_pct:+.1f}% — 즉시 체결 가능성 높음, 추격매수 여부 재검토"
    return {
        "current_price_num": cur,
        "entry_price_num": entry,
        "gap_pct": round(gap_pct, 1),
        "gap_stage": stage,
        "gap_note": note,
    }


def _build_action(row: dict, action_type: str, side: str, briefing_type: str,
                  current_prices: dict | None = None) -> dict:
    """정규화된 액션 dict 생성 (텔레그램 렌더 + 메모리 저장 공용)."""
    is_night = briefing_type in ("KR_NIGHT", "US_NIGHT")
    if side == "buy":
        if action_type == CONDITIONAL_NEW_BUY:
            disp_type = "예약매수"
        else:
            disp_type = "예약매수" if is_night else "매수·즉시"
        price = row.get("entry_price", "")
        target = row.get("target_price", "")
    else:
        disp_type = "예약매도" if is_night else "매도·즉시"
        price = row.get("current_price", "") or row.get("take_profit", "")
        target = row.get("take_profit", "")

    # 매수 액션: 현재가 대비 예약가 괴리율 안내 (가격 오류 오인 방지)
    gap = {}
    if side == "buy" and current_prices:
        tk = row.get("ticker", "")
        cur = current_prices.get(tk, current_prices.get(tk.replace(".KS", "").replace(".KQ", ""), 0))
        gap = _price_gap_fields(_num(price), _num(cur))

    return {
        **gap,
        "action_type": action_type,
        "type": disp_type,
        "side": side,
        "account": row.get("account", ""),
        "ticker": row.get("ticker", ""),
        "name": row.get("name", ""),
        "horizon": row.get("horizon", ""),
        "order_method": "지정가",
        "price": price,
        "qty": row.get("shares", ""),
        "validity": ("예약" if is_night else "당일"),
        "target": target,
        "stop": row.get("stop_loss", ""),
        "cancel_if": row.get("invalidation_condition", ""),
        "long_term_plan": row.get("long_term_plan", ""),
        "reason": row.get("reason", ""),
        "strategy_type": row.get("strategy_type", ""),
        "_raw": row,  # 저장 단계가 원본 상세 필드(horizon_days/risk_reward 등) 접근용
    }


def classify_row(signal: str, reason: str, strategy_type: str = "",
                 is_held: bool = False) -> str:
    """단일 row를 action_type으로 분류 (마이그레이션/감사 공용 — 결정론적).

    normalize_actions와 동일한 신호어 규칙. raw row의 signal+reason만으로 판정.
    """
    text = reason or ""
    if signal == "매수":
        if _has_phrase(text, BUY_NOT_NOW_PHRASES) or strategy_type == "신규진입" or "눌림목" in text:
            return CONDITIONAL_NEW_BUY
        return AI_ADD_BUY if is_held else AI_NEW_BUY
    if signal == "매도":
        canceller = _has_phrase(text, SELL_CANCEL_PHRASES)
        if canceller:
            return HOLD_REVIEW if ("홀딩" in canceller or "보유" in canceller) else CANCEL_SELL
        return AI_SELL_MANAGEMENT
    return WATCH_ONLY


def normalize_actions(
    raw: dict,
    briefing_type: str,
    current_prices: dict | None = None,
    holdings: dict | None = None,
) -> dict:
    """LLM raw JSON을 결정론적으로 분류.

    raw의 strategy_buy/strategy_sell를 읽어 신호어 기반으로 실행/조건부/취소를 가른다.
    raw의 actions 필드는 신뢰하지 않는다 (LLM이 모순되게 채우므로 strategy_*에서 재생성).

    Returns: {executable_actions, conditional_buy_candidates, watch_only,
              cancelled_sells, no_buy_reason}
    """
    executable: list[dict] = []
    conditional_buy: list[dict] = []
    watch_only: list[dict] = []
    cancelled_sells: list[dict] = []

    # ── 매수 분류 ──
    for row in raw.get("strategy_buy", []) or []:
        if not row.get("ticker") and not row.get("name"):
            continue
        text = _row_text(row, "reason", "execution_condition", "timing")
        strat = str(row.get("strategy_type", ""))
        ticker = row.get("ticker", "")
        name = row.get("name", "")

        blocker = _has_phrase(text, BUY_NOT_NOW_PHRASES)
        is_pullback = strat == "신규진입" or "눌림목" in text

        if blocker or is_pullback:
            # 조건 미충족/눌림목 → 조건부 후보 (즉시 실행 금지)
            act = _build_action(row, CONDITIONAL_NEW_BUY, "buy", briefing_type, current_prices)
            act["block_reason"] = blocker or "눌림목 예약"
            conditional_buy.append(act)
        else:
            atype = AI_ADD_BUY if _is_held(ticker, name, holdings) else AI_NEW_BUY
            executable.append(_build_action(row, atype, "buy", briefing_type, current_prices))

    # ── 매도 분류 ──
    for row in raw.get("strategy_sell", []) or []:
        if not row.get("ticker") and not row.get("name"):
            continue
        text = _row_text(row, "reason", "execution_condition", "timing")
        canceller = _has_phrase(text, SELL_CANCEL_PHRASES)

        if canceller:
            # "홀딩 전환/매도 취소" → 실행 매도 아님
            atype = HOLD_REVIEW if ("홀딩" in canceller or "보유" in canceller) else CANCEL_SELL
            act = _build_action(row, atype, "sell", briefing_type)
            act["cancel_reason"] = canceller
            cancelled_sells.append(act)
        else:
            executable.append(_build_action(row, AI_SELL_MANAGEMENT, "sell", briefing_type))

    # ── no_buy_reason: 실행 매수도 조건부 매수도 없을 때 ──
    has_any_buy = any(a["side"] == "buy" for a in executable) or bool(conditional_buy)
    no_buy_reason = ""
    if not has_any_buy:
        no_buy_reason = (
            str(raw.get("next_action", "")).strip()
            or str(raw.get("strategy_summary", ""))[:150].strip()
            or "매수 후보 없음 — 발굴/Watchlist에서 진입 조건 미충족"
        )

    return {
        "executable_actions": executable,
        "conditional_buy_candidates": conditional_buy,
        "watch_only": watch_only,
        "cancelled_sells": cancelled_sells,
        "no_buy_reason": no_buy_reason,
    }
