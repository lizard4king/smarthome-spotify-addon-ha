"""Atomic complete-statement import using local statement/ordinal identities."""
import csv
import io
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path

from .statement_archive import read_statement_archive, statement_key, statement_period, source_account_key, _cash
from .statement_model import StatementError
from .bank_archive import _directory


@dataclass(frozen=True)
class StatementImportResult:
    booking_count: int
    inserted: int

    @property
    def skipped(self):
        return self.booking_count - self.inserted


def import_statement_archive(store, path, *, account_id, confirmed_source_account, source_category):
    """Import one immutable complete statement; never reconcile uncertain legacy rows."""
    s = read_statement_archive(path)
    return _import_validated(store, s, path, account_id=account_id,
                             confirmed_source_account=confirmed_source_account,
                             source_category=source_category, key=statement_key(s),
                             source_key=source_account_key(s), period=statement_period(s), namespace='statement')


def import_monthly_archive(store, path, *, account_id, confirmed_source_account, source_category,
                           require_adopted_source=False):
    """Import one fixed month with LOCAL month/ordinal IDs; no bank-ID claim."""
    from .monthly_archive import read_monthly_archive, monthly_key, monthly_source_account_key
    s = read_monthly_archive(path)
    return _import_validated(store, s, path, account_id=account_id,
                             confirmed_source_account=confirmed_source_account,
                             source_category=source_category, key=monthly_key(s),
                             source_key=monthly_source_account_key(s), period=(s.period_start, s.period_end), namespace='period',
                             require_adopted_source=require_adopted_source)


def _registered_import_row(store, account_id, external_id, booked_on):
    parts = external_id.split(':')
    if len(parts) != 3 or parts[0] not in ('statement', 'period') or not parts[1]:
        return False
    try:
        ordinal = int(parts[2])
    except ValueError:
        return False
    if ordinal < 1 or str(ordinal) != parts[2]:
        return False
    registry = store.db.execute(
        'SELECT account_id,period_start,period_end,booking_count FROM bank_statement_imports WHERE statement_key=?',
        (parts[1],),
    ).fetchone()
    return (registry is not None and registry['account_id'] == account_id and
            ordinal <= registry['booking_count'] and
            registry['period_start'] <= booked_on <= registry['period_end'])


