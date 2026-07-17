#!/usr/bin/env -S -i PATH=/usr/bin:/bin CUSTOMS_CRON_INSTALL_CLEAN=1 /bin/bash --noprofile --norc
# SECURITY: direct execution is rejected. A trusted operator must install and
# verify the reviewed launcher and this script as root:root mode 0700, then
# execute only the fixed launcher. The launcher creates the clean Bash process.
set -uo pipefail

if [[ "${CUSTOMS_CRON_INSTALL_CLEAN:-}" != "1" ]]; then
    printf '%s\n' '{"installer_status":"preflight_failed","ok":false}'
    exit 1
fi

umask 077
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP BASH_ENV ENV CDPATH LD_PRELOAD LD_LIBRARY_PATH

readonly RUN_USER="kanzaka110"
readonly REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"
readonly WRAPPER="$REPO_DIR/deploy/run_customs_export.sh"
readonly LOGGED_WRAPPER="$REPO_DIR/deploy/run_customs_export_logged.sh"
readonly CAPTURE_HELPER="$REPO_DIR/deploy/capture_bounded_output.py"
readonly TARGET_DIR="/etc/cron.d"
readonly TARGET_FILE="$TARGET_DIR/sanjuk-customs-export"
readonly STATE_DIR="/var/lib/sanjuk-stock-simulator"
readonly LAUNCHER_PATH="$STATE_DIR/install-customs-export-cron-launcher"
readonly INSTALLER_PATH="$STATE_DIR/install-customs-export-cron.sh"
readonly BACKUP_DIR="$STATE_DIR/cron.d-backups"
readonly REQUIRED_EUID=0
readonly INSTALL_UID=0
readonly INSTALL_GID=0
readonly SOURCE_SHA256="698b5c0c418ee1cdcc4ffb80a59eab8a7b4b4b0bb7f3b4f3ad5cd0f7646a84a8"
readonly SOURCE_SIZE=928
readonly CRONTAB_BIN="/usr/bin/crontab"
readonly SHA256SUM_BIN="/usr/bin/sha256sum"
readonly STAT_BIN="/usr/bin/stat"
readonly MKDIR_BIN="/usr/bin/mkdir"
readonly CHMOD_BIN="/usr/bin/chmod"
readonly INSTALL_BIN="/usr/bin/install"
readonly MKTEMP_BIN="/usr/bin/mktemp"
readonly CMP_BIN="/usr/bin/cmp"
readonly RM_BIN="/bin/rm"
readonly MV_BIN="/bin/mv"
readonly ID_BIN="/usr/bin/id"
readonly LOGGER_BIN="/usr/bin/logger"
readonly LOGGER_MESSAGE_BYTES=4096
readonly CRON_TIMEOUT_BIN="/usr/bin/timeout"
readonly PYTHON3_BIN="/usr/bin/python3"
readonly READLINK_BIN="/usr/bin/readlink"

STAGING=""
ROLLBACK_STAGING=""
HAD_TARGET=false
BACKUP_PATH=""
BACKUP_COMPLETE=false
ROLLBACK_REQUIRED=false
ROLLBACK_ACTIVE=false

emit_installer_event() {
    local status="$1"
    local ok="$2"
    printf '{"installer_status":"%s","ok":%s}\n' "$status" "$ok"
}

fail_install() {
    emit_installer_event "$1" false
    exit "${2:-1}"
}

cleanup() {
    [[ -z "$STAGING" ]] || "$RM_BIN" -f -- "$STAGING"
    [[ -z "$ROLLBACK_STAGING" ]] || "$RM_BIN" -f -- "$ROLLBACK_STAGING"
    if [[ -n "$BACKUP_PATH" && "$BACKUP_COMPLETE" != "true" ]]; then
        "$RM_BIN" -f -- "$BACKUP_PATH"
    fi
}

transactional_exit() {
    local exit_code=$?
    trap - EXIT
    if [[ "$ROLLBACK_REQUIRED" == "true" && "$ROLLBACK_ACTIVE" != "true" ]]; then
        ROLLBACK_ACTIVE=true
        (( exit_code != 0 )) || exit_code=1
        rollback_after_verify_failure "interrupted" "$exit_code"
    fi
    cleanup
    exit "$exit_code"
}
trap transactional_exit EXIT
trap 'trap - HUP INT TERM QUIT; exit 129' HUP
trap 'trap - HUP INT TERM QUIT; exit 130' INT
trap 'trap - HUP INT TERM QUIT; exit 143' TERM
trap 'trap - HUP INT TERM QUIT; exit 131' QUIT

