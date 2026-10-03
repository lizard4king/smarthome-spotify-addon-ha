"""Offline, resumable import of mail-inventory candidates into document intake."""
import argparse
import hashlib
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from .classification import refresh_document_candidate, register_document
from .core import Store, money
from .document_intake import MAX_TEXT_CHARS, cache_document_source
from .import_preview import outside_repository
from .invoice_import import InvoiceContractError, parse_invoice_candidate
from .mail_booking_filter import booking_match_statuses

MAX_ITEM_BYTES = 16 * 1024 * 1024
MAX_SOURCE_TEXT_BYTES = 1_000_000
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_SAFE_DOCUMENT_WARNINGS = {'ocr_output_unreviewed'}


class MailInventoryBridgeError(ValueError):
    """Safe import error that never includes private candidate values."""


def _load_summary(directory):
    try:
        summary = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as error:
        raise MailInventoryBridgeError('invalid_or_incomplete_inventory') from error
    if (not isinstance(summary, dict)
            or summary.get('schema_version') != 'mail_review_inventory_v1'
            or summary.get('scan_complete') is not True):
        raise MailInventoryBridgeError('invalid_or_incomplete_inventory')


def _candidate_document(item, document):
    if (not isinstance(item, dict) or item.get('status') != 'inspected'
            or not isinstance(document, dict) or document.get('status') != 'invoice_candidate'):
        return None
    source_hash = item.get('source_sha256')
    document_hash = document.get('sha256')
    if (not isinstance(source_hash, str) or not _HASH.fullmatch(source_hash)
            or not isinstance(document_hash, str) or not _HASH.fullmatch(document_hash)):
        raise MailInventoryBridgeError('invalid_source_hash')
    try:
        candidate = parse_invoice_candidate(document.get('candidate'))
    except InvoiceContractError as error:
        raise MailInventoryBridgeError('invalid_candidate_contract') from error
    if candidate.document_hash != document_hash:
        raise MailInventoryBridgeError('candidate_document_hash_mismatch')

    source_text = document.get('source_text')
    if (source_text is not None
            and (not isinstance(source_text, str) or not source_text.strip()
                 or len(source_text) > MAX_TEXT_CHARS
                 or len(source_text.encode('utf-8')) > MAX_SOURCE_TEXT_BYTES
                 or hashlib.sha256(source_text.encode('utf-8')).hexdigest() != candidate.text_hash)):
        raise MailInventoryBridgeError('candidate_source_text_mismatch')

    values = candidate.values
    warnings = set(candidate.warnings)
    document_warnings = document.get('warnings', [])
    if not isinstance(document_warnings, list) or not all(
            isinstance(value, str) for value in document_warnings):
        raise MailInventoryBridgeError('invalid_document_warnings')
    warnings.update(set(document_warnings) & _SAFE_DOCUMENT_WARNINGS)
    if set(document_warnings) - _SAFE_DOCUMENT_WARNINGS:
        warnings.add('document_extraction_warning')
    amount = values['gross_total']
    currency = values['currency']
    if amount is None or currency != 'EUR' or money(amount) <= 0:
        if amount is not None and money(amount) <= 0:
            warnings.add('nonpositive_gross_total')
        amount, currency = None, None
    if source_text is None:
        warnings.add('source_text_not_in_inventory')
    document_date = None if values['invoice_date'] is None else values['invoice_date'].isoformat()
    if document_date is None and item.get('message_date') is not None:
        try:
            document_date = date.fromisoformat(item['message_date']).isoformat()
        except (TypeError, ValueError):
            raise MailInventoryBridgeError('invalid_message_date') from None
        warnings.add('date_from_mail_header')
    return {
        'kind': 'invoice',
        'vendor': str(values['vendor_name'] or 'Unbekannter Anbieter')[:240],
        'title': str(values['invoice_number'] or 'Rechnung aus Mail-Inventar')[:240],
        'document_date': document_date,
        'amount': None if amount is None else format(amount, '.2f'),
        'currency': currency,
        'source_reference': f'mail-document:{document_hash}',
        'warnings': sorted(warnings),
        'status': 'unreviewed',
    }, source_text


