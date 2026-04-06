"""
Notion 브리핑 저장 — 블록 빌더 + 페이지 생성
Stock_bot/scripts/briefing.py 섹션 5~6 로직 이전
"""

from __future__ import annotations

import requests
from datetime import datetime

from config.settings import KST, NOTION_API_KEY, NOTION_DB_ID
from core.market import fmt_change, fmt_price, pct_bar, signal_badge
from core.models import BriefingResult, MarketSnapshot


# ═══════════════════════════════════════════════════════
# Notion 블록 빌더
# ═══════════════════════════════════════════════════════
def _rt(text: str, bold: bool = False, color: str | None = None) -> list[dict]:
    ann: dict = {}
    if bold:
        ann["bold"] = True
    if color:
        ann["color"] = color
    item: dict = {"type": "text", "text": {"content": str(text)[:2000]}}
    if ann:
        item["annotations"] = ann
    return [item]


def H1(txt: str, bg: str = "blue_background") -> dict:
    return {"object": "block", "type": "heading_1",
            "heading_1": {"rich_text": _rt(txt), "color": bg}}

def H2(txt: str, bg: str = "default") -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rt(txt), "color": bg}}

def P(txt: str, bold: bool = False) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rt(txt, bold=bold)}}

def BUL(txt: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rt(txt)}}

def DIV() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}

def CALLOUT(txt: str, emoji: str = "📌", bg: str = "gray_background") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": _rt(txt),
                        "icon": {"type": "emoji", "emoji": emoji}, "color": bg}}

def TOGGLE(title: str, children: list[dict], color: str = "default") -> dict:
    return {"object": "block", "type": "toggle",
            "toggle": {"rich_text": _rt(title, bold=True),
                       "color": color, "children": children}}

def TABLE(rows: list[list[str]], has_header: bool = True) -> dict:
    if not rows:
        return P("(데이터 없음)")
    return {
        "object": "block", "type": "table",
        "table": {
            "table_width": len(rows[0]),
            "has_column_header": has_header,
            "has_row_header": False,
            "children": [
                {"object": "block", "type": "table_row",
                 "table_row": {"cells": [
                     [{"type": "text", "text": {"content": c}}] for c in row
                 ]}}
                for row in rows
            ],
        },
    }


def urgency_badge(u: str) -> str:
    m = {
        "🔥강력": "🔥 강력", "⚡적극": "⚡ 적극", "✅일반": "✅ 일반",
        "🔴즉시": "🔴 즉시", "🟠주의": "🟠 주의", "🟡모니터링": "🟡 모니터링",
    }
    return m.get(u, u)


# ═══════════════════════════════════════════════════════
# 섹션별 블록 생성
# ═══════════════════════════════════════════════════════
def _section_header(now_kst: str, label: str) -> list[dict]:
    return [
        CALLOUT(f"📅  {now_kst}   |   {label}", "🚀", "yellow_background"),
        DIV(),
    ]


def _section_market_overview(snapshot: MarketSnapshot) -> list[dict]:
    blocks = [H2("📈  시장 지수", "blue_background")]
    rows: list[list[str]] = [["지수", "현재가", "등락률", "방향"]]
    for nm, q in snapshot.indices.items():
        arrow = "▲" if q.pct >= 0 else "▼"
        rows.append([nm, f"{q.price:,.2f}", f"{arrow} {q.pct:+.2f}%", pct_bar(q.pct)])
    blocks.append(TABLE(rows))

    blocks.append(H2("🌐  매크로 지표", "gray_background"))
    mac_rows: list[list[str]] = [["지표", "현재값", "전일비", "방향"]]
    for nm, q in snapshot.macro.items():
        if "원달러" in nm:
            val = f"₩{q.price:,.2f}"
        elif "VIX" in nm or "국채" in nm:
            val = f"{q.price:.2f}"
        else:
            val = f"${q.price:,.2f}"
        arrow = "▲" if q.pct >= 0 else "▼"
        mac_rows.append([nm, val, f"{arrow} {q.pct:+.2f}%", pct_bar(q.pct)])
    blocks.append(TABLE(mac_rows))
    blocks.append(DIV())
    return blocks