mode_has_no_group_world_write() {
    local mode="$1"
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] || return 1
    (( (8#$mode & 8#22) == 0 ))
}

is_secure_source_artifact() {
    local path="$1"
    local expected_uid="$2"
    local links mode uid
    [[ -f "$path" && ! -L "$path" ]] || return 1
    uid=$("$STAT_BIN" -c %u -- "$path" 2>/dev/null) || return 1
    mode=$("$STAT_BIN" -c %a -- "$path" 2>/dev/null) || return 1
    links=$("$STAT_BIN" -c %h -- "$path" 2>/dev/null) || return 1
    [[ "$uid" == "$expected_uid" && "$links" == "1" ]] || return 1
    mode_has_no_group_world_write "$mode"
}

is_secure_source_directory() {
    local path="$1"
    local expected_uid="$2"
    local mode uid
    [[ -d "$path" && ! -L "$path" ]] || return 1
    uid=$("$STAT_BIN" -c %u -- "$path" 2>/dev/null) || return 1
    mode=$("$STAT_BIN" -c %a -- "$path" 2>/dev/null) || return 1
    [[ "$uid" == "$expected_uid" ]] || return 1
    mode_has_no_group_world_write "$mode"
}

has_expected_cron_users() {
    local path="$1"
    local line minute hour day month weekday user command
    local active_count=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[0-9] ]] || continue
        minute=""
        hour=""
        day=""
        month=""
        weekday=""
        user=""
        command=""
        read -r minute hour day month weekday user command <<< "$line"
        [[ "$user" == "$RUN_USER" && -n "$command" ]] || return 1
        (( active_count += 1 ))
    done < "$path"
    (( active_count == 2 ))
}

is_secure_root_target() {
    local path="$1"
    [[ -f "$path" && ! -L "$path" ]] || return 1
    [[ "$("$STAT_BIN" -c %u:%g:%a -- "$path" 2>/dev/null)" == \
        "${INSTALL_UID}:${INSTALL_GID}:600" ]]
}

