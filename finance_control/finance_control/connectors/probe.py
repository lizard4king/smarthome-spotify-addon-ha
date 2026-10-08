"""Explicit, local read-only FinTS probe; never stores or imports bookings."""

import argparse
from collections import defaultdict
from datetime import date
from decimal import Decimal
import getpass
import json
from pathlib import Path
import re
import sys

from finance_control.connectors.fints_readonly import (
    BankErrorCode, BankErrorKind, BankErrorOrigin, BankReadError, ReadOperation, create_reader,
)
from finance_control.connectors.profiles import PROFILES
from finance_control.security.credentials import Credentials, WindowsCredentialStore
from finance_control.statement_model import StatementError


class ProbeError(RuntimeError):
    """A diagnostic that contains no bank or credential payload."""


def _bank_diagnostic(error):
    # Only fixed categories, never exception text or a raw bank response.
    messages = {
        BankErrorCode.AUTH_REJECTED: 'Bankanmeldung abgelehnt; Ursache noch zu prüfen. Gespeicherten Zugang vor einem weiteren Bankabruf lokal prüfen.',
        BankErrorCode.ONLINE_LOGIN_REQUIRED: 'ING verlangt einen erneuten Online-Banking-Login; QR-Login mit der ING-App vollständig abschließen.',
        BankErrorCode.CREDENTIALS_REJECTED: 'ING meldet eine abgelehnte Anmeldung. Gespeicherten Benutzernamen und Online-Banking-Passwort lokal prüfen; keine weiteren Versuche mit ungeprüftem Zugang.',
        BankErrorCode.AUTH_TEMPORARY: 'Bank meldet eine vorübergehende Zugangssperre.',
        BankErrorCode.SCA_REQUIRED: 'Zusätzliche Bankfreigabe erforderlich; Freigabeverfahren noch einzurichten.',
        BankErrorCode.UNSUPPORTED: 'Die Bibliothek unterstützt diesen Bankabruf nicht.',
        BankErrorCode.CONNECTION: 'Verbindung zum Bankserver fehlgeschlagen.',
        BankErrorCode.TIMEOUT: 'Zeitüberschreitung bei der Bankverbindung.',
        BankErrorCode.TLS: 'Zertifikatsprüfung der Bankverbindung fehlgeschlagen.',
        BankErrorCode.DIALOG_INIT: 'Bankdialog konnte nicht eröffnet werden.',
        BankErrorCode.NO_RESPONSE: 'Erwartete Nutzdaten fehlen in der Bankantwort.',
        BankErrorCode.BANK_REJECTED: 'Bankdialog abgelehnt; Bankprofil und Anmeldung prüfen.',
        BankErrorCode.LOCAL_AUTH_ABORT: 'Lokale Freigabe abgebrochen.',
        BankErrorCode.GRAPHICAL_TAN: 'Grafisches Freigabeverfahren benötigt eine lokale Anzeige.',
        BankErrorCode.TAN_LIMIT: 'Maximale Zahl der Freigabeschritte erreicht.',
        BankErrorCode.DATA_FORMAT: 'Bankdaten konnten nicht verlustfrei verarbeitet werden.',
        BankErrorCode.IDENTIFICATION_FORMAT: 'Gespeicherte Nutzerkennung passt nicht in die FinTS-Anmeldenachricht. Zugang lokal neu hinterlegen.',
        BankErrorCode.PRODUCT_FORMAT: 'Produktangaben passen nicht in die FinTS-Anmeldenachricht.',
        BankErrorCode.AUTH_SETUP_REQUIRED: 'Bankfreigabeverfahren oder Freigabegerät konnte noch nicht eingerichtet werden.',
        BankErrorCode.AUTH_SELECTION_INVALID: 'Lokale Verfahrens- oder Geräteauswahl ist nicht eindeutig verfügbar.',
        BankErrorCode.STATEMENT_INCOMPLETE: 'Vollstaendige Seiten, Monatsgrenzen oder passende Kontrollsalden fehlen; nichts archiviert.',
        BankErrorCode.STATEMENT_FORMAT: 'MT940-Format nicht sicher erkannt; nichts archiviert.',
        BankErrorCode.STATEMENT_ID_MISSING: 'Bank liefert keine Auszugsnummer; fester Monatsabruf erforderlich. Nichts archiviert.',
    }
    code = error.code if isinstance(error.code, BankErrorCode) else BankErrorCode.UNKNOWN
    kind = error.error_kind if isinstance(error.error_kind, BankErrorKind) else BankErrorKind.OTHER
    origin = error.origin if isinstance(error.origin, BankErrorOrigin) else BankErrorOrigin.OTHER
    raw_codes = error.bank_return_codes if isinstance(error.bank_return_codes, tuple) else ()
    bank_codes = tuple(value for value in raw_codes[:16]
                       if isinstance(value, str) and re.fullmatch(r'[0-9]{4}', value))
    details = f' Technik: {kind.name}/{origin.name}; Bankcodes: {", ".join(bank_codes) or "keine"}.'
    return f'{code.name}: {messages.get(code, "Bankabruf fehlgeschlagen; Ursache noch unbekannt.")}{details}'


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats unknown arguments, which may contain secrets.
        raise ProbeError('Ungültige Kommandozeile; --help zeigt die erlaubten Optionen.')


