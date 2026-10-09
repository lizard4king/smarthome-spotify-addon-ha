"""Narrow read adapter. No raw bank payload is persisted by this module."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import binascii
import logging
import os
import threading

from finance_control.core import money
from .profiles import PROFILES


class BankErrorCode(str, Enum):
    AUTH_REJECTED = 'AUTH_REJECTED'
    ONLINE_LOGIN_REQUIRED = 'ONLINE_LOGIN_REQUIRED'
    CREDENTIALS_REJECTED = 'CREDENTIALS_REJECTED'
    AUTH_TEMPORARY = 'AUTH_TEMPORARY'
    SCA_REQUIRED = 'SCA_REQUIRED'
    UNSUPPORTED = 'UNSUPPORTED'
    CONNECTION = 'CONNECTION'
    TIMEOUT = 'TIMEOUT'
    TLS = 'TLS'
    DIALOG_INIT = 'DIALOG_INIT'
    NO_RESPONSE = 'NO_RESPONSE'
    BANK_REJECTED = 'BANK_REJECTED'
    UNKNOWN = 'UNKNOWN'
    LOCAL_AUTH_ABORT = 'LOCAL_AUTH_ABORT'
    GRAPHICAL_TAN = 'GRAPHICAL_TAN'
    TAN_LIMIT = 'TAN_LIMIT'
    DATA_FORMAT = 'DATA_FORMAT'
    IDENTIFICATION_FORMAT = 'IDENTIFICATION_FORMAT'
    PRODUCT_FORMAT = 'PRODUCT_FORMAT'
    AUTH_SETUP_REQUIRED = 'AUTH_SETUP_REQUIRED'
    AUTH_SELECTION_INVALID = 'auth_selection_invalid'
    STATEMENT_INCOMPLETE = 'STATEMENT_INCOMPLETE'
    STATEMENT_FORMAT = 'STATEMENT_FORMAT'
    STATEMENT_ID_MISSING = 'STATEMENT_ID_MISSING'


class BankErrorKind(str, Enum):
    VALUE_ERROR = 'VALUE_ERROR'
    TYPE_ERROR = 'TYPE_ERROR'
    ATTRIBUTE_ERROR = 'ATTRIBUTE_ERROR'
    KEY_ERROR = 'KEY_ERROR'
    NOT_IMPLEMENTED = 'NOT_IMPLEMENTED'
    RUNTIME_ERROR = 'RUNTIME_ERROR'
    BINASCII_ERROR = 'BINASCII_ERROR'
    OTHER = 'OTHER'


class BankErrorOrigin(str, Enum):
    SYSTEM_SYNC = 'SYSTEM_SYNC'
    DIALOG_INIT = 'DIALOG_INIT'
    TRANSPORT = 'TRANSPORT'
    ACCOUNTS = 'ACCOUNTS'
    ADAPTER = 'ADAPTER'
    OTHER = 'OTHER'


_SAFE_ERROR_MESSAGES = {
    BankErrorCode.AUTH_REJECTED: 'Bankanmeldung abgelehnt; Ursache prüfen.',
    BankErrorCode.ONLINE_LOGIN_REQUIRED: 'ING verlangt eine erneute Anmeldung auf ING.de.',
    BankErrorCode.CREDENTIALS_REJECTED: 'ING hat die Anmeldedaten abgelehnt.',
    BankErrorCode.AUTH_TEMPORARY: 'Bankanmeldung vorübergehend gesperrt.',
    BankErrorCode.SCA_REQUIRED: 'Zusätzliche starke Authentifizierung erforderlich.',
    BankErrorCode.UNSUPPORTED: 'Bank unterstützt diese Leseoperation nicht.',
    BankErrorCode.CONNECTION: 'Keine Verbindung zum Bankendpunkt möglich.',
    BankErrorCode.TIMEOUT: 'Zeitüberschreitung beim Bankendpunkt.',
    BankErrorCode.TLS: 'Sichere TLS-Verbindung zum Bankendpunkt fehlgeschlagen.',
    BankErrorCode.DIALOG_INIT: 'FinTS-Dialog konnte nicht gestartet werden.',
    BankErrorCode.NO_RESPONSE: 'Keine verwertbare Antwort vom Bankendpunkt erhalten.',
    BankErrorCode.BANK_REJECTED: 'Bank hat die Anfrage abgelehnt; Ursache prüfen.',
    BankErrorCode.UNKNOWN: 'Bankvorgang nicht abgeschlossen; Ursache unbekannt.',
    BankErrorCode.LOCAL_AUTH_ABORT: 'Lokale Authentifizierung abgebrochen.',
    BankErrorCode.GRAPHICAL_TAN: 'Grafisches TAN-Verfahren benötigt eine lokale Anzeige.',
    BankErrorCode.TAN_LIMIT: 'Zu viele Authentifizierungsschritte; Vorgang beendet.',
    BankErrorCode.DATA_FORMAT: 'Bankdaten liegen nicht im unterstützten exakten Format vor.',
    BankErrorCode.IDENTIFICATION_FORMAT: 'FinTS-Kennung hat ein ungültiges Format.',
    BankErrorCode.PRODUCT_FORMAT: 'FinTS-Produktdaten haben ein ungültiges Format.',
    BankErrorCode.AUTH_SETUP_REQUIRED: 'Authentifizierungsverfahren konnte nicht sicher eingerichtet werden.',
    BankErrorCode.AUTH_SELECTION_INVALID: 'Lokale Verfahrens- oder Geräteauswahl ist nicht eindeutig verfügbar.',
    BankErrorCode.STATEMENT_INCOMPLETE: 'Vollstaendige Seiten, Zeitraumbasis oder passende Kontrollsalden fehlen.',
    BankErrorCode.STATEMENT_FORMAT: 'Das gelieferte MT940-Format wird nicht sicher verarbeitet.',
    BankErrorCode.STATEMENT_ID_MISSING: 'Bankseitige Auszugsnummer fehlt; festen Monatsabruf verwenden.',
}


class BankReadError(RuntimeError):
    def __init__(self, message=None, code=BankErrorCode.UNKNOWN, *,
                 error_kind=BankErrorKind.OTHER, origin=BankErrorOrigin.OTHER, bank_return_codes=()):
        self.code = code if isinstance(code, BankErrorCode) else BankErrorCode.UNKNOWN
        self.error_kind = error_kind if isinstance(error_kind, BankErrorKind) else BankErrorKind.OTHER
        self.origin = origin if isinstance(origin, BankErrorOrigin) else BankErrorOrigin.OTHER
        self.bank_return_codes = _valid_bank_return_codes(bank_return_codes)
        super().__init__(message or _SAFE_ERROR_MESSAGES[self.code])


def _valid_bank_return_codes(values):
    valid = []
    for value in values if isinstance(values, (tuple, list)) else ():
        if (type(value) is str and len(value) == 4 and value.isascii() and value.isdigit()
                and value not in valid):
            valid.append(value)
        if len(valid) == 16:
            break
    return tuple(valid)


_ING_9942_MESSAGES = {
    'bitte erneuern sie ihre authentifizierung. einfach unter ing.de einloggen.':
        BankErrorCode.ONLINE_LOGIN_REQUIRED,
    'log-in fehlgeschlagen.': BankErrorCode.CREDENTIALS_REJECTED,
    'log-in fehlgeschlagen. 3 fehlversuche führen zur sperrung. entsperren auf ing.de':
        BankErrorCode.CREDENTIALS_REJECTED,
    'log-in fehlgeschlagen. 3 fehlversuche führen zur sperrung. entsperren auf ing.de.':
        BankErrorCode.CREDENTIALS_REJECTED,
    'pin ungültig.': BankErrorCode.CREDENTIALS_REJECTED,
}


def _ing_9942_error_code(text):
    """Classify only complete known ING text; never retain or expose bank text."""
    if type(text) is not str:
        return None
    return _ING_9942_MESSAGES.get(' '.join(text.casefold().split()))


def _error_kind(error):
    if isinstance(error, binascii.Error):
        return BankErrorKind.BINASCII_ERROR
    for error_type, kind in (
        (ValueError, BankErrorKind.VALUE_ERROR),
        (TypeError, BankErrorKind.TYPE_ERROR),
        (AttributeError, BankErrorKind.ATTRIBUTE_ERROR),
        (KeyError, BankErrorKind.KEY_ERROR),
        (NotImplementedError, BankErrorKind.NOT_IMPLEMENTED),
        (RuntimeError, BankErrorKind.RUNTIME_ERROR),
    ):
        if isinstance(error, error_type):
            return kind
    return BankErrorKind.OTHER


def _error_origin(error):
    import fints
    package_root = os.path.realpath(os.path.dirname(fints.__file__))
    fints_origins = {
        '_ensure_system_id': BankErrorOrigin.SYSTEM_SYNC,
        'init': BankErrorOrigin.DIALOG_INIT,
        'send': BankErrorOrigin.TRANSPORT,
        'get_sepa_accounts': BankErrorOrigin.ACCOUNTS,
        '_continue_get_sepa_accounts': BankErrorOrigin.ACCOUNTS,
    }
    adapter_codes = {
        ReadOnlyFinTS.read.__code__, ReadOnlyFinTS._resolve.__code__,
        ReadOnlyFinTS.configure_auth.__code__, create_reader.__code__,
    }
    origin = BankErrorOrigin.OTHER
    traceback_node = error.__traceback__
    while traceback_node is not None:
        frame = traceback_node.tb_frame
        if frame.f_code in adapter_codes:
            origin = BankErrorOrigin.ADAPTER
        elif frame.f_code.co_name in fints_origins:
            filename = os.path.realpath(frame.f_code.co_filename)
            try:
                inside_fints = os.path.commonpath((package_root, filename)) == package_root
            except ValueError:
                inside_fints = False
            if inside_fints:
                origin = fints_origins[frame.f_code.co_name]
        traceback_node = traceback_node.tb_next
    return origin


def _classified_error(error, bank_return_codes=()):
    """Return a safe diagnostic code without retaining third-party error text."""
    from fints.exceptions import (
        FinTSClientError, FinTSClientPINError, FinTSClientTemporaryAuthError,
        FinTSConnectionError, FinTSDialogInitError, FinTSNoResponseError,
        FinTSSCARequiredError, FinTSUnsupportedOperation,
    )
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import SSLError, Timeout

    # FinTS 5.0 wraps arbitrary dialog-init exceptions. Unwrap only our own
    # safe adapter error, and only one level; never inspect foreign causes.
    if isinstance(error, FinTSDialogInitError) and isinstance(error.__cause__, BankReadError):
        return _classified_error(error.__cause__, bank_return_codes)

    if isinstance(error, BankReadError):
        code = error.code if isinstance(error.code, BankErrorCode) else BankErrorCode.UNKNOWN
        kind = error.error_kind if isinstance(error.error_kind, BankErrorKind) else BankErrorKind.OTHER
        origin = error.origin if isinstance(error.origin, BankErrorOrigin) else BankErrorOrigin.OTHER
        if origin is BankErrorOrigin.OTHER:
            origin = _error_origin(error)
        codes = _valid_bank_return_codes((*error.bank_return_codes, *_valid_bank_return_codes(bank_return_codes)))
        # Preserve only messages this module itself emitted for that code.
        if error.args == (_SAFE_ERROR_MESSAGES[code],):
            return BankReadError(_SAFE_ERROR_MESSAGES[code], code, error_kind=kind,
                                 origin=origin, bank_return_codes=codes)
        return BankReadError(code=code, error_kind=kind, origin=origin, bank_return_codes=codes)
    if isinstance(error, FinTSClientPINError):
        code = BankErrorCode.AUTH_REJECTED
    elif isinstance(error, FinTSClientTemporaryAuthError):
        code = BankErrorCode.AUTH_TEMPORARY
    elif isinstance(error, FinTSSCARequiredError):
        code = BankErrorCode.SCA_REQUIRED
    elif isinstance(error, FinTSUnsupportedOperation):
        code = BankErrorCode.UNSUPPORTED
    elif isinstance(error, SSLError):
        code = BankErrorCode.TLS
    elif isinstance(error, Timeout):
        code = BankErrorCode.TIMEOUT
    elif isinstance(error, (RequestsConnectionError, ConnectionError, FinTSConnectionError)):
        code = BankErrorCode.CONNECTION
    elif isinstance(error, FinTSDialogInitError):
        code = BankErrorCode.DIALOG_INIT
    elif isinstance(error, FinTSNoResponseError):
        code = BankErrorCode.NO_RESPONSE
    elif isinstance(error, FinTSClientError):
        code = BankErrorCode.BANK_REJECTED
    elif any(value.startswith('9') for value in _valid_bank_return_codes(bank_return_codes)):
        # FinTS 9xxx replies indicate a bank error; do not infer its cause or expose text.
        code = BankErrorCode.BANK_REJECTED
    else:
        code = BankErrorCode.UNKNOWN
    return BankReadError(code=code, error_kind=_error_kind(error), origin=_error_origin(error),
                         bank_return_codes=bank_return_codes)


class ReadOperation(Enum):
    ACCOUNTS = 'accounts'
    BALANCE = 'balance'
    TRANSACTIONS = 'transactions'
    CAMT_TRANSACTIONS = 'camt_transactions'
    STATEMENTS = 'statements'
    MONTHLY_SNAPSHOT = 'monthly_snapshot'
    PERIOD_SNAPSHOT = 'period_snapshot'
    HOLDINGS = 'holdings'


@dataclass(frozen=True)
class AccountRef:
    iban: str = field(repr=False)
    bic: str = field(repr=False)
    accountnumber: str = field(repr=False)
    subaccount: str = field(repr=False)
    blz: str = field(repr=False)


@dataclass(frozen=True)
class Balance:
    amount: Decimal
    currency: str
    booked_on: date


@dataclass(frozen=True)
class Booking:
    # Missing / unstable source IDs must be resolved before ledger import.
    external_id: str | None = field(repr=False)
    amount: Decimal
    currency: str
    booked_on: date
    reference_diagnostics: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)
    source_entry_date: date | None = field(default=None, repr=False, compare=False)
    source_booking_code: str | None = field(default=None, repr=False, compare=False)


def _source_entry_date(value):
    """Keep only a date value suitable for diagnostics; ignore datetimes."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return date(value.year, value.month, value.day)
    return None