def _enrich_candidate(store, document_id, data):
    """Fill missing fields on the same unreviewed, unlinked canonical document."""
    row = store.db.execute(
        'SELECT * FROM classification_documents WHERE id=?', (document_id,)).fetchone()
    if row is None or row['status'] != 'unreviewed' or store.db.execute(
            'SELECT 1 FROM classification_document_links WHERE document_id=?',
            (document_id,)).fetchone():
        return False
    merged = {
        'vendor': row['vendor'], 'title': row['title'],
        'document_date': row['document_date'] or data['document_date'],
        'amount': row['amount'] or data['amount'],
        'currency': row['currency'] or data['currency'],
        'warnings': sorted(set(json.loads(row['warnings'])) | set(data['warnings'])),
    }
    if (merged['document_date'] == row['document_date']
            and merged['amount'] == row['amount']
            and merged['currency'] == row['currency']
            and merged['warnings'] == json.loads(row['warnings'])):
        return False
    refresh_document_candidate(store, {
        'id': row['id'], 'revision': row['revision'], 'confirmed': True,
        'kind': row['kind'], 'vendor': merged['vendor'], 'title': merged['title'],
        'document_date': merged['document_date'], 'amount': merged['amount'],
        'currency': merged['currency'], 'source_reference': row['source_reference'],
        'warnings': merged['warnings'], 'status': 'unreviewed',
    })
    return True


def _store_database_path(store):
    """Return the main SQLite file; in-memory stores intentionally have no cache."""
    row = store.db.execute("PRAGMA database_list").fetchone()
    if row is None or not row['file']:
        return None
    return Path(row['file'])


def _record_occurrence(store, document_id, source_hash, document_hash):
    """Attach one mail occurrence to a canonical byte-identical document."""
    source_reference = f'mail:{source_hash}:{document_hash}'
    store.db.execute(
        'INSERT OR IGNORE INTO classification_document_occurrences('
        'document_id,document_sha256,source_sha256,source_reference) VALUES (?,?,?,?)',
        (document_id, document_hash, source_hash, source_reference),
    )


def _canonical_document(store, data, source_hash, document_hash):
    """Return the canonical document, adopting an exact legacy occurrence if present."""
    row = store.db.execute(
        'SELECT d.id FROM classification_document_fingerprints f '
        'JOIN classification_documents d ON d.id=f.document_id '
        'WHERE f.document_sha256=?', (document_hash,),
    ).fetchone()
    if row is not None:
        _enrich_candidate(store, row['id'], data)
        with store.db:
            _record_occurrence(store, row['id'], source_hash, document_hash)
        return row['id'], False

    legacy_reference = f'mail:{source_hash}:{document_hash}'
    row = store.db.execute(
        'SELECT id FROM classification_documents WHERE source_reference IN (?,?) ORDER BY id LIMIT 1',
        (data['source_reference'], legacy_reference),
    ).fetchone()
    created = row is None
    if created:
        row = register_document(store, data)['document']

    try:
        with store.db:
            store.db.execute(
                'INSERT INTO classification_document_fingerprints(document_id,document_sha256) '
                'VALUES (?,?)', (row['id'], document_hash),
            )
            _record_occurrence(store, row['id'], source_hash, document_hash)
    except sqlite3.IntegrityError:
        # A concurrent/resumed importer may have registered the fingerprint first.
        canonical = store.db.execute(
            'SELECT document_id FROM classification_document_fingerprints WHERE document_sha256=?',
            (document_hash,),
        ).fetchone()
        if canonical is None:
            raise
        with store.db:
            _record_occurrence(store, canonical['document_id'], source_hash, document_hash)
        return canonical['document_id'], False
    return row['id'], created


def _import_candidate_record(store, record, report):
    data = record['data']
    source_text = record['source_text']
    source_hash = record['source_hash']
    document_hash = record['document_hash']
    occurrence_reference = f'mail:{source_hash}:{document_hash}'
    existing_occurrence = store.db.execute(
        'SELECT document_id FROM classification_document_occurrences '
        'WHERE source_reference=?', (occurrence_reference,)).fetchone()
    database_path = _store_database_path(store)
    if source_text is not None and database_path is None:
        data = dict(data)
        data['warnings'] = sorted(set(data['warnings']) | {'source_text_not_in_inventory'})
    if existing_occurrence is not None:
        _enrich_candidate(store, existing_occurrence['document_id'], data)
        if source_text is not None and database_path is not None:
            _cache_candidate_source(
                store, database_path, existing_occurrence['document_id'], source_text, report)
        report['skipped'] += 1
        return
    document_id, created = _canonical_document(
        store, data, source_hash, document_hash)
    if source_text is not None and database_path is not None:
        _cache_candidate_source(store, database_path, document_id, source_text, report)
    report['occurrences_imported'] += 1
    if created:
        report['imported'] += 1
    else:
        report['canonical_reused'] += 1


