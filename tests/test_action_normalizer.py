"""
action_normalizer 결정론적 분류 테스트 + 운영 DB 무결성 테스트.

핵심 불변식 (Hermes 검증 기준):
- 매수 reason에 "추격 금지/대기/조건 미충족/FOMC 후/눌림목" → executable 금지(조건부로)
- 매도 reason에 "매도 취소/홀딩 전환" → 실행 매도 금지(CANCEL/HOLD로)
- 저장 시 action_type/briefing_type/account_type 채워짐, original_signal 보존
- DB: IMMEDIATE_ACTION인데 reasoning에 '추격 금지/조건 미충족' = 0
- DB: signal='매도'인데 reasoning에 '매도 취소/홀딩 전환' = 0
"""

import sqlite3

import pytest

from core.action_normalizer import (
    AI_NEW_BUY, AI_ADD_BUY, CONDITIONAL_NEW_BUY, AI_SELL_MANAGEMENT,
    CANCEL_SELL, HOLD_REVIEW, normalize_actions,
)


class TestNormalizeBuy:
    @pytest.mark.parametrize("phrase", [
        "추격 금지", "대기", "조건 미충족", "FOMC 후", "눌림목",
        "현재 진입 조건 미충족", "즉시 진입은 부적절", "검토",
    ])
    def test_buy_block_phrases_go_conditional(self, phrase):
        raw = {"strategy_buy": [
            {"ticker": "091160.KS", "name": "X", "account": "[RIA]",
             "entry_price": "₩100", "reason": f"강세지만 {phrase}"}], "strategy_sell": []}
        n = normalize_actions(raw, "KR_BEFORE", {}, {})
        assert len(n["executable_actions"]) == 0
        assert len(n["conditional_buy_candidates"]) == 1
        assert n["conditional_buy_candidates"][0]["action_type"] == CONDITIONAL_NEW_BUY

    def test_clean_buy_executable_new(self):
        raw = {"strategy_buy": [
            {"ticker": "035720.KS", "name": "카카오", "account": "[ISA]",
             "entry_price": "₩40,000", "reason": "RSI 30 과매도 반등 + 거래량 급증"}],
            "strategy_sell": []}
        n = normalize_actions(raw, "KR_BEFORE", {}, {})
        assert len(n["executable_actions"]) == 1
        assert n["executable_actions"][0]["action_type"] == AI_NEW_BUY

    def test_held_buy_is_add(self):
        raw = {"strategy_buy": [
            {"ticker": "005930.KS", "name": "삼성전자", "account": "[일반]",
             "entry_price": "₩60,000", "reason": "추가 매집 적기"}],
            "strategy_sell": []}
        n = normalize_actions(raw, "KR_BEFORE", {}, {"005930.KS": {"shares": 10}})
        assert n["executable_actions"][0]["action_type"] == AI_ADD_BUY

    def test_pullback_strategy_type_conditional(self):
        raw = {"strategy_buy": [
            {"ticker": "183300.KQ", "name": "코미코", "account": "[ISA]",
             "entry_price": "₩110,000", "strategy_type": "신규진입", "reason": "발굴주"}],
            "strategy_sell": []}
        n = normalize_actions(raw, "KR_BEFORE", {}, {})
        assert len(n["conditional_buy_candidates"]) == 1


class TestNormalizeSell:
    @pytest.mark.parametrize("phrase,expect", [
        ("매도 취소", CANCEL_SELL), ("홀딩 전환", HOLD_REVIEW),
        ("매도 보류", CANCEL_SELL), ("홀딩 유지", HOLD_REVIEW),
        ("무효화 조건 충족", CANCEL_SELL), ("전량 매도 부적절", CANCEL_SELL),
        ("잔여 보유", HOLD_REVIEW),
    ])
    def test_sell_cancel_phrases(self, phrase, expect):
        raw = {"strategy_buy": [], "strategy_sell": [
            {"ticker": "MU", "name": "마이크론", "current_price": "$900",
             "reason": f"기존 포지션 {phrase}"}]}
        n = normalize_actions(raw, "KR_BEFORE", {}, {})
        assert len(n["executable_actions"]) == 0
        assert len(n["cancelled_sells"]) == 1
        assert n["cancelled_sells"][0]["action_type"] == expect

    def test_clean_sell_executable(self):
        raw = {"strategy_buy": [], "strategy_sell": [
            {"ticker": "LMT", "name": "록히드", "current_price": "$540",
             "take_profit": "$560", "reason": "RSI 75 과열 부분 익절"}]}
        n = normalize_actions(raw, "KR_BEFORE", {}, {})
        assert len(n["executable_actions"]) == 1
        assert n["executable_actions"][0]["action_type"] == AI_SELL_MANAGEMENT


