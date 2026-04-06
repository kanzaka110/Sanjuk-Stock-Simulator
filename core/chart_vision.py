"""
멀티모달 차트 분석 — 차트 이미지 생성 → Gemini 패턴 인식

matplotlib로 캔들차트+지표 이미지를 생성하고,
Gemini Pro에 전송하여 차트 패턴을 AI가 인식한다.

지원 패턴: 더블탑/바텀, 헤드앤숄더, 컵앤핸들, 삼각형 등
"""

from __future__ import annotations

import io
import base64
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yfinance as yf

from config.settings import GEMINI_API_KEY

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChartAnalysis:
    """차트 패턴 분석 결과."""

    ticker: str
    name: str
    patterns: tuple[str, ...]  # 발견된 패턴들
    support_levels: tuple[float, ...]  # 지지선
    resistance_levels: tuple[float, ...]  # 저항선
    trend_description: str  # 추세 설명
    action_suggestion: str  # 매매 제안
    confidence: int  # 0-100


def generate_chart_image(
    ticker: str, period: str = "6mo"
) -> bytes | None:
    """캔들차트 + 이동평균 + 거래량 이미지 생성.

    Returns:
        PNG 이미지 바이트 또는 None
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 20:
            return None

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(12, 8),
            height_ratios=[3, 1],
            sharex=True,
        )
        fig.patch.set_facecolor("#1a1a2e")

        dates = hist.index
        close = hist["Close"]
        opens = hist["Open"]
        high = hist["High"]
        low = hist["Low"]
        volume = hist["Volume"]

        # ─── 캔들차트 ─────────────────────────────
        ax1.set_facecolor("#1a1a2e")
        for i in range(len(hist)):
            color = "#00d4aa" if close.iloc[i] >= opens.iloc[i] else "#ff6b6b"
            ax1.plot(
                [dates[i], dates[i]],
                [low.iloc[i], high.iloc[i]],
                color=color, linewidth=0.8,
            )
            ax1.plot(
                [dates[i], dates[i]],
                [opens.iloc[i], close.iloc[i]],
                color=color, linewidth=3,
            )

        # 이동평균
        if len(close) >= 20:
            sma20 = close.rolling(20).mean()
            ax1.plot(dates, sma20, color="#ffd700", linewidth=1, label="SMA20", alpha=0.8)
        if len(close) >= 50:
            sma50 = close.rolling(50).mean()
            ax1.plot(dates, sma50, color="#ff69b4", linewidth=1, label="SMA50", alpha=0.8)

        # 볼린저밴드
        if len(close) >= 20:
            bb_mid = close.rolling(20).mean()
            bb_std = close.rolling(20).std()
            bb_upper = bb_mid + 2 * bb_std
            bb_lower = bb_mid - 2 * bb_std
            ax1.fill_between(dates, bb_upper, bb_lower, alpha=0.1, color="#87ceeb")

        ax1.set_title(f"{ticker} Chart Analysis", color="white", fontsize=14)
        ax1.tick_params(colors="white")
        ax1.legend(loc="upper left", fontsize=8, facecolor="#2a2a4e", edgecolor="none", labelcolor="white")
        ax1.grid(True, alpha=0.1)

        # ─── 거래량 ───────────────────────────────
        ax2.set_facecolor("#1a1a2e")
        colors = ["#00d4aa" if c >= o else "#ff6b6b" for c, o in zip(close, opens)]
        ax2.bar(dates, volume, color=colors, alpha=0.6, width=0.8)
        ax2.tick_params(colors="white")
        ax2.set_ylabel("Volume", color="white", fontsize=10)
        ax2.grid(True, alpha=0.1)

        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        fig.autofmt_xdate()

        plt.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor="#1a1a2e")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except Exception as e:
        log.warning(f"차트 생성 실패 ({ticker}): {e}")
        return None


def analyze_chart_with_vision(
    ticker: str, name: str = "", period: str = "6mo"
) -> ChartAnalysis | None:
    """차트 이미지를 Gemini에 전송하여 패턴 분석.

    Returns:
        ChartAnalysis 또는 실패 시 None
    """
    if not GEMINI_API_KEY:
        return None

    image_bytes = generate_chart_image(ticker, period)
    if not image_bytes:
        return None

    try:
        import json
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)

        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/png")

        prompt = f"""이 차트는 {name}({ticker})의 {period} 캔들차트입니다.
이동평균(SMA20, SMA50)과 볼린저밴드가 표시되어 있습니다.

다음을 분석하여 JSON으로 반환하세요 (코드블록 없이):
{{
  "patterns": ["발견된 차트 패턴 목록 (더블탑, 헤드앤숄더, 컵앤핸들, 삼각형, 깃발 등)"],
  "support_levels": [주요 지지선 가격들],
  "resistance_levels": [주요 저항선 가격들],
  "trend_description": "현재 추세와 흐름 설명 (50자 이내)",
  "action_suggestion": "매매 제안 (50자 이내)",
  "confidence": 0-100
}}"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt, image_part],
            config=types.GenerateContentConfig(
                max_output_tokens=1000,
                response_mime_type="application/json",
            ),
        )

        raw = response.text.strip() if response.text else ""
        if not raw:
            return None

        data: dict = {}
        if "```" in raw:
            for part in raw.split("```"):
                part = part.strip().lstrip("json").strip()
                try:
                    data = json.loads(part)
                    break
                except json.JSONDecodeError:
                    continue
        if not data:
            data = json.loads(raw)

        return ChartAnalysis(
            ticker=ticker,
            name=name,
            patterns=tuple(data.get("patterns", [])),
            support_levels=tuple(float(s) for s in data.get("support_levels", [])),
            resistance_levels=tuple(float(r) for r in data.get("resistance_levels", [])),
            trend_description=data.get("trend_description", ""),
            action_suggestion=data.get("action_suggestion", ""),
            confidence=int(data.get("confidence", 50)),
        )
    except Exception as e:
        log.warning(f"차트 비전 분석 실패 ({ticker}): {e}")
        return None


def analyze_key_charts(
    tickers: dict[str, str], max_charts: int = 4
) -> list[ChartAnalysis]:
    """주요 종목 차트 분석 (비용 절감을 위해 최대 4개)."""
    results: list[ChartAnalysis] = []
    # 주요 종목 우선
    priority = ["NVDA", "005930.KS", "012450.KS", "MU"]
    ordered = [tk for tk in priority if tk in tickers]
    ordered += [tk for tk in tickers if tk not in ordered]

    for tk in ordered[:max_charts]:
        analysis = analyze_chart_with_vision(tk, tickers[tk])
        if analysis:
            results.append(analysis)

    return results


def chart_analyses_to_text(analyses: list[ChartAnalysis]) -> str:
    """차트 분석 결과를 텍스트로 변환."""
    if not analyses:
        return ""

    lines = ["【차트 패턴 분석 (AI Vision)】"]
    for ca in analyses:
        patterns = ", ".join(ca.patterns) if ca.patterns else "특이 패턴 없음"
        supports = ", ".join(f"{s:,.0f}" for s in ca.support_levels) if ca.support_levels else "-"
        resists = ", ".join(f"{r:,.0f}" for r in ca.resistance_levels) if ca.resistance_levels else "-"
        lines.append(
            f"  {ca.name} ({ca.ticker}): 패턴=[{patterns}]\n"
            f"    지지선: {supports} | 저항선: {resists}\n"
            f"    추세: {ca.trend_description}\n"
            f"    제안: {ca.action_suggestion} (확신도 {ca.confidence}%)"
        )
    return "\n".join(lines)
