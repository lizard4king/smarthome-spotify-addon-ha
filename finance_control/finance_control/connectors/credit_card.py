"""FinTS V3 C.12 credit-card reads and normalized booking context.

Segment layouts follow DK change G112, chapter C.12 (HKKKU/HIKKU and
HKKKS/HIKKS, all version 1). Structured card identities stay in ephemeral
FinTS objects. Known card identities are masked in descriptive booking data,
which is retained for an authenticated server preview.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
import hashlib
import hmac
import re
from zoneinfo import ZoneInfo

from fints.fields import (
    AlphanumericField, BooleanField, CodeField, CountryField, DataElementField,
    DataElementGroupField,
)
from fints.formals import Balance1, Balance2, CreditDebit2, DataElementGroup, KTI1
from fints.segments.base import FinTS3Segment, ParameterSegment


_MAX_TOUCHDOWNS = 100
_MAX_TRANSACTIONS = 100_000
_BANK_TIMEZONE = ZoneInfo('Europe/Berlin')


class CreditCardReadError(ValueError):
    """Static, source-independent error for malformed card replies or metadata."""


class CreditCardUnsupportedError(CreditCardReadError):
    """The bank did not advertise the required C.12 read segment."""


class AmountWithCreditDebit1(DataElementGroup):
    """FinTS ``btgv`` version 1: amount, currency, then C/D marker."""

    amount = DataElementField(type='wrt', _d='Wert')
    currency = DataElementField(type='cur', _d='Währung')
    credit_debit = CodeField(enum=CreditDebit2, length=1, _d='Soll/Haben-Kennzeichen')


class CreditCardBooking1(DataElementGroup):
    """One flat HIKKU booking wire layout in the G112-specified field order."""

    card_number = AlphanumericField(max_length=30, _d='Umsatz getätigt von')
    receipt_date = DataElementField(type='dat', _d='Belegdatum')
    booking_date = DataElementField(type='dat', _d='Buchungsdatum')
    billing_date = DataElementField(type='dat', required=False, _d='Abrechnungsdatum')
    value_date = DataElementField(type='dat', required=False, _d='Wertstellungsdatum')
    original_amount_value = DataElementField(type='wrt', required=False, _d='Originalbetrag Wert')
    original_currency = DataElementField(type='cur', required=False, _d='Originalbetrag Währung')
    original_credit_debit = CodeField(enum=CreditDebit2, length=1, required=False,
                                      _d='Originalbetrag Soll/Haben-Kennzeichen')
    exchange_rate = DataElementField(type='wrt', required=False, _d='Umrechnungskurs')
    booking_amount_value = DataElementField(type='wrt', _d='Buchungsbetrag Wert')
    booking_currency = DataElementField(type='cur', _d='Buchungsbetrag Währung')
    booking_credit_debit = CodeField(enum=CreditDebit2, length=1, _d='Buchungsbetrag Soll/Haben-Kennzeichen')
    description_1_base = AlphanumericField(max_length=50, required=False, _d='Transaktionsbeschreibung 1 Grundtext')
    description_1_additional = AlphanumericField(max_length=50, required=False, _d='Transaktionsbeschreibung 1 Zusatz')
    description_2_base = AlphanumericField(max_length=50, required=False, _d='Transaktionsbeschreibung 2 Grundtext')
    description_2_additional = AlphanumericField(max_length=50, required=False, _d='Transaktionsbeschreibung 2 Zusatz')
    description_3_base = AlphanumericField(max_length=50, required=False, _d='Transaktionsbeschreibung 3 Grundtext')
    description_3_additional = AlphanumericField(max_length=50, required=False, _d='Transaktionsbeschreibung 3 Zusatz')
    description_4_base = AlphanumericField(max_length=50, required=False, _d='Transaktionsbeschreibung 4 Grundtext')
    description_4_additional = AlphanumericField(max_length=50, required=False, _d='Transaktionsbeschreibung 4 Zusatz')
    country_code = CountryField(required=False, _d='Länderkennzeichen')
    merchant_name = AlphanumericField(max_length=140, required=False, _d='Händlername')
    terminal_id = AlphanumericField(max_length=35, required=False, _d='Kartenzahlungsterminal-ID')
    billed = BooleanField(required=False, _d='Umsatz abgerechnet')
    booking_reference = DataElementField(type='id', required=False, _d='Buchungsreferenz')
    fee_code = AlphanumericField(length=4, required=False, _d='Gebührenschlüssel')
    billing_label = AlphanumericField(max_length=30, required=False, _d='Abrechnungskennzeichen')
    atm_fee_reference = AlphanumericField(max_length=40, required=False, _d='GAA-/BAR-Entgelt + Buchungsreferenz')
    foreign_use_fee_reference = AlphanumericField(max_length=40, required=False, _d='AEE + Buchungsreferenz')


class CreditCardTransactionsParameter1(DataElementGroup):
    """HIKKUS1 C.12.1 parameters, in published order."""

    storage_days = DataElementField(type='num', max_length=4, _d='Speicherzeitraum')
    max_responses_allowed = BooleanField(_d='Eingabe Anzahl Einträge erlaubt')
    date_range_allowed = BooleanField(_d='Angabe Zeitraum erlaubt')
    account_required = BooleanField(_d='Kontoverbindung benötigt')


class CreditCardBalanceParameter1(DataElementGroup):
    """HIKKSS1 C.12.2 parameters."""

    account_required = BooleanField(_d='Kontoverbindung benötigt')


class HKKKU1(FinTS3Segment):
    """Request card transactions, FinTS C.12.1 version 1."""

    account = DataElementGroupField(type=KTI1, required=False, _d='Kontoverbindung international')
    card_number = AlphanumericField(max_length=30, _d='Kreditkartennummer')
    card_account_number = DataElementField(type='id', required=False, _d='Kreditkartenkonto-/Kundennummer')
    date_start = DataElementField(type='dat', required=False, _d='Von Datum')
    date_end = DataElementField(type='dat', required=False, _d='Bis Datum')
    max_number_responses = DataElementField(type='num', max_length=4, required=False, _d='Maximale Anzahl Einträge')
    touchdown_point = AlphanumericField(max_length=35, required=False, _d='Aufsetzpunkt')


class HIKKU1(FinTS3Segment):
    """Response card transactions, FinTS C.12.1 version 1."""

    card_number = AlphanumericField(max_length=30, _d='Kreditkartennummer')
    card_account_number = DataElementField(type='id', required=False, _d='Kreditkartenkonto-/Kundennummer')
    current_balance = DataElementGroupField(type=Balance2, required=False, _d='Aktueller Saldo')
    last_billing_date = DataElementField(type='dat', required=False, _d='Datum der letzten Abrechnung')
    expected_billing_date = DataElementField(type='dat', required=False, _d='Voraussichtliches Abrechnungsdatum')
    bookings = DataElementGroupField(type=CreditCardBooking1, max_count=_MAX_TRANSACTIONS, required=False,
                                     _d='Umsatz Kreditkartenkonto')


class HIKKUS1(ParameterSegment):
    """Bank parameters for HKKKU1, FinTS C.12.1 version 1."""

    parameter = DataElementGroupField(type=CreditCardTransactionsParameter1, _d='Parameter Kreditkartenumsätze')


class HKKKS1(FinTS3Segment):
    """Request current card balance, FinTS C.12.2 version 1."""

    account = DataElementGroupField(type=KTI1, required=False, _d='Kontoverbindung international')
    card_number = AlphanumericField(max_length=30, _d='Kreditkartennummer')
    card_account_number = DataElementField(type='id', required=False, _d='Kreditkartenkonto-/Kundennummer')


class HIKKS1(FinTS3Segment):
    """Response current card balance, FinTS C.12.2 version 1."""

    card_number = AlphanumericField(max_length=30, _d='Kreditkartennummer')
    card_account_number = DataElementField(type='id', required=False, _d='Kreditkartenkonto-/Kundennummer')
    current_balance = DataElementGroupField(type=Balance2, _d='Aktueller Saldo')
    available_amount = DataElementGroupField(type=AmountWithCreditDebit1, required=False, _d='Verfügbarer Betrag Kreditkarte')
    open_authorizations = DataElementField(type='wrt', required=False, _d='Summe offener Autorisierungen')
    credit_limit = DataElementField(type='wrt', required=False, _d='Verfügungsrahmen')
    expected_billing_date = DataElementField(type='dat', required=False, _d='Voraussichtliches Abrechnungsdatum')


class HIKKSS1(ParameterSegment):
    """Bank parameters for HKKKS1, FinTS C.12.2 version 1."""

    parameter = DataElementGroupField(type=CreditCardBalanceParameter1, _d='Parameter Kreditkartensaldo')


@dataclass(frozen=True, repr=False)
class CreditCardMetadata:
    """Ephemeral request data; repr intentionally hides account and PAN fields."""

    card_number: str = field(repr=False)
    account: KTI1 | None = field(default=None, repr=False)
    card_account_number: str | None = field(default=None, repr=False)

    @property
    def masked_number(self):
        """Display label for a locally supplied PAN, without exposing its prefix."""
        _check_metadata(self)
        return f'•••• {self.card_number[-4:]}'

    def fingerprint(self, key):
        """Stable opaque selector scoped to a caller-held secret key."""
        _check_metadata(self)
        if type(key) is not bytes or len(key) < 16:
            _invalid()
        return hmac.new(key, b'finance-control/card/v1:' + self.card_number.encode('ascii'),
                        hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class CreditCardBooking:
    receipt_date: date
    booking_date: date
    billing_date: date | None
    value_date: date | None
    amount: Decimal
    currency: str
    original_amount: Decimal | None
    original_currency: str | None
    original_exchange_rate: Decimal | None
    billed: bool | None
    descriptions: tuple[tuple[str | None, str | None], ...] = ()
    merchant_name: str | None = None
    country_code: str | None = None
    terminal_id: str | None = None
    booking_reference: str | None = None
    fee_code: str | None = None
    billing_label: str | None = None
    atm_fee_reference: str | None = None
    foreign_use_fee_reference: str | None = None


@dataclass(frozen=True)
class CreditCardBalance:
    amount: Decimal
    currency: str
    as_of: date
    available_amount: Decimal | None = None
    available_currency: str | None = None
    open_authorizations: Decimal | None = None
    credit_limit: Decimal | None = None
    last_billing_date: date | None = None
    expected_billing_date: date | None = None


@dataclass(frozen=True)
class CreditCardTransactions:
    bookings: tuple[CreditCardBooking, ...]
    balance: CreditCardBalance | None
    last_billing_date: date | None
    expected_billing_date: date | None


def _invalid():
    raise CreditCardReadError('Kreditkartenantwort ist unvollständig oder ungültig.')


def _bank_today():
    """Current calendar date in the bank's local timezone."""
    return datetime.now(_BANK_TIMEZONE).date()


