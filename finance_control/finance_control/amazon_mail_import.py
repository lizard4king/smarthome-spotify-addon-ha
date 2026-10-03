"""Offline import of verified Amazon order confirmations from the local mail archive."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from .classification import (
    confirm_document,
    link_document,
    refresh_document_candidate,
    register_document,
)
from .core import Store, money
from .document_intake import cache_document_source
from .import_preview import outside_repository
from .mail_preview import MAX_EML_BYTES

_ORDER = re.compile(r'(?<!\d)(\d{3}-\d{7}-\d{7})(?!\d)')
_TOTAL = re.compile(
    r'(?i)(?<![A-Za-zÄÖÜäöüß])(?:gesamtsumme|summe)\s*:?\s*(?:EUR\s*)?'
    r'(\d{1,6}(?:[. ]\d{3})*,\d{2}|\d{1,6}[.]\d{2})\s*(?:EUR|€|�)')
_SUBJECT = re.compile(r'^(?:bestellt:|ihre bestellung bei amazon[.]de)', re.IGNORECASE)
_INVISIBLES = dict.fromkeys(map(ord, '\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff'))


def _clean(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').translate(_INVISIBLES).split())


def _plain_text(message):
    parts = []
    candidates = message.walk() if message.is_multipart() else (message,)
    for part in candidates:
        if (part.get_content_maintype(), part.get_content_subtype(), part.get_content_disposition()) == ('text', 'plain', None):
            try:
                parts.append(part.get_content())
            except (LookupError, UnicodeError):
                continue
    return '\n'.join(parts)


def parse_order_confirmation(raw):
    """Return a bounded Amazon order record or ``None`` for unsupported mail."""
    record, _ = _parse_order_confirmation(raw)
    return record


def _parse_order_confirmation(raw):
    """Return a record plus a stable, content-free coverage reason."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_EML_BYTES:
        return None, 'invalid_or_oversized_source'
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        sender = parseaddr(str(message.get('From') or ''))[1].rsplit('@', 1)[-1].casefold()
        subject = _clean(str(message.get('Subject') or ''))
        sent = parsedate_to_datetime(str(message.get('Date') or ''))
        authentication_headers = message.get_all('Authentication-Results', [])
    except Exception:  # noqa: BLE001 - malformed untrusted mail has no further safe detail.
        return None, 'malformed_message'
    authentication = (str(authentication_headers[0]).casefold()
                      if authentication_headers else '')
    trusted_receiver = authentication.lstrip().startswith('mx.google.com;')
    amazon_dkim = re.search(
        r'dkim=pass(?:(?!;).)*header[.]i=@amazon[.](?:de|com)(?:\s|;)',
        authentication) is not None
    amazon_dmarc = re.search(
        r'dmarc=pass(?:(?!;).)*header[.]from=amazon[.](?:de|com)(?:\s|;|$)',
        authentication) is not None
    authenticated = trusted_receiver and amazon_dkim and amazon_dmarc
    amazon_sender = sender in {'amazon.de', 'amazon.com'} or sender.endswith(
        ('.amazon.de', '.amazon.com'))
    if not amazon_sender:
        return None, 'not_amazon_sender'
    if not authenticated:
        return None, 'not_authenticated'
    if not _SUBJECT.search(subject):
        return None, 'subject_not_supported'
    if sent is None:
        return None, 'missing_date'
    body = _plain_text(message)
    orders, totals = set(_ORDER.findall(body)), _TOTAL.findall(body)
    normalized_totals = {
        value.replace(' ', '').replace('.', '').replace(',', '.') if ',' in value
        else value.replace(' ', '') for value in totals}
    if not orders:
        return None, 'missing_order_number'
    if len(orders) != 1:
        return None, 'ambiguous_order_number'
    if not normalized_totals:
        return None, 'missing_total'
    if len(normalized_totals) != 1:
        return None, 'ambiguous_total'
    try:
        amount = money(normalized_totals.pop())
    except ValueError:
        return None, 'invalid_total'
    if amount <= 0:
        return None, 'nonpositive_total'
    order_number = orders.pop()
    return ({'order_number': order_number, 'date': sent.date().isoformat(),
             'amount': format(amount, '.2f'),
             'title': f'Amazon-Bestellbestätigung {order_number}',
             'source_text': _clean(body)[:200_000]}, 'eligible_order_confirmation')


