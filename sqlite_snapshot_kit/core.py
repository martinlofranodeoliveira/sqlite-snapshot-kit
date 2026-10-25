"""File snapshots, with SQLite doing the database copying and validation."""

from __future__ import annotations

import hashlib
import math
import numbers
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time


class SnapshotError(Exception):
    """The snapshot could not be created or verified; messages contain no paths."""


_ERROR = "snapshot operation failed"
_HEADER = b"SQLite format 3\x00"
_FILE_ERRORS = (OSError, sqlite3.Error, TypeError, ValueError, OverflowError, RuntimeError)


class _Budget:
    def __init__(self, timeout: float):
        if isinstance(timeout, bool) or not isinstance(timeout, numbers.Real):
            raise SnapshotError(_ERROR)
        seconds = float(timeout)
        if not math.isfinite(seconds) or not 0 < seconds <= 3600:
            raise SnapshotError(_ERROR)
        self.deadline = time.monotonic() + seconds

    def check(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SnapshotError(_ERROR)
        return remaining

    def sql_progress(self) -> int:
        # Returning nonzero interrupts sqlite3_step, including integrity_check.
        return int(time.monotonic() >= self.deadline)

    def backup_progress(self, status: int, remaining: int, total: int) -> None:
        # The callback also runs when a locked source makes backup return BUSY.
        self.check()


def _database_path(value: os.PathLike[str] | str, budget: _Budget) -> Path:
    budget.check()
    path = Path(value).resolve(strict=True)
    if not stat.S_ISREG(path.stat().st_mode):
        raise SnapshotError(_ERROR)
    with path.open("rb") as stream:
        if stream.read(len(_HEADER)) != _HEADER:
            # Reject empty/uninitialized files, which SQLite otherwise accepts.
            raise SnapshotError(_ERROR)
    budget.check()
    return path


def _read_connection(path: Path, budget: _Budget) -> sqlite3.Connection:
    return sqlite3.connect(
        path.as_uri() + "?mode=ro",
        uri=True,
        timeout=budget.check(),
        isolation_level=None,
    )


def _verify(path: Path, budget: _Budget) -> dict[str, object]:
    connection = _read_connection(path, budget)
    try:
        connection.set_progress_handler(budget.sql_progress, 1000)
        budget.check()
        results = connection.execute("PRAGMA integrity_check")
        # Full integrity_check can return several diagnostics on a broken file.
        # None of those diagnostics are included in a report or public error.
        if results.fetchone() != ("ok",) or results.fetchone() is not None:
            raise SnapshotError(_ERROR)
        budget.check()
        metadata = {}
        for name in ("page_count", "page_size", "user_version"):
            budget.check()
            metadata[name] = connection.execute("PRAGMA " + name).fetchone()[0]
        budget.check()
    finally:
        connection.close()

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            budget.check()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    budget.check()
    return {
        "ok": True,
        "integrity": "ok",
        "sha256": digest.hexdigest(),
        "size_bytes": size,
        **metadata,
    }


def verify_snapshot(
    path: os.PathLike[str] | str, timeout: float = 5.0
) -> dict[str, object]:
    """Run full integrity_check and hash a stable local SQLite file.

    timeout must be finite, greater than zero, and at most 3600 seconds.
    A caller must prevent concurrent modification of the file while verifying.
    """
    try:
        budget = _Budget(timeout)
        return _verify(_database_path(path, budget), budget)
    except _FILE_ERRORS:
        raise SnapshotError(_ERROR) from None


def create_snapshot(
    source: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Back up a SQLite database, validate, and publish a new file atomically.

    The source is opened using SQLite URI mode=ro. The existing parent of
    destination must support POSIX hard links; an existing path is never replaced.
    """
    staging = None
    source_connection = None
    target_connection = None
    try:
        budget = _Budget(timeout)
        if os.name != "posix":
            raise SnapshotError(_ERROR)
        source_path = _database_path(source, budget)
        target_path = Path(destination).absolute()
        if os.path.lexists(target_path) or not target_path.parent.is_dir():
            raise SnapshotError(_ERROR)

        source_connection = _read_connection(source_path, budget)
        descriptor, name = tempfile.mkstemp(
            prefix=".sqlite-snapshot-", suffix=".tmp", dir=target_path.parent
        )
        staging = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        target_connection = sqlite3.connect(
            str(staging), timeout=budget.check(), isolation_level=None
        )
        source_connection.backup(
            target_connection,
            pages=64,
            progress=budget.backup_progress,
            sleep=min(0.01, budget.check()),
        )
        budget.check()
        # A WAL source may transfer persistent journal-mode flags. Finish the
        # destination in DELETE mode so the published main file stands alone.
        target_connection.set_progress_handler(budget.sql_progress, 1000)
        if target_connection.execute("PRAGMA journal_mode=DELETE").fetchone() != (
            "delete",
        ):
            raise SnapshotError(_ERROR)
        target_connection.close()
        target_connection = None
        source_connection.close()
        source_connection = None
        budget.check()

        report = _verify(_database_path(staging, budget), budget)
        with staging.open("rb") as stream:
            os.fsync(stream.fileno())
        budget.check()
        # Unlike rename/replace, link fails if *any* destination entry exists,
        # including a dangling symlink or a path created since the first check.
        os.link(staging, target_path, follow_symlinks=False)
        return report
    except _FILE_ERRORS:
        raise SnapshotError(_ERROR) from None
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
        if staging is not None:
            for suffix in ("-wal", "-shm", "-journal", ""):
                try:
                    Path(str(staging) + suffix).unlink()
                except OSError:
                    # Cleanup must not replace the original error, or turn a
                    # successfully published snapshot into a reported failure.
                    pass
