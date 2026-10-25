"""Only databases created in temporary directories are ever opened by these tests."""

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from sqlite_snapshot_kit import SnapshotError, create_snapshot, verify_snapshot
from sqlite_snapshot_kit import core


KEYS = {
    "ok", "integrity", "sha256", "size_bytes", "page_count", "page_size", "user_version"
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_database(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version=7")
        connection.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, value TEXT)")
        connection.executemany(
            "INSERT INTO samples(value) VALUES (?)",
            [("synthetic apple",), ("synthetic pear",)],
        )


def damage_index(path):
    # Keep every page structurally valid but make the index disagree with its
    # table. quick_check accepts this; full integrity_check must reject it.
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('synthetic_unique_value')")
        connection.execute("CREATE INDEX sample_value ON sample(value)")
    data = bytearray(path.read_bytes())
    offset = data.rfind(b"synthetic_unique_value")
    if offset < 0:
        raise AssertionError("synthetic index marker missing")
    data[offset] = ord("t")
    path.write_bytes(data)


@unittest.skipUnless(os.name == "posix", "snapshot publication requires POSIX")
class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "synthetic-source.db"
        self.destination = self.root / "synthetic-snapshot.db"
        make_database(self.source)

    def assert_clean(self):
        self.assertEqual(list(self.root.glob(".sqlite-snapshot-*")), [])

    def assert_report(self, report, path):
        self.assertEqual(set(report), KEYS)
        self.assertIs(report["ok"], True)
        self.assertEqual(report["integrity"], "ok")
        self.assertEqual(report["sha256"], digest(path))
        self.assertEqual(report["size_bytes"], path.stat().st_size)
        self.assertEqual(report["size_bytes"], report["page_count"] * report["page_size"])
        self.assertEqual(report["user_version"], 7)
        self.assertNotIn(str(self.root), json.dumps(report))

    def test_create_verify_and_source_preservation(self):
        original = digest(self.source)
        report = create_snapshot(self.source, self.destination)
        self.assert_report(report, self.destination)
        self.assertEqual(report, verify_snapshot(self.destination))
        self.assertEqual(digest(self.source), original)
        with closing(sqlite3.connect(self.destination)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM samples").fetchone(), (2,))
        self.assert_clean()

    def test_committed_live_wal_data_is_included(self):
        connection = sqlite3.connect(self.source)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO samples(value) VALUES ('committed WAL-only sample')")
        connection.commit()
        wal = Path(str(self.source) + "-wal")
        self.assertTrue(wal.is_file())
        original = (digest(self.source), digest(wal))
        report = create_snapshot(self.source, self.destination)
        self.assert_report(report, self.destination)
        self.assertEqual((digest(self.source), digest(wal)), original)
        with closing(sqlite3.connect(self.destination)) as snapshot:
            self.assertEqual(snapshot.execute("SELECT count(*) FROM samples").fetchone(), (3,))
            self.assertEqual(snapshot.execute("PRAGMA journal_mode").fetchone(), ("delete",))
        self.assertFalse(Path(str(self.destination) + "-wal").exists())
        self.assertFalse(Path(str(self.destination) + "-shm").exists())
        self.assert_clean()

    def test_uncommitted_wal_data_is_excluded(self):
        connection = sqlite3.connect(self.source)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO samples(value) VALUES ('uncommitted synthetic sample')")
        create_snapshot(self.source, self.destination)
        with closing(sqlite3.connect(self.destination)) as snapshot:
            self.assertEqual(snapshot.execute("SELECT count(*) FROM samples").fetchone(), (2,))
        connection.rollback()

    def test_destination_and_staging_permissions_are_0600(self):
        real_link = os.link
        observed = []

        def inspect_then_link(source, destination, **kwargs):
            observed.append(stat.S_IMODE(Path(source).stat().st_mode))
            return real_link(source, destination, **kwargs)

        with mock.patch.object(core.os, "link", side_effect=inspect_then_link):
            create_snapshot(self.source, self.destination)
        self.assertEqual(observed, [0o600])
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o600)
        self.assert_clean()

    def test_weird_filenames_use_encoded_sqlite_uri(self):
        unusual = self.root / "synthetic # ? % ü.db"
        self.source.rename(unusual)
        report = create_snapshot(str(unusual), str(self.destination))
        self.assert_report(report, self.destination)

    def test_source_symlink_can_be_read(self):
        alias = self.root / "synthetic-alias.db"
        alias.symlink_to(self.source)
        self.assert_report(create_snapshot(alias, self.destination), self.destination)
        self.assertTrue(alias.is_symlink())

    def test_existing_regular_file_and_empty_file_are_preserved(self):
        for content in (b"", b"synthetic existing content"):
            with self.subTest(content=content):
                self.destination.write_bytes(content)
                with self.assertRaises(SnapshotError):
                    create_snapshot(self.source, self.destination)
                self.assertEqual(self.destination.read_bytes(), content)
                self.destination.unlink()
                self.assert_clean()

    def test_source_as_destination_is_preserved(self):
        before = digest(self.source)
        with self.assertRaises(SnapshotError):
            create_snapshot(self.source, self.source)
        self.assertEqual(digest(self.source), before)
        self.assert_clean()

    def test_existing_hardlink_to_source_is_preserved(self):
        os.link(self.source, self.destination)
        before = digest(self.source)
        with self.assertRaises(SnapshotError):
            create_snapshot(self.source, self.destination)
        self.assertTrue(os.path.samefile(self.source, self.destination))
        self.assertEqual(digest(self.source), before)
        self.assert_clean()

    def test_existing_live_and_broken_symlinks_are_preserved(self):
        for link_target in (self.source, self.root / "nonexistent-synthetic.db"):
            with self.subTest(link_target=link_target.name):
                self.destination.symlink_to(link_target)
                with self.assertRaises(SnapshotError):
                    create_snapshot(self.source, self.destination)
                self.assertTrue(self.destination.is_symlink())
                self.assertEqual(self.destination.readlink(), link_target)
                self.destination.unlink()
                self.assert_clean()

    def test_existing_directory_and_fifo_are_preserved(self):
        self.destination.mkdir()
        with self.assertRaises(SnapshotError):
            create_snapshot(self.source, self.destination)
        self.assertTrue(self.destination.is_dir())
        self.destination.rmdir()
        os.mkfifo(self.destination)
        with self.assertRaises(SnapshotError):
            create_snapshot(self.source, self.destination)
        self.assertTrue(stat.S_ISFIFO(self.destination.stat().st_mode))
        self.assert_clean()

    def test_publication_race_does_not_replace_concurrent_file(self):
        real_link = os.link

        def concurrent_creator(source, destination, **kwargs):
            Path(destination).write_bytes(b"synthetic concurrent owner")
            return real_link(source, destination, **kwargs)

        with mock.patch.object(core.os, "link", side_effect=concurrent_creator):
            with self.assertRaises(SnapshotError):
                create_snapshot(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"synthetic concurrent owner")
        self.assert_clean()

    def test_failed_publication_removes_stage(self):
        with mock.patch.object(core.os, "link", side_effect=OSError("synthetic failure")):
            with self.assertRaises(SnapshotError):
                create_snapshot(self.source, self.destination)
        self.assertFalse(os.path.lexists(self.destination))
        self.assert_clean()

    def test_missing_and_invalid_parent_reject(self):
        for parent in (self.root / "missing", self.source):
            with self.subTest(parent=parent.name):
                with self.assertRaises(SnapshotError):
                    create_snapshot(self.source, parent / "snapshot.db")
                self.assert_clean()

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory mode bits")
    def test_unwritable_parent_rejects_without_stage(self):
        parent = self.root / "synthetic-readonly"
        parent.mkdir()
        parent.chmod(0o500)
        try:
            with self.assertRaises(SnapshotError):
                create_snapshot(self.source, parent / "snapshot.db")
            self.assertEqual(list(parent.iterdir()), [])
        finally:
            parent.chmod(0o700)

    def test_missing_invalid_empty_and_directory_sources_reject(self):
        invalid = self.root / "synthetic-text.db"
        invalid.write_text("synthetic data, not SQLite")
        empty = self.root / "synthetic-empty.db"
        empty.touch()
        for source in (self.root / "missing.db", invalid, empty, self.root):
            for operation in (verify_snapshot, lambda p: create_snapshot(p, self.destination)):
                with self.subTest(source=source.name, operation=operation):
                    with self.assertRaises(SnapshotError):
                        operation(source)
                    self.assertFalse(self.destination.exists())
                    self.assert_clean()

    def test_symlink_loop_and_invalid_path_reject_as_snapshot_error(self):
        loop = self.root / "synthetic-loop"
        loop.symlink_to(loop)
        for source in (loop, "bad\x00path", None, 42):
            with self.subTest(source=repr(source)):
                with self.assertRaises(SnapshotError):
                    verify_snapshot(source)

    def test_truncated_database_rejects(self):
        self.source.write_bytes(self.source.read_bytes()[:-128])
        with self.assertRaises(SnapshotError):
            verify_snapshot(self.source)
        with self.assertRaises(SnapshotError):
            create_snapshot(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assert_clean()

    def test_full_integrity_check_detects_logically_inconsistent_index(self):
        damaged = self.root / "synthetic-damaged-index.db"
        damage_index(damaged)
        with closing(sqlite3.connect(damaged)) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertNotEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        before = digest(damaged)
        with self.assertRaises(SnapshotError):
            verify_snapshot(damaged)
        with self.assertRaises(SnapshotError):
            create_snapshot(damaged, self.destination)
        self.assertEqual(digest(damaged), before)
        self.assertFalse(self.destination.exists())
        self.assert_clean()

    def test_invalid_timeouts_reject_before_publication(self):
        for timeout in (True, False, 0, -1, float("nan"), float("inf"), 3600.1, "5", None):
            with self.subTest(timeout=timeout):
                with self.assertRaises(SnapshotError):
                    create_snapshot(self.source, self.destination, timeout=timeout)
                with self.assertRaises(SnapshotError):
                    verify_snapshot(self.source, timeout=timeout)
                self.assertFalse(self.destination.exists())
                self.assert_clean()

    def test_tiny_deadline_rejects_without_publication(self):
        with self.assertRaises(SnapshotError):
            create_snapshot(self.source, self.destination, timeout=1e-12)
        with self.assertRaises(SnapshotError):
            verify_snapshot(self.source, timeout=1e-12)
        self.assertFalse(self.destination.exists())
        self.assert_clean()

    def test_locked_source_backup_obeys_deadline_and_cleans_up(self):
        connection = sqlite3.connect(self.source)
        self.addCleanup(connection.close)
        connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with self.assertRaises(SnapshotError):
            create_snapshot(self.source, self.destination, timeout=0.04)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(self.destination.exists())
        self.assert_clean()
        connection.rollback()

    def test_locked_verification_obeys_deadline(self):
        connection = sqlite3.connect(self.source)
        self.addCleanup(connection.close)
        connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with self.assertRaises(SnapshotError):
            verify_snapshot(self.source, timeout=0.04)
        self.assertLess(time.monotonic() - started, 1.0)
        connection.rollback()

    def test_large_integrity_check_can_be_interrupted(self):
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute(
                "WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL "
                "SELECT n+1 FROM numbers WHERE n<100000) "
                "INSERT INTO samples(value) SELECT printf('%0200d', n) FROM numbers"
            )
        started = time.monotonic()
        with self.assertRaises(SnapshotError):
            verify_snapshot(self.source, timeout=0.002)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_api_error_does_not_disclose_paths_or_sqlite_diagnostics(self):
        private_marker = self.root / "synthetic-sensitive-marker.db"
        with self.assertRaises(SnapshotError) as caught:
            verify_snapshot(private_marker)
        self.assertEqual(str(caught.exception), "snapshot operation failed")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "synthetic-source.db"
        self.destination = self.root / "synthetic-output.db"
        make_database(self.source)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "sqlite_snapshot_kit", *map(str, args)],
            capture_output=True, text=True, timeout=5,
        )

    def test_create_and_verify_output_only_success_json(self):
        create = self.cli("create", self.source, self.destination)
        self.assertEqual(create.returncode, 0, create.stderr)
        self.assertEqual(create.stderr, "")
        report = json.loads(create.stdout)
        self.assertEqual(set(report), KEYS)
        self.assertEqual(report["sha256"], digest(self.destination))
        verify = self.cli("verify", self.destination, "--timeout", "2")
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertEqual(verify.stderr, "")
        self.assertEqual(json.loads(verify.stdout), report)

    def test_errors_are_fixed_sanitized_stderr_and_exit_2(self):
        cases = [
            (), ("unknown-synthetic-command",), ("create",),
            ("verify", self.root / "synthetic-missing.db"),
            ("create", self.source, self.source),
            ("verify", self.source, "--timeout", "synthetic-secret-value"),
            ("verify", self.source, "--timeout", "nan"),
            ("verify", self.source, "--timeout", "0"),
            ("verify", self.source, "--unexpected-synthetic-option"),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "sqlite-snapshot-kit: operation failed\n")


if __name__ == "__main__":
    unittest.main()