def _registration(path):
    try:
        source = Path(path).resolve(strict=True)
        repository = Path(__file__).resolve().parents[3]
        if source == repository or repository in source.parents or not source.is_file():
            raise ProbeError('Registrierungsdatei muss außerhalb des Repositorys liegen.')
        if source.stat().st_size > 4096:
            raise ProbeError('Registrierungsdatei ist ungültig.')
        data = json.loads(source.read_text(encoding='utf-8'))
    except ProbeError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise ProbeError('Registrierungsdatei ist nicht lesbar oder ungültig.') from None
    required = {'product_id', 'product_version', 'product_name'}
    if not isinstance(data, dict) or not required <= set(data) or \
            not set(data) <= required | {'received_on', 'activation_verified'}:
        raise ProbeError('Registrierungsdatei muss Produktkennung, Version und Name enthalten.')
    product_id = data['product_id']
    if not isinstance(product_id, str) or re.fullmatch(r'[A-Za-z0-9]{25}', product_id) is None:
        raise ProbeError('FinTS-Produktkennung muss exakt 25 ASCII-Buchstaben oder Ziffern enthalten.')
    if data['product_name'] != 'Finance Control' or data['product_version'] != '0.1.0':
        raise ProbeError('Registrierungsname oder Version passt nicht zum FinTS-Adapter.')
    return product_id


def _confirm(prompt):
    return input(prompt).strip() == 'LESEN'


def _respond(challenge):
    # Bank challenge text can contain arbitrary account or authentication data.
    if challenge.decoupled:
        return input('App-Freigabe lokal prüfen; danach FREIGEGEBEN eingeben (sonst Abbruch): ').strip() == 'FREIGEGEBEN'
    return getpass.getpass('TAN verdeckt eingeben (leer = Abbruch): ')


def _choose_option(options, prompt):
    # Names are shown only locally, without device phone numbers or bank payloads.
    for index, option in enumerate(options, 1):
        label = ''.join(char for char in option.label if char.isprintable())[:80]
        print(f'{index}: {label}')
    selection = input(prompt).strip()
    if not selection.isascii() or not selection.isdecimal():
        return None
    index = int(selection) - 1
    return index if 0 <= index < len(options) else None


def _choose_named_option(options, name):
    matches = [index for index, option in enumerate(options)
               if type(getattr(option, 'label', None)) is str and option.label == name]
    if len(matches) != 1:
        raise BankReadError(code=BankErrorCode.AUTH_SELECTION_INVALID, origin=BankErrorOrigin.ADAPTER)
    return matches[0]


def _choose_method(options, name=None):
    if name is not None:
        return _choose_named_option(options, name)
    return _choose_option(options, 'Freigabeverfahren wählen (Nummer, sonst Abbruch): ')


def _choose_medium(options, name=None):
    if name is not None:
        return _choose_named_option(options, name)
    return _choose_option(options, 'Freigabegerät wählen (Nummer, sonst Abbruch): ')


def _masked_account(account):
    reference = ''.join(char for char in (account.iban or account.accountnumber) if char.isalnum())
    return '••••' + reference[-4:] if len(reference) > 4 else '••••'


