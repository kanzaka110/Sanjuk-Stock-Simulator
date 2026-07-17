#!/usr/bin/env -S -i HOME=/home/kanzaka110 PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC /bin/bash --noprofile --norc
set -uo pipefail

umask 077
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP BASH_ENV ENV CDPATH LD_PRELOAD LD_LIBRARY_PATH

readonly REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"
readonly WRAPPER="$REPO_DIR/deploy/run_customs_export.sh"
readonly CAPTURE_HELPER="$REPO_DIR/deploy/capture_bounded_output.py"
readonly STATE_DIR="/home/kanzaka110/.local/state/sanjuk-stock-simulator"
readonly OUTPUT_LIMIT_BYTES=1048576
readonly LOGGER_MESSAGE_BYTES=4096
readonly ID_BIN="/usr/bin/id"
readonly STAT_BIN="/usr/bin/stat"
readonly MKDIR_BIN="/usr/bin/mkdir"
readonly CHMOD_BIN="/usr/bin/chmod"
readonly MKTEMP_BIN="/usr/bin/mktemp"
readonly RM_BIN="/bin/rm"
readonly LOGGER_BIN="/usr/bin/logger"
readonly LOGGER_REQUIRED_UID=0
readonly TIMEOUT_BIN="/usr/bin/timeout"
readonly PYTHON3_BIN="/usr/bin/python3"

OUTPUT_FILE=""
STATUS_FILE=""
LOGGER_GROUP_PID=""

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

kill_logger_group() {
    [[ "$LOGGER_GROUP_PID" =~ ^[1-9][0-9]*$ ]] || return 0
    kill -KILL -- "-$LOGGER_GROUP_PID" 2>/dev/null || true
    LOGGER_GROUP_PID=""
}

cleanup() {
    kill_logger_group
    [[ -z "$OUTPUT_FILE" ]] || "$RM_BIN" -f -- "$OUTPUT_FILE"
    [[ -z "$STATUS_FILE" ]] || "$RM_BIN" -f -- "$STATUS_FILE"
}

terminate_and_exit() {
    local signal_number="$1"
    trap - EXIT HUP INT TERM QUIT
    cleanup
    exit "$((128 + signal_number))"
}

hold_for_outer_kill() {
    trap '' EXIT HUP INT TERM QUIT
    cleanup
    OUTPUT_FILE=""
    while true; do
        /bin/sleep 1
    done
}

trap cleanup EXIT
trap 'terminate_and_exit 1' HUP
trap 'terminate_and_exit 2' INT
trap hold_for_outer_kill TERM
trap 'terminate_and_exit 3' QUIT

for binary in "$ID_BIN" "$STAT_BIN" "$MKDIR_BIN" "$CHMOD_BIN" \
    "$MKTEMP_BIN" "$RM_BIN" "$LOGGER_BIN" "$TIMEOUT_BIN" \
    "$PYTHON3_BIN"; do
    [[ -x "$binary" ]] || exit 73
done
[[ -f "$LOGGER_BIN" && ! -L "$LOGGER_BIN" ]] || exit 73
logger_uid=$("$STAT_BIN" -c %u -- "$LOGGER_BIN" 2>/dev/null) || exit 73
logger_mode=$("$STAT_BIN" -c %a -- "$LOGGER_BIN" 2>/dev/null) || exit 73
[[ "$logger_uid" == "$LOGGER_REQUIRED_UID" ]] || exit 73
mode_has_no_group_world_write "$logger_mode" || exit 73
"$LOGGER_BIN" --no-act --tag sanjuk-customs-export \
    --size "$LOGGER_MESSAGE_BYTES" capability-probe >/dev/null 2>&1 || exit 73

run_uid=$("$ID_BIN" -u 2>/dev/null) || exit 73
[[ "$run_uid" =~ ^[0-9]+$ ]] || exit 73
for directory in "$REPO_DIR" "$REPO_DIR/deploy"; do
    is_secure_owned_directory "$directory" "$run_uid" || exit 73
