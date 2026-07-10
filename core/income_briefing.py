"""core/income_briefing.py

정규 브리핑의 수입 중심(income-first) 결정론적 payload.

[목적]
LLM synthesis가 전원 실패해도 계좌·수입·보유관리·Toss 자동운영 상태는
결정론적으로 브리핑에 출력한다. 브리핑의 최우선 KPI는 매수/매도 신호
개수가 아니라 실제 수입/P&L 관리다.

[계좌 분리 — 절대 규칙]
- Toss AI: 제한형 완전자율 실계좌. 자동주문 여부는 기존 Toss pipeline/gate만
  결정한다. 이 모듈은 상태·후보·실행 결과를 보고만 하고 주문을 만들지 않는다.
- 삼성증권(일반/RIA/ISA/IRP/연금저축): 분석 + 수동 주문표만.
  모든 티켓 manual_only=true / auto_execution=false. 자동화 없음.

[수입 표기 — 절대 규칙]
- 실현수입 데이터가 없으면 반드시 None + '산출불가'. 0으로 대체 금지.
- 오늘 평가변동(unrealized)을 실현수입처럼 표시 금지.
- 예상수입은 '예상 — 실제 수입 아님'으로만 표기.

read-only: 주문/전송/파일쓰기 부작용 없음. 토큰·계좌번호·broker order id·
raw API 응답은 payload에 넣지 않는다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

VERSION = "income_briefing_v1"
_SAMSUNG_STALE_HOURS = 24
_MAX_MANUAL_BUY_TICKETS = 3
_MAX_READY_BUYS = 3
_THESIS_EXPIRY_WARN_DAYS = 30

# 수입형 manual ticket 허용 계좌 (RIA/IRP/연금저축은 장기 계좌 — 회전매매 금지)
_INCOME_TICKET_ACCOUNTS = {"일반", "ISA"}
_LONG_TERM_ACCOUNTS = {"RIA", "IRP", "연금저축"}

# 삼성 position management 결정론 규칙
_PM_LOSS_PCT = -8.0
_PM_DAY_MOVE_PCT = 5.0
_PM_CONCENTRATION_PCT = 15.0
_PM_PROFIT_PROTECT_PCT = 15.0


def _num(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _now_kst() -> datetime:
    return datetime.now(KST)


def _clean_account(raw: str) -> str:
    return str(raw or "").strip().strip("[]").strip()


def _is_kr_symbol(symbol: str) -> bool:
    s = str(symbol or "").upper().strip()
    return s.endswith((".KS", ".KQ")) or (s.isdigit() and len(s) == 6)


# ─── 1. payload 수집 ─────────────────────────────────────────────

def build_income_briefing_context(briefing_type: str) -> dict:
    """계좌/수입/보유관리/Toss 자동운영 결정론 payload. 예외는 warnings로."""
    now = _now_kst()
    warnings: list[str] = []
    sources: dict[str, str] = {}

    payload: dict = {
        "version": VERSION,
        "briefing_type": str(briefing_type or "MANUAL"),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "income_kpi": {"toss": _empty_toss_kpi(), "samsung": _empty_samsung_kpi()},
        "toss": {
            "automation_mode": "unknown",
            "holdings_count": 0,
            "ready_buys": [],
            "rebalance": {
                "portfolio_rebalance_required": False,
                "funding_rebalance_required": False,
                "funding_currency": None,
                "funding_target": None,
                "expected_release_krw": None,
                "sell_to_fund_candidates": [],
                "zero_reason": "",
            },
            "recent_orders": [],
            "block_reasons": [],
        },
        "samsung": {
            "manual_only": True,
            "auto_execution": False,
            "accounts": [],
            "manual_income_tickets": [],
            "position_management": [],
            "blocked_tickets": [],
        },
        "thesis": {"valid": [], "expired": [], "invalid": [], "expiring_within_30d": []},
        "quality": {"warnings": warnings, "sources": sources},
    }

    # ── Toss 계좌 KPI ──
    toss_summary: dict = {}
    try:
        from core.dashboard_data import toss_account_summary
        toss_summary = toss_account_summary() or {}
        sources["toss_account_summary"] = "error" if toss_summary.get("error") else "ok"
        payload["income_kpi"]["toss"] = _toss_kpi(toss_summary)
        payload["toss"]["holdings_count"] = int(_num(toss_summary.get("holdings_count")))
    except Exception as e:
        warnings.append(f"Toss 계좌 조회 실패: {str(e)[:80]}")
        sources["toss_account_summary"] = "error"

    # ── Toss 자동운영 모드 ──
    try:
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        policy = compute_toss_live_pilot_policy() or {}
        payload["toss"]["automation_mode"] = (
            "autonomous_live_pilot" if policy.get("autonomous_mode") else "monitored_readonly"
        )
        payload["toss"]["kill_switch"] = bool(policy.get("autonomous_kill_switch"))
        sources["toss_policy"] = "ok"
    except Exception as e:
        warnings.append(f"Toss 정책 조회 실패: {str(e)[:80]}")
        sources["toss_policy"] = "error"

    # ── Toss 후보/차단 ──
    try:
        from core.dashboard_data import toss_buy_candidates_data
        buys = toss_buy_candidates_data(range_="today", market="ALL", limit=80) or {}
        sources["toss_buy_candidates"] = "ok"
        ready, blocks = _toss_ready_and_blocks(buys)
        payload["toss"]["ready_buys"] = ready
        payload["toss"]["block_reasons"] = blocks
    except Exception as e:
        warnings.append(f"Toss 매수후보 조회 실패: {str(e)[:80]}")
        sources["toss_buy_candidates"] = "error"

    # ── Toss 리밸런싱/funding ──
    try:
        from core.dashboard_data import toss_rebalance_plan_data
        plan = toss_rebalance_plan_data(limit=80, market="ALL") or {}
        sources["toss_rebalance_plan"] = "error" if plan.get("error") else "ok"
        payload["toss"]["rebalance"] = _toss_rebalance(plan)
    except Exception as e:
        warnings.append(f"Toss 리밸런싱 계획 실패: {str(e)[:80]}")
        sources["toss_rebalance_plan"] = "error"

    # ── Toss 최근 자동주문 결과 ──
    try:
        from core.dashboard_data import toss_live_pilot_events_data
        events = toss_live_pilot_events_data(limit=30) or {}
        sources["toss_live_pilot_events"] = "ok"
        payload["toss"]["recent_orders"] = _toss_recent_orders(events)
    except Exception as e:
        warnings.append(f"Toss 주문 이벤트 조회 실패: {str(e)[:80]}")
        sources["toss_live_pilot_events"] = "error"

    # ── 삼성 KPI + 계좌 + position management ──
    try:
        from core.dashboard_data import portfolio_data
        pf = portfolio_data() or {}
        sources["portfolio"] = "ok"
        kpi = _samsung_kpi(pf, now)
        payload["income_kpi"]["samsung"] = kpi
        payload["samsung"]["accounts"] = _samsung_accounts(pf)
        payload["samsung"]["position_management"] = _samsung_position_management(pf)
        if kpi["data_status"] == "stale":
            warnings.append(
                f"삼성 보유 기준일 {kpi.get('holdings_as_of')} — stale, 수동 주문표 생성 중단")
    except Exception as e:
        warnings.append(f"삼성 포트폴리오 조회 실패: {str(e)[:80]}")
        sources["portfolio"] = "error"
        payload["income_kpi"]["samsung"]["data_status"] = "error"

    # ── AI Berkshire thesis ──
    try:
        payload["thesis"] = _thesis_section(now)
        sources["ai_berkshire"] = "ok"
    except Exception as e:
        warnings.append(f"AI Berkshire 논지 조회 실패: {str(e)[:80]}")
        sources["ai_berkshire"] = "error"

    return payload


def _empty_toss_kpi() -> dict:
    return {
        "realized_income_krw": None,
        "realized_income_status": "unavailable",
        "today_unrealized_krw": None,
        "total_unrealized_krw": None,
        "cash_krw": None,
        "cash_usd": None,
        "total_account_value_krw": None,
        "data_status": "error",
    }


def _empty_samsung_kpi() -> dict:
    return {
        "realized_income_krw": None,
        "realized_income_status": "unavailable",
        "today_unrealized_krw": None,
        "total_unrealized_krw": None,
        "cash_krw": None,
        "total_asset_krw": None,
        "holdings_as_of": None,
        "data_status": "error",
    }


def _toss_kpi(summary: dict) -> dict:
    realized = (summary.get("realized_profit_loss") or {}).get("krw")
    return {
        # 실현수입 원천 없음 — None 유지, 0으로 바꾸지 않는다
        "realized_income_krw": realized,
        "realized_income_status": "available" if realized is not None else "unavailable",
        "today_unrealized_krw": (summary.get("today_profit_loss") or {}).get("krw"),
        "total_unrealized_krw": (summary.get("profit_loss") or {}).get("krw"),
        "cash_krw": (summary.get("cash") or {}).get("krw_native"),
        "cash_usd": (summary.get("cash") or {}).get("usd"),
        "total_account_value_krw": (summary.get("total_account_value") or {}).get("krw"),
        "data_status": "error" if summary.get("error") else "live",
    }


def _samsung_kpi(pf: dict, now: datetime) -> dict:
    holdings_as_of = str(pf.get("holdings_as_of") or "") or None
    stale = True
    if holdings_as_of:
        try:
            as_of_dt = datetime.fromisoformat(holdings_as_of[:10]).replace(tzinfo=KST)
            stale = (now - as_of_dt) > timedelta(hours=_SAMSUNG_STALE_HOURS)
        except ValueError:
            stale = True
    total_unrealized = sum(_num(a.get("pnl_krw")) for a in pf.get("accounts") or [])
    return {
        "realized_income_krw": None,   # 실현손익 원천 현재 산출 불가
        "realized_income_status": "unavailable",
        "today_unrealized_krw": pf.get("today_pnl_krw"),  # 오늘 평가변동 (실현 아님)
        "total_unrealized_krw": round(total_unrealized),
        "cash_krw": pf.get("total_cash"),
        "total_asset_krw": pf.get("total_asset"),
        "holdings_as_of": holdings_as_of,
        "data_status": "stale" if stale else "live",
    }


def _samsung_accounts(pf: dict) -> list[dict]:
    out = []
    for a in pf.get("accounts") or []:
        out.append({
            "account": a.get("name", ""),
            "asset_total_krw": a.get("asset_total"),
            "cash_krw": a.get("cash"),
            "pnl_krw": a.get("pnl_krw"),
            "pnl_pct": a.get("pnl_pct"),
            "today_pnl_krw": a.get("today_pnl_krw"),
            "manual_only": True,
            "auto_execution": False,
        })
    return out


def _samsung_position_management(pf: dict) -> list[dict]:
    """결정론 보유관리 목록 — 매도 주문이 아니라 수동 점검 대상."""
    total_asset = _num(pf.get("total_asset"))
    out: list[dict] = []
    for acct in pf.get("accounts") or []:
        acct_name = acct.get("name", "")
        long_term = acct_name in _LONG_TERM_ACCOUNTS
        for it in acct.get("items") or []:
            pnl_pct = _num(it.get("pnl_pct"))
            day_pct = it.get("day_pct")
            weight = (_num(it.get("eval_krw")) / total_asset * 100) if total_asset else 0.0
            status = None
            reason = ""
            if pnl_pct <= _PM_LOSS_PCT:
                status, reason = "risk_review", f"손실률 {pnl_pct:+.1f}% — 손절/논지 점검 필요"
            elif day_pct is not None and abs(_num(day_pct)) >= _PM_DAY_MOVE_PCT:
                status, reason = "risk_review", f"당일 {_num(day_pct):+.1f}% 급변 — 원인 확인"
            elif weight >= _PM_CONCENTRATION_PCT:
                status, reason = "concentration", f"전체자산 비중 {weight:.1f}% — 집중도 점검"
            elif pnl_pct >= _PM_PROFIT_PROTECT_PCT and not long_term \
                    and not str(it.get("horizon") or "").startswith("장기"):
                status, reason = "profit_protection", f"평가수익 {pnl_pct:+.1f}% — 이익보호 검토"
            elif it.get("price_source") == "missing_quote" or it.get("day_pct_source") == "missing_quote":
                status, reason = "risk_review", "시세 결측 — 데이터 점검"
            if status:
                out.append({
                    "account": acct_name,
                    "symbol": it.get("ticker", ""),
                    "name": it.get("name", ""),
                    "status": status,
                    "pnl_pct": round(pnl_pct, 2),
                    "weight_pct": round(weight, 1),
                    "manual_only": True,
                    "reason": reason,
                })
    return out


def _toss_ready_and_blocks(buys: dict) -> tuple[list[dict], list[dict]]:
    items = buys.get("items") or []
    ready = []
    for it in items:
        income = it.get("income_strategy") or {}
        if not (it.get("stock_agent_ready") and income.get("income_pass")):
            continue
        ready.append({
            "symbol": it.get("symbol") or it.get("ticker"),
            "name": it.get("name"),
            "current_price": it.get("price"),
            "limit_price": it.get("limit_price"),
            "quantity": it.get("quantity"),
            "estimated_amount_krw": it.get("estimated_amount_krw"),
            "expected_pnl_krw": income.get("expected_pnl_krw"),
            "income_edge_ratio": income.get("income_edge_ratio"),
            "risk_reward": it.get("risk_reward"),
            "target_price": it.get("target_price"),
            "stop_loss": it.get("stop_loss"),
            "execution_status": it.get("execution_status"),
            "automation": "toss_autonomous",
        })
        if len(ready) >= _MAX_READY_BUYS:
            break

    counts: dict[str, int] = {}
    for it in items:
        if it.get("stock_agent_ready"):
            continue
        key = str(it.get("execution_status") or it.get("block_reason") or "unknown")[:60]
        counts[key] = counts.get(key, 0) + 1
    blocks = [{"reason": k, "count": v}
              for k, v in sorted(counts.items(), key=lambda kv: -kv[1])][:8]
    return ready, blocks


def _toss_rebalance(plan: dict) -> dict:
    rows = []
    for r in plan.get("sell_to_fund_candidates") or []:
        ab = r.get("ai_berkshire") or {}
        rows.append({
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "quantity": r.get("quantity"),
            "estimated_release_krw": r.get("estimated_release_krw"),
            "auto_sell_eligible": r.get("auto_sell_eligible"),
            "classification": ab.get("classification"),
            "block_reason": r.get("auto_sell_block_reason"),
            "funding_target_symbol": r.get("funding_target_symbol"),
        })
    portfolio_req = bool(plan.get("portfolio_rebalance_required"))
    funding_req = bool(plan.get("funding_rebalance_required"))
    zero_reason = ""
    if not rows:
        if not portfolio_req and not funding_req:
            zero_reason = (
                f"보유 {plan.get('holdings_count', '?')}개 ≤ 기준 — 리밸런싱 불필요, "
                "funding도 '매도 후 전액 매수 가능' 후보 없음")
        else:
            zero_reason = "발동 조건 충족했으나 eligible 매도 후보 없음 (AI Berkshire 차단 포함)"
    return {
        "portfolio_rebalance_required": portfolio_req,
        "funding_rebalance_required": funding_req,
        "funding_currency": plan.get("funding_currency"),
        "funding_target": plan.get("funding_target"),
        "expected_release_krw": sum(
            _num(r.get("estimated_release_krw")) for r in rows
            if r.get("auto_sell_eligible")) or None,
        "sell_to_fund_candidates": rows,
        "zero_reason": zero_reason,
    }


def _toss_recent_orders(events: dict) -> list[dict]:
    """최근 자동주문 결과 — 민감 필드(broker id/계좌/토큰) 제외 whitelist 복사."""
    out = []
    for r in (events.get("records") or [])[:5]:
        out.append({
            "symbol": r.get("symbol"),
            "side": r.get("side"),
            "status": r.get("status"),
            "reason": str(r.get("reason") or "")[:60],
            "created_at": r.get("created_at"),
        })
    return out


def _thesis_section(now: datetime) -> dict:
    from core.ai_berkshire_toss import load_ai_berkshire_scores, score_for_symbol
    data = load_ai_berkshire_scores()
    section: dict = {"valid": [], "expired": [], "invalid": [], "expiring_within_30d": []}
    today = now.date()
    for sym in (data.get("items") or {}):
        item = score_for_symbol(sym, data, as_of_date=today)
        if item is None:
            continue
        row = {
            "symbol": sym,
            "name": item.get("name"),
            "stored_classification": item.get("stored_classification"),
            "classification": item.get("classification"),
            "as_of": item.get("as_of"),
            "valid_until": item.get("valid_until"),
            "freshness_valid": item.get("freshness_valid"),
            "freshness_issues": item.get("freshness_issues"),
            "thesis": item.get("thesis"),
            "red_lines": item.get("red_lines"),
        }
        if item.get("thesis_expired"):
            section["expired"].append(row)
        elif not item.get("freshness_valid"):
            section["invalid"].append(row)
        else:
            section["valid"].append(row)
            try:
                until = datetime.fromisoformat(str(item.get("valid_until"))).date()
                if (until - today).days <= _THESIS_EXPIRY_WARN_DAYS:
                    section["expiring_within_30d"].append(
                        {"symbol": sym, "valid_until": item.get("valid_until")})
            except (TypeError, ValueError):
                pass
    return section


# ─── 2. LLM 프롬프트용 컨텍스트 ──────────────────────────────────

def render_income_context_for_prompt(payload: dict) -> str:
    """synthesis 프롬프트에 넣을 구조화 수입 컨텍스트 (재계산·창작 금지 지시 포함)."""
    kpi = payload.get("income_kpi") or {}
    toss_kpi = kpi.get("toss") or {}
    ss_kpi = kpi.get("samsung") or {}
    toss = payload.get("toss") or {}
    reb = toss.get("rebalance") or {}
    lines = [
        "━━━ 💰 수입 계기판 (구조화 — 이 수치를 재계산/창작하지 말 것) ━━━",
        f"[Toss AI — 자동운영({toss.get('automation_mode')})] "
        f"총액 {_won(toss_kpi.get('total_account_value_krw'))} / 보유 {toss.get('holdings_count')}개 / "
        f"KRW현금 {_won(toss_kpi.get('cash_krw'))} / USD현금 ${_num(toss_kpi.get('cash_usd')):,.2f}",
        f"  실현수입: {_realized_text(toss_kpi)} / 오늘 평가변동: {_won(toss_kpi.get('today_unrealized_krw'))} "
        f"/ 누적 평가손익: {_won(toss_kpi.get('total_unrealized_krw'))}",
        f"  리밸런싱 portfolio={reb.get('portfolio_rebalance_required')} "
        f"funding={reb.get('funding_rebalance_required')} sell_to_fund={len(reb.get('sell_to_fund_candidates') or [])}건",
        f"  자동 매수 준비(ready) {len(toss.get('ready_buys') or [])}건 — Toss 주문은 자동 파이프라인 전담, LLM이 주문표 생성 금지",
        f"[삼성증권 — 수동 전용] 총자산 {_won(ss_kpi.get('total_asset_krw'))} / 현금 {_won(ss_kpi.get('cash_krw'))} "
        f"/ 보유기준일 {ss_kpi.get('holdings_as_of')} ({ss_kpi.get('data_status')})",
        f"  실현수입: {_realized_text(ss_kpi)} / 오늘 평가변동: {_won(ss_kpi.get('today_unrealized_krw'))} (실현수입 아님)",
        "규칙: ①실제 수입/P&L 관리가 목적 ②보유관리·현금·리밸런싱을 신규매수보다 먼저 "
        "③실현/평가/예상 혼합 금지 ④양수 기대값 후보만 ⑤BUY는 exit plan+thesis 필수 "
        "⑥Toss 자동/삼성 수동 완전 분리 ⑦위 수치 재계산 금지",
    ]
    if ss_kpi.get("data_status") == "stale":
        lines.append("  ⚠ 삼성 보유 stale — 삼성 신규 수동 주문표 생성 금지, 보유관리 점검만")
    return "\n".join(lines)


def _won(v) -> str:
    if v is None:
        return "산출불가"
    return f"{_num(v):+,.0f}원" if _num(v) < 0 else f"{_num(v):,.0f}원"


def _realized_text(kpi: dict) -> str:
    if kpi.get("realized_income_krw") is None:
        return "산출불가"
    return _won(kpi.get("realized_income_krw"))


# ─── 3. normalized 후처리 ────────────────────────────────────────

_NORMALIZED_LIST_KEYS = (
    "executable_actions", "conditional_buy_candidates", "conditional_sell_candidates",
    "watch_only", "cancelled_sells", "blocked_buys",
)


def _is_toss_action(action: dict) -> bool:
    acct = str(action.get("account") or action.get("account_type") or "")
    return "토스" in acct or "TOSS" in acct.upper()


def strip_toss_from_manual_normalized(normalized: dict | None) -> dict | None:
    """LLM normalized action에서 Toss 계좌 action 제거.

    Toss 주문은 자동 파이프라인 전담 — LLM이 만든 Toss 주문표가 수동
    실행 섹션에 남으면 이중 주문 위험이 있다. 원본은 변경하지 않는다.
    """
    if not isinstance(normalized, dict):
        return normalized
    out = dict(normalized)
    removed = 0
    for key in _NORMALIZED_LIST_KEYS:
        rows = normalized.get(key)
        if not isinstance(rows, list):
            continue
        kept = [r for r in rows if not (isinstance(r, dict) and _is_toss_action(r))]
        removed += len(rows) - len(kept)
        out[key] = kept
    if removed:
        out["toss_actions_stripped"] = removed
    return out


# ─── 4. finalize — 삼성 manual ticket ────────────────────────────

def finalize_income_briefing(
    payload: dict,
    normalized: dict | None,
    briefing_type: str,
) -> dict:
    """normalized(LLM) 삼성 action → manual income ticket 심사.

    LLM 실패(normalized=None)면 티켓 0 — fallback에서 가짜 후보를 만들지 않는다.
    """
    out = dict(payload or {})
    samsung = dict(out.get("samsung") or {})
    samsung.setdefault("manual_only", True)
    samsung.setdefault("auto_execution", False)
    tickets: list[dict] = []
    blocked: list[dict] = []
    position_mgmt = list(samsung.get("position_management") or [])

    ss_kpi = (out.get("income_kpi") or {}).get("samsung") or {}
    stale = ss_kpi.get("data_status") != "live"
    is_daily_review = str(briefing_type or "").upper() == "US_CLOSE"

    if normalized and not is_daily_review:
        held = _samsung_held_symbols(samsung)
        candidates = _collect_samsung_actions(normalized)
        for act in candidates:
            verdict, reason, ticket = _judge_manual_ticket(act, held, stale)
            if ticket and verdict in ("PASS", "HOLD"):
                if ticket["side"] == "BUY" and (
                        sum(1 for t in tickets if t["side"] == "BUY") >= _MAX_MANUAL_BUY_TICKETS):
                    blocked.append(_blocked_row(act, "BUY 티켓 상한(3) 초과"))
                else:
                    tickets.append(ticket)
            elif verdict == "POSITION_MGMT":
                position_mgmt.append({
                    "account": _clean_account(act.get("account", "")),
                    "symbol": act.get("ticker") or act.get("symbol") or "",
                    "name": act.get("name") or "",
                    "status": "thesis_review",
                    "pnl_pct": None,
                    "weight_pct": None,
                    "manual_only": True,
                    "reason": reason,
                })
            else:
                blocked.append(_blocked_row(act, reason))

    samsung["manual_income_tickets"] = tickets
    samsung["blocked_tickets"] = blocked
    samsung["position_management"] = position_mgmt
    out["samsung"] = samsung
    out["daily_review"] = is_daily_review
    return out


def _samsung_held_symbols(samsung: dict) -> set[str]:
    held: set[str] = set()
    try:
        from config.settings import (
            HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION,
        )
        for hh in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
            held.update(str(k).upper() for k in hh)
    except Exception:
        pass
    for row in samsung.get("position_management") or []:
        if row.get("symbol"):
            held.add(str(row["symbol"]).upper())
    return held


def _collect_samsung_actions(normalized: dict) -> list[dict]:
    out = []
    for key in ("executable_actions", "conditional_buy_candidates", "conditional_sell_candidates"):
        for row in normalized.get(key) or []:
            if isinstance(row, dict) and not _is_toss_action(row):
                out.append(row)
    return out


def _action_field(act: dict, *keys, default=None):
    for k in keys:
        if act.get(k) not in (None, "", 0):
            return act.get(k)
    return default


def _judge_manual_ticket(act: dict, held: set[str], stale: bool):
    """삼성 action 1건 → (verdict, reason, ticket|None)."""
    account = _clean_account(str(act.get("account") or ""))
    symbol = str(act.get("ticker") or act.get("symbol") or "").upper().strip()
    side = str(act.get("side") or act.get("action_type") or "").upper()
    if "SELL" in side or "매도" in side:
        side = "SELL"
    else:
        side = "BUY"

    if stale:
        return "BLOCK", "삼성 보유 stale — 수동 주문표 생성 중단", None
    if not symbol or not account:
        return "BLOCK", "필수 필드 누락 (계좌/종목)", None
    if account in _LONG_TERM_ACCOUNTS and side == "BUY":
        return "BLOCK", f"{account}는 장기 계좌 — 수입형 회전매매 금지", None
    if account == "ISA" and not _is_kr_symbol(symbol):
        return "BLOCK", "ISA는 국내 종목/국내상장 ETF만 매수 가능", None
    if side == "BUY" and symbol in held:
        return "POSITION_MGMT", "이미 보유 중 — 신규 BUY가 아니라 보유관리 대상", None

    # AI Berkshire 게이트
    berkshire_note = ""
    verdict = "PASS"
    try:
        from core.ai_berkshire_toss import score_for_symbol
        item = score_for_symbol(symbol)
        if item is None:
            berkshire_note = "needs_research: AI Berkshire 미분류"
        elif side == "BUY" and item.get("stored_classification") == "avoid":
            return "BLOCK", "AI Berkshire avoid — 신규 BUY 차단", None
        elif item.get("thesis_expired") or not item.get("freshness_valid"):
            verdict = "HOLD"
            berkshire_note = "AI Berkshire 논지 만료/불량 — 재리서치 전 HOLD"
    except Exception:
        berkshire_note = "needs_research: AI Berkshire 조회 실패"

    price = _num(_action_field(act, "limit_price", "entry_price", "price", "진입가"))
    quantity = int(_num(_action_field(act, "quantity", "수량", default=0)))
    target = _num(_action_field(act, "target_price", "목표가"))
    stop = _num(_action_field(act, "stop_loss", "손절가"))
    current = _num(_action_field(act, "current_price", "현재가"), price)

    ticket = {
        "account": account,
        "symbol": symbol,
        "name": act.get("name") or symbol,
        "side": side,
        "current_price": current or None,
        "limit_price": price or None,
        "quantity": quantity or None,
        "estimated_amount_krw": None,
        "expected_pnl_krw": None,
        "income_edge_ratio": None,
        "risk_reward": _num(act.get("risk_reward")) or None,
        "target_price": target or None,
        "stop_loss": stop or None,
        "manual_only": True,
        "auto_execution": False,
        "verdict": verdict,
        "reason": berkshire_note or "",
    }

    if side == "BUY":
        if not (price and quantity and target and stop):
            return "BLOCK", "필수 필드 누락 (지정가/수량/목표/손절)", None
        try:
            from core.toss_income_strategy import prepare_income_buy_plan, compute_income_edge
            cand = prepare_income_buy_plan({
                "symbol": symbol, "side": "buy", "quantity": quantity,
                "limit_price": price, "target_price": target, "stop_loss": stop,
                "risk_reward": _num(act.get("risk_reward")),
                "score": _num(act.get("score"), 65.0),
                "market": "KR" if _is_kr_symbol(symbol) else "US",
            })
            edge = compute_income_edge(cand)
            ticket["estimated_amount_krw"] = edge.get("estimated_amount_krw")
            ticket["expected_pnl_krw"] = edge.get("expected_pnl_krw")
            ticket["income_edge_ratio"] = edge.get("income_edge_ratio")
            ticket["risk_reward"] = edge.get("risk_reward") or ticket["risk_reward"]
            ticket["target_price"] = cand.get("target_price") or target
            ticket["stop_loss"] = cand.get("stop_loss") or stop
            if not edge.get("income_pass"):
                return "BLOCK", f"income gate 미달: {edge.get('income_block_label') or edge.get('income_block_reason')}", None
        except Exception as e:
            return "BLOCK", f"income 계산 실패: {str(e)[:60]}", None
    else:
        # SELL — 보유 수량/평단을 알 수 있을 때만 '예상' 실현손익
        est = _estimate_sell_realized(symbol, quantity, price)
        if est is None:
            return "BLOCK", "SELL은 보유 수량/평단 확인 가능할 때만 티켓 생성", None
        ticket["expected_pnl_krw"] = est   # '예상' — 실제 수입에 합산 금지
        ticket["reason"] = (ticket["reason"] + " " if ticket["reason"] else "") + "예상 실현손익 — 실제 수입 아님"

    return verdict, ticket["reason"], ticket


def _estimate_sell_realized(symbol: str, quantity: int, price: float) -> float | None:
    """보유 평단 기반 '예상' 실현손익. 원천 없으면 None."""
    try:
        from config.settings import (
            HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION,
        )
        for hh in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
            info = hh.get(symbol) or hh.get(symbol.upper())
            if not info:
                continue
            shares = int(_num(info.get("shares")))
            avg = _num(info.get("avg_cost") or info.get("avg_cost_usd"))
            if shares <= 0 or avg <= 0 or not price:
                return None
            qty = min(quantity or shares, shares)
            return round((price - avg) * qty, 2)
    except Exception:
        return None
    return None


def _blocked_row(act: dict, reason: str) -> dict:
    return {
        "account": _clean_account(str(act.get("account") or "")),
        "symbol": str(act.get("ticker") or act.get("symbol") or ""),
        "name": act.get("name") or "",
        "side": str(act.get("side") or "").upper() or "BUY",
        "manual_only": True,
        "auto_execution": False,
        "verdict": "BLOCK",
        "reason": reason,
    }


# ─── 5. 텔레그램 렌더 ────────────────────────────────────────────

def render_income_telegram(payload: dict) -> list[str]:
    if not payload:
        return []
    SEP = "━━━━━━━━━━━━━━━━━━"
    kpi = payload.get("income_kpi") or {}
    toss_kpi = kpi.get("toss") or {}
    ss_kpi = kpi.get("samsung") or {}
    toss = payload.get("toss") or {}
    samsung = payload.get("samsung") or {}
    thesis = payload.get("thesis") or {}
    daily_review = bool(payload.get("daily_review"))

    lines: list[str] = [SEP, "💰 *오늘 수입 계기판*"]
    lines.append(f"[Toss AI] 실현수입: {_realized_text(toss_kpi)}")
    lines.append(f"  오늘 평가변동: {_won(toss_kpi.get('today_unrealized_krw'))} / "
                 f"누적 평가손익: {_won(toss_kpi.get('total_unrealized_krw'))}")
    lines.append(f"  총액 {_won(toss_kpi.get('total_account_value_krw'))} · "
                 f"현금 {_won(toss_kpi.get('cash_krw'))} + ${_num(toss_kpi.get('cash_usd')):,.2f}")
    lines.append(f"[삼성] 실현수입: {_realized_text(ss_kpi)}")
    lines.append(f"  오늘 평가변동: {_won(ss_kpi.get('today_unrealized_krw'))} (실현수입 아님)")
    lines.append(f"  총자산 {_won(ss_kpi.get('total_asset_krw'))} · 현금 {_won(ss_kpi.get('cash_krw'))}"
                 + (f" · 기준일 {ss_kpi.get('holdings_as_of')} ⚠stale" if ss_kpi.get("data_status") == "stale" else ""))

    # 보유 관리·현금 만들기
    lines.append("")
    lines.append("🛡 *보유 관리·현금 만들기*")
    pm = samsung.get("position_management") or []
    for row in pm[:6]:
        lines.append(f"  [{row.get('account')}] {row.get('name')}({row.get('symbol')}) "
                     f"{row.get('status')}: {row.get('reason')}")
    if not pm:
        lines.append("  점검 대상 없음")
    reb = toss.get("rebalance") or {}
    lines.append(f"  Toss 리밸런싱: portfolio={reb.get('portfolio_rebalance_required')} "
                 f"funding={reb.get('funding_rebalance_required')}")
    if reb.get("funding_rebalance_required"):
        tgt = reb.get("funding_target") or {}
        lines.append(f"    funding {reb.get('funding_currency')} → {tgt.get('symbol')} "
                     f"(확보예상 {_won(reb.get('expected_release_krw'))})")
    elif reb.get("zero_reason"):
        lines.append(f"    후보 0: {reb.get('zero_reason')}")

    if daily_review:
        # US_CLOSE: 미래 후보 렌더 금지 — 체결/평가/논지만
        lines.append("")
        lines.append("🤖 *Toss 자동운영 — 오늘 실행 결과*")
        orders = toss.get("recent_orders") or []
        for o in orders:
            lines.append(f"  {o.get('symbol')} {str(o.get('side')).upper()} {o.get('status')} ({o.get('reason')})")
        if not orders:
            lines.append("  오늘 자동주문 없음")
    else:
        lines.append("")
        lines.append(f"🤖 *Toss: 자동운영* ({toss.get('automation_mode')})")
        ready = toss.get("ready_buys") or []
        for rb in ready:
            lines.append(f"  🟢 {rb.get('name')}({rb.get('symbol')}) {rb.get('quantity')}주 "
                         f"@{_num(rb.get('limit_price')):,.0f} 예상수입 {_won(rb.get('expected_pnl_krw'))} (실제 수입 아님)")
        if not ready:
            lines.append("  자동 매수 준비 후보 없음")

        lines.append("")
        lines.append("🏦 *삼성: 수동 주문만 · 자동실행 없음*")
        tickets = samsung.get("manual_income_tickets") or []
        for t in tickets:
            lines.append(f"  [{t.get('account')}] {t.get('side')} {t.get('name')}({t.get('symbol')}) "
                         f"{t.get('quantity')}주 @{_num(t.get('limit_price')):,.0f} "
                         f"예상수입 {_won(t.get('expected_pnl_krw'))} · {t.get('verdict')}")
        if not tickets:
            reason = "삼성 보유 stale" if ss_kpi.get("data_status") == "stale" else "적격 수동 후보 없음"
            lines.append(f"  수동 수입 후보 없음 ({reason})")

    # 논지·만료
    lines.append("")
    lines.append("📜 *논지·만료*")
    n_valid = len(thesis.get("valid") or [])
    expired = thesis.get("expired") or []
    invalid = thesis.get("invalid") or []
    expiring = thesis.get("expiring_within_30d") or []
    lines.append(f"  유효 {n_valid} / 만료 {len(expired)} / 불량 {len(invalid)}")
    for row in expired[:3]:
        lines.append(f"  ⏰ {row.get('symbol')} 만료({row.get('valid_until')}) — 재리서치 필요")
    for row in expiring[:3]:
        lines.append(f"  ⚠ {row.get('symbol')} {row.get('valid_until')} 만료 예정")

    # 차단 이유
    blocks = toss.get("block_reasons") or []
    blocked_tickets = samsung.get("blocked_tickets") or []
    if (blocks or blocked_tickets) and not daily_review:
        lines.append("")
        lines.append("🚫 *차단 이유*")
        for b in blocks[:5]:
            lines.append(f"  Toss {b.get('reason')}: {b.get('count')}건")
        for bt in blocked_tickets[:5]:
            lines.append(f"  삼성 [{bt.get('account')}] {bt.get('symbol')}: {bt.get('reason')}")
    lines.append(SEP)
    return lines


# ─── 6. 이메일 HTML 렌더 ─────────────────────────────────────────

def render_income_html(payload: dict) -> str:
    if not payload:
        return ""
    import html as _html

    def esc(v) -> str:
        return _html.escape(str(v if v is not None else ""))

    kpi = payload.get("income_kpi") or {}
    toss_kpi = kpi.get("toss") or {}
    ss_kpi = kpi.get("samsung") or {}
    toss = payload.get("toss") or {}
    samsung = payload.get("samsung") or {}
    thesis = payload.get("thesis") or {}
    daily_review = bool(payload.get("daily_review"))

    parts: list[str] = ['<div class="income-briefing">']
    parts.append("<h2>💰 오늘 수입 계기판</h2>")
    parts.append("<table border='1' cellpadding='6' cellspacing='0'>")
    parts.append("<tr><th></th><th>실현수입</th><th>오늘 평가변동</th><th>현금</th><th>총액</th></tr>")
    parts.append(
        f"<tr><td>Toss AI (자동운영)</td><td>{esc(_realized_text(toss_kpi))}</td>"
        f"<td>{esc(_won(toss_kpi.get('today_unrealized_krw')))}</td>"
        f"<td>{esc(_won(toss_kpi.get('cash_krw')))} + ${_num(toss_kpi.get('cash_usd')):,.2f}</td>"
        f"<td>{esc(_won(toss_kpi.get('total_account_value_krw')))}</td></tr>")
    stale_mark = " ⚠stale" if ss_kpi.get("data_status") == "stale" else ""
    parts.append(
        f"<tr><td>삼성 (수동 주문만 · 자동실행 없음)</td><td>{esc(_realized_text(ss_kpi))}</td>"
        f"<td>{esc(_won(ss_kpi.get('today_unrealized_krw')))} (실현수입 아님)</td>"
        f"<td>{esc(_won(ss_kpi.get('cash_krw')))}</td>"
        f"<td>{esc(_won(ss_kpi.get('total_asset_krw')))}{esc(stale_mark)}</td></tr>")
    parts.append("</table>")

    parts.append("<h3>🛡 보유 관리·현금 만들기</h3><ul>")
    pm = samsung.get("position_management") or []
    for row in pm[:8]:
        parts.append(f"<li>[{esc(row.get('account'))}] {esc(row.get('name'))}({esc(row.get('symbol'))}) "
                     f"{esc(row.get('status'))} — {esc(row.get('reason'))}</li>")
    if not pm:
        parts.append("<li>점검 대상 없음</li>")
    reb = toss.get("rebalance") or {}
    parts.append(f"<li>Toss 리밸런싱: portfolio={esc(reb.get('portfolio_rebalance_required'))} "
                 f"funding={esc(reb.get('funding_rebalance_required'))}"
                 + (f" — {esc(reb.get('zero_reason'))}" if reb.get("zero_reason") else "") + "</li>")
    parts.append("</ul>")

    if not daily_review:
        parts.append(f"<h3>🤖 Toss: 자동운영 ({esc(toss.get('automation_mode'))})</h3><ul>")
        ready = toss.get("ready_buys") or []
        for rb in ready:
            parts.append(f"<li>{esc(rb.get('name'))}({esc(rb.get('symbol'))}) {esc(rb.get('quantity'))}주 "
                         f"@{_num(rb.get('limit_price')):,.0f} 예상수입 {esc(_won(rb.get('expected_pnl_krw')))} (실제 수입 아님)</li>")
        if not ready:
            parts.append("<li>자동 매수 준비 후보 없음</li>")
        parts.append("</ul>")

        parts.append("<h3>🏦 삼성: 수동 주문만 · 자동실행 없음</h3><ul>")
        tickets = samsung.get("manual_income_tickets") or []
        for t in tickets:
            parts.append(f"<li>[{esc(t.get('account'))}] {esc(t.get('side'))} {esc(t.get('name'))}({esc(t.get('symbol'))}) "
                         f"{esc(t.get('quantity'))}주 예상수입 {esc(_won(t.get('expected_pnl_krw')))} · {esc(t.get('verdict'))}</li>")
        if not tickets:
            parts.append("<li>수동 수입 후보 없음</li>")
        parts.append("</ul>")
    else:
        parts.append("<h3>🤖 Toss 자동운영 — 오늘 실행 결과</h3><ul>")
        orders = toss.get("recent_orders") or []
        for o in orders:
            parts.append(f"<li>{esc(o.get('symbol'))} {esc(str(o.get('side')).upper())} {esc(o.get('status'))}</li>")
        if not orders:
            parts.append("<li>오늘 자동주문 없음</li>")
        parts.append("</ul>")

    expired = thesis.get("expired") or []
    invalid = thesis.get("invalid") or []
    parts.append("<h3>📜 논지·만료</h3><ul>")
    parts.append(f"<li>유효 {len(thesis.get('valid') or [])} / 만료 {len(expired)} / 불량 {len(invalid)}</li>")
    for row in (thesis.get("expiring_within_30d") or [])[:5]:
        parts.append(f"<li>⚠ {esc(row.get('symbol'))} {esc(row.get('valid_until'))} 만료 예정</li>")
    parts.append("</ul>")

    blocks = toss.get("block_reasons") or []
    blocked_tickets = samsung.get("blocked_tickets") or []
    if blocks or blocked_tickets:
        parts.append("<h3>🚫 차단 이유</h3><ul>")
        for b in blocks[:6]:
            parts.append(f"<li>Toss {esc(b.get('reason'))}: {esc(b.get('count'))}건</li>")
        for bt in blocked_tickets[:6]:
            parts.append(f"<li>삼성 [{esc(bt.get('account'))}] {esc(bt.get('symbol'))}: {esc(bt.get('reason'))}</li>")
        parts.append("</ul>")
    parts.append("</div>")
    return "\n".join(parts)