def _reference_check(bookings):
    ids = [booking.external_id for booking in bookings if booking.external_id]
    missing = len(bookings) - len(ids)
    duplicates = len(ids) - len(set(ids))
    return missing, duplicates


def _print_reference_diagnostics(bookings):
    """Display only a fixed vocabulary; never reference values or source keys."""
    names = ('bank_reference', 'customer_reference', 'end_to_end_reference',
             'additional_position_reference', 'transaction_reference')
    labels = {'keymissing': 'Feld fehlt', 'empty': 'leer', 'placeholder': 'Platzhalter',
              'wrong_type': 'ungeeigneter Datentyp', 'invalid_format': 'Format ungeeignet',
              'present': 'vorhanden; Eindeutigkeit ungeprüft'}
    unresolved = [value for value in bookings if value.external_id is None]
    print(f'Referenzdiagnose: {len(unresolved)} Buchungen ohne verwendbare Bankreferenz.')
    for index, booking in enumerate(unresolved, 1):
        print(f'Buchung ohne Kennung {index}:')
        entry_date = getattr(booking, 'source_entry_date', None)
        value_date = getattr(booking, 'booked_on', None)
        def show_date(value):
            if isinstance(value, date):
                return date(value.year, value.month, value.day).isoformat()
            return 'nicht geliefert'
        amount = getattr(booking, 'amount', None)
        currency = getattr(booking, 'currency', None)
        if isinstance(amount, Decimal) and amount.is_finite() and type(currency) is str and re.fullmatch(r'[A-Z]{3}', currency):
            print(f'  Betrag: {amount} {currency}')
        print(f'  Buchungsdatum laut Bibliothek: {show_date(entry_date)}')
        print(f'  Valutadatum laut Bibliothek: {show_date(value_date)}')
        code = getattr(booking, 'source_booking_code', None)
        code_label = code if type(code) is str and re.fullmatch(r'[A-Z0-9]{1,8}', code) else 'nicht geliefert'
        print(f'  Buchungsart-Code: {code_label} (Bedeutung noch abzugleichen)')
        diagnostics = getattr(booking, 'reference_diagnostics', ())
        statuses = {}
        if type(diagnostics) is tuple:
            for entry in diagnostics:
                if type(entry) is tuple and len(entry) == 2:
                    name, status = entry
                    if type(name) is str and type(status) is str and name in names and status in labels:
                        statuses[name] = status
        for name in names:
            print(f'  {name}: {labels.get(statuses.get(name), "Diagnose nicht verfügbar")}')
    print('Kunden-, End-to-End- und Mandatsreferenzen sind kein automatischer Ersatz für eine Buchungs-ID.')


def _verify_transaction_repeat(first, second):
    """Compare bank reference candidates without exposing or persisting them."""
    if not first or not second:
        print('Wiedererkennung ungeprüft: mindestens ein Abruf enthält keine Buchungen.')
        return
    if any(_reference_check(values) != (0, 0) for values in (first, second)):
        print('Wiedererkennung blockiert: Quellen-IDs fehlen oder sind mehrfach vergeben.')
        return
    def signature(values):
        return {value.external_id: (value.booked_on, value.amount, value.currency) for value in values}
    if signature(first) != signature(second):
        print('Wiedererkennung nicht bestätigt: die beiden Lieferungen unterscheiden sich.')
        return
    print('Wiedererkennung für diesen Zeitraum bestätigt: gleiche eindeutige Bankreferenzen und Buchungswerte.')
    print('Langfristige Stabilität und Abgleich mit Kontoauszug bleiben ungeprüft. Kein Import.')


def _transaction_dates(start_text=None, end_text=None):
    """Read and validate a bounded ISO date range before any transaction request."""
    try:
        if start_text is None:
            start_text = input('Umsatzzeitraum Beginn (JJJJ-MM-TT): ').strip()
            end_text = input('Umsatzzeitraum Ende (JJJJ-MM-TT): ').strip()
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except (ValueError, OverflowError):
        raise ProbeError('Umsatzzeitraum muss aus gültigen ISO-Daten bestehen.') from None
    today = date.today()
    if start.isoformat() != start_text or end.isoformat() != end_text:
        raise ProbeError('Umsatzzeitraum muss im Format JJJJ-MM-TT angegeben werden.')
    if start > end:
        raise ProbeError('Umsatzzeitraum ist nicht chronologisch geordnet.')
    if end > today:
        raise ProbeError('Umsatzzeitraum darf nicht in der Zukunft liegen.')
    if (end - start).days > 365:
        raise ProbeError('Umsatzzeitraum darf höchstens 366 Kalendertage umfassen.')
    return start, end


