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
BLOCKED_BUY = "BLOCKED_BUY"  # 게이트 차단된 매수 (체결가능성/무효화/대량주문/충돌)

# ─── 가격/주문 게이트 임계값 ──────────────────────────────
PULLBACK_MAX_RATIO = 0.97       # 눌림목 인정: 지정가 ≤ 현재가×0.97 (현재가 대비 -3%↓)
IMMEDIATE_FILL_RATIO = 0.995    # 즉시 체결 가능: 지정가 ≥ 현재가×0.995
LARGE_ORDER_KRW = 4_000_000     # 대량주문 경고 총액
LARGE_ORDER_ASSET_PCT = 3.0     # 또는 총자산 대비 3%+

# 상단 판단이 "신규 진입 보류/이벤트 대기"임을 나타내는 표현
# → executable·즉시체결 conditional buy가 있으면 충돌(integrity error)
EVENT_WAIT_PHRASES: tuple[str, ...] = (
    "오늘 실행 없음", "오늘 실행할 것 없음", "신규 진입 보류", "신규진입 보류",
    "FOMC 대기", "확인 후 진입", "이벤트 대기", "이벤트 통과 후",
    "신규 매수 보류", "신규매수 보류",
)

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


def _detect_event_wait(raw: dict) -> str:
    """상단 판단(summary/conclusion/decision/next_action)에서 이벤트 대기/보류 표현 탐지.

    Returns: 탐지된 표현 (없으면 빈 문자열). 이 모드면 즉시 매수가 충돌.
    """
    fields = [
        str(raw.get("market_summary", "")),
        str(raw.get("strategy_summary", "")),
        str(raw.get("advisor_conclusion", "")),
        str(raw.get("advisor_oneliner", "")),
        str(raw.get("next_action", "")),
        str(raw.get("investment_decision", "")),
    ]
    text = " ".join(fields)
    return _has_phrase(text, EVENT_WAIT_PHRASES)


def _buy_price_gate(entry: float, cur: float, inval: float) -> tuple[str, str]:
    """매수 지정가의 체결가능성/눌림목/무효화 판정 (결정론적).

    Returns: (verdict, note)
      verdict ∈ {ok_pullback, immediate_fill, invalidated, no_price}
    - inval(무효화가) 이상으로 현재가가 떨어졌으면 → invalidated (지지선 이탈)
    - 지정가 ≥ 현재가×0.995 → immediate_fill (조건부 아님, 즉시 체결 가능)
    - 지정가 ≤ 현재가×0.97 → ok_pullback (진짜 눌림목)
    - 그 사이(−3%~−0.5%) → immediate_fill 경고 (눌림목이라 부르기엔 너무 가까움)
    """
    if entry <= 0 or cur <= 0:
        return "no_price", ""
    # 무효화: 현재가가 무효화가 이하로 이탈 (지지선 깨짐)
    if inval > 0 and cur <= inval:
        return "invalidated", f"현재가 {cur:,.0f} ≤ 무효화가 {inval:,.0f} 이탈 — 매수 의견 무효"
    if entry >= cur * IMMEDIATE_FILL_RATIO:
        return "immediate_fill", f"지정가 {entry:,.0f} ≥ 현재가 {cur:,.0f} 근접/초과 — 즉시 체결 가능, 조건부 아님"
    if entry <= cur * PULLBACK_MAX_RATIO:
        return "ok_pullback", ""
    # 현재가 대비 -0.5%~-3% 사이: 눌림목이라 부르기 애매 → 즉시체결로 취급
    return "immediate_fill", f"지정가 {entry:,.0f}가 현재가 {cur:,.0f}에 근접(-3% 미만) — 눌림목 아님, 재확인 필요"


