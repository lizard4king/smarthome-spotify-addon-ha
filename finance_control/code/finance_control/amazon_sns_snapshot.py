"""Create a private SNS inbox snapshot for trusted local transport."""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from .amazon_sns_inbox import AmazonSnsInbox
from .amazon_sns import SNS_TOPIC_ARN_PATTERN as _TOPIC_ARN
from .storage_paths import outside_repository

def _is_existing_inbox(path: Path) -> bool:
    try:
        uri = path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            return connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='amazon_sns_inbox'"
            ).fetchone() is not None
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private local Amazon SNS inbox snapshot")
    parser.add_argument("--inbox-database", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output")
    action.add_argument("--merge-into")
    parser.add_argument("--topic-arn")
    args = parser.parse_args(argv)
    try:
        source = outside_repository(Path(args.inbox_database))
        if (source.suffix.lower() != ".sqlite" or source.name.lower() == "finance.sqlite"
                or not source.is_file() or not _is_existing_inbox(source)):
            raise ValueError
        if args.merge_into:
            if not args.topic_arn or not _TOPIC_ARN.fullmatch(args.topic_arn):
                raise ValueError
            destination = outside_repository(Path(args.merge_into))
            if (destination.suffix.lower() != ".sqlite" or destination.name.lower() == "finance.sqlite"
                    or not destination.is_file() or not _is_existing_inbox(destination)):
                raise ValueError
            with AmazonSnsInbox(destination) as inbox:
                inbox.merge_pending_from(source, args.topic_arn)
        else:
            if args.topic_arn is not None:
                raise ValueError
            with AmazonSnsInbox(source) as inbox:
                inbox.snapshot(Path(args.output))
    except Exception:
        parser.exit(2, "Lokale SNS-Sicherung fehlgeschlagen; Eingaben und Ziel prüfen.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
