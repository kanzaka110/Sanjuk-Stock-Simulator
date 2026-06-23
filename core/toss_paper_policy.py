"""
Toss Paper sizing/risk policy — 표본부족 보수 규칙

- 실제 주문 0건. read-only 성과 데이터 기반 정책 계산만.
- evaluated_count < 5: 소액 paper 검증 모드 (max ₩300,000)
- consensus_anomaly ticker: price_consensus_anomaly block
- 성과 확대는 evaluated_count >= 5 + win_rate >= 60% + avg_pnl > 0 조건 충족 후에만
- live_order_allowed는 항상 False
"""

from __future__ import annotations

import logging
import math
import re as _re

logger = logging.getLogger(__name__)


def _is_kr_ticker(symbol: str) -> bool:
    """KR 종목 여부 (KRW 가격 간주)."""
    return symbol.endswith((".KS", ".KQ")) or bool(_re.match(r"^\d{6}$", symbol))

# ─── 정책 상수 ─────────────────────────────────────────
_SAMPLE_INSUFFICIENT_THRESHOLD = 5    # evaluated_count < 5 → insufficient
_GOOD_WIN_RATE_THRESHOLD = 60.0       # ≥60% → 정책 완화 가능
_POOR_WIN_RATE_THRESHOLD = 40.0       # <40% → 정책 강화
_GOOD_PNL_THRESHOLD = 0.0             # avg_pnl_pct > 0 필요

# 예산 상수 (KRW)
_BUDGET_INSUFFICIENT = 100_000        # 기본 권장
_BUDGET_INSUFFICIENT_MAX = 300_000    # 표본부족 상한
_BUDGET_GOOD_MAX = 500_000            # 성과 좋을 때 상한 (이번 단계)
_BUDGET_POOR_MAX = 100_000            # 성과 나쁠 때 상한
_BUDGET_MIN = 0                       # consensus_anomaly → 0

# sizing multiplier
_MULTIPLIER_INSUFFICIENT = 0.3
_MULTIPLIER_GOOD = 0.5
_MULTIPLIER_POOR = 0.1


def _determine_sample_status(evaluated_count: int, win_rate: float, avg_pnl_pct: float) -> str:
    """evaluated_count / win_rate 기반 표본 상태 판별."""
    if evaluated_count < _SAMPLE_INSUFFICIENT_THRESHOLD:
        return "insufficient"
    if win_rate >= _GOOD_WIN_RATE_THRESHOLD and avg_pnl_pct > _GOOD_PNL_THRESHOLD:
        return "good"
    if win_rate < _POOR_WIN_RATE_THRESHOLD:
        return "poor"
    return "neutral"


