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
    CANCEL_SELL, HOLD_REVIEW, BLOCKED_BUY, normalize_actions,
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

    # 테스트 전용 티커 — 운영 데이터와 절대 충돌 안 함
    _TEST_BUY_TICKER = "ZZTEST_BUY.KS"
    _TEST_SELL_TICKER = "ZZTEST_SELL"

    def _cleanup(self, tickers):
        from core.memory import _get_conn
        conn = _get_conn()
        for tk in tickers:
            conn.execute(
                "DELETE FROM predictions WHERE ticker=?", (tk,))
        conn.commit()

    def setup_method(self):
        self._cleanup([self._TEST_BUY_TICKER, self._TEST_SELL_TICKER])

    def teardown_method(self):
        self._cleanup([self._TEST_BUY_TICKER, self._TEST_SELL_TICKER])

    def test_save_fills_required_fields(self):
        from core.action_normalizer import normalize_actions
        from core.memory import save_predictions_from_briefing, _get_conn
        raw = {
            "strategy_buy": [
                {"ticker": self._TEST_BUY_TICKER, "name": "테스트매수", "account": "[ISA]",
                 "entry_price": "₩40,000", "target_price": "₩44,000", "stop_loss": "₩38,000",
                 "risk_reward": 2.0, "invalidation_condition": "OBV매도", "reason": "즉시 진입"}],
            "strategy_sell": [
                {"ticker": self._TEST_SELL_TICKER, "name": "테스트매도", "current_price": "$900",
                 "reason": "홀딩 전환"}],
        }
        prices = {self._TEST_BUY_TICKER: 40500, self._TEST_SELL_TICKER: 880}
        norm = normalize_actions(raw, "KR_BEFORE", prices, {})
        save_predictions_from_briefing(raw, current_prices=prices,
                                       briefing_type="KR_BEFORE", normalized=norm)
        conn = _get_conn()
        rows = conn.execute(
            """SELECT name, signal, original_signal, action_type, briefing_type, account_type
               FROM predictions WHERE ticker IN (?,?)
               AND created_at >= datetime('now','-2 minutes')""",
            (self._TEST_BUY_TICKER, self._TEST_SELL_TICKER),
        ).fetchall()
        by_name = {r[0]: r for r in rows}
        # 테스트매수: 매수 실행
        k = by_name.get("테스트매수")
        assert k and k[3] == "AI_NEW_BUY" and k[4] == "KR_BEFORE" and k[5] == "ISA"
        assert k[1] == "매수" and k[2] == "매수"
        # 테스트매도: 홀딩 전환 → signal=관망 (매도로 저장 안 됨)
        m = by_name.get("테스트매도")
        assert m and m[3] == "HOLD_REVIEW" and m[1] == "관망"

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


