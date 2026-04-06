"""
산적 주식 시뮬레이터 — 엔트리포인트
"""

import os
import sys
from pathlib import Path

# .env 로드
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from terminal.app import StockSimulatorApp


def main() -> None:
    app = StockSimulatorApp()
    app.run()


if __name__ == "__main__":
    main()