class TestNoBuyReason:
    def test_no_buy_reason_populated(self):
        raw = {"strategy_buy": [], "strategy_sell": [],
               "next_action": "FOMC 통과 후 재검토"}
        n = normalize_actions(raw, "KR_BEFORE", {}, {})
        assert n["no_buy_reason"] == "FOMC 통과 후 재검토"

    def test_conditional_buy_suppresses_no_buy_reason(self):
        raw = {"strategy_buy": [
            {"ticker": "X", "name": "Y", "entry_price": "₩1", "reason": "눌림목 대기"}],
            "strategy_sell": []}
        n = normalize_actions(raw, "KR_BEFORE", {}, {})
        assert n["no_buy_reason"] == ""  # 조건부 후보 있으면 사유 비움


class TestSaveIntegration:
    """저장 시 action_type/briefing_type/original_signal 무결성."""

    def _cleanup(self, tickers):
        from core.memory import _get_conn
        conn = _get_conn()
        for tk in tickers:
            conn.execute(
                "DELETE FROM predictions WHERE ticker=? AND created_at >= datetime('now','-2 minutes')",
                (tk,))
        conn.commit()

    def test_save_fills_required_fields(self):
        from core.action_normalizer import normalize_actions
        from core.memory import save_predictions_from_briefing, _get_conn
        raw = {
            "strategy_buy": [
                {"ticker": "035720.KS", "name": "카카오", "account": "[ISA]",
                 "entry_price": "₩40,000", "target_price": "₩44,000", "stop_loss": "₩38,000",
                 "risk_reward": 2.0, "invalidation_condition": "OBV매도", "reason": "즉시 진입"}],
            "strategy_sell": [
                {"ticker": "MU", "name": "마이크론", "current_price": "$900",
                 "reason": "홀딩 전환"}],
        }
        prices = {"035720.KS": 40500, "MU": 880}
        norm = normalize_actions(raw, "KR_BEFORE", prices, {})
        save_predictions_from_briefing(raw, current_prices=prices,
                                       briefing_type="KR_BEFORE", normalized=norm)
        conn = _get_conn()
        rows = conn.execute(
            """SELECT name, signal, original_signal, action_type, briefing_type, account_type
               FROM predictions WHERE ticker IN ('035720.KS','MU')
               AND created_at >= datetime('now','-2 minutes')"""
        ).fetchall()
        by_name = {r[0]: r for r in rows}
        # 카카오: 매수 실행
        k = by_name.get("카카오")
        assert k and k[3] == "AI_NEW_BUY" and k[4] == "KR_BEFORE" and k[5] == "ISA"
        assert k[1] == "매수" and k[2] == "매수"
        # MU: 홀딩 전환 → signal=관망 (매도로 저장 안 됨)
        m = by_name.get("마이크론")
        assert m and m[3] == "HOLD_REVIEW" and m[1] == "관망"
        self._cleanup(["035720.KS", "MU"])

    def test_no_normalized_saves_nothing(self):
        from core.memory import save_predictions_from_briefing
        raw = {"strategy_buy": [{"ticker": "X", "name": "Y"}]}
        assert save_predictions_from_briefing(raw, normalized=None) == 0


