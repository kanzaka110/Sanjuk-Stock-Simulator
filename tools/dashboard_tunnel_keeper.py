"""tools/dashboard_tunnel_keeper.py — 대시보드 + Quick Tunnel 감시/자동복구.

cron 5분 간격 실행 가정. 역할:
1. 대시보드(127.0.0.1:8787) 생존 확인 → 죽었으면 재기동
2. cloudflared Quick Tunnel 생존 확인 → 죽었으면 재기동
3. 터널 URL이 바뀌면 텔레그램으로 새 주소 알림

실행: ./venv/bin/python tools/dashboard_tunnel_keeper.py
사전 준비: db/data/.env.dashboard 에 DASHBOARD_USER/DASHBOARD_PASS (chmod 600)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DASH_URL = "http://127.0.0.1:8787/api/health"
DASH_ENV_FILE = BASE / "db" / "data" / ".env.dashboard"  # gitignore(.env.*) 매칭
STATE_FILE = BASE / "db" / "data" / "dashboard_tunnel_url.txt"
TUNNEL_LOG = Path("/tmp/cloudflared_keeper.log")
VENV_PY = BASE / "venv" / "bin" / "python"
CLOUDFLARED = "/usr/local/bin/cloudflared"

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _log(msg: str) -> None:
    print(f"[keeper] {msg}", flush=True)


def _notify(text: str) -> None:
    try:
        from core.telegram import send_simple_message

        send_simple_message(text)
    except Exception as e:  # 알림 실패가 keeper를 죽이면 안 됨
        _log(f"telegram notify failed: {e}")


def _pgrep(pattern: str) -> bool:
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
    return r.returncode == 0


def _bootstrap_env_file() -> None:
    """영구 env 파일이 없으면 /tmp/dashboard.env.* 최신본에서 1회 복사."""
    if DASH_ENV_FILE.exists():
        return
    candidates = sorted(
        Path("/tmp").glob("dashboard.env.*"), key=lambda p: p.stat().st_mtime
    )
    if not candidates:
        return
    DASH_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASH_ENV_FILE.write_bytes(candidates[-1].read_bytes())
    DASH_ENV_FILE.chmod(0o600)
    _log(f"bootstrapped env file from {candidates[-1].name}")


def _load_dash_env() -> dict[str, str] | None:
    """DASHBOARD_USER/PASS env 파일 로드. 값은 로그/알림에 절대 출력하지 않는다."""
    _bootstrap_env_file()
    if not DASH_ENV_FILE.exists():
        return None
    env = dict(os.environ)
    for line in DASH_ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def dashboard_alive() -> bool:
    try:
        requests.get(DASH_URL, timeout=5)  # 401이어도 응답 자체가 생존 증거
        return True
    except requests.RequestException:
        return False


def ensure_dashboard() -> bool:
    if dashboard_alive():
        return True
    env = _load_dash_env()
    if env is None:
        _notify(
            "⚠️ 대시보드 다운 + 인증 env 파일 없음\n"
            f"`{DASH_ENV_FILE}` 를 만들어야 자동 재기동 가능 "
            "(DASHBOARD_USER/DASHBOARD_PASS)"
        )
        return False
    _log("dashboard down — restarting")
    with open("/tmp/dashboard.log", "ab") as out:
        subprocess.Popen(
            [str(VENV_PY), "main.py", "dashboard"],
            cwd=str(BASE),
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    for _ in range(15):
        time.sleep(2)
        if dashboard_alive():
            _notify("🔄 대시보드 자동 재기동 완료 (프로세스 다운 감지)")
            return True
    _notify("🚨 대시보드 재기동 실패 — /tmp/dashboard.log 확인 필요")
    return False


def current_tunnel_url() -> str | None:
    if not TUNNEL_LOG.exists():
        return None
    matches = _URL_RE.findall(TUNNEL_LOG.read_text(errors="ignore"))
    return matches[-1] if matches else None


def ensure_tunnel() -> str | None:
    if _pgrep("cloudflared tunnel --url"):
        return current_tunnel_url()
    _log("tunnel down — restarting")
    TUNNEL_LOG.write_text("")  # 이전 URL 잔재 제거
    with open(TUNNEL_LOG, "ab") as out:
        subprocess.Popen(
            [CLOUDFLARED, "tunnel", "--url", "http://localhost:8787"],
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    for _ in range(15):
        time.sleep(2)
        url = current_tunnel_url()
        if url:
            return url
    _notify("🚨 Cloudflare 터널 재기동 실패 — /tmp/cloudflared_keeper.log 확인 필요")
    return None


def main() -> int:
    dash_ok = ensure_dashboard()
    url = ensure_tunnel()
    if url:
        prev = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""
        if url != prev:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(url + "\n")
            _notify(
                "📡 *대시보드 주소 변경*\n"
                f"{url}\n"
                "이전 주소는 만료됨. 새 주소로 접속해줘. (Basic Auth 동일)"
            )
            _log(f"url changed → {url}")
        else:
            _log(f"url unchanged: {url}")
    return 0 if (dash_ok and url) else 1


if __name__ == "__main__":
    raise SystemExit(main())