def _source_booking_code(value):
    """Accept only the library's bounded ASCII transaction code form."""
    import re
    if type(value) is str and re.fullmatch(r'[A-Z0-9]{1,8}', value, flags=re.ASCII):
        return value
    return None


_BANK_REFERENCE_PLACEHOLDERS = frozenset({
    'NONREF', 'NOTPROVIDED', 'NOT PROVIDED', 'N/A', 'NONE', 'NULL', 'UNKNOWN',
})
_REFERENCE_DIAGNOSTIC_FIELDS = (
    'bank_reference', 'customer_reference', 'end_to_end_reference',
    'additional_position_reference', 'transaction_reference',
)


def _reference_status(data, name):
    """Classify one whitelisted source field without retaining or formatting its value."""
    if name not in data:
        return 'keymissing'
    value = data[name]
    if value is None or (type(value) is str and value == ''):
        return 'empty'
    # Check placeholders before format validity: e.g. NOT PROVIDED is a placeholder.
    if type(value) is str and (value.upper() in _BANK_REFERENCE_PLACEHOLDERS
                               or all(character == '0' for character in value)):
        return 'placeholder'
    if type(value) is not str:
        return 'wrong_type'
    if (len(value) > 251 or any(character.isspace() or not character.isprintable()
                                for character in value)):
        return 'invalid_format'
    return 'present'