def _section_market_summary(result: BriefingResult) -> list[dict]:
    blocks = [H2("📋  시장 요약", "blue_background")]
    for line in result.market_summary.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("- ") or line.startswith("* "):
            blocks.append(BUL(line[2:]))
        else:
            blocks.append(P(line))
    blocks.append(DIV())
    return blocks


def _section_portfolio(result: BriefingResult, snapshot: MarketSnapshot) -> list[dict]:
    blocks = [H2("📋  보유 종목 브리핑", "blue_background")]
    rows: list[list[str]] = [["종목", "현재가", "등락", "신호", "판단 근거"]]

    for sig in result.portfolio_signals:
        raw_row = result.raw_json.get("portfolio_rows", [])
        matching = next((r for r in raw_row if r.get("ticker") == sig.ticker), {})
        rows.append([
            sig.name,
            matching.get("price_display", ""),
            matching.get("change_pct", ""),
            signal_badge(sig.signal),
            sig.reason[:80],
        ])

    if len(rows) == 1:
        for tk, q in snapshot.stocks.items():
            rows.append([
                q.name, fmt_price(tk, q.price),
                f"{q.pct:+.2f}% ({fmt_change(tk, q.change)})", "—", "—",
            ])
    blocks.append(TABLE(rows))
    blocks.append(DIV())
    return blocks


def _section_strategy(result: BriefingResult) -> list[dict]:
    blocks = [H1("🎯  매수 / 매도 전략", "red_background")]
    if result.strategy_summary:
        blocks.append(CALLOUT(result.strategy_summary, "⚡", "yellow_background"))
    blocks.append(DIV())

    # 매수
    if result.buy_signals:
        blocks.append(H2("🟢  매수 액션", "green_background"))
        buy_rows: list[list[str]] = [["종목", "긴급도", "진입가", "목표가", "손절가", "수량"]]
        for sig in result.buy_signals:
            buy_rows.append([
                f"{sig.name} ({sig.ticker})", urgency_badge(sig.urgency),
                sig.entry_price, sig.target_price, sig.stop_loss, sig.shares,
            ])
        blocks.append(TABLE(buy_rows))

        for sig in result.buy_signals:
            detail: list[dict] = []
            if sig.timing:
                detail.append(CALLOUT(f"⏰  진입 타이밍\n{sig.timing}", "⏰", "blue_background"))
            if sig.split_plan:
                detail.append(CALLOUT(f"📐  분할 매수 계획\n{sig.split_plan}", "📐", "purple_background"))
            if sig.reason:
                detail.append(P("📌  매수 근거", bold=True))
                for line in sig.reason.split("\n"):
                    if line.strip():
                        detail.append(BUL(line.strip()))
            if detail:
                blocks.append(TOGGLE(f"▸  {sig.name} ({sig.ticker}) 상세 전략", detail, "green"))
        blocks.append(DIV())

    # 매도
    if result.sell_signals:
        blocks.append(H2("🔴  매도 / 주의 종목", "red_background"))
        sell_rows: list[list[str]] = [["종목", "긴급도", "현재가", "익절가", "손절가", "근거"]]
        for sig in result.sell_signals:
            sell_rows.append([
                f"{sig.name} ({sig.ticker})", urgency_badge(sig.urgency),
                sig.entry_price, sig.target_price, sig.stop_loss,
                sig.reason[:60],
            ])
        blocks.append(TABLE(sell_rows))

        for sig in result.sell_signals:
            detail = []
            if sig.timing:
                detail.append(CALLOUT(f"⏰  매도 타이밍\n{sig.timing}", "⏰", "orange_background"))
            if sig.reason:
                detail.append(P("📌  매도 근거", bold=True))
                for line in sig.reason.split("\n"):
                    if line.strip():
                        detail.append(BUL(line.strip()))
            if detail:
                blocks.append(TOGGLE(f"▸  {sig.name} ({sig.ticker}) 매도 상세", detail, "red"))
        blocks.append(DIV())

    return blocks


