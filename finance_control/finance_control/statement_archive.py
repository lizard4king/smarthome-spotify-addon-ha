"""Offline append-only archive of complete, balanced statements."""
import hashlib
import json
import re
import tempfile
import os
import sys
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from .bank_archive import ArchiveResult, _directory, _publish_without_overwrite, _sync_directory
from .connectors.profiles import PROFILES
from .statement_model import BankStatement, StatementRow, StatementError

MAX_BYTES = 32 * 1024 * 1024


def _cash(value):
    if type(value) is not Decimal or not value.is_finite() or abs(value) >= Decimal('1e18'):
        raise StatementError('Endlicher, centgenauer Betrag erforderlich.')
    with localcontext() as context:
        context.prec = 40
        if value != value.quantize(Decimal('.01')):
            raise StatementError('Endlicher, centgenauer Betrag erforderlich.')
        return format(value if value else Decimal(0), '.2f')


def validate_statement(statement):
    if type(statement) is not BankStatement:
        raise StatementError('Vollständiger normalisierter Kontoauszug erforderlich.')
    s = statement
    if type(s.source_profile) is not str or s.source_profile not in PROFILES:
        raise StatementError('Unbekanntes Quellprofil.')
    if type(s.source_account) is not str or not 1 <= len(s.source_account) <= 120 or any(
            char.isspace() or not char.isprintable() for char in s.source_account):
        raise StatementError('Bankseitige Kontokennung fehlt oder ist ungültig.')
    if type(s.year) is not int or not 1000 <= s.year <= 9999 or type(s.number) is not int or not 1 <= s.number <= 999999999:
        raise StatementError('Bankseitige Auszugsidentität fehlt oder ist ungültig.')
    if type(s.opening_date) is not date or type(s.closing_date) is not date or s.opening_date > s.closing_date or s.year != s.closing_date.year:
        raise StatementError('Auszugsdaten sind ungültig.')
    if type(s.currency) is not str or re.fullmatch(r'[A-Z]{3}', s.currency) is None:
        raise StatementError('Auszugswährung ist ungültig.')
    _cash(s.opening_balance)
    _cash(s.closing_balance)
    if type(s.rows) is not tuple or len(s.rows) > 100000:
        raise StatementError('Geordneter, begrenzter Postenbestand erforderlich.')
    with localcontext() as context:
        context.prec = 40
        total = s.opening_balance
        for row in s.rows:
            if type(row) is not StatementRow or type(row.booked_on) is not date or not s.opening_date <= row.booked_on <= s.closing_date or row.currency != s.currency:
                raise StatementError('Auszugsposten widerspricht Datum oder Währung.')
            _cash(row.amount)
            total += row.amount
        if total != s.closing_balance:
            raise StatementError('Anfangssaldo plus Posten ergibt nicht den Endsaldo; Auszug gesperrt.')
    return s


def statement_key(s):
    validate_statement(s)
    identity = [s.source_profile, s.source_account, s.year, s.number]
    return hashlib.sha256(json.dumps(identity, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def source_account_key(s):
    validate_statement(s)
    return hashlib.sha256(json.dumps([s.source_profile, s.source_account],
                                    separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def statement_period(s):
    start = min((row.booked_on for row in s.rows), default=s.closing_date)
    if s.opening_date < s.closing_date:
        start = min(start, s.opening_date + timedelta(days=1))
    return start, s.closing_date


def _payload(s):
    validate_statement(s)
    return {'schema': 1, 'source_profile': s.source_profile, 'source_account': s.source_account,
            'year': s.year, 'number': s.number, 'opening_date': s.opening_date.isoformat(),
            'closing_date': s.closing_date.isoformat(), 'opening_balance': _cash(s.opening_balance),
            'closing_balance': _cash(s.closing_balance), 'currency': s.currency,
            'rows': [{'booked_on': row.booked_on.isoformat(), 'amount': _cash(row.amount),
                      'currency': row.currency} for row in s.rows]}


def _encode(s):
    return (json.dumps(_payload(s), sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def read_statement_archive(path):
    try:
        path = Path(path)
        _directory(path.parent)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES or re.fullmatch(r'[0-9a-f]{64}\.json', path.name) is None:
            raise StatementError('Auszugsarchiv ist ungültig.')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != path.stem:
            raise StatementError('Auszugsarchiv ist verändert.')
        p = json.loads(raw)
        if type(p) is dict and p.get('kind') is not None:
            raise StatementError('Archivtyp passt nicht zum Bankauszug; Bankauszugs- und Monatsarchive in getrennten Verzeichnissen speichern.')
        if type(p) is not dict or type(p.get('schema')) is not int or p['schema'] != 1 or type(p.get('rows')) is not list or len(p['rows']) > 100000:
            raise StatementError('Auszugsarchiv ist ungültig.')
        rows = tuple(StatementRow(date.fromisoformat(r['booked_on']), Decimal(r['amount']), r['currency']) for r in p['rows'])
        s = BankStatement(p['source_profile'], p['source_account'], p['year'], p['number'],
                          date.fromisoformat(p['opening_date']), date.fromisoformat(p['closing_date']),
                          Decimal(p['opening_balance']), Decimal(p['closing_balance']), p['currency'], rows)
        if _encode(s) != raw:
            raise StatementError('Auszugsarchiv ist nicht kanonisch oder vollständig.')
        return s
    except StatementError:
        raise
    except Exception:
        raise StatementError('Auszugsarchiv ist nicht lesbar oder ungültig.') from None


def archive_statement(statement, directory):
    raw = _encode(statement)
    if len(raw) > MAX_BYTES:
        raise StatementError('Auszugsarchiv ist zu groß.')
    key = statement_key(statement)
    digest = hashlib.sha256(raw).hexdigest()
    temporary = None
    lock = None
    try:
        root = _directory(directory)
        root.mkdir(parents=True, exist_ok=True)
        lock = root / '.statement-archive.lock'
        try:
            lock.mkdir()
        except FileExistsError:
            lock = None
            raise StatementError('Auszugsarchiv ist gesperrt.') from None
        for existing in root.glob('*.json'):
            other = read_statement_archive(existing)
            if statement_key(other) == key and existing.stem != digest:
                raise StatementError('Dieselbe Auszugsidentität hat geänderten Inhalt; Archivierung gesperrt.')
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
        raise StatementError('Auszugsarchiv konnte nicht sicher geschrieben werden.') from None
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
            raise StatementError('Auszugsarchivabschluss unklar; Sperre und Bestand prüfen.')