def _reference_diagnostics(data):
    return tuple((name, _reference_status(data, name)) for name in _REFERENCE_DIAGNOSTIC_FIELDS)


def _bank_reference_candidate(value):
    """Return only a well-formed bank reference candidate; stability is unverified."""
    if type(value) is not str or not value or len(value) > 251:
        return None
    if any(character.isspace() or not character.isprintable() for character in value):
        return None
    if value.upper() in _BANK_REFERENCE_PLACEHOLDERS or all(character == '0' for character in value):
        return None
    return 'bank:' + value


@dataclass(frozen=True)
class Holding:
    isin: str
    pieces: Decimal
    total_value: Decimal
    value_symbol: str
    valuation_date: date


@dataclass(frozen=True)
class Challenge:
    text: str = field(repr=False)
    decoupled: bool


@dataclass(frozen=True)
class AuthOption:
    key: str
    label: str


_session_lock = threading.RLock()


def _exact_quantity(value):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BankReadError(code=BankErrorCode.DATA_FORMAT)
    return value


@contextmanager
def _without_bank_logs():
    # Current synchronous adapter: suppress Python logging while handling secrets.
    # A future concurrent GUI must use an isolated banking worker process.
    with _session_lock:
        previous = logging.root.manager.disable
        logging.disable(max(previous, 2**63 - 1))
        try:
            yield
        finally:
            logging.disable(previous)


