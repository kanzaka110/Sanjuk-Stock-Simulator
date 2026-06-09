"""Gemini CLI subprocess 유틸리티.

Gemini SDK(API 키)의 크레딧 고갈(429) 문제를 피하기 위해 Gemini CLI를
OAuth(Google 개인계정, GOOGLE_GENAI_USE_GCA) 모드로 subprocess 호출한다.
news/sentiment/chart_vision의 폴백 경로에서 Claude CLI 다음 단계로 사용.

주의:
    - Gemini CLI의 Google One/무료 등급 지원은 2026-06-18 종료 예정
      (이후 Antigravity CLI 등으로 이전 필요). 그때까지의 한시적 폴백.

환경변수:
    GEMINI_CLI_PATH — CLI 바이너리 경로 override (기본: PATH 조회)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger(__name__)


def _resolve_cli_path() -> str | None:
    override = os.environ.get("GEMINI_CLI_PATH")
    if override:
        if os.path.exists(override):
            return override
        log.warning("GEMINI_CLI_PATH override가 존재하지 않음: %s → PATH 조회로 폴백", override)
    return shutil.which("gemini")


def _oauth_env() -> dict[str, str]:
    """OAuth(GCA) 무료 모드를 강제하는 환경. API 키를 제거해 크레딧 경로를 차단."""
    env = dict(os.environ)
    env.pop("GEMINI_API_KEY", None)
    env.pop("GOOGLE_API_KEY", None)
    env["GOOGLE_GENAI_USE_GCA"] = "true"
    return env


def _strip_json_fence(text: str) -> str:
    """```json ... ``` 펜스를 제거하고 순수 JSON 문자열만 반환."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def gemini_cli(
    prompt: str,
    *,
    model: str = "",
    timeout: int = 180,
    want_json: bool = False,
    image_paths: list[str] | None = None,
) -> str:
    """Gemini CLI를 OAuth 모드로 subprocess 호출한다. 실패 시 빈 문자열 반환.

    Args:
        prompt: 사용자 프롬프트
        model: 모델 별칭(미지정 시 CLI 기본값)
        timeout: 타임아웃 (초)
        want_json: True면 응답에서 ```json 펜스를 제거한 순수 JSON 문자열 반환
        image_paths: 멀티모달 입력 이미지 경로 목록 (@경로 문법으로 주입)
    """
    cli_path = _resolve_cli_path()
    if not cli_path:
        log.warning("Gemini CLI를 찾을 수 없음 (PATH/GEMINI_CLI_PATH 확인)")
        return ""

    # 이미지가 있으면 @경로 접두로 프롬프트에 주입
    full_prompt = prompt
    for img in image_paths or []:
        full_prompt = f"@{img} {full_prompt}"

    cmd = [
        cli_path,
        "-p", full_prompt,
        "-o", "json",
        "--skip-trust",
    ]
    if model:
        cmd += ["-m", model]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=_oauth_env(),
        )
    except subprocess.TimeoutExpired:
        log.warning("Gemini CLI 타임아웃 (%d초, model=%s)", timeout, model or "default")
        return ""
    except FileNotFoundError:
        log.error("Gemini CLI 실행 실패: %s", cli_path)
        return ""
    except Exception as e:  # noqa: BLE001
        log.warning("Gemini CLI 오류: %s", e)
        return ""

    if result.returncode != 0 or not result.stdout.strip():
        stderr_preview = result.stderr[:200] if result.stderr else ""
        log.warning(
            "Gemini CLI 실패: returncode=%d, stderr=%s",
            result.returncode, stderr_preview,
        )
        return ""

    # -o json: {"response": "...", "stats": {...}} 봉투에서 response 추출
    try:
        envelope = json.loads(result.stdout.strip())
        response = str(envelope.get("response", "")).strip()
    except json.JSONDecodeError:
        # 봉투 파싱 실패 시 원문 그대로 사용
        response = result.stdout.strip()

    if not response:
        return ""

    return _strip_json_fence(response) if want_json else response
