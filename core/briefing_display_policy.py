"""core/briefing_display_policy.py

브리핑 유형별 렌더링 정책.
"""

from __future__ import annotations

# 전체 보유종목 표를 렌더하는 브리핑 유형
_FULL_PORTFOLIO_TYPES: frozenset[str] = frozenset(["KR_OPEN", "KR_BEFORE"])


def should_render_full_portfolio(briefing_type: str) -> bool:
    """전체 보유종목 현황을 렌더할지 여부.

    KR_OPEN / KR_BEFORE → True (아침 브리핑, 전체 표 표시)
    나머지 → False (요약만)
    """
    return briefing_type in _FULL_PORTFOLIO_TYPES