def _mail_rows(path, from_date, coverage):
    source = outside_repository(path)
    connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        folder_where = "lower(folder)='inbox/amazon'"
        source_rows, stored_sources = connection.execute(
            "SELECT COUNT(*),SUM(CASE WHEN status='stored' THEN 1 ELSE 0 END) "
            "FROM mail_sync_v1 WHERE " + folder_where
        ).fetchone()
        coverage['source_rows'] += source_rows
        coverage['stored_sources'] += stored_sources or 0
        coverage['nonstored_sources'] += source_rows - (stored_sources or 0)
        source_where = "status='stored' AND " + folder_where
        unavailable_sources = connection.execute(
            "SELECT SUM(CASE WHEN raw IS NULL OR length(raw)=0 OR length(raw)>? "
            "THEN 1 ELSE 0 END) FROM mail_sync_v1 WHERE " + source_where,
            (MAX_EML_BYTES,)).fetchone()[0]
        if unavailable_sources:
            coverage['invalid_or_oversized_source'] += unavailable_sources
        rows = connection.execute(
            "SELECT raw_sha256,raw FROM mail_sync_v1 WHERE " + source_where
            + " AND raw IS NOT NULL AND length(raw) BETWEEN 1 AND ? ORDER BY uidvalidity,uid",
            (MAX_EML_BYTES,))
        seen = set()
        for digest, raw in rows:
            if hashlib.sha256(raw).hexdigest() != digest:
                coverage['source_hash_mismatch'] += 1
                continue
            if digest in seen:
                coverage['duplicate_source'] += 1
                continue
            seen.add(digest)
            record, reason = _parse_order_confirmation(raw)
            if record is None:
                coverage[reason] += 1
                continue
            if date.fromisoformat(record['date']) < from_date:
                coverage['before_from_date'] += 1
                continue
            coverage[reason] += 1
            yield digest, record
    finally:
        connection.close()


def _existing_document(store, digest):
    return store.db.execute(
        "SELECT DISTINCT d.* FROM classification_documents d LEFT JOIN classification_document_occurrences o ON o.document_id=d.id WHERE d.source_reference=? OR d.source_reference LIKE ? OR o.source_reference LIKE ? ORDER BY d.id LIMIT 1",
        (f'amazon-mail:{digest}', f'mail:{digest}:%', f'mail:{digest}:%')).fetchone()


def _order_already_imported(store, order_number, current_id=None):
    return store.db.execute(
        'SELECT 1 FROM amazon_order_documents WHERE order_number=? '
        'AND (? IS NULL OR document_id!=?) LIMIT 1',
        (order_number, current_id, current_id)).fetchone() is not None


def _remember_order(store, order_number, document_id):
    with store.db:
        store.db.execute(
            'INSERT OR IGNORE INTO amazon_order_documents(order_number,document_id) VALUES (?,?)',
            (order_number, document_id))


def _transaction_candidates(store, record):
    start = record['date']
    end = (date.fromisoformat(start) + timedelta(days=7)).isoformat()
    return [dict(row) for row in store.db.execute(
        "SELECT t.account_id,t.external_id FROM transactions t LEFT JOIN transaction_context c ON c.account_id=t.account_id AND c.external_id=t.external_id WHERE t.date BETWEEN ? AND ? AND t.amount=? AND lower(COALESCE(c.counterparty,'') || ' ' || COALESCE(c.description,'')) LIKE '%amazon%' AND NOT EXISTS (SELECT 1 FROM classification_document_links l JOIN classification_documents d ON d.id=l.document_id WHERE l.account_id=t.account_id AND l.external_id=t.external_id AND d.kind='invoice')",
        (start, end, '-' + record['amount']))]


def _coverage_report(coverage):
    """Derive the compatible import summary from one aggregate-only counter."""
    counts = dict(sorted(coverage.items()))
    stored_source_scan_complete = (
        counts.get('source_rows', 0) > 0
        and counts.get('nonstored_sources', 0) == 0
        and counts.get('invalid_or_oversized_source', 0) == 0
        and counts.get('source_hash_mismatch', 0) == 0
        and counts.get('malformed_message', 0) == 0
    )
    return {
        'checked': counts.get('eligible_order_confirmation', 0),
        'imported': counts.get('imported', 0),
        'confirmed': counts.get('confirmed', 0),
        'linked': counts.get('linked', 0),
        'skipped': counts.get('already_present', 0),
        'updated': counts.get('updated', 0),
        'ambiguous': counts.get('ambiguous_payment_match', 0),
        'unmatched': counts.get('unmatched_payment', 0),
        'failed': counts.get('link_failed', 0),
        'coverage': {
            'schema_version': 'amazon_mail_coverage_v2',
            'stored_source_scan_complete': stored_source_scan_complete,
            'mailbox_completeness': 'not_evaluated',
            'counts': counts,
        },
    }