class TestTelegram4Sections:
    """텔레그램 4섹션 — 실행 매도가 있어도 조건부 매수 섹션이 숨지 않음."""

    def _msg(self, raw, briefing_type="KR_BEFORE"):
        from core.models import BriefingResult
        from core.telegram import _build_impact_message
        raw["normalized"] = normalize_actions(raw, briefing_type, {}, {})
        result = BriefingResult(title="t", raw_json=raw)
        return _build_impact_message(result, raw, "🇰🇷", "테스트", briefing_type)

    def test_all_four_sections_present(self):
        raw = {
            "strategy_buy": [
                {"ticker": "035720.KS", "name": "카카오", "account": "[ISA]",
                 "entry_price": "₩40,000", "reason": "즉시 진입"},
                {"ticker": "091160.KS", "name": "KODEX 반도체", "account": "[RIA]",
                 "entry_price": "₩166,500", "reason": "추격 금지 눌림목"},
            ],
            "strategy_sell": [
                {"ticker": "MU", "name": "마이크론", "current_price": "$900",
                 "reason": "홀딩 전환"}],
            "next_action": "관망",
        }
        msg = self._msg(raw)
        assert "⚡ *오늘 실제 실행*" in msg
        assert "🕐 *조건부 매수 후보*" in msg
        assert "🟡 *매도 취소·홀딩 전환*" in msg

    def test_conditional_not_hidden_by_executable_sell(self):
        """실행 매도가 있어도 조건부 매수 후보 섹션이 보여야 함."""
        raw = {
            "strategy_buy": [
                {"ticker": "091160.KS", "name": "KODEX 반도체", "account": "[RIA]",
                 "entry_price": "₩166,500", "reason": "눌림목 대기"}],
            "strategy_sell": [
                {"ticker": "LMT", "name": "록히드", "current_price": "$540",
                 "take_profit": "$560", "reason": "RSI 75 과열 부분 익절"}],
        }
        msg = self._msg(raw)
        assert "🕐 *조건부 매수 후보*" in msg and "KODEX 반도체" in msg
        assert "오늘 실제 실행" in msg and "록히드" in msg

    def test_no_buy_reason_section(self):
        raw = {"strategy_buy": [], "strategy_sell": [], "next_action": "FOMC 대기"}
        msg = self._msg(raw)
        assert "🔍 *매수 후보 없음 사유*" in msg


class TestPriceGapNote:
    """예약매수 현재가 대비 괴리율 안내 (가격 오류 오인 방지)."""

    def _gap_msg(self, ticker, entry, cur, reason="눌림목 대기"):
        from core.models import BriefingResult
        from core.telegram import _build_impact_message
        raw = {"strategy_buy": [{"ticker": ticker, "name": "테스트종목",
                                 "account": "[RIA]", "entry_price": entry,
                                 "target_price": "₩145,000", "reason": reason}],
               "strategy_sell": []}
        raw["normalized"] = normalize_actions(raw, "KR_BEFORE", {ticker: cur}, {})
        return _build_impact_message(BriefingResult(title="t", raw_json=raw),
                                     raw, "🇰🇷", "t", "KR_BEFORE")

    def test_pullback_shows_negative_gap(self):
        # current=143000, entry=138000 → -3.5% 진짜 눌림목 → 조건부 + 괴리 표시
        msg = self._gap_msg("069500.KS", "₩138,000", 143000)
        assert "현재가 대비:" in msg
        assert "미체결 가능" in msg
        assert "현재가: 143,000" in msg

    def test_near_price_not_conditional(self):
        # entry=138000, current=139550 (-1.1%) → 눌림목 아님 → 조건부 섹션에 없음
        from core.action_normalizer import normalize_actions
        raw = {"strategy_buy": [{"ticker": "069500.KS", "name": "테스트종목",
                                 "account": "[RIA]", "entry_price": "₩138,000",
                                 "reason": "눌림목 대기"}], "strategy_sell": []}
        n = normalize_actions(raw, "KR_BEFORE", {"069500.KS": 139550}, {})
        assert len(n["conditional_buy_candidates"]) == 0  # 너무 근접 → 눌림목 아님

    def test_chase_above_current_blocked_or_executable(self):
        # entry > current → 즉시 체결, 조건부 섹션 금지
        from core.action_normalizer import normalize_actions
        raw = {"strategy_buy": [{"ticker": "069500.KS", "name": "테스트종목",
                                 "account": "[RIA]", "entry_price": "₩141,000",
                                 "reason": "눌림목 대기"}], "strategy_sell": []}
        n = normalize_actions(raw, "KR_BEFORE", {"069500.KS": 139550}, {})
        assert len(n["conditional_buy_candidates"]) == 0

    def test_no_price_no_gap_note(self):
        # 현재가 없으면 괴리 안내 생략 (에러 없이)
        from core.action_normalizer import normalize_actions
        raw = {"strategy_buy": [{"ticker": "X", "name": "Y",
                                 "entry_price": "₩100", "reason": "눌림목"}],
               "strategy_sell": []}
        n = normalize_actions(raw, "KR_BEFORE", {}, {})
        assert "gap_note" not in n["conditional_buy_candidates"][0]