def _section_advisor(result: BriefingResult) -> list[dict]:
    verdict = result.advisor_verdict
    oneliner = result.advisor_oneliner
    conclusion = result.advisor_conclusion
    if not verdict and not oneliner:
        return []

    verdict_map = {
        "매수대기": ("orange_background", "⏸️"),
        "소액분할": ("blue_background", "🔵"),
        "적극매수": ("green_background", "🟢"),
        "매도고려": ("red_background", "🔴"),
    }
    bg, emoji = verdict_map.get(verdict, ("yellow_background", "💡"))

    blocks = [H1(f"💬  AI 솔직한 조언 — {verdict}", bg)]
    if oneliner:
        blocks.append(CALLOUT(oneliner, emoji, bg))

    # raw_json에서 추가 데이터 사용
    raw = result.raw_json
    checklist = raw.get("advisor_checklist", [])
    if checklist:
        blocks.append(H2("✅  매수 조건 체크리스트", "gray_background"))
        icon_map = {"충족": "✅", "미충족": "❌", "부분충족": "🔶"}
        ck_rows: list[list[str]] = [["조건", "상태", "현재 상황"]]
        for item in checklist:
            icon = icon_map.get(item.get("status", ""), "—")
            ck_rows.append([
                item.get("condition", ""),
                f"{icon} {item.get('status', '')}",
                item.get("detail", ""),
            ])
        blocks.append(TABLE(ck_rows))
        blocks.append(DIV())

    risks = raw.get("advisor_risks", [])
    opps = raw.get("advisor_opportunities", [])
    if risks or opps:
        blocks.append(H2("⚖️  리스크 vs 기회", "gray_background"))
        if risks:
            blocks.append(CALLOUT("리스크\n" + "\n".join(f"• {r}" for r in risks), "⚠️", "red_background"))
        if opps:
            blocks.append(CALLOUT("기회\n" + "\n".join(f"• {o}" for o in opps), "💡", "green_background"))
        blocks.append(DIV())

    scenarios = raw.get("advisor_scenarios", [])
    if scenarios:
        blocks.append(H2("📅  시나리오별 액션 플랜", "blue_background"))
        sc_rows: list[list[str]] = [["시나리오", "발동 조건", "액션", "집행 금액"]]
        for sc in scenarios:
            sc_rows.append([sc.get("label", ""), sc.get("condition", ""),
                            sc.get("action", ""), sc.get("amount", "")])
        blocks.append(TABLE(sc_rows))
        blocks.append(DIV())

    if conclusion:
        blocks.append(H2("📝  종합 결론 (직언)", "yellow_background"))
        for line in conclusion.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("- ") or line.startswith("* "):
                blocks.append(BUL(line[2:]))
            else:
                blocks.append(P(line))
        blocks.append(DIV())

    return blocks


def _section_portfolio_raw(snapshot: MarketSnapshot) -> list[dict]:
    from config.settings import KRW_TICKERS
    blocks = [H2("📊  포트폴리오 실시간 현황 (yfinance)", "gray_background")]
    rows: list[list[str]] = [["종목 (티커)", "구분", "현재가", "등락률", "변동액", "고가", "저가"]]
    for tk, q in snapshot.stocks.items():
        if tk in KRW_TICKERS:
            cat = "국내주식" if not any(kw in q.name for kw in ["TIGER", "KODEX", "PLUS"]) else "국내 ETF"
        else:
            cat = "미국주식"
        s = "▲" if q.pct >= 0 else "▼"
        rows.append([
            f"{q.name} ({tk})", cat, fmt_price(tk, q.price),
            f"{s} {q.pct:+.2f}%", fmt_change(tk, q.change),
            fmt_price(tk, q.high), fmt_price(tk, q.low),
        ])
    blocks.append(TABLE(rows))
    blocks.append(DIV())
    return blocks


def _section_footer() -> list[dict]:
    return [CALLOUT(
        "본 브리핑은 AI 투자 파트너가 yfinance + Google Search를 기반으로 자동 생성합니다.\n"
        "최종 투자 판단은 본인 책임입니다.",
        "⚠️", "red_background",
    )]