def _check_metadata(metadata):
    if not isinstance(metadata, CreditCardMetadata):
        _invalid()
    if (type(metadata.card_number) is not str or re.fullmatch(r'[0-9]{16}', metadata.card_number) is None or
            (metadata.account is not None and not isinstance(metadata.account, KTI1)) or
            (metadata.card_account_number is not None and
             (type(metadata.card_account_number) is not str or not metadata.card_account_number or
              len(metadata.card_account_number) > 30))):
        _invalid()


def _check_dates(start_date, end_date):
    if ((start_date is not None and type(start_date) is not date) or
            (end_date is not None and type(end_date) is not date) or
            (start_date is not None and end_date is not None and start_date > end_date)):
        _invalid()


def _bpd_parameters(client, segment_type):
    segment = client.bpd.find_segment_first(segment_type, 1)
    if segment is None:
        raise CreditCardUnsupportedError('Kreditkartenabruf ist nicht als unterstützt gemeldet.')
    return segment.parameter


def _check_response_identity(response, metadata):
    if response.card_number != metadata.card_number:
        # DK leaves masking to the bank; a masked echo cannot be matched safely.
        _invalid()
    if (metadata.card_account_number is not None and response.card_account_number is not None and
            response.card_account_number != metadata.card_account_number):
        _invalid()


