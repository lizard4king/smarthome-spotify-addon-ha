"""Atomic import of a validated local bank archive into the ledger."""

import csv
import io
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .bank_archive import BankArchiveError, _read_archive


@dataclass(frozen=True)
class BankArchiveImportResult:
    source_profile: str
    account_id: str
    archive_sha256: str
    booking_count: int
    inserted: int

    @property
    def skipped(self):
        return self.booking_count - self.inserted


def _category(value):
    if (type(value) is not str or not value.strip() or value != value.strip()
            or len(value) > 240
            or any(unicodedata.category(char).startswith('C') for char in value)):
        raise BankArchiveError('Explizite gültige Quellkategorie erforderlich.')
    return value


def _mapped_account(metadata, account_mapping):
    if type(account_mapping) is not dict:
        raise BankArchiveError('Explizite Kontozuordnung erforderlich.')
    archive_account = metadata['account_id']
    if set(account_mapping) != {archive_account} or account_mapping[archive_account] != archive_account:
        raise BankArchiveError('Archivkonto muss ausdrücklich dem gleichnamigen Ledgerkonto zugeordnet sein.')
    return archive_account


def _external_id(source_profile, source_id):
    """Preserve the bank ID and add only its deterministic source namespace."""
    return f'bank:{source_profile}:{source_id}'


def _csv_source(payload, account_id, source_category):
    stream = io.StringIO(newline='')
    columns = ['external_id', 'account_id', 'date', 'amount', 'currency',
               'category', 'transfer_id']
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator='\n')
    writer.writeheader()
    for row in payload['bookings']:
        writer.writerow({
            'external_id': _external_id(payload['metadata']['source_profile'], row['external_id']),
            'account_id': account_id,
            'date': row['booked_on'],
            'amount': row['amount'],
            'currency': row['currency'],
            'category': source_category,
            'transfer_id': '',
        })
    return stream.getvalue()


def _preflight(store, payload, account_id, source_category):
    account = store.db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
    if account is None:
        raise BankArchiveError('Zugeordnetes Ledgerkonto fehlt.')
    profile = payload['metadata']['source_profile']
    for row in payload['bookings']:
        if row['currency'] != account['currency']:
            raise BankArchiveError('Archivwährung stimmt nicht mit dem Ledgerkonto überein.')
        if row['booked_on'] <= account['opening_date']:
            raise BankArchiveError('Archivbuchung liegt nicht nach der Kontoeröffnung.')
        expected = (account_id, _external_id(profile, row['external_id']), row['booked_on'],
                    row['amount'], row['currency'], source_category, '')
        existing = store.db.execute(
            'SELECT account_id,external_id,date,amount,currency,category,transfer_id '
            'FROM transactions WHERE account_id=? AND external_id=?', expected[:2]).fetchone()
        if existing is not None and tuple(existing) != expected:
            raise BankArchiveError('Quellen-ID widerspricht einer vorhandenen Ledgerbuchung.')


def import_bank_archive(store, archive_path, *, account_mapping, source_category):
    """Validate and atomically import one normalized archive without network access.

    ``account_mapping`` explicitly confirms the archive's internal account identifier.
    The identifier must map to the same ledger account; account renaming is deliberately
    outside this import boundary. ``source_category`` is stored verbatim after validation.
    """
    payload = _read_archive(Path(archive_path))
    source_category = _category(source_category)
    account_id = _mapped_account(payload['metadata'], account_mapping)
    _preflight(store, payload, account_id, source_category)
    source = _csv_source(payload, account_id, source_category)
    try:
        with store.db:
            inserted = store.import_csv(source, manage_transaction=False)
    except (ValueError, TypeError, sqlite3.DatabaseError):
        raise BankArchiveError('Bankarchiv konnte nicht vollständig ins Ledger übernommen werden.') from None
    return BankArchiveImportResult(
        source_profile=payload['metadata']['source_profile'],
        account_id=account_id,
        archive_sha256=Path(archive_path).stem,
        booking_count=len(payload['bookings']),
        inserted=inserted,
    )
