#!/usr/bin/env python3
"""Small sudo-free log rotator for TechnoScout on macOS.

The rotator is designed for launchd stdout/stderr files that may still have an
open file descriptor. It therefore uses copy+truncate instead of rename for the
active log file.

Safety properties:
- rotates only regular files directly below the configured log directory
- never follows symlinks
- compresses and installs an archive before truncating the active file
- aborts rotation if the source grows or is replaced while the snapshot is made
- preserves the active inode so long-running launchd processes keep logging
- serializes concurrent rotators with a local flock
- excludes its own logrotate*.log files
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import os
from pathlib import Path
import tempfile
from typing import Any


DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP = 7
CHUNK = 1024 * 1024


def _archive_path(path: Path, generation: int) -> Path:
    return path.with_name(f"{path.name}.{generation}.gz")


def _eligible(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
    except OSError:
        return False
    return path.name.endswith(".log") and not path.name.startswith("logrotate")


def _shift_archives(path: Path, keep: int) -> None:
    oldest = _archive_path(path, keep)
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass

    for generation in range(keep - 1, 0, -1):
        src = _archive_path(path, generation)
        dst = _archive_path(path, generation + 1)
        if src.exists():
            os.replace(src, dst)


def rotate_file(path: Path, *, max_bytes: int, keep: int, dry_run: bool = False) -> dict[str, Any]:
    """Rotate one log if it is large enough.

    The source is copied only up to the initial size. If inode/size changes
    before truncation, the active log is left untouched. This intentionally
    prefers a delayed rotation over losing newly appended bytes.
    """
    if not _eligible(path):
        return {"file": path.name, "state": "SKIPPED", "reason": "not eligible"}

    try:
        before = path.stat()
    except FileNotFoundError:
        return {"file": path.name, "state": "SKIPPED", "reason": "missing"}

    if before.st_size < max_bytes:
        return {"file": path.name, "state": "SKIPPED", "reason": "below threshold", "bytes": before.st_size}

    if dry_run:
        return {"file": path.name, "state": "WOULD_ROTATE", "bytes": before.st_size}

    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".gz.tmp", dir=str(path.parent))
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    installed_archive = False

    try:
        remaining = before.st_size
        with path.open("rb", buffering=0) as src, gzip.open(tmp, "wb", compresslevel=6) as out:
            while remaining:
                chunk = src.read(min(CHUNK, remaining))
                if not chunk:
                    return {"file": path.name, "state": "DEFERRED", "reason": "source shortened during snapshot"}
                out.write(chunk)
                remaining -= len(chunk)

        # Fail closed if a writer appended while we were making the snapshot or
        # if the file was replaced. Retry on the next hourly pass instead.
        try:
            after_copy = path.stat()
        except FileNotFoundError:
            return {"file": path.name, "state": "DEFERRED", "reason": "source disappeared"}
        if after_copy.st_ino != before.st_ino or after_copy.st_size != before.st_size:
            return {"file": path.name, "state": "DEFERRED", "reason": "source changed during snapshot"}

        # Archive operations happen before touching the active log. If any of
        # these fail, the active file remains completely intact.
        _shift_archives(path, keep)
        os.replace(tmp, _archive_path(path, 1))
        installed_archive = True
        tmp = Path()  # mark consumed

        # Re-check immediately before truncate. If a writer appended in the
        # meantime, keep the active log intact and simply retry later. The
        # already-installed archive is then only a harmless duplicate snapshot.
        with path.open("r+b", buffering=0) as active:
            current = os.fstat(active.fileno())
            if current.st_ino != before.st_ino or current.st_size != before.st_size:
                return {"file": path.name, "state": "DEFERRED", "reason": "source changed before truncate"}
            active.truncate(0)
            os.fsync(active.fileno())

        return {"file": path.name, "state": "ROTATED", "bytes": before.st_size}
    finally:
        if str(tmp) not in {"", "."}:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def rotate_logs(log_dir: Path, *, max_bytes: int = DEFAULT_MAX_BYTES, keep: int = DEFAULT_KEEP, dry_run: bool = False) -> list[dict[str, Any]]:
    log_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = max(1, int(max_bytes))
    keep = max(1, min(30, int(keep)))

    lock_path = log_dir / ".logrotate.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return [{"file": "*", "state": "BUSY", "reason": "another rotator is active"}]

        results: list[dict[str, Any]] = []
        for path in sorted(log_dir.iterdir(), key=lambda item: item.name):
            if _eligible(path):
                results.append(rotate_file(path, max_bytes=max_bytes, keep=keep, dry_run=dry_run))
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="sudo-free TechnoScout log rotation")
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    results = rotate_logs(Path(args.log_dir), max_bytes=args.max_bytes, keep=args.keep, dry_run=args.dry_run)
    rotated = sum(1 for item in results if item["state"] == "ROTATED")
    deferred = sum(1 for item in results if item["state"] == "DEFERRED")
    would_rotate = sum(1 for item in results if item["state"] == "WOULD_ROTATE")
    print(
        f"TechnoScout Log Rotate | files={len(results)} rotated={rotated} "
        f"deferred={deferred} would_rotate={would_rotate}"
    )
    for item in results:
        if item["state"] in {"ROTATED", "DEFERRED", "WOULD_ROTATE", "BUSY"}:
            suffix = f" bytes={item['bytes']}" if "bytes" in item else ""
            reason = f" reason={item['reason']}" if item.get("reason") else ""
            print(f"  {item['file']} state={item['state']}{suffix}{reason}")


if __name__ == "__main__":
    main()