def _suggest_qty(account: str, entry: float, confidence: int, is_held: bool) -> dict:
    """조건부 매수 수량/총액 산정 (예산 규칙 기반).

    예산: RIA 1차 ETF 진입 기본 50~100만, 확신도 50+ 코어 ETF 최대 100~150만,
          확신도 40 미만/타계좌 보유 중이면 30~60만. 수량=floor(예산/지정가).
    수량 0이면 shortage 플래그. AI가 shares를 명시했으면 그쪽을 우선.
    """
    if entry <= 0:
        return {}
    # 예산 결정 (보수적으로 하한 사용 — 1차 진입은 소액 분할 원칙)
    if confidence < 40 or is_held:
        budget = 600_000
    elif confidence >= 50:
        budget = 1_000_000
    else:
        budget = 800_000
    qty = int(budget // entry)
    if qty <= 0:
        return {"qty_num": 0, "budget": budget, "shortage": True}
    return {
        "qty_num": qty,
        "budget": budget,
        "order_total": qty * entry,
        "shortage": False,
    }


def _build_action(row: dict, action_type: str, side: str, briefing_type: str,
                  current_prices: dict | None = None, confidence: int = 50,
                  is_held: bool = False) -> dict:
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

    # 조건부 매수: 수량/총액 산정 (실제 주문에 바로 쓸 수 있게)
    qty_fields = {}
    if side == "buy" and action_type == CONDITIONAL_NEW_BUY:
        entry_num = _num(price)
        ai_shares = _num(row.get("shares", ""))  # AI 명시 수량 우선
        if ai_shares > 0 and entry_num > 0:
            qty_fields = {"qty_num": int(ai_shares), "order_total": int(ai_shares) * entry_num,
                          "shortage": False, "qty_source": "ai"}
        else:
            qty_fields = {**_suggest_qty(row.get("account", ""), entry_num, confidence, is_held),
                          "qty_source": "budget"}

    return {
        **gap,
        **qty_fields,
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
    total_assets: float = 0.0,
) -> dict:
    """LLM raw JSON을 결정론적으로 분류 + 주문 정합성 게이트.

    raw의 strategy_buy/strategy_sell를 읽어 신호어 + 가격 게이트로 분류한다.
    게이트(2026-06-17): 체결가능성/눌림목 표현/무효화/대량주문/상단판단 충돌.

    Returns: {executable_actions, conditional_buy_candidates, watch_only,
              cancelled_sells, blocked_buys, no_buy_reason, integrity_errors}
    """
    executable: list[dict] = []
    conditional_buy: list[dict] = []
    watch_only: list[dict] = []
    cancelled_sells: list[dict] = []
    blocked_buys: list[dict] = []
    integrity_errors: list[str] = []

    # 상단 판단이 "이벤트 대기/신규 진입 보류"인지 — 즉시 매수와 충돌
    event_wait = _detect_event_wait(raw)

    def _cur_of(tk: str) -> float:
        if not current_prices:
            return 0.0
        return current_prices.get(tk, current_prices.get(
            tk.replace(".KS", "").replace(".KQ", ""), 0.0))

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
        held = _is_held(ticker, name, holdings)
        conf = int(_num(row.get("confidence", row.get("urgency_score", "50"))) or 50)

        entry = _num(row.get("entry_price", ""))
        cur = _cur_of(ticker)
        inval = _num(row.get("invalidation_price", "")) or _num(row.get("stop_loss", ""))
        verdict, gate_note = _buy_price_gate(entry, cur, inval)

        def _mk(atype):
            a = _build_action(row, atype, "buy", briefing_type, current_prices,
                              confidence=conf, is_held=held)
            return a

        # 게이트 1: 무효화가(지지선) 이탈 → BLOCKED (지지선 이탈 후 눌림목 매수 방지)
        if verdict == "invalidated":
            a = _mk(BLOCKED_BUY)
            a["block_reason"] = gate_note
            a["blocked"] = True
            blocked_buys.append(a)
            continue

        intended_conditional = bool(blocker or is_pullback)

        # 게이트 2+3: 즉시 체결 가능인데 조건부/눌림목으로 분류 → 충돌
        if verdict == "immediate_fill":
            # 눌림목/미체결 표현 금지. 이벤트 대기 모드면 BLOCKED, 아니면 즉시매수 후보로.
            if event_wait or intended_conditional:
                a = _mk(BLOCKED_BUY)
                a["block_reason"] = (
                    f"즉시 체결 가능({gate_note}) + "
                    + (f"상단 판단 '{event_wait}' 충돌 — 주문 제외" if event_wait
                       else "조건부/눌림목 분류 부적합 — 재확인 필요")
                )
                a["blocked"] = True
                a["immediate_fill"] = True
                blocked_buys.append(a)
                integrity_errors.append(
                    f"{name or ticker}: 조건부 매수가 즉시 체결 가능"
                    + (f" (상단 '{event_wait}'와 충돌)" if event_wait else "")
                )
                continue
            # 이벤트 대기 아님 + 원래 즉시매수 의도 → executable로
            a = _mk(AI_ADD_BUY if held else AI_NEW_BUY)
            a["immediate_fill"] = True
            executable.append(a)
            continue

        # 게이트 4: 진짜 눌림목 또는 가격 미상 → 조건부 후보
        if intended_conditional or verdict == "ok_pullback":
            a = _mk(CONDITIONAL_NEW_BUY)
            a["block_reason"] = blocker or "눌림목 예약"
            # 무효화 조건 필수화
            if inval > 0:
                a["invalidation_price"] = inval
                a["invalidation_note"] = f"{inval:,.0f} 이탈 시 매수 무효"
            else:
                a["invalidation_note"] = "무효화가 미설정 — 손절/지지선 확인 필요"
            # 게이트 5: 대량주문 체크
            total = a.get("order_total", 0) or 0
            large = total >= LARGE_ORDER_KRW or (
                total_assets > 0 and total >= total_assets * LARGE_ORDER_ASSET_PCT / 100)
            if large:
                a["large_order"] = True
                if event_wait:
                    a["action_type"] = BLOCKED_BUY
                    a["blocked"] = True
                    a["block_reason"] = (
                        f"대량주문(총액 {total:,.0f}) + 이벤트 대기 '{event_wait}' — 차단")
                    blocked_buys.append(a)
                    integrity_errors.append(
                        f"{name or ticker}: 대량주문 {total:,.0f} 이벤트 대기 중 차단")
                    continue
                a["large_order_note"] = f"⚠️ 대량주문 {total:,.0f} — 분할/비중 재검토"
            conditional_buy.append(a)
            continue

        # 그 외(가격 정상 + 즉시매수 의도) → executable
        # 단 이벤트 대기 모드면 즉시매수 금지 → BLOCKED
        if event_wait:
            a = _mk(BLOCKED_BUY)
            a["block_reason"] = f"상단 판단 '{event_wait}' 중 신규 매수 — 주문 제외"
            a["blocked"] = True
            blocked_buys.append(a)
            integrity_errors.append(f"{name or ticker}: 이벤트 대기 중 즉시매수 충돌")
            continue
        executable.append(_mk(AI_ADD_BUY if held else AI_NEW_BUY))

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
            (f"상단 판단 '{event_wait}' — 신규 매수 보류" if event_wait else "")
            or str(raw.get("next_action", "")).strip()
            or str(raw.get("strategy_summary", ""))[:150].strip()
            or "매수 후보 없음 — 발굴/Watchlist에서 진입 조건 미충족"
        )

    # 최종 충돌 검사: 이벤트 대기인데 실행/조건부 매수가 남아 있으면 integrity error
    if event_wait:
        exec_buys = [a for a in executable if a["side"] == "buy"]
        if exec_buys:
            integrity_errors.append(
                f"상단 판단 '{event_wait}' 중 executable 매수 {len(exec_buys)}건 잔존 — 정합성 오류")
        imm_cond = [a for a in conditional_buy if a.get("immediate_fill")]
        if imm_cond:
            integrity_errors.append(
                f"상단 판단 '{event_wait}' 중 즉시체결 조건부매수 {len(imm_cond)}건 잔존")

    return {
        "executable_actions": executable,
        "conditional_buy_candidates": conditional_buy,
        "watch_only": watch_only,
        "cancelled_sells": cancelled_sells,
        "blocked_buys": blocked_buys,
        "no_buy_reason": no_buy_reason,
        "integrity_errors": integrity_errors,
        "event_wait": event_wait,
    }
