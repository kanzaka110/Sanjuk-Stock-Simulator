"""
시장 레짐 감지 — VIX + 모멘텀 기반 자동 분류

AgentQuant 패턴 참고: 시장 상태를 자동으로 분류하고
레짐에 따라 전략을 조정한다.

레짐: 강세장 / 약세장 / 횡보장 / 위기
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import yfinance as yf

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketRegime:
    """시장 레짐 감지 결과."""

    regime: str  # 강세장/약세장/횡보장/위기
    confidence: int  # 0-100

    # VIX 관련
    vix: float = 0.0
    vix_level: str = ""  # 안정/경계/공포/극도공포

    # 모멘텀 (S&P500 기준)
    momentum_20d: float = 0.0  # 20일 수익률 (%)
    momentum_60d: float = 0.0  # 60일 수익률 (%)
    trend: str = ""  # 상승추세/하락추세/횡보

    # 이동평균 배열
    above_sma50: bool = False
    above_sma200: bool = False
    golden_cross: bool = False  # 50일 > 200일
    death_cross: bool = False  # 50일 < 200일

    # 전략 가이드
    strategy_guide: str = ""
    risk_adjustment: str = ""  # 공격적/중립/방어적/현금비중확대

    def to_text(self) -> str:
        return (
            f"【시장 레짐】 {self.regime} (확신도 {self.confidence}%)\n"
            f"  VIX: {self.vix:.1f} [{self.vix_level}]\n"
            f"  모멘텀: 20일 {self.momentum_20d:+.1f}% | 60일 {self.momentum_60d:+.1f}%\n"
            f"  추세: {self.trend} | SMA50{'↑' if self.above_sma50 else '↓'} "
            f"SMA200{'↑' if self.above_sma200 else '↓'} "
            f"{'골든크로스' if self.golden_cross else '데드크로스' if self.death_cross else ''}\n"
            f"  전략: {self.risk_adjustment} — {self.strategy_guide}"
        )


def detect_regime() -> MarketRegime:
    """현재 시장 레짐을 감지.

    S&P500 + VIX 데이터를 분석하여 시장 상태를 자동 분류한다.
    """
    try:
        # VIX 조회
        vix_hist = yf.Ticker("^VIX").history(period="5d")
        vix = float(vix_hist["Close"].iloc[-1]) if len(vix_hist) >= 1 else 20.0

        if vix >= 35:
            vix_level = "극도공포"
        elif vix >= 25:
            vix_level = "공포"
        elif vix >= 18:
            vix_level = "경계"
        else:
            vix_level = "안정"

        # S&P500 모멘텀 + 이동평균
        sp_hist = yf.Ticker("^GSPC").history(period="1y")
        if len(sp_hist) < 200:
            return MarketRegime(
                regime="판단불가", confidence=0,
                vix=vix, vix_level=vix_level,
                strategy_guide="데이터 부족", risk_adjustment="중립",
            )

        close = sp_hist["Close"]
        current = float(close.iloc[-1])

        # 모멘텀
        if len(close) >= 20:
            mom_20 = (current / float(close.iloc[-20]) - 1) * 100
        else:
            mom_20 = 0.0

        if len(close) >= 60:
            mom_60 = (current / float(close.iloc[-60]) - 1) * 100
        else:
            mom_60 = 0.0

        # 이동평균
        sma50 = float(close.rolling(50).mean().iloc[-1])
        sma200 = float(close.rolling(200).mean().iloc[-1])

        above_50 = current > sma50
        above_200 = current > sma200
        gc = sma50 > sma200
        dc = sma50 < sma200

        # 추세 판단
        if mom_20 > 2 and mom_60 > 5:
            trend = "상승추세"
        elif mom_20 < -2 and mom_60 < -5:
            trend = "하락추세"
        else:
            trend = "횡보"

        # ─── 레짐 분류 로직 ────────────────────────
        score = 0  # 양수=강세, 음수=약세

        # VIX 기반 (-3 ~ +2)
        if vix < 15:
            score += 2
        elif vix < 20:
            score += 1
        elif vix < 25:
            score -= 1
        elif vix < 35:
            score -= 2
        else:
            score -= 3

        # 모멘텀 기반 (-2 ~ +2)
        if mom_20 > 3:
            score += 1
        elif mom_20 < -3:
            score -= 1

        if mom_60 > 8:
            score += 1
        elif mom_60 < -8:
            score -= 1

        # 이동평균 기반 (-2 ~ +2)
        if above_50:
            score += 1
        else:
            score -= 1

        if gc:
            score += 1
        elif dc:
            score -= 1

        # 레짐 결정
        if score <= -4:
            regime = "위기"
            confidence = min(95, 70 + abs(score) * 5)
            strategy = "현금 비중 50%+, 방어주/금 비중 확대, 신규 매수 중단"
            adjustment = "현금비중확대"
        elif score <= -2:
            regime = "약세장"
            confidence = min(85, 60 + abs(score) * 5)
            strategy = "포지션 축소, 손절 엄격, 단기 반등에 비중 축소"
            adjustment = "방어적"
        elif score >= 4:
            regime = "강세장"
            confidence = min(90, 65 + score * 5)
            strategy = "추세 추종 매수, 조정 시 분할 매수, 수익 종목 홀딩"
            adjustment = "공격적"
        elif score >= 2:
            regime = "강세장"
            confidence = min(80, 55 + score * 5)
            strategy = "선별적 매수, 기술적 확인 후 진입"
            adjustment = "중립"
        else:
            regime = "횡보장"
            confidence = max(40, 50 - abs(score) * 5)
            strategy = "박스권 매매, 지지선 매수/저항선 매도, 소규모 포지션"
            adjustment = "중립"

        return MarketRegime(
            regime=regime,
            confidence=confidence,
            vix=round(vix, 1),
            vix_level=vix_level,
            momentum_20d=round(mom_20, 1),
            momentum_60d=round(mom_60, 1),
            trend=trend,
            above_sma50=above_50,
            above_sma200=above_200,
            golden_cross=gc,
            death_cross=dc,
            strategy_guide=strategy,
            risk_adjustment=adjustment,
        )
    except Exception as e:
        log.warning(f"시장 레짐 감지 실패: {e}")
        return MarketRegime(
            regime="판단불가", confidence=0,
            strategy_guide="데이터 조회 실패", risk_adjustment="중립",
        )
