"""
시장 모니터 엔진 — 2-tier 실시간 감시 시스템

Tier 1 (무료): 5분 간격 yfinance 수치 체크 (VIX, RSI, 가격 변동)
Tier 2 (유료): 트리거 발동 시에만 Claude Haiku AI 분석
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from config.settings import (
    KR_PORTFOLIO,
    KST,
    MONITOR_INTERVAL_SEC,
    PORTFOLIO,
    PRICE_CHANGE_THRESHOLD,
    RSI_HIGH_THRESHOLD,
    RSI_LOW_THRESHOLD,
    US_PORTFOLIO,
    VIX_THRESHOLD,
)
from core.monitor_models import AlertResult, AlertTrigger, Severity, TriggerType

log = logging.getLogger(__name__)

AI_ANALYSIS_DEFAULT_MODEL = "haiku"
AI_ANALYSIS_HIGH_STAKES_MODEL = "sonnet"
AI_ANALYSIS_COOLDOWN_SEC = 60 * 60  # same ticker/type: at most once per hour
AI_FAILURE_WINDOW_SEC = 30 * 60
AI_FAILURE_CIRCUIT_THRESHOLD = 3
AI_ANALYSIS_TIMEOUT_SEC = 45


class MarketMonitor:
    """2-tier 시장 감시 엔진."""

    def __init__(self) -> None:
        self._running: bool = False
        self._active_alerts: set[str] = set()  # 현재 발동 중인 알림 키
        self._last_scan: datetime | None = None
        self._ai_last_called: dict[str, float] = {}
        self._ai_failure_times: list[float] = []

    def run(self) -> None:
        """메인 감시 루프 — 주문 가능 시간 기준 (미국 프리/애프터 포함)."""
        from core.market_hours import is_any_market_tradeable, next_tradeable_session

        self._running = True
        log.info(f"시장 모니터 시작 (간격: {MONITOR_INTERVAL_SEC}초)")

        while self._running:
            try:
                now = datetime.now(KST)

                # 보조 태스크 — market tradeable gate 밖에서 항상 실행.
                # (각자 내부 스로틀/dedup/장중 게이트 보유 — 장외에 통째로 막히면
                #  KST 16시 일일 리포트·DART 장외 공시 등이 유실됨)
                self._run_auxiliary_tasks(now)

                if not is_any_market_tradeable(now):
                    next_open = next_tradeable_session(now)
                    wait_sec = (next_open - now).total_seconds()
                    # 장외에도 보조 태스크(일일 리포트/DART)는 돌아야 하므로 최대 10분 대기
                    wait_sec = max(60, min(wait_sec, 600))
                    log.info(f"주문 가능 시간 아님 — {wait_sec:.0f}초 대기 (다음: {next_open.strftime('%H:%M')})")
                    self._sleep(wait_sec)
                    continue

                triggers = self._scan_all()
                self._last_scan = now

                # 현재 스캔에서 발동된 키 수집
                current_keys: set[str] = set()
                for trigger in triggers:
                    key = self._alert_key(trigger)
                    current_keys.add(key)

                    if key in self._active_alerts:
                        # 이미 알림 보낸 상태 → 스킵
                        continue
                    # 새로 발동 → AI 분석 후 액션 가능한 경우만 전송
                    result = self._process_trigger(trigger)
                    if self._is_actionable(result):
                        self._send_alert(result)
                        self._record_alert_history(result, delivered=True)
                    else:
                        log.info(
                            "알림 억제 (비액션): %s %s — %s",
                            trigger.ticker,
                            trigger.trigger_type.value,
                            result.severity.value,
                        )
                        self._record_alert_history(
                            result, delivered=False, suppress_reason="non_actionable",
                        )
                    # 비액션/빈응답도 조건이 유지되는 동안 재분석하지 않는다.
                    # 이전에는 전송 성공시에만 active 처리되어, 같은 급락/목표가 조건이
                    # 매 스캔마다 Claude CLI를 다시 호출하며 토큰을 소모했다.
                    self._active_alerts.add(key)

                # 조건 해소된 알림 제거 (다음에 다시 발동하면 재전송)
                self._active_alerts -= (self._active_alerts - current_keys)

                self._sleep(MONITOR_INTERVAL_SEC)

            except Exception as e:
                log.error(f"모니터 오류: {e}")
                self._sleep(60)

    def _run_auxiliary_tasks(self, now: datetime) -> None:
        """장 시간 게이트와 무관하게 매 루프 실행하는 보조 태스크.

        가격 트리거 스캔(_scan_all)만 market tradeable gate 안에서 돌고,
        아래 태스크는 각자 내부 스로틀/dedup/장중 조건을 갖고 있으므로
        gate 밖에서 호출한다. 개별 실패는 루프를 중단시키지 않는다.
        """
        # Toss 미체결/exit 감시 (read-only, 내부 30분 스로틀)
        try:
            from core.toss_order_watch import run_toss_order_watch
            run_toss_order_watch(now=now)
        except Exception as e:
            log.warning(f"toss order watch 실패: {e}")

        # Toss 자율 파이프라인 — PASS_EXECUTE 후보 자동 preview/검증/판정
        # (내부 스로틀 기본 10분(env TOSS_PIPELINE_INTERVAL_MIN) + KR 장중 + autonomous mode 게이트)
        try:
            from core.toss_autonomous_pipeline import run_toss_autonomous_pipeline
            run_toss_autonomous_pipeline(now=now)
        except Exception as e:
            log.warning(f"toss autonomous pipeline 실패: {e}")

        # 보유 포지션 일일 재평가 — 손절/익절 기준 초과 시 자동 매도 후보
        # (KST 10시 이후 1일 1회, 내부 dedup)
        try:
            from core.toss_position_review import run_toss_position_review
            run_toss_position_review(now=now)
        except Exception as e:
            log.warning(f"toss position review 실패: {e}")

        # 자율매매 일일 리포트 — 가동률 KPI + 파이프라인 결과 + 미거래 진단
        # (KST 16시 이후 1일 1회, 내부 dedup — 장외라도 16시 이후 첫 루프에서 발송)
        try:
            from core.toss_autonomous_pipeline import send_daily_pipeline_report
            send_daily_pipeline_report(now=now)
        except Exception as e:
            log.warning(f"toss daily pipeline report 실패: {e}")

        # DART 공시 모니터 — 보유종목 리스크 공시 알림
        # (DART_API_KEY 필요, 내부 30분 스로틀 + rcept_no dedup — 장외 공시도 감시)
        try:
            from core.dart_monitor import run_dart_monitor
            run_dart_monitor(now=now)
        except Exception as e:
            log.warning(f"dart monitor 실패: {e}")

    def stop(self) -> None:
        """감시 종료."""
        self._running = False
        log.info("시장 모니터 종료")

    @property
    def last_scan(self) -> datetime | None:
        return self._last_scan

    @property
    def active_cooldowns(self) -> dict[str, datetime]:
        """하위 호환용 — 활성 알림 키 반환."""
        now = datetime.now(KST)
        return {k: now for k in self._active_alerts}

    # ─── Tier 1: 무료 수치 체크 ───────────────────────

    def _scan_all(self) -> list[AlertTrigger]:
        """주문 가능 시간 기준으로 스캔.

        한국장: 정규장(09:00~15:30)에만 스캔 (ALLOW_KR_AFTER_HOURS_ALERT=false)
        미국장: 프리마켓+정규장+애프터마켓 모두 스캔 (주문 가능)
        """
        from core.market_hours import get_market_session, KR_REGULAR, US_PREMARKET, US_REGULAR, US_AFTERMARKET, CLOSED
        from config.settings import ALLOW_KR_AFTER_HOURS_ALERT

        triggers: list[AlertTrigger] = []
        now = datetime.now(KST)
        session = get_market_session(now)
        kr_session = session["kr"]
        us_session = session["us"]

        kr_tradeable = kr_session == KR_REGULAR or (ALLOW_KR_AFTER_HOURS_ALERT and kr_session != CLOSED)
        us_tradeable = us_session in (US_PREMARKET, US_REGULAR, US_AFTERMARKET)

        # VIX — 미국 주문 가능 시간
        if us_tradeable:
            vix_trigger = self._check_vix(now)
            if vix_trigger:
                triggers.append(AlertTrigger(
                    ticker=vix_trigger.ticker, name=vix_trigger.name,
                    trigger_type=vix_trigger.trigger_type,
                    current_value=vix_trigger.current_value,
                    threshold=vix_trigger.threshold,
                    timestamp=vix_trigger.timestamp,
                    market_session=us_session,
                ))

        # 환율 — 어느 시장이든 주문 가능하면 체크
        if kr_tradeable or us_tradeable:
            fx_trigger = self._check_fx_change(now)
            if fx_trigger:
                triggers.append(AlertTrigger(
                    ticker=fx_trigger.ticker, name=fx_trigger.name,
                    trigger_type=fx_trigger.trigger_type,
                    current_value=fx_trigger.current_value,
                    threshold=fx_trigger.threshold,
                    timestamp=fx_trigger.timestamp,
                    market_session=kr_session if kr_tradeable else us_session,
                ))

        # 종목 스캔
        scan_targets: dict[str, str] = {}
        scan_sessions: dict[str, str] = {}  # ticker → session

        if kr_tradeable:
            for tk, nm in KR_PORTFOLIO.items():
                scan_targets[tk] = nm
                scan_sessions[tk] = kr_session
        if us_tradeable:
            for tk, nm in US_PORTFOLIO.items():
                scan_targets[tk] = nm
                scan_sessions[tk] = us_session

        for ticker, name in scan_targets.items():
            sess = scan_sessions.get(ticker, CLOSED)

            rsi_trigger = self._check_rsi(ticker, name, now)
            if rsi_trigger:
                triggers.append(AlertTrigger(
                    ticker=rsi_trigger.ticker, name=rsi_trigger.name,
                    trigger_type=rsi_trigger.trigger_type,
                    current_value=rsi_trigger.current_value,
                    threshold=rsi_trigger.threshold,
                    timestamp=rsi_trigger.timestamp,
                    market_session=sess,
                ))

            price_trigger = self._check_price_change(ticker, name, now)
            if price_trigger:
                triggers.append(AlertTrigger(
                    ticker=price_trigger.ticker, name=price_trigger.name,
                    trigger_type=price_trigger.trigger_type,
                    current_value=price_trigger.current_value,
                    threshold=price_trigger.threshold,
                    timestamp=price_trigger.timestamp,
                    market_session=sess,
                ))

            time.sleep(0.1)

        # 목표가/손절가 체크
        target_triggers = self._check_price_targets(now)
        triggers.extend(target_triggers)

        # 사용자 지정 가격 알림 (PRICE_ALERTS — 재진입/돌파 트리거)
        alert_triggers = self._check_price_alerts(now, kr_tradeable, us_tradeable, kr_session, us_session)
        triggers.extend(alert_triggers)

        # 미결 예약(비보유)의 무효화 조건 감시 — 셋업 붕괴 시 예약 취소 알림
        invalidation_triggers = self._check_invalidation_alerts(now, kr_tradeable, us_tradeable, kr_session, us_session)
        triggers.extend(invalidation_triggers)

        if triggers:
            log.info(f"트리거 {len(triggers)}건 감지 (KR={kr_session}, US={us_session})")
        return triggers

    def _check_fx_change(self, now: datetime) -> AlertTrigger | None:
        """원달러 환율 급변동 체크."""
        from core.market import _get_quote_realtime
        from config.settings import FX_CHANGE_THRESHOLD

        quote = _get_quote_realtime("USDKRW=X")
        if quote is None or not quote.pct:
            return None

        if abs(quote.pct) >= FX_CHANGE_THRESHOLD:
            return AlertTrigger(
                ticker="USDKRW=X",
                name="원달러 환율",
                trigger_type=TriggerType.FX_CHANGE,
                current_value=quote.pct,
                threshold=FX_CHANGE_THRESHOLD,
                timestamp=now,
            )
        return None

    def _check_vix(self, now: datetime) -> AlertTrigger | None:
        """VIX 급등 체크 — 교차검증 포함."""
        from core.market import _get_quote_realtime

        quote = _get_quote_realtime("^VIX")
        if quote is None:
            return None

        if quote.price >= VIX_THRESHOLD:
            # yfinance로 교차검증
            cross = self._cross_verify_price("^VIX")
            if cross is not None:
                # VIX는 변동률이 아니라 절대값이므로, yfinance 가격 직접 확인
                try:
                    import yfinance as yf
                    yf_price = yf.Ticker("^VIX").fast_info["lastPrice"]
                    if yf_price < VIX_THRESHOLD * 0.85:
                        log.info("VIX 교차검증 불일치: 1차 %.1f vs yf %.1f → 스킵", quote.price, yf_price)
                        return None
                except Exception:
                    pass  # 검증 실패 시 원래 값 신뢰

            return AlertTrigger(
                ticker="^VIX",
                name="VIX",
                trigger_type=TriggerType.VIX_SPIKE,
                current_value=quote.price,
                threshold=VIX_THRESHOLD,
                timestamp=now,
            )
        return None

    def _check_rsi(
        self, ticker: str, name: str, now: datetime,
    ) -> AlertTrigger | None:
        """RSI 과매도/과매수 체크."""
        from core.indicators import calculate_indicators

        result = calculate_indicators(ticker, name)
        if result is None:
            return None

        if result.rsi <= RSI_LOW_THRESHOLD:
            return AlertTrigger(
                ticker=ticker,
                name=name,
                trigger_type=TriggerType.RSI_OVERSOLD,
                current_value=result.rsi,
                threshold=RSI_LOW_THRESHOLD,
                timestamp=now,
            )
        if result.rsi >= RSI_HIGH_THRESHOLD:
            return AlertTrigger(
                ticker=ticker,
                name=name,
                trigger_type=TriggerType.RSI_OVERBOUGHT,
                current_value=result.rsi,
                threshold=RSI_HIGH_THRESHOLD,
                timestamp=now,
            )
        return None

    def _check_price_change(
        self, ticker: str, name: str, now: datetime,
    ) -> AlertTrigger | None:
        """일중 급등/급락 체크 — KIS + yfinance 교차검증."""
        from core.market import _get_quote_realtime

        quote = _get_quote_realtime(ticker)
        if quote is None or quote.pct == 0:
            return None

        pct = quote.pct

        # 임계치 초과 시 교차검증: yfinance로 별도 확인
        if abs(pct) >= PRICE_CHANGE_THRESHOLD:
            cross_pct = self._cross_verify_price(ticker)
            if cross_pct is not None and abs(cross_pct) < PRICE_CHANGE_THRESHOLD * 0.5:
                # yfinance에서 절반도 안 되면 오탐 가능성 → 스킵
                log.info(
                    "교차검증 불일치 [%s]: 1차 %.1f%% vs 2차 %.1f%% → 스킵",
                    ticker, pct, cross_pct,
                )
                return None

        if pct <= -PRICE_CHANGE_THRESHOLD:
            return AlertTrigger(
                ticker=ticker,
                name=name,
                trigger_type=TriggerType.PRICE_DROP,
                current_value=pct,
                threshold=PRICE_CHANGE_THRESHOLD,
                timestamp=now,
            )
        if pct >= PRICE_CHANGE_THRESHOLD:
            return AlertTrigger(
                ticker=ticker,
                name=name,
                trigger_type=TriggerType.PRICE_SURGE,
                current_value=pct,
                threshold=PRICE_CHANGE_THRESHOLD,
                timestamp=now,
            )
        return None

    def _check_price_targets(self, now: datetime) -> list[AlertTrigger]:
        """미결 추천의 목표가/손절가 도달 체크.

        안전장치:
        - 실제 보유 종목만 (HOLDINGS 기준)
        - "매수" 또는 "매도" 시그널만 (관망/홀딩 제외)
        - 7일 이상 된 추천은 무시 (최신 추천만)
        - 같은 종목에 여러 추천 시 가장 최신 것만 사용
        """
        from core.market import _get_quote_realtime

        triggers: list[AlertTrigger] = []

        # 실제 보유 종목만
        from config.settings import (
            HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA,
            HOLDINGS_IRP, HOLDINGS_PENSION,
        )
        held_tickers: set[str] = set()
        for holdings in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
            held_tickers.update(holdings.keys())

        try:
            from core.memory import _get_conn
            conn = _get_conn()
            # 최신 7일 이내 + 매수/매도 시그널만 + 목표가 또는 손절가 있는 것만
            from datetime import timedelta
            cutoff_7d = (datetime.now(KST) - timedelta(days=7)).isoformat()
            rows = conn.execute(
                """SELECT ticker, name, signal, target_price, stop_loss, created_at
                   FROM predictions
                   WHERE status = 'open'
                     AND signal IN ('매수', '매도')
                     AND created_at > ?
                     AND ((target_price IS NOT NULL AND target_price > 0)
                       OR (stop_loss IS NOT NULL AND stop_loss > 0))
                   ORDER BY created_at DESC""",
                (cutoff_7d,),
            ).fetchall()
        except Exception as e:
            log.debug("목표가/손절가 조회 실패: %s", e)
            return triggers

        # 현재 세션 — 주문 불가 시간이면 트리거 생성 금지
        from core.market_hours import get_market_session, KR_REGULAR, US_PREMARKET, US_REGULAR, US_AFTERMARKET, CLOSED
        session = get_market_session(now)

        def _is_kr_ticker(tk: str) -> bool:
            return tk.endswith((".KS", ".KQ"))

        checked: set[str] = set()  # 같은 종목은 최신 1건만
        for row in rows:
            ticker = row["ticker"]
            if ticker in checked:
                continue
            checked.add(ticker)

            if ticker not in held_tickers:
                continue

            # 주문 가능 시간 체크
            is_kr = _is_kr_ticker(ticker)
            if is_kr:
                if session["kr"] != KR_REGULAR:
                    continue  # 한국 정규장 아니면 스킵
                ticker_session = KR_REGULAR
            else:
                if session["us"] not in (US_PREMARKET, US_REGULAR, US_AFTERMARKET):
                    continue  # 미국 주문 불가 시간이면 스킵
                ticker_session = session["us"]

            quote = _get_quote_realtime(ticker)
            if quote is None or quote.price <= 0:
                continue

            name = row["name"]
            target = float(row["target_price"] or 0)
            stop = float(row["stop_loss"] or 0)
            signal = row["signal"]

            # 매수 추천: 현재가 ≥ 목표가 → 익절, 현재가 ≤ 손절가 → 손절
            if signal == "매수":
                if target > 0 and quote.price >= target:
                    triggers.append(AlertTrigger(
                        ticker=ticker, name=name,
                        trigger_type=TriggerType.TARGET_HIT,
                        current_value=quote.price, threshold=target,
                        timestamp=now, market_session=ticker_session,
                    ))
                elif stop > 0 and quote.price <= stop:
                    triggers.append(AlertTrigger(
                        ticker=ticker, name=name,
                        trigger_type=TriggerType.STOP_LOSS_HIT,
                        current_value=quote.price, threshold=stop,
                        timestamp=now, market_session=ticker_session,
                    ))
            # 매도 추천: 현재가 ≤ 목표가 → 익절, 현재가 ≥ 손절가 → 손절
            elif signal == "매도":
                if target > 0 and quote.price <= target:
                    triggers.append(AlertTrigger(
                        ticker=ticker, name=name,
                        trigger_type=TriggerType.TARGET_HIT,
                        current_value=quote.price, threshold=target,
                        timestamp=now, market_session=ticker_session,
                    ))
                elif stop > 0 and quote.price >= stop:
                    triggers.append(AlertTrigger(
                        ticker=ticker, name=name,
                        trigger_type=TriggerType.STOP_LOSS_HIT,
                        current_value=quote.price, threshold=stop,
                        timestamp=now, market_session=ticker_session,
                    ))

            time.sleep(0.1)  # rate limit

        if triggers:
            log.info("목표가/손절가 트리거 %d건 감지", len(triggers))
        return triggers

    def _check_price_alerts(
        self,
        now: datetime,
        kr_tradeable: bool,
        us_tradeable: bool,
        kr_session: str,
        us_session: str,
    ) -> list[AlertTrigger]:
        """사용자 지정 가격 알림 (settings.PRICE_ALERTS).

        브리핑의 재진입/돌파 트리거를 5분 간격 실시간 감시.
        below: 가격 ≤ 기준 → 알림 (눌림목 매수 기회)
        above: 가격 ≥ 기준 → 알림 (돌파 확인)
        지수(^)는 한국/미국 어느 쪽이든 거래 가능 시간이면 체크.
        """
        from config.settings import PRICE_ALERTS
        from core.market import _get_quote_realtime

        # 정적 PRICE_ALERTS + 동적 눌림목 예약(신규진입 미결 추천)을 병합 감시
        alerts = {**PRICE_ALERTS, **_load_pullback_alerts()}

        triggers: list[AlertTrigger] = []
        if not alerts:
            return triggers

        for ticker, cfg in alerts.items():
            is_kr = ticker.endswith((".KS", ".KQ")) or ticker in ("^KS11", "^KQ11")
            is_index = ticker.startswith("^")
            if is_index:
                if not (kr_tradeable or us_tradeable):
                    continue
                sess = kr_session if is_kr else us_session
            elif is_kr:
                if not kr_tradeable:
                    continue
                sess = kr_session
            else:
                if not us_tradeable:
                    continue
                sess = us_session

            quote = _get_quote_realtime(ticker)
            if quote is None or quote.price <= 0:
                continue

            name = cfg.get("name", ticker)
            below = cfg.get("below", 0)
            above = cfg.get("above", 0)

            if below and quote.price <= below:
                triggers.append(AlertTrigger(
                    ticker=ticker, name=f"{name} (지정가 도달: {cfg.get('reason', '')[:40]})",
                    trigger_type=TriggerType.TARGET_HIT,
                    current_value=quote.price, threshold=below,
                    timestamp=now, market_session=sess,
                ))
            elif above and quote.price >= above:
                triggers.append(AlertTrigger(
                    ticker=ticker, name=f"{name} (돌파 확인: {cfg.get('reason', '')[:40]})",
                    trigger_type=TriggerType.TARGET_HIT,
                    current_value=quote.price, threshold=above,
                    timestamp=now, market_session=sess,
                ))
            time.sleep(0.1)

        if triggers:
            log.info("가격 알림 트리거 %d건 (PRICE_ALERTS)", len(triggers))
        return triggers

    def _check_invalidation_alerts(
        self,
        now: datetime,
        kr_tradeable: bool,
        us_tradeable: bool,
        kr_session: str,
        us_session: str,
    ) -> list[AlertTrigger]:
        """비보유 미결 매수 예약(눌림목 예약 등)의 무효화 조건 감시.

        보유 종목 손절은 _check_price_targets가 커버 — 여기는 **비보유** 예약 전용.
        예약을 걸어둔 종목이 손절선 아래로 붕괴하면, 지정가에 이미 체결됐거나
        곧 체결될 위험 → "예약 취소·재평가" 알림을 보낸다.
        """
        from core.market import _get_quote_realtime

        triggers: list[AlertTrigger] = []
        try:
            from datetime import timedelta

            from config.settings import (
                HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA,
                HOLDINGS_IRP, HOLDINGS_PENSION,
            )
            held: set[str] = set()
            for h in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
                held.update(h.keys())

            from core.memory import _get_conn
            cutoff = (datetime.now(KST) - timedelta(days=14)).isoformat()
            rows = _get_conn().execute(
                """SELECT ticker, name, stop_loss, invalidation_condition, created_at
                   FROM predictions
                   WHERE status='open' AND signal='매수'
                     AND stop_loss > 0 AND created_at >= ?
                   ORDER BY created_at DESC""",
                (cutoff,),
            ).fetchall()
        except Exception as e:
            log.debug("무효화 감시 조회 실패: %s", e)
            return triggers

        checked: set[str] = set()
        for row in rows:
            ticker = row["ticker"]
            if ticker in checked or ticker in held:
                continue  # 보유 종목은 _check_price_targets 담당
            checked.add(ticker)

            is_kr = ticker.endswith((".KS", ".KQ"))
            if is_kr:
                if not kr_tradeable:
                    continue
                sess = kr_session
            else:
                if not us_tradeable:
                    continue
                sess = us_session

            quote = _get_quote_realtime(ticker)
            if quote is None or quote.price <= 0:
                continue

            stop = float(row["stop_loss"] or 0)
            if stop > 0 and quote.price <= stop:
                cond = (row["invalidation_condition"] or "")[:60]
                name = row["name"]
                if cond:
                    name = f"{name} (무효화: {cond})"
                triggers.append(AlertTrigger(
                    ticker=ticker, name=name,
                    trigger_type=TriggerType.INVALIDATION,
                    current_value=quote.price, threshold=stop,
                    timestamp=now, market_session=sess,
                ))
            time.sleep(0.1)

        if triggers:
            log.info("무효화 조건 트리거 %d건 (미결 예약)", len(triggers))
        return triggers

    def _cross_verify_price(self, ticker: str) -> float | None:
        """yfinance로 별도 가격 변동률 확인 (교차검증용)."""
        try:
            import yfinance as yf

            fi = yf.Ticker(ticker).fast_info
            price = fi["lastPrice"]
            prev = fi["previousClose"]
            if prev and prev > 0:
                return ((price - prev) / prev) * 100
        except Exception:
            pass
        return None

    # ─── 알림 상태 관리 ─────────────────────────────────

    def _alert_key(self, trigger: AlertTrigger) -> str:
        return f"{trigger.ticker}:{trigger.trigger_type.value}"

    # ─── Tier 2: AI 분석 (트리거 시에만) ───────────────

    def _process_trigger(self, trigger: AlertTrigger) -> AlertResult:
        """트리거에 대해 심각도 판정 + AI 분석."""
        severity = self._classify_severity(trigger)
        ai_analysis = ""

        # INVALIDATION은 매수/매도 판단이 아닌 '예약 취소' 통지 — AI 분석 불필요
        if trigger.trigger_type == TriggerType.INVALIDATION:
            return AlertResult(trigger=trigger, severity=severity)

        # CRITICAL/WARNING 시에만 CLI AI 호출하되, 비용 가드로 반복 호출을 막는다.
        if severity in (Severity.CRITICAL, Severity.WARNING) and self._should_run_ai_analysis(trigger):
            ai_analysis = self._ai_analyze(trigger, self._analysis_model_for(trigger, severity))

        return AlertResult(
            trigger=trigger,
            severity=severity,
            ai_analysis=ai_analysis,
        )


    def _should_run_ai_analysis(self, trigger: AlertTrigger) -> bool:
        """Claude CLI 분석 비용 가드: 동일 트리거 쿨다운 + 실패 서킷브레이커."""
        now = time.time()
        window_start = now - AI_FAILURE_WINDOW_SEC
        self._ai_failure_times = [t for t in self._ai_failure_times if t >= window_start]
        if len(self._ai_failure_times) >= AI_FAILURE_CIRCUIT_THRESHOLD:
            log.warning(
                "CLI 분석 서킷브레이커: 최근 %d분 실패 %d회 → 스킵",
                AI_FAILURE_WINDOW_SEC // 60,
                len(self._ai_failure_times),
            )
            return False

        key = self._alert_key(trigger)
        last = self._ai_last_called.get(key)
        if last is not None and now - last < AI_ANALYSIS_COOLDOWN_SEC:
            remain = int(AI_ANALYSIS_COOLDOWN_SEC - (now - last))
            log.info("CLI 분석 쿨다운: %s — %d초 남음", key, remain)
            return False

        # 호출 전에 기록해 timeout/빈응답도 즉시 재시도 루프를 만들지 않게 한다.
        self._ai_last_called[key] = now
        return True

    def _record_ai_analysis_failure(self) -> None:
        self._ai_failure_times.append(time.time())

    def _analysis_model_for(self, trigger: AlertTrigger, severity: Severity) -> str:
        """위험도별 모델 라우팅: 일반 경고는 haiku, 실제 위험/손절은 sonnet."""
        if severity == Severity.CRITICAL or trigger.trigger_type == TriggerType.STOP_LOSS_HIT:
            return AI_ANALYSIS_HIGH_STAKES_MODEL
        return AI_ANALYSIS_DEFAULT_MODEL

    def _classify_severity(self, trigger: AlertTrigger) -> Severity:
        """트리거 심각도 분류. 발송 여부는 _is_actionable()에서 별도 판단."""
        tt = trigger.trigger_type
        val = abs(trigger.current_value)

        if tt == TriggerType.VIX_SPIKE:
            return Severity.CRITICAL if val >= 40 else Severity.WARNING
        if tt in (TriggerType.PRICE_DROP, TriggerType.PRICE_SURGE):
            return Severity.CRITICAL if val >= 10 else Severity.WARNING
        if tt == TriggerType.FX_CHANGE:
            return Severity.CRITICAL if val >= 1.5 else Severity.WARNING
        if tt == TriggerType.RSI_OVERSOLD:
            return Severity.CRITICAL if val <= 20 else Severity.WARNING
        if tt == TriggerType.RSI_OVERBOUGHT:
            return Severity.INFO  # 과매수는 롱 전략에서 무의미
        if tt == TriggerType.TARGET_HIT:
            return Severity.WARNING  # 익절 검토
        if tt == TriggerType.STOP_LOSS_HIT:
            return Severity.CRITICAL  # 손절은 항상 알림
        if tt == TriggerType.INVALIDATION:
            return Severity.WARNING  # 예약 셋업 붕괴 — 취소 액션 필요

        return Severity.INFO

    def _is_actionable(self, result: AlertResult) -> bool:
        """알림을 실제 전송할지 판단.

        핵심 원칙: AI가 [매수]/[매도] + 거래세션/계좌/주문 정보가 모두 있을 때만 전송.
        CRITICAL이라도 [관망]이거나 주문 정보 누락이면 억제.
        """
        # INFO → 항상 억제
        if result.severity == Severity.INFO:
            log.info("알림 억제: %s — INFO", result.trigger.ticker)
            return False

        # INVALIDATION → AI 주문 필드 게이트 미적용 (매수/매도가 아닌 '예약 취소' 액션)
        if result.trigger.trigger_type == TriggerType.INVALIDATION:
            log.info("알림 전송 결정: %s — invalidation_cancel", result.trigger.ticker)
            return True

        # AI 응답 없으면 억제
        if not result.ai_analysis or not result.ai_analysis.strip():
            log.info("알림 억제: %s — ai_analysis_empty", result.trigger.ticker)
            return False

        first_line = result.ai_analysis.strip().split("\n")[0]

        # [관망]이면 CRITICAL이라도 억제
        if first_line.startswith("[관망]"):
            log.info("알림 억제: %s — watch_only (severity=%s)", result.trigger.ticker, result.severity.value)
            return False

        # [매수] 또는 [매도]인지 확인
        if not (first_line.startswith("[매수]") or first_line.startswith("[매도]")):
            log.info("알림 억제: %s — 매수/매도 아님 (first_line=%s)", result.trigger.ticker, first_line[:30])
            return False

        # 필수 필드 확인: 거래세션/계좌/주문/목표/시계/사유 — 전부 있어야 발송
        # (긴급 액션은 받자마자 주문 입력 가능해야 함 — 하나라도 빠지면 실행 불가)
        analysis = result.ai_analysis
        required = ["거래세션:", "계좌:", "주문:", "목표:", "시계:", "사유:"]
        missing = [f.rstrip(":") for f in required if f not in analysis]

        if missing:
            log.info(
                "알림 억제: %s — missing_order_fields (%s)",
                result.trigger.ticker, ", ".join(missing),
            )
            return False

        # 시계 값 검증 (장기/중기/단기 중 하나)
        import re
        m = re.search(r"시계:\s*(\S+)", analysis)
        if m and not any(h in m.group(1) for h in ("단기", "중기", "장기")):
            log.info("알림 억제: %s — 시계 값 불명 (%s)", result.trigger.ticker, m.group(1)[:10])
            return False

        # 주문에 수량(주)과 가격 기호가 실제로 있는지
        order_m = re.search(r"주문:\s*(.+)", analysis)
        if order_m:
            order_line = order_m.group(1)
            if "주" not in order_line or not re.search(r"[₩$]\s*[\d,]+", order_line):
                log.info("알림 억제: %s — 주문 라인에 수량/가격 누락 (%s)", result.trigger.ticker, order_line[:40])
                return False

        log.info("알림 전송 결정: %s — actionable_order", result.trigger.ticker)
        return True

    def _ai_analyze(self, trigger: AlertTrigger, model: str | None = None) -> str:
        """Claude CLI로 AI 분석 — 즉시 행동할 매수/매도만 판정."""
        import subprocess

        # 보유 종목 정보 수집
        from config.settings import (
            HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_IRP, HOLDINGS_PENSION,
            DEFAULT_CASH, ISA_CASH, HOLDING_STRATEGY,
        )
        holdings_info = ""
        for label, holdings in [("[일반]", HOLDINGS_GENERAL), ("[ISA]", HOLDINGS_ISA)]:
            for tk, info in holdings.items():
                if tk == trigger.ticker:
                    shares = info.get("shares", 0)
                    avg = info.get("avg_cost_krw", info.get("avg_cost_usd", 0))
                    holdings_info += f"\n보유: {label} {shares}주 (매수가 {avg:,.0f})"

        # 투자 시계 (장기 종목은 단기 트리거로 매도 금지)
        strat = HOLDING_STRATEGY.get(trigger.ticker, {})
        horizon_info = ""
        if strat:
            horizon_info = (
                f"\n투자 시계: 〔{strat.get('horizon', '?')}〕 — {strat.get('thesis', '')[:120]}"
            )
            if strat.get("horizon") == "장기":
                horizon_info += (
                    "\n⚠️ 장기 보유 종목 — 단기 등락/RSI만으로 매도 권고 금지. "
                    "보유 논지(thesis) 자체가 훼손된 경우에만 매도. 급락은 오히려 추가 매수 기회로 평가."
                )

        # 세션별 주문 주의사항
        session = trigger.market_session
        session_labels = {
            "KR_REGULAR": "한국 정규장",
            "US_PREMARKET": "미국 프리마켓",
            "US_REGULAR": "미국 정규장",
            "US_AFTERMARKET": "미국 애프터마켓",
        }
        session_label = session_labels.get(session, "")
        session_warning = ""
        if session in ("US_PREMARKET", "US_AFTERMARKET"):
            session_warning = (
                f"\n⚠️ 현재 {session_label} — 유동성 낮음, 스프레드 확대 주의.\n"
                f"- 시장가 주문 금지 → 반드시 지정가\n"
                f"- 수량 과도 금지 (정규장 대비 50% 이하 권장)\n"
            )

        unit = "₩" if trigger.ticker.endswith((".KS", ".KQ")) else "$"
        prompt = (
            f"당신은 실전 투자 어드바이저입니다. 이 알림은 사용자가 받자마자 HTS에 주문을 입력하는 "
            f"'긴급 액션 명령'입니다 — 즉시 행동해야 하는 경우에만 발동합니다.\n\n"
            f"종목: {trigger.name} ({trigger.ticker})\n"
            f"상황: {trigger.description}\n"
            f"현재가: {unit}{trigger.current_value:,.2f}\n"
            f"거래세션: {session_label or '확인 불가'}\n"
            f"시각: {trigger.timestamp.strftime('%Y-%m-%d %H:%M KST')}\n"
            f"{holdings_info}{horizon_info}\n"
            f"일반 예수금: ₩{DEFAULT_CASH:,.0f} | ISA 예수금: ₩{ISA_CASH:,.0f}\n"
            f"{session_warning}\n"
            f"판단 규칙:\n"
            f"- 지금 당장 사거나 팔아야 하는 상황이면 → [매수] 또는 [매도]\n"
            f"- 단순 변동성·경고·모니터링이면 → [관망]\n"
            f"- [관망]이 90% 이상이어야 정상. 진짜 급할 때만 [매수]/[매도].\n"
            f"- 장기 보유 종목의 단기 등락 매도 금지 (위 투자 시계 참조).\n"
            f"- ISA 계좌는 국내주식/국내 ETF만 가능. 매수 금액은 해당 계좌 예수금의 30% 이내.\n\n"
            f"[매수] 또는 [매도] 판단 시 반드시 아래 7줄 형식 그대로 출력 (필드명·순서 변경 금지):\n"
            f"[매수] 또는 [매도]\n"
            f"거래세션: {session_label}\n"
            f"계좌: [일반] 또는 [ISA] 또는 [IRP]\n"
            f"주문: 지정가 {unit}[단일가] × [수량]주 ([금액], 예수금의 [N]%)\n"
            f"목표: {unit}[목표가] ([±N%]) 도달 시 [전량/절반] 매도 — 또는 손절 회피 목적이면 '즉시 청산'\n"
            f"시계: 단기 또는 중기 또는 장기\n"
            f"사유: 왜 지금 즉시 행동해야 하는가 — 트리거 + 핵심 근거 한 줄\n\n"
            f"주의: 주문 가격은 현재가에서 체결 가능한 단일가 (범위 금지). "
            f"목표는 가격 또는 '며칠 내 [조건]' 형태로 반드시 구체화.\n"
            f"[관망] 판단이면 한 줄이면 됩니다: [관망] 단순 변동성, 추가 모니터링.\n"
            f"실수가 있으면 안 됩니다. 확실할 때만 [매수]/[매도]를 내리세요."
        )

        model = model or AI_ANALYSIS_DEFAULT_MODEL
        try:
            result = subprocess.run(
                ["/usr/bin/claude", "-p", prompt, "--model", model],
                capture_output=True,
                text=True,
                timeout=AI_ANALYSIS_TIMEOUT_SEC,
                cwd="/home/kanzaka110/Sanjuk-Stock-Simulator",
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            self._record_ai_analysis_failure()
            log.warning("CLI 분석 실패: model=%s returncode=%d", model, result.returncode)
            return ""
        except subprocess.TimeoutExpired:
            self._record_ai_analysis_failure()
            log.warning("CLI 분석 타임아웃 (%d초)", AI_ANALYSIS_TIMEOUT_SEC)
            return ""
        except Exception as e:
            self._record_ai_analysis_failure()
            log.warning("CLI 분석 오류: %s", e)
            return ""

    # ─── 알림 전송 ────────────────────────────────────

    def _send_alert(self, result: AlertResult) -> None:
        """텔레그램 알림 전송."""
        from core.telegram import send_simple_message

        msg = _build_alert_message(result)
        sent = send_simple_message(msg)
        if sent:
            log.info(f"알림 전송: {result.trigger.ticker} {result.trigger.trigger_type.value}")

    def _record_alert_history(
        self, result: AlertResult, delivered: bool, suppress_reason: str = "",
    ) -> None:
        """알림 이력 저장 (대시보드 /api/alerts/history용) — 실패해도 모니터 무중단."""
        try:
            from core.memory import save_alert

            analysis = (result.ai_analysis or "").strip()
            save_alert({
                "ticker": result.trigger.ticker,
                "name": result.trigger.name,
                "alert_type": result.trigger.trigger_type.value,
                "severity": result.severity.value,
                "title": result.trigger.description,
                "message": analysis[:2000],
                "price": result.trigger.current_value,
                "delivered": delivered,
                "suppress_reason": suppress_reason,
            })
        except Exception as e:
            log.debug("알림 이력 저장 실패: %s", e)

    def _sleep(self, seconds: float) -> None:
        """인터럽트 가능한 sleep."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))