def _cache_candidate_source(store, database_path, document_id, source_text, report):
    """Preserve an existing cache conflict and make it ineligible for automation."""
    from .document_intake import DocumentIntakeError
    try:
        cache_document_source(database_path, document_id, source_text)
    except (DocumentIntakeError, OSError):
        report['cache_conflicts'] += 1
        _refresh_warning_set(store, document_id, add={'source_cache_conflict'})
        return
    _refresh_warning_set(
        store, document_id,
        remove={'source_text_not_in_inventory', 'source_cache_conflict'},
    )


def _refresh_warning_set(store, document_id, *, add=frozenset(), remove=frozenset()):
    """Update warnings on an editable candidate while retaining its audit trail."""
    row = store.db.execute(
        'SELECT * FROM classification_documents WHERE id=?', (document_id,)).fetchone()
    if row is None or row['status'] != 'unreviewed':
        return
    warnings = (set(json.loads(row['warnings'])) | set(add)) - set(remove)
    if warnings == set(json.loads(row['warnings'])):
        return
    try:
        refresh_document_candidate(store, {
            'id': row['id'], 'revision': row['revision'], 'confirmed': True,
            'kind': row['kind'], 'vendor': row['vendor'], 'title': row['title'],
            'document_date': row['document_date'], 'amount': row['amount'],
            'currency': row['currency'], 'source_reference': row['source_reference'],
            'warnings': sorted(warnings), 'status': 'unreviewed',
        })
    except ValueError:
        # A concurrently confirmed/linked document no longer needs importer edits.
        return