class TestConditionalBuyCard:
    """조건부 매수 8필드 주문 카드 (계좌/지정가/수량/총액/괴리/조건/미체결)."""

    def _card(self, account="[RIA]", entry="₩138,000", cur=143000, conf="55"):
        # cur=143000이면 138000은 -3.5% 진짜 눌림목 (게이트 통과)
        from core.models import BriefingResult
        from core.telegram import _build_impact_message
        raw = {"strategy_buy": [{"ticker": "069500.KS", "name": "KODEX 200",
                                 "account": account, "entry_price": entry,
                                 "target_price": "₩145,000", "confidence": conf,
                                 "reason": "눌림목 대기"}],
               "strategy_sell": []}
        raw["normalized"] = normalize_actions(raw, "KR_BEFORE", {"069500.KS": cur}, {})
        return raw["normalized"]["conditional_buy_candidates"][0], \
            _build_impact_message(BriefingResult(title="t", raw_json=raw),
                                  raw, "🇰🇷", "t", "KR_BEFORE")

    def test_card_has_all_fields(self):
        c, msg = self._card()
        assert "[RIA]" in msg and "KODEX 200" in msg
        assert "지정가: 138,000원" in msg
        assert "수량: 7주" in msg
        assert "총액: 966,000원" in msg
        assert "현재가 대비:" in msg
        assert "이하 눌림목 도달 시만 체결" in msg
        assert "미체결 가능" in msg

    def test_qty_from_budget_conf55(self):
        c, _ = self._card(conf="55")
        assert c["qty_num"] == 7  # 100만 / 138000 = 7

    def test_qty_low_conf_smaller_budget(self):
        c, _ = self._card(conf="35")
        assert c["qty_num"] == 4  # 60만 / 138000 = 4

    def test_ai_shares_respected(self):
        raw = {"strategy_buy": [{"ticker": "069500.KS", "name": "KODEX 200",
                                 "account": "[RIA]", "entry_price": "₩138,000",
                                 "shares": "10주", "reason": "눌림목"}],
               "strategy_sell": []}
        raw["normalized"] = normalize_actions(raw, "KR_BEFORE", {"069500.KS": 143000}, {})
        c = raw["normalized"]["conditional_buy_candidates"][0]
        assert c["qty_num"] == 10 and c["qty_source"] == "ai"

    def test_shortage_when_qty_zero(self):
        # 고가주 → 예산 부족 (cur=730000이면 700000은 -4.1% 눌림목)
        c, msg = self._card(entry="₩700,000", cur=730000, conf="35")
        assert c["shortage"] is True
        assert "예산 부족/가격 과대" in msg


class TestOrderIntegrityGates:
    """주문 정합성/실시간 무효화 게이트 (2026-06-17 HPSP 사례)."""

    def _buy(self, entry, cur, qty=None, summary="", inval="", strat="신규진입",
             reason="눌림목 대기", total_assets=0.0):
        row = {"ticker": "403870.KS", "name": "HPSP", "account": "[일반]",
               "entry_price": entry, "strategy_type": strat, "reason": reason}
        if qty:
            row["shares"] = qty
        if inval:
            row["invalidation_price"] = inval
        raw = {"strategy_buy": [row], "strategy_sell": []}
        if summary:
            raw["market_summary"] = summary
        return normalize_actions(raw, "KR_BEFORE", {"403870.KS": cur}, {},
                                 total_assets=total_assets)

    # 케이스1: current=69000, limit=70000, qty=70, summary=오늘 실행 없음 → BLOCKED
    def test_immediate_fill_event_wait_blocked(self):
        n = self._buy("₩70,000", 69000, qty="70주", summary="오늘 실행 없음. FOMC 대기.")
        assert len(n["executable_actions"]) == 0
        assert len(n["conditional_buy_candidates"]) == 0
        assert len(n["blocked_buys"]) == 1
        assert n["integrity_errors"]

    # 케이스2: current=69000, limit=66000 → conditional 허용
    def test_real_pullback_allowed(self):
        n = self._buy("₩66,000", 69000, qty="10주")  # 66000 ≤ 69000×0.97=66930
        assert len(n["conditional_buy_candidates"]) == 1
        assert len(n["blocked_buys"]) == 0

    # 케이스3: current=69000, limit=68900 → 즉시 체결, 조건부 섹션 금지
    def test_near_price_not_pullback(self):
        n = self._buy("₩68,900", 69000, qty="10주")  # 68900 ≥ 69000×0.995=68655
        assert len(n["conditional_buy_candidates"]) == 0  # 눌림목 아님
        # 이벤트대기 없으면 executable 또는 blocked (즉시체결)
        assert (len(n["executable_actions"]) == 1) or (len(n["blocked_buys"]) == 1)

    # 케이스4: current=65300, invalidation=65400 → BLOCKED (지지선 이탈)
    def test_invalidation_breach_blocked(self):
        n = self._buy("₩66,000", 65300, qty="10주", inval="₩65,400")
        assert len(n["blocked_buys"]) == 1
        assert "무효화" in n["blocked_buys"][0]["block_reason"]
        assert len(n["conditional_buy_candidates"]) == 0

    # 케이스5: summary=FOMC 대기인데 executable/즉시체결 존재 → integrity fail
    def test_event_wait_conflict_integrity_error(self):
        n = self._buy("₩70,000", 69000, qty="10주", summary="FOMC 대기 신규 진입 보류")
        assert n["integrity_errors"]
        assert len(n["executable_actions"]) == 0

    # 케이스6: 총액 490만 large_order + event_wait → BLOCKED
    def test_large_order_event_wait_blocked(self):
        # limit 70000 × 70주 = 490만. 진짜 눌림목가(67000)로 두되 event_wait
        n = self._buy("₩67,000", 70000, qty="70주", summary="FOMC 대기",
                      inval="₩60,000")
        # 67000 ≤ 70000×0.97=67900 → 눌림목. 총액 469만 ≥ 400만 → large + event_wait → blocked
        assert len(n["blocked_buys"]) == 1
        assert "대량주문" in n["blocked_buys"][0]["block_reason"]


