"""Run with `python -m examples.demo` from the checkout; uses synthetic data only."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile

from sqlite_snapshot_kit import create_snapshot, verify_snapshot


def main():
    with tempfile.TemporaryDirectory(prefix="sqlite-snapshot-demo-") as directory:
        source = Path(directory) / "synthetic.db"
        destination = Path(directory) / "synthetic-snapshot.db"
        with closing(sqlite3.connect(source)) as database, database:
            database.execute("CREATE TABLE fruit (name TEXT)")
            database.executemany(
                "INSERT INTO fruit VALUES (?)", [("apple",), ("pear",), ("plum",)]
            )
        report = create_snapshot(source, destination)
        assert verify_snapshot(destination) == report
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
