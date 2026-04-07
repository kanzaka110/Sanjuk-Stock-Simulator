"""
브리핑 API 서버 — FastAPI 기반 자동화 엔드포인트

사용법: python main.py server
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from functools import partial

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config.settings import API_SECRET_KEY, KST

log = logging.getLogger(__name__)

app = FastAPI(title="산적 주식 시뮬레이터 API", version="1.0.0")


# ─── 인증 ──────────────────────────────────────────
async def verify_api_key(request: Request) -> None:
    """API_SECRET_KEY가 설정된 경우 X-API-Key 헤더 검증."""
    if not API_SECRET_KEY:
        return
    key = request.headers.get("X-API-Key", "")
    if key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ─── 모델 ──────────────────────────────────────────
class BriefingRequest(BaseModel):
    briefing_type: str = "MANUAL"


class BriefingResponse(BaseModel):
    success: bool
    title: str = ""
    notion_url: str = ""
    telegram_sent: bool = False
    error: str = ""


# ─── 엔드포인트 ────────────────────────────────────
@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "timestamp": datetime.now(KST).isoformat(),
    }


@app.post(
    "/api/briefing",
    response_model=BriefingResponse,
    dependencies=[Depends(verify_api_key)],
)
async def create_briefing(req: BriefingRequest) -> BriefingResponse:
    """브리핑 생성 → Notion 저장 → 텔레그램 전송."""
    from core.briefing_runner import is_briefing_running, run_briefing

    if is_briefing_running():
        raise HTTPException(status_code=429, detail="브리핑이 이미 진행 중입니다")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, partial(run_briefing, req.briefing_type)
    )
    return BriefingResponse(
        success=result.success,
        title=result.title,
        notion_url=result.notion_url,
        telegram_sent=result.telegram_sent,
        error=result.error,
    )
