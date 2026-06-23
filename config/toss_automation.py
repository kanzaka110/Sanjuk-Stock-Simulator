"""
Toss 실전 AI 자동거래 설정 — Phase A 기본값

모든 live 실행 관련 플래그는 기본 비활성.
실주문 전환은 별도 Phase C 승인 후에만 가능.
"""

from __future__ import annotations

# ─── 자동화 활성화 ────────────────────────────────────
TOSS_AUTOMATION_ENABLED: bool = False
TOSS_AUTOMATION_MODE: str = "paper"  # paper only (이번 단계)
TOSS_DRY_RUN: bool = True

# ─── 예산/한도 ────────────────────────────────────────
TOSS_ACCOUNT_BUDGET_KRW: int = 10_000_000
TOSS_MAX_ORDER_KRW: int = 300_000
TOSS_MAX_DAILY_ORDER_KRW: int = 1_000_000
TOSS_MAX_POSITIONS: int = 5
TOSS_MAX_POSITION_WEIGHT_PCT: int = 20
TOSS_MIN_CASH_BUFFER_KRW: int = 2_000_000
TOSS_DAILY_LOSS_LIMIT_KRW: int = 150_000

# ─── 안전장치 ─────────────────────────────────────────
TOSS_KILL_SWITCH: bool = True           # True = 자동 실행 차단
TOSS_REQUIRE_TELEGRAM_APPROVAL: bool = True
TOSS_ALLOW_LIVE_ORDERS: bool = False    # 절대 이번 단계에서 True 금지

# ─── 종목 필터 ────────────────────────────────────────
TOSS_ALLOWED_MARKETS: list[str] = ["KR", "US"]
TOSS_SYMBOL_WHITELIST: list[str] = []   # 비어있으면 live 차단
TOSS_SYMBOL_BLACKLIST: list[str] = ["MU"]  # 기존 보유 혼동 방지

# ─── 신호 최소 기준 ──────────────────────────────────
TOSS_MIN_CONFIDENCE: float = 0.6
TOSS_MAX_QUOTE_AGE_SEC: int = 300       # 시세 5분 초과 시 차단
