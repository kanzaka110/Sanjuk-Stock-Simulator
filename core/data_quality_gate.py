"""core/data_quality_gate.py — 브리핑 생성 전 데이터 품질 게이트

배경 (2026-07-01, Hermes 감시 레이어와 짝):
KR_NIGHT 브리핑에 `HTTP 401 / Invalid Crumb`, `Toss GET /api/v1/accounts 401`,
`삼성전자 지지 ₩320K` 같은 스케일 이상이 섞여 있는데도 정상 브리핑처럼 발송된
사례가 있었다. 이 모듈은 브리핑 생성 단계에서 데이터 소스 실패/가격 이상치를
구조적으로 판정하고, 실행 판단 제한 여부를 결정한다.

핵심 설계:
- MarketSnapshot은 per-ticker 실패를 저장하지 않는다 → 요청 티커 대비 시세가 빠졌으면
  (auth 401 / delisted / no price data 등) 실패로 간주한다.
- 가격 스케일 이상은 HOLDINGS 평단(avg_cost) 대비 비율로 판정한다.
  자릿수 오류/통화 혼동급(×8 초과 또는 ×1/8 미만)만 이상치로 본다 —
  장기 저가매집 종목은 평단 3~5배가 정상 시세일 수 있다 (삼성전자 실사례).
- FX(원달러) 결측/비정상은 미국 종목 평가 왜곡으로 이어지므로 경고한다.

이 모듈은 순수 판정만 한다. 주문 생성/전송/실행 경로는 절대 건드리지 않는다.
실행제한 판정은 렌더링/프롬프트 계층이 매수·매도 표현을 HOLD/BLOCK로 낮추는 데 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ─── 상태 라벨 ──────────────────────────────────────────────
STATUS_NORMAL = "정상"
STATUS_WARNING = "경고"
STATUS_LIMITED = "실행제한"

# ─── 판정 임계값 ────────────────────────────────────────────
# 요청 티커 중 시세 결측 비율이 이 값 이상이면 대규모 데이터 장애 → 실행제한
_MISSING_RATIO_LIMIT = 0.5
# 가격 스케일 이상 판별 (평단 대비). 소스 오류의 전형인 자릿수 오류(×10/×100)나
# 통화 혼동(원↔달러 ≈ ×1,300)급만 잡는다.
# 주의: 장기 저가매집 코어 종목은 정상적으로 평단 3~5배 도달 가능
# (실사례 2026-07-02: 삼성전자 평단 ₩83,482 vs 실체결가 ₩290,000 = 3.5배 — 정상 시세).
_PRICE_SCALE_HIGH = 8.0    # 현재가 > 평단 × 8 → 스케일 이상 의심
_PRICE_SCALE_LOW = 0.125   # 현재가 < 평단 × 1/8 → 스케일 이상 의심
# 원달러 정상 범위 (이 범위 밖이면 FX 소스 이상)
_FX_MIN = 900.0
_FX_MAX = 2000.0


@dataclass(frozen=True)
class DataQualityReport:
    """브리핑 데이터 품질 판정 결과 (frozen)."""

    status: str = STATUS_NORMAL
    execution_limited: bool = False
    warnings: tuple[str, ...] = ()
    failed_sources: tuple[str, ...] = ()
    missing_price_tickers: tuple[str, ...] = ()
    price_scale_anomalies: tuple[str, ...] = ()
    source_mismatches: tuple[str, ...] = ()
    fx_ok: bool = True
    broker_snapshot_stale: bool = False

    def header_text(self) -> str:
        """브리핑 상단에 붙일 데이터 품질 헤더."""
        lines = [f"데이터 품질: {self.status}"]
        if self.failed_sources:
            lines.append("· 실패 소스: " + ", ".join(self.failed_sources))
        if self.price_scale_anomalies:
            lines.append("· 가격 스케일 이상: " + ", ".join(self.price_scale_anomalies))
        if self.source_mismatches:
            lines.append("· 시세 소스 불일치: " + ", ".join(self.source_mismatches))
        if not self.fx_ok:
            lines.append("· 환율(원달러) 결측/비정상 — 미국 종목 평가 신뢰도 낮음")
        if self.execution_limited:
            lines.append("· 실행 판단 제한: 매수/매도 실행 표현을 HOLD/BLOCK로 낮춤")
        return "\n".join(lines)


def _lookup_fx(macro: dict, current_prices: dict[str, float]) -> float:
    """원달러 환율 조회. macro는 표시명 키, current_prices는 티커 키."""
    # macro는 이름으로 키가 걸려 있으므로 Quote.ticker == "USDKRW=X"를 찾는다.
    for q in (macro or {}).values():
        if getattr(q, "ticker", "") == "USDKRW=X":
            return float(getattr(q, "price", 0) or 0)
    return float((current_prices or {}).get("USDKRW=X", 0) or 0)


def _baseline_cost(info: dict, fx: float) -> float:
    """HOLDINGS 항목 평단을 현지통화 기준으로 반환 (스케일 판정 baseline)."""
    if not isinstance(info, dict):
        return 0.0
    # 국내 종목은 avg_cost_krw, 해외는 avg_cost_usd. 스케일 비율만 볼 것이라
    # 통화 환산 없이 동일 통화(현재가와 같은 통화) 기준으로 비교한다.
    return float(info.get("avg_cost_krw") or info.get("avg_cost_usd") or 0.0)


def assess_data_quality(
    snapshot,
    current_prices: dict[str, float],
    requested_tickers,
    holdings: dict[str, dict],
    *,
    broker_snapshot_stale: bool = False,
    price_cross_check: dict[str, tuple[float, float, float]] | None = None,
) -> DataQualityReport:
    """브리핑 생성 전 데이터 품질 판정.

    Args:
        snapshot: MarketSnapshot (stocks/indices/macro)
        current_prices: {ticker: price} — analyzer가 snapshot.stocks에서 만든 dict
        requested_tickers: 브리핑이 요청한 종목 티커 (dict 또는 iterable)
        holdings: 전 계좌 병합 HOLDINGS ({ticker: {shares, avg_cost_*}})
        broker_snapshot_stale: 삼성 실계좌 원본 스냅샷 없음/오래됨 여부
        price_cross_check: market.cross_check_prices() 결과
            ({ticker: (primary, secondary, diff_pct)}) — 3%+ 불일치만 담김.
            10%+ 불일치는 자릿수/통화 오류급 → 실행제한

    Returns:
        DataQualityReport
    """
    warnings: list[str] = []
    failed_sources: list[str] = []

    requested = list(requested_tickers or [])
    fetched = {tk for tk, p in (current_prices or {}).items() if p and p > 0}

    # ── 1) 시세 결측 (401/Invalid Crumb/delisted/no price data의 구조적 신호) ──
    missing = [tk for tk in requested if tk not in fetched]
    missing_ratio = len(missing) / len(requested) if requested else 0.0
    if missing:
        failed_sources.append(f"시세 조회 실패 {len(missing)}종목")
        warnings.append(
            f"시세 결측 {len(missing)}/{len(requested)}종목 — 인증(401)/상장폐지/"
            "데이터 없음 가능. 해당 종목 실행 판단 보류"
        )

    # ── 2) 가격 스케일 이상 (평단 대비 비율) ──
    scale_anomalies: list[str] = []
    fx_for_baseline = _lookup_fx(getattr(snapshot, "macro", {}), current_prices)
    for tk, price in (current_prices or {}).items():
        if not price or price <= 0:
            continue
        info = (holdings or {}).get(tk)
        base = _baseline_cost(info, fx_for_baseline)
        if base <= 0:
            continue
        ratio = price / base
        if ratio > _PRICE_SCALE_HIGH or ratio < _PRICE_SCALE_LOW:
            scale_anomalies.append(f"{tk}(현재가 {price:,.0f} vs 평단 {base:,.0f})")
    if scale_anomalies:
        warnings.append(
            "가격 스케일 이상 " + ", ".join(scale_anomalies)
            + " — 시세 소스 오류 의심, 실행 판단 제한"
        )

    # ── 2.5) 이중 소스 교차검증 불일치 ──
    _CROSS_CRITICAL_PCT = 10.0
    mismatch_labels: list[str] = []
    critical_mismatch = False
    for tk, (primary, secondary, diff_pct) in (price_cross_check or {}).items():
        mismatch_labels.append(
            f"{tk}(주 {primary:,.0f} vs 검증 {secondary:,.0f}, {diff_pct:.1f}%)"
        )
        if diff_pct >= _CROSS_CRITICAL_PCT:
            critical_mismatch = True
    if mismatch_labels:
        warnings.append(
            "시세 소스 불일치 " + ", ".join(mismatch_labels)
            + (" — 10%+ 괴리는 소스 오류급, 실행 판단 제한" if critical_mismatch
               else " — 소스 시차/이상 의심, 해당 종목 가격 재확인")
        )
        if critical_mismatch:
            failed_sources.append("시세 교차검증 10%+ 불일치")

    # ── 3) FX 결측/비정상 ──
    fx = _lookup_fx(getattr(snapshot, "macro", {}), current_prices)
    fx_ok = bool(fx) and _FX_MIN <= fx <= _FX_MAX
    if not fx_ok:
        failed_sources.append("환율(원달러) 결측/비정상")
        warnings.append("원달러 환율 결측/비정상 — 미국 종목 원화 평가 신뢰도 낮음")

    # ── 4) 삼성 실계좌 원본 스냅샷 없음/오래됨 ──
    if broker_snapshot_stale:
        warnings.append(
            "삼성 원본 미확인 — HTML 추정 포트폴리오 기준, 실계좌와 차이 가능"
        )

    # ── 실행제한 판정 ──
    # 대규모 시세 장애 또는 가격 스케일 이상이 있으면 실행 판단을 제한한다.
    execution_limited = (
        (missing_ratio >= _MISSING_RATIO_LIMIT)
        or bool(scale_anomalies)
        or critical_mismatch
    )

    if execution_limited:
        status = STATUS_LIMITED
    elif warnings:
        status = STATUS_WARNING
    else:
        status = STATUS_NORMAL

    return DataQualityReport(
        status=status,
        execution_limited=execution_limited,
        warnings=tuple(warnings),
        failed_sources=tuple(failed_sources),
        missing_price_tickers=tuple(missing),
        price_scale_anomalies=tuple(scale_anomalies),
        source_mismatches=tuple(mismatch_labels),
        fx_ok=fx_ok,
        broker_snapshot_stale=bool(broker_snapshot_stale),
    )