def compute_toss_paper_policy(performance_summary: dict | None = None) -> dict:
    """Toss Paper 성과 기반 sizing/risk policy 계산.

    live_order_allowed는 항상 False.
    evaluated_count < 5이면 소액 보수 모드.
    consensus_anomaly는 경고에 포함.

    Returns:
        {
            "mode": "paper_only",
            "live_order_allowed": False,
            "sample_status": str,
            "base_budget_krw": int,
            "max_budget_krw": int,
            "min_budget_krw": int,
            "sizing_multiplier": float,
            "consensus_anomaly_count": int,
            "consensus_anomaly_symbols": list[str],
            "reason": str,
            "blocks": list[str],
            "warnings": list[str],
            "_note": str,
        }
    """
    if performance_summary is None:
        try:
            from core.toss_paper_performance import get_paper_performance_summary
            performance_summary = get_paper_performance_summary()
        except Exception as e:
            logger.debug("paper performance 조회 실패: %s", e)
            performance_summary = {}

    s = (performance_summary or {}).get("summary", {})
    recent = (performance_summary or {}).get("recent", [])

    evaluated_count = s.get("evaluated_count", 0)
    win_rate = s.get("win_rate", 0.0)
    avg_pnl_pct = s.get("avg_pnl_pct", 0.0)
    consensus_anomaly_count = s.get("consensus_anomaly", 0)
    data_error_count = s.get("data_error", 0)

    # consensus_anomaly 종목 이름 수집
    consensus_symbols = [
        r.get("symbol", "")
        for r in recent
        if r.get("error_type") == "consensus_anomaly" and r.get("symbol")
    ]

    sample_status = _determine_sample_status(evaluated_count, win_rate, avg_pnl_pct)

    # ─── 예산 / 배율 결정 ──────────────────────────────
    if sample_status == "insufficient":
        base_budget = _BUDGET_INSUFFICIENT
        max_budget = _BUDGET_INSUFFICIENT_MAX
        multiplier = _MULTIPLIER_INSUFFICIENT
        reason = "Toss Paper 평가 표본부족 — 보수 sizing"
    elif sample_status == "good":
        base_budget = _BUDGET_INSUFFICIENT
        max_budget = _BUDGET_GOOD_MAX
        multiplier = _MULTIPLIER_GOOD
        reason = f"Toss Paper 성과 양호 (승률 {win_rate:.1f}%, 평균 {avg_pnl_pct:+.2f}%) — sizing 완화"
    elif sample_status == "poor":
        base_budget = _BUDGET_MIN
        max_budget = _BUDGET_POOR_MAX
        multiplier = _MULTIPLIER_POOR
        reason = f"Toss Paper 성과 불량 (승률 {win_rate:.1f}%) — sizing 축소"
    else:
        base_budget = _BUDGET_INSUFFICIENT
        max_budget = _BUDGET_INSUFFICIENT_MAX
        multiplier = _MULTIPLIER_INSUFFICIENT
        reason = "Toss Paper 성과 중립 — 보수 sizing 유지"

    blocks: list[str] = []
    warnings: list[str] = []

    if sample_status == "insufficient":
        warnings.append("표본부족 — paper 소액 검증 모드")

    if consensus_anomaly_count > 0:
        sym_str = " · ".join(consensus_symbols) if consensus_symbols else f"{consensus_anomaly_count}건"
        warnings.append(f"기업행동/entry_price 재확인 필요 종목: {sym_str}")

    if data_error_count > 0 and data_error_count > consensus_anomaly_count:
        warnings.append(f"가격 조회 오류 종목 {data_error_count}건 — paper 평가 제한")

    return {
        "mode": "paper_only",
        "live_order_allowed": False,
        "sample_status": sample_status,
        "base_budget_krw": base_budget,
        "max_budget_krw": max_budget,
        "min_budget_krw": _BUDGET_MIN,
        "sizing_multiplier": multiplier,
        "evaluated_count": evaluated_count,
        "win_rate": win_rate,
        "avg_pnl_pct": avg_pnl_pct,
        "consensus_anomaly_count": consensus_anomaly_count,
        "consensus_anomaly_symbols": consensus_symbols,
        "data_error_count": data_error_count,
        "reason": reason,
        "blocks": blocks,
        "warnings": warnings,
        "_note": "Paper sizing/risk policy · 실제 주문 아님 · live_order_allowed=False",
    }


