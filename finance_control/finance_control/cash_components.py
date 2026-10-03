"""Conservative cent-exact cash principal parsing for confirmed withdrawals."""

import re
from decimal import Decimal

from .core import money

_EUR_AMOUNT = r"\s*[:=]?\s*(?:EUR\s+([+-]?\d[\d.,]*)|([+-]?\d[\d.,]*)\s+EUR\b)"
_CASH_AMOUNT = re.compile(
    r"\b(?:Bargeldausz(?:ahlung)?\.?|Barauszahlung|cash\s+withdrawal)" + _EUR_AMOUNT,
    re.IGNORECASE,
)
_CASH_FEE = re.compile(
    r"\b(?:Fremdentgelt|Geb(?:ü|ue)hr(?:en)?|Fees?)" + _EUR_AMOUNT,
    re.IGNORECASE,
)
_VALID_AMOUNT = re.compile(r"(?:\d{1,3}(?:\.\d{3})*,\d{2}|\d+\.\d{2}|\d+)")


def cash_principal(row):
    """Return cash principal of a confirmed withdrawal, retaining explicit noncash debit.

    No bank-name heuristics: callers must establish cash classification. Only
    labelled EUR amounts reduce the debit; malformed or contradictory evidence
    does not hide the posting. An explicit principal takes precedence over fees.
    """
    debit = max(-money(row["amount"]), Decimal("0.00"))
    text = " ".join(str(row[key] or "") for key in
                    ("category", "source_category", "counterparty", "description")
                    if key in row.keys())

    def amounts(pattern):
        values = set()
        for before, after in pattern.findall(text):
            token = before or after
            if _VALID_AMOUNT.fullmatch(token) is None:
                return None
            try:
                values.add(money(token.replace(".", "").replace(",", ".")) if "," in token
                           else money(token))
            except ValueError:
                return None
        return values

    principals = amounts(_CASH_AMOUNT)
    fees = amounts(_CASH_FEE)
    if principals is None or fees is None:
        return Decimal("0.00")
    if principals:
        if len(principals) != 1:
            return Decimal("0.00")
        principal = next(iter(principals))
        if principal > debit or (fees and principal + sum(fees) > debit):
            return Decimal("0.00")
        return principal
    # Multiple fee declarations can be duplicate or separate charges; ambiguity
    # keeps the full posting visible instead of guessing a sum.
    if len(fees) > 1:
        return Decimal("0.00")
    fee = next(iter(fees), Decimal("0.00"))
    return debit - fee if fee <= debit else Decimal("0.00")


def is_cash_withdrawal(row):
    """Recognize an explicit withdrawal purpose; a bank name alone is insufficient."""
    text = " ".join(str(row[key] or "") for key in
                    ("category", "source_category", "counterparty", "description")
                    if key in row.keys())
    if re.search(r"\b(?:Baufinanzierung|Kreditkarte|f(?:ä|ae)lliger\s+Belastungsbetrag)\b",
                 text, re.IGNORECASE):
        return False
    if any(str(row[key] or "").rsplit("/", 1)[-1].strip().casefold() in
           {"bargeld", "bargeldabhebung", "cash withdrawal", "cash_withdrawal"}
           for key in ("category", "source_category") if key in row.keys()):
        return True
    if re.search(r"\bGA\s+Nr\.?(?:\s|\d|$)", text, re.IGNORECASE):
        return True
    return re.search(
        r"\b(?:Bargeld(?:abhebung|bezug)|Bargeldausz(?:ahlung)?\.?|Barauszahlung|Geldautomat|GA\s+Nr\.?|"
        r"cash\s+withdrawal|Auszahlung)\b", text, re.IGNORECASE) is not None
