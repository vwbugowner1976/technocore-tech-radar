#!/usr/bin/env python3
"""Small advisory file lock used to serialize managed MLX work across processes."""

from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path
from typing import TextIO


class InterprocessFileLock:
    """Exclusive advisory lock with a bounded wait.

    The lock is intentionally process-scoped via ``flock`` and is released when
    the file descriptor closes, including normal process exit. A crashed process
    therefore cannot leave a permanently stale lock behind.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        timeout_seconds: float,
        poll_seconds: float = 0.05,
    ) -> None:
        self.path = Path(path).expanduser()
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.poll_seconds = max(0.01, min(1.0, float(poll_seconds)))
        self.handle: TextIO | None = None
        self.waited_seconds = 0.0

    def acquire(self) -> float:
        if self.handle is not None:
            return self.waited_seconds

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        started = time.monotonic()
        deadline = started + self.timeout_seconds

        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                pass
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    handle.close()
                    raise

            now = time.monotonic()
            if now >= deadline:
                handle.close()
                waited = max(0.0, now - started)
                raise TimeoutError(
                    f"managed MLX process lock timed out after {waited:.1f}s "
                    f"path={self.path}"
                )
            time.sleep(min(self.poll_seconds, max(0.0, deadline - now)))

        self.waited_seconds = max(0.0, time.monotonic() - started)
        self.handle = handle
        try:
            handle.seek(0)
            handle.truncate(0)
            handle.write(f"pid={os.getpid()} acquired={time.time():.6f}\n")
            handle.flush()
        except Exception:
            # Lock ownership does not depend on the diagnostic text.
            pass
        return self.waited_seconds

    def release(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "InterprocessFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False