def apply_toss_paper_policy_to_candidate(
    candidate: dict,
    policy: dict,
    performance_summary: dict | None = None,
) -> dict:
    """후보 1건에 paper policy를 적용하여 권장 예산/수량을 계산한다.

    실제 주문 없음. read-only 계산만.

    Args:
        candidate: {symbol, side, limit_price, quantity, estimated_amount_krw, ...}
        policy: compute_toss_paper_policy() 결과
        performance_summary: 최근 평가 결과 (consensus_anomaly ticker 식별용)

    Returns:
        candidate copy + "paper_policy" 필드
    """
    symbol = candidate.get("symbol", "")
    side = candidate.get("side", "buy")
    limit_price = float(candidate.get("limit_price") or 0)

    max_budget = policy.get("max_budget_krw", _BUDGET_INSUFFICIENT_MAX)
    base_budget = policy.get("base_budget_krw", _BUDGET_INSUFFICIENT)
    sizing_multiplier = policy.get("sizing_multiplier", _MULTIPLIER_INSUFFICIENT)
    consensus_symbols = set(policy.get("consensus_anomaly_symbols", []))

    candidate_blocks: list[str] = []
    candidate_warnings: list[str] = list(policy.get("warnings", []))

    # consensus_anomaly ticker 차단
    is_consensus_anomaly = symbol in consensus_symbols
    if is_consensus_anomaly:
        candidate_blocks.append("price_consensus_anomaly")
        candidate_warnings = [
            w for w in candidate_warnings
            if "기업행동" not in w
        ]
        candidate_warnings.insert(0, f"{symbol}: 기업행동/entry_price 재확인 전 paper 제외")

    # 권장 예산 — consensus_anomaly이면 0
    if is_consensus_anomaly:
        recommended_budget = 0
        recommended_quantity = 0
        max_possible_quantity = 0
    else:
        recommended_budget = min(int(base_budget * sizing_multiplier / 0.3), max_budget)
        # 최소 base_budget 보장
        recommended_budget = max(recommended_budget, base_budget) if not is_consensus_anomaly else 0
        recommended_budget = min(recommended_budget, max_budget)

        # 수량 계산 (buy 기준) — 통화 변환 포함
        if side == "buy" and limit_price > 0:
            if _is_kr_ticker(symbol):
                price_krw_per_share = limit_price
            else:
                # US ticker: USD × usdkrw → KRW 환산
                usdkrw = float(candidate.get("_usdkrw") or 0)
                if usdkrw <= 0:
                    usdkrw = 1_350.0  # conservative fallback
                price_krw_per_share = limit_price * usdkrw
            recommended_quantity = max(0, math.floor(recommended_budget / price_krw_per_share))
            max_possible_quantity = max(0, math.floor(max_budget / price_krw_per_share))
        else:
            price_krw_per_share = 0.0
            recommended_quantity = candidate.get("quantity", 0)
            max_possible_quantity = recommended_quantity

    result = dict(candidate)
    result["paper_policy"] = {
        "recommended_budget_krw": recommended_budget,
        "max_budget_krw": max_budget,
        "recommended_quantity": recommended_quantity,
        "max_possible_quantity": max_possible_quantity,
        "sizing_multiplier": sizing_multiplier,
        "sample_status": policy.get("sample_status", "insufficient"),
        "blocks": candidate_blocks,
        "warnings": candidate_warnings,
        "live_order_allowed": False,
        "_note": "Paper sizing · 실제 주문 아님",
    }
    return result


def get_policy_sizing_text(policy: dict, candidate: dict | None = None) -> str:
    """주문표/브리핑용 policy 한 줄 요약 텍스트."""
    sample_status = policy.get("sample_status", "insufficient")
    max_budget = policy.get("max_budget_krw", _BUDGET_INSUFFICIENT_MAX)
    multiplier = policy.get("sizing_multiplier", _MULTIPLIER_INSUFFICIENT)
    consensus_count = policy.get("consensus_anomaly_count", 0)

    if candidate:
        pp = candidate.get("paper_policy", {})
        rec_budget = pp.get("recommended_budget_krw", 0)
        rec_qty = pp.get("recommended_quantity", 0)
        max_qty = pp.get("max_possible_quantity", rec_qty)
        pp_blocks = pp.get("blocks", [])
        if "price_consensus_anomaly" in pp_blocks:
            return "정책 차단: price_consensus_anomaly — 기업행동/entry_price 재확인 전 paper 제외"
        if rec_qty == 0 and max_qty > 0:
            return (
                f"Paper sizing: {sample_status} · 최대 ₩{max_budget:,} · "
                f"권장 예산으로 1주 불가 · 최대 예산 기준 {max_qty}주 가능"
            )
        return (
            f"Paper sizing: {sample_status} · 최대 ₩{max_budget:,} · "
            f"권장 수량 {rec_qty}주 · 권장 금액 ₩{rec_budget:,}"
        )

    status_label = {
        "insufficient": "표본부족",
        "good": "성과양호",
        "poor": "성과불량",
        "neutral": "중립",
    }.get(sample_status, sample_status)

    line = f"Paper sizing: {status_label} · 최대 ₩{max_budget:,} · multiplier={multiplier}"
    if consensus_count > 0:
        line += f" · ⚠️ 기업행동 의심 {consensus_count}건"
    return line