# ═══════════════════════════════════════════════════════
# Notion 페이지 저장
# ═══════════════════════════════════════════════════════
LABEL_MAP = {
    "KR_BEFORE": "🇰🇷 국내장 시작 전",
    "US_BEFORE": "🇺🇸 미국장 시작 전",
    "MANUAL": "📊 수시 브리핑",
}


def save_to_notion(
    result: BriefingResult,
    snapshot: MarketSnapshot,
    briefing_type: str = "MANUAL",
) -> str:
    """브리핑 결과를 Notion 페이지로 저장.

    Returns:
        Notion 페이지 ID

    Raises:
        ValueError: API 키 미설정
        requests.HTTPError: Notion API 오류
    """
    if not NOTION_API_KEY or not NOTION_DB_ID:
        raise ValueError("NOTION_API_KEY 또는 NOTION_DB_ID가 설정되지 않았습니다.")

    label = LABEL_MAP.get(briefing_type, "📊 수시 브리핑")
    now_kst = datetime.now(KST)
    dt_iso = now_kst.isoformat()

    # 블록 조립
    blocks: list[dict] = []
    blocks += _section_header(now_kst.strftime("%Y-%m-%d %H:%M KST"), label)
    blocks += _section_market_overview(snapshot)
    blocks += _section_market_summary(result)
    blocks += _section_portfolio(result, snapshot)
    blocks += _section_advisor(result)
    blocks += _section_strategy(result)
    blocks += _section_portfolio_raw(snapshot)
    blocks += _section_footer()
    children = blocks[:100]

    # Notion API
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    def rt(s: str) -> list[dict]:
        return [{"type": "text", "text": {"content": str(s)[:2000]}}]

    # DB 스키마 조회
    db_res = requests.get(
        f"https://api.notion.com/v1/databases/{NOTION_DB_ID}",
        headers=headers, timeout=30,
    )
    db_props: set[str] = set()
    title_prop: str | None = None
    if db_res.status_code == 200:
        db_data = db_res.json()
        db_props = set(db_data.get("properties", {}).keys())
        for pname, pinfo in db_data.get("properties", {}).items():
            if pinfo.get("type") == "title":
                title_prop = pname
                break

    raw = result.raw_json
    all_props = {
        "브리핑 제목": {"title": rt(result.title or f"{now_kst.strftime('%Y.%m.%d %H:%M')} 브리핑")},
        "날짜": {"date": {"start": dt_iso}},
        "브리핑구분": {"select": {"name": label}},
        "시장상황": {"select": {"name": result.market_status}},
        "KOSPI": {"rich_text": rt(raw.get("kospi", ""))},
        "코스닥": {"rich_text": rt(raw.get("kosdaq", ""))},
        "브렌트유_유가": {"rich_text": rt(raw.get("brent", ""))},
        "원달러환율": {"rich_text": rt(raw.get("usdkrw", ""))},
        "VIX": {"rich_text": rt(raw.get("vix", ""))},
        "투자결정": {"select": {"name": result.investment_decision}},
        "핵심키워드": {"rich_text": rt(raw.get("keywords", ""))},
        "다음액션": {"rich_text": rt(raw.get("next_action", ""))},
        "AI조언": {"select": {"name": result.advisor_verdict
                    if result.advisor_verdict in ["매수대기", "소액분할", "적극매수", "매도고려", "중립"]
                    else "중립"}},
    }

    if db_props:
        properties = {k: v for k, v in all_props.items() if k in db_props}
        if title_prop and title_prop not in properties:
            properties[title_prop] = all_props.get("브리핑 제목", {})
    else:
        properties = all_props

    payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "icon": {"type": "emoji", "emoji": "📊"},
        "properties": properties,
        "children": children,
    }

    res = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers, json=payload, timeout=60,
    )
    if res.status_code != 200:
        raise requests.HTTPError(f"Notion API 오류 {res.status_code}: {res.text[:400]}")

    return res.json()["id"]
