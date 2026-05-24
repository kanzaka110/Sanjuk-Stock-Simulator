"""
4/9 한국장 체크리스트 알림 스크립트
cron으로 핵심 시간대에 실행 → 텔레그램 알림
"""

import os
import sys
from pathlib import Path

# 프로젝트 루트 설정
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# .env 로드
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from datetime import datetime
from config.settings import KST, HOLDINGS_GENERAL
from core.market import _get_quote_extended, _get_quote_kis, _get_quote_realtime
from core.telegram import send_simple_message

import yfinance as yf


def get_portfolio_drawdown() -> float:
    """포트폴리오 최고점 대비 현재 낙폭 추정 (단순화)."""
    # 미국 보유
    us_holdings = {
        "NVDA": (46, 132.91),
        "GOOGL": (9, 318.03),
        "MU": (11, 408.82),
        "LMT": (1, 639.00),
    }
    total_val = 0.0
    total_cost = 0.0
    for tk, (sh, avg) in us_holdings.items():
        ext = _get_quote_extended(tk) or _get_quote_kis(tk)
        if ext:
            total_val += ext.price * sh
            total_cost += avg * sh

    if total_cost > 0:
        return (total_val - total_cost) / total_cost * 100
    return 0.0


def check_circuit_breaker() -> tuple[bool, float]:
    """서킷브레이커 해제 여부. (해제됨, 낙폭%)"""
    pnl = get_portfolio_drawdown()
    # 낙폭이 -5% 이상이면 해제 (수익이 양수거나 손실이 -5% 미만)
    return pnl > -5.0, pnl


def check_vix() -> tuple[float, str]:
    """VIX 현재값 + 상태."""
    q = _get_quote_realtime("^VIX")
    if q:
        if q.price < 18:
            return q.price, "안전"
        elif q.price < 22:
            return q.price, "경계"
        else:
            return q.price, "위험"
    return 0.0, "조회실패"


def check_nvda_185() -> tuple[float, bool]:
    """NVDA $185 돌파 여부."""
    ext = _get_quote_extended("NVDA") or _get_quote_kis("NVDA")
    if ext:
        return ext.price, ext.price >= 185.0
    return 0.0, False


def check_lmt_596() -> tuple[float, bool]:
    """LMT $596 이하 손절 경고."""
    ext = _get_quote_extended("LMT") or _get_quote_kis("LMT")
    if ext:
        return ext.price, ext.price <= 596.0
    return 0.0, False


def check_kospi() -> tuple[float, float]:
    """KOSPI 현재가 + 등락률."""
    q = _get_quote_realtime("^KS11")
    if q:
        return q.price, q.pct
    return 0.0, 0.0


def check_foreigner_flow() -> str:
    """외국인 수급 (간단 참고)."""
    try:
        from core.market_kis import get_domestic_price
        q = get_domestic_price("005930")
        if q and q.pct > 0:
            return f"삼성전자 {q.pct:+.2f}% (외국인 방향 참고)"
        elif q:
            return f"삼성전자 {q.pct:+.2f}% (약세)"
    except Exception:
        pass
    return "확인 불가"


def build_alert(phase: str) -> str:
    """시간대별 알림 메시지 생성."""
    now = datetime.now(KST).strftime("%H:%M")

    cb_ok, cb_pnl = check_circuit_breaker()
    vix_val, vix_status = check_vix()
    nvda_price, nvda_185 = check_nvda_185()
    lmt_price, lmt_danger = check_lmt_596()
    kospi, kospi_chg = check_kospi()
    flow = check_foreigner_flow()

    lines = [f"📋 *4/9 체크리스트 알림* ({now} KST)"]
    lines.append(f"━━━━━━━━━━━━━━━")

    # 서킷브레이커
    cb_icon = "✅" if cb_ok else "❌"
    lines.append(f"{cb_icon} 서킷브레이커: {'해제' if cb_ok else '발동 중'} (미국포트 {cb_pnl:+.1f}%)")

    # VIX
    vix_icon = "✅" if vix_val < 18 else ("🔶" if vix_val < 22 else "❌")
    lines.append(f"{vix_icon} VIX: {vix_val:.2f} ({vix_status})")

    # KOSPI
    lines.append(f"📊 KOSPI: {kospi:,.2f} ({kospi_chg:+.2f}%)")

    # 수급
    lines.append(f"📈 수급: {flow}")

    lines.append(f"━━━━━━━━━━━━━━━")

    # NVDA $185
    nvda_icon = "🔥" if nvda_185 else "⏳"
    lines.append(f"{nvda_icon} NVDA: ${nvda_price:.2f} ({'$185 돌파! RIA 20주 익절 검토' if nvda_185 else '$185 미도달'})")

    # LMT $596
    if lmt_danger:
        lines.append(f"🚨 LMT: ${lmt_price:.2f} — $596 이하! 손절 실행 필요!")
    else:
        lines.append(f"✅ LMT: ${lmt_price:.2f} (손절가 $596 대비 안전)")

    lines.append(f"━━━━━━━━━━━━━━━")

    # 시간대별 가이드
    if phase == "open":
        lines.append("🔔 *장 시작*")
        lines.append("→ 갭업 확인. 추격매수 금지")
        lines.append("→ 30분~1시간 눌림 대기")
        if cb_ok:
            lines.append("→ ✅ 서킷브레이커 해제! ISA 매수 준비")
    elif phase == "mid_morning":
        lines.append("🔔 *오전 중반*")
        if cb_ok and vix_val < 22:
            lines.append("→ ISA 1차 매수 적기 판단")
            lines.append("→ TIGER S&P500 50주 + 나스닥100 25주")
            lines.append("→ 단, 장중 하락세이면 대기")
        else:
            lines.append("→ 조건 미충족, ISA 대기 유지")
    elif phase == "afternoon":
        lines.append("🔔 *오후 수급 확인*")
        lines.append("→ 외국인/기관 순매수 전환 여부 확인")
        lines.append("→ 내일 미국장 NVDA $185 돌파 주시")
    elif phase == "close":
        lines.append("🔔 *장 마감 정리*")
        lines.append("→ 4/11(금) 한은 금통위 대비")
        if nvda_185:
            lines.append("→ 🔥 오늘 밤 미국장: NVDA RIA 20주 익절 실행 준비")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python checklist_alert.py [open|mid_morning|afternoon|close]")
        sys.exit(1)

    phase = sys.argv[1]
    msg = build_alert(phase)
    print(msg)
    ok = send_simple_message(msg)
    print(f"\n텔레그램 전송: {'성공' if ok else '실패'}")


if __name__ == "__main__":
    main()
