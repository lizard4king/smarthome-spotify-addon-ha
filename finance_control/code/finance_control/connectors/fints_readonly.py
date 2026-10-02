"""Narrow read adapter. No raw bank payload is persisted by this module."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
import logging
import threading

from finance_control.core import money
from .profiles import PROFILES


class BankReadError(RuntimeError):
    pass


class ReadOperation(Enum):
    ACCOUNTS = 'accounts'
    BALANCE = 'balance'
    TRANSACTIONS = 'transactions'
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


_session_lock = threading.RLock()


def _exact_quantity(value):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BankReadError('Depotstückzahl liegt nicht verlustfrei als Decimal vor.')
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


class ReadOnlyFinTS:
    def __init__(self, client, respond):
        self._client = client
        self._respond = respond

    def _resolve(self, result):
        from fints.client import NeedTANResponse
        for _ in range(5):
            if not isinstance(result, NeedTANResponse):
                return result
            # Graphical challenges need a dedicated local UI, never a raw dump.
            if result.challenge_matrix or result.challenge_hhduc:
                raise BankReadError('Grafisches TAN-Verfahren benötigt eine lokale Anzeige.')
            answer = self._respond(Challenge(result.challenge or '', bool(result.decoupled)))
            if result.decoupled:
                if answer is not True:
                    raise BankReadError('App-Freigabe abgebrochen.')
                answer = ''
            elif not isinstance(answer, str) or not answer.strip():
                raise BankReadError('TAN-Eingabe abgebrochen.')
            result = self._client.send_tan(result, answer)
        if not isinstance(result, NeedTANResponse):
            return result
        raise BankReadError('Zu viele Authentifizierungsschritte; Vorgang beendet.')

    def read(self, operation, account=None, start=None, end=None):
        if not isinstance(operation, ReadOperation):
            raise BankReadError('Nur definierte Leseoperationen sind erlaubt.')
        if operation is not ReadOperation.ACCOUNTS and not isinstance(account, AccountRef):
            raise BankReadError('Geprüfte Kontoreferenz erforderlich.')
        if operation is ReadOperation.TRANSACTIONS:
            if type(start) is not date or type(end) is not date or start > end:
                raise BankReadError('Gültiger Buchungszeitraum erforderlich.')
        from fints.models import SEPAAccount
        native = None if account is None else SEPAAccount(
            account.iban, account.bic, account.accountnumber, account.subaccount, account.blz)
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
                    if operation is ReadOperation.TRANSACTIONS:
                        values = self._resolve(self._client.get_transactions(
                            native, start_date=start, end_date=end, include_pending=False))
                        return tuple(Booking(
                            None, money(value.data['amount'].amount), value.data['amount'].currency,
                            value.data['date']) for value in values)
                    values = self._resolve(self._client.get_holdings(native))
                    return tuple(Holding(value.ISIN, _exact_quantity(value.pieces), money(value.total_value),
                                         value.value_symbol, value.valuation_date) for value in values)
            except Exception:
                # Do not propagate third-party exceptions, bank text, PINs or TANs.
                raise BankReadError('Bankabruf nicht abgeschlossen; lokale Einrichtung und Freigabe prüfen.') from None


def create_reader(bank, bank_code, product_id, credentials, respond, tan_method=None, tan_medium=None):
    """Construct only; the first read initiates network traffic. No cached state."""
    import re
    profile = PROFILES.get(bank)
    if profile is None or profile.endpoint is None:
        raise BankReadError('Bankendpunkt ist noch nicht verifiziert.')
    if not isinstance(product_id, str) or not product_id.strip():
        raise BankReadError('Registrierte FinTS-Produktkennung erforderlich.')
    if not isinstance(bank_code, str) or not re.fullmatch(r'[0-9]{8}', bank_code):
        raise BankReadError('Gültige Bankleitzahl erforderlich.')
    from fints.client import FinTS3PinTanClient
    from requests import Session

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
            client = FinTS3PinTanClient(bank_code, credentials.username, credentials.pin,
                                       profile.endpoint, product_id=product_id, product_version='0.1.0',
                                       tan_medium=tan_medium)
            client.connection.session.close()
            client.connection.session = RestrictedSession()
            if tan_method is not None:
                client.set_tan_mechanism(tan_method)
            return ReadOnlyFinTS(client, respond)
        except Exception:
            raise BankReadError('FinTS-Einrichtung nicht abgeschlossen.') from None
