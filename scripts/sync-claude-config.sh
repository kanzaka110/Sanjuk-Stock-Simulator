#!/usr/bin/env bash
# Claude Code 설정 동기화 스크립트 (외부 PC ↔ GCP)
#
# 주의(2026-05-18): GCP를 직접 운영(kanzaka110)으로 전환한 뒤 이 스크립트는
# 사실상 사용하지 않습니다. 외부 PC에서 GCP로 메모리 백업이 필요할 때만 활용.
# SSH는 ohmil 계정으로 접근하지만 실제 운영 메모리는 kanzaka110에 있습니다.
#
# 사용법:
#   ./scripts/sync-claude-config.sh push   # 로컬 → GCP
#   ./scripts/sync-claude-config.sh pull   # GCP → 로컬
#   ./scripts/sync-claude-config.sh diff   # 차이만 확인

set -euo pipefail

GCP_HOST="ohmil@35.238.77.143"
# 실제 운영 메모리는 kanzaka110 계정 아래에 있음. 쓰기 시 sudo 필요할 수 있음.
GCP_CLAUDE_DIR="/home/kanzaka110/.claude-rc"

# 동기화 대상 (상대 경로, ~/.claude/ 기준)
SYNC_PATHS=(
    "rules/"
    "settings.json"
    "projects/C--dev-Sanjuk-Stock-Simulator/memory/"
)

# 로컬 ~/.claude 경로 (OS별)
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    LOCAL_CLAUDE_DIR="$USERPROFILE/.claude"
else
    LOCAL_CLAUDE_DIR="$HOME/.claude"
fi

usage() {
    echo "Usage: $0 {push|pull|diff}"
    echo ""
    echo "  push  - 로컬 → GCP (로컬 설정을 GCP로 업로드)"
    echo "  pull  - GCP → 로컬 (GCP 설정을 로컬로 다운로드)"
    echo "  diff  - 차이점만 확인 (dry-run)"
    exit 1
}

sync_push() {
    echo "=== 로컬 → GCP 동기화 ==="
    for path in "${SYNC_PATHS[@]}"; do
        local src="$LOCAL_CLAUDE_DIR/$path"
        local dst="$GCP_CLAUDE_DIR/$path"

        if [[ ! -e "$src" ]]; then
            echo "  SKIP: $path (로컬에 없음)"
            continue
        fi

        # 디렉토리면 상위 경로 생성
        if [[ "$path" == */ ]]; then
            ssh "$GCP_HOST" "mkdir -p '$dst'"
            rsync -avz --delete "$src" "$GCP_HOST:$(dirname "$dst")/"
        else
            ssh "$GCP_HOST" "mkdir -p '$(dirname "$dst")'"
            rsync -avz "$src" "$GCP_HOST:$dst"
        fi
        echo "  OK: $path"
    done
    echo "=== 완료 ==="
}

sync_pull() {
    echo "=== GCP → 로컬 동기화 ==="
    for path in "${SYNC_PATHS[@]}"; do
        local src="$GCP_CLAUDE_DIR/$path"
        local dst="$LOCAL_CLAUDE_DIR/$path"

        # 디렉토리면 상위 경로 생성
        if [[ "$path" == */ ]]; then
            mkdir -p "$dst"
            rsync -avz --delete "$GCP_HOST:$src" "$(dirname "$dst")/"
        else
            mkdir -p "$(dirname "$dst")"
            rsync -avz "$GCP_HOST:$src" "$dst"
        fi
        echo "  OK: $path"
    done
    echo "=== 완료 ==="
}

sync_diff() {
    echo "=== 차이점 확인 (dry-run) ==="
    for path in "${SYNC_PATHS[@]}"; do
        local src="$LOCAL_CLAUDE_DIR/$path"
        echo "--- $path ---"
        if [[ "$path" == */ ]]; then
            rsync -avzn --delete "$src" "$GCP_HOST:$GCP_CLAUDE_DIR/$path" 2>/dev/null || echo "  (비교 불가)"
        else
            rsync -avzn "$src" "$GCP_HOST:$GCP_CLAUDE_DIR/$path" 2>/dev/null || echo "  (비교 불가)"
        fi
    done
}

[[ $# -lt 1 ]] && usage

case "$1" in
    push) sync_push ;;
    pull) sync_pull ;;
    diff)  sync_diff ;;
    *)     usage ;;
esac
