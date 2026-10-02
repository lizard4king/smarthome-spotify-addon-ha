"""Offline, append-only archive of normalized bank bookings; no ledger or raw payloads."""

import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .connectors.fints_readonly import Booking
from .connectors.profiles import PROFILES
from .core import money

MAX_EXTERNAL_ID = 256
MAX_BOOKINGS = 100_000
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_LOCK = '.bank-archive.lock'


class BankArchiveError(ValueError):
    """Safe diagnostic: no source IDs, private paths or original payloads."""


@dataclass(frozen=True)
class ArchiveResult:
    path: Path = field(repr=False)
    sha256: str
    booking_count: int
    created: bool


def _metadata(source_profile, account_id, start, end):
    if type(source_profile) is not str or source_profile not in PROFILES:
        raise BankArchiveError('Unbekanntes Bankquellprofil.')
    if type(account_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', account_id):
        raise BankArchiveError('Gültige interne Kontokennung erforderlich.')
    if type(start) is not date or type(end) is not date or start > end:
        raise BankArchiveError('Gültiger, geordneter Buchungszeitraum erforderlich.')
    return {'source_profile': source_profile, 'account_id': account_id,
            'start': start.isoformat(), 'end': end.isoformat()}


def _rows(bookings, start, end):
    if type(bookings) not in (list, tuple) or len(bookings) > MAX_BOOKINGS:
        raise BankArchiveError('Begrenzte Lieferung normalisierter Buchungen erforderlich.')
    result, seen = [], set()
    for booking in bookings:
        if type(booking) is not Booking:
            raise BankArchiveError('Nur normalisierte Booking-Datensätze sind zulässig.')
        external_id = booking.external_id
        if (type(external_id) is not str or not external_id.strip()
                or external_id != external_id.strip() or len(external_id) > MAX_EXTERNAL_ID
                or any(unicodedata.category(char).startswith('C') for char in external_id)):
            raise BankArchiveError('Belastbare Quellen-ID fehlt oder ist ungültig; Lieferung blockiert.')
        if external_id in seen:
            raise BankArchiveError('Doppelte Quellen-ID innerhalb der Lieferung; Lieferung blockiert.')
        seen.add(external_id)
        if type(booking.amount) is not Decimal:
            raise BankArchiveError('Buchungsbetrag muss endlich und verlustfrei centgenau sein.')
        try:
            value = money(booking.amount)
        except ValueError:
            raise BankArchiveError(
                'Buchungsbetrag muss endlich und verlustfrei centgenau sein.') from None
        if type(booking.currency) is not str or not re.fullmatch(r'[A-Z]{3}', booking.currency):
            raise BankArchiveError('Gültiges Währungskürzel erforderlich.')
        if (not isinstance(booking.booked_on, date) or isinstance(booking.booked_on, datetime)
                or not start <= booking.booked_on <= end):
            raise BankArchiveError('Buchungsdatum liegt nicht im angegebenen Zeitraum.')
        result.append({'external_id': external_id, 'amount': format(value if value else Decimal(0), '.2f'),
                       'currency': booking.currency, 'booked_on': booking.booked_on.isoformat()})
    return sorted(result, key=lambda row: row['external_id'])


def _encode(payload):
    return (json.dumps(payload, sort_keys=True, ensure_ascii=False,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _directory(directory):
    raw = Path(directory).expanduser().absolute()
    resolved = raw.resolve()
    # Include the directory itself and worktree .git files, even dangling links.
    for candidate in (raw, *raw.parents, resolved, *resolved.parents):
        marker = candidate / '.git'
        bare = ((candidate / 'HEAD').is_file() and (candidate / 'objects').is_dir()
                and (candidate / 'refs').is_dir())
        if marker.exists() or marker.is_symlink() or bare:
            raise BankArchiveError('Bankarchive müssen außerhalb jedes Git-Repositorys liegen.')
    return resolved


def _read_archive(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise BankArchiveError('Archivbestand ist unvollständig oder verändert; Archivierung blockiert.')
    raw = path.read_bytes()
    if not re.fullmatch(r'[0-9a-f]{64}\.json', path.name) or hashlib.sha256(raw).hexdigest() != path.stem:
        raise BankArchiveError('Archivprüfsumme stimmt nicht; Archivierung blockiert.')
    try:
        payload = json.loads(raw)
        if (type(payload) is not dict or set(payload) != {'schema', 'metadata', 'bookings'}
                or type(payload['schema']) is not int or payload['schema'] != 1):
            raise ValueError
        meta = payload['metadata']
        if type(meta) is not dict or set(meta) != {'source_profile', 'account_id', 'start', 'end'}:
            raise ValueError
        start, end = date.fromisoformat(meta['start']), date.fromisoformat(meta['end'])
        canonical_meta = _metadata(meta['source_profile'], meta['account_id'], start, end)
        if type(payload['bookings']) is not list or len(payload['bookings']) > MAX_BOOKINGS:
            raise ValueError
        bookings = []
        for row in payload['bookings']:
            if (type(row) is not dict or set(row) != {'external_id', 'amount', 'currency', 'booked_on'}
                    or type(row['amount']) is not str):
                raise ValueError
            bookings.append(Booking(row['external_id'], Decimal(row['amount']), row['currency'],
                                    date.fromisoformat(row['booked_on'])))
        validated = {'schema': 1, 'metadata': canonical_meta, 'bookings': _rows(bookings, start, end)}
        if raw != _encode(validated):
            raise ValueError
        return validated
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        raise BankArchiveError('Archivinhalt ist ungültig oder verändert; Archivierung blockiert.') from None


def _check_history(root, incoming):
    existing = {}
    for path in root.iterdir():
        if path.name == _LOCK:
            continue
        archived = _read_archive(path)
        meta = archived['metadata']
        for row in archived['bookings']:
            key = (meta['source_profile'], meta['account_id'], row['external_id'])
            core = (row['booked_on'], row['amount'], row['currency'])
            if key in existing and existing[key] != core:
                raise BankArchiveError('Widersprüchliche Quellen-ID im Archivbestand; Archivierung blockiert.')
            existing[key] = core
    meta = incoming['metadata']
    for row in incoming['bookings']:
        key = (meta['source_profile'], meta['account_id'], row['external_id'])
        core = (row['booked_on'], row['amount'], row['currency'])
        if key in existing and existing[key] != core:
            raise BankArchiveError('Quellen-ID widerspricht vorhandenem Archiv; Lieferung blockiert.')


def _publish_without_overwrite(temporary, target):
    """Publish atomically without replacing a peer's archive file."""
    if os.name == 'nt':
        os.rename(temporary, target)
        return
    os.link(temporary, target)


def _sync_directory(root):
    """Persist a new POSIX directory entry; Windows uses its rename semantics."""
    if os.name == 'nt':
        return
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive_bookings(bookings, directory, *, source_profile, account_id, start, end):
    """Validate all rows before writing; retrying identical content returns created=False.

    IDs are scoped by source profile and internal account. The dedicated directory
    must be locally controlled. Files are never replaced; hashing detects accidental
    changes, not an attacker rewriting both contents and names. No bank/ledger access.
    """
    metadata = _metadata(source_profile, account_id, start, end)
    payload = {'schema': 1, 'metadata': metadata, 'bookings': _rows(bookings, start, end)}
    raw = _encode(payload)
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise BankArchiveError('Archivlieferung ist zu groß.')
    digest = hashlib.sha256(raw).hexdigest()
    temporary = None
    locked = False
    try:
        root = _directory(directory)
        root.mkdir(parents=True, exist_ok=True)
        lock = root / _LOCK
        try:
            lock.mkdir()
        except FileExistsError:
            raise BankArchiveError('Archiv ist gesperrt; laufenden oder abgebrochenen Vorgang prüfen.') from None
        locked = True
        _check_history(root, payload)
        target = root / f'{digest}.json'
        if target.exists():
            return ArchiveResult(target, digest, len(payload['bookings']), False)
        with tempfile.NamedTemporaryFile(dir=root, prefix='.pending-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _publish_without_overwrite(temporary, target)
        _sync_directory(root)
        return ArchiveResult(target, digest, len(payload['bookings']), True)
    except (OSError, RuntimeError):
        raise BankArchiveError('Archiv konnte nicht sicher gelesen oder geschrieben werden.') from None
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_failed = False
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        if locked:
            try:
                lock.rmdir()
            except OSError:
                cleanup_failed = True
        if cleanup_failed and not active_error:
            raise BankArchiveError(
                'Archivabschluss unklar; Bestand und Sperre vor Wiederholung prüfen.') from None
