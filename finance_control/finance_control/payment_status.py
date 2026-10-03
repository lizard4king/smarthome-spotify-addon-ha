"""Read-only payment-service mail status bridge.

Raw mail remains in the separately installed invoice-mail-archive database.
Finance Control stores only structured, idempotent status events.
"""
import argparse
import hashlib
import json
import re
import sqlite3
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from .core import Store, money
from .import_preview import outside_repository
from .transfer_corrections import sql_transfer_predicate

_HASH = re.compile(r'[0-9a-f]{64}\Z')
_REFERENCE = re.compile(
    r'(?:transaktionscode|transaktions-id|transaction\s*id|referenz|reference|'
    r'bestellnummer|order\s*id)\s*[:#]?\s*([a-z0-9-]{6,64})', re.IGNORECASE)
_AMOUNT = re.compile(r'(?<!\d)([0-9][0-9 .,’\']{0,20}[0-9])\s*(?:EUR|€)', re.IGNORECASE)
_TOTAL_AMOUNT = re.compile(
    r'(?:gesamtbetrag|gesamtsumme|zahlungsbetrag|zu zahlen|total|summe)\s*[:\-]?\s*'
    r'([0-9][0-9 .,’\']{0,20}[0-9])\s*(?:EUR|€)', re.IGNORECASE)
_STATUS_PATTERNS = (
    ('payment_plan', re.compile(
        r'\b(ratenzahlung|zahlung in raten|zahlungsplan|payment plan|pay in 3|'
        r'rate fällig|rate faellig)\b',
        re.IGNORECASE)),
    ('paid', re.compile(
        r'\b(zahlung erfolgreich|erfolgreich bezahlt|payment completed|payment successful|'
        r'zahlung abgeschlossen|zahlung gesendet|zahlung erhalten|wurde bezahlt|'
        r'vollständig bezahlt|vollstaendig bezahlt)\b',
        re.IGNORECASE)),
    ('processing', re.compile(
        r'\b(wird bearbeitet|in bearbeitung|zahlung ausstehend|payment processing|'
        r'processing payment|pending payment)\b',
        re.IGNORECASE)),
    ('authorization', re.compile(
        r'\b(autorisiert|authori[sz]ed|zahlung genehmigt|payment approved|genehmigte zahlung)\b',
        re.IGNORECASE)),
)


class PaymentStatusError(ValueError):
    """Safe bridge error without raw mail values."""


