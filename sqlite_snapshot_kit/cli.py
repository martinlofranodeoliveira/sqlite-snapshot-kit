"""Small JSON command-line interface with path-free failure diagnostics."""

import argparse
import json
import sys

from .core import SnapshotError, create_snapshot, verify_snapshot


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SnapshotError("snapshot operation failed")


def main(argv=None) -> int:
    try:
        parser = _Parser(description="Create or verify a local SQLite snapshot.")
        commands = parser.add_subparsers(dest="command", required=True)
        create = commands.add_parser("create", help="create a new snapshot file")
        create.add_argument("source")
        create.add_argument("destination")
        create.add_argument("--timeout", type=float, default=5.0)
        verify = commands.add_parser("verify", help="verify a stable snapshot file")
        verify.add_argument("path")
        verify.add_argument("--timeout", type=float, default=5.0)
        arguments = parser.parse_args(argv)
        if arguments.command == "create":
            report = create_snapshot(
                arguments.source, arguments.destination, timeout=arguments.timeout
            )
        else:
            report = verify_snapshot(arguments.path, timeout=arguments.timeout)
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
        return 0
    except (SnapshotError, OSError):
        sys.stderr.write("sqlite-snapshot-kit: operation failed\n")
        return 2