done
[[ -f "$WRAPPER" && ! -L "$WRAPPER" && -x "$WRAPPER" ]] || exit 73
wrapper_mode=$("$STAT_BIN" -c %a -- "$WRAPPER" 2>/dev/null) || exit 73
wrapper_uid=$("$STAT_BIN" -c %u -- "$WRAPPER" 2>/dev/null) || exit 73
wrapper_links=$("$STAT_BIN" -c %h -- "$WRAPPER" 2>/dev/null) || exit 73
(( (8#$wrapper_mode & 8#022) == 0 )) || exit 73
[[ "$wrapper_uid" == "$run_uid" ]] || exit 73
[[ "$wrapper_links" == "1" ]] || exit 73
[[ -f "$CAPTURE_HELPER" && ! -L "$CAPTURE_HELPER" \
    && -r "$CAPTURE_HELPER" ]] || exit 73
capture_mode=$("$STAT_BIN" -c %a -- "$CAPTURE_HELPER" 2>/dev/null) || exit 73
capture_uid=$("$STAT_BIN" -c %u -- "$CAPTURE_HELPER" 2>/dev/null) || exit 73
capture_links=$("$STAT_BIN" -c %h -- "$CAPTURE_HELPER" 2>/dev/null) || exit 73
(( (8#$capture_mode & 8#022) == 0 )) || exit 73
[[ "$capture_uid" == "$run_uid" ]] || exit 73
[[ "$capture_links" == "1" ]] || exit 73

[[ ! -L "$STATE_DIR" ]] || exit 73
"$MKDIR_BIN" -p -m 700 -- "$STATE_DIR" || exit 73
[[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] || exit 73
"$CHMOD_BIN" 700 -- "$STATE_DIR" || exit 73
[[ "$("$STAT_BIN" -c %u:%a -- "$STATE_DIR" 2>/dev/null)" == \
    "${run_uid}:700" ]] || exit 73

OUTPUT_FILE=$("$MKTEMP_BIN" "$STATE_DIR/.customs-export-output.XXXXXX") || exit 73
[[ -f "$OUTPUT_FILE" && ! -L "$OUTPUT_FILE" ]] || exit 73
[[ "$("$STAT_BIN" -c %u:%a -- "$OUTPUT_FILE" 2>/dev/null)" == \
    "${run_uid}:600" ]] || exit 73
STATUS_FILE=$("$MKTEMP_BIN" "$STATE_DIR/.customs-export-status.XXXXXX") || exit 73
[[ -f "$STATUS_FILE" && ! -L "$STATUS_FILE" ]] || exit 73
[[ "$("$STAT_BIN" -c %u:%a -- "$STATUS_FILE" 2>/dev/null)" == \
    "${run_uid}:600" ]] || exit 73

(
    set +e
    /bin/bash --noprofile --norc "$WRAPPER" 2>&1 \
        | "$PYTHON3_BIN" -I -S "$CAPTURE_HELPER" \
            "$OUTPUT_FILE" "$OUTPUT_LIMIT_BYTES"
    pipeline_status=("${PIPESTATUS[@]}")
    printf '%s %s\n' "${pipeline_status[0]}" "${pipeline_status[1]}" \
        >"$STATUS_FILE"
) &
workload_pid=$!
wait "$workload_pid"
pipeline_rc=$?
(( pipeline_rc == 0 )) || exit 73
workload_rc=""
capture_rc=""
extra_status=""
IFS=' ' read -r workload_rc capture_rc extra_status <"$STATUS_FILE" || exit 73
[[ "$workload_rc" =~ ^([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$ ]] \
    || exit 73
[[ "$capture_rc" =~ ^([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$ ]] \
    || exit 73
[[ -z "$extra_status" ]] || exit 73

"$TIMEOUT_BIN" --signal=TERM --kill-after=5s 10s \
    "$LOGGER_BIN" --tag sanjuk-customs-export \
        --size "$LOGGER_MESSAGE_BYTES" <"$OUTPUT_FILE" &
LOGGER_GROUP_PID=$!
wait "$LOGGER_GROUP_PID"
logger_rc=$?
LOGGER_GROUP_PID=""

if (( workload_rc != 0 )); then
    exit "$workload_rc"
fi
if (( capture_rc != 0 )); then
    exit "$capture_rc"
fi
exit "$logger_rc"
