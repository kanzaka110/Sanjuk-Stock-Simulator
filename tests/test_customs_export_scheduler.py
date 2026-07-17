from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "deploy" / "run_customs_export.sh"
LOGGED_WRAPPER = ROOT / "deploy" / "run_customs_export_logged.sh"
CAPTURE_HELPER = ROOT / "deploy" / "capture_bounded_output.py"
CRON_D_SOURCE = ROOT / "deploy" / "customs-export.cron.d"
CRON_INSTALLER = ROOT / "deploy" / "install_customs_export_cron.sh"
CRON_INSTALLER_LAUNCHER = ROOT / "deploy" / "install_customs_export_cron_launcher.c"


def _scheduler_fixture(
    tmp_path: Path, *, create_runtime: bool = True
) -> tuple[Path, Path, Path, Path, Path, Path]:
    repo = tmp_path / "repo [scheduler]; safe"
    state = tmp_path / "state [scheduler]; safe"
    python_bin = repo / "venv" / "bin" / "python"
    collector = repo / "tools" / "collect_customs_export_observations.py"
    capture = tmp_path / "argv.txt"
    lock_file = state / "customs-export.lock"
    state.mkdir(parents=True, mode=0o700)
    lock_file.touch(mode=0o600)
    lock_file.chmod(0o600)
    if create_runtime:
        python_bin.parent.mkdir(parents=True)
        collector.parent.mkdir(parents=True)
        collector.write_text("# test collector placeholder\n", encoding="utf-8")
        python_bin.write_text(
            "#!/bin/bash\n"
            'if [[ -n "${CUSTOMS_EXPORT_TEST_READY:-}" ]]; then : > "$CUSTOMS_EXPORT_TEST_READY"; fi\n'
            'printf \'%s\\n\' "$@" > "$CUSTOMS_EXPORT_TEST_CAPTURE"\n'
            "if [[ -n \"${CUSTOMS_EXPORT_TEST_IGNORE_TERM:-}\" ]]; then trap '' TERM; fi\n"
            'if [[ -n "${CUSTOMS_EXPORT_TEST_SLEEP:-}" ]]; then /bin/sleep "$CUSTOMS_EXPORT_TEST_SLEEP"; fi\n'
            'if [[ -n "${CUSTOMS_EXPORT_TEST_LOCK_PATH:-}" ]]; then\n'
            "  for fd in /proc/$$/fd/*; do\n"
            '    [[ "$(/usr/bin/readlink "$fd" 2>/dev/null || true)" == "$CUSTOMS_EXPORT_TEST_LOCK_PATH" ]] && exit 90\n'
            "  done\n"
            "fi\n"
            "printf '%s\\n' '{\"ok\":true,\"scheduler_test\":true}'\n"
            'exit "${CUSTOMS_EXPORT_TEST_EXIT:-0}"\n',
            encoding="utf-8",
        )
        python_bin.chmod(0o755)
    source = WRAPPER.read_text(encoding="utf-8")
    source = source.replace(
        'REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"',
        f'REPO_DIR="{repo}"',
    ).replace(
        'STATE_DIR="/home/kanzaka110/.local/state/sanjuk-stock-simulator"',
        f'STATE_DIR="{state}"',
    )
    wrapper = tmp_path / "run-customs-export.sh"
    wrapper.write_text(source, encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper, repo, state, python_bin, collector, capture


def _scheduler_env(capture: Path) -> dict[str, str]:
    return {**os.environ, "CUSTOMS_EXPORT_TEST_CAPTURE": str(capture)}


def _last_json_line(output: str) -> dict[str, object]:
    return json.loads(output.splitlines()[-1])


def test_scheduler_wrapper_invokes_scheduled_live_and_preserves_success(tmp_path):
    wrapper, _repo, state, _python, collector, capture = _scheduler_fixture(tmp_path)
    lock_file = state / "customs-export.lock"

    result = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=_scheduler_env(capture),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    argv = capture.read_text(encoding="utf-8").splitlines()
    assert argv[:5] == [
        "-u",
        str(collector),
        "--collection-mode",
        "scheduled_live",
        "--run-id",
    ]
    assert re.fullmatch(r"customs-scheduled-[0-9a-f-]{36}", argv[5])
    event = _last_json_line(result.stdout)
    assert event["ok"] is True
    assert event["scheduler_status"] == "success"
    assert event["exit_code"] == 0
    assert event["run_id"] == argv[5]
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(event["timestamp_utc"])
    )
    assert result.stderr == ""
    assert lock_file.stat().st_mode & 0o777 == 0o600


def test_scheduler_wrapper_preserves_typed_failure_exit(tmp_path):
    wrapper, _repo, _state, _python, _collector, capture = _scheduler_fixture(tmp_path)
    env = _scheduler_env(capture)
    env["CUSTOMS_EXPORT_TEST_EXIT"] = "2"

    result = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert capture.exists()
    event = _last_json_line(result.stdout)
    assert event["ok"] is False
    assert event["scheduler_status"] == "typed_failure"
    assert event["exit_code"] == 2