def _safe_auth_label(value, fallback):
    if type(value) is not str:
        return fallback
    label = ''.join(character for character in value if character.isprintable())[:80]
    return label or fallback


def _auth_options(items, label_getter, fallback):
    options = []
    values = []
    for key, value in items:
        if (type(key) is not str or not key or len(key) > 20 or not key.isascii()
                or not key.isprintable()):
            continue
        options.append(AuthOption(key, _safe_auth_label(label_getter(value), fallback)))
        values.append((key, value))
    return tuple(options), tuple(values)


def _selected_index(chooser, options, *, force=False):
    if len(options) == 1 and not force:
        return 0
    if not callable(chooser):
        raise BankReadError(code=BankErrorCode.AUTH_SETUP_REQUIRED)
    selected = chooser(options)
    if selected is None:
        raise BankReadError(code=BankErrorCode.LOCAL_AUTH_ABORT)
    if type(selected) is not int or not 0 <= selected < len(options):
        raise BankReadError(code=BankErrorCode.AUTH_SETUP_REQUIRED)
    return selected


class ReadOnlyFinTS:
    def __init__(self, client, respond, preferred_tan_method=None, *, allow_ing_single_step=False):
        self._client = client
        self._respond = respond
        self._preferred_tan_method = preferred_tan_method
        self._allow_ing_single_step = allow_ing_single_step
        self._auth_configured = not getattr(type(client), '_requires_auth_configuration', False)

    def configure_auth(self, choose_method, choose_medium, *, force_selection=False):
        """Explicitly select supported local FinTS authentication options."""
        if self._allow_ing_single_step:
            # ING checks the second factor via its recent online login. The factory
            # enables this path only for its exact profile and bank identifier.
            with _without_bank_logs():
                try:
                    if (self._preferred_tan_method not in (None, '999')
                            or self._client.selected_tan_medium is not None or force_selection):
                        raise BankReadError(code=BankErrorCode.AUTH_SELECTION_INVALID,
                                            origin=BankErrorOrigin.ADAPTER)
                    if self._auth_configured:
                        return
                    self._client.set_tan_mechanism('999')
                    self._auth_configured = True
                    return
                except Exception as error:
                    raise _classified_error(error) from None

        if self._auth_configured:
            return
        if not getattr(type(self._client), '_requires_auth_configuration', False):
            self._auth_configured = True
            return

        with _without_bank_logs():
            try:
                client = self._client
                if getattr(type(client), '_captures_finance_response_codes', False):
                    client._finance_response_codes = ()
                methods = client.get_tan_mechanisms()
                if not methods:
                    client.fetch_tan_mechanisms()
                    methods = client.get_tan_mechanisms()

                eligible_methods = tuple(
                    (key, value) for key, value in methods.items()
                    if (key != '999' and type(key) is str and key.isascii() and key.isprintable()
                        and getattr(value, 'tan_process', None) == '2')
                )
                method_options, method_values = _auth_options(
                    eligible_methods, lambda value: getattr(value, 'name', None), 'FinTS-Verfahren')
                if not method_values:
                    raise BankReadError(code=BankErrorCode.AUTH_SETUP_REQUIRED)

                method_keys = tuple(key for key, _ in method_values)
                if self._preferred_tan_method in method_keys:
                    selected_method = method_keys.index(self._preferred_tan_method)
                elif len(method_values) == 1 and not force_selection:
                    selected_method = 0
                else:
                    selected_method = _selected_index(choose_method, method_options, force=force_selection)
                chosen_method = method_values[selected_method][0]
                if client.get_current_tan_mechanism() != chosen_method:
                    client.set_tan_mechanism(chosen_method)

                medium_required = client.is_tan_media_required()
                if medium_required and client.selected_tan_medium is None:
                    _, media = client.get_tan_media()
                    media_items = tuple((str(index), item) for index, item in enumerate(media))
                    media_options, media_values = _auth_options(
                        media_items, lambda value: getattr(value, 'tan_medium_name', None), 'TAN-Medium')
                    if not media_values:
                        from fints.client import NeedTANResponse
                        challenge = client.init_tan_response
                        if isinstance(challenge, NeedTANResponse) and challenge.decoupled:
                            client.selected_tan_medium = ''
                        else:
                            raise BankReadError(code=BankErrorCode.AUTH_SETUP_REQUIRED)
                    else:
                        selected_medium = _selected_index(choose_medium, media_options, force=force_selection)
                        medium = media_values[selected_medium][1]
                        medium_name = getattr(medium, 'tan_medium_name', None)
                        if type(medium_name) is not str or not medium_name or len(medium_name) > 32:
                            raise BankReadError(code=BankErrorCode.AUTH_SETUP_REQUIRED)
                        client.set_tan_medium(medium)

                if medium_required and client.selected_tan_medium is None:
                    raise BankReadError(code=BankErrorCode.AUTH_SETUP_REQUIRED)

                self._auth_configured = True
            except Exception as error:
                raise _classified_error(
                    error, getattr(self._client, '_finance_response_codes', ())) from None

    def _resolve(self, result):
        from fints.client import NeedTANResponse
        for _ in range(5):
            if not isinstance(result, NeedTANResponse):
                return result
            # Graphical challenges need a dedicated local UI, never a raw dump.
            if result.challenge_matrix or result.challenge_hhduc:
                raise BankReadError(code=BankErrorCode.GRAPHICAL_TAN)
            answer = self._respond(Challenge(result.challenge or '', bool(result.decoupled)))
            if result.decoupled:
                if answer is not True:
                    raise BankReadError(code=BankErrorCode.LOCAL_AUTH_ABORT)
                answer = ''
            elif not isinstance(answer, str) or not answer.strip():
                raise BankReadError(code=BankErrorCode.LOCAL_AUTH_ABORT)
            result = self._client.send_tan(result, answer)
        if not isinstance(result, NeedTANResponse):
            return result
        raise BankReadError(code=BankErrorCode.TAN_LIMIT)

    def read(self, operation, account=None, start=None, end=None):
        if getattr(type(self._client), '_captures_finance_response_codes', False):
            self._client._finance_response_codes = ()
        if not isinstance(operation, ReadOperation):
            raise BankReadError('Nur definierte Leseoperationen sind erlaubt.')
        if operation is not ReadOperation.ACCOUNTS and not isinstance(account, AccountRef):
            raise BankReadError('Geprüfte Kontoreferenz erforderlich.')
        if operation in (ReadOperation.TRANSACTIONS, ReadOperation.CAMT_TRANSACTIONS, ReadOperation.STATEMENTS, ReadOperation.MONTHLY_SNAPSHOT, ReadOperation.PERIOD_SNAPSHOT):
            if type(start) is not date or type(end) is not date or start > end:
                raise BankReadError('Gültiger Buchungszeitraum erforderlich.')
        if operation is ReadOperation.MONTHLY_SNAPSHOT:
            from calendar import monthrange
            if (start.day != 1 or end != date(start.year, start.month, monthrange(start.year, start.month)[1]) or end >= date.today()):
                raise BankReadError('Ein abgeschlossener voller Kalendermonat ist erforderlich.')
        if operation is ReadOperation.PERIOD_SNAPSHOT:
            if (start.day != 1 or (start.year, start.month) != (end.year, end.month)
                    or end > date.today()):
                raise BankReadError(code=BankErrorCode.STATEMENT_INCOMPLETE)
            if getattr(self._client, '_finance_source_profile', None) not in ('ING', 'POSTBANK', 'NASPA'):
                raise BankReadError(code=BankErrorCode.UNSUPPORTED)
        from fints.models import SEPAAccount
        native = None if account is None else SEPAAccount(
            account.iban, account.bic, account.accountnumber, account.subaccount, account.blz)
        if not self._auth_configured:
            raise BankReadError(code=BankErrorCode.AUTH_SETUP_REQUIRED)
        with _without_bank_logs():
            try:
                with self._client:
                    self._resolve(self._client.init_tan_response)
                    if operation is ReadOperation.ACCOUNTS:
                        values = self._resolve(self._client.get_sepa_accounts())
                        return tuple(AccountRef(*value) for value in values)
                    if operation is ReadOperation.BALANCE:
                        value = self._resolve(self._client.get_balance(native))
                        return Balance(money(value.amount.amount), value.amount.currency, value.date)
                    if operation in (ReadOperation.STATEMENTS, ReadOperation.MONTHLY_SNAPSHOT, ReadOperation.PERIOD_SNAPSHOT):
                        from .mt940_statements import parse_mt940_statements, parse_monthly_snapshot
                        from finance_control.statement_model import StatementError
                        if not getattr(type(self._client), '_captures_finance_statement_bytes', False):
                            raise BankReadError(code=BankErrorCode.UNSUPPORTED)
                        self._client._finance_statement_bytes = None
                        self._client._finance_capture_statements = True
                        try:
                            self._resolve(self._client.get_transactions(
                                native, start_date=start, end_date=end, include_pending=False))
                            raw = self._client._finance_statement_bytes
                            if raw is None:
                                raise BankReadError(code=BankErrorCode.NO_RESPONSE)
                            try:
                                if operation is ReadOperation.PERIOD_SNAPSHOT:
                                    from .ing_period_snapshot import parse_bank_period
                                    statements = (parse_bank_period(
                                        raw, source_profile=self._client._finance_source_profile,
                                        start=start, end=end),)
                                elif operation is ReadOperation.MONTHLY_SNAPSHOT:
                                    statements = (parse_monthly_snapshot(raw, source_profile=self._client._finance_source_profile, start=start, end=end),)
                                else:
                                    statements = parse_mt940_statements(raw, source_profile=self._client._finance_source_profile)
                            except StatementError as error:
                                reasons = {'FORMAT': BankErrorCode.STATEMENT_FORMAT,
                                           'IDENTITY_MISSING': BankErrorCode.STATEMENT_ID_MISSING}
                                raise BankReadError(code=reasons.get(getattr(error, 'reason', None), BankErrorCode.STATEMENT_INCOMPLETE)) from None
                            for statement in statements:
                                source = statement.source_account
                                parts = source.split('/')
                                matches = source == account.iban or source == account.accountnumber
                                if len(parts) == 2:
                                    matches = matches or (parts[0] == account.blz and parts[1].lstrip('0') == account.accountnumber.lstrip('0'))
                                if not matches:
                                    raise BankReadError(code=BankErrorCode.DATA_FORMAT)
                            return statements
                        finally:
                            self._client._finance_capture_statements = False
                            self._client._finance_statement_bytes = None
                    if operation is ReadOperation.TRANSACTIONS:
                        values = self._resolve(self._client.get_transactions(
                            native, start_date=start, end_date=end, include_pending=False))
                        bookings = []
                        for value in values:
                            data = value.data
                            external_id = _bank_reference_candidate(data.get('bank_reference'))
                            source_entry_date = (_source_entry_date(data.get('entry_date'))
                                                 if external_id is None else None)
                            source_booking_code = (_source_booking_code(data.get('id'))
                                                   if external_id is None else None)
                            bookings.append(Booking(
                                external_id, money(data['amount'].amount), data['amount'].currency,
                                data['date'],
                                _reference_diagnostics(data) if external_id is None else (),
                                source_entry_date, source_booking_code))
                        return tuple(bookings)
                    if operation is ReadOperation.CAMT_TRANSACTIONS:
                        from .camt_bookings import parse_booked_camt
                        streams = self._resolve(self._client.get_transactions_xml(
                            native, start_date=start, end_date=end))
                        if not isinstance(streams, (tuple, list)) or len(streams) != 2:
                            raise BankReadError(code=BankErrorCode.DATA_FORMAT)
                        return parse_booked_camt(streams[0])
                    values = self._resolve(self._client.get_holdings(native))
                    return tuple(Holding(value.ISIN, _exact_quantity(value.pieces), money(value.total_value),
                                         value.value_symbol, value.valuation_date) for value in values)
            except Exception as error:
                # Do not propagate third-party exceptions, bank text, PINs or TANs.
                raise _classified_error(
                    error, getattr(self._client, '_finance_response_codes', ())) from None