class TestBuyFocusMode:
    """BUY_FOCUS_MODE는 매도 차단 플래그가 아니다."""

    def test_clean_sell_stays_management_under_buy_focus(self, monkeypatch):
        import config.settings as st
        monkeypatch.setattr(st, "BUY_FOCUS_MODE", True, raising=False)
        raw = {"strategy_buy": [], "strategy_sell": [
            {"ticker": "LMT", "name": "록히드", "current_price": "$540",
             "take_profit": "$560", "reason": "과열 부분 익절"}]}
        n = normalize_actions(raw, "KR_BEFORE", {}, {})
        assert len(n["executable_actions"]) == 1
        assert n["executable_actions"][0]["action_type"] == AI_SELL_MANAGEMENT

    def test_cancel_sell_classified_under_buy_focus(self, monkeypatch):
        import config.settings as st
        monkeypatch.setattr(st, "BUY_FOCUS_MODE", True, raising=False)
        raw = {"strategy_buy": [], "strategy_sell": [
            {"ticker": "MU", "name": "마이크론", "current_price": "$900",
             "reason": "홀딩 전환"}]}
        n = normalize_actions(raw, "KR_BEFORE", {}, {})
        assert len(n["cancelled_sells"]) == 1
        assert n["cancelled_sells"][0]["action_type"] == HOLD_REVIEW


class TestDBIntegrity:
    """운영 DB 무결성 — 최근 24시간 신규 저장 기준 모순 0건.

    과거 오염 데이터는 제외하고, normalizer 도입 이후 저장(briefing_type 채워진 건)만 검사.
    """

    def _conn(self):
        from config.settings import DB_DIR
        c = sqlite3.connect(DB_DIR / "memory.db")
        c.row_factory = sqlite3.Row
        return c

    def test_immediate_action_no_block_phrase(self):
        """IMMEDIATE_ACTION인데 reasoning에 '추격 금지/조건 미충족' (briefing_type 있는 신규분)."""
        conn = self._conn()
        for phrase in ("추격 금지", "조건 미충족"):
            n = conn.execute(
                """SELECT COUNT(*) FROM predictions
                   WHERE action_grade='IMMEDIATE_ACTION' AND briefing_type != ''
                     AND reasoning LIKE ?""", (f"%{phrase}%",)).fetchone()[0]
            assert n == 0, f"IMMEDIATE_ACTION + '{phrase}' {n}건"

    def test_sell_signal_no_cancel_phrase(self):
        """signal='매도'인데 reasoning에 '매도 취소/홀딩 전환' (briefing_type 있는 신규분)."""
        conn = self._conn()
        for phrase in ("매도 취소", "홀딩 전환"):
            n = conn.execute(
                """SELECT COUNT(*) FROM predictions
                   WHERE signal='매도' AND briefing_type != ''
                     AND reasoning LIKE ?""", (f"%{phrase}%",)).fetchone()[0]
            assert n == 0, f"signal=매도 + '{phrase}' {n}건"