def _pending_groups(records):
    """Group exact documents and equivalent representations within one mail."""
    parents = list(range(len(records)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parents[right] = left

    by_document, by_representation = {}, {}
    for index, record in enumerate(records):
        data = record['data']
        normalized_text = ' '.join((record.get('source_text') or '').casefold().split())
        representation = None if not normalized_text else (
            record['source_hash'], data.get('amount'), data.get('currency'),
            data.get('document_date'), ' '.join(data.get('vendor', '').casefold().split()),
            hashlib.sha256(normalized_text.encode('utf-8')).hexdigest(),
        )
        mappings = [(by_document, record['document_hash'])]
        if representation is not None:
            mappings.append((by_representation, representation))
        for mapping, key in mappings:
            previous = mapping.setdefault(key, index)
            union(index, previous)
    groups = {}
    for index, record in enumerate(records):
        groups.setdefault(find(index), []).append(record)
    return list(groups.values())


def _preferred_record(group):
    kind_rank = {'mail_body': 0, 'text_attachment': 1, 'document_attachment': 2}
    return max(group, key=lambda record: (
        record['source_text'] is not None,
        kind_rank.get(record['document'].get('kind'), -1),
        (record['document'].get('extraction') or {}).get('method') == 'pymupdf',
        -record['line'], -record['candidate'],
    ))


def import_inventory_snapshot(store, inventory_directory, *, booking_driven=False, max_days=45):
    """Import validated invoice candidates; each candidate is independently resumable."""
    if type(booking_driven) is not bool:
        raise MailInventoryBridgeError('invalid_booking_filter')
    if type(max_days) is not int or not 0 <= max_days <= 45:
        raise MailInventoryBridgeError('invalid_booking_match_days')
    directory = outside_repository(Path(inventory_directory))
    if not directory.is_dir():
        raise MailInventoryBridgeError('inventory_directory_not_found')
    _load_summary(directory)
    items_path = outside_repository(directory / 'items.jsonl')
    if not items_path.is_relative_to(directory) or not items_path.is_file():
        raise MailInventoryBridgeError('inventory_items_not_found')

    report = {
        'schema_version': 'mail_inventory_import_v1',
        'scan_complete': True,
        'records': 0,
        'candidates': 0,
        'imported': 0,
        'skipped': 0,
        'canonical_reused': 0,
        'occurrences_imported': 0,
        'booking_filter_enabled': booking_driven,
        'booking_max_days': max_days,
        'booking_unique': 0,
        'booking_unmatched': 0,
        'booking_ambiguous': 0,
        'booking_representations_collapsed': 0,
        'booking_filtered': 0,
        'cache_conflicts': 0,
        'failed': 0,
        'errors': [],
        'documents_confirmed': 0,
        'bookings_created': 0,
        'links_created': 0,
        'mailbox_accessed': False,
    }
    pending = []
    try:
        stream = items_path.open('rb')
    except OSError as error:
        raise MailInventoryBridgeError('inventory_items_not_readable') from error
    with stream:
        for line_number, raw_line in enumerate(stream, 1):
            if not raw_line.strip():
                continue
            report['records'] += 1
            if len(raw_line) > MAX_ITEM_BYTES:
                report['failed'] += 1
                report['errors'].append({'line': line_number, 'candidate': None,
                                         'code': 'inventory_item_too_large'})
                continue
            try:
                item = json.loads(raw_line)
            except (UnicodeError, ValueError, RecursionError):
                report['failed'] += 1
                report['errors'].append({'line': line_number, 'candidate': None,
                                         'code': 'invalid_inventory_item'})
                continue
            if not isinstance(item, dict):
                report['failed'] += 1
                report['errors'].append({'line': line_number, 'candidate': None,
                                         'code': 'invalid_inventory_item'})
                continue
            if item.get('status') != 'inspected':
                continue
            inspection = item.get('inspection')
            if not isinstance(inspection, dict):
                report['failed'] += 1
                report['errors'].append({'line': line_number, 'candidate': None,
                                         'code': 'invalid_inspection'})
                continue
            documents = inspection.get('documents')
            if not isinstance(documents, list):
                report['failed'] += 1
                report['errors'].append({'line': line_number, 'candidate': None,
                                         'code': 'invalid_document_list'})
                continue
            for candidate_index, document in enumerate(documents):
                if not isinstance(document, dict) or document.get('status') != 'invoice_candidate':
                    continue
                report['candidates'] += 1
                try:
                    parsed = _candidate_document(item, document)
                    if parsed is None:
                        continue
                    data, source_text = parsed
                    record = {
                        'line': line_number, 'candidate': candidate_index, 'data': data,
                        'source_text': source_text, 'source_hash': item['source_sha256'],
                        'document_hash': document['sha256'], 'document': document,
                    }
                    if booking_driven:
                        pending.append(record)
                    else:
                        _import_candidate_record(store, record, report)
                except Exception as error:  # noqa: BLE001 - isolate each private candidate.
                    code = str(error)
                    if code == 'duplicate_source_reference':
                        report['skipped'] += 1
                        continue
                    if not isinstance(error, MailInventoryBridgeError):
                        code = 'candidate_import_failed'
                    report['failed'] += 1
                    report['errors'].append({'line': line_number, 'candidate': candidate_index,
                                             'code': code})
    if booking_driven:
        groups = _pending_groups(pending)
        identities = [_preferred_record(group)['document_hash'] for group in groups]
        distinct = {identity: _preferred_record(group)['data']
                    for identity, group in zip(identities, groups)}
        statuses = booking_match_statuses(store, distinct, max_days)
        for identity, group in zip(identities, groups):
            match_status = statuses[identity]['status']
            if match_status != 'unique':
                report[f'booking_{match_status}'] += 1
                report['booking_filtered'] += len(group)
                report['skipped'] += len(group)
                continue
            report['booking_unique'] += 1
            preferred = _preferred_record(group)
            selected = [record for record in group
                        if record['document_hash'] == preferred['document_hash']]
            collapsed = len(group) - len(selected)
            report['booking_representations_collapsed'] += collapsed
            report['skipped'] += collapsed
            for record in selected:
                try:
                    _import_candidate_record(store, record, report)
                except Exception as error:  # noqa: BLE001 - isolate each private candidate.
                    code = str(error)
                    if code == 'duplicate_source_reference':
                        report['skipped'] += 1
                        continue
                    if not isinstance(error, MailInventoryBridgeError):
                        code = 'candidate_import_failed'
                    report['failed'] += 1
                    report['errors'].append({
                        'line': record['line'], 'candidate': record['candidate'], 'code': code,
                    })
    report['status'] = (
        'needs_attention' if report['failed'] or report['cache_conflicts'] else 'complete')
    return report


def main():
    parser = argparse.ArgumentParser(
        description='Lokaler Import ungeprüfter Belegkandidaten aus einer Mail-Prüfliste')
    parser.add_argument('--inventory-directory', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--booking-driven', action='store_true')
    parser.add_argument('--max-days', type=int, default=45)
    args = parser.parse_args()
    store = None
    try:
        database = outside_repository(Path(args.database))
        store = Store(database)
        report = import_inventory_snapshot(
            store, args.inventory_directory, booking_driven=args.booking_driven,
            max_days=args.max_days)
    except Exception:  # noqa: BLE001 - CLI must not print private parser or SQLite details.
        parser.exit(2, 'Inventarimport nicht gestartet oder abgebrochen; Details bleiben lokal.\n')
    finally:
        if store is not None:
            store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report['failed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
