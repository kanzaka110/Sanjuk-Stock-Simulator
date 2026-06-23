"""
Toss 자동거래 설정 기본값 테스트

모든 live/실주문 관련 플래그가 안전한 기본값인지 검증.
"""

from config import toss_automation as cfg


class TestSafeDefaults:
    def test_automation_disabled(self):
        assert cfg.TOSS_AUTOMATION_ENABLED is False

    def test_mode_is_paper(self):
        assert cfg.TOSS_AUTOMATION_MODE == "paper"

    def test_dry_run_true(self):
        assert cfg.TOSS_DRY_RUN is True

    def test_kill_switch_on(self):
        assert cfg.TOSS_KILL_SWITCH is True

    def test_live_orders_disabled(self):
        assert cfg.TOSS_ALLOW_LIVE_ORDERS is False

    def test_telegram_approval_required(self):
        assert cfg.TOSS_REQUIRE_TELEGRAM_APPROVAL is True


class TestLimits:
    def test_max_order_positive(self):
        assert cfg.TOSS_MAX_ORDER_KRW > 0

    def test_daily_limit_positive(self):
        assert cfg.TOSS_MAX_DAILY_ORDER_KRW > 0

    def test_cash_buffer_positive(self):
        assert cfg.TOSS_MIN_CASH_BUFFER_KRW > 0

    def test_max_positions_positive(self):
        assert cfg.TOSS_MAX_POSITIONS > 0

    def test_budget_consistent(self):
        assert cfg.TOSS_MAX_ORDER_KRW <= cfg.TOSS_MAX_DAILY_ORDER_KRW
        assert cfg.TOSS_MIN_CASH_BUFFER_KRW < cfg.TOSS_ACCOUNT_BUDGET_KRW


class TestFilters:
    def test_blacklist_exists(self):
        assert isinstance(cfg.TOSS_SYMBOL_BLACKLIST, list)

    def test_whitelist_empty_by_default(self):
        assert cfg.TOSS_SYMBOL_WHITELIST == []

    def test_allowed_markets(self):
        assert "KR" in cfg.TOSS_ALLOWED_MARKETS
        assert "US" in cfg.TOSS_ALLOWED_MARKETS