def import_amazon_orders(store, database, mail_database, *, from_date='2026-01-01'):
    """Import order evidence and auto-link only bidirectionally unique exact matches."""
    since = date.fromisoformat(from_date) if isinstance(from_date, str) else from_date
    records, order_counts, coverage = [], Counter(), Counter()
    for digest, record in _mail_rows(mail_database, since, coverage):
        existing = _existing_document(store, digest)
        current_id = None if existing is None else existing['id']
        linked = existing is not None and store.db.execute(
            'SELECT 1 FROM classification_document_links WHERE document_id=?',
            (current_id,)).fetchone() is not None
        dismissed = existing is not None and '"not_invoice_like"' in existing['warnings']
        if existing is not None:
            _remember_order(store, record['order_number'], current_id)
        if (dismissed or (existing is not None and existing['status'] == 'confirmed' and linked)
                or _order_already_imported(store, record['order_number'], current_id)):
            coverage['already_present'] += 1
            continue
        records.append((digest, record, existing))
        order_counts[record['order_number']] += 1
    candidates, reverse = {}, defaultdict(list)
    for digest, record, _ in records:
        matches = _transaction_candidates(store, record)
        candidates[digest] = matches
        for match in matches:
            reverse[(match['account_id'], match['external_id'])].append(digest)
    for digest, record, existing in records:
        data = {'kind': 'invoice', 'vendor': 'Amazon.de', 'title': record['title'],
                'document_date': record['date'], 'amount': record['amount'], 'currency': 'EUR',
                'source_reference': f'amazon-mail:{digest}',
                'warnings': ['amazon_order_confirmation_not_tax_invoice'],
                'status': 'unreviewed'}
        if existing is None:
            document = register_document(store, data)['document']
            _remember_order(store, record['order_number'], document['id'])
            cache_document_source(database, document['id'], record['source_text'])
            coverage['imported'] += 1
        elif existing['status'] == 'unreviewed':
            data['source_reference'] = existing['source_reference']
            document = refresh_document_candidate(store, {
                'id': existing['id'], 'revision': existing['revision'], 'confirmed': True,
                **data})['document']
            coverage['updated'] += 1
        else:
            document = confirm_document(store, {
                'id': existing['id'], 'revision': existing['revision'], 'confirmed': True,
                'vendor': data['vendor'], 'title': data['title'],
                'document_date': data['document_date'], 'amount': data['amount'],
                'currency': data['currency'], 'source_reference': existing['source_reference'],
                'status': 'confirmed'})['document']
            coverage['updated'] += 1
        matches = candidates[digest]
        unique = (order_counts[record['order_number']] == 1 and len(matches) == 1
                  and len(reverse[(matches[0]['account_id'], matches[0]['external_id'])]) == 1)
        if not unique:
            coverage['ambiguous_payment_match' if matches else 'unmatched_payment'] += 1
            continue
        match = matches[0]
        # Recheck immediately before the two audited writes. Concurrent changes
        # leave the evidence visible for review instead of aborting the batch.
        if _transaction_candidates(store, record) != matches:
            coverage['ambiguous_payment_match'] += 1
            continue
        try:
            confirmed = document
            if document['status'] == 'unreviewed':
                confirmed = confirm_document(store, {
                    'id': document['id'], 'revision': document['revision'], 'confirmed': True,
                    'vendor': document['vendor'], 'title': document['title'],
                    'document_date': document['document_date'], 'amount': document['amount'],
                    'currency': document['currency'], 'source_reference': document['source_reference'],
                    'status': 'confirmed'})['document']
            link_document(store, {'account_id': match['account_id'], 'external_id': match['external_id'],
                                  'document_id': confirmed['id'], 'confirmed': True})
        except ValueError:
            coverage['link_failed'] += 1
            continue
        coverage['confirmed'] += document['status'] == 'unreviewed'
        coverage['linked'] += 1
    return _coverage_report(coverage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--mail-database', required=True)
    parser.add_argument('--from-date', default='2026-01-01')
    args = parser.parse_args(argv)
    store = Store(args.database)
    try:
        print(json.dumps(
            import_amazon_orders(store, args.database, args.mail_database, from_date=args.from_date),
            ensure_ascii=False, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