is_approved_installer_copy() {
    local launcher_inode parent_exe parent_inode
    [[ "${CUSTOMS_CRON_INSTALL_CLEAN:-}" == "1" ]] || return 1
    [[ "${BASH_SOURCE[0]}" == "$INSTALLER_PATH" ]] || return 1
    [[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] || return 1
    [[ "$("$STAT_BIN" -c %u:%g:%a -- "$STATE_DIR" 2>/dev/null)" == \
        "${INSTALL_UID}:${INSTALL_GID}:700" ]] \
        || return 1
    [[ -f "$INSTALLER_PATH" && ! -L "$INSTALLER_PATH" ]] || return 1
    [[ "$("$STAT_BIN" -c %u:%g:%a -- "$INSTALLER_PATH" 2>/dev/null)" == \
        "${INSTALL_UID}:${INSTALL_GID}:700" ]] \
        || return 1
    [[ -f "$LAUNCHER_PATH" && ! -L "$LAUNCHER_PATH" ]] || return 1
    [[ "$("$STAT_BIN" -c %u:%g:%a -- "$LAUNCHER_PATH" 2>/dev/null)" == \
        "${INSTALL_UID}:${INSTALL_GID}:700" ]] \
        || return 1
    parent_exe=$("$READLINK_BIN" -f -- "/proc/$PPID/exe" 2>/dev/null) \
        || return 1
    [[ "$parent_exe" == "$LAUNCHER_PATH" ]] || return 1
    parent_inode=$("$STAT_BIN" -Lc %d:%i -- "/proc/$PPID/exe" 2>/dev/null) \
        || return 1
    launcher_inode=$("$STAT_BIN" -Lc %d:%i -- "$LAUNCHER_PATH" 2>/dev/null) \
        || return 1
    [[ "$parent_inode" == "$launcher_inode" ]]
}

copy_approved_source() {
    "$PYTHON3_BIN" -I - "$REPO_DIR" "deploy/customs-export.cron.d" \
        "$STAGING" "$run_user_uid" \
        "$INSTALL_UID" "$INSTALL_GID" "$SOURCE_SIZE" <<'PY'
import os
import stat
import sys

repo, relative_source, target, source_uid, target_uid, target_gid, expected_size = sys.argv[1:]
source_uid = int(source_uid)
target_uid = int(target_uid)
target_gid = int(target_gid)
expected_size = int(expected_size)
source_fd = -1
target_fd = -1
repo_fd = -1
deploy_fd = -1
try:
    repo_fd = os.open(
        repo,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    repo_stat = os.fstat(repo_fd)
    if not (
        stat.S_ISDIR(repo_stat.st_mode)
        and repo_stat.st_uid == source_uid
        and (stat.S_IMODE(repo_stat.st_mode) & 0o022) == 0
    ):
        raise OSError
    deploy_component, source_component = relative_source.split("/", 1)
    if deploy_component != "deploy" or "/" in source_component:
        raise OSError
    deploy_fd = os.open(
        deploy_component,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        dir_fd=repo_fd,
    )
    deploy_stat = os.fstat(deploy_fd)
    if not (
        stat.S_ISDIR(deploy_stat.st_mode)
        and deploy_stat.st_uid == source_uid
        and (stat.S_IMODE(deploy_stat.st_mode) & 0o022) == 0
    ):
        raise OSError
    source_fd = os.open(
        source_component,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=deploy_fd,
    )
    source_stat = os.fstat(source_fd)
    if not (
        stat.S_ISREG(source_stat.st_mode)
        and source_stat.st_uid == source_uid
        and (stat.S_IMODE(source_stat.st_mode) & 0o022) == 0
        and source_stat.st_nlink == 1
        and source_stat.st_size == expected_size
    ):
        raise OSError

    target_fd = os.open(
        target,
        os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    target_stat = os.fstat(target_fd)
    if not (
        stat.S_ISREG(target_stat.st_mode)
        and target_stat.st_uid == target_uid
        and target_stat.st_gid == target_gid
        and stat.S_IMODE(target_stat.st_mode) == 0o600
        and target_stat.st_nlink == 1
    ):
        raise OSError

    remaining = expected_size
    while remaining:
        chunk = os.read(source_fd, min(remaining, 65536))
        if not chunk:
            raise OSError
        view = memoryview(chunk)
        while view:
            written = os.write(target_fd, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        remaining -= len(chunk)
    if os.read(source_fd, 1):
        raise OSError

    os.fchmod(target_fd, 0o600)
    target_stat = os.fstat(target_fd)
    if stat.S_IMODE(target_stat.st_mode) != 0o600:
        raise OSError
    os.fsync(target_fd)
except Exception:
    sys.exit(1)
finally:
    if target_fd >= 0:
        os.close(target_fd)
    if source_fd >= 0:
        os.close(source_fd)
    if deploy_fd >= 0:
        os.close(deploy_fd)
    if repo_fd >= 0:
        os.close(repo_fd)
PY
}

fsync_approved_path() {
    local kind="$1"
    local path="$2"
    local uid="$3"
    local gid="$4"
    local mode="$5"
    "$PYTHON3_BIN" -I - "$kind" "$path" "$uid" "$gid" "$mode" <<'PY'
import os
import stat
import sys

kind, path, uid, gid, mode = sys.argv[1:]
uid = int(uid)
gid = int(gid)
mode = int(mode, 8)
fd = -1
try:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if kind == "directory":
        flags |= os.O_DIRECTORY
    elif kind != "file":
        raise OSError
    fd = os.open(path, flags)
    metadata = os.fstat(fd)
    expected_type = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if not (
        expected_type(metadata.st_mode)
        and metadata.st_uid == uid
        and metadata.st_gid == gid
        and stat.S_IMODE(metadata.st_mode) == mode
    ):
        raise OSError
    os.fsync(fd)
except Exception:
    sys.exit(1)
finally:
    if fd >= 0:
        os.close(fd)
PY
}

fail_rollback() {
    cleanup
    emit_installer_event "rollback_failed" false
    exit 2
}

rollback_after_verify_failure() {
    local original_status="$1"
    local exit_code="${2:-1}"

    ROLLBACK_ACTIVE=true
    trap '' HUP INT TERM QUIT

    if [[ "$HAD_TARGET" == "true" ]]; then
        [[ -n "$BACKUP_PATH" && -f "$BACKUP_PATH" && ! -L "$BACKUP_PATH" ]] \
            || fail_rollback
        [[ "$("$STAT_BIN" -c %u:%g:%a -- "$BACKUP_PATH" 2>/dev/null)" == \
            "${INSTALL_UID}:${INSTALL_GID}:600" ]] \
            || fail_rollback
        ROLLBACK_STAGING=$("$MKTEMP_BIN" \
            "$TARGET_DIR/.sanjuk-customs-export.rollback.XXXXXX") \
            || fail_rollback
        "$INSTALL_BIN" -o "$INSTALL_UID" -g "$INSTALL_GID" -m 600 -- \
            "$BACKUP_PATH" "$ROLLBACK_STAGING" \
            || fail_rollback
        is_secure_root_target "$ROLLBACK_STAGING" || fail_rollback
        fsync_approved_path file "$ROLLBACK_STAGING" \
            "$INSTALL_UID" "$INSTALL_GID" 600 \
            || fail_rollback
        fsync_approved_path directory "$TARGET_DIR" \
            "$INSTALL_UID" "$INSTALL_GID" "$target_dir_mode" \
            || fail_rollback
        if ! "$MV_BIN" -fT -- "$ROLLBACK_STAGING" "$TARGET_FILE"; then
            if is_secure_root_target "$TARGET_FILE" \
                && "$CMP_BIN" -s -- "$BACKUP_PATH" "$TARGET_FILE" \
                && [[ ! -e "$ROLLBACK_STAGING" \
                    && ! -L "$ROLLBACK_STAGING" ]]; then
                fsync_approved_path directory "$TARGET_DIR" \
                    "$INSTALL_UID" "$INSTALL_GID" "$target_dir_mode" \
                    || fail_rollback
            fi
            fail_rollback
        fi
        if [[ -e "$ROLLBACK_STAGING" || -L "$ROLLBACK_STAGING" ]]; then
            fsync_approved_path directory "$TARGET_DIR" \
                "$INSTALL_UID" "$INSTALL_GID" "$target_dir_mode" \
                || fail_rollback
            fail_rollback
        fi
        fsync_approved_path directory "$TARGET_DIR" \
            "$INSTALL_UID" "$INSTALL_GID" "$target_dir_mode" \
            || fail_rollback
        is_secure_root_target "$TARGET_FILE" || fail_rollback
        "$CMP_BIN" -s -- "$BACKUP_PATH" "$TARGET_FILE" || fail_rollback
        [[ ! -e "$ROLLBACK_STAGING" && ! -L "$ROLLBACK_STAGING" ]] \
            || fail_rollback
        ROLLBACK_STAGING=""
    else
        if [[ -e "$TARGET_FILE" || -L "$TARGET_FILE" ]]; then
            if ! "$RM_BIN" -f -- "$TARGET_FILE"; then
                if [[ ! -e "$TARGET_FILE" && ! -L "$TARGET_FILE" ]]; then
                    fsync_approved_path directory "$TARGET_DIR" \
                        "$INSTALL_UID" "$INSTALL_GID" "$target_dir_mode" \
                        || fail_rollback
                fi
                fail_rollback
            fi
        fi
        fsync_approved_path directory "$TARGET_DIR" \
            "$INSTALL_UID" "$INSTALL_GID" "$target_dir_mode" \
            || fail_rollback
        [[ ! -e "$TARGET_FILE" && ! -L "$TARGET_FILE" ]] || fail_rollback
    fi

    ROLLBACK_REQUIRED=false
    cleanup
    emit_installer_event "$original_status" false
    exit "$exit_code"
}

(( EUID == REQUIRED_EUID )) || fail_install "preflight_failed"
for binary in "$CRONTAB_BIN" "$SHA256SUM_BIN" "$STAT_BIN" "$MKDIR_BIN" "$CHMOD_BIN" \
    "$INSTALL_BIN" "$MKTEMP_BIN" "$CMP_BIN" "$RM_BIN" "$MV_BIN" \
    "$ID_BIN" "$LOGGER_BIN" "$CRON_TIMEOUT_BIN" \
    "$PYTHON3_BIN" "$READLINK_BIN"; do
    [[ -x "$binary" ]] || fail_install "preflight_failed"
done
"$LOGGER_BIN" --no-act --tag sanjuk-customs-export \
    --size "$LOGGER_MESSAGE_BYTES" capability-probe >/dev/null 2>&1 \
    || fail_install "preflight_failed"
is_approved_installer_copy || fail_install "preflight_failed"
unset CUSTOMS_CRON_INSTALL_CLEAN

run_user_uid=$("$ID_BIN" -u "$RUN_USER" 2>/dev/null) \
    || fail_install "preflight_failed"
[[ "$run_user_uid" =~ ^[0-9]+$ ]] || fail_install "preflight_failed"
for directory in "$REPO_DIR" "$REPO_DIR/deploy" "$REPO_DIR/tools" \
    "$REPO_DIR/venv" "$REPO_DIR/venv/bin"; do
    is_secure_source_directory "$directory" "$run_user_uid" \
        || fail_install "preflight_failed"
done
is_secure_source_artifact "$WRAPPER" "$run_user_uid" \
    || fail_install "preflight_failed"
[[ -x "$WRAPPER" ]] || fail_install "preflight_failed"
is_secure_source_artifact "$LOGGED_WRAPPER" "$run_user_uid" \
    || fail_install "preflight_failed"
[[ -x "$LOGGED_WRAPPER" ]] || fail_install "preflight_failed"
is_secure_source_artifact "$CAPTURE_HELPER" "$run_user_uid" \
    || fail_install "preflight_failed"
[[ -r "$CAPTURE_HELPER" ]] || fail_install "preflight_failed"

[[ -d "$TARGET_DIR" && ! -L "$TARGET_DIR" ]] || fail_install "preflight_failed"
[[ "$("$STAT_BIN" -c %u:%g -- "$TARGET_DIR" 2>/dev/null)" == \
    "${INSTALL_UID}:${INSTALL_GID}" ]] \
    || fail_install "preflight_failed"
target_dir_mode=$("$STAT_BIN" -c %a -- "$TARGET_DIR" 2>/dev/null) \
    || fail_install "preflight_failed"
mode_has_no_group_world_write "$target_dir_mode" || fail_install "preflight_failed"

[[ ! -L "$STATE_DIR" ]] || fail_install "preflight_failed"
"$MKDIR_BIN" -p -m 700 -- "$STATE_DIR" || fail_install "preflight_failed"
[[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] || fail_install "preflight_failed"
"$CHMOD_BIN" 700 -- "$STATE_DIR" || fail_install "preflight_failed"
[[ "$("$STAT_BIN" -c %u:%g:%a -- "$STATE_DIR" 2>/dev/null)" == \
    "${INSTALL_UID}:${INSTALL_GID}:700" ]] \
    || fail_install "preflight_failed"
[[ ! -L "$BACKUP_DIR" ]] || fail_install "preflight_failed"
"$MKDIR_BIN" -p -m 700 -- "$BACKUP_DIR" || fail_install "preflight_failed"
[[ -d "$BACKUP_DIR" && ! -L "$BACKUP_DIR" ]] || fail_install "preflight_failed"
"$CHMOD_BIN" 700 -- "$BACKUP_DIR" || fail_install "preflight_failed"
[[ "$("$STAT_BIN" -c %u:%g:%a -- "$BACKUP_DIR" 2>/dev/null)" == \
    "${INSTALL_UID}:${INSTALL_GID}:700" ]] \
    || fail_install "preflight_failed"
fsync_approved_path directory "$STATE_DIR" \
    "$INSTALL_UID" "$INSTALL_GID" 700 \
    || fail_install "preflight_failed"

STAGING=$("$MKTEMP_BIN" "$TARGET_DIR/.sanjuk-customs-export.XXXXXX") \
    || fail_install "install_failed"
copy_approved_source || fail_install "preflight_failed"
is_secure_root_target "$STAGING" || fail_install "install_failed"
staging_digest=$("$SHA256SUM_BIN" -- "$STAGING" 2>/dev/null) \
    || fail_install "preflight_failed"
[[ "${staging_digest%% *}" == "$SOURCE_SHA256" ]] \
    || fail_install "preflight_failed"
has_expected_cron_users "$STAGING" || fail_install "preflight_failed"
"$CRONTAB_BIN" -n "$STAGING" >/dev/null 2>&1 \
    || fail_install "syntax_failed"
fsync_approved_path directory "$TARGET_DIR" \
    "$INSTALL_UID" "$INSTALL_GID" "$target_dir_mode" \
    || fail_install "install_failed"

if [[ -e "$TARGET_FILE" || -L "$TARGET_FILE" ]]; then
    is_secure_root_target "$TARGET_FILE" || fail_install "preflight_failed"
    if "$CMP_BIN" -s -- "$STAGING" "$TARGET_FILE"; then
        unchanged_digest=$("$SHA256SUM_BIN" -- "$TARGET_FILE" 2>/dev/null) \
            || fail_install "verify_failed"
        [[ "${unchanged_digest%% *}" == "$SOURCE_SHA256" ]] \
            || fail_install "verify_failed"
        has_expected_cron_users "$TARGET_FILE" \
            || fail_install "verify_failed"
        "$CRONTAB_BIN" -n "$TARGET_FILE" >/dev/null 2>&1 \
            || fail_install "verify_failed"
        fsync_approved_path file "$TARGET_FILE" \
            "$INSTALL_UID" "$INSTALL_GID" 600 \
            || fail_install "verify_failed"
        if ! "$RM_BIN" -f -- "$STAGING"; then
            if [[ ! -e "$STAGING" && ! -L "$STAGING" ]]; then
                fsync_approved_path directory "$TARGET_DIR" \
                    "$INSTALL_UID" "$INSTALL_GID" "$target_dir_mode" \
                    || fail_install "install_failed"
            fi
            fail_install "install_failed"
        fi
        [[ ! -e "$STAGING" && ! -L "$STAGING" ]] \
            || fail_install "install_failed"
        STAGING=""
        fsync_approved_path directory "$TARGET_DIR" \
            "$INSTALL_UID" "$INSTALL_GID" "$target_dir_mode" \
            || fail_install "install_failed"
        emit_installer_event "unchanged" true
        exit 0
    fi
    TZ=UTC printf -v backup_stamp '%(%Y%m%dT%H%M%SZ)T' -1
    BACKUP_PATH=$("$MKTEMP_BIN" --suffix=.cron.d \
        "$BACKUP_DIR/sanjuk-customs-export.$backup_stamp.XXXXXXXX") \
        || fail_install "backup_failed"
    "$INSTALL_BIN" -o "$INSTALL_UID" -g "$INSTALL_GID" -m 600 -- \
        "$TARGET_FILE" "$BACKUP_PATH" \
        || fail_install "backup_failed"
    [[ -f "$BACKUP_PATH" && ! -L "$BACKUP_PATH" ]] \
        || fail_install "backup_failed"
    [[ "$("$STAT_BIN" -c %u:%g:%a -- "$BACKUP_PATH" 2>/dev/null)" == \
        "${INSTALL_UID}:${INSTALL_GID}:600" ]] \
        || fail_install "backup_failed"
    "$CMP_BIN" -s -- "$TARGET_FILE" "$BACKUP_PATH" \
        || fail_install "backup_failed"
    fsync_approved_path file "$BACKUP_PATH" \
        "$INSTALL_UID" "$INSTALL_GID" 600 \
        || fail_install "backup_failed"
    fsync_approved_path directory "$BACKUP_DIR" \
        "$INSTALL_UID" "$INSTALL_GID" 700 \
        || fail_install "backup_failed"
    BACKUP_COMPLETE=true
    HAD_TARGET=true
fi

ROLLBACK_REQUIRED=true
if ! "$MV_BIN" -fT -- "$STAGING" "$TARGET_FILE"; then
    rollback_after_verify_failure "install_failed"
fi
if [[ -e "$STAGING" || -L "$STAGING" ]]; then
    rollback_after_verify_failure "install_failed"
fi
if ! fsync_approved_path directory "$TARGET_DIR" \
    "$INSTALL_UID" "$INSTALL_GID" "$target_dir_mode"; then
    rollback_after_verify_failure "install_failed"
fi
if ! is_secure_root_target "$TARGET_FILE"; then
    rollback_after_verify_failure "verify_failed"
fi
target_digest=$("$SHA256SUM_BIN" -- "$TARGET_FILE" 2>/dev/null) \
    || rollback_after_verify_failure "verify_failed"
if [[ "${target_digest%% *}" != "$SOURCE_SHA256" ]]; then
    rollback_after_verify_failure "verify_failed"
fi
if ! has_expected_cron_users "$TARGET_FILE"; then
    rollback_after_verify_failure "verify_failed"
fi
if ! "$CRONTAB_BIN" -n "$TARGET_FILE" >/dev/null 2>&1; then
    rollback_after_verify_failure "verify_failed"
fi
ROLLBACK_REQUIRED=false
STAGING=""

emit_installer_event "installed" true
