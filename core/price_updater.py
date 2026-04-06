"""
Notion 주가 자동 업데이트
Stock_bot/update_price.py 이전
"""

from __future__ import annotations

import logging
from datetime import datetime

import requests
import yfinance as yf

from config.settings import KST, NOTION_TOKEN, NOTION_DATABASE_ID

log = logging.getLogger(__name__)


def _get_exchange_rate() -> float:
    try:
        rate_data = yf.Ticker("USDKRW=X").history(period="1d")
        if not rate_data.empty:
            return float(rate_data["Close"].iloc[-1])
    except Exception:
        pass
    return 1400.0


def _get_notion_pages() -> list[dict]:
    """Notion DB에서 매매종목 티커 목록 조회."""
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        log.warning("NOTION_TOKEN 또는 NOTION_DATABASE_ID 미설정")
        return []

    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    res = requests.post(url, headers=headers, timeout=30)
    if res.status_code != 200:
        return []

    pages: list[dict] = []
    for page in res.json().get("results", []):
        page_id = page["id"]
        prop_data = page.get("properties", {}).get("매매종목", {})
        prop_type = prop_data.get("type", "")

        ticker = ""
        if prop_type == "rich_text":
            text_arr = prop_data.get("rich_text", [])
            if text_arr:
                ticker = text_arr[0].get("plain_text", "")
        elif prop_type == "title":
            text_arr = prop_data.get("title", [])
            if text_arr:
                ticker = text_arr[0].get("plain_text", "")
        elif prop_type == "select":
            select_data = prop_data.get("select")
            if select_data:
                ticker = select_data.get("name", "")
        elif prop_type == "formula":
            formula_data = prop_data.get("formula", {})
            if formula_data.get("type") == "string":
                ticker = formula_data.get("string", "")

        ticker = ticker.strip()
        if ticker:
            pages.append({"id": page_id, "ticker": ticker})

    return pages


def _get_stock_price(ticker: str) -> float | None:
    yf_ticker = ticker
    if ticker.startswith("KRX:"):
        yf_ticker = ticker.replace("KRX:", "") + ".KS"

    try:
        hist = yf.Ticker(yf_ticker).history(period="1d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def _update_notion_page(page_id: str, formatted_price: str, krw_price: int) -> None:
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    payload = {
        "properties": {
            "현재가": {"rich_text": [{"text": {"content": formatted_price}}]},
            "원화가격": {"number": krw_price},
        },
    }
    requests.patch(url, headers=headers, json=payload, timeout=30)


def update_all_prices() -> int:
    """모든 종목의 Notion 주가를 업데이트.

    Returns:
        업데이트된 종목 수
    """
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"[{now}] 주가 자동 업데이트 시작")

    exchange_rate = _get_exchange_rate()
    pages = _get_notion_pages()
    updated = 0

    for page in pages:
        ticker = page["ticker"]
        price = _get_stock_price(ticker)

        if price is not None:
            if ticker.startswith("KRX:"):
                formatted = f"₩{int(price):,}"
                krw = int(price)
            else:
                formatted = f"${round(price, 2):,.2f}"
                krw = int(price * exchange_rate)

            _update_notion_page(page["id"], formatted, krw)
            updated += 1
            log.info(f"  ✅ {ticker}: {formatted}")

    log.info(f"업데이트 완료: {updated}/{len(pages)} 종목")
    return updated