def create_reader(bank, bank_code, product_id, credentials, respond, tan_method=None, tan_medium=None):
    """Construct only; the first read initiates network traffic. No cached state."""
    import re
    profile = PROFILES.get(bank)
    if profile is None or profile.endpoint is None:
        raise BankReadError('Bankendpunkt ist noch nicht verifiziert.')
    if not isinstance(product_id, str) or re.fullmatch(r'[A-Za-z0-9]{25}', product_id) is None:
        raise BankReadError('FinTS-Produktkennung muss aus exakt 25 ASCII-Buchstaben oder Ziffern bestehen.')
    if not isinstance(bank_code, str) or not re.fullmatch(r'[0-9]{8}', bank_code):
        raise BankReadError('Gültige Bankleitzahl erforderlich.')
    from fints.client import FinTS3PinTanClient
    from fints.formals import Language2, SystemIDStatus
    from fints.segments.auth import HKIDN2, HKVVB3
    from requests import Session

    class DiagnosticFinTSClient(FinTS3PinTanClient):
        _captures_finance_response_codes = True
        _requires_auth_configuration = True
        _captures_finance_statement_bytes = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._finance_response_codes = ()
            self._finance_source_profile = bank
            self._finance_capture_statements = False
            self._finance_statement_bytes = None

        def _fetch_with_touchdowns(self, dialog, segment_factory, response_processor, *args, **kwargs):
            if self._finance_capture_statements and args == ('HIKAZ',):
                original = response_processor
                def capture(responses):
                    chunks = []
                    total = 0
                    for segment in responses:
                        raw = segment.statement_booked
                        if type(raw) is not bytes or len(chunks) >= 100000:
                            raise BankReadError(code=BankErrorCode.DATA_FORMAT)
                        total += len(raw)
                        if total > 16 * 1024 * 1024:
                            raise BankReadError(code=BankErrorCode.DATA_FORMAT)
                        chunks.append(raw)
                    self._finance_statement_bytes = b''.join(chunks)
                    return original(responses)
                response_processor = capture
            return super()._fetch_with_touchdowns(dialog, segment_factory, response_processor, *args, **kwargs)

        def _process_response(self, dialog, segment, response):
            code = getattr(response, 'code', None)
            codes = self._finance_response_codes
            if (type(code) is str and len(code) == 4 and code.isascii() and code.isdigit()
                    and code not in codes and len(codes) < 16):
                self._finance_response_codes = (*codes, code)
            if (self._finance_source_profile == 'ING' and bank_code == '50010517'
                    and type(code) is str and code == '9942'):
                diagnostic = _ing_9942_error_code(getattr(response, 'text', None))
                if diagnostic is not None:
                    if self.pin:
                        self.pin.block()
                    raise BankReadError(code=diagnostic,
                                        bank_return_codes=self._finance_response_codes)
            return super()._process_response(dialog, segment, response)

    class RestrictedSession(Session):
        def request(self, method, url, **kwargs):
            if method.upper() != 'POST' or url != profile.endpoint:
                raise BankReadError('Unerwartetes Bankziel oder HTTP-Verfahren.')
            kwargs['timeout'] = (10, 30)
            kwargs['allow_redirects'] = False
            kwargs['verify'] = True
            return super().request(method, url, **kwargs)

    with _without_bank_logs():
        try:
            client = DiagnosticFinTSClient(bank_code, credentials.username, credentials.pin,
                                           profile.endpoint, product_id=product_id, product_version='0.1.0',
                                           tan_medium=tan_medium)
            try:
                HKIDN2(client.bank_identifier, client.customer_id, client.system_id,
                       SystemIDStatus.ID_NECESSARY)
            except (ValueError, TypeError) as error:
                raise BankReadError(code=BankErrorCode.IDENTIFICATION_FORMAT,
                                    error_kind=_error_kind(error)) from None
            try:
                HKVVB3(client.bpd_version, client.upd_version, Language2.DE,
                       client.product_name, client.product_version)
            except (ValueError, TypeError) as error:
                raise BankReadError(code=BankErrorCode.PRODUCT_FORMAT,
                                    error_kind=_error_kind(error)) from None
            client.connection.session.close()
            client.connection.session = RestrictedSession()
            return ReadOnlyFinTS(
                client, respond, preferred_tan_method=tan_method,
                allow_ing_single_step=(bank == 'ING' and bank_code == '50010517'))
        except Exception as error:
            raise _classified_error(error) from None