class TestUrgentAlertBlockedBuyFilter:
    """긴급 알림에서 BLOCKED_BUY ticker가 노출되지 않음 (HPSP 실측)."""

    def _build_msg(self, raw, prices, briefing_type="KR_NIGHT"):
        from core.models import BriefingResult, Signal
        raw["normalized"] = normalize_actions(raw, briefing_type, prices, {})
        # 강력 매수 urgency로 buy_signals 생성 (LLM이 "강력 매수"로 태깅한 경우 시뮬레이션)
        buy_sigs = []
        for row in raw.get("strategy_buy", []):
            buy_sigs.append(Signal(
                ticker=row.get("ticker", ""), name=row.get("name", ""),
                signal="매수", urgency="강력 매수",
                entry_price=row.get("entry_price", ""),
                target_price=row.get("target_price", "₩80,000"),
                stop_loss=row.get("stop_loss", "₩55,000"),
                shares=row.get("shares", "70주"),
            ))
        result = BriefingResult(
            title="t", raw_json=raw, buy_signals=tuple(buy_sigs),
        )
        from core.telegram import _build_briefing_message
        ret = _build_briefing_message(result, raw, "🇰🇷", "테스트", briefing_type)
        return ret if isinstance(ret, str) else "\n".join(ret)

    def test_hpsp_blocked_not_in_urgent_alert(self):
        """HPSP 현재가 62,800 / 지정가 70,000 / urgency 강력 매수 → 긴급 알림 미노출."""
        raw = {
            "strategy_buy": [
                {"ticker": "403870.KS", "name": "HPSP", "account": "[일반]",
                 "entry_price": "₩70,000", "strategy_type": "신규진입",
                 "reason": "FOMC 대기 눌림목", "shares": "70주"},
            ],
            "strategy_sell": [],
            "market_summary": "FOMC 대기",
        }
        msg = self._build_msg(raw, {"403870.KS": 62800})
        # HPSP가 매수 실행 / 적극 매수 / 매수 액션 어디에도 나오면 안 됨
        assert "매수 실행:  HPSP" not in msg, "blocked HPSP가 긴급 매수 실행에 노출됨"
        assert "적극 매수:  HPSP" not in msg, "blocked HPSP가 적극 매수에 노출됨"
        assert "매수 액션" not in msg or "HPSP" not in msg.split("매수 액션")[-1].split("━")[0], \
            "blocked HPSP가 매수 액션 섹션에 노출됨"

    def test_hpsp_blocked_decision_fallback_suppressed(self):
        """HPSP blocked + investment_decision='매수실행' → fallback '매수실행' 미표시."""
        raw = {
            "strategy_buy": [
                {"ticker": "403870.KS", "name": "HPSP", "account": "[일반]",
                 "entry_price": "₩70,000", "strategy_type": "신규진입",
                 "reason": "FOMC 대기 눌림목", "shares": "70주"},
            ],
            "strategy_sell": [],
            "market_summary": "FOMC 대기",
        }
        from core.models import BriefingResult, Signal
        raw["normalized"] = normalize_actions(raw, "KR_NIGHT", {"403870.KS": 62800}, {})
        buy_sigs = [Signal(
            ticker="403870.KS", name="HPSP", signal="매수", urgency="강력 매수",
            entry_price="₩70,000", target_price="₩80,000", stop_loss="₩55,000", shares="70주",
        )]
        result = BriefingResult(
            title="t", raw_json=raw, buy_signals=tuple(buy_sigs),
            investment_decision="매수실행",
        )
        from core.telegram import _build_briefing_message
        ret = _build_briefing_message(result, raw, "🇰🇷", "테스트", "KR_NIGHT")
        msg = ret if isinstance(ret, str) else "\n".join(ret)
        # 매수실행 fallback 미표시
        assert "매수실행 — Notion 상세 확인 필요" not in msg, \
            "blocked만 있는데 매수실행 fallback이 표시됨"
        # HPSP가 매수 실행 / 적극 매수에 안 나옴
        assert "매수 실행:  HPSP" not in msg
        assert "적극 매수:  HPSP" not in msg
        # 차단 섹션에는 HPSP 포함
        assert "차단된 매수 후보" in msg
        assert "HPSP" in msg

    def test_hpsp_blocked_next_action_filtered(self):
        """HPSP blocked + next_action='HPSP 매수 검토' → 다음 액션에서 매수 검토 미포함."""
        raw = {
            "strategy_buy": [
                {"ticker": "403870.KS", "name": "HPSP", "account": "[일반]",
                 "entry_price": "₩70,000", "strategy_type": "신규진입",
                 "reason": "FOMC 대기 눌림목", "shares": "70주"},
            ],
            "strategy_sell": [],
            "market_summary": "FOMC 대기",
            "next_action": "①HPSP 매수 검토 ②KODEX 200 적립",
        }
        from core.models import BriefingResult, Signal
        raw["normalized"] = normalize_actions(raw, "KR_NIGHT", {"403870.KS": 62800}, {})
        result = BriefingResult(title="t", raw_json=raw)
        from core.telegram import _build_briefing_message
        ret = _build_briefing_message(result, raw, "🇰🇷", "테스트", "KR_NIGHT")
        msg = ret if isinstance(ret, str) else "\n".join(ret)
        assert "HPSP 매수 검토" not in msg, "blocked HPSP가 다음 액션에서 매수 검토로 노출됨"
        # KODEX 200은 blocked가 아니므로 표시되어야 함
        assert "KODEX 200" in msg

    def test_hpsp_blocked_solo_next_action_omitted(self):
        """next_action='HPSP 매수 검토' 단독 → 다음 액션 섹션 자체 생략."""
        raw = {
            "strategy_buy": [
                {"ticker": "403870.KS", "name": "HPSP", "account": "[일반]",
                 "entry_price": "₩70,000", "strategy_type": "신규진입",
                 "reason": "FOMC 대기 눌림목", "shares": "70주"},
            ],
            "strategy_sell": [],
            "next_action": "HPSP 매수 검토",
        }
        from core.models import BriefingResult
        raw["normalized"] = normalize_actions(raw, "KR_NIGHT", {"403870.KS": 62800}, {})
        result = BriefingResult(title="t", raw_json=raw)
        from core.telegram import _build_briefing_message
        ret = _build_briefing_message(result, raw, "🇰🇷", "테스트", "KR_NIGHT")
        msg = ret if isinstance(ret, str) else "\n".join(ret)
        assert "HPSP 매수 검토" not in msg
        assert "다음 액션" not in msg, "빈 다음 액션 섹션이 표시됨"

    def test_hpsp_blocked_numbered_solo(self):
        """next_action='①HPSP 매수 검토' → 미포함."""
        raw = {
            "strategy_buy": [
                {"ticker": "403870.KS", "name": "HPSP", "account": "[일반]",
                 "entry_price": "₩70,000", "strategy_type": "신규진입",
                 "reason": "눌림목", "shares": "70주"},
            ],
            "strategy_sell": [],
            "next_action": "①HPSP 매수 검토",
        }
        from core.models import BriefingResult
        raw["normalized"] = normalize_actions(raw, "KR_NIGHT", {"403870.KS": 62800}, {})
        result = BriefingResult(title="t", raw_json=raw)
        from core.telegram import _build_briefing_message
        ret = _build_briefing_message(result, raw, "🇰🇷", "테스트", "KR_NIGHT")
        msg = ret if isinstance(ret, str) else "\n".join(ret)
        assert "HPSP 매수 검토" not in msg

    def test_slash_trailing_cleanup(self):
        """'KODEX 200 적립 / HPSP 매수 검토' → 'KODEX 200 적립' trailing / 없음."""
        from core.telegram import _filter_blocked_from_text
        normalized = {"blocked_buys": [{"ticker": "403870.KS", "name": "HPSP"}]}
        result = _filter_blocked_from_text("KODEX 200 적립 / HPSP 매수 검토", normalized)
        assert "KODEX 200 적립" in result
        assert "HPSP" not in result
        assert not result.endswith("/")
        assert not result.endswith(" /")

    def test_hpsp_blocked_night_reason_fallback_filtered(self):
        """KR_NIGHT + HPSP blocked + next_action='HPSP 매수 검토' → reason fallback 미노출."""
        raw = {
            "strategy_buy": [
                {"ticker": "403870.KS", "name": "HPSP", "account": "[일반]",
                 "entry_price": "₩70,000", "strategy_type": "신규진입",
                 "reason": "FOMC 대기 눌림목", "shares": "70주"},
            ],
            "strategy_sell": [],
            "next_action": "HPSP 매수 검토",
            "advisor_oneliner": "HPSP 진입 검토 필요",
        }
        raw["normalized"] = normalize_actions(raw, "KR_NIGHT", {"403870.KS": 62800}, {})
        from core.models import BriefingResult
        result = BriefingResult(title="t", raw_json=raw)
        from core.telegram import _build_impact_message
        msg = _build_impact_message(result, raw, "🇰🇷", "테스트", "KR_NIGHT")
        assert "HPSP 매수 검토" not in msg, "blocked HPSP가 야간 reason fallback에 노출됨"
        assert "HPSP 진입 검토" not in msg, "blocked HPSP가 oneliner fallback에 노출됨"

    def test_hpsp_blocked_only_night_decision_fallback_suppressed(self):
        """KR_NIGHT + HPSP blocked만 존재 + investment_decision='매수실행' → 💬 매수실행 미표시."""
        raw = {
            "strategy_buy": [
                {"ticker": "403870.KS", "name": "HPSP", "account": "[일반]",
                 "entry_price": "₩70,000", "strategy_type": "신규진입",
                 "reason": "FOMC 대기 눌림목", "shares": "70주"},
            ],
            "strategy_sell": [],
            "investment_decision": "매수실행",
        }
        raw["normalized"] = normalize_actions(raw, "KR_NIGHT", {"403870.KS": 62800}, {})
        # blocked만 존재, executable 없음 확인
        assert len(raw["normalized"]["blocked_buys"]) == 1
        assert len(raw["normalized"]["executable_actions"]) == 0
        from core.models import BriefingResult
        result = BriefingResult(title="t", raw_json=raw)
        from core.telegram import _build_impact_message
        msg = _build_impact_message(result, raw, "🇰🇷", "테스트", "KR_NIGHT")
        # 💬 매수실행 fallback 미표시
        assert "💬 매수실행" not in msg, "blocked만 있는데 💬 매수실행 fallback이 표시됨"
        # 내일 예약 주문: 없음은 허용
        assert "내일 예약 주문" in msg
        # HPSP는 차단 섹션에서만 허용
        assert "HPSP" in msg  # 차단 섹션에는 있어야 함

    def test_non_blocked_buy_still_shows_in_urgent(self):
        """정상 매수(지정가 < 현재가)는 긴급 알림에 여전히 노출."""
        raw = {
            "strategy_buy": [
                {"ticker": "005930.KS", "name": "삼성전자", "account": "[일반]",
                 "entry_price": "₩55,000", "strategy_type": "신규진입",
                 "reason": "즉시 진입", "shares": "10주"},
            ],
            "strategy_sell": [],
        }
        msg = self._build_msg(raw, {"005930.KS": 60000})
        # 55,000 < 60,000 → ok_pullback → conditional (강력 매수 urgency가 있어도 normalizer가 conditional로 분류)
        # 또는 executable로 갈 수 있음. 어쨌든 blocked가 아니므로 urgent에 나올 수 있음
        assert "삼성전자" in msg


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


