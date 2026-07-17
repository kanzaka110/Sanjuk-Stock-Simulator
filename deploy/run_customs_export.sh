#!/bin/bash
set -uo pipefail

umask 077
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP BASH_ENV ENV CDPATH LD_PRELOAD LD_LIBRARY_PATH

readonly REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"
readonly STATE_DIR="/home/kanzaka110/.local/state/sanjuk-stock-simulator"
readonly LOCK_FILE="$STATE_DIR/customs-export.lock"
readonly PYTHON_BIN="$REPO_DIR/venv/bin/python"
readonly COLLECTOR="$REPO_DIR/tools/collect_customs_export_observations.py"
readonly FLOCK_BIN="/usr/bin/flock"
readonly TIMEOUT_BIN="/usr/bin/timeout"
readonly ID_BIN="/usr/bin/id"
readonly STAT_BIN="/usr/bin/stat"
readonly MKDIR_BIN="/usr/bin/mkdir"

RUN_ID="customs-scheduled-unavailable"
TIMESTAMP_UTC="1970-01-01T00:00:00Z"

refresh_timestamp() {
    TZ=UTC printf -v TIMESTAMP_UTC '%(%Y-%m-%dT%H:%M:%SZ)T' -1
}

emit_event() {
    local status="$1"
    local exit_code="$2"
    local ok="$3"
    refresh_timestamp
    printf '{"exit_code":%d,"ok":%s,"run_id":"%s","scheduler_status":"%s","timestamp_utc":"%s"}\n' \
        "$exit_code" "$ok" "$RUN_ID" "$status" "$TIMESTAMP_UTC"
}

fail_runtime() {
    emit_event "runtime_missing" 1 false
    exit 1
}

mode_has_no_group_world_write() {
    local mode="$1"
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] || return 1
    (( (8#$mode & 8#22) == 0 ))
}

is_secure_owned_directory() {
    local path="$1"
    local expected_uid="$2"
    local mode uid
    [[ -d "$path" && ! -L "$path" ]] || return 1
    uid=$("$STAT_BIN" -c %u -- "$path" 2>/dev/null) || return 1
    mode=$("$STAT_BIN" -c %a -- "$path" 2>/dev/null) || return 1
    [[ "$uid" == "$expected_uid" ]] || return 1
    mode_has_no_group_world_write "$mode"
}

uuid=""
IFS= read -r uuid < /proc/sys/kernel/random/uuid || fail_runtime
[[ "$uuid" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] \
    || fail_runtime
RUN_ID="customs-scheduled-$uuid"

[[ -x "$FLOCK_BIN" && -x "$TIMEOUT_BIN" && -x "$ID_BIN" && \
    -x "$STAT_BIN" && -x "$MKDIR_BIN" ]] \
    || fail_runtime
run_uid=$("$ID_BIN" -u 2>/dev/null) || fail_runtime
[[ "$run_uid" =~ ^[0-9]+$ ]] || fail_runtime
for directory in "$REPO_DIR" "$REPO_DIR/tools" "$REPO_DIR/venv" \
    "$REPO_DIR/venv/bin"; do
    is_secure_owned_directory "$directory" "$run_uid" || fail_runtime
done
[[ -x "$PYTHON_BIN" ]] || fail_runtime
python_uid=$("$STAT_BIN" -Lc %u -- "$PYTHON_BIN" 2>/dev/null) || fail_runtime
python_mode=$("$STAT_BIN" -Lc %a -- "$PYTHON_BIN" 2>/dev/null) || fail_runtime
[[ "$python_uid" == "0" || "$python_uid" == "$run_uid" ]] || fail_runtime
mode_has_no_group_world_write "$python_mode" || fail_runtime
[[ -f "$COLLECTOR" && ! -L "$COLLECTOR" && -O "$COLLECTOR" && -r "$COLLECTOR" ]] \
    || fail_runtime
collector_mode=$("$STAT_BIN" -c %a -- "$COLLECTOR" 2>/dev/null) || fail_runtime
collector_links=$("$STAT_BIN" -c %h -- "$COLLECTOR" 2>/dev/null) || fail_runtime
mode_has_no_group_world_write "$collector_mode" || fail_runtime
[[ "$collector_links" == "1" ]] || fail_runtime

if [[ ! -e "$STATE_DIR" && ! -L "$STATE_DIR" ]]; then
    "$MKDIR_BIN" -p -m 700 -- "$STATE_DIR" || fail_runtime
fi
[[ -d "$STATE_DIR" && ! -L "$STATE_DIR" && -O "$STATE_DIR" ]] || fail_runtime
[[ "$("$STAT_BIN" -c %a -- "$STATE_DIR" 2>/dev/null)" == "700" ]] || fail_runtime
if [[ ! -e "$LOCK_FILE" && ! -L "$LOCK_FILE" ]]; then
    : >> "$LOCK_FILE" || fail_runtime
fi
[[ -f "$LOCK_FILE" && ! -L "$LOCK_FILE" && -O "$LOCK_FILE" ]] || fail_runtime
[[ "$("$STAT_BIN" -c %a -- "$LOCK_FILE" 2>/dev/null)" == "600" ]] || fail_runtime

exec 9<>"$LOCK_FILE" || fail_runtime
"$FLOCK_BIN" -n -E 75 9
lock_rc=$?
if (( lock_rc != 0 )); then
    if (( lock_rc == 75 )); then
        emit_event "skipped_locked" 0 true
        exit 0
    fi
    emit_event "lock_failed" "$lock_rc" false
    exit "$lock_rc"
fi

"$TIMEOUT_BIN" --foreground --signal=TERM --kill-after=30s 300s \
    "$PYTHON_BIN" -u "$COLLECTOR" \
    --collection-mode scheduled_live \
    --run-id "$RUN_ID" \
    9>&-
rc=$?

case "$rc" in
    0)
        emit_event "success" 0 true
        exit 0
        ;;
    2)
        emit_event "typed_failure" 2 false
        exit 2
        ;;
    124)
        emit_event "timeout" 124 false
        exit 124
        ;;
    137)
        emit_event "timeout_killed" 137 false
        exit 137
        ;;
    *)
        emit_event "failed" "$rc" false
        exit "$rc"
        ;;
esac
