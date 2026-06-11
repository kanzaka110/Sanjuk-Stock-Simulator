"""
KIS 계좌 잔고 조회 — settings.HOLDINGS 수동 관리 검증용

목적: HOLDINGS/예수금이 수동 하드코딩이라 매매 후 갱신 누락 시
AI가 틀린 포지션으로 판단함. KIS 계좌의 실잔고를 조회하여
settings와 대조 — 불일치 시 브리핑에 경고 주입.

주의: HOLDINGS는 삼성증권 계좌 기준일 수 있음 (settings 주석 참조).
KIS 계좌가 보유 계좌와 다르면 이 검증은 KIS 계좌 한정으로만 유효.

읽기 전용 API만 사용 (주문 기능 없음).
- 국내주식 잔고: TTTC8434R
- 해외주식 잔고: TTTS3012R
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from config.settings import KIS_ACCOUNT_NO, KIS_BASE_URL

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KisPosition:
    """KIS 계좌 보유 포지션."""

    ticker: str  # 정규화 (.KS 접미사 또는 US 심볼)
    name: str
    shares: int
    avg_cost: float
    current_price: float
    pnl_pct: float


@dataclass(frozen=True)
class KisBalance:
    """KIS 계좌 잔고 스냅샷."""

    positions: tuple[KisPosition, ...] = ()
    cash_krw: float = 0.0
    available: bool = False  # 조회 성공 여부
    error: str = ""


def _account_parts() -> tuple[str, str] | None:
    """KIS_ACCOUNT_NO를 (계좌번호 8자리, 상품코드 2자리)로 분리."""
    if not KIS_ACCOUNT_NO or "-" not in KIS_ACCOUNT_NO:
        return None
    cano, prdt = KIS_ACCOUNT_NO.split("-", 1)
    return cano.strip(), prdt.strip()


def fetch_domestic_balance() -> KisBalance:
    """국내주식 잔고 조회 (TTTC8434R). 실패 시 available=False."""
    from core.market_kis import _get_access_token, _is_kis_configured
    from config.settings import KIS_APP_KEY, KIS_APP_SECRET

    parts = _account_parts()
    if not _is_kis_configured() or parts is None:
        return KisBalance(error="KIS 미설정 또는 계좌번호 형식 오류")

    token = _get_access_token()
    if not token:
        return KisBalance(error="토큰 발급 실패")

    cano, prdt = parts
    try:
        r = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": "TTTC8434R",
            },
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": prdt,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            timeout=10,
        )
        data = r.json()
        if data.get("rt_cd") != "0":
            return KisBalance(error=f"KIS 응답 오류: {data.get('msg1', '')[:80]}")

        positions = []
        for row in data.get("output1", []):
            shares = int(row.get("hldg_qty", "0") or 0)
            if shares <= 0:
                continue
            positions.append(KisPosition(
                ticker=f"{row.get('pdno', '')}.KS",
                name=row.get("prdt_name", ""),
                shares=shares,
                avg_cost=float(row.get("pchs_avg_pric", "0") or 0),
                current_price=float(row.get("prpr", "0") or 0),
                pnl_pct=float(row.get("evlu_pfls_rt", "0") or 0),
            ))

        cash = 0.0
        out2 = data.get("output2", [])
        if out2:
            cash = float(out2[0].get("dnca_tot_amt", "0") or 0)

        return KisBalance(positions=tuple(positions), cash_krw=cash, available=True)
    except Exception as e:
        log.warning("KIS 잔고 조회 실패: %s", e)
        return KisBalance(error=str(e)[:100])


def compare_with_settings() -> str:
    """KIS 실잔고와 settings.HOLDINGS를 대조 — 불일치 경고 텍스트 반환.

    KIS 계좌가 보유 계좌(삼성증권)와 다르면 빈 문자열 (검증 불가).
    브리핑 프롬프트에 주입용.
    """
    bal = fetch_domestic_balance()
    if not bal.available:
        log.info("KIS 잔고 검증 스킵: %s", bal.error)
        return ""

    from config.settings import (
        HOLDINGS_GENERAL,
        HOLDINGS_IRP,
        HOLDINGS_ISA,
        HOLDINGS_PENSION,
        HOLDINGS_RIA,
    )

    settings_holdings: dict[str, int] = {}
    for h in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
        for tk, info in h.items():
            settings_holdings[tk] = settings_holdings.get(tk, 0) + info.get("shares", 0)

    kis_holdings = {p.ticker: p.shares for p in bal.positions}

    # KIS 계좌에 잔고가 전혀 없으면 보유 계좌가 아닌 것 — 검증 불가
    if not kis_holdings:
        return ""

    mismatches: list[str] = []
    for tk, kis_shares in kis_holdings.items():
        s = settings_holdings.get(tk, 0)
        if s != kis_shares:
            mismatches.append(f"  {tk}: KIS 실잔고 {kis_shares}주 vs settings {s}주")
    for tk, s_shares in settings_holdings.items():
        if tk.endswith(".KS") and tk not in kis_holdings and s_shares > 0:
            # KIS 계좌에 없는 종목 — 타 증권사 보유일 수 있어 정보로만
            pass

    if not mismatches:
        return "✅ KIS 계좌 잔고와 settings 일치 확인"

    return (
        "⚠️ KIS 실잔고와 settings.HOLDINGS 불일치 감지 — 수동 갱신 누락 가능성:\n"
        + "\n".join(mismatches)
        + "\n→ 최근 매매가 settings.py에 반영됐는지 확인 필요. 아래 분석은 settings 기준."
    )
