"""Bank statement contracts: source identities are distinct from transaction IDs."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


class StatementError(ValueError):
    """Static diagnostic; never carries source text or private identifiers."""


class StatementParseError(StatementError):
    """Parser failure with a fixed, source-independent reason code."""

    _REASONS = frozenset({'FORMAT', 'IDENTITY_MISSING', 'INCOMPLETE'})

    def __init__(self, reason: str):
        if reason not in self._REASONS:
            raise ValueError('Invalid parser reason')
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class StatementRow:
    booked_on: date
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class BankStatement:
    source_profile: str
    source_account: str = field(repr=False)
    year: int
    number: int
    opening_date: date
    closing_date: date
    opening_balance: Decimal
    closing_balance: Decimal
    currency: str
    rows: tuple[StatementRow, ...] = field(repr=False)


@dataclass(frozen=True)
class MonthlySnapshot:
    source_profile: str
    source_account: str = field(repr=False)
    period_start: date
    period_end: date
    opening_date: date
    closing_date: date
    opening_balance: Decimal
    closing_balance: Decimal
    currency: str
    rows: tuple[StatementRow, ...] = field(repr=False)
