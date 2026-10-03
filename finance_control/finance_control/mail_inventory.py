"""Offline acquisition-to-review adapter. Private output, no ledger mutations."""
import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import date
from email import policy
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime

from .import_preview import outside_repository
from .mail_preview import MAX_EML_BYTES, SUPPLIER_COMMIT, MailPreviewError, inspect_raw


def _message_date(raw):
    """Return the RFC822 message date without parsing attachments or body text."""
    try:
        value = BytesHeaderParser(policy=policy.default).parsebytes(raw, headersonly=True).get('Date')
        parsed = parsedate_to_datetime(value) if value else None
    except Exception:  # noqa: BLE001 - malformed untrusted headers mean unknown date.
        return None
    if parsed is None:
        return None
    return parsed.date()


def build_inventory(database, output_directory, *, from_date=None, to_date=None):
    """Read a consistent snapshot and create a new, never-overwritten review directory.

    items.jsonl contains private source references and unconfirmed values.
    summary.json is written last; its absence means the run was interrupted.
    """
    if isinstance(from_date, str):
        from_date = date.fromisoformat(from_date)
    if isinstance(to_date, str):
        to_date = date.fromisoformat(to_date)
    if from_date and to_date and from_date > to_date:
        raise ValueError('invalid_date_range')
    source = outside_repository(database)
    output = outside_repository(output_directory)
    connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        states = dict(connection.execute('SELECT status, COUNT(*) FROM mail_sync_v1 GROUP BY status'))
        rows = connection.execute('''SELECT account, folder, uidvalidity, uid, raw_sha256,
            CASE WHEN length(raw) <= ? THEN raw ELSE NULL END
            FROM mail_sync_v1 WHERE status='stored'
            ORDER BY account, folder, uidvalidity, uid''', (MAX_EML_BYTES,))
        output.mkdir(parents=True, exist_ok=False)
        counts = Counter()
        seen = set()
        with (output / 'items.jsonl').open('x', encoding='utf-8') as stream:
            for account, folder, generation, uid, digest, raw in rows:
                item = {'reference': {'account': account, 'folder': folder,
                                      'uidvalidity': generation, 'uid': uid},
                        'source_sha256': digest}
                counts['stored_references'] += 1
                if raw is None or hashlib.sha256(raw).hexdigest() != digest:
                    item['status'] = 'invalid_or_oversized_source'
                    counts['failed'] += 1
                    stream.write(json.dumps(item, ensure_ascii=False) + '\n')
                    continue
                if from_date or to_date:
                    message_date = _message_date(raw)
                    if message_date is None:
                        counts['date_unavailable_references'] += 1
                        continue
                    if ((from_date and message_date < from_date)
                            or (to_date and message_date > to_date)):
                        counts['outside_date_range_references'] += 1
                        continue
                    counts['in_date_range_references'] += 1
                    item['message_date'] = message_date.isoformat()
                if digest in seen:
                    item['status'] = 'identical_raw_already_inspected'
                    counts['identical_raw_references'] += 1
                else:
                    try:
                        inspection = inspect_raw(raw, include_candidates=True)
                    except MailPreviewError:
                        item['status'] = 'inspection_failed'
                        counts['failed'] += 1
                    else:
                        seen.add(digest)
                        item.update(status='inspected', inspection=inspection)
                        counts['unique_messages_inspected'] += 1
                        counts['invoice_candidates'] += inspection['invoice_candidates']
                        counts['document_extraction_incomplete'] += sum(
                            d.get('kind') == 'attachment'
                            and d.get('status') not in {'unsupported_text_encoding', 'unsupported'}
                            for d in inspection['documents'])
                        counts['encoding_pending'] += sum(
                            d['status'] == 'unsupported_text_encoding' for d in inspection['documents'])
                        counts['messages_with_html'] += inspection['html_present']
                stream.write(json.dumps(item, ensure_ascii=False) + '\n')
        summary = {
            'schema_version': 'mail_review_inventory_v1',
            'status': 'needs_attention' if counts['failed'] else 'needs_review',
            'scan_complete': True, 'counts': dict(counts), 'source_states': states,
            'legacy_review_pending': states.get('legacy_review', 0),
            'supplier_expected_commit': SUPPLIER_COMMIT,
            'mailbox_accessed': False, 'ledger_written': False,
            'date_range': {
                'from': from_date.isoformat() if from_date else None,
                'to': to_date.isoformat() if to_date else None,
            },
            'limitations': (
                ['legacy_matches_unresolved', 'html_not_rendered',
                 'candidate_values_not_approved']
                + (['document_extraction_incomplete']
                   if counts['document_extraction_incomplete'] else [])
            ),
        }
        with (output / 'summary.json').open('x', encoding='utf-8') as stream:
            json.dump(summary, stream, indent=2)
        return summary
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description='Lokale Rechnungskandidaten-Prüfliste ohne Postfachzugriff')
    parser.add_argument('--database', required=True)
    parser.add_argument('--output-directory', required=True)
    parser.add_argument('--from-date', type=date.fromisoformat)
    parser.add_argument('--to-date', type=date.fromisoformat)
    args = parser.parse_args()
    try:
        report = build_inventory(
            args.database, args.output_directory,
            from_date=args.from_date, to_date=args.to_date,
        )
    except Exception:  # noqa: BLE001 - CLI must not expose private parser or SQLite details.
        parser.exit(2, 'Prüfliste nicht abgeschlossen. Quelldaten unverändert; Details bleiben lokal.\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
