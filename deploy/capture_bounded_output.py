#!/usr/bin/python3
"""Drain stdin completely while storing at most a fixed number of bytes."""

from __future__ import annotations

import os
import stat
import sys

_EXIT_USAGE = 64
_EXIT_IO = 74
_READ_SIZE = 64 * 1024
_TRUNCATION_MARKER = b"\n[customs-export output truncated]\n"


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short_write")
        view = view[written:]


def main(*, input_fd: int = 0) -> int:
    if len(sys.argv) != 3:
        return _EXIT_USAGE
    output_path = sys.argv[1]
    try:
        limit = int(sys.argv[2])
    except ValueError:
        return _EXIT_USAGE
    if limit <= len(_TRUNCATION_MARKER):
        return _EXIT_USAGE
    if not os.path.basename(output_path).startswith(".customs-export-output."):
        return _EXIT_USAGE

    fd = -1
    storage_failed = False
    try:
        try:
            fd = os.open(
                output_path,
                os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            metadata = os.fstat(fd)
            if not (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and stat.S_IMODE(metadata.st_mode) == 0o600
                and metadata.st_nlink == 1
                and metadata.st_size == 0
            ):
                storage_failed = True
        except OSError:
            storage_failed = True

        payload_limit = limit - len(_TRUNCATION_MARKER)
        stored = 0
        truncated = False
        while True:
            chunk = os.read(input_fd, _READ_SIZE)
            if not chunk:
                break
            remaining = payload_limit - stored
            if remaining > 0:
                selected = chunk[:remaining]
                if not storage_failed:
                    try:
                        _write_all(fd, selected)
                    except OSError:
                        storage_failed = True
                stored += len(selected)
                if len(selected) != len(chunk):
                    truncated = True
            else:
                truncated = True
        if storage_failed:
            return _EXIT_IO
        if truncated:
            _write_all(fd, _TRUNCATION_MARKER)
        os.fsync(fd)
        return 0
    except OSError:
        return _EXIT_IO
    finally:
        if fd >= 0:
            os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