def _print_transaction_summary(bookings):
    totals = defaultdict(lambda: [Decimal('0'), Decimal('0'), Decimal('0')])
    missing_ids = 0
    for booking in bookings:
        if not isinstance(booking.amount, Decimal) or not booking.amount.is_finite():
            raise ProbeError('Umsatzbetrag ist nicht verlustfrei darstellbar.')
        amount = booking.amount
        if booking.external_id is None or not booking.external_id.strip():
            missing_ids += 1
        if amount > 0:
            totals[booking.currency][0] += amount
        elif amount < 0:
            totals[booking.currency][1] -= amount
        totals[booking.currency][2] += amount
    print(f'Gebuchte Buchungen: {len(bookings)}')
    for currency in sorted(totals):
        incoming, outgoing, net = totals[currency]
        print(f'{currency}: Zugänge {incoming}; Abgänge {outgoing}; Netto {net}')
    print(f'Buchungen ohne Quellen-ID: {missing_ids}')
    _, duplicates = _reference_check(bookings)
    print(f'Mehrfach vergebene Quellen-IDs: {duplicates}')
    if missing_ids:
        print('Import blockiert: Quellen-ID fehlt.')
    elif duplicates:
        print('Import blockiert: Quellen-ID mehrfach vergeben.')
    else:
        print('Bankreferenzen sind Kandidaten; die Probe gibt keinen Import frei.')
    print('Keine Buchungen gespeichert oder importiert. Netto ist die Kontobewegung im Zeitraum, kein Kontostand.')


def _print_auth_challenges(challenges):
    if challenges['tan'] == 0 and challenges['app'] == 0:
        print('Bank hat für diese Abrufe keine TAN-/App-Freigabe angefordert.')
    else:
        print(f'Bankseitig zurückgegebene Freigabeanforderungen: TAN {challenges["tan"]}; '
              f'App-Freigabe {challenges["app"]}.')


