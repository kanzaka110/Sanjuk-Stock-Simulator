"""Claude CLI subprocess 유틸리티.

Anthropic API 대신 Claude Code CLI (Max 구독)를 subprocess로 호출해 API 비용을 $0으로 만든다.
페르소나 분석(tool_use 구조화·병렬)은 API 유지, 종합 판단(1회·텍스트 JSON)만 CLI 사용.

환경변수:
    CLAUDE_CLI_PATH — CLI 바이너리 경로 override (기본: PATH 조회)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)


def _resolve_cli_path() -> str | None:
    override = os.environ.get("CLAUDE_CLI_PATH")
    if override:
        if os.path.exists(override):
            return override
        log.warning("CLAUDE_CLI_PATH override가 존재하지 않음: %s → PATH 조회로 폴백", override)
    return shutil.which("claude")


def claude_cli(
    prompt: str,
    *,
    model: str = "opus",
    system_prompt: str = "",
    timeout: int = 180,
    json_schema: str = "",
    effort: str = "",
    allowed_tools: str = "",
    add_dirs: list[str] | None = None,
) -> str:
    """Claude CLI를 subprocess로 호출한다. 실패 시 빈 문자열 반환.

    Args:
        prompt: 사용자 프롬프트
        model: 모델 별칭("opus"/"sonnet"/"haiku") 또는 풀 ID("claude-opus-4-7")
        system_prompt: 시스템 프롬프트
        timeout: 타임아웃 (초)
        json_schema: JSON 스키마 문자열 (구조화된 출력 강제).
            지정 시 --output-format json 으로 호출하고 응답의 structured_output을
            JSON 문자열로 반환한다 (텍스트 모드의 빈 응답 문제 회피).
        effort: 탐색 깊이 (low, medium, high, xhigh, max)
        allowed_tools: 자동 승인할 내장 툴 (예: "WebSearch", "Read")
        add_dirs: 툴 접근을 허용할 추가 디렉터리 목록 (이미지 Read 등)
    """
    cli_path = _resolve_cli_path()
    if not cli_path:
        log.warning("Claude CLI를 찾을 수 없음 (PATH/CLAUDE_CLI_PATH 확인)")
        return ""

    cmd = [
        cli_path,
        "-p", prompt,
        "--model", model,
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    # json_schema 사용 시 구조화 출력을 안정적으로 받으려면 JSON 출력 포맷 필수.
    # (텍스트 모드 + --json-schema 조합은 빈 stdout을 반환하는 경우가 있음)
    structured = bool(json_schema)
    if structured:
        cmd += ["--output-format", "json", "--json-schema", json_schema]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    if effort:
        cmd += ["--effort", effort]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    for d in add_dirs or []:
        cmd += ["--add-dir", d]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("Claude CLI 타임아웃 (%d초, model=%s)", timeout, model)
        return ""
    except FileNotFoundError:
        log.error("Claude CLI 실행 실패: %s", cli_path)
        return ""
    except Exception as e:
        log.warning("Claude CLI 오류: %s", e)
        return ""

    if result.returncode != 0 or not result.stdout.strip():
        stderr_preview = result.stderr[:200] if result.stderr else ""
        log.warning(
            "Claude CLI 실패: returncode=%d, stderr=%s",
            result.returncode, stderr_preview,
        )
        return ""

    stdout = result.stdout.strip()
    if not structured:
        return stdout

    # --output-format json: 결과 봉투를 파싱해 structured_output을 우선 반환.
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("Claude CLI JSON 봉투 파싱 실패: %s", stdout[:200])
        return ""

    so = envelope.get("structured_output")
    if so is not None:
        return json.dumps(so, ensure_ascii=False)

    # structured_output 없으면 result 텍스트로 폴백 (스키마 미검증 응답)
    return str(envelope.get("result", "")).strip()
