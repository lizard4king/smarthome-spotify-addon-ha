"""Immutable balanced monthly snapshots with explicitly local identities."""
import hashlib
import json
import re
import tempfile
import os
import sys
from datetime import date
from calendar import monthrange
from decimal import Decimal, localcontext
from pathlib import Path

from .bank_archive import ArchiveResult, _directory, _publish_without_overwrite, _sync_directory
from .connectors.profiles import PROFILES
from .statement_model import MonthlySnapshot, StatementRow, StatementError

MAX_BYTES = 32 * 1024 * 1024


def _cash(value):
    if type(value) is not Decimal or not value.is_finite() or abs(value) >= Decimal('1e18'):
        raise StatementError('Endlicher, centgenauer Betrag erforderlich.')
    with localcontext() as context:
        context.prec = 40
        if value != value.quantize(Decimal('.01')):
            raise StatementError('Endlicher, centgenauer Betrag erforderlich.')
        return format(value if value else Decimal(0), '.2f')


def validate_snapshot(s):
    if type(s) is not MonthlySnapshot:
        raise StatementError('Gepruefter Monatsabruf erforderlich.')
    if type(s.source_profile) is not str or s.source_profile not in PROFILES:
        raise StatementError('Unbekanntes Quellprofil.')
    if type(s.source_account) is not str or not 1 <= len(s.source_account) <= 120 or any(
            char.isspace() or not char.isprintable() for char in s.source_account):
        raise StatementError('Bankseitige Kontokennung fehlt oder ist ungueltig.')
    if any(type(value) is not date for value in (s.period_start, s.period_end, s.opening_date, s.closing_date)):
        raise StatementError('Monatsdaten sind ungueltig.')
    if (s.period_start.day != 1 or s.period_end != date(s.period_start.year, s.period_start.month,
            monthrange(s.period_start.year, s.period_start.month)[1]) or
            s.opening_date > s.period_start or s.closing_date != s.period_end):
        raise StatementError('Ein ganzer Kalendermonat mit passenden Kontrollsalden erforderlich.')
    if type(s.currency) is not str or re.fullmatch(r'[A-Z]{3}', s.currency) is None:
        raise StatementError('Monatswaehrung ist ungueltig.')
    _cash(s.opening_balance)
    _cash(s.closing_balance)
    if type(s.rows) is not tuple or len(s.rows) > 100000:
        raise StatementError('Geordneter, begrenzter Postenbestand erforderlich.')
    with localcontext() as context:
        context.prec = 40
        total = s.opening_balance
        for row in s.rows:
            if (type(row) is not StatementRow or type(row.booked_on) is not date or
                    not s.period_start <= row.booked_on <= s.period_end or row.currency != s.currency):
                raise StatementError('Monatsposten widerspricht Zeitraum oder Waehrung.')
            _cash(row.amount)
            total += row.amount
        if total != s.closing_balance:
            raise StatementError('Anfangssaldo plus Posten ergibt nicht den Endsaldo; Monat gesperrt.')
    return s


def monthly_key(s):
    validate_snapshot(s)
    identity = ['monthly', s.source_profile, s.source_account, s.period_start.isoformat(), s.period_end.isoformat()]
    return hashlib.sha256(json.dumps(identity, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def monthly_source_account_key(s):
    validate_snapshot(s)
    return hashlib.sha256(json.dumps([s.source_profile, s.source_account],
                                    separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _payload(s):
    validate_snapshot(s)
    return {'schema': 1, 'kind': 'monthly-snapshot', 'source_profile': s.source_profile, 'source_account': s.source_account,
            'period_start': s.period_start.isoformat(), 'period_end': s.period_end.isoformat(), 'opening_date': s.opening_date.isoformat(),
            'closing_date': s.closing_date.isoformat(), 'opening_balance': _cash(s.opening_balance),
            'closing_balance': _cash(s.closing_balance), 'currency': s.currency,
            'rows': [{'booked_on': row.booked_on.isoformat(), 'amount': _cash(row.amount),
                      'currency': row.currency} for row in s.rows]}


def _encode(s):
    return (json.dumps(_payload(s), sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def read_monthly_archive(path):
    try:
        path = Path(path)
        _directory(path.parent)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES or re.fullmatch(r'[0-9a-f]{64}\.json', path.name) is None:
            raise StatementError('Monatsarchiv ist ungültig.')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != path.stem:
            raise StatementError('Monatsarchiv ist verändert.')
        p = json.loads(raw)
        if type(p) is dict and p.get('kind') != 'monthly-snapshot':
            raise StatementError('Archivtyp passt nicht zum Monatsabruf; Bankauszugs- und Monatsarchive in getrennten Verzeichnissen speichern.')
        if type(p) is not dict or type(p.get('schema')) is not int or p['schema'] != 1 or p.get('kind') != 'monthly-snapshot' or type(p.get('rows')) is not list or len(p['rows']) > 100000:
            raise StatementError('Monatsarchiv ist ungültig.')
        rows = tuple(StatementRow(date.fromisoformat(r['booked_on']), Decimal(r['amount']), r['currency']) for r in p['rows'])
        s = MonthlySnapshot(p['source_profile'], p['source_account'], date.fromisoformat(p['period_start']), date.fromisoformat(p['period_end']),
                          date.fromisoformat(p['opening_date']), date.fromisoformat(p['closing_date']),
                          Decimal(p['opening_balance']), Decimal(p['closing_balance']), p['currency'], rows)
        if _encode(s) != raw:
            raise StatementError('Monatsarchiv ist nicht kanonisch oder vollständig.')
        return s
    except StatementError:
        raise
    except Exception:
        raise StatementError('Monatsarchiv ist nicht lesbar oder ungültig.') from None


def archive_monthly_snapshot(statement, directory):
    raw = _encode(statement)
    if len(raw) > MAX_BYTES:
        raise StatementError('Monatsarchiv ist zu groß.')
    key = monthly_key(statement)
    digest = hashlib.sha256(raw).hexdigest()
    temporary = None
    lock = None
    try:
        root = _directory(directory)
        root.mkdir(parents=True, exist_ok=True)
        lock = root / '.monthly-archive.lock'
        try:
            lock.mkdir()
        except FileExistsError:
            lock = None
            raise StatementError('Monatsarchiv ist gesperrt.') from None
        for existing in root.glob('*.json'):
            other = read_monthly_archive(existing)
            if monthly_key(other) == key and existing.stem != digest:
                raise StatementError('Dieselbe lokale Monatsidentitaet hat geänderten Inhalt; Archivierung gesperrt.')
        target = root / (digest + '.json')
        if target.exists():
            return ArchiveResult(target, digest, len(statement.rows), False)
        with tempfile.NamedTemporaryFile(dir=root, prefix='.pending-', delete=False) as file:
            temporary = Path(file.name)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        _publish_without_overwrite(temporary, target)
        _sync_directory(root)
        return ArchiveResult(target, digest, len(statement.rows), True)
    except StatementError:
        raise
    except Exception:
        raise StatementError('Monatsarchiv konnte nicht sicher geschrieben werden.') from None
    finally:
        active_error = sys.exc_info()[0] is not None
        failed = False
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                failed = True
        if lock is not None:
            try:
                lock.rmdir()
            except OSError:
                failed = True
        if failed and not active_error:
            raise StatementError('Monatsarchivabschluss unklar; Sperre und Bestand prüfen.')