def main(argv=None):
    stage = 'Kommandozeile'
    parser = _SafeParser(description='Lokale lesende Bankprobe: Konten und ein Saldo')
    parser.add_argument('--bank', required=True, choices=tuple(PROFILES))
    parser.add_argument('--bank-code', required=True)
    parser.add_argument('--registration-file', required=True)
    parser.add_argument('--alias', required=True)
    parser.add_argument('--tan-method', help='Numerische FinTS-TAN-Verfahrenskennung')
    parser.add_argument('--read-authorized', action='store_true', help='Lokale Freigabe für angeforderte lesende Abrufe setzen')
    parser.add_argument('--method-name', help='Exakte lokale Bezeichnung des Freigabeverfahrens')
    parser.add_argument('--medium-name', help='Exakte lokale Bezeichnung des Freigabegeräts')
    parser.add_argument('--account-suffix', help='Letzte vier ASCII-Buchstaben/Ziffern des maskierten Kontos')
    parser.add_argument('--start-date', help='Umsatzzeitraum Beginn (JJJJ-MM-TT)')
    parser.add_argument('--end-date', help='Umsatzzeitraum Ende (JJJJ-MM-TT)')
    parser.add_argument('--save-credentials', action='store_true')
    parser.add_argument('--check-only', action='store_true', help='Nur lokale Anmeldung prüfen, ohne Bankverbindung')
    parser.add_argument('--transactions', action='store_true', help='Nach dem Saldo eine Umsatzprobe anbieten')
    parser.add_argument('--verify-transactions', action='store_true', help='Umsatzzeitraum nach Bestätigung erneut lesen und Bankreferenzen vergleichen')
    parser.add_argument('--diagnose-references', action='store_true', help='Nur Feldstatus für Buchungen ohne Bankreferenz lokal anzeigen')
    parser.add_argument('--compare-camt', action='store_true', help='Strukturierte CAMT-Umsätze zusätzlich lesend prüfen; kein Import')
    parser.add_argument('--statements', action='store_true', help='Vollständige MT940-Kontoauszüge prüfen und lokal archivieren; kein Ledgerimport')
    parser.add_argument('--monthly-snapshot', action='store_true', help='Festen abgeschlossenen Monat mit lokaler Identitaet pruefen und archivieren')
    parser.add_argument('--statement-directory', help='Privates Auszugsarchiv außerhalb von Git')
    try:
        args = parser.parse_args(argv)
        stage = 'Terminalprüfung'
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ProbeError('Ein lokales interaktives Terminal ist erforderlich.')
        stage = 'Kommandozeile'
        if args.monthly_snapshot and args.statements:
            raise ProbeError('--monthly-snapshot und --statements sind nicht kombinierbar.')
        args.statements = args.statements or args.monthly_snapshot
        if args.statements and (args.check_only or args.transactions or not args.statement_directory):
            raise ProbeError('--statements erfordert ein privates Archiv und ist nicht mit --transactions/--check-only kombinierbar.')
        if args.statement_directory and not args.statements:
            raise ProbeError('--statement-directory erfordert --statements.')
        if args.statements:
            from finance_control.bank_archive import _directory
            root = _directory(args.statement_directory)
            if args.monthly_snapshot:
                from finance_control.monthly_archive import read_monthly_archive as preflight_archive
            else:
                from finance_control.statement_archive import read_statement_archive as preflight_archive
            for candidate in root.glob('*.json'):
                preflight_archive(candidate)
        if args.check_only and args.transactions:
            raise ProbeError('--transactions ist mit --check-only nicht zulässig.')
        if args.verify_transactions and (args.check_only or not args.transactions):
            raise ProbeError('--verify-transactions erfordert --transactions ohne --check-only.')
        if args.diagnose_references and (args.check_only or not args.transactions):
            raise ProbeError('--diagnose-references erfordert --transactions ohne --check-only.')
        if args.compare_camt and (args.check_only or not args.transactions):
            raise ProbeError('--compare-camt erfordert --transactions ohne --check-only.')
        if args.method_name is not None and args.tan_method is not None:
            raise ProbeError('--method-name und --tan-method sind nicht kombinierbar.')
        if (args.start_date is None) != (args.end_date is None):
            raise ProbeError('--start-date und --end-date müssen gemeinsam angegeben werden.')
        if args.start_date is not None and not (args.transactions or args.statements):
            raise ProbeError('--start-date/--end-date erfordern einen Umsatz- oder Auszugsabruf.')
        if args.start_date is not None:
            begin, finish = _transaction_dates(args.start_date, args.end_date)
            if args.monthly_snapshot:
                from calendar import monthrange
                if begin.day != 1 or finish != date(begin.year, begin.month, monthrange(begin.year, begin.month)[1]) or finish >= date.today():
                    raise ProbeError('Monatsabruf erfordert genau einen abgeschlossenen Kalendermonat.')
        elif args.monthly_snapshot:
            raise ProbeError('Monatsabruf erfordert --start-date und --end-date.')
        if args.account_suffix is not None and re.fullmatch(r'[A-Za-z0-9]{4}', args.account_suffix) is None:
            raise ProbeError('--account-suffix muss exakt vier ASCII-Buchstaben oder Ziffern enthalten.')
        if re.fullmatch(r'[0-9]{8}', args.bank_code) is None:
            raise ProbeError('Bankleitzahl muss acht ASCII-Ziffern enthalten.')
        if args.tan_method is not None and re.fullmatch(r'[0-9]{1,8}', args.tan_method) is None:
            raise ProbeError('TAN-Verfahrenskennung muss numerisch sein.')
        WindowsCredentialStore._alias(args.alias)
        stage = 'Registrierung'
        product_id = _registration(args.registration_file)
        stage = 'Credential Manager'
        store = WindowsCredentialStore()
        if args.save_credentials:
            stage = 'Zugangseingabe'
            credentials = Credentials(getpass.getpass('Bank-Benutzerkennung: '),
                                      getpass.getpass('Bank-PIN/Passwort: '))
            stage = 'Zugangsspeicherung'
            store.save(args.alias, credentials)
            print('Zugang im Windows Credential Manager gespeichert.')
        else:
            stage = 'Zugangsladen'
            credentials = store.load(args.alias)
        stage = 'Kontenfreigabe'
        if args.check_only:
            stage = 'Lokale FinTS-Prüfung'
            create_reader(args.bank, args.bank_code, product_id, credentials, _respond,
                          tan_method=args.tan_method)
            print('Lokale FinTS-Anmeldenachricht gültig. Keine Bankverbindung hergestellt.')
            return 0
        if not args.read_authorized and not _confirm('Konten lesend abrufen? LESEN eingeben (sonst Abbruch): '):
            raise ProbeError('Bankabruf abgebrochen.')
        if args.read_authorized:
            print('Lokale Lesefreigabe gesetzt.')
        challenges = {'tan': 0, 'app': 0}
        def respond(challenge):
            challenges['app' if challenge.decoupled else 'tan'] += 1
            return _respond(challenge)
        stage = 'Bankclient-Aufbau'
        reader = create_reader(args.bank, args.bank_code, product_id, credentials, respond,
                               tan_method=args.tan_method)
        stage = 'Freigabeverfahren'
        choose_method = lambda options: _choose_method(options, args.method_name)
        choose_medium = lambda options: _choose_medium(options, args.medium_name)
        if args.method_name is not None or args.medium_name is not None:
            reader.configure_auth(choose_method, choose_medium, force_selection=True)
        else:
            reader.configure_auth(choose_method, choose_medium)
        stage = 'Kontenabruf'
        accounts = reader.read(ReadOperation.ACCOUNTS)
        if not accounts:
            print('Keine Konten gemeldet.')
            _print_auth_challenges(challenges)
            return 0
        for index, account in enumerate(accounts, 1):
            print(f'{index}: {_masked_account(account)}')
        stage = 'Kontenauswahl'
        if args.account_suffix is not None:
            matching = [index for index, account in enumerate(accounts)
                        if ''.join(char for char in (account.iban or account.accountnumber)
                                   if char.isalnum())[-4:] == args.account_suffix]
            if len(matching) != 1:
                raise ProbeError('Maskiertes Konto ist nicht eindeutig verfügbar.')
            selected_index = matching[0]
        else:
            selection = input('Kontonummer aus der Liste wählen (sonst Abbruch): ').strip()
            if not selection.isascii() or not selection.isdecimal() or not 1 <= int(selection) <= len(accounts):
                raise ProbeError('Kontenauswahl abgebrochen oder ungültig.')
            selected_index = int(selection) - 1
        stage = 'Saldofreigabe'
        if not args.read_authorized and not _confirm('Saldo dieses Kontos lesend abrufen? LESEN eingeben (sonst Abbruch): '):
            raise ProbeError('Saldoabruf abgebrochen.')
        stage = 'Saldoabruf'
        selected_account = accounts[selected_index]
        balance = reader.read(ReadOperation.BALANCE, selected_account)
        print(f'Saldo: {balance.amount} {balance.currency}; Stand: {balance.booked_on}')
        if args.statements:
            start, end = _transaction_dates(args.start_date, args.end_date)
            stage = 'Auszugsfreigabe'
            if not args.read_authorized and not _confirm('Vollständige Kontoauszüge lesen und privat archivieren? LESEN eingeben (sonst Abbruch): '):
                raise ProbeError('Auszugsabruf abgebrochen.')
            stage = 'Auszugsabruf'
            operation = ReadOperation.MONTHLY_SNAPSHOT if args.monthly_snapshot else ReadOperation.STATEMENTS
            statements = reader.read(operation, selected_account, start=start, end=end)
            from finance_control.statement_archive import archive_statement
            for index, statement in enumerate(statements, 1):
                stage = 'Auszugsarchivierung'
                if args.monthly_snapshot:
                    from finance_control.monthly_archive import archive_monthly_snapshot
                    archived = archive_monthly_snapshot(statement, args.statement_directory)
                    incoming = sum((r.amount for r in statement.rows if r.amount > 0), Decimal(0))
                    outgoing = -sum((r.amount for r in statement.rows if r.amount < 0), Decimal(0))
                    print(f'Monatsabruf {start} bis {end}: {len(statement.rows)} Posten; lokale Monatskennung, keine Bank-Auszugsnummer.')
                    print(f'{statement.currency}: Zugaenge {incoming}; Abgaenge {outgoing}; Netto {incoming-outgoing}.')
                    print(f'Kontrolle: Anfang {statement.opening_balance} + Netto = Ende {statement.closing_balance}; stimmt.')
                    print('Archiv neu.' if archived.created else 'Archiv bereits identisch vorhanden; keine neue Lieferung.')
                else:
                    archived = archive_statement(statement, args.statement_directory)
                    print(f'Auszug {statement.number}/{statement.year}: {statement.opening_date} bis {statement.closing_date}; {len(statement.rows)} Posten; Anfang {statement.opening_balance} + Bewegungen = Ende {statement.closing_balance} {statement.currency}; Kontrollsaldo stimmt.')
                print(f'Privates Archiv: {archived.path}')
            print(f'Gepruefte Lieferungen: {len(statements)}. Keine Ledgerbuchungen importiert.')
        if args.transactions:
            stage = 'Umsatzzeitraum'
            start, end = _transaction_dates(args.start_date, args.end_date)
            stage = 'Umsatzfreigabe'
            if not args.read_authorized and not _confirm('Umsätze dieses Kontos lesend abrufen? LESEN eingeben (sonst Abbruch): '):
                raise ProbeError('Umsatzabruf abgebrochen.')
            stage = 'Umsatzabruf'
            bookings = reader.read(ReadOperation.TRANSACTIONS, selected_account, start=start, end=end)
            _print_transaction_summary(bookings)
            if args.diagnose_references:
                _print_reference_diagnostics(bookings)
            if args.compare_camt:
                stage = 'CAMT-Freigabe'
                if not args.read_authorized and not _confirm('CAMT-Umsätze zusätzlich lesend abrufen? LESEN eingeben (sonst Abbruch): '):
                    raise ProbeError('CAMT-Abruf abgebrochen.')
                stage = 'CAMT-Abruf'
                try:
                    camt = reader.read(ReadOperation.CAMT_TRANSACTIONS, selected_account, start=start, end=end)
                except BankReadError as exc:
                    if exc.code is not BankErrorCode.UNSUPPORTED:
                        raise
                    print('Die Bank bietet über diesen Zugang keinen unterstützten CAMT-Abruf an. Kein Import.')
                else:
                    print('CAMT: gebuchte Auszugsposten (Sammelbuchungen können mehrere Einzelumsätze enthalten).')
                    _print_transaction_summary(camt)
                    def totals(values):
                        result = defaultdict(lambda: Decimal('0'))
                        for item in values:
                            result[item.currency] += item.amount
                        return dict(result)
                    if bookings and camt and totals(bookings) == totals(camt):
                        print('CAMT und bisheriger Abruf: Netto je Währung stimmt überein; Einzelbuchungsabgleich bleibt offen.')
                    else:
                        print('CAMT und bisheriger Abruf: kein bestätigter Summenabgleich. Kein Import.')
            if args.verify_transactions:
                stage = 'Wiederholungsfreigabe'
                if not args.read_authorized and not _confirm('Denselben Umsatzzeitraum zur Kennungsprüfung erneut lesen? LESEN eingeben (sonst Abbruch): '):
                    raise ProbeError('Wiederholungsabruf abgebrochen.')
                stage = 'Wiederholungsabruf'
                repeated = reader.read(ReadOperation.TRANSACTIONS, selected_account, start=start, end=end)
                _verify_transaction_repeat(bookings, repeated)
        _print_auth_challenges(challenges)
        return 0
    except BankReadError as exc:
        print(f'Phase {stage}: {_bank_diagnostic(exc)}', file=sys.stderr)
        return 1
    except (ProbeError, StatementError) as exc:
        # Only our static, local diagnostics reach this branch.
        print(f'Phase {stage}: {exc}', file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        # Third-party exceptions may contain secrets; report only our fixed stage.
        print(f'Phase {stage}: Vorgang nicht abgeschlossen.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
