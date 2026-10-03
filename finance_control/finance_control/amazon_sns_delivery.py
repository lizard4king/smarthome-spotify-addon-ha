"""Local handoff of authenticated SNS inbox events to Amazon query status only."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3

from .amazon_portability_sync import AmazonPortabilitySync, AmazonPortabilitySyncError
from .amazon_sns_inbox import AmazonSnsInbox, InboxError

_TOPIC_ARN = re.compile(r'^arn:aws(?:-cn|-us-gov)?:sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_-]{1,256}$')


def _validate_configuration(topic: str, limit: int) -> None:
    if not isinstance(topic, str) or not _TOPIC_ARN.fullmatch(topic):
        raise ValueError('invalid_topic')
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid_limit')


def apply_notifications(inbox, store, *, expected_topic_arn: str, limit: int = 100) -> dict:
    """Drain a bounded batch; acknowledge only after the local query update commits.

    The inbox was authenticated at receipt. Delayed local processing must not
    reinterpret the original signature age as an invalid delivery. Unknown or
    conflicting queries stay pending. A crash after commit is safe to retry.
    No record downloads, credential lookups or financial imports take place.
    """
    _validate_configuration(expected_topic_arn, limit)
    # This service uses only the existing query-status transition, never its API.
    sync = AmazonPortabilitySync(store, None)
    processed = failed = 0
    for envelope in inbox.pending_notifications(limit=limit):
        if envelope.topic_arn != expected_topic_arn:
            failed += 1
            continue
        try:
            sync._record_verified_notification(envelope)
            inbox.mark_processed(envelope.message_id, envelope.topic_arn)
        except (AmazonPortabilitySyncError, InboxError, sqlite3.Error):
            failed += 1
        else:
            processed += 1
    return {'processed': processed, 'failed': failed,
            'remaining': inbox.counts()['pending_notifications']}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Amazon SNS: lokale Statusübernahme')
    parser.add_argument('--inbox-database', required=True)
    parser.add_argument('--data-directory', required=True)
    parser.add_argument('--topic-arn', required=True)
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args(argv)
    try:
        _validate_configuration(args.topic_arn, args.limit)
        from .core import Store
        from .import_preview import outside_repository
        from .read_api_server import _default_database

        database = outside_repository(_default_database(args.data_directory))
        with AmazonSnsInbox(args.inbox_database) as inbox:
            store = Store(database)
            try:
                result = apply_notifications(inbox, store, expected_topic_arn=args.topic_arn,
                                             limit=args.limit)
            finally:
                store.close()
        print(json.dumps(result, sort_keys=True))
    except (InboxError, ValueError, OSError, sqlite3.Error):
        parser.exit(2, 'Lokale SNS-Statusübernahme fehlgeschlagen; Konfiguration prüfen.\n')
    return 0 if result['failed'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
