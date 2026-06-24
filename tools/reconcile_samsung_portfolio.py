"""
삼성증권 포트폴리오 reconciliation 리포트 — read-only, 자동 수정 금지

목표:
- settings.HOLDINGS vs 현재가 source vs dashboard /api/portfolio 대조
- 가격/평가금액 차이 원인 분리 (수량/평단/현재가 source/환율/예수금/미반영 거래)
- Toss Paper ledger는 삼성증권 포트폴리오에서 제외
- 삼성증권 원본 스냅샷이 없으면 "원본 미확인"으로 표시
- 자동 수정 없음

사용법:
  python tools/reconcile_samsung_portfolio.py
  python tools/reconcile_samsung_portfolio.py --no-api   # dashboard API 조회 스킵
  python tools/reconcile_samsung_portfolio.py --no-kis   # KIS API 스킵 (yfinance만)
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

KST = timezone(timedelta(hours=9))
_REPORT_DIR = ROOT / "reports"

# ─── 계좌 정의 ────────────────────────────────────────────
_ACCOUNTS = {
    "일반": "HOLDINGS_GENERAL",
    "RIA":  "HOLDINGS_RIA",
    "IRP":  "HOLDINGS_IRP",
    "연금저축": "HOLDINGS_PENSION",
    "ISA":  "HOLDINGS_ISA",
}

_CASH_KEYS = {
    "일반":    "DEFAULT_CASH",
    "RIA":     "RIA_CASH",
    "IRP":     "IRP_CASH",
    "연금저축": "PENSION_MMF",
    "ISA":     "ISA_CASH",
}

# IRP_DEFAULT_OPTION은 예수금과 별도 (디폴트옵션 안정투자형)
# dashboard는 IRP_CASH + IRP_DEFAULT_OPTION을 합산해서 반환

_KR_SUFFIX = (".KS", ".KQ")

# 가격 source 불일치 임계값
_WARN_PCT   = 1.0   # 1% 이상 → 주의
_ALERT_PCT  = 3.0   # 3% 이상 → 이상치 (source 불일치)
# entry 대비 현재가 이상치 배율 (평가용, 참고)
_ANOMALY_RATIO_HIGH = 2.0   # 현재가 > entry × 2.0 → 기업행동/스냅샷 재확인 후보
_ANOMALY_RATIO_LOW  = 0.5   # 현재가 < entry × 0.5 → 동일


def _is_kr(ticker: str) -> bool:
    return any(ticker.endswith(s) for s in _KR_SUFFIX)


def _now_kst_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _load_settings() -> dict:
    """settings.py에서 HOLDINGS_* / CASH 변수를 로드한다."""
    from config import settings as s
    return {
        "HOLDINGS_GENERAL":  getattr(s, "HOLDINGS_GENERAL", {}),
        "HOLDINGS_RIA":      getattr(s, "HOLDINGS_RIA", {}),
        "HOLDINGS_IRP":      getattr(s, "HOLDINGS_IRP", {}),
        "HOLDINGS_PENSION":  getattr(s, "HOLDINGS_PENSION", {}),
        "HOLDINGS_ISA":      getattr(s, "HOLDINGS_ISA", {}),
        "DEFAULT_CASH":      getattr(s, "DEFAULT_CASH", 0.0),
        "RIA_CASH":          getattr(s, "RIA_CASH", 0.0),
        "IRP_CASH":          getattr(s, "IRP_CASH", 0.0),
        "IRP_DEFAULT_OPTION": getattr(s, "IRP_DEFAULT_OPTION", 0.0),
        "PENSION_MMF":       getattr(s, "PENSION_MMF", 0.0),
        "ISA_CASH":          getattr(s, "ISA_CASH", 0.0),
        "ACCOUNT_PRINCIPAL_KRW": getattr(s, "ACCOUNT_PRINCIPAL_KRW", {}),
        "TOTAL_PRINCIPAL_KRW":   getattr(s, "TOTAL_PRINCIPAL_KRW", 0.0),
    }


def _get_usdkrw() -> float:
    """USD/KRW 환율 조회."""
    try:
        from core.market import _get_quote_yf_live
        q = _get_quote_yf_live("USDKRW=X")
        if q and q.price and q.price > 0:
            return float(q.price)
    except Exception:
        pass
    try:
        import yfinance as yf
        info = yf.Ticker("USDKRW=X").fast_info
        p = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
        if p and float(p) > 0:
            return float(p)
    except Exception:
        pass
    return 1_350.0


def _query_price_kis(ticker: str) -> float | None:
    try:
        from core.market import _get_quote_kis
        q = _get_quote_kis(ticker)
        if q and q.price and q.price > 0:
            return float(q.price)
    except Exception:
        pass
    return None


def _query_price_naver(ticker: str) -> float | None:
    """Naver fallback (국내 종목 전용)."""
    if not _is_kr(ticker):
        return None
    try:
        from core.kr_price_fallback import get_kr_stock_price_fallback
        fb = get_kr_stock_price_fallback(ticker)
        if fb.get("ok") and fb.get("price") and fb["price"] > 0:
            return float(fb["price"])
    except Exception:
        pass
    return None


def _query_price_yf(ticker: str) -> float | None:
    try:
        from core.market import _get_quote_yf_live
        q = _get_quote_yf_live(ticker)
        if q and q.price and q.price > 0:
            return float(q.price)
    except Exception:
        pass
    return None


def _query_price_all(ticker: str, skip_kis: bool = False) -> dict:
    """ticker별 source별 가격 조회. 실패 시 None."""
    kis   = None if skip_kis else _query_price_kis(ticker)
    naver = _query_price_naver(ticker)   # KR only
    yf    = _query_price_yf(ticker)
    return {"KIS": kis, "Naver": naver, "yfinance": yf}


def _source_agreement(prices: dict) -> dict:
    """source간 가격 차이 분석."""
    valid = {k: v for k, v in prices.items() if v is not None}
    if len(valid) < 2:
        return {"status": "단일소스", "max_diff_pct": None, "sources": list(valid.keys())}
    vals = list(valid.values())
    lo, hi = min(vals), max(vals)
    diff_pct = (hi - lo) / lo * 100 if lo > 0 else 0.0
    if diff_pct < _WARN_PCT:
        status = "정상"
    elif diff_pct < _ALERT_PCT:
        status = "주의"
    else:
        status = "source_불일치"
    return {"status": status, "max_diff_pct": round(diff_pct, 2), "sources": list(valid.keys())}


def _best_price(prices: dict) -> float | None:
    """KIS → Naver → yfinance 우선순위로 최선 가격 선택."""
    for src in ("KIS", "Naver", "yfinance"):
        if prices.get(src) is not None:
            return prices[src]
    return None


def _load_trades_ledger() -> list[dict]:
    """memory.db trades 조회. 삼성증권 거래 기록."""
    try:
        from core.trade_log import list_trades
        result = list_trades(limit=100, pending_only=False)
        return result.get("items", [])
    except Exception:
        return []


def _load_dashboard_api(timeout: int = 8) -> dict | None:
    """GET /api/portfolio from running dashboard."""
    try:
        req = urllib.request.urlopen(
            "http://127.0.0.1:8787/api/portfolio", timeout=timeout
        )
        raw = req.read().decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


def _mask_sensitive(text: str) -> str:
    """계좌번호/토큰/API 키 마스킹 — 리포트 출력 안전화."""
    # 8자리-2자리 계좌번호
    text = re.sub(r"\b\d{8}-\d{2}\b", "****-**", text)
    # 10자리 이상 연속 숫자 (계좌번호 후보)
    text = re.sub(r"\b\d{10,}\b", "**masked**", text)
    # Bearer 토큰
    text = re.sub(r"Bearer\s+\S+", "Bearer **masked**", text, flags=re.IGNORECASE)
    return text


# ─── 핵심 reconciliation 로직 ─────────────────────────────

def reconcile(
    skip_kis: bool = False,
    skip_api: bool = False,
) -> dict:
    """
    삼성증권 reconciliation 실행.

    Returns:
        dict with keys: settings, prices, accounts, diffs, trades, dashboard, issues, summary
    """
    cfg = _load_settings()
    usdkrw = _get_usdkrw()
    now_str = _now_kst_str()

    # ── 1. settings 종목 수집 ────────────────────────────
    # 계좌별 holdings를 flat list로 모음. Toss Paper 제외.
    holdings_flat: list[dict] = []
    for acct, settings_key in _ACCOUNTS.items():
        h = cfg[settings_key]
        for ticker, v in h.items():
            shares = v.get("shares", 0)
            avg_cost_krw = v.get("avg_cost_krw")
            avg_cost_usd = v.get("avg_cost_usd")
            if avg_cost_usd:
                entry_price_native = avg_cost_usd
                entry_price_krw = round(avg_cost_usd * usdkrw, 0)
                currency = "USD"
            else:
                entry_price_native = avg_cost_krw
                entry_price_krw = avg_cost_krw
                currency = "KRW"
            holdings_flat.append({
                "ticker": ticker,
                "account": acct,
                "shares": shares,
                "avg_cost_native": entry_price_native,
                "avg_cost_krw": entry_price_krw,
                "currency": currency,
                "cost_total_krw": round((entry_price_krw or 0) * shares, 0),
            })

    # ── 2. 종목별 현재가 조회 ─────────────────────────────
    # 동일 ticker를 여러 계좌에서 보유 — 중복 조회 방지
    unique_tickers = sorted({h["ticker"] for h in holdings_flat})
    price_results: dict[str, dict] = {}
    for ticker in unique_tickers:
        prices = _query_price_all(ticker, skip_kis=skip_kis)
        best = _best_price(prices)
        agree = _source_agreement(prices)

        # native / krw 변환
        if _is_kr(ticker):
            best_krw = best
            best_native = best
            cur_currency = "KRW"
        else:
            best_native = best
            best_krw = round(best * usdkrw, 0) if best else None
            cur_currency = "USD"

        price_results[ticker] = {
            "sources": prices,
            "best_native": best_native,
            "best_krw": best_krw,
            "currency": cur_currency,
            "source_agreement": agree,
        }

    # ── 3. 계좌별/종목별 평가 ─────────────────────────────
    account_data: dict[str, dict] = {}
    issues: list[dict] = []

    for acct in _ACCOUNTS:
        cash_key = _CASH_KEYS[acct]
        settings_cash = cfg.get(cash_key, 0.0)
        # IRP는 CASH + DEFAULT_OPTION을 합산 (dashboard와 맞추기 위해)
        if acct == "IRP":
            settings_cash_total = settings_cash + cfg.get("IRP_DEFAULT_OPTION", 0.0)
        else:
            settings_cash_total = settings_cash

        rows = [h for h in holdings_flat if h["account"] == acct]
        holdings_krw = 0.0
        ticker_rows = []
        for h in rows:
            ticker = h["ticker"]
            pr = price_results.get(ticker, {})
            cur_price_krw = pr.get("best_krw")
            avg_krw = h["avg_cost_krw"]
            shares = h["shares"]

            if cur_price_krw:
                eval_krw = round(cur_price_krw * shares, 0)
                pnl_krw = round((cur_price_krw - (avg_krw or 0)) * shares, 0)
                pnl_pct = round((cur_price_krw - (avg_krw or 0)) / (avg_krw or 1) * 100, 2) if avg_krw else None
            else:
                eval_krw = None
                pnl_krw = None
                pnl_pct = None

            if eval_krw:
                holdings_krw += eval_krw

            # 이상치 체크
            agree = pr.get("source_agreement", {})
            row_issues = []
            if agree.get("status") == "source_불일치":
                row_issues.append(f"source_불일치 ({agree.get('max_diff_pct', 0):.1f}%)")
                issues.append({
                    "ticker": ticker, "account": acct,
                    "category": "현재가_source_불일치",
                    "detail": f"최대 diff {agree.get('max_diff_pct', 0):.1f}%",
                })
            if cur_price_krw and avg_krw and avg_krw > 0:
                ratio = cur_price_krw / avg_krw
                if ratio >= _ANOMALY_RATIO_HIGH:
                    row_issues.append(f"현재가/평단 {ratio:.2f}x — 기업행동/스냅샷 재확인 후보")
                    issues.append({
                        "ticker": ticker, "account": acct,
                        "category": "현재가_vs_평단_비율_이상",
                        "detail": f"ratio={ratio:.2f}x (현재 {cur_price_krw:,.0f} / 평단 {avg_krw:,.0f})",
                        "diagnosis": "기업행동(분할/병합) 또는 삼성증권 스냅샷 재확인 필요",
                    })
                elif ratio <= _ANOMALY_RATIO_LOW:
                    row_issues.append(f"현재가/평단 {ratio:.2f}x — 급락 또는 스냅샷 재확인 후보")
                    issues.append({
                        "ticker": ticker, "account": acct,
                        "category": "현재가_vs_평단_비율_이상",
                        "detail": f"ratio={ratio:.2f}x (현재 {cur_price_krw:,.0f} / 평단 {avg_krw:,.0f})",
                        "diagnosis": "급락 또는 삼성증권 스냅샷 재확인 필요",
                    })

            ticker_rows.append({
                "ticker": ticker,
                "shares": shares,
                "avg_cost_native": h["avg_cost_native"],
                "avg_cost_krw": avg_krw,
                "currency": h["currency"],
                "prices": pr.get("sources", {}),
                "best_price_native": pr.get("best_native"),
                "best_price_krw": cur_price_krw,
                "eval_krw": eval_krw,
                "pnl_krw": pnl_krw,
                "pnl_pct": pnl_pct,
                "source_agreement": agree.get("status", "N/A"),
                "issues": row_issues,
            })

        account_data[acct] = {
            "cash_settings": settings_cash,
            "cash_with_extra": settings_cash_total,
            "holdings_eval_krw": round(holdings_krw, 0),
            "total_eval_krw": round(holdings_krw + settings_cash_total, 0),
            "principal_krw": cfg["ACCOUNT_PRINCIPAL_KRW"].get(acct, 0.0),
            "rows": ticker_rows,
        }

    # ── 4. 전체 합계 계산 ─────────────────────────────────
    total_holdings_krw = sum(
        a["holdings_eval_krw"] for a in account_data.values()
    )
    total_cash_krw = sum(
        a["cash_with_extra"] for a in account_data.values()
    )
    settings_total_eval = round(total_holdings_krw + total_cash_krw, 0)
    settings_total_principal = cfg["TOTAL_PRINCIPAL_KRW"]

    # ── 5. 미반영 거래 확인 ───────────────────────────────
    trades = _load_trades_ledger()
    pending_trades = [t for t in trades if not t.get("applied", True)]
    # applied 컬럼이 없으면 전부 applied=True로 간주
    unapplied_issue: list[dict] = []
    if pending_trades:
        for t in pending_trades:
            unapplied_issue.append({
                "ticker": t.get("ticker", ""), "side": t.get("side", ""),
                "shares": t.get("shares", 0), "price": t.get("price", 0),
                "date": t.get("created_at", ""), "account": t.get("account", ""),
            })
            issues.append({
                "ticker": t.get("ticker", ""),
                "account": t.get("account", ""),
                "category": "미반영_거래",
                "detail": f"{t.get('side')} {t.get('shares')}주 @{t.get('price')} ({t.get('created_at','')})",
                "diagnosis": "settings.HOLDINGS에 미반영 — 수량/평단 차이 원인 후보",
            })

    # ── 6. dashboard API 대조 ─────────────────────────────
    api_data: dict | None = None
    api_total_eval: float | None = None
    api_vs_settings_diff: float | None = None
    api_diff_pct: float | None = None
    api_stale = False

    if not skip_api:
        api_data = _load_dashboard_api()
        if api_data:
            api_total_eval = api_data.get("total_value_krw") or api_data.get("total_value")
            if api_total_eval and settings_total_eval:
                api_vs_settings_diff = round(api_total_eval - settings_total_eval, 0)
                api_diff_pct = round(api_vs_settings_diff / settings_total_eval * 100, 2)
            # stale check: "last_updated" or "evaluated_at" 필드 있으면 확인
            last_update = api_data.get("last_updated") or api_data.get("evaluated_at", "")
            if last_update:
                try:
                    lu_dt = datetime.fromisoformat(last_update.replace("+09:00", "")).replace(tzinfo=KST)
                    age_min = (datetime.now(KST) - lu_dt).total_seconds() / 60
                    if age_min > 10:
                        api_stale = True
                        issues.append({
                            "ticker": "dashboard", "account": "N/A",
                            "category": "dashboard_stale",
                            "detail": f"last_updated {last_update} ({age_min:.0f}분 경과)",
                            "diagnosis": "dashboard 캐시 또는 서버 재시작 필요 (자동 재시작 금지)",
                        })
                except Exception:
                    pass
        else:
            issues.append({
                "ticker": "dashboard", "account": "N/A",
                "category": "dashboard_unavailable",
                "detail": "http://127.0.0.1:8787/api/portfolio 응답 없음",
                "diagnosis": "dashboard 서버가 실행 중인지 확인",
            })

    # ── 7. 삼성증권 원본 스냅샷 확인 ──────────────────────
    # Hermes exports는 /root 소유로 현재 사용자 접근 불가
    snapshot_sources = _check_snapshot_sources()

    return {
        "evaluated_at": now_str,
        "usdkrw": usdkrw,
        "settings": cfg,
        "account_data": account_data,
        "unique_tickers": unique_tickers,
        "price_results": price_results,
        "trades_all": trades,
        "pending_trades": unapplied_issue,
        "api": api_data,
        "api_total_eval": api_total_eval,
        "settings_total_eval": settings_total_eval,
        "settings_total_principal": settings_total_principal,
        "api_vs_settings_diff": api_vs_settings_diff,
        "api_diff_pct": api_diff_pct,
        "api_stale": api_stale,
        "snapshot_sources": snapshot_sources,
        "issues": issues,
        "summary": {
            "settings_total_eval_krw": settings_total_eval,
            "settings_principal_krw": settings_total_principal,
            "settings_unrealized_pnl_krw": round(
                settings_total_eval - settings_total_principal - total_cash_krw, 0
            ),
            "api_total_eval_krw": api_total_eval,
            "api_vs_settings_diff_krw": api_vs_settings_diff,
            "api_vs_settings_diff_pct": api_diff_pct,
            "issue_count": len(issues),
            "pending_trade_count": len(unapplied_issue),
            "samsung_snapshot_found": snapshot_sources.get("found", False),
            "toss_paper_excluded": True,
        },
    }


def _check_snapshot_sources() -> dict:
    """삼성증권 원본 스냅샷 흔적 검색 (read-only)."""
    sources_checked = []
    found = False
    snippets = []

    # Hermes exports
    for path_str in [
        "/root/.hermes/gcp-memory/exports/stockbot_conversations.txt",
        "/root/.hermes/gcp-memory/exports/chatbot_recent.txt",
        "/root/.hermes/shared-context/current-stock.md",
    ]:
        p = Path(path_str)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            sources_checked.append({"path": path_str, "status": "읽기 성공"})
            keywords = ["삼성증권", "스샷", "체결", "평단", "평가금액", "예수금"]
            for kw in keywords:
                if kw in text:
                    # context 50자
                    idx = text.index(kw)
                    snippet = text[max(0, idx - 30): idx + 80].strip().replace("\n", " ")
                    snippets.append({"source": path_str, "keyword": kw, "snippet": snippet[:120]})
                    found = True
        except PermissionError:
            sources_checked.append({"path": path_str, "status": "Permission denied"})
        except FileNotFoundError:
            sources_checked.append({"path": path_str, "status": "파일 없음"})
        except Exception as e:
            sources_checked.append({"path": path_str, "status": f"오류: {e}"})

    return {
        "found": found,
        "sources_checked": sources_checked,
        "snippets": snippets[:5],
        "note": "원본 미확인" if not found else "일부 흔적 발견",
    }


# ─── 리포트 포맷터 ────────────────────────────────────────

def format_report(result: dict) -> str:
    lines = []
    s = result["summary"]
    issues = result["issues"]
    ad = result["account_data"]

    def sep(label: str = "") -> None:
        if label:
            lines.append(f"\n{'─' * 20} {label} {'─' * 20}")
        else:
            lines.append("─" * 55)

    lines.append("=" * 55)
    lines.append("삼성증권 포트폴리오 Reconciliation 리포트")
    lines.append(f"평가 시각: {result['evaluated_at']}")
    lines.append(f"USD/KRW: {result['usdkrw']:,.2f}")
    lines.append(f"Toss Paper 제외: {'✅' if s['toss_paper_excluded'] else '❌'}")
    lines.append(f"삼성증권 원본 스냅샷: {'발견' if s['samsung_snapshot_found'] else '원본 미확인'}")
    lines.append("=" * 55)

    # 요약
    sep("요약")
    lines.append(f"  settings 종목 평가액:  ₩{s['settings_total_eval_krw']:>16,.0f}")
    lines.append(f"  settings 투자 원금:    ₩{s['settings_principal_krw']:>16,.0f}")
    if s.get("api_total_eval_krw"):
        lines.append(f"  dashboard 종목 평가액: ₩{s['api_total_eval_krw']:>16,.0f}")
        diff = s.get("api_vs_settings_diff_krw", 0) or 0
        diff_pct = s.get("api_vs_settings_diff_pct", 0) or 0
        lines.append(f"  차이 (api - settings): ₩{diff:>+16,.0f} ({diff_pct:+.2f}%)")
    else:
        lines.append("  dashboard 평가액: 조회 불가")
    lines.append(f"  이슈 건수: {s['issue_count']}건 / 미반영 거래: {s['pending_trade_count']}건")

    # 원인 후보 리스트
    categories = {}
    for iss in issues:
        cat = iss.get("category", "기타")
        categories.setdefault(cat, []).append(iss)

    if categories:
        sep("원인 후보")
        for cat, items in sorted(categories.items()):
            lines.append(f"  [{cat}] {len(items)}건")
            for it in items[:3]:
                ticker = it.get("ticker", "")
                acct = it.get("account", "")
                detail = it.get("detail", "")
                diagnosis = it.get("diagnosis", "")
                lines.append(f"    - {ticker} [{acct}]: {detail}")
                if diagnosis:
                    lines.append(f"      → {diagnosis}")

    # 계좌별 비교
    sep("계좌별 비교")
    for acct, data in ad.items():
        h_eval = data["holdings_eval_krw"]
        cash = data["cash_with_extra"]
        total = data["total_eval_krw"]
        principal = data["principal_krw"]
        lines.append(f"\n  [{acct}]")
        lines.append(f"    종목평가: ₩{h_eval:>14,.0f}")
        lines.append(f"    예수금:   ₩{cash:>14,.0f}")
        lines.append(f"    계좌합계: ₩{total:>14,.0f}")
        if principal:
            pnl = h_eval - principal
            pnl_pct = pnl / principal * 100 if principal else 0.0
            lines.append(f"    투자원금: ₩{principal:>14,.0f}  (종목 손익 {pnl:+,.0f} / {pnl_pct:+.2f}%)")

    # 종목별 비교
    sep("종목별 현재가 비교")
    hdr = f"  {'티커':<14} {'계좌':<6} {'수량':>5} {'평단(원)':>12} {'KIS가':>10} {'yfinance':>10} {'평가(원)':>14} {'손익%':>8} {'source':>12} {'이슈'}"
    lines.append(hdr)
    lines.append("  " + "-" * 105)

    for acct, data in ad.items():
        for row in data["rows"]:
            ticker = row["ticker"]
            shares = row["shares"]
            avg_krw = row["avg_cost_krw"]
            if row["currency"] == "USD":
                avg_disp = f"${row['avg_cost_native']:,.2f}"
            else:
                avg_disp = f"₩{avg_krw:,.0f}" if avg_krw else "-"
            kis_p = row["prices"].get("KIS")
            yf_p  = row["prices"].get("yfinance")
            if row["currency"] == "USD":
                kis_disp = f"${kis_p:,.2f}" if kis_p else "-"
                yf_disp  = f"${yf_p:,.2f}" if yf_p else "-"
            else:
                kis_disp = f"₩{kis_p:,.0f}" if kis_p else "-"
                yf_disp  = f"₩{yf_p:,.0f}" if yf_p else "-"
            eval_disp = f"₩{row['eval_krw']:,.0f}" if row.get("eval_krw") else "N/A"
            pnl_disp  = f"{row['pnl_pct']:+.1f}%" if row.get("pnl_pct") is not None else "N/A"
            agree_disp = row.get("source_agreement", "N/A")
            issues_disp = " | ".join(row.get("issues", []))[:40] or "-"
            lines.append(
                f"  {ticker:<14} {acct:<6} {shares:>5} {avg_disp:>12} "
                f"{kis_disp:>10} {yf_disp:>10} {eval_disp:>14} {pnl_disp:>8} "
                f"{agree_disp:>12}  {issues_disp}"
            )

    # 거래 ledger
    sep("거래 Ledger (memory.db)")
    all_trades = result.get("trades_all", [])
    if all_trades:
        lines.append(f"  총 {len(all_trades)}건 (최근 5건):")
        for t in all_trades[:5]:
            applied_mark = "✅" if t.get("applied") else "⚠️ 미반영"
            acct_mark = t.get("account", "?")
            lines.append(
                f"  {applied_mark} {t.get('created_at','')[:16]}  "
                f"{t.get('ticker','')}  {t.get('side','')}  "
                f"{t.get('shares','')}주  @{t.get('price','')}  [{acct_mark}]"
            )
        pending = result.get("pending_trades", [])
        if pending:
            lines.append(f"\n  ⚠️ 미반영 거래 {len(pending)}건 — settings.HOLDINGS 차이 원인 후보")
        else:
            lines.append("\n  ✅ 미반영 거래 없음 — settings.HOLDINGS 최신")
    else:
        lines.append("  거래 기록 없음")

    # 삼성증권 스냅샷 확인
    snap = result.get("snapshot_sources", {})
    sep("삼성증권 원본 스냅샷 확인")
    for src in snap.get("sources_checked", []):
        lines.append(f"  {src['path']}: {src['status']}")
    if snap.get("snippets"):
        lines.append("\n  [발견된 키워드]")
        for sn in snap["snippets"]:
            lines.append(f"  '{sn['keyword']}' in {Path(sn['source']).name}")
            lines.append(f"    … {sn['snippet'][:100]} …")
    else:
        lines.append("\n  → 원본 미확인 (Hermes exports 접근 불가 또는 키워드 없음)")
        lines.append("    수동으로 삼성증권 앱 스샷/거래내역 확인 후 승인 필요")

    # 수정 제안
    sep("수정 제안 (자동 수정 안 함)")
    lines.append("  이 리포트는 진단용입니다. 수정은 사용자가 승인 후 진행하세요.")
    lines.append("")
    lines.append("  수정이 필요할 수 있는 항목:")
    if pending_trades := result.get("pending_trades", []):
        lines.append(f"  - 미반영 거래 {len(pending_trades)}건 → settings.HOLDINGS 수량/평단 조정 검토")
    if "현재가_vs_평단_비율_이상" in categories:
        lines.append("  - 현재가/평단 비율 이상 종목 → 기업행동(분할/병합) 확인 후 avg_cost 조정 검토")
    if "현재가_source_불일치" in categories:
        lines.append("  - 현재가 source 불일치 → KIS API 또는 yfinance 재확인")
    if not s["samsung_snapshot_found"]:
        lines.append("  - 삼성증권 원본 스냅샷 없음 → 앱에서 잔고/체결내역 스샷 후 수동 검증")
    if result.get("api_stale"):
        lines.append("  - dashboard stale → 서버 재시작 검토 (자동 재시작 금지, 수동 확인)")
    if not any([pending_trades, categories.get("현재가_vs_평단_비율_이상"), categories.get("현재가_source_불일치"), not s["samsung_snapshot_found"], result.get("api_stale")]):
        lines.append("  - 현재 식별된 자동 수정 후보 없음")

    lines.append("")
    lines.append("=" * 55)
    lines.append("리포트 종료. 자동 수정 없음. 실주문 없음.")
    lines.append("=" * 55)

    return "\n".join(lines)


def save_report(text: str) -> Path:
    """reports/ 디렉토리에 타임스탬프 리포트 저장."""
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M")
    path = _REPORT_DIR / f"samsung_reconciliation_{ts}.md"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> None:
    skip_api = "--no-api" in sys.argv
    skip_kis = "--no-kis" in sys.argv

    print("=" * 55)
    print("[삼성증권 Reconciliation] read-only · 자동 수정 금지")
    print("=" * 55)

    print("\n  USD/KRW 조회 중...")
    print("  settings.HOLDINGS 로드 중...")
    print("  현재가 조회 중 (KIS + yfinance)...")

    result = reconcile(skip_kis=skip_kis, skip_api=skip_api)

    report_text = format_report(result)
    report_text = _mask_sensitive(report_text)

    print(report_text)

    report_path = save_report(report_text)
    print(f"\n→ 리포트 저장: {report_path}")


if __name__ == "__main__":
    main()