# ─── 동적 눌림목 알림 (신규진입 미결 추천 → 진입가 도달 감시) ───

def _load_pullback_alerts() -> dict:
    """미결 신규진입(눌림목 예약) 추천을 PRICE_ALERTS 형식으로 로드.

    발굴주가 눌림목 예약으로 strategy_buy에 등재되면 predictions에 status=open으로 저장됨.
    그 entry_price(눌림목 지정가)에 현재가가 도달하면 매수 알림 → 발굴이 실제 매수로 완결.
    14일 이내 미결 건만 (오래된 예약은 만료). 실패 시 빈 dict (모니터 중단 없이).
    """
    try:
        from datetime import datetime, timedelta

        from config.settings import KST
        from core.memory import _get_conn

        cutoff = (datetime.now(KST) - timedelta(days=14)).isoformat()
        rows = _get_conn().execute(
            """SELECT ticker, name, entry_price, account_type FROM predictions
               WHERE status='open' AND signal='매수' AND strategy_type='신규진입'
                 AND entry_price > 0 AND created_at >= ?""",
            (cutoff,),
        ).fetchall()
        out: dict = {}
        for r in rows:
            tk = r["ticker"]
            if tk in out:
                continue
            acct = f"[{r['account_type']}] " if r["account_type"] else ""
            out[tk] = {
                "name": r["name"],
                "below": float(r["entry_price"]),  # 눌림목 도달 = 매수 기회
                "reason": f"{acct}눌림목 예약 진입가 도달 — 발굴주 신규진입 검토",
            }
        return out
    except Exception as e:
        log.debug("눌림목 알림 로드 실패: %s", e)
        return {}