def _decimal(value, *, optional=False):
    if value is None and optional:
        return None
    if type(value) is not Decimal or not value.is_finite():
        _invalid()
    return value


def _text(value, max_length, card_numbers=()):
    if value is None:
        return None
    if (type(value) is not str or not 1 <= len(value) <= max_length or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _invalid()
    # Preserve unrelated long bank references. Match only actual request/booking
    # card numbers, including versions separated by ASCII spaces or hyphens.
    for number in card_numbers:
        pattern = r'[ -]*'.join(number)
        value = re.sub(pattern, '•••• ' + number[-4:], value)
    return value


def _date_value(value, *, optional=False):
    if value is None and optional:
        return None
    if type(value) is not date:
        _invalid()
    return value


def _code_value(value):
    code = getattr(value, 'value', value)
    if code not in ('C', 'D'):
        _invalid()
    return code


def _signed_amount(amount_value, marker):
    amount = _decimal(amount_value)
    if amount < 0:
        _invalid()
    code = _code_value(marker)
    return amount if code == 'C' else amount.copy_negate()


def _balance(value, *, extra=None, optional=False):
    if value is None or (optional and not isinstance(value, (Balance1, Balance2))):
        return None
    if not isinstance(value, (Balance1, Balance2)):
        _invalid()
    amount_value = value.amount.amount if isinstance(value, Balance2) else value.amount
    currency = value.amount.currency if isinstance(value, Balance2) else value.currency
    if optional and amount_value is None:
        return None
    magnitude = _decimal(amount_value)
    if magnitude < 0:
        _invalid()
    code = _code_value(value.credit_debit)
    amount = magnitude if code == 'C' else magnitude.copy_negate()
    if type(currency) is not str or re.fullmatch(r'[A-Z]{3}', currency) is None:
        _invalid()
    available_amount = available_currency = open_authorizations = credit_limit = None
    last_billing_date = expected_billing_date = None
    if extra is not None:
        if extra.available_amount is not None and extra.available_amount.amount is not None:
            available_amount = _signed_amount(extra.available_amount.amount, extra.available_amount.credit_debit)
            available_currency = extra.available_amount.currency
        open_authorizations = _decimal(extra.open_authorizations, optional=True)
        credit_limit = _decimal(extra.credit_limit, optional=True)
        last_billing_date = _date_value(getattr(extra, 'last_billing_date', None), optional=True)
        expected_billing_date = _date_value(getattr(extra, 'expected_billing_date', None), optional=True)
    return CreditCardBalance(amount, currency, _date_value(value.date), available_amount,
                             available_currency, open_authorizations, credit_limit,
                             last_billing_date, expected_billing_date)


def _booking(value, card_numbers=()):
    def text(value, limit):
        return _text(value, limit, card_numbers)
    currency = value.booking_currency
    if type(currency) is not str or re.fullmatch(r'[A-Z]{3}', currency) is None:
        _invalid()
    original_values = (value.original_amount_value, value.original_currency, value.original_credit_debit)
    original_amount = original_currency = None
    if any(part is not None for part in original_values):
        if any(part is None for part in original_values):
            _invalid()
        original_amount = _signed_amount(value.original_amount_value, value.original_credit_debit)
        original_currency = value.original_currency
        if type(original_currency) is not str or re.fullmatch(r'[A-Z]{3}', original_currency) is None:
            _invalid()
    exchange_rate = _decimal(value.exchange_rate, optional=True)
    if exchange_rate is not None and exchange_rate <= 0:
        _invalid()
    billed = value.billed
    if billed is not None and type(billed) is not bool:
        _invalid()
    return CreditCardBooking(
        _date_value(value.receipt_date), _date_value(value.booking_date),
        _date_value(value.billing_date, optional=True), _date_value(value.value_date, optional=True),
        _signed_amount(value.booking_amount_value, value.booking_credit_debit), currency,
        original_amount, original_currency, exchange_rate, billed,
        tuple((text(getattr(value, f'description_{index}_base'), 50),
               text(getattr(value, f'description_{index}_additional'), 50))
              for index in range(1, 5)),
        text(value.merchant_name, 140), text(value.country_code, 3),
        text(value.terminal_id, 35), text(value.booking_reference, 35),
        text(value.fee_code, 4), text(value.billing_label, 30),
        text(value.atm_fee_reference, 40), text(value.foreign_use_fee_reference, 40),
    )


def _fetch(client, command_class, response_type, parameter_type, metadata, start_date, end_date,
           max_number_responses=None):
    _check_metadata(metadata)
    _check_dates(start_date, end_date)
    params = _bpd_parameters(client, parameter_type.TYPE)
    if (type(params.account_required) is not bool or
            (parameter_type is HIKKUS1 and
             (type(params.date_range_allowed) is not bool or
              type(params.max_responses_allowed) is not bool or
              type(params.storage_days) is not int or params.storage_days < 1))):
        _invalid()
    if params.account_required and metadata.account is None:
        _invalid()
    if (start_date is not None or end_date is not None) and not params.date_range_allowed:
        _invalid()
    if max_number_responses is not None:
        if (type(max_number_responses) is not int or not 1 <= max_number_responses <= 9999 or
                not params.max_responses_allowed):
            _invalid()
    command = client._find_highest_supported_command(command_class)

    def process(segments):
        if not segments or len(segments) > _MAX_TRANSACTIONS:
            _invalid()
        # Collect every proven card identity before normalizing any booking.
        # A later supplementary-card row may mention its PAN in an earlier row.
        card_numbers = {metadata.card_number}
        booking_count = 0
        for segment in segments:
            if not isinstance(segment, response_type):
                _invalid()
            _check_response_identity(segment, metadata)
            for item in segment.bookings:
                booking_count += 1
                if booking_count > _MAX_TRANSACTIONS:
                    _invalid()
                if (type(item.card_number) is not str or
                        re.fullmatch(r'[0-9]{16,30}', item.card_number) is None):
                    _invalid()
                card_numbers.add(item.card_number)
        card_numbers = tuple(sorted(card_numbers, key=lambda value: (-len(value), value)))
        bookings = []
        balance = None
        last_billing_date = expected_billing_date = None
        for segment in segments:
            if not isinstance(segment, response_type):
                _invalid()
            _check_response_identity(segment, metadata)
            page_balance = _balance(segment.current_balance, optional=True)
            if page_balance is not None:
                if balance is not None and page_balance != balance:
                    _invalid()
                balance = page_balance
            page_last = _date_value(getattr(segment, 'last_billing_date', None), optional=True)
            page_expected = _date_value(getattr(segment, 'expected_billing_date', None), optional=True)
            if page_last is not None:
                if last_billing_date is not None and last_billing_date != page_last:
                    _invalid()
                last_billing_date = page_last
            if page_expected is not None:
                if expected_billing_date is not None and expected_billing_date != page_expected:
                    _invalid()
                expected_billing_date = page_expected
            for item in segment.bookings:
                if len(bookings) >= _MAX_TRANSACTIONS:
                    _invalid()
                booking = _booking(item, card_numbers)
                if ((start_date is not None and booking.booking_date < start_date) or
                        (end_date is not None and booking.booking_date > end_date) or
                        booking.booking_date > _bank_today()):
                    _invalid()
                bookings.append(booking)
        return CreditCardTransactions(tuple(bookings), balance, last_billing_date, expected_billing_date)

    pages = 0

    def bounded_factory(touchdown):
        nonlocal pages
        pages += 1
        if pages > _MAX_TOUCHDOWNS:
            _invalid()
        return command(account=metadata.account, card_number=metadata.card_number,
                       card_account_number=metadata.card_account_number,
                       date_start=start_date, date_end=end_date,
                       max_number_responses=max_number_responses, touchdown_point=touchdown)

    with client._get_dialog() as dialog:
        return client._fetch_with_touchdowns(dialog, bounded_factory, process, response_type.TYPE)


def read_credit_card_transactions(client, metadata, *, start_date=None, end_date=None,
                                  max_number_responses=None):
    """Read card transactions; return a pending FinTS TAN challenge unchanged."""
    from fints.client import NeedTANResponse

    result = _fetch(client, HKKKU1, HIKKU1, HIKKUS1, metadata, start_date, end_date,
                    max_number_responses)
    if isinstance(result, NeedTANResponse):
        return result
    if not isinstance(result, CreditCardTransactions):
        _invalid()
    return result


def read_credit_card_balance(client, metadata):
    """Read a current card balance without using or returning card identity fields."""
    from fints.client import NeedTANResponse

    _check_metadata(metadata)
    params = _bpd_parameters(client, HIKKSS1.TYPE)
    if type(params.account_required) is not bool:
        _invalid()
    if params.account_required and metadata.account is None:
        _invalid()
    command = client._find_highest_supported_command(HKKKS1)

    pages = 0

    def bounded_factory(touchdown):
        nonlocal pages
        if touchdown is not None:
            # HKKKS1 has no continuation field and cannot fetch another page safely.
            _invalid()
        pages += 1
        if pages > 1:
            _invalid()
        return command(account=metadata.account, card_number=metadata.card_number,
                       card_account_number=metadata.card_account_number)

    def process(segments):
        if len(segments) != 1 or not isinstance(segments[0], HIKKS1):
            _invalid()
        segment = segments[0]
        _check_response_identity(segment, metadata)
        return _balance(segment.current_balance, extra=segment)

    with client._get_dialog() as dialog:
        result = client._fetch_with_touchdowns(dialog, bounded_factory, process, HIKKS1.TYPE)
    if isinstance(result, NeedTANResponse):
        return result
    if not isinstance(result, CreditCardBalance):
        _invalid()
    return result