def test_scheduler_wrapper_bootstraps_missing_state_and_lock(tmp_path):
    for case_name in ("missing-state", "missing-lock"):
        wrapper, _repo, state, _python, _collector, capture = _scheduler_fixture(
            tmp_path / case_name
        )
        lock_file = state / "customs-export.lock"
        if case_name == "missing-state":
            shutil.rmtree(state)
        else:
            lock_file.unlink()

        result = subprocess.run(
            ["/bin/bash", str(wrapper)],
            env=_scheduler_env(capture),
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, case_name
        assert _last_json_line(result.stdout)["scheduler_status"] == "success"
        assert state.stat().st_mode & 0o777 == 0o700
        assert lock_file.stat().st_mode & 0o777 == 0o600


def test_scheduler_wrapper_skips_when_lock_is_held(tmp_path):
    wrapper, _repo, state, _python, _collector, capture = _scheduler_fixture(tmp_path)
    lock_file = state / "customs-export.lock"
    lock_handle = lock_file.open("a", encoding="utf-8")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = subprocess.run(
            ["/bin/bash", str(wrapper)],
            env=_scheduler_env(capture),
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()

    assert result.returncode == 0
    assert not capture.exists()
    event = _last_json_line(result.stdout)
    assert event["ok"] is True
    assert event["scheduler_status"] == "skipped_locked"
    assert event["exit_code"] == 0
    assert result.stderr == ""


def test_scheduler_lock_is_held_through_timeout_child(tmp_path):
    wrapper, _repo, state, _python, _collector, capture = _scheduler_fixture(tmp_path)
    ready = tmp_path / "child-ready"
    env = _scheduler_env(capture)
    env["CUSTOMS_EXPORT_TEST_READY"] = str(ready)
    env["CUSTOMS_EXPORT_TEST_SLEEP"] = "0.25"
    env["CUSTOMS_EXPORT_TEST_LOCK_PATH"] = str(state / "customs-export.lock")
    first = subprocess.Popen(
        ["/bin/bash", str(wrapper)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()

    second = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    first_stdout, first_stderr = first.communicate(timeout=2)

    assert first.returncode == 0
    assert '"scheduler_test":true' in first_stdout
    assert first_stderr == ""
    assert second.returncode == 0
    assert _last_json_line(second.stdout)["scheduler_status"] == "skipped_locked"
    assert second.stderr == ""


def test_scheduler_timeout_is_reported_and_releases_lock(tmp_path):
    wrapper, _repo, _state, _python, _collector, capture = _scheduler_fixture(tmp_path)
    source = wrapper.read_text(encoding="utf-8").replace(
        "--kill-after=30s 300s", "--kill-after=0.05s 0.05s"
    )
    wrapper.write_text(source, encoding="utf-8")
    env = _scheduler_env(capture)
    env["CUSTOMS_EXPORT_TEST_SLEEP"] = "1"

    timed_out = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert timed_out.returncode == 124
    event = _last_json_line(timed_out.stdout)
    assert event["ok"] is False
    assert event["scheduler_status"] == "timeout"
    assert event["exit_code"] == 124

    env.pop("CUSTOMS_EXPORT_TEST_SLEEP")
    retried = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert retried.returncode == 0
    assert _last_json_line(retried.stdout)["scheduler_status"] == "success"


def test_scheduler_kill_escalation_is_reported_and_releases_lock(tmp_path):
    wrapper, _repo, _state, _python, _collector, capture = _scheduler_fixture(tmp_path)
    wrapper.write_text(
        wrapper.read_text(encoding="utf-8").replace(
            "--kill-after=30s 300s", "--kill-after=0.05s 0.05s"
        ),
        encoding="utf-8",
    )
    env = _scheduler_env(capture)
    env["CUSTOMS_EXPORT_TEST_SLEEP"] = "1"
    env["CUSTOMS_EXPORT_TEST_IGNORE_TERM"] = "1"

    killed = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert killed.returncode == 137
    event = _last_json_line(killed.stdout)
    assert event["ok"] is False
    assert event["scheduler_status"] == "timeout_killed"
    assert event["exit_code"] == 137

    env.pop("CUSTOMS_EXPORT_TEST_SLEEP")
    env.pop("CUSTOMS_EXPORT_TEST_IGNORE_TERM")
    retried = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert retried.returncode == 0
    assert _last_json_line(retried.stdout)["scheduler_status"] == "success"


def test_scheduler_preserves_child_exit_75_as_failure(tmp_path):
    wrapper, _repo, _state, _python, _collector, capture = _scheduler_fixture(tmp_path)
    env = _scheduler_env(capture)
    env["CUSTOMS_EXPORT_TEST_EXIT"] = "75"

    result = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 75
    assert capture.exists()
    event = _last_json_line(result.stdout)
    assert event["ok"] is False
    assert event["scheduler_status"] == "failed"
    assert event["exit_code"] == 75


def test_scheduler_flock_operational_error_is_not_lock_skip(tmp_path):
    wrapper, _repo, _state, _python, _collector, capture = _scheduler_fixture(tmp_path)
    fake_flock = tmp_path / "flock-fail"
    fake_flock.write_text("#!/bin/bash\nexit 74\n", encoding="utf-8")
    fake_flock.chmod(0o755)
    source = wrapper.read_text(encoding="utf-8").replace(
        'FLOCK_BIN="/usr/bin/flock"', f'FLOCK_BIN="{fake_flock}"'
    )
    wrapper.write_text(source, encoding="utf-8")

    result = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=_scheduler_env(capture),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 74
    assert not capture.exists()
    event = _last_json_line(result.stdout)
    assert event["ok"] is False
    assert event["scheduler_status"] == "lock_failed"
    assert event["exit_code"] == 74


def test_scheduler_wrapper_rejects_symlink_lock(tmp_path):
    wrapper, _repo, state, _python, _collector, capture = _scheduler_fixture(tmp_path)
    lock_target = tmp_path / "lock-target"
    lock_target.touch(mode=0o600)
    lock_target.chmod(0o600)
    lock_file = state / "customs-export.lock"
    lock_file.unlink()
    lock_file.symlink_to(lock_target)

    result = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=_scheduler_env(capture),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert not capture.exists()
    event = _last_json_line(result.stdout)
    assert event["ok"] is False
    assert event["scheduler_status"] == "runtime_missing"
    assert event["exit_code"] == 1
    assert result.stderr == ""


def test_scheduler_wrapper_rejects_insecure_runtime_artifacts(tmp_path):
    cases: list[tuple[str, str]] = [
        ("state_mode", "state_mode"),
        ("symlink_state", "symlink_state"),
        ("lock_mode", "lock_mode"),
        ("fifo_lock", "fifo_lock"),
        ("collector_mode", "collector_mode"),
        ("symlink_collector", "symlink_collector"),
        ("tools_mode", "tools_mode"),
        ("venv_bin_mode", "venv_bin_mode"),
        ("python_mode", "python_mode"),
        ("collector_hardlink", "collector_hardlink"),
    ]
    for case_name, mutation in cases:
        wrapper, repo, state, python_bin, collector, capture = _scheduler_fixture(
            tmp_path / case_name
        )
        lock_file = state / "customs-export.lock"
        if mutation == "state_mode":
            state.chmod(0o755)
        elif mutation == "symlink_state":
            state_target = tmp_path / case_name / "state-target"
            state_target.mkdir(mode=0o700)
            state_target.chmod(0o700)
            shutil.rmtree(state)
            state.symlink_to(state_target, target_is_directory=True)
        elif mutation == "lock_mode":
            lock_file.chmod(0o644)
        elif mutation == "fifo_lock":
            lock_file.unlink()
            os.mkfifo(lock_file, mode=0o600)
        elif mutation == "collector_mode":
            collector.chmod(0o666)
        elif mutation == "symlink_collector":
            collector_target = tmp_path / case_name / "collector-target.py"
            collector_target.write_text("# safe target\n", encoding="utf-8")
            collector_target.chmod(0o644)
            collector.unlink()
            collector.symlink_to(collector_target)
        elif mutation == "tools_mode":
            (repo / "tools").chmod(0o777)
        elif mutation == "venv_bin_mode":
            (repo / "venv/bin").chmod(0o777)
        elif mutation == "python_mode":
            python_bin.chmod(0o777)
        else:
            os.link(collector, tmp_path / case_name / "collector-hardlink.py")

        result = subprocess.run(
            ["/bin/bash", str(wrapper)],
            env=_scheduler_env(capture),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

        assert result.returncode == 1, case_name
        assert not capture.exists(), case_name
        event = _last_json_line(result.stdout)
        assert event["scheduler_status"] == "runtime_missing", case_name
        assert result.stderr == "", case_name


def test_scheduler_wrapper_fails_safely_when_runtime_is_missing(tmp_path):
    wrapper, repo, state, _python, _collector, _capture = _scheduler_fixture(
        tmp_path, create_runtime=False
    )
    lock_file = state / "customs-export.lock"
    shutil.rmtree(state)
    (repo / "tools").mkdir(parents=True)
    (repo / "venv/bin").mkdir(parents=True)

    result = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=os.environ,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    event = _last_json_line(result.stdout)
    assert event["ok"] is False
    assert event["scheduler_status"] == "runtime_missing"
    assert event["exit_code"] == 1
    assert not state.exists()
    assert not lock_file.exists()
    assert str(repo) not in result.stdout
    assert result.stderr == ""


def test_scheduler_never_leaks_secret_sentinel_across_exit_paths(tmp_path):
    sentinel = "CUSTOMS_SECRET_SENTINEL_DO_NOT_LOG"
    cases: list[tuple[str, int]] = [
        ("success", 0),
        ("typed_failure", 2),
        ("interpreter_failure", 127),
        ("timeout", 124),
    ]
    for case_name, expected_exit in cases:
        wrapper, _repo, _state, python_bin, _collector, capture = _scheduler_fixture(
            tmp_path / case_name
        )
        env = _scheduler_env(capture)
        env["DATA_GO_KR_SERVICE_KEY"] = sentinel
        if case_name == "typed_failure":
            env["CUSTOMS_EXPORT_TEST_EXIT"] = "2"
        elif case_name == "interpreter_failure":
            python_bin.write_text(
                "#!/bin/bash\n"
                'printf \'%s\\n\' "$@" > "$CUSTOMS_EXPORT_TEST_CAPTURE"\n'
                "printf '%s\\n' 'fixed interpreter failure' >&2\n"
                "exit 127\n",
                encoding="utf-8",
            )
            python_bin.chmod(0o755)
        elif case_name == "timeout":
            wrapper.write_text(
                wrapper.read_text(encoding="utf-8").replace(
                    "--kill-after=30s 300s", "--kill-after=0.05s 0.05s"
                ),
                encoding="utf-8",
            )
            env["CUSTOMS_EXPORT_TEST_SLEEP"] = "1"

        result = subprocess.run(
            ["/bin/bash", str(wrapper)],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

        assert result.returncode == expected_exit, case_name
        assert sentinel not in result.stdout, case_name
        assert sentinel not in result.stderr, case_name
        assert capture.exists(), case_name
        assert sentinel not in capture.read_text(encoding="utf-8"), case_name


def test_scheduler_wrapper_has_fixed_authority_paths():
    text = WRAPPER.read_text(encoding="utf-8")

    assert "SANJUK_STOCK_REPO_DIR" not in text
    assert "CUSTOMS_EXPORT_LOCK_FILE" not in text
    assert 'REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"' in text
    assert 'STATE_DIR="/home/kanzaka110/.local/state/sanjuk-stock-simulator"' in text
    assert (
        "unset PYTHONPATH PYTHONHOME PYTHONSTARTUP BASH_ENV ENV CDPATH "
        "LD_PRELOAD LD_LIBRARY_PATH"
    ) in text


def test_scheduler_wrapper_has_valid_shell_syntax():
    result = subprocess.run(
        ["/bin/bash", "-n", str(WRAPPER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_scheduler_wrapper_has_a_hard_runtime_bound():
    text = WRAPPER.read_text(encoding="utf-8")

    assert 'readonly TIMEOUT_BIN="/usr/bin/timeout"' in text
    assert 'exec 9<>"$LOCK_FILE"' in text
    assert '"$FLOCK_BIN" -n -E 75 9' in text
    assert "9>&-" in text
    assert '"$TIMEOUT_BIN" --foreground --signal=TERM --kill-after=30s 300s' in text
    assert 'exec "$TIMEOUT_BIN"' not in text


def test_scheduler_default_lock_is_inside_the_trusted_state_dir():
    text = WRAPPER.read_text(encoding="utf-8")

    assert 'LOCK_FILE="$STATE_DIR/customs-export.lock"' in text
    assert "/tmp/sanjuk-customs-export.lock" not in text


def _cron_d_active_lines() -> list[str]:
    return [
        line
        for line in CRON_D_SOURCE.read_text(encoding="utf-8").splitlines()
        if line[:1].isdigit()
    ]


def _logged_wrapper_fixture(
    tmp_path: Path,
    *,
    wrapper_exit: int,
    logger_exit: int = 0,
    logger_hangs: bool = False,
    check_clean_env: bool = False,
    output_limit_bytes: int = 1048576,
    capture_exit: int | None = None,
    logger_probe_exit: int = 0,
    shorten_logger_timeout: bool = True,
    logger_spawns_child: bool = False,
) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo [logged]; safe"
    state = tmp_path / "state [logged]; safe"
    wrapper = repo / "deploy/run_customs_export.sh"
    logger = tmp_path / "logger"
    logger_capture = tmp_path / "logger-input.txt"
    logger_pid = tmp_path / "logger.pid"
    logger_child_pid = tmp_path / "logger-child.pid"
    wrapper.parent.mkdir(parents=True)
    capture_helper = wrapper.parent / CAPTURE_HELPER.name
    shutil.copy2(CAPTURE_HELPER, capture_helper)
    if capture_exit is not None:
        capture_helper.write_text(
            f"import sys\nsys.stdin.buffer.read()\nraise SystemExit({capture_exit})\n",
            encoding="utf-8",
        )
    checks = ""
    if check_clean_env:
        checks = (
            '[[ -z "${BASH_ENV+x}" ]] || exit 91\n'
            '[[ -z "${LD_PRELOAD+x}" ]] || exit 92\n'
            '[[ -z "${PYTHONPATH+x}" ]] || exit 93\n'
        )
    wrapper.write_text(
        "#!/bin/bash\n"
        + checks
        + "printf '%s\\n' '{\"scheduler_status\":\"probe\"}'\n"
        + f"exit {wrapper_exit}\n",
        encoding="utf-8",
    )
    nested_logger = ""
    if logger_spawns_child:
        logger_child = tmp_path / "logger-child"
        logger_child.write_text(
            "#!/bin/bash\ntrap '' TERM\nwhile true; do /bin/sleep 1; done\n",
            encoding="utf-8",
        )
        logger_child.chmod(0o755)
        nested_logger = (
            f'"{logger_child}" </dev/null >/dev/null 2>&1 &\n'
            f'printf \'%s\\n\' "$!" > "{logger_child_pid}"\n'
        )
    if logger_hangs:
        logger_body = (
            f'printf \'%s\\n\' "$$" > "{logger_pid}"\n'
            + nested_logger
            + "trap '' TERM\n"
            f'/bin/cat > "{logger_capture}"\n'
            "/bin/sleep 10\n"
        )
    else:
        logger_body = f'/bin/cat > "{logger_capture}"\nexit {logger_exit}\n'
    logger.write_text(
        "#!/bin/bash\n"
        'for arg in "$@"; do [[ "$arg" != "--no-act" ]] || '
        f"exit {logger_probe_exit}; done\n" + logger_body,
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    logger.chmod(0o755)
    logger_timeout = (
        "--kill-after=0.05s 0.05s" if shorten_logger_timeout else "--kill-after=10s 10s"
    )
    source = (
        LOGGED_WRAPPER.read_text(encoding="utf-8")
        .replace(
            'REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"',
            f'REPO_DIR="{repo}"',
        )
        .replace(
            'STATE_DIR="/home/kanzaka110/.local/state/sanjuk-stock-simulator"',
            f'STATE_DIR="{state}"',
        )
        .replace('LOGGER_BIN="/usr/bin/logger"', f'LOGGER_BIN="{logger}"')
        .replace("LOGGER_REQUIRED_UID=0", f"LOGGER_REQUIRED_UID={os.geteuid()}")
        .replace(
            "OUTPUT_LIMIT_BYTES=1048576",
            f"OUTPUT_LIMIT_BYTES={output_limit_bytes}",
        )
        .replace("--kill-after=5s 10s", logger_timeout)
    )
    logged_wrapper = tmp_path / "run-customs-export-logged.sh"
    logged_wrapper.write_text(source, encoding="utf-8")
    logged_wrapper.chmod(0o755)
    return logged_wrapper, logger_capture, logger_pid, state


def _run_logged_wrapper(
    logged_wrapper: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/usr/bin/env",
            "-i",
            "HOME=/home/kanzaka110",
            "PATH=/usr/bin:/bin",
            "LANG=C.UTF-8",
            "TZ=UTC",
            "/bin/bash",
            "--noprofile",
            "--norc",
            str(logged_wrapper),
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )


def test_bounded_capture_drains_input_after_storage_failure(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "customs_capture_helper_test", CAPTURE_HELPER
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = b"x" * (module._READ_SIZE * 3 + 17)
    input_path = tmp_path / "input.bin"
    input_path.write_bytes(payload)

    def fail_write(_fd, _payload):
        raise OSError("injected write failure")

    for failure_mode in ("open", "write"):
        output_path = tmp_path / failure_mode / ".customs-export-output.test"
        if failure_mode == "write":
            output_path.parent.mkdir()
            output_path.touch(mode=0o600)
        input_fd = os.open(input_path, os.O_RDONLY)
        original_argv = sys.argv
        original_write_all = getattr(module, "_write_all")
        if failure_mode == "write":
            setattr(module, "_write_all", fail_write)
        try:
            sys.argv = [str(CAPTURE_HELPER), str(output_path), "1048576"]
            result = module.main(input_fd=input_fd)
            assert result == 74
            assert os.lseek(input_fd, 0, os.SEEK_CUR) == len(payload)
        finally:
            setattr(module, "_write_all", original_write_all)
            sys.argv = original_argv
            os.close(input_fd)


def test_cron_d_source_has_isolated_utc_schedules_and_no_secrets():
    text = CRON_D_SOURCE.read_text(encoding="utf-8")
    command = (
        "/usr/bin/env -i HOME=/home/kanzaka110 PATH=/usr/bin:/bin "
        "LANG=C.UTF-8 TZ=UTC /usr/bin/timeout --signal=TERM "
        "--kill-after=10s 350s /bin/bash --noprofile --norc "
        "/home/kanzaka110/Sanjuk-Stock-Simulator/deploy/"
        "run_customs_export_logged.sh"
    )

    assert _cron_d_active_lines() == [
        f"20 3 * * * kanzaka110 {command}",
        f"20 9 1-3,11-13,21-23 * * kanzaka110 {command}",
    ]
    assert text.splitlines()[1] == (
        "# INSTALL TARGET: /etc/cron.d/sanjuk-customs-export"
    )
    assert "KST 12:20" in text
    assert "KST 18:20" in text
    assert text.count("/usr/bin/env -i ") == 2
    assert text.count("/usr/bin/timeout --signal=TERM --kill-after=10s 350s") == 2
    assert text.count("--noprofile --norc") == 2
    assert text.count("run_customs_export_logged.sh") == 2
    assert "/usr/bin/logger" not in text
    assert ".env" not in text
    assert "serviceKey" not in text
    assert "DATA_GO_KR_SERVICE_KEY" not in text


def test_cron_d_clean_environment_blocks_bash_startup_hook_and_preserves_exit(
    tmp_path,
):
    logged_wrapper, logger_capture, _logger_pid, state = _logged_wrapper_fixture(
        tmp_path,
        wrapper_exit=2,
        check_clean_env=True,
    )
    startup_marker = tmp_path / "bash-env-executed"
    bash_env = tmp_path / "hostile-bash-env"
    bash_env.write_text(f': > "{startup_marker}"\n', encoding="utf-8")
    hostile_env = {
        **os.environ,
        "BASH_ENV": str(bash_env),
        "LD_PRELOAD": "/definitely/not/a/library.so",
        "PYTHONPATH": "/hostile/python/path",
    }

    result = _run_logged_wrapper(logged_wrapper, env=hostile_env)

    assert result.returncode == 2
    assert not startup_marker.exists()
    assert '"scheduler_status":"probe"' in logger_capture.read_text(encoding="utf-8")
    assert list(state.glob(".customs-export-output.*")) == []


def test_cron_d_logger_pipeline_preserves_wrapper_and_logger_status(tmp_path):
    for wrapper_exit, logger_exit, expected_exit in (
        (0, 0, 0),
        (2, 0, 2),
        (124, 0, 124),
        (0, 74, 74),
        (2, 74, 2),
    ):
        case_dir = tmp_path / f"{wrapper_exit}-{logger_exit}"
        case_dir.mkdir()
        logged_wrapper, logger_capture, _logger_pid, state = _logged_wrapper_fixture(
            case_dir,
            wrapper_exit=wrapper_exit,
            logger_exit=logger_exit,
        )
        result = _run_logged_wrapper(logged_wrapper)

        assert result.returncode == expected_exit
        assert '"scheduler_status":"probe"' in logger_capture.read_text(
            encoding="utf-8"
        )
        assert list(state.glob(".customs-export-output.*")) == []


def test_logged_wrapper_rejects_incompatible_logger_before_workload(tmp_path):
    logged_wrapper, logger_capture, _logger_pid, state = _logged_wrapper_fixture(
        tmp_path,
        wrapper_exit=0,
        logger_probe_exit=64,
    )

    result = _run_logged_wrapper(logged_wrapper)

    assert result.returncode == 73
    assert not logger_capture.exists()
    assert not state.exists()


def test_logged_wrapper_rejects_insecure_repo_boundaries(tmp_path):
    mutations = [
        "deploy_mode",
        "wrapper_hardlink",
        "capture_hardlink",
        "logger_mode",
        "logger_symlink",
    ]
    if os.geteuid() == 0:
        mutations.append("logger_uid")
    for mutation in mutations:
        case_dir = tmp_path / mutation
        case_dir.mkdir()
        logged_wrapper, logger_capture, _logger_pid, state = _logged_wrapper_fixture(
            case_dir,
            wrapper_exit=0,
        )
        deploy_dir = case_dir / "repo [logged]; safe/deploy"
        wrapper = deploy_dir / "run_customs_export.sh"
        capture_helper = deploy_dir / CAPTURE_HELPER.name
        logger = case_dir / "logger"
        if mutation == "deploy_mode":
            deploy_dir.chmod(0o777)
        elif mutation == "wrapper_hardlink":
            os.link(wrapper, case_dir / "wrapper-hardlink")
        elif mutation == "capture_hardlink":
            os.link(capture_helper, case_dir / "capture-hardlink")
        elif mutation == "logger_mode":
            logger.chmod(0o777)
        elif mutation == "logger_symlink":
            logger_target = case_dir / "logger-target"
            logger.rename(logger_target)
            logger.symlink_to(logger_target)
        else:
            os.chown(logger, 65534, -1)

        result = _run_logged_wrapper(logged_wrapper)

        assert result.returncode == 73, mutation
        assert not logger_capture.exists(), mutation
        assert not state.exists(), mutation


def test_logged_wrapper_caps_log_without_limiting_workload_files(tmp_path):
    logged_wrapper, logger_capture, _logger_pid, state = _logged_wrapper_fixture(
        tmp_path,
        wrapper_exit=0,
        output_limit_bytes=4096,
    )
    workload_file = tmp_path / "collector-output.db"
    wrapper = tmp_path / "repo [logged]; safe/deploy/run_customs_export.sh"
    wrapper.write_text(
        "#!/bin/bash\n"
        f'/usr/bin/dd if=/dev/zero of="{workload_file}" bs=8192 count=1 status=none\n'
        "/usr/bin/dd if=/dev/zero bs=8192 count=1 status=none\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = _run_logged_wrapper(logged_wrapper)

    logged = logger_capture.read_bytes()
    assert result.returncode == 0
    assert workload_file.stat().st_size == 8192
    assert len(logged) <= 4096
    assert logged.endswith(b"\n[customs-export output truncated]\n")
    assert list(state.glob(".customs-export-output.*")) == []
    assert list(state.glob(".customs-export-status.*")) == []


def test_logged_wrapper_preserves_workload_then_capture_then_logger_status(tmp_path):
    for wrapper_exit, logger_exit, expected_exit in (
        (0, 0, 74),
        (2, 0, 2),
        (0, 75, 74),
    ):
        case_dir = tmp_path / f"{wrapper_exit}-{logger_exit}"
        case_dir.mkdir()
        logged_wrapper, _logger_capture, _logger_pid, state = _logged_wrapper_fixture(
            case_dir,
            wrapper_exit=wrapper_exit,
            logger_exit=logger_exit,
            capture_exit=74,
        )

        result = _run_logged_wrapper(logged_wrapper)

        assert result.returncode == expected_exit
        assert list(state.glob(".customs-export-output.*")) == []
        assert list(state.glob(".customs-export-status.*")) == []


def test_cron_d_total_timeout_bounds_hung_logger(tmp_path):
    for wrapper_exit, expected_exit in ((0, 137), (2, 2)):
        case_dir = tmp_path / str(wrapper_exit)
        case_dir.mkdir()
        logged_wrapper, logger_capture, logger_pid, state = _logged_wrapper_fixture(
            case_dir,
            wrapper_exit=wrapper_exit,
            logger_hangs=True,
        )

        started = time.monotonic()
        result = _run_logged_wrapper(logged_wrapper)
        elapsed = time.monotonic() - started

        assert result.returncode == expected_exit
        assert elapsed < 2.0
        assert '"scheduler_status":"probe"' in logger_capture.read_text(
            encoding="utf-8"
        )
        child_pid = logger_pid.read_text(encoding="utf-8").strip()
        deadline = time.monotonic() + 1
        while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not Path(f"/proc/{child_pid}").exists()
        assert list(state.glob(".customs-export-output.*")) == []


def test_logged_wrapper_has_bounded_logger_and_workload_status_priority():
    text = LOGGED_WRAPPER.read_text(encoding="utf-8")

    syntax = subprocess.run(
        ["/bin/bash", "-n", str(LOGGED_WRAPPER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr
    assert "readonly OUTPUT_LIMIT_BYTES=1048576" in text
    assert "readonly LOGGER_MESSAGE_BYTES=4096" in text
    assert '"$LOGGER_BIN" --no-act --tag sanjuk-customs-export' in text
    assert "readonly LOGGER_REQUIRED_UID=0" in text
    assert '[[ -f "$LOGGER_BIN" && ! -L "$LOGGER_BIN" ]]' in text
    assert '[[ "$logger_uid" == "$LOGGER_REQUIRED_UID" ]]' in text
    assert "ulimit -f" not in text
    assert '"$PYTHON3_BIN" -I -S "$CAPTURE_HELPER"' in text
    assert 'pipeline_status=("${PIPESTATUS[@]}")' in text
    assert '"$TIMEOUT_BIN" --signal=TERM --kill-after=5s 10s' in text
    assert "LOGGER_GROUP_PID=$!" in text
    assert 'kill -KILL -- "-$LOGGER_GROUP_PID"' in text
    assert 'wait "$LOGGER_GROUP_PID"' in text
    assert "if (( workload_rc != 0 )); then" in text
    assert 'exit "$workload_rc"' in text
    assert "if (( capture_rc != 0 )); then" in text
    assert 'exit "$capture_rc"' in text
    assert 'exit "$logger_rc"' in text
    assert "trap hold_for_outer_kill TERM" in text
    assert "while true; do" in text
    assert "--kill-after=10s 350s" in CRON_D_SOURCE.read_text(encoding="utf-8")


def test_cron_d_outer_timeout_bounds_hung_logged_supervisor(tmp_path):
    case_dir = tmp_path / "real-supervisor"
    case_dir.mkdir()
    logged_wrapper, _logger_capture, _logger_pid, state = _logged_wrapper_fixture(
        case_dir,
        wrapper_exit=0,
    )
    wrapper = case_dir / "repo [logged]; safe/deploy/run_customs_export.sh"
    child_pid_file = case_dir / "workload.pid"
    wrapper.write_text(
        "#!/bin/bash\n"
        f'printf \'%s\\n\' "$$" > "{child_pid_file}"\n'
        "trap '' TERM\n"
        "/bin/sleep 10\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    command = _cron_d_active_lines()[0].split(maxsplit=6)[6]
    command = command.replace(
        "/home/kanzaka110/Sanjuk-Stock-Simulator/deploy/run_customs_export_logged.sh",
        str(logged_wrapper),
    ).replace("--kill-after=10s 350s", "--kill-after=0.5s 0.5s")

    started = time.monotonic()
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 137
    assert elapsed < 2.0
    child_pid = child_pid_file.read_text(encoding="utf-8").strip()
    deadline = time.monotonic() + 1
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{child_pid}").exists()
    assert list(state.glob(".customs-export-output.*")) == []


def test_cron_d_outer_timeout_kills_hung_logger_group(tmp_path):
    case_dir = tmp_path / "logger-stage"
    case_dir.mkdir()
    logged_wrapper, _logger_capture, logger_pid, state = _logged_wrapper_fixture(
        case_dir,
        wrapper_exit=0,
        logger_hangs=True,
        shorten_logger_timeout=False,
        logger_spawns_child=True,
    )
    command = _cron_d_active_lines()[0].split(maxsplit=6)[6]
    command = command.replace(
        "/home/kanzaka110/Sanjuk-Stock-Simulator/deploy/run_customs_export_logged.sh",
        str(logged_wrapper),
    ).replace("--kill-after=10s 350s", "--kill-after=0.5s 0.5s")

    started = time.monotonic()
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 137
    assert elapsed < 2.0
    child_pid = logger_pid.read_text(encoding="utf-8").strip()
    deadline = time.monotonic() + 1
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{child_pid}").exists()
    nested_pid_file = case_dir / "logger-child.pid"
    nested_pid = nested_pid_file.read_text(encoding="utf-8").strip()
    deadline = time.monotonic() + 1
    while Path(f"/proc/{nested_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{nested_pid}").exists()
    assert list(state.glob(".customs-export-output.*")) == []
    assert list(state.glob(".customs-export-status.*")) == []


def _cron_d_installer_fixture(
    tmp_path: Path,
    *,
    launcher_timeout: int = 60,
    launcher_grace: int = 5,
    pause_containment: bool = False,
    pause_before_watchdog: bool = False,
    fail_watchdog: bool = False,
) -> tuple[Path, Path, Path, Path, Path, Path]:
    repo = tmp_path / "repo [cron.d]; safe"
    state = tmp_path / "state [cron.d]; safe"
    target_dir = tmp_path / "etc cron.d [safe]"
    target = target_dir / "sanjuk-customs-export"
    control = tmp_path / "installer-control"
    calls = control / "crontab-calls.log"
    user_crontab = control / "user.crontab"
    control.mkdir(parents=True)
    state.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    target_dir.mkdir(parents=True)
    user_crontab.write_text(
        "# unrelated user table\n10 * * * * /home/example/unrelated.sh\n",
        encoding="utf-8",
    )
    (repo / "deploy").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "venv/bin").mkdir(parents=True)
    shutil.copy2(CRON_D_SOURCE, repo / "deploy/customs-export.cron.d")
    shutil.copy2(WRAPPER, repo / "deploy/run_customs_export.sh")
    shutil.copy2(LOGGED_WRAPPER, repo / "deploy/run_customs_export_logged.sh")
    shutil.copy2(CAPTURE_HELPER, repo / "deploy/capture_bounded_output.py")

    fake_crontab = control / "crontab"
    fake_crontab.write_text(
        "#!/bin/bash\n"
        f'CALLS="{calls}"\nCONTROL="{control}"\nTARGET="{target}"\nLOCK="{state / "install.lock"}"\n'
        "for fd in /proc/$$/fd/*; do\n"
        '  [[ "$(/usr/bin/readlink "$fd" 2>/dev/null || true)" != "$LOCK" ]] || exit 96\n'
        "done\n"
        'printf \'%s\\n\' "$*" >> "$CALLS"\n'
        '[[ "${1:-}" == "-n" && -f "${2:-}" ]] || exit 97\n'
        '[[ ! -e "$CONTROL/fail-syntax" ]] || exit 1\n',
        encoding="utf-8",
    )
    fake_crontab.chmod(0o755)
    fake_mv = control / "mv"
    fake_mv.write_text(
        "#!/bin/bash\n"
        f'CONTROL="{control}"\n'
        'COUNT_FILE="$CONTROL/mv-count"\n'
        "count=0\n"
        '[[ ! -f "$COUNT_FILE" ]] || read -r count < "$COUNT_FILE"\n'
        "count=$((count + 1))\n"
        'printf \'%s\\n\' "$count" > "$COUNT_FILE"\n'
        'if [[ -e "$CONTROL/fail-move" ]]; then /bin/rm -f "$CONTROL/fail-move"; exit 1; fi\n'
        'if [[ -e "$CONTROL/corrupt-and-fail-rollback" && "$count" -ge 2 ]]; then exit 1; fi\n'
        'if [[ -e "$CONTROL/rollback-move-then-fail" && "$count" -ge 2 ]]; then\n'
        '  /bin/mv "$@" || exit $?\n'
        '  /bin/rm -f "$CONTROL/rollback-move-then-fail"\n'
        "  exit 1\n"
        "fi\n"
        'if [[ -e "$CONTROL/copy-without-unlink" && "$count" -eq 1 ]]; then\n'
        '  /bin/cp -- "${@: -2:1}" "${@: -1}" || exit $?\n'
        '  /bin/rm -f "$CONTROL/copy-without-unlink"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ -e "$CONTROL/no-op-move" ]]; then rm -f "$CONTROL/no-op-move"; exit 0; fi\n'
        'if [[ -e "$CONTROL/move-then-fail" ]]; then\n'
        '  /bin/mv "$@" || exit $?\n'
        '  /bin/rm -f "$CONTROL/move-then-fail"\n'
        "  exit 1\n"
        "fi\n"
        '/bin/mv "$@" || exit $?\n'
        'if [[ -e "$CONTROL/pause-after-move" && "$count" -eq 1 ]]; then\n'
        '  : > "$CONTROL/move-complete"\n'
        "  /bin/sleep 10\n"
        "fi\n"
        'if [[ -e "$CONTROL/corrupt-after-move" || '
        '-e "$CONTROL/corrupt-and-fail-rollback" || '
        '-e "$CONTROL/corrupt-rollback-readback" || '
        '-e "$CONTROL/rollback-move-then-fail" ]]; then\n'
        "  printf '%s\\n' '# corrupted after rename' >> \"${@: -1}\"\n"
        '  rm -f "$CONTROL/corrupt-after-move"\n'
        "fi\n"
        'if [[ -e "$CONTROL/chmod-after-move" ]]; then\n'
        '  /bin/chmod 644 -- "${@: -1}"\n'
        '  /bin/rm -f "$CONTROL/chmod-after-move"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)

    fake_install = control / "install"
    fake_install.write_text(
        "#!/bin/bash\n"
        f'CONTROL="{control}"\n'
        'source_arg="${@: -2:1}"\n'
        'dest_arg="${@: -1}"\n'
        'if [[ "$dest_arg" == */cron.d-backups/* && '
        '-e "$CONTROL/backup-noop" ]]; then exit 0; fi\n'
        'if [[ "$dest_arg" == */cron.d-backups/* && '
        '-e "$CONTROL/backup-corrupt" ]]; then\n'
        '  /usr/bin/install "$@" || exit $?\n'
        "  printf '%s\\n' '# corrupt backup' >> \"$dest_arg\"\n"
        "  exit 0\n"
        "fi\n"
        'exec /usr/bin/install "$@"\n',
        encoding="utf-8",
    )
    fake_install.chmod(0o755)
    fake_id = control / "id"
    fake_id.write_text(
        "#!/bin/bash\n"
        '[[ "${1:-}" == "-u" && "${2:-}" == "kanzaka110" ]] || exit 1\n'
        f"printf '%s\\n' '{os.geteuid()}'\n",
        encoding="utf-8",
    )
    fake_id.chmod(0o755)
    fake_rm = control / "rm"
    fake_rm.write_text(
        "#!/bin/bash\n"
        f'CONTROL="{control}"\n'
        'if [[ -e "$CONTROL/fail-target-remove" && '
        '"${@: -1}" == */sanjuk-customs-export ]]; then exit 1; fi\n'
        'if [[ -e "$CONTROL/no-op-target-remove" && '
        '"${@: -1}" == */sanjuk-customs-export ]]; then exit 0; fi\n'
        'if [[ -e "$CONTROL/remove-then-fail-target" && '
        '"${@: -1}" == */sanjuk-customs-export ]]; then\n'
        '  /bin/rm "$@" || exit $?\n'
        '  /bin/rm -f "$CONTROL/remove-then-fail-target"\n'
        "  exit 1\n"
        "fi\n"
        'if [[ -e "$CONTROL/no-op-staging-remove" && '
        '"${@: -1}" == */.sanjuk-customs-export.* ]]; then\n'
        '  /bin/rm -f "$CONTROL/no-op-staging-remove"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ -e "$CONTROL/remove-then-fail-staging" && '
        '"${@: -1}" == */.sanjuk-customs-export.* ]]; then\n'
        '  /bin/rm "$@" || exit $?\n'
        '  /bin/rm -f "$CONTROL/remove-then-fail-staging"\n'
        "  exit 1\n"
        "fi\n"
        'exec /bin/rm "$@"\n',
        encoding="utf-8",
    )
    fake_rm.chmod(0o755)
    fake_logger = control / "logger"
    fake_logger.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    fake_logger.chmod(0o755)
    fake_python3 = control / "python3"
    fake_python3.write_text(
        "#!/bin/bash\n"
        f'CONTROL="{control}"\nLOCK="{state / "install.lock"}"\n'
        "for fd in /proc/$$/fd/*; do\n"
        '  [[ "$(/usr/bin/readlink "$fd" 2>/dev/null || true)" != "$LOCK" ]] || exit 96\n'
        "done\n"
        'if [[ -f "$CONTROL/forbidden-fd-target" ]]; then\n'
        '  read -r forbidden_fd_target < "$CONTROL/forbidden-fd-target"\n'
        "  for fd in /proc/$$/fd/*; do\n"
        '    [[ "$(/usr/bin/readlink "$fd" 2>/dev/null || true)" != "$forbidden_fd_target" ]] || exit 95\n'
        "  done\n"
        "fi\n"
        'if [[ "${3:-}" == "file" || "${3:-}" == "directory" ]]; then\n'
        '  count_file="$CONTROL/fsync-count"\n'
        "  count=0\n"
        '  [[ ! -f "$count_file" ]] || read -r count < "$count_file"\n'
        "  count=$((count + 1))\n"
        '  printf \'%s\\n\' "$count" > "$count_file"\n'
        '  printf \'%s %s\\n\' "${3:-}" "${4:-}" >> "$CONTROL/fsync-calls"\n'
        '  if [[ -f "$CONTROL/fail-fsync-at" ]]; then\n'
        '    read -r fail_at < "$CONTROL/fail-fsync-at"\n'
        '    [[ "$count" != "$fail_at" ]] || exit 1\n'
        "  fi\n"
        "fi\n"
        'if [[ -e "$CONTROL/hang-python" ]]; then\n'
        '  printf \'%s\\n\' "$$" > "$CONTROL/hang-pid"\n'
        "  trap '' TERM INT HUP QUIT\n"
        "  /bin/sleep 10\n"
        "fi\n"
        'if [[ -e "$CONTROL/spawn-detached" ]]; then\n'
        "  /usr/bin/setsid /bin/bash -c "
        "'trap \"\" TERM INT HUP QUIT; printf \"%s\\n\" \"$$\" > \"$1\"; "
        "exec /bin/sleep 10' _ \"$CONTROL/detached-pid\" "
        "</dev/null >/dev/null 2>&1 &\n"
        "  for _ in {1..300}; do\n"
        '    [[ ! -e "$CONTROL/detached-pid" ]] || break\n'
        "    /bin/sleep 0.01\n"
        "  done\n"
        '  [[ -e "$CONTROL/detached-pid" ]] || exit 94\n'
        "fi\n"
        'if [[ -e "$CONTROL/source-race" && "${4:-}" == "deploy/customs-export.cron.d" ]]; then\n'
        '  source_arg="$3/$4"\n'
        '  race_tmp="${source_arg}.race"\n'
        '  : > "$race_tmp"\n'
        '  while IFS= read -r line || [[ -n "$line" ]]; do\n'
        '    [[ "$line" != "20 3 "* ]] || line="21${line:2}"\n'
        '    printf \'%s\\n\' "$line" >> "$race_tmp"\n'
        '  done < "$source_arg"\n'
        '  /bin/mv -- "$race_tmp" "$source_arg"\n'
        "fi\n"
        'exec /usr/bin/python3 "$@"\n',
        encoding="utf-8",
    )
    fake_python3.chmod(0o755)
    fake_cmp = control / "cmp"
    fake_cmp.write_text(
        "#!/bin/bash\n"
        f'CONTROL="{control}"\nSOURCE="{repo / "deploy/customs-export.cron.d"}"\n'
        f'TARGET="{target}"\n'
        'source_arg="${@: -2:1}"\n'
        'target_arg="${@: -1}"\n'
        'if [[ -e "$CONTROL/no-op-cmp" && "$target_arg" == "$TARGET" && '
        '"$source_arg" == */.sanjuk-customs-export.* ]]; then exit 0; fi\n'
        'if [[ -e "$CONTROL/source-to-target-before-unchanged" && '
        '"$target_arg" == "$TARGET" && "$source_arg" == */.sanjuk-customs-export.* ]]; then\n'
        '  /usr/bin/cp -- "$TARGET" "$SOURCE"\n'
        '  /bin/rm -f "$CONTROL/source-to-target-before-unchanged"\n'
        "fi\n"
        'exec /usr/bin/cmp "$@"\n',
        encoding="utf-8",
    )
    fake_cmp.chmod(0o755)

    source = CRON_INSTALLER.read_text(encoding="utf-8")
    source = (
        source.replace(
            'REPO_DIR="/home/kanzaka110/Sanjuk-Stock-Simulator"',
            f'REPO_DIR="{repo}"',
        )
        .replace('TARGET_DIR="/etc/cron.d"', f'TARGET_DIR="{target_dir}"')
        .replace(
            'STATE_DIR="/var/lib/sanjuk-stock-simulator"',
            f'STATE_DIR="{state}"',
        )
        .replace("REQUIRED_EUID=0", f"REQUIRED_EUID={os.geteuid()}")
        .replace("INSTALL_UID=0", f"INSTALL_UID={os.geteuid()}")
        .replace("INSTALL_GID=0", f"INSTALL_GID={os.getegid()}")
        .replace(
            'CRONTAB_BIN="/usr/bin/crontab"',
            f'CRONTAB_BIN="{fake_crontab}"',
        )
        .replace('MV_BIN="/bin/mv"', f'MV_BIN="{fake_mv}"')
        .replace('INSTALL_BIN="/usr/bin/install"', f'INSTALL_BIN="{fake_install}"')
        .replace('CMP_BIN="/usr/bin/cmp"', f'CMP_BIN="{fake_cmp}"')
        .replace('ID_BIN="/usr/bin/id"', f'ID_BIN="{fake_id}"')
        .replace('RM_BIN="/bin/rm"', f'RM_BIN="{fake_rm}"')
        .replace('LOGGER_BIN="/usr/bin/logger"', f'LOGGER_BIN="{fake_logger}"')
        .replace('PYTHON3_BIN="/usr/bin/python3"', f'PYTHON3_BIN="{fake_python3}"')
    )
    installer = tmp_path / "install-customs-cron.sh"
    launcher = tmp_path / "install-customs-cron-launcher"
    source = source.replace(
        'INSTALLER_PATH="$STATE_DIR/install-customs-export-cron.sh"',
        f'INSTALLER_PATH="{installer}"',
    ).replace(
        'LAUNCHER_PATH="$STATE_DIR/install-customs-export-cron-launcher"',
        f'LAUNCHER_PATH="{launcher}"',
    )
    installer.write_text(source, encoding="utf-8")
    installer.chmod(0o700)
    installer_digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    installer_size = len(installer.read_bytes())
    launcher_source = CRON_INSTALLER_LAUNCHER.read_text(encoding="utf-8")
    launcher_source = re.sub(
        r'(#define INSTALLER_SHA256 \\\n\s+)"[0-9a-f]{64}"',
        lambda match: f'{match.group(1)}"{installer_digest}"',
        launcher_source,
    )
    launcher_source = re.sub(
        r"#define INSTALLER_SIZE \(\(off_t\)[0-9]+\)",
        f"#define INSTALLER_SIZE ((off_t){installer_size})",
        launcher_source,
    )
    launcher_source = (
        launcher_source.replace(
            '#define STATE_DIR "/var/lib/sanjuk-stock-simulator"',
            f'#define STATE_DIR "{state}"',
        )
        .replace(
            '#define LAUNCHER_PATH STATE_DIR "/install-customs-export-cron-launcher"',
            f'#define LAUNCHER_PATH "{launcher}"',
        )
        .replace(
            '#define INSTALLER_PATH STATE_DIR "/install-customs-export-cron.sh"',
            f'#define INSTALLER_PATH "{installer}"',
        )
        .replace(
            "#define REQUIRED_UID ((uid_t)0)",
            f"#define REQUIRED_UID ((uid_t){os.geteuid()})",
        )
        .replace(
            "#define REQUIRED_GID ((gid_t)0)",
            f"#define REQUIRED_GID ((gid_t){os.getegid()})",
        )
        .replace(
            "#define INSTALL_TIMEOUT_SECONDS 60U",
            f"#define INSTALL_TIMEOUT_SECONDS {launcher_timeout}U",
        )
        .replace(
            "#define KILL_GRACE_SECONDS 5U",
            f"#define KILL_GRACE_SECONDS {launcher_grace}U",
        )
    )
    if pause_before_watchdog:
        watchdog_setup = (
            "    if (!move_child_to_cgroup(&scope, child)) {\n"
            "        return abort_installer_before_watchdog(barrier[1], child, lock_fd,\n"
            "                                               &scope);\n"
            "    }\n\n"
            "    int watchdog_pipe[2];"
        )
        assert watchdog_setup in launcher_source
        entered_path = json.dumps(str(control / "watchdog-setup-entered"))
        release_path = json.dumps(str(control / "release-watchdog-setup"))
        launcher_source = launcher_source.replace(
            watchdog_setup,
            watchdog_setup.replace(
                "\n\n    int watchdog_pipe[2];",
                "\n"
                "    {\n"
                "        struct timespec test_delay = {0, 10000000L};\n"
                f"        int marker_fd = open({entered_path}, O_WRONLY | O_CREAT | "
                "O_TRUNC | O_CLOEXEC, 0600);\n"
                "        if (marker_fd >= 0) {\n"
                '            dprintf(marker_fd, "%ld\\n", (long)child);\n'
                "            close(marker_fd);\n"
                "        }\n"
                f"        while (access({release_path}, F_OK) != 0) {{\n"
                "            nanosleep(&test_delay, NULL);\n"
                "        }\n"
                "    }\n\n"
                "    int watchdog_pipe[2];",
            ),
            1,
        )
    if pause_containment:
        containment_entry = (
            "static void contain_installer_cgroup("
            "const struct installer_cgroup *scope) {\n"
        )
        assert containment_entry in launcher_source
        pause_path = json.dumps(str(control / "pause-containment"))
        entered_path = json.dumps(str(control / "containment-entered"))
        release_path = json.dumps(str(control / "release-containment"))
        launcher_source = launcher_source.replace(
            containment_entry,
            containment_entry
            + f"    if (access({pause_path}, F_OK) == 0) {{\n"
            + "        struct timespec test_delay = {0, 10000000L};\n"
            + "        int marker_fd;\n"
            + f"        marker_fd = open({entered_path}, O_WRONLY | O_CREAT | "
            + "O_TRUNC | O_CLOEXEC, 0600);\n"
            + "        if (marker_fd >= 0) {\n"
            + '            dprintf(marker_fd, "C\\n");\n'
            + "            close(marker_fd);\n"
            + "        }\n"
            + f"        while (access({release_path}, F_OK) != 0) {{\n"
            + "            nanosleep(&test_delay, NULL);\n"
            + "        }\n"
            + "    }\n",
            1,
        )
    if fail_watchdog:
        watchdog_containment = (
            "        contain_installer_cgroup(&scope);\n"
            "        remove_installer_cgroup_or_wait(&scope);\n"
            "        close(scope.directory_fd);\n"
            "        _exit(0);"
        )
        assert watchdog_containment in launcher_source
        launcher_source = launcher_source.replace(
            watchdog_containment,
            "        close(scope.directory_fd);\n"
            "        _exit(1);",
            1,
        )
    launcher_c = control / "launcher.c"
    launcher_c.write_text(launcher_source, encoding="utf-8")
    compiler = shutil.which("cc")
    assert compiler is not None
    subprocess.run(
        [
            compiler,
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-static",
            "-Wl,--build-id=none",
            str(launcher_c),
            "-o",
            str(launcher),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    launcher.chmod(0o700)
    return installer, target, state, control, calls, user_crontab


def _run_installer(
    installer: Path,
    *,
    env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
    preexec_fn=None,
) -> subprocess.CompletedProcess[str]:
    launcher = installer.with_name("install-customs-cron-launcher")
    return subprocess.run(
        [str(launcher)],
        env=env,
        pass_fds=pass_fds,
        preexec_fn=preexec_fn,
        check=False,
        capture_output=True,
        text=True,
    )


def test_cron_d_installer_rejects_unapproved_execution_copy(tmp_path):
    for case_name in (
        "mode",
        "symlink",
        "launcher-mode",
        "launcher-symlink",
        "state-mode",
        "state-symlink",
    ):
        installer, target, state, _control, _calls, user_crontab = (
            _cron_d_installer_fixture(tmp_path / case_name)
        )
        original_user_table = user_crontab.read_text(encoding="utf-8")
        launcher = installer.with_name("install-customs-cron-launcher")
        if case_name == "mode":
            installer.chmod(0o755)
        elif case_name == "symlink":
            approved_copy = installer.with_name("approved-copy")
            installer.rename(approved_copy)
            installer.symlink_to(approved_copy)
        elif case_name == "launcher-mode":
            launcher.chmod(0o755)
        elif case_name == "launcher-symlink":
            approved_launcher = launcher.with_name("approved-launcher")
            launcher.rename(approved_launcher)
            launcher.symlink_to(approved_launcher)
        elif case_name == "state-mode":
            state.chmod(0o755)
        else:
            state_target = state.with_name("state-target")
            state_target.mkdir(mode=0o700)
            state_target.chmod(0o700)
            state.rmdir()
            state.symlink_to(state_target, target_is_directory=True)

        result = _run_installer(installer)

        assert result.returncode == 1, case_name
        assert _last_json_line(result.stdout)["installer_status"] == (
            "launcher_preflight_failed"
        )
        assert not target.exists()
        assert user_crontab.read_text(encoding="utf-8") == original_user_table


def test_cron_d_launcher_rejects_wrong_root_uid_or_gid(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("ownership boundary probe requires root")
    wrong_uid = 65534 if os.geteuid() != 65534 else 65533
    wrong_gid = 65534 if os.getegid() != 65534 else 65533
    for case_name in (
        "installer-uid",
        "installer-gid",
        "launcher-uid",
        "launcher-gid",
        "state-uid",
        "state-gid",
    ):
        installer, target, state, _control, _calls, user_crontab = (
            _cron_d_installer_fixture(tmp_path / case_name)
        )
        launcher = installer.with_name("install-customs-cron-launcher")
        artifact = {
            "installer": installer,
            "launcher": launcher,
            "state": state,
        }[case_name.split("-", 1)[0]]
        if case_name.endswith("-uid"):
            os.chown(artifact, wrong_uid, -1)
        else:
            os.chown(artifact, -1, wrong_gid)
        original_user_table = user_crontab.read_text(encoding="utf-8")

        result = _run_installer(installer)

        assert result.returncode == 1, case_name
        assert _last_json_line(result.stdout)["installer_status"] == (
            "launcher_preflight_failed"
        )
        assert not target.exists()
        assert user_crontab.read_text(encoding="utf-8") == original_user_table


def test_cron_d_launcher_rejects_unexpected_arguments(tmp_path):
    installer, target, _state, _control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    launcher = installer.with_name("install-customs-cron-launcher")

    result = subprocess.run(
        [str(launcher), "--unexpected"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert _last_json_line(result.stdout)["installer_status"] == (
        "launcher_preflight_failed"
    )
    assert not target.exists()


def test_cron_d_launcher_rejects_same_size_installer_digest_mismatch(tmp_path):
    installer, target, state, _control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    lock_file = state / "install.lock"
    assert not lock_file.exists()
    assert list(state.iterdir()) == []
    original_size = installer.stat().st_size
    payload = bytearray(installer.read_bytes())
    offset = payload.index(b"RUN_USER")
    payload[offset] = ord("X")
    installer.write_bytes(payload)
    installer.chmod(0o700)
    assert installer.stat().st_size == original_size
    launcher = installer.with_name("install-customs-cron-launcher")

    result = subprocess.run(
        [str(launcher)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert _last_json_line(result.stdout)["installer_status"] == (
        "launcher_preflight_failed"
    )
    assert not target.exists()
    assert not lock_file.exists()
    assert list(state.iterdir()) == []


def test_cron_d_installer_clean_entry_blocks_bash_env(tmp_path):
    installer, _target, _state, _control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    startup_marker = tmp_path / "installer-bash-env-executed"
    bash_env = tmp_path / "hostile-bash-env"
    bash_env.write_text(f': > "{startup_marker}"\n', encoding="utf-8")
    env = {**os.environ, "BASH_ENV": str(bash_env), "PYTHONPATH": "/hostile/path"}

    result = _run_installer(installer, env=env)

    assert result.returncode == 0
    assert _last_json_line(result.stdout)["installer_status"] == "installed"
    assert not startup_marker.exists()


def test_cron_d_launcher_timeout_kills_installer_group_and_releases_lock(tmp_path):
    installer, target, _state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(
            tmp_path,
            launcher_timeout=1,
            launcher_grace=1,
        )
    )
    marker = control / "hang-python"
    marker.touch()

    started = time.monotonic()
    result = _run_installer(installer)
    elapsed = time.monotonic() - started

    assert result.returncode == 124
    assert _last_json_line(result.stdout)["installer_status"] == "launcher_timeout"
    assert elapsed < 3.0
    child_pid = (control / "hang-pid").read_text(encoding="utf-8").strip()
    deadline = time.monotonic() + 1
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{child_pid}").exists()
    assert not target.exists()

    marker.unlink()
    retried = _run_installer(installer)
    assert retried.returncode == 0
    assert _last_json_line(retried.stdout)["installer_status"] == "installed"


def test_cron_d_launcher_forwards_term_and_kills_installer_group(tmp_path):
    installer, target, _state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(
            tmp_path,
            launcher_timeout=10,
            launcher_grace=1,
        )
    )
    marker = control / "hang-python"
    marker.touch()
    launcher = installer.with_name("install-customs-cron-launcher")
    process = subprocess.Popen(
        [str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 3
    while not (control / "hang-pid").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (control / "hang-pid").exists()

    process.terminate()
    process.communicate(timeout=3)

    assert process.returncode == 143
    child_pid = (control / "hang-pid").read_text(encoding="utf-8").strip()
    deadline = time.monotonic() + 1
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{child_pid}").exists()
    assert not target.exists()

    marker.unlink()
    retried = _run_installer(installer)
    assert retried.returncode == 0
    assert _last_json_line(retried.stdout)["installer_status"] == "installed"


def test_cron_d_launcher_sigkill_watchdog_kills_orphan_group(tmp_path):
    installer, target, _state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(
            tmp_path,
            launcher_timeout=10,
            launcher_grace=1,
        )
    )
    marker = control / "hang-python"
    marker.touch()
    launcher = installer.with_name("install-customs-cron-launcher")
    process = subprocess.Popen(
        [str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 3
    while not (control / "hang-pid").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (control / "hang-pid").exists()

    process.kill()
    process.communicate(timeout=3)

    assert process.returncode == -signal.SIGKILL
    child_pid = (control / "hang-pid").read_text(encoding="utf-8").strip()
    deadline = time.monotonic() + 1
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{child_pid}").exists()
    assert not target.exists()

    marker.unlink()
    retried = _run_installer(installer)
    assert retried.returncode == 0
    assert _last_json_line(retried.stdout)["installer_status"] == "installed"


def test_cron_d_launcher_uses_private_cgroup_and_removes_it_after_sigkill(
    tmp_path,
):
    installer, target, _state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path, launcher_timeout=10, launcher_grace=1)
    )
    hang_marker = control / "hang-python"
    hang_marker.touch()
    launcher = installer.with_name("install-customs-cron-launcher")
    process = subprocess.Popen(
        [str(launcher)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid = 0
    child_group = 0
    try:
        deadline = time.monotonic() + 3
        while not (control / "hang-pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (control / "hang-pid").exists()
        child_pid = int((control / "hang-pid").read_text(encoding="utf-8"))
        child_group = os.getpgid(child_pid)
        cgroup_lines = Path(f"/proc/{child_pid}/cgroup").read_text(
            encoding="utf-8"
        ).splitlines()
        unified = [line[3:] for line in cgroup_lines if line.startswith("0::/")]
        assert len(unified) == 1
        child_cgroup = unified[0]
        child_cgroup_path = Path(child_cgroup)
        assert child_cgroup_path.name == "_payload"
        assert child_cgroup_path.parent.name.startswith(
            "sanjuk-customs-export-installer-"
        )
        assert child_cgroup_path.parent.name.endswith(".scope")
        delegation = subprocess.run(
            [
                "/usr/bin/systemctl",
                "show",
                child_cgroup_path.parent.name,
                "--property=Delegate",
                "--value",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert delegation.returncode == 0, delegation.stderr
        assert delegation.stdout.strip() == "yes"
        cgroup_path = Path("/sys/fs/cgroup" + child_cgroup)
        assert cgroup_path.is_dir()

        process.kill()
        process.communicate(timeout=3)
        assert process.returncode == -signal.SIGKILL
        deadline = time.monotonic() + 3
        while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not Path(f"/proc/{child_pid}").exists()
        deadline = time.monotonic() + 3
        while cgroup_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not cgroup_path.exists()
        assert not target.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        if child_group and Path(f"/proc/{child_pid}").exists():
            try:
                os.killpg(child_group, signal.SIGKILL)
            except ProcessLookupError:
                pass

    hang_marker.unlink()
    installed = _run_installer(installer)
    assert installed.returncode == 0
    assert _last_json_line(installed.stdout)["installer_status"] == "installed"
    assert target.exists()


def test_cron_d_launcher_sigkill_before_watchdog_collects_blocked_payload(
    tmp_path,
):
    installer, target, state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(
            tmp_path,
            launcher_timeout=10,
            launcher_grace=1,
            pause_before_watchdog=True,
        )
    )
    launcher = installer.with_name("install-customs-cron-launcher")
    process = subprocess.Popen(
        [str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    marker = control / "watchdog-setup-entered"
    release = control / "release-watchdog-setup"
    child_pid = 0
    child_group = 0
    lock_fd = -1
    scope_unit = ""
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        child_pid = int(marker.read_text(encoding="utf-8"))
        child_group = os.getpgid(child_pid)
        membership = Path(f"/proc/{child_pid}/cgroup").read_text(
            encoding="utf-8"
        )
        unified = [
            line[3:] for line in membership.splitlines() if line.startswith("0::/")
        ]
        assert len(unified) == 1
        payload_path = Path("/sys/fs/cgroup" + unified[0])
        assert payload_path.name == "_payload"
        scope_path = payload_path.parent
        scope_unit = scope_path.name
        assert scope_unit.startswith("sanjuk-customs-export-installer-")
        assert scope_unit.endswith(".scope")

        lock_fd = os.open(state / "install.lock", os.O_RDWR | os.O_CLOEXEC)
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        process.kill()
        process.wait(timeout=3)
        assert process.returncode == -signal.SIGKILL
        deadline = time.monotonic() + 5
        while (
            (
                Path(f"/proc/{child_pid}").exists()
                or payload_path.exists()
                or scope_path.exists()
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert not Path(f"/proc/{child_pid}").exists()
        assert not payload_path.exists()
        assert not scope_path.exists()
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not target.exists()
    finally:
        release.touch(exist_ok=True)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        if child_group > 0:
            try:
                os.killpg(child_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if lock_fd >= 0:
            os.close(lock_fd)
        if scope_unit:
            subprocess.run(
                ["/usr/bin/systemctl", "stop", scope_unit],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["/usr/bin/systemctl", "reset-failed", scope_unit],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def test_cron_d_launcher_sigkill_watchdog_holds_lock_until_cgroup_contained(
    tmp_path,
):
    installer, target, state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(
            tmp_path,
            launcher_timeout=10,
            launcher_grace=1,
            pause_containment=True,
        )
    )
    (control / "hang-python").touch()
    (control / "pause-containment").touch()
    launcher = installer.with_name("install-customs-cron-launcher")
    process = subprocess.Popen(
        [str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    release = control / "release-containment"
    child_pid = ""
    child_group = 0
    lock_fd = -1
    try:
        deadline = time.monotonic() + 3
        while not (control / "hang-pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (control / "hang-pid").exists()
        child_pid = (control / "hang-pid").read_text(encoding="utf-8").strip()
        child_group = os.getpgid(int(child_pid))

        process.kill()
        process.wait(timeout=3)
        assert process.returncode == -signal.SIGKILL
        deadline = time.monotonic() + 3
        while (
            not (control / "containment-entered").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert (control / "containment-entered").exists()
        assert (control / "containment-entered").read_text(encoding="utf-8") == "C\n"
        assert Path(f"/proc/{child_pid}").exists()

        lock_fd = os.open(state / "install.lock", os.O_RDWR | os.O_CLOEXEC)
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        release.touch()
        deadline = time.monotonic() + 3
        acquired = False
        while time.monotonic() < deadline:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.01)
        assert acquired
        assert not Path(f"/proc/{child_pid}").exists()
        assert not target.exists()
    finally:
        release.touch(exist_ok=True)
        if child_group > 0:
            try:
                os.killpg(child_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if lock_fd >= 0:
            os.close(lock_fd)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def test_cron_d_launcher_watchdog_failure_fallback_contains_detached_helper(
    tmp_path,
):
    installer, _target, state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(
            tmp_path,
            launcher_timeout=10,
            launcher_grace=1,
            pause_containment=True,
            fail_watchdog=True,
        )
    )
    (control / "spawn-detached").touch()
    (control / "pause-containment").touch()
    launcher = installer.with_name("install-customs-cron-launcher")
    process = subprocess.Popen(
        [str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    release = control / "release-containment"
    detached_pid = 0
    lock_fd = -1
    try:
        deadline = time.monotonic() + 3
        while (
            not (control / "detached-pid").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert (control / "detached-pid").exists()
        detached_pid = int(
            (control / "detached-pid").read_text(encoding="utf-8")
        )
        assert os.getpgid(detached_pid) == detached_pid
        deadline = time.monotonic() + 3
        while (
            not (control / "containment-entered").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert (control / "containment-entered").exists()
        assert (control / "containment-entered").read_text(encoding="utf-8") == "C\n"
        assert Path(f"/proc/{detached_pid}").exists()

        lock_fd = os.open(state / "install.lock", os.O_RDWR | os.O_CLOEXEC)
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        release.touch()
        process.communicate(timeout=3)
        assert process.returncode == 1
        deadline = time.monotonic() + 3
        while (
            Path(f"/proc/{detached_pid}").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert not Path(f"/proc/{detached_pid}").exists()
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.touch(exist_ok=True)
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        if detached_pid and Path(f"/proc/{detached_pid}").exists():
            try:
                os.killpg(detached_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if lock_fd >= 0:
            os.close(lock_fd)


def test_cron_d_launcher_term_or_timeout_after_rename_rolls_back_target(tmp_path):
    previous = "# previous dedicated customs schedule\n"
    for trigger in ("term", "timeout"):
        for had_target in (False, True):
            case_dir = tmp_path / trigger / str(had_target)
            installer, target, state, control, _calls, _user_crontab = (
                _cron_d_installer_fixture(
                    case_dir,
                    launcher_timeout=10 if trigger == "term" else 2,
                    launcher_grace=2,
                )
            )
            if had_target:
                target.write_text(previous, encoding="utf-8")
                target.chmod(0o600)
            (control / "pause-after-move").touch()
            launcher = installer.with_name("install-customs-cron-launcher")
            process = subprocess.Popen(
                [str(launcher)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 3
            while (
                not (control / "move-complete").exists() and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert (control / "move-complete").exists()
            assert target.read_bytes() == CRON_D_SOURCE.read_bytes()

            if trigger == "term":
                process.terminate()
            stdout, _stderr = process.communicate(timeout=5)

            expected_exit = 143 if trigger == "term" else 124
            expected_status = "interrupted" if trigger == "term" else "launcher_timeout"
            assert process.returncode == expected_exit
            assert _last_json_line(stdout)["installer_status"] == expected_status
            if had_target:
                assert target.read_text(encoding="utf-8") == previous
            else:
                assert not target.exists()
            assert not list(target.parent.glob(".sanjuk-customs-export.*"))
            backups = list((state / "cron.d-backups").glob("*.cron.d"))
            assert len(backups) == int(had_target)
            if had_target:
                assert backups[0].read_text(encoding="utf-8") == previous


def test_cron_d_launcher_preserves_rollback_failed_over_term(tmp_path):
    installer, target, state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(
            tmp_path,
            launcher_timeout=10,
            launcher_grace=2,
        )
    )
    previous = "# previous dedicated customs schedule\n"
    target.write_text(previous, encoding="utf-8")
    target.chmod(0o600)
    (control / "pause-after-move").touch()
    (control / "corrupt-and-fail-rollback").touch()
    launcher = installer.with_name("install-customs-cron-launcher")
    process = subprocess.Popen(
        [str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 3
    while not (control / "move-complete").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (control / "move-complete").exists()

    process.terminate()
    stdout, _stderr = process.communicate(timeout=5)

    assert process.returncode == 2
    assert _last_json_line(stdout)["installer_status"] == "rollback_failed"
    assert target.read_bytes() == CRON_D_SOURCE.read_bytes()
    backups = list((state / "cron.d-backups").glob("*.cron.d"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == previous


def test_cron_d_launcher_normalizes_blocked_alarm_term_and_ignored_sigchld(
    tmp_path,
):
    installer, target, _state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(
            tmp_path,
            launcher_timeout=1,
            launcher_grace=1,
        )
    )
    (control / "hang-python").touch()

    def hostile_signal_state():
        signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {signal.SIGALRM, signal.SIGTERM},
        )
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    started = time.monotonic()
    result = _run_installer(installer, preexec_fn=hostile_signal_state)
    elapsed = time.monotonic() - started

    assert result.returncode == 124
    assert _last_json_line(result.stdout)["installer_status"] == "launcher_timeout"
    assert elapsed < 3.0
    assert not target.exists()


def test_cron_d_launcher_rejects_inherited_pending_term_before_child(tmp_path):
    installer, target, _state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )

    def pending_term_state():
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        os.kill(os.getpid(), signal.SIGTERM)

    result = _run_installer(installer, preexec_fn=pending_term_state)

    assert result.returncode == 143
    assert _last_json_line(result.stdout)["installer_status"] == "launcher_cancelled"
    assert not (control / "hang-pid").exists()
    assert not target.exists()


def test_cron_d_launcher_closes_inherited_high_fd(tmp_path):
    installer, _target, _state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    sentinel = tmp_path / "inherited-fd-sentinel"
    sentinel.write_text("sentinel\n", encoding="utf-8")
    (control / "forbidden-fd-target").write_text(
        str(sentinel),
        encoding="utf-8",
    )
    descriptor = os.open(sentinel, os.O_RDONLY)
    try:
        result = _run_installer(installer, pass_fds=(descriptor,))
    finally:
        os.close(descriptor)

    assert result.returncode == 0
    assert _last_json_line(result.stdout)["installer_status"] == "installed"


def test_cron_d_installer_rejects_direct_interpreter_bypass(tmp_path):
    installer, target, _state, _control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )

    result = subprocess.run(
        ["/bin/bash", str(installer)],
        env={
            "PATH": "/usr/bin:/bin",
            "CUSTOMS_CRON_INSTALL_CLEAN": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert _last_json_line(result.stdout)["installer_status"] == "preflight_failed"
    assert not target.exists()


def test_cron_d_installer_rejects_insecure_preflight_boundaries(tmp_path):
    for case_name in (
        "euid",
        "repo-mode",
        "deploy-mode",
        "wrapper-hardlink",
        "backup-symlink",
    ):
        installer, target, state, _control, _calls, user_crontab = (
            _cron_d_installer_fixture(tmp_path / case_name)
        )
        original_user_table = user_crontab.read_text(encoding="utf-8")
        if case_name == "euid":
            source = installer.read_text(encoding="utf-8").replace(
                f"REQUIRED_EUID={os.geteuid()}",
                f"REQUIRED_EUID={os.geteuid() + 1}",
            )
            installer.write_text(source, encoding="utf-8")
            installer.chmod(0o700)
        elif case_name in ("repo-mode", "deploy-mode", "wrapper-hardlink"):
            repo = target.parent.parent / "repo [cron.d]; safe"
            if case_name == "repo-mode":
                repo.chmod(0o777)
            elif case_name == "deploy-mode":
                (repo / "deploy").chmod(0o777)
            else:
                os.link(
                    repo / "deploy/run_customs_export.sh",
                    repo / "wrapper-hardlink",
                )
        else:
            backup_target = state.with_name("backup-target")
            backup_target.mkdir(mode=0o700)
            (state / "cron.d-backups").symlink_to(
                backup_target, target_is_directory=True
            )

        result = _run_installer(installer)

        assert result.returncode == 1, case_name
        expected_status = (
            "launcher_preflight_failed" if case_name == "euid" else "preflight_failed"
        )
        assert _last_json_line(result.stdout)["installer_status"] == expected_status
        assert not target.exists()
        assert user_crontab.read_text(encoding="utf-8") == original_user_table


def test_cron_d_installer_never_trusts_mutable_source_for_unchanged(tmp_path):
    installer, target, _state, control, _calls, user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    previous = CRON_D_SOURCE.read_text(encoding="utf-8").replace(
        "20 3 * * *", "21 3 * * *", 1
    )
    target.write_text(previous, encoding="utf-8")
    target.chmod(0o600)
    original_user_table = user_crontab.read_text(encoding="utf-8")
    (control / "source-to-target-before-unchanged").touch()

    result = _run_installer(installer)

    assert result.returncode == 0
    assert _last_json_line(result.stdout)["installer_status"] == "installed"
    assert target.read_bytes() == CRON_D_SOURCE.read_bytes()
    mutable_source = (
        target.parent.parent / "repo [cron.d]; safe/deploy/customs-export.cron.d"
    )
    assert mutable_source.read_text(encoding="utf-8") == previous
    assert user_crontab.read_text(encoding="utf-8") == original_user_table


def test_cron_d_installer_rejects_noop_cmp_for_unchanged(tmp_path):
    installer, target, _state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    previous = CRON_D_SOURCE.read_text(encoding="utf-8").replace(
        "20 3 * * *", "21 3 * * *", 1
    )
    target.write_text(previous, encoding="utf-8")
    target.chmod(0o600)
    (control / "no-op-cmp").touch()

    result = _run_installer(installer)

    assert result.returncode == 1
    assert _last_json_line(result.stdout)["installer_status"] == "verify_failed"
    assert target.read_text(encoding="utf-8") == previous
    assert not list(target.parent.glob(".sanjuk-customs-export.*"))


def test_cron_d_installer_reads_back_unchanged_staging_removal(tmp_path):
    for marker in ("no-op-staging-remove", "remove-then-fail-staging"):
        installer, target, _state, control, _calls, _user_crontab = (
            _cron_d_installer_fixture(tmp_path / marker)
        )
        first = _run_installer(installer)
        assert first.returncode == 0
        (control / marker).touch()

        result = _run_installer(installer)

        assert result.returncode == 1, marker
        assert _last_json_line(result.stdout)["installer_status"] == "install_failed"
        assert target.read_bytes() == CRON_D_SOURCE.read_bytes()
        assert not list(target.parent.glob(".sanjuk-customs-export.*"))


def test_cron_d_installer_atomically_installs_and_is_idempotent(tmp_path):
    installer, target, state, _control, calls, user_crontab = _cron_d_installer_fixture(
        tmp_path
    )
    original_user_table = user_crontab.read_text(encoding="utf-8")

    first = _run_installer(installer)
    assert first.returncode == 0, (first.stdout, first.stderr)
    first_stat = target.stat()
    second = _run_installer(installer)

    assert second.returncode == 0, (second.stdout, second.stderr)
    assert target.read_bytes() == CRON_D_SOURCE.read_bytes()
    assert first_stat.st_mode & 0o777 == 0o600
    assert first_stat.st_uid == os.geteuid()
    assert first_stat.st_gid == os.getegid()
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "cron.d-backups").stat().st_mode & 0o777 == 0o700
    assert (state / "install.lock").stat().st_mode & 0o777 == 0o600
    assert _last_json_line(first.stdout)["installer_status"] == "installed"
    assert _last_json_line(second.stdout)["installer_status"] == "unchanged"
    assert user_crontab.read_text(encoding="utf-8") == original_user_table
    assert all(
        line.startswith("-n ")
        for line in calls.read_text(encoding="utf-8").splitlines()
    )
    assert not list(target.parent.glob(".sanjuk-customs-export.*"))


def test_cron_d_installer_backs_up_only_its_dedicated_target(tmp_path):
    installer, target, state, _control, _calls, user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    previous = "# previous dedicated customs schedule\n"
    target.write_text(previous, encoding="utf-8")
    target.chmod(0o600)
    original_user_table = user_crontab.read_text(encoding="utf-8")

    result = _run_installer(installer)

    assert result.returncode == 0
    assert target.read_bytes() == CRON_D_SOURCE.read_bytes()
    backups = list((state / "cron.d-backups").glob("*.cron.d"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == previous
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert user_crontab.read_text(encoding="utf-8") == original_user_table


def test_cron_d_installer_verifies_backup_before_replacing_target(tmp_path):
    for marker in ("backup-noop", "backup-corrupt"):
        installer, target, state, control, _calls, _user_crontab = (
            _cron_d_installer_fixture(tmp_path / marker)
        )
        previous = "# previous dedicated customs schedule\n"
        target.write_text(previous, encoding="utf-8")
        target.chmod(0o600)
        (control / marker).touch()

        result = _run_installer(installer)

        assert result.returncode == 1, marker
        assert _last_json_line(result.stdout)["installer_status"] == "backup_failed"
        assert target.read_text(encoding="utf-8") == previous
        assert not (control / "mv-count").exists()
        assert not list((state / "cron.d-backups").glob("*.cron.d"))


def test_cron_d_installer_failure_leaves_existing_target_unchanged(tmp_path):
    for case_name, marker, expected_status in (
        ("syntax", "fail-syntax", "syntax_failed"),
        ("move", "fail-move", "install_failed"),
        ("move-then-fail", "move-then-fail", "install_failed"),
        ("move-noop", "no-op-move", "install_failed"),
        ("copy-without-unlink", "copy-without-unlink", "install_failed"),
        ("source-race", "source-race", "preflight_failed"),
    ):
        installer, target, _state, control, _calls, user_crontab = (
            _cron_d_installer_fixture(tmp_path / case_name)
        )
        previous = "# previous dedicated customs schedule\n"
        target.write_text(previous, encoding="utf-8")
        target.chmod(0o600)
        original_user_table = user_crontab.read_text(encoding="utf-8")
        (control / marker).touch()

        result = _run_installer(installer)

        assert result.returncode == 1, case_name
        assert _last_json_line(result.stdout)["installer_status"] == expected_status
        assert target.read_text(encoding="utf-8") == previous, case_name
        assert user_crontab.read_text(encoding="utf-8") == original_user_table
        if case_name == "source-race":
            source = (
                target.parent.parent
                / "repo [cron.d]; safe/deploy/customs-export.cron.d"
            )
            active_lines = [
                line
                for line in source.read_text(encoding="utf-8").splitlines()
                if line[:1].isdigit()
            ]
            assert len(active_lines) == 2
            assert all(line.split()[5] == "kanzaka110" for line in active_lines)
            assert any(line.startswith("21 3 ") for line in active_lines)
        assert not list(target.parent.glob(".sanjuk-customs-export.*"))


def test_cron_d_installer_handles_fsync_failures_transactionally(tmp_path):
    previous = "# previous dedicated customs schedule\n"
    cases = (
        ("first-post-rename", None, 3, "install_failed", None, 0),
        ("backup-file", previous, 3, "backup_failed", previous, 0),
        ("replace-post-rename", previous, 5, "install_failed", previous, 1),
        (
            "unchanged-file",
            CRON_D_SOURCE.read_text(encoding="utf-8"),
            3,
            "verify_failed",
            CRON_D_SOURCE.read_text(encoding="utf-8"),
            0,
        ),
    )
    for case_name, initial, fail_at, status, expected, backup_count in cases:
        installer, target, state, control, _calls, _user_crontab = (
            _cron_d_installer_fixture(tmp_path / case_name)
        )
        if initial is not None:
            target.write_text(initial, encoding="utf-8")
            target.chmod(0o600)
        (control / "fail-fsync-at").write_text(f"{fail_at}\n", encoding="utf-8")

        result = _run_installer(installer)

        assert result.returncode == 1, case_name
        assert _last_json_line(result.stdout)["installer_status"] == status
        if expected is None:
            assert not target.exists()
        else:
            assert target.read_text(encoding="utf-8") == expected
        assert len(list((state / "cron.d-backups").glob("*.cron.d"))) == backup_count
        assert not list(target.parent.glob(".sanjuk-customs-export.*"))


def test_cron_d_installer_reports_rollback_fsync_failure(tmp_path):
    installer, target, state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    previous = "# previous dedicated customs schedule\n"
    target.write_text(previous, encoding="utf-8")
    target.chmod(0o600)
    (control / "corrupt-after-move").touch()
    (control / "fail-fsync-at").write_text("6\n", encoding="utf-8")

    result = _run_installer(installer)

    assert result.returncode == 2
    assert _last_json_line(result.stdout)["installer_status"] == "rollback_failed"
    assert target.read_text(encoding="utf-8") != previous
    backups = list((state / "cron.d-backups").glob("*.cron.d"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == previous


def test_cron_d_installer_rolls_back_existing_target_after_post_rename_failure(
    tmp_path,
):
    for marker in ("corrupt-after-move", "chmod-after-move"):
        installer, target, state, control, _calls, _user_crontab = (
            _cron_d_installer_fixture(tmp_path / marker)
        )
        previous = "# previous dedicated customs schedule\n"
        target.write_text(previous, encoding="utf-8")
        target.chmod(0o600)
        (control / marker).touch()

        result = _run_installer(installer)

        assert result.returncode == 1, marker
        assert _last_json_line(result.stdout)["installer_status"] == "verify_failed"
        assert target.read_text(encoding="utf-8") == previous
        assert target.stat().st_mode & 0o777 == 0o600
        backups = list((state / "cron.d-backups").glob("*.cron.d"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == previous
        assert not list(target.parent.glob(".sanjuk-customs-export.*"))


def test_cron_d_installer_removes_new_target_after_post_rename_failure(tmp_path):
    installer, target, _state, control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    (control / "corrupt-after-move").touch()

    result = _run_installer(installer)

    assert result.returncode == 1
    assert _last_json_line(result.stdout)["installer_status"] == "verify_failed"
    assert not target.exists()
    assert not list(target.parent.glob(".sanjuk-customs-export.*"))


def test_cron_d_installer_reports_new_target_remove_failure(tmp_path):
    for marker, target_survives in (
        ("fail-target-remove", True),
        ("no-op-target-remove", True),
        ("remove-then-fail-target", False),
    ):
        installer, target, _state, control, _calls, _user_crontab = (
            _cron_d_installer_fixture(tmp_path / marker)
        )
        (control / "corrupt-after-move").touch()
        (control / marker).touch()

        result = _run_installer(installer)

        assert result.returncode == 2, marker
        assert _last_json_line(result.stdout)["installer_status"] == ("rollback_failed")
        assert target.exists() is target_survives
        assert not list(target.parent.glob(".sanjuk-customs-export.*"))


def test_cron_d_installer_reports_rollback_failure_and_keeps_backup(tmp_path):
    for marker in (
        "corrupt-and-fail-rollback",
        "corrupt-rollback-readback",
        "rollback-move-then-fail",
    ):
        installer, target, state, control, _calls, _user_crontab = (
            _cron_d_installer_fixture(tmp_path / marker)
        )
        previous = "# previous dedicated customs schedule\n"
        target.write_text(previous, encoding="utf-8")
        target.chmod(0o600)
        (control / marker).touch()

        result = _run_installer(installer)

        assert result.returncode == 2, marker
        assert _last_json_line(result.stdout)["installer_status"] == ("rollback_failed")
        backups = list((state / "cron.d-backups").glob("*.cron.d"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == previous
        if marker == "rollback-move-then-fail":
            assert target.read_text(encoding="utf-8") == previous
        assert not list(target.parent.glob(".sanjuk-customs-export.*"))


def test_cron_d_installer_rejects_symlink_or_insecure_source(tmp_path):
    for case_name in (
        "target-symlink",
        "deploy-symlink",
        "source-symlink",
        "source-fifo",
        "source-oversized",
        "source-mode",
        "source-user-field",
        "logged-wrapper-mode",
        "capture-helper-mode",
        "logger-missing",
    ):
        installer, target, _state, control, _calls, _user_crontab = (
            _cron_d_installer_fixture(tmp_path / case_name)
        )
        outside = tmp_path / case_name / "outside"
        outside.write_text("do not modify\n", encoding="utf-8")
        if case_name == "target-symlink":
            target.symlink_to(outside)
        elif case_name == "deploy-symlink":
            deploy = target.parent.parent / "repo [cron.d]; safe/deploy"
            deploy_target = deploy.with_name("deploy-target")
            deploy.rename(deploy_target)
            deploy.symlink_to(deploy_target, target_is_directory=True)
        elif case_name == "logged-wrapper-mode":
            logged_wrapper = (
                target.parent.parent
                / "repo [cron.d]; safe/deploy/run_customs_export_logged.sh"
            )
            logged_wrapper.chmod(0o777)
        elif case_name == "capture-helper-mode":
            capture_helper = (
                target.parent.parent
                / "repo [cron.d]; safe/deploy/capture_bounded_output.py"
            )
            capture_helper.chmod(0o666)
        elif case_name != "logger-missing":
            source = (
                target.parent.parent
                / "repo [cron.d]; safe/deploy/customs-export.cron.d"
            )
            if case_name == "source-symlink":
                source.unlink()
                source.symlink_to(outside)
            elif case_name == "source-fifo":
                source.unlink()
                os.mkfifo(source, mode=0o600)
            elif case_name == "source-oversized":
                source.write_bytes(source.read_bytes() + b"x")
            elif case_name == "source-mode":
                source.chmod(0o666)
            else:
                source.write_text(
                    source.read_text(encoding="utf-8").replace(
                        " kanzaka110 ", " notallowed "
                    ),
                    encoding="utf-8",
                )
        else:
            (control / "logger").unlink()

        started = time.monotonic()
        result = _run_installer(installer)
        elapsed = time.monotonic() - started

        assert result.returncode == 1, case_name
        assert _last_json_line(result.stdout)["installer_status"] == (
            "preflight_failed"
        )
        if case_name == "source-fifo":
            assert elapsed < 2.0
        assert outside.read_text(encoding="utf-8") == "do not modify\n"


def test_cron_d_installer_lock_contention_never_touches_target(tmp_path):
    installer, target, state, _control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    state.mkdir(mode=0o700, exist_ok=True)
    lock_path = state / "install.lock"
    lock_path.touch(mode=0o600)
    lock_handle = lock_path.open("a", encoding="utf-8")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _run_installer(installer)
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()

    assert result.returncode == 75
    assert _last_json_line(result.stdout)["installer_status"] == "skipped_locked"
    assert not target.exists()


def test_cron_d_installer_lock_error_is_not_reported_as_contention(tmp_path):
    for case_name in ("symlink", "fifo"):
        installer, target, state, _control, _calls, _user_crontab = (
            _cron_d_installer_fixture(tmp_path / case_name)
        )
        outside = tmp_path / case_name / "outside-lock"
        outside.write_text("do not lock\n", encoding="utf-8")
        lock_path = state / "install.lock"
        if case_name == "symlink":
            lock_path.symlink_to(outside)
        else:
            os.mkfifo(lock_path, mode=0o600)

        started = time.monotonic()
        result = _run_installer(installer)
        elapsed = time.monotonic() - started

        assert result.returncode == 74, case_name
        assert _last_json_line(result.stdout)["installer_status"] == "lock_failed"
        assert elapsed < 2.0
        assert not target.exists()
        assert outside.read_text(encoding="utf-8") == "do not lock\n"


def test_cron_d_launcher_requires_delegated_systemd_scope_for_cgroup():
    launcher_text = CRON_INSTALLER_LAUNCHER.read_text(encoding="utf-8")

    assert '#define SYSTEMD_RUN_PATH "/usr/bin/systemd-run"' in launcher_text
    assert (
        '#define DELEGATED_SCOPE_PREFIX "sanjuk-customs-export-installer-"'
        in launcher_text
    )
    assert '#define CGROUP_PAYLOAD_NAME "_payload"' in launcher_text
    assert '#define DELEGATION_NONCE_ENV "SANJUK_CUSTOMS_DELEGATION_NONCE"' in launcher_text
    assert '"--scope"' in launcher_text
    assert '"--collect"' in launcher_text
    assert '"--property=Delegate=yes"' in launcher_text
    assert "execv(SYSTEMD_RUN_PATH" in launcher_text
    assert "delegated_scope_context(" in launcher_text
    assert "CGROUP2_SUPER_MAGIC" in launcher_text
    assert "fstatfs(" in launcher_text
    assert "cgroup_procs_contains_pid(root_fd, getpid())" in launcher_text
    assert "trusted, digest-pinned installer tree" in launcher_text
    assert '"/proc/self/cgroup"' in launcher_text
    assert '"cgroup.procs"' in launcher_text
    assert '"cgroup.kill"' in launcher_text
    assert '"cgroup.events"' in launcher_text
    assert '"populated 0"' in launcher_text
    assert '"populated 1"' in launcher_text
    assert "mkdir(" in launcher_text
    assert "openat(" in launcher_text
    assert "rmdir(" in launcher_text
    assert 'opendir("/proc")' not in launcher_text
    assert "kill(-process_group, SIGKILL)" not in launcher_text

    scope_admitted = launcher_text.index(
        "int delegation = delegated_scope_context();"
    )
    digest_verified = launcher_text.index("if (!verify_installer_digest())")
    lock_acquired = launcher_text.index("int lock_fd = acquire_install_lock()")
    assert scope_admitted < digest_verified < lock_acquired


def test_cgroup_direct_membership_probe_requires_exact_pid(tmp_path):
    harness = tmp_path / "cgroup-membership-probe.c"
    binary = tmp_path / "cgroup-membership-probe"
    include_path = json.dumps(str(CRON_INSTALLER_LAUNCHER))
    harness.write_text(
        "#define main sanjuk_launcher_main\n"
        f"#include {include_path}\n"
        "#undef main\n"
        "int main(int argc, char **argv) {\n"
        "    if (argc != 2) return 20;\n"
        "    int root = open(argv[1], O_RDONLY | O_DIRECTORY | O_CLOEXEC);\n"
        "    if (root < 0) return 21;\n"
        "    int out = openat(root, \"cgroup.procs\", O_WRONLY | O_CREAT | "
        "O_TRUNC | O_CLOEXEC, 0600);\n"
        "    if (out < 0) return 22;\n"
        '    if (dprintf(out, "1\\n%ld\\n", (long)getpid()) < 0) return 23;\n'
        "    close(out);\n"
        "    if (!cgroup_procs_contains_pid(root, getpid())) return 24;\n"
        "    out = openat(root, \"cgroup.procs\", O_WRONLY | O_TRUNC | "
        "O_CLOEXEC);\n"
        "    if (out < 0) return 25;\n"
        '    if (dprintf(out, "1\\n") < 0) return 26;\n'
        "    close(out);\n"
        "    if (cgroup_procs_contains_pid(root, getpid())) return 27;\n"
        "    if (unlinkat(root, \"cgroup.procs\", 0) != 0) return 28;\n"
        "    if (symlinkat(\"/proc/self/status\", root, \"cgroup.procs\") != 0) "
        "return 29;\n"
        "    if (cgroup_procs_contains_pid(root, getpid())) return 30;\n"
        "    close(root);\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    fake_cgroup = tmp_path / "fake-cgroup"
    fake_cgroup.mkdir()
    compiler = shutil.which("cc")
    assert compiler is not None
    subprocess.run(
        [
            compiler,
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-static",
            "-Wl,--build-id=none",
            str(harness),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [str(binary), str(fake_cgroup)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_cron_d_launcher_rejects_spoofed_delegation_nonce_without_state(
    tmp_path,
):
    installer, target, state, _control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    launcher = installer.with_name("install-customs-cron-launcher")
    environment = os.environ.copy()
    environment["SANJUK_CUSTOMS_DELEGATION_NONCE"] = "1"

    result = subprocess.run(
        [str(launcher)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=3,
    )

    assert result.returncode == 1
    assert _last_json_line(result.stdout)["installer_status"] == (
        "launcher_preflight_failed"
    )
    assert not (state / "install.lock").exists()
    assert not target.exists()


def test_cron_d_launcher_rejects_matching_scope_with_wrong_pid_nonce(
    tmp_path,
):
    installer, target, state, _control, _calls, _user_crontab = (
        _cron_d_installer_fixture(tmp_path)
    )
    launcher = installer.with_name("install-customs-cron-launcher")
    wrong_nonce = str(os.getpid() + 100_000_000)
    unit = f"sanjuk-customs-export-installer-{wrong_nonce}.scope"
    try:
        result = subprocess.run(
            [
                "/usr/bin/systemd-run",
                "--quiet",
                "--scope",
                "--collect",
                f"--unit={unit}",
                "--property=Delegate=yes",
                f"--setenv=SANJUK_CUSTOMS_DELEGATION_NONCE={wrong_nonce}",
                str(launcher),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert result.returncode == 1
        assert _last_json_line(result.stdout)["installer_status"] == (
            "launcher_preflight_failed"
        )
        assert not (state / "install.lock").exists()
        assert not target.exists()
    finally:
        subprocess.run(
            ["/usr/bin/systemctl", "stop", unit],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["/usr/bin/systemctl", "reset-failed", unit],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def test_cron_d_installer_has_fixed_dedicated_target_and_valid_shell_syntax(
    tmp_path,
):
    text = CRON_INSTALLER.read_text(encoding="utf-8")
    launcher_text = CRON_INSTALLER_LAUNCHER.read_text(encoding="utf-8")

    assert 'TARGET_DIR="/etc/cron.d"' in text
    assert 'TARGET_FILE="$TARGET_DIR/sanjuk-customs-export"' in text
    assert 'RUN_USER="kanzaka110"' in text
    assert "REQUIRED_EUID=0" in text
    assert "INSTALL_UID=0" in text
    assert "INSTALL_GID=0" in text
    assert '"${INSTALL_UID}:${INSTALL_GID}:600"' in text
    assert "os.fchmod(target_fd, 0o600)" in text
    assert text.startswith(
        "#!/usr/bin/env -S -i PATH=/usr/bin:/bin "
        "CUSTOMS_CRON_INSTALL_CLEAN=1 /bin/bash --noprofile --norc\n"
    )
    assert 'readonly LOGGER_BIN="/usr/bin/logger"' in text
    assert 'readonly CRON_TIMEOUT_BIN="/usr/bin/timeout"' in text
    assert 'readonly PYTHON3_BIN="/usr/bin/python3"' in text
    assert 'INSTALLER_PATH="$STATE_DIR/install-customs-export-cron.sh"' in text
    assert 'LAUNCHER_PATH="$STATE_DIR/install-customs-export-cron-launcher"' in text
    assert 'is_approved_installer_copy || fail_install "preflight_failed"' in text
    assert 'parent_exe=$("$READLINK_BIN" -f -- "/proc/$PPID/exe"' in text
    digest_match = re.search(r'readonly SOURCE_SHA256="([0-9a-f]{64})"', text)
    size_match = re.search(r"readonly SOURCE_SIZE=([0-9]+)", text)
    assert digest_match
    assert size_match
    assert (
        digest_match.group(1) == hashlib.sha256(CRON_D_SOURCE.read_bytes()).hexdigest()
    )
    assert int(size_match.group(1)) == len(CRON_D_SOURCE.read_bytes())
    assert 'has_expected_cron_users "$SOURCE_FILE"' not in text
    assert 'has_expected_cron_users "$STAGING"' in text
    assert 'has_expected_cron_users "$TARGET_FILE"' in text
    assert 'LOGGED_WRAPPER="$REPO_DIR/deploy/run_customs_export_logged.sh"' in text
    assert 'is_secure_source_artifact "$LOGGED_WRAPPER"' in text
    assert 'CAPTURE_HELPER="$REPO_DIR/deploy/capture_bounded_output.py"' in text
    assert 'is_secure_source_artifact "$CAPTURE_HELPER"' in text
    assert '"$LOGGER_BIN" --no-act --tag sanjuk-customs-export' in text
    assert "os.O_NOFOLLOW | os.O_NONBLOCK" in text
    assert "os.fsync(target_fd)" in text
    assert 'fsync_approved_path directory "$TARGET_DIR"' in text
    assert "LOCK_FILE" not in text
    assert '"$MV_BIN" -fT -- "$STAGING" "$TARGET_FILE"' in text
    backup_file_fsync = text.index('fsync_approved_path file "$BACKUP_PATH"')
    backup_dir_fsync = text.index(
        'fsync_approved_path directory "$BACKUP_DIR"',
        backup_file_fsync,
    )
    had_target = text.index("HAD_TARGET=true", backup_dir_fsync)
    install_rename = text.index(
        'if ! "$MV_BIN" -fT -- "$STAGING" "$TARGET_FILE"',
        had_target,
    )
    rollback_armed = text.index("ROLLBACK_REQUIRED=true", had_target)
    staging_readback = text.index(
        'if [[ -e "$STAGING" || -L "$STAGING" ]]',
        install_rename,
    )
    target_dir_fsync = text.index(
        'if ! fsync_approved_path directory "$TARGET_DIR"',
        staging_readback,
    )
    assert backup_file_fsync < backup_dir_fsync < had_target
    assert had_target < rollback_armed < install_rename
    assert install_rename < staging_readback < target_dir_fsync
    final_syntax_check = text.index(
        'if ! "$CRONTAB_BIN" -n "$TARGET_FILE"', target_dir_fsync
    )
    transaction_committed = text.index("ROLLBACK_REQUIRED=false", final_syntax_check)
    assert target_dir_fsync < final_syntax_check < transaction_committed
    assert "transactional_exit()" in text
    assert 'rollback_after_verify_failure "interrupted" "$exit_code"' in text
    rollback_start = text.index("rollback_after_verify_failure()")
    rollback_file_fsync = text.index(
        'fsync_approved_path file "$ROLLBACK_STAGING"',
        rollback_start,
    )
    rollback_pre_dir_fsync = text.index(
        'fsync_approved_path directory "$TARGET_DIR"',
        rollback_file_fsync,
    )
    rollback_rename = text.index(
        'if ! "$MV_BIN" -fT -- "$ROLLBACK_STAGING" "$TARGET_FILE"',
        rollback_pre_dir_fsync,
    )
    rollback_source_readback = text.index(
        'if [[ -e "$ROLLBACK_STAGING" || -L "$ROLLBACK_STAGING" ]]',
        rollback_rename,
    )
    rollback_post_dir_fsync = text.index(
        'fsync_approved_path directory "$TARGET_DIR"',
        rollback_source_readback,
    )
    rollback_target_readback = text.index(
        'is_secure_root_target "$TARGET_FILE"',
        rollback_post_dir_fsync,
    )
    assert rollback_file_fsync < rollback_pre_dir_fsync < rollback_rename
    assert rollback_rename < rollback_source_readback < rollback_post_dir_fsync
    assert rollback_post_dir_fsync < rollback_target_readback
    unchanged_file_fsync = text.index(
        'fsync_approved_path file "$TARGET_FILE"',
        text.index('if "$CMP_BIN" -s -- "$STAGING" "$TARGET_FILE"'),
    )
    unchanged_remove = text.index(
        'if ! "$RM_BIN" -f -- "$STAGING"',
        unchanged_file_fsync,
    )
    unchanged_readback = text.index(
        '[[ ! -e "$STAGING" && ! -L "$STAGING" ]]',
        unchanged_remove,
    )
    unchanged_dir_fsync = text.index(
        'fsync_approved_path directory "$TARGET_DIR"',
        unchanged_readback,
    )
    assert unchanged_file_fsync < unchanged_remove < unchanged_readback
    assert unchanged_readback < unchanged_dir_fsync
    assert "crontab -l" not in text
    assert "--restore-managed-block" not in text
    assert "clearenv()" in launcher_text
    assert "if (argc != 1)" in launcher_text
    launcher_digest = re.search(
        r'#define INSTALLER_SHA256 \\\n\s+"([0-9a-f]{64})"', launcher_text
    )
    launcher_size = re.search(
        r"#define INSTALLER_SIZE \(\(off_t\)([0-9]+)\)", launcher_text
    )
    assert launcher_digest
    assert launcher_size
    assert (
        launcher_digest.group(1)
        == hashlib.sha256(CRON_INSTALLER.read_bytes()).hexdigest()
    )
    assert int(launcher_size.group(1)) == len(CRON_INSTALLER.read_bytes())
    assert '#define SHA256SUM_PATH "/usr/bin/sha256sum"' in launcher_text
    assert "verify_installer_digest()" in launcher_text
    digest_verified = launcher_text.index("if (!verify_installer_digest())")
    lock_acquired = launcher_text.index(
        "int lock_fd = acquire_install_lock()", digest_verified
    )
    assert digest_verified < lock_acquired
    assert '#define LOCK_PATH STATE_DIR "/install.lock"' in launcher_text
    assert "O_CLOEXEC" in launcher_text
    assert "flock(lock_fd, LOCK_EX | LOCK_NB)" in launcher_text
    assert "fork()" in launcher_text
    assert "PR_SET_PDEATHSIG" in launcher_text
    assert "setpgid(0, 0)" in launcher_text
    assert "sigaction(SIGCHLD, &default_action, NULL)" in launcher_text
    assert "if (!restore_child_signals())" in launcher_text
    assert "sigprocmask(SIG_SETMASK, &clean_mask, NULL)" in launcher_text
    parent_group_ready = launcher_text.index("child_process_group = child;")
    timeout_armed = launcher_text.index(
        "alarm(INSTALL_TIMEOUT_SECONDS);", parent_group_ready
    )
    signals_unmasked = launcher_text.index(
        "sigprocmask(SIG_SETMASK, &clean_mask, NULL)", timeout_armed
    )
    child_released = launcher_text.index(
        "write(barrier[1], &token, 1)", signals_unmasked
    )
    assert timeout_armed < signals_unmasked < child_released
    assert launcher_text.count("alarm(INSTALL_TIMEOUT_SECONDS);") == 1
    assert "pipe2(barrier, O_CLOEXEC)" in launcher_text
    assert "pipe2(watchdog_pipe, O_CLOEXEC)" in launcher_text
    assert "token != 'G'" in launcher_text
    assert "token != 'D'" not in launcher_text
    assert "signal_child_group(SIGKILL)" in launcher_text
    assert 'opendir("/proc")' not in launcher_text
    assert "kill(-process_group, SIGKILL)" not in launcher_text
    assert "move_child_to_cgroup(&scope, child)" in launcher_text
    assert "token == 'W'" not in launcher_text
    assert "contain_installer_cgroup(&scope)" in launcher_text
    assert "write_cgroup_kill(scope)" in launcher_text
    assert "cgroup_is_populated(scope)" in launcher_text
    assert "stable_empty_checks >= 2U" in launcher_text
    watchdog_containment = launcher_text.index(
        "contain_installer_cgroup(&scope);"
    )
    watchdog_removal = launcher_text.index(
        "remove_installer_cgroup_or_wait(&scope);", watchdog_containment
    )
    watchdog_exit = launcher_text.index("_exit(0);", watchdog_removal)
    assert watchdog_containment < watchdog_removal < watchdog_exit
    assert "WEXITED | WNOWAIT" in launcher_text
    assert "finish_watchdog(watchdog_pipe[1], watchdog, &scope)" in launcher_text
    parent_fallback = launcher_text.index("if (!watchdog_ok)")
    assert launcher_text.index(
        "contain_installer_cgroup(scope);", parent_fallback
    ) < launcher_text.index(
        "remove_installer_cgroup_or_wait(scope);", parent_fallback
    )
    recovery_failed = launcher_text.index("WEXITSTATUS(status) == 2")
    timeout_result = launcher_text.index("if (timed_out)", recovery_failed)
    assert recovery_failed < timeout_result
    assert "sigaction(SIGPIPE, &ignore_action, NULL)" in launcher_text
    assert "sigaction(SIGPIPE, &action, NULL)" in launcher_text
    assert 'execv("/bin/bash", argv)' in launcher_text
    assert "reaped = waitpid(child, &status, 0)" in launcher_text
    assert 'readlink("/proc/self/exe"' in launcher_text
    shell_result = subprocess.run(
        ["/bin/bash", "-n", str(CRON_INSTALLER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert shell_result.returncode == 0, shell_result.stderr
    compiler = shutil.which("cc")
    assert compiler is not None
    compile_result = subprocess.run(
        [
            compiler,
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-static",
            "-Wl,--build-id=none",
            str(CRON_INSTALLER_LAUNCHER),
            "-o",
            str(tmp_path / "installer-launcher"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    readelf = shutil.which("readelf")
    assert readelf is not None
    elf_result = subprocess.run(
        [readelf, "-l", str(tmp_path / "installer-launcher")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert elf_result.returncode == 0, elf_result.stderr
    assert "INTERP" not in elf_result.stdout