def _raw_mail_row(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > 8 * 1024 * 1024:
        return None
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        sent_at = parsedate_to_datetime(message.get('Date')).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None
    body = ''
    try:
        part = message.get_body(preferencelist=('plain', 'html'))
        if part is not None:
            body = part.get_content()
    except (LookupError, TypeError, ValueError):
        return None
    return {
        'content_hash': hashlib.sha256(raw).hexdigest(), 'sent_at': sent_at,
        'sender': str(message.get('From') or ''),
        'subject': str(message.get('Subject') or ''), 'body': str(body or ''),
    }


def _decimal_text(value):
    value = value.replace(' ', '').replace("'", '').replace('’', '')
    if ',' in value and '.' in value:
        decimal_separator = ',' if value.rfind(',') > value.rfind('.') else '.'
        thousands_separator = '.' if decimal_separator == ',' else ','
        value = value.replace(thousands_separator, '').replace(decimal_separator, '.')
    elif re.fullmatch(r'\d{1,3}(?:[.]\d{3})+', value):
        value = value.replace('.', '')
    elif re.fullmatch(r'\d{1,3}(?:,\d{3})+', value):
        value = value.replace(',', '')
    elif ',' in value:
        value = value.replace('.', '').replace(',', '.')
    try:
        parsed = money(value)
    except ValueError:
        return None
    return format(parsed, '.2f') if parsed > 0 else None


def _single_amount(text):
    labeled = {_decimal_text(match.group(1)) for match in _TOTAL_AMOUNT.finditer(text)}
    labeled.discard(None)
    if len(labeled) == 1:
        return next(iter(labeled))
    if len(labeled) > 1:
        return None
    amounts = {_decimal_text(match.group(1)) for match in _AMOUNT.finditer(text)}
    amounts.discard(None)
    return next(iter(amounts)) if len(amounts) == 1 else None


def _provider(sender, subject):
    source = f'{sender or ""} {subject or ""}'.casefold()
    providers = [name for name in ('klarna', 'paypal') if name in source]
    return providers[0] if len(providers) == 1 else None


def _event_statuses(text):
    return [status for status, pattern in _STATUS_PATTERNS if pattern.search(text)]


def _events_from_row(row):
    source_key = row['content_hash']
    if not isinstance(source_key, str) or not _HASH.fullmatch(source_key):
        raise PaymentStatusError('invalid_supplier_source_key')
    event_date = str(row['sent_at'] or '')[:10]
    try:
        event_date = date.fromisoformat(event_date).isoformat()
    except ValueError:
        return []
    provider = _provider(row['sender'], row['subject'])
    text = ' '.join((row['subject'] or '', row['body'] or ''))
    statuses = _event_statuses(text)
    if provider is None or not statuses:
        return []
    reference = _REFERENCE.search(text)
    amount = _single_amount(text)
    return [{
        'source_key': source_key, 'provider': provider, 'event_status': status,
        'event_date': event_date, 'amount': amount,
        'currency': 'EUR' if amount is not None else None,
        'provider_reference': None if reference is None else reference.group(1)[:64],
    } for status in statuses]


def read_supplier_events(database, since='2026-01-01'):
    """Read structured events from the supplier archive without modifying it."""
    source = outside_repository(Path(database))
    try:
        since = date.fromisoformat(since).isoformat()
    except ValueError as error:
        raise PaymentStatusError('invalid_status_start_date') from error
    uri = f'file:{source.as_posix()}?mode=ro'
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        tables = {row['name'] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'emails' in tables:
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(emails)')}
            required = {
                'content_hash', 'sent_at', 'sender', 'subject',
                'body_text', 'body_clean_text',
            }
            if not required <= columns:
                raise PaymentStatusError('unsupported_supplier_database')
            rows = connection.execute(
                "SELECT content_hash,sent_at,sender,subject,"
                "coalesce(body_clean_text,body_text,'') AS body FROM emails "
                "WHERE substr(sent_at,1,10)>=? ORDER BY sent_at,id", (since,)).fetchall()
        elif 'mail_sync_v1' in tables:
            columns = {row['name'] for row in connection.execute(
                'PRAGMA table_info(mail_sync_v1)')}
            if not {'status', 'raw'} <= columns:
                raise PaymentStatusError('unsupported_supplier_database')
            raw_rows = connection.execute(
                "SELECT raw FROM mail_sync_v1 WHERE status='stored' AND raw IS NOT NULL "
                'ORDER BY account,folder,uidvalidity,uid').fetchall()
            rows = [parsed for row in raw_rows if (parsed := _raw_mail_row(row['raw']))]
            rows = [row for row in rows if str(row['sent_at'])[:10] >= since]
        else:
            raise PaymentStatusError('unsupported_supplier_database')
    except sqlite3.Error as error:
        raise PaymentStatusError('supplier_database_not_readable') from error
    finally:
        if 'connection' in locals():
            connection.close()
    events = []
    for row in rows:
        events.extend(_events_from_row(row))
    return events


def _booking_candidates(store, event):
    if event['amount'] is None:
        return []
    event_day = date.fromisoformat(event['event_date'])
    result = []
    transfer_sql = sql_transfer_predicate('t', 'c')
    rows = store.db.execute(
        "SELECT t.account_id,t.external_id,t.date,t.amount,"
        f"CASE WHEN {transfer_sql} THEN 1 ELSE 0 END AS is_transfer,"
        "coalesce(c.counterparty,'') AS counterparty,coalesce(c.description,'') AS description "
        "FROM transactions t LEFT JOIN transaction_context c USING(account_id,external_id) "
        "WHERE t.currency='EUR' AND t.date>=? AND t.date<=? "
        "ORDER BY t.date,t.account_id,t.external_id",
        ((event_day - timedelta(days=7)).isoformat(),
         (event_day + timedelta(days=7)).isoformat())).fetchall()
    for row in rows:
        if abs((date.fromisoformat(row['date']) - event_day).days) > 7:
            continue
        signed_amount = money(row['amount'])
        if signed_amount >= 0 and not row['is_transfer']:
            continue
        if abs(signed_amount) != money(event['amount']):
            continue
        context = f"{row['counterparty']} {row['description']}".casefold()
        provider_match = event['provider'] in context
        reference_match = (event['provider_reference'] is not None
                           and event['provider_reference'].casefold() in context)
        if provider_match or reference_match:
            result.append((row['account_id'], row['external_id']))
    return result


def import_supplier_statuses(store, supplier_database, since='2026-01-01'):
    """Idempotently import structured status events and unique booking matches."""
    events = read_supplier_events(supplier_database, since)
    report = {'seen': len(events), 'imported': 0, 'updated': 0, 'skipped': 0,
              'unique': 0, 'ambiguous': 0, 'unmatched': 0, 'conflict': 0, 'stale': 0}
    store.db.execute('BEGIN IMMEDIATE')
    try:
        for event in events:
            candidates = _booking_candidates(store, event)
            match_status = 'unique' if len(candidates) == 1 else (
                'ambiguous' if candidates else 'unmatched')
            booking = candidates[0] if match_status == 'unique' else (None, None)
            payload = event | {
                'account_id': booking[0], 'external_id': booking[1],
                'match_status': match_status,
            }
            existing = store.db.execute(
                'SELECT id,provider,event_status,event_date,amount,currency,provider_reference,'
                'account_id,external_id,match_status FROM payment_mail_events '
                'WHERE source_key=? AND event_status=?',
                (event['source_key'], event['event_status'])).fetchone()
            if existing is not None:
                immutable = ('provider', 'event_status', 'event_date', 'amount',
                             'currency', 'provider_reference')
                if any(existing[key] != payload[key] for key in immutable):
                    raise PaymentStatusError('payment_status_source_conflict')
                previous_booking = (existing['account_id'], existing['external_id'])
                if (existing['match_status'] in ('unique', 'conflict', 'stale')
                        and not candidates):
                    payload['account_id'], payload['external_id'] = previous_booking
                    payload['match_status'] = 'stale'
                elif (existing['match_status'] in ('unique', 'conflict', 'stale')
                      and previous_booking != booking):
                    payload['account_id'], payload['external_id'] = previous_booking
                    payload['match_status'] = 'conflict'
                if any(existing[key] != payload[key]
                       for key in ('account_id', 'external_id', 'match_status')):
                    store.db.execute(
                        'UPDATE payment_mail_events SET account_id=?,external_id=?,match_status=? '
                        'WHERE id=?', (payload['account_id'], payload['external_id'],
                                      payload['match_status'], existing['id']))
                    report['updated'] += 1
                    report[payload['match_status']] += 1
                else:
                    report['skipped'] += 1
                continue
            store.db.execute(
                'INSERT INTO payment_mail_events('
                'source_key,provider,event_status,event_date,amount,currency,provider_reference,'
                'account_id,external_id,match_status) VALUES (?,?,?,?,?,?,?,?,?,?)',
                tuple(payload[key] for key in (
                    'source_key', 'provider', 'event_status', 'event_date', 'amount', 'currency',
                    'provider_reference', 'account_id', 'external_id', 'match_status')))
            report['imported'] += 1
            report[match_status] += 1
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return report


def statuses_for_transactions(store, keys):
    result = {key: [] for key in keys}
    if not keys:
        return result
    clauses = ' OR '.join('(account_id=? AND external_id=?)' for _ in keys)
    params = [value for key in sorted(keys) for value in key]
    for row in store.db.execute(
            'SELECT provider,event_status,event_date,amount,currency,provider_reference,'
            'account_id,external_id,match_status FROM payment_mail_events WHERE '
            + clauses + ' ORDER BY event_date,id', params):
        key = (row['account_id'], row['external_id'])
        result[key].append({name: row[name] for name in (
            'provider', 'event_status', 'event_date', 'amount', 'currency',
            'provider_reference', 'match_status')})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Zahlungsstatus aus der lokalen Mailablage übernehmen')
    parser.add_argument('--supplier-database', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--since', default='2026-01-01')
    args = parser.parse_args(argv)
    store = None
    try:
        target = outside_repository(Path(args.database))
        store = Store(target)
        report = import_supplier_statuses(store, args.supplier_database, args.since)
    except Exception as error:
        parser.exit(2, 'Zahlungsstatus nicht übernommen; Details bleiben lokal. '
                       f'({type(error).__name__})\n')
    finally:
        if store is not None:
            store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