class TestChaseBlockGate:
    """현재가 초과/+3% 추격 지정가 → 무조건 BLOCKED_BUY (2026-06-17 HPSP/한화엔진 사례)."""

    def _buy(self, ticker, name, entry, cur, reason="눌림목 대기", summary="", inval=""):
        row = {"ticker": ticker, "name": name, "account": "[일반]",
               "entry_price": entry, "strategy_type": "신규진입", "reason": reason}
        if inval:
            row["invalidation_price"] = inval
        raw = {"strategy_buy": [row], "strategy_sell": []}
        if summary:
            raw["market_summary"] = summary
        return normalize_actions(raw, "KR_NIGHT", {ticker: cur}, {})

    # HPSP: 현재가 62,800, 지정가 70,000 (+11.5%) → BLOCKED
    def test_hpsp_chase_blocked(self):
        """HPSP 현재가 62,800 / 지정가 70,000 → 현재가 초과 추격, 무조건 차단."""
        n = self._buy("403870.KS", "HPSP", "₩70,000", 62800,
                      reason="FOMC 대기 / 내일장 눌림목", summary="FOMC 대기")
        assert len(n["executable_actions"]) == 0, "executable에 HPSP 있으면 안 됨"
        assert len(n["conditional_buy_candidates"]) == 0, "conditional에 HPSP 있으면 안 됨"
        assert len(n["blocked_buys"]) == 1
        blk = n["blocked_buys"][0]
        assert blk["action_type"] == BLOCKED_BUY
        assert blk.get("chase_blocked") is True
        assert "현재가 초과" in blk["block_reason"]
        assert n["integrity_errors"]

    # HPSP 지정가가 현재가 약간 위 (+1%) → 여전히 BLOCKED
    def test_hpsp_slightly_above_blocked(self):
        """지정가가 현재가보다 1%만 높아도 차단."""
        n = self._buy("403870.KS", "HPSP", "₩63,400", 62800)
        assert len(n["executable_actions"]) == 0
        assert len(n["conditional_buy_candidates"]) == 0
        assert len(n["blocked_buys"]) == 1

    # HPSP 지정가가 현재가 이하 (-5%) → 정상 조건부 통과
    def test_hpsp_real_pullback_allowed(self):
        """지정가 59,660 (현재가 -5%) → 진짜 눌림목, conditional 허용."""
        n = self._buy("403870.KS", "HPSP", "₩59,660", 62800,
                      reason="눌림목 대기", inval="₩57,000")
        assert len(n["conditional_buy_candidates"]) == 1
        assert len(n["blocked_buys"]) == 0

    # 한화엔진: 현재가 65,500, 전일대비 +10.46%, 지정가가 현재가 이상 → BLOCKED
    def test_hanwha_engine_chase_after_surge_blocked(self):
        """한화엔진 급등 후 현재가 65,500, 지정가 66,000 → 추격 차단."""
        n = self._buy("082740.KS", "한화엔진", "₩66,000", 65500,
                      reason="급등 후 추격 금지")
        assert len(n["executable_actions"]) == 0
        assert len(n["conditional_buy_candidates"]) == 0
        assert len(n["blocked_buys"]) == 1
        assert n["blocked_buys"][0].get("chase_blocked") is True

    # 한화엔진: 현재가 65,500, 지정가 70,000 (+6.9%) → +3% 이상 추격 차단
    def test_hanwha_engine_large_gap_blocked(self):
        """지정가가 현재가 대비 +6.9% → 무조건 BLOCKED."""
        n = self._buy("082740.KS", "한화엔진", "₩70,000", 65500)
        assert len(n["blocked_buys"]) == 1
        assert "+6.9%" in n["blocked_buys"][0]["block_reason"] or "+6" in n["blocked_buys"][0]["block_reason"]

    # 한화엔진: 현재가 65,500, 지정가 60,000 (-8.4%) → 진짜 눌림목
    def test_hanwha_engine_real_pullback(self):
        """지정가 60,000 (현재가 -8.4%) → 정상 conditional."""
        n = self._buy("082740.KS", "한화엔진", "₩60,000", 65500,
                      reason="눌림목 대기", inval="₩58,000")
        assert len(n["conditional_buy_candidates"]) == 1
        assert len(n["blocked_buys"]) == 0


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
