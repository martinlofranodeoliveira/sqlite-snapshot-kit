# sqlite-snapshot-kit

Create consistent local SQLite snapshots and verify their integrity using only
Python's standard library. A snapshot includes committed data in a live WAL;
copying just the database's main file can miss that data.

The tool reads its source through SQLite's `mode=ro` URI and copies it with the
[SQLite Online Backup API](https://sqlite.org/backup.html). It checks the entire
snapshot with `PRAGMA integrity_check`, calculates its SHA-256, then publishes a
new file without replacing an existing destination.

Python 3.10+ with the standard `sqlite3` module is required. Creating snapshots
requires a POSIX system and a filesystem that supports hard links within the
destination directory. There are no runtime dependencies.

## Try it with synthetic data

From a checkout:

```sh
python -m examples.demo
python -m unittest discover -s tests -v
```

The demo creates a temporary database containing three invented fruit names,
backs it up, and verifies it. It removes all its temporary files when finished.
The tests also use only temporary, synthetic fixtures; they do not discover or
open other databases on the machine.

To install the command:

```sh
python -m pip install .
sqlite-snapshot-kit --help
```

## Command line

```sh
python -m sqlite_snapshot_kit create SOURCE.db SNAPSHOT.db --timeout 5
python -m sqlite_snapshot_kit verify SNAPSHOT.db --timeout 5
```

`sqlite-snapshot-kit` accepts the same arguments after installation. Choose a
new destination filename in an existing directory. Each successful operation
writes one JSON object to stdout. Its exact keys are:

| Key | Value |
| --- | --- |
| `ok` | `true` |
| `integrity` | `"ok"` |
| `sha256` | SHA-256 hex digest of the resulting file bytes |
| `size_bytes` | Main file size in bytes |
| `page_count` | SQLite page count |
| `page_size` | SQLite page size in bytes |
| `user_version` | Database's integer `PRAGMA user_version` |

Reports contain no paths, schema definitions, or row data. An unsuccessful
command writes no JSON, exits with status 2, and writes exactly
`sqlite-snapshot-kit: operation failed` followed by a newline to stderr.
Argument errors use the same diagnostic, including invalid timeout values.
`--help` prints conventional command help and exits successfully.

## Python API

```python
from sqlite_snapshot_kit import SnapshotError, create_snapshot, verify_snapshot

try:
    report = create_snapshot("SOURCE.db", "NEW-SNAPSHOT.db", timeout=5.0)
    verified = verify_snapshot("NEW-SNAPSHOT.db", timeout=5.0)
except SnapshotError:
    # Operation failed: check the paths, permissions, locks, and database health.
    pass
```

Both functions accept string paths or `pathlib.Path` objects and return the
report described above. `SnapshotError` uses a fixed message that does not expose
paths or SQLite's potentially sensitive diagnostics. Its generic nature is
intentional; this package does not log database content or detailed errors.

`timeout` defaults to 5 seconds. It must be a finite real number greater than
zero and at most 3600; booleans and strings are rejected by the API. The budget
covers opening, backup, integrity checking, hashing, and prepublication work.
Backup progress callbacks check the deadline, including during lock retries.
An SQLite progress handler interrupts a long integrity check. Hashing checks
between chunks. These are cooperative deadlines: OS I/O, filesystem calls, an
individual SQLite step, and connection lock waits can outlast the remaining
budget. This is not a hard real-time process timeout.

## Publication and database behavior

- The source must be an existing regular SQLite file with its SQLite header.
  Empty/uninitialized files, missing paths, directories, and corrupt databases
  are rejected. Source symlinks are resolved. The source connection is read-only;
  the package executes no database writes through it.
- SQLite manages [WAL and shared-memory sidecars](https://sqlite.org/wal.html#read_only_databases).
  A read-only connection can
  require existing readable sidecars or permission to create them in the source
  directory, depending on SQLite and the database state. SQLite may create or
  update sidecars even though the database connection uses `mode=ro`. Do not use
  `immutable=1` as a workaround for a live WAL: that can omit committed data.
- An open writer is supported when SQLite can acquire the required locks. A
  backup represents a consistent committed state selected by SQLite during the
  operation; it is not a guarantee to capture the precise instant of invocation.
  Uncommitted transactions are excluded. A continuously busy writer can prevent
  completion before the deadline.
- A temporary file is created in the destination's parent with mode `0600`.
  SQLite backs up into it; the destination is finished in DELETE journal mode,
  closed, checked with full `integrity_check`, hashed, and flushed. The published
  snapshot is a standalone database file with mode `0600`.
- Publication uses `os.link`, which atomically fails if a destination entry
  exists. Regular files, directories, hard links, live symlinks, broken symlinks,
  and a file created by a competing process are all preserved. Supplying the
  source as the destination also fails. There is no overwrite or copy fallback.
- Temporary files and their SQLite sidecars are removed after success and on
  ordinary failures. Cleanup is best effort if directory access is revoked or
  the process is terminated; a crash or forced kill can leave a hidden staging
  file. Failure before publication leaves no new snapshot under normal directory
  permissions. A failure to remove a staging link after successful publication
  does not invalidate the published snapshot.
- Use a trusted local destination directory. The tool does not defend against
  an adversary replacing directory entries or a database while it runs. Network
  filesystems may have different SQLite locking or hard-link guarantees. This
  package does not guarantee recovery across power loss or a filesystem fault.
- `verify_snapshot` is intended for a stable snapshot file. Prevent concurrent
  writes while verifying so that the metadata, integrity result, and byte hash
  describe the same file state. Verification of a live WAL database can inspect
  committed sidecar data, while `sha256` and `size_bytes` cover its main file
  only. `create_snapshot` avoids this mismatch by verifying its closed staging
  database before publication.

There is no backup retention, scheduling, encryption, upload, database discovery,
or automatic restore. Integrity checking verifies SQLite structure and index
consistency; it does not prove that application data is correct. Preserve and
manage snapshots according to your own recovery requirements.

## Development

```sh
python -m unittest discover -s tests -v
```

The suite checks committed live-WAL data, exclusion of uncommitted rows, index
inconsistency that `quick_check` misses, missing/corrupt files, destination races
and all common existing path types, cleanup, permissions, byte hashes, deadline
failure, and CLI output. CI runs on Python 3.10, 3.12, and 3.14 with read-only
repository permissions and checkout credentials disabled. POSIX creation tests
are skipped on other platforms; this is not Windows snapshot support.

Original code, licensed under MIT. Copyright 2026 Martin Lofrano de Oliveira.
