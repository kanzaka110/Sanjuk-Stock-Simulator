"""
설정 계층 로더 — claw-code 패턴 적용

5단계 설정 로딩 체인 (나중이 우선):
  1. 코드 기본값 (config/settings.py 상수)
  2. .env 파일 (python-dotenv)
  3. 환경변수 (os.environ — Docker/systemd)
  4. settings.local.json (머신별 오버라이드)
  5. CLI 인자 (런타임 오버라이드)

설정 검증으로 필수 키 누락 시 조기 실패.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent


@dataclass(frozen=True)
class ConfigValidation:
    """설정 검증 결과."""

    valid: bool
    missing_required: tuple[str, ...] = ()
    missing_optional: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def load_local_settings(path: Path | None = None) -> dict[str, Any]:
    """settings.local.json 로드 (있으면).

    프로젝트 루트의 config/settings.local.json을 읽는다.
    없으면 빈 dict 반환 (정상).
    """
    if path is None:
        path = _PROJECT_ROOT / "config" / "settings.local.json"

    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        log.info("로컬 설정 로드: %s (%d keys)", path, len(data))
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("로컬 설정 파싱 실패: %s — %s", path, e)
        return {}


def apply_overrides(overrides: dict[str, Any]) -> int:
    """설정 오버라이드를 환경변수로 주입.

    settings.local.json이나 CLI 인자로 받은 값을
    os.environ에 반영하여 config/settings.py 재로드 없이 적용.

    Returns:
        적용된 오버라이드 수
    """
    count = 0
    for key, value in overrides.items():
        env_key = key.upper()
        if value is not None:
            os.environ[env_key] = str(value)
            count += 1
    if count > 0:
        log.info("설정 오버라이드 %d건 적용", count)
    return count


def validate_config(mode: str = "briefing") -> ConfigValidation:
    """운영 모드별 필수 설정 검증.

    Args:
        mode: "briefing", "monitor", "bot", "tui"

    Returns:
        ConfigValidation
    """
    # 모든 모드 공통 필수
    required_all: list[str] = []

    # 모드별 필수
    mode_required: dict[str, list[str]] = {
        "briefing": ["GEMINI_API_KEY", "CLAUDE_API_KEY"],
        "monitor": ["CLAUDE_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
        "bot": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CLAUDE_API_KEY", "GEMINI_API_KEY"],
        "tui": [],
    }

    # 선택적 (있으면 좋지만 없어도 작동)
    optional: list[str] = [
        "KIS_APP_KEY", "KIS_APP_SECRET",
        "NOTION_API_KEY", "NOTION_DB_ID",
    ]

    required = required_all + mode_required.get(mode, [])

    missing_req = tuple(k for k in required if not os.environ.get(k))
    missing_opt = tuple(k for k in optional if not os.environ.get(k))

    warnings: list[str] = []
    if missing_opt:
        warnings.append(
            f"선택적 키 미설정 (기능 제한): {', '.join(missing_opt)}"
        )

    # KIS 설정 부분 검증
    kis_keys = ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_HTS_ID", "KIS_ACCOUNT_NO"]
    kis_set = [k for k in kis_keys if os.environ.get(k)]
    if 0 < len(kis_set) < len(kis_keys):
        warnings.append(
            f"KIS API 키 일부만 설정됨 ({len(kis_set)}/{len(kis_keys)})"
        )

    valid = len(missing_req) == 0

    if not valid:
        log.error(
            "[%s 모드] 필수 설정 누락: %s",
            mode, ", ".join(missing_req),
        )
    elif warnings:
        for w in warnings:
            log.warning("[%s 모드] %s", mode, w)

    return ConfigValidation(
        valid=valid,
        missing_required=missing_req,
        missing_optional=missing_opt,
        warnings=tuple(warnings),
    )


def init_config(mode: str = "briefing", cli_overrides: dict[str, Any] | None = None) -> ConfigValidation:
    """설정 초기화 — 5단계 로딩 체인 실행.

    1. 코드 기본값은 config/settings.py에서 이미 로드됨
    2. .env는 dotenv로 로드 (있으면)
    3. 환경변수는 os.environ에서 이미 참조됨
    4. settings.local.json 로드 + 적용
    5. CLI 오버라이드 적용

    Returns:
        ConfigValidation 결과
    """
    # Step 2: .env 로드
    try:
        from dotenv import load_dotenv
        env_path = _PROJECT_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
    except ImportError:
        pass

    # Step 4: settings.local.json
    local_settings = load_local_settings()
    if local_settings:
        apply_overrides(local_settings)

    # Step 5: CLI overrides (최우선)
    if cli_overrides:
        apply_overrides(cli_overrides)

    # 검증
    return validate_config(mode)