# ─── 알림 메시지 포매터 ───────────────────────────────

def _build_alert_message(result: AlertResult) -> str:
    """텔레그램 알림 메시지 생성 — 받자마자 주문 입력 가능한 액션 카드."""
    trigger = result.trigger
    analysis = result.ai_analysis or ""

    # 액션 방향 (게이트 통과 시 [매수]/[매도] 보장 — INVALIDATION은 예약 취소)
    if trigger.trigger_type == TriggerType.INVALIDATION:
        action_title = "⛔ 예약 취소 액션"
    elif analysis.startswith("[매수]"):
        action_title = "🟢 매수 긴급 액션"
    elif analysis.startswith("[매도]"):
        action_title = "🔴 매도 긴급 액션"
    else:
        action_title = "긴급 액션"

    lines: list[str] = []
    lines.append("━" * 24)
    lines.append(f"⚡  *{action_title}*")
    lines.append(f"_{trigger.timestamp.strftime('%Y.%m.%d %H:%M')} KST — 장중, 즉시 실행_")
    lines.append("━" * 24)
    lines.append("")

    # 트리거 정보
    type_icons = {
        TriggerType.VIX_SPIKE: "🔥",
        TriggerType.RSI_OVERSOLD: "📉",
        TriggerType.RSI_OVERBOUGHT: "📈",
        TriggerType.PRICE_DROP: "🔻",
        TriggerType.PRICE_SURGE: "🔺",
        TriggerType.TARGET_HIT: "🎯",
        TriggerType.STOP_LOSS_HIT: "🛑",
        TriggerType.INVALIDATION: "⛔",
        TriggerType.FX_CHANGE: "💱",
    }
    icon = type_icons.get(trigger.trigger_type, "📢")
    lines.append(f"{icon} *{trigger.name}* ({trigger.ticker})")
    lines.append(f"    {trigger.description}")

    # 거래세션 표시
    session_labels = {
        "KR_REGULAR": "🇰🇷 한국 정규장",
        "US_PREMARKET": "🇺🇸 미국 프리마켓",
        "US_REGULAR": "🇺🇸 미국 정규장",
        "US_AFTERMARKET": "🇺🇸 미국 애프터마켓",
    }
    if trigger.market_session and trigger.market_session in session_labels:
        lines.append(f"    거래세션: {session_labels[trigger.market_session]}")
    if trigger.market_session in ("US_PREMARKET", "US_AFTERMARKET"):
        lines.append("    ⚠️ 스프레드·체결 리스크 높음 — 지정가 필수")
    lines.append("")

    # AI 분석 — 액션 카드 (필드별 구조화 출력)
    if analysis:
        is_action = analysis.startswith("[매수]") or analysis.startswith("[매도]")

        if is_action:
            import re

            def _field(name: str) -> str:
                m = re.search(rf"{name}:\s*(.+)", analysis)
                return m.group(1).strip() if m else ""

            lines.append("─" * 24)
            account = _field("계좌")
            order = _field("주문")
            target = _field("목표")
            horizon = _field("시계")
            reason = _field("사유")

            lines.append(f"*{account}  {trigger.name}*  〔{horizon}〕")
            lines.append("")
            lines.append(f"📋 *주문: {order}*")
            lines.append(f"🎯 목표: {target}")
            lines.append(f"💬 사유: {reason}")
            lines.append("")
            # 실행 후 매매 기록 명령을 구체 값으로 미리 구성
            side = "매수" if analysis.startswith("[매수]") else "매도"
            qty_m = re.search(r"×\s*(\d+)\s*주", order)
            price_m = re.search(r"[₩$]\s*([\d,]+(?:\.\d+)?)", order)
            acct_clean = account.strip("[]")
            qty_s = qty_m.group(1) if qty_m else "N"
            price_s = price_m.group(1).replace(",", "") if price_m else "체결가"
            lines.append(
                f"✅ 체결 후 입력: `매매 {trigger.name} {side} {qty_s}주 {price_s} {acct_clean}`"
            )

            # 형식 외 추가 분석이 있으면 뒤에 (7줄 이후)
            extra_lines = analysis.split("\n")[7:]
            extra = "\n".join(l for l in extra_lines if l.strip())
            if extra:
                lines.append("")
                lines.append(extra[:400])
        else:
            # [관망] — 게이트에서 억제되므로 도달하지 않지만 방어
            lines.append("─" * 24)
            lines.append(f"🤖 {analysis[:200]}")
        lines.append("")
    elif trigger.trigger_type == TriggerType.INVALIDATION:
        lines.append("─" * 24)
        lines.append("📋 *액션: 걸어둔 예약매수 주문을 취소하라*")
        lines.append("    셋업 붕괴 — 진입 근거였던 손절선이 이미 깨짐.")
        lines.append("    이미 체결됐다면 손절 기준으로 즉시 재평가.")
        lines.append("")

    lines.append("━" * 24)

    return "\n".join(lines)