def _import_validated(store, s, path, *, account_id, confirmed_source_account, source_category,
                      key, source_key, period, namespace, require_adopted_source=False):
    if type(require_adopted_source) is not bool:
        raise StatementError('Quellenfreigabe ist ungültig.')
    if type(confirmed_source_account) is not str or confirmed_source_account != s.source_account:
        raise StatementError('Bankkonto muss ausdruecklich bestaetigt sein.')
    if type(account_id) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', account_id) is None:
        raise StatementError('Explizites Ledgerkonto erforderlich.')
    if type(source_category) is not str or not source_category.strip() or source_category != source_category.strip() or len(source_category) > 240 or any(not char.isprintable() for char in source_category):
        raise StatementError('Explizite Quellkategorie erforderlich.')
    digest = Path(path).stem
    start, end = period
    empty_point = namespace == 'statement' and not s.rows and s.opening_date == s.closing_date
    columns = ['external_id', 'account_id', 'date', 'amount', 'currency', 'category', 'transfer_id']
    rows = [{'external_id': f'{namespace}:{key}:{index}', 'account_id': account_id,
             'date': row.booked_on.isoformat(), 'amount': _cash(row.amount), 'currency': row.currency,
             'category': source_category, 'transfer_id': ''} for index, row in enumerate(s.rows, 1)]
    expected_registry = (key, account_id, digest, source_category, start.isoformat(), end.isoformat(),
                         len(rows), _cash(s.opening_balance), _cash(s.closing_balance))
    if store.db.in_transaction:
        raise StatementError('Laufende Ledgertransaktion muss zuerst abgeschlossen werden.')
    try:
        store.db.execute('BEGIN IMMEDIATE')
        account = store.db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if account is None or account['currency'] != s.currency or any(row['date'] <= account['opening_date'] for row in rows):
            raise StatementError('Ledgerkonto, Währung oder Eröffnungsdatum passt nicht zum Auszug.')
        binding = store.db.execute('SELECT account_id FROM bank_statement_account_bindings WHERE source_key=?', (source_key,)).fetchone()
        reverse = store.db.execute('SELECT source_key FROM bank_statement_account_bindings WHERE account_id=?', (account_id,)).fetchone()
        if (binding is not None and binding[0] != account_id) or (reverse is not None and reverse[0] != source_key):
            raise StatementError('Bankkonto und Ledgerkonto haben bereits eine andere bestätigte Zuordnung.')
        direct_binding = store.db.execute(
            'SELECT provider,account_id FROM bank_source_accounts WHERE source_key=?', (source_key,)).fetchone()
        direct_reverse = store.db.execute(
            'SELECT provider,source_key FROM bank_source_accounts WHERE account_id=?', (account_id,)).fetchone()
        if ((direct_binding is not None and
             (direct_binding['provider'] != s.source_profile or direct_binding['account_id'] != account_id)) or
                (direct_reverse is not None and
                 (direct_reverse['provider'] != s.source_profile or direct_reverse['source_key'] != source_key))):
            raise StatementError('Bankquelle ist direkt einem anderen Konto oder Provider zugeordnet.')
        if require_adopted_source:
            # A recurring job cannot establish an initial source cutover. Check
            # after BEGIN IMMEDIATE so a concurrent writer cannot revoke the
            # previously reviewed binding between this guard and the inserts.
            adoption = store.db.execute(
                'SELECT 1 FROM bank_monthly_adoptions WHERE source_key=? AND account_id=? LIMIT 1',
                (source_key, account_id)).fetchone()
            if direct_binding is None or direct_reverse is None or adoption is None:
                raise StatementError('Bestätigte und finanziell abgeglichene Bankquelle fehlt.')
        previous = store.db.execute('SELECT * FROM bank_statement_imports WHERE statement_key=?', (key,)).fetchone()
        if previous is not None:
            if tuple(previous) != expected_registry:
                raise StatementError('Auszug, Kontozuordnung oder Kategorie widerspricht dem früheren Import.')
            existing_rows = store.db.execute(
                'SELECT external_id,date FROM transactions WHERE account_id=? AND date>=? AND date<=?',
                (account_id, start.isoformat(), end.isoformat()),
            ).fetchall()
            if empty_point:
                if any(not _registered_import_row(store, account_id, row['external_id'], row['date'])
                       for row in existing_rows):
                    raise StatementError('Leerer Auszugspunkt enthält nicht registrierte Buchungen.')
            elif {row['external_id'] for row in existing_rows} != {row['external_id'] for row in rows}:
                raise StatementError('Früherer Auszugimport ist unvollständig oder enthält zusätzliche Buchungen.')
            for row in rows:
                actual = store.db.execute('SELECT external_id,account_id,date,amount,currency,category,transfer_id FROM transactions WHERE account_id=? AND external_id=?', (account_id, row['external_id'])).fetchone()
                if actual is None or tuple(actual) != tuple(row[column] for column in columns):
                    raise StatementError('Früherer Auszugimport ist unvollständig oder verändert.')
            if binding is None:
                store.db.execute('INSERT INTO bank_statement_account_bindings VALUES (?,?)', (source_key, account_id))
            store.db.commit()
            return StatementImportResult(len(rows), 0)
        overlap = None if empty_point else store.db.execute('SELECT 1 FROM bank_statement_imports WHERE account_id=? AND NOT (booking_count=0 AND period_start=period_end) AND period_start<=? AND period_end>=?', (account_id, end.isoformat(), start.isoformat())).fetchone()
        legacy = store.db.execute('SELECT 1 FROM transactions WHERE account_id=? AND date>=? AND date<=?', (account_id, start.isoformat(), end.isoformat())).fetchone()
        if overlap is not None or legacy is not None:
            raise StatementError('Auszugszeitraum überschneidet vorhandene Buchungen; Abgleich erforderlich.')
        with localcontext() as context:
            context.prec = 40
            ledger_opening = Decimal(account['opening'])
            for existing in store.db.execute('SELECT amount FROM transactions WHERE account_id=? AND date<?', (account_id, start.isoformat())):
                ledger_opening += Decimal(existing['amount'])
            if ledger_opening != s.opening_balance or account['opening_date'] >= start.isoformat():
                raise StatementError('Ledger-Anfangssaldo passt nicht zum Kontoauszug.')
        if rows:
            stream = io.StringIO(newline='')
            writer = csv.DictWriter(stream, fieldnames=columns, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
            inserted = store.import_csv(stream.getvalue(), manage_transaction=False)
            if inserted != len(rows):
                raise StatementError('Auszug konnte nicht vollständig übernommen werden.')
        else:
            inserted = 0
        store.db.execute('INSERT INTO bank_statement_imports VALUES (?,?,?,?,?,?,?,?,?)', expected_registry)
        if binding is None:
            store.db.execute('INSERT INTO bank_statement_account_bindings VALUES (?,?)', (source_key, account_id))
        store.db.commit()
        return StatementImportResult(len(rows), inserted)
    except StatementError:
        store.db.rollback()
        raise
    except Exception:
        store.db.rollback()
        raise StatementError('Auszug konnte nicht atomar übernommen werden.') from None


def _snapshot(database, target):
    source = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def preview_statement_import(database, archive, **kwargs):
    """Exercise the entire import on a consistent private DB copy; original stays read-only."""
    return _preview_import(import_statement_archive, database, archive, kwargs)


def preview_monthly_import(database, archive, **kwargs):
    return _preview_import(import_monthly_archive, database, archive, kwargs)


def _preview_import(importer, database, archive, kwargs):
    from .core import Store
    database = Path(database).resolve(strict=True)
    parent = _directory(database.parent)
    with tempfile.TemporaryDirectory(prefix='.statement-preview-', dir=parent) as directory:
        snapshot = Path(directory) / 'preview.sqlite'
        _snapshot(database, snapshot)
        store = Store(snapshot)
        try:
            return importer(store, archive, **kwargs)
        finally:
            store.close()


def main(argv=None):
    import argparse
    from .core import Store
    parser = argparse.ArgumentParser(description='Vollständigen Kontoauszug offline prüfen; --apply übernimmt nach Sicherung.')
    parser.add_argument('--database', required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--category', default='FinTS · ungeprüft')
    parser.add_argument('--confirm-account', action='store_true', help='Zuordnung von Auszugsbankkonto und Ledgerkonto ausdrücklich bestätigen')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--monthly-snapshot', action='store_true', help='Archiv mit ausdruecklich lokaler Monatskennung')
    args = parser.parse_args(argv)
    try:
        if not args.confirm_account:
            raise StatementError('Explizite Bestätigung der Kontozuordnung fehlt.')
        if args.monthly_snapshot:
            from .monthly_archive import read_monthly_archive
            statement = read_monthly_archive(args.archive)
            preview = preview_monthly_import
            importer = import_monthly_archive
        else:
            statement = read_statement_archive(args.archive)
            preview = preview_statement_import
            importer = import_statement_archive
        parameters = dict(account_id=args.account_id, confirmed_source_account=statement.source_account,
                          source_category=args.category)
        result = preview(args.database, args.archive, **parameters)
        print(f'Vorschau: {result.booking_count} Posten; neu {result.inserted}; bereits übernommen {result.skipped}.')
        if not args.apply:
            print('Originaldatenbank unverändert. Keine Übernahme.')
            return 0
        database = Path(args.database).resolve(strict=True)
        backup = _directory(database.parent) / ('statement-before-import-' + uuid.uuid4().hex + '.sqlite')
        _snapshot(database, backup)
        store = Store(database)
        try:
            result = importer(store, args.archive, **parameters)
        finally:
            store.close()
        print(f'Übernommen: {result.inserted}; übersprungen: {result.skipped}. Konsistente Sicherung: {backup}')
        return 0
    except StatementError as error:
        print(f'Auszug gesperrt: {error}')
        return 1
    except Exception:
        print('Auszugprüfung nicht abgeschlossen; keine sichere Übernahme bestätigt.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
