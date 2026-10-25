"""Create and verify consistent SQLite snapshot files using the standard library."""

from .core import SnapshotError, create_snapshot, verify_snapshot

__all__ = ["SnapshotError", "create_snapshot", "verify_snapshot"]
__version__ = "0.1.0"
