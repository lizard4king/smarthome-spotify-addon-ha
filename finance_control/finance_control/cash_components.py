"""Conservative cent-exact cash principal parsing for confirmed withdrawals."""

import re
from decimal import Decimal

from .core import money

_EUR_AMOUNT = r"\s*[:=]?\s*(?:EUR\s+([+-]?\d[\d.,]*)(?![\w.,])|([+-]?\d[\d.,]*)\s+EUR\b)"
_CASH_LABEL = re.compile(
    r"\b(?:Bargeldausz(?:ahlung)?\.?|Barauszahlung|cash\s+withdrawal|Auszahlung)",
    re.IGNORECASE,
)
_CASH_AMOUNT = re.compile(
    r"\b(?:Bargeldausz(?:ahlung)?\.?|Barauszahlung|cash\s+withdrawal|Auszahlung)" + _EUR_AMOUNT,
    re.IGNORECASE,
)
_CASH_FEE = re.compile(
    r"\b(?:Fremdentgelt|Geb(?:ü|ue)hr(?:en)?|Fees?)" + _EUR_AMOUNT,
    re.IGNORECASE,
)
_FEE_LABEL = re.compile(r"\b(?:Fremdentgelt|Geb(?:ü|ue)hr(?:en)?|Fees?)\b", re.IGNORECASE)
_VALID_AMOUNT = re.compile(r"(?:\d{1,3}(?:\.\d{3})*,\d{2}|\d+\.\d{2}|\d+)")
_EXPLICIT_CASH_LABEL = re.compile(
    r"\b(?:Bargeldausz(?:ahlung)?\.(?=\d)|"
    r"(?:Bargeldausz(?:ahlung)?\.?|Barauszahlung|cash\s+withdrawal)(?=\W|$))",
    re.IGNORECASE,
)
# The leading-EUR form needs its own right boundary: without it, regex matching
# accepts ``EUR 20,00abc`` as the valid prefix ``EUR 20,00``.
_EXPLICIT_AMOUNT = re.compile(
    r"\s*[:=]?\s*(?:EUR\s+([+-]?\d[\d.,]*)(?![\w.,])|([+-]?\d[\d.,]*)\s+EUR\b)",
    re.IGNORECASE,
)


def cash_receipt_key(entry_id, account_id):
    """Build a cash receipt key; callers compare it whole rather than split it."""
    return f"bonsy-cash:{entry_id}:{account_id}"


def explicit_cash_component(row, *, allow_fee=False):
    """Return (status, principal) only for one explicit, cent-exact EUR label.

    The caller must separately establish a non-transfer expense. A neutral
    ``Auszahlung`` and a confirmed cash category alone do not qualify here.
    """
    columns = row.keys()
    if ("currency" in columns and row["currency"] != "EUR"):
        return "none", Decimal("0.00")
    text = " ".join(str(row[key] or "") for key in
                    ("category", "source_category", "counterparty", "description")
                    if key in columns)
    markers = list(_EXPLICIT_CASH_LABEL.finditer(text))
    if not markers:
        return "none", Decimal("0.00")
    if len(markers) != 1:
        return "review_required", Decimal("0.00")
    amount_match = _EXPLICIT_AMOUNT.match(text, markers[0].end())
    if amount_match is None:
        return "review_required", Decimal("0.00")
    token = amount_match.group(1) or amount_match.group(2)
    if _VALID_AMOUNT.fullmatch(token) is None:
        return "review_required", Decimal("0.00")
    try:
        principal = (money(token.replace(".", "").replace(",", "."))
                     if "," in token else money(token))
        debit = -money(row["amount"])
    except (KeyError, ValueError):
        return "review_required", Decimal("0.00")
    if principal <= 0 or debit <= 0 or principal > debit:
        return "review_required", Decimal("0.00")
    fees = _CASH_FEE.findall(text)
    if len(list(_FEE_LABEL.finditer(text))) != len(fees):
        return "review_required", Decimal("0.00")
    if fees:
        if not allow_fee or len(fees) != 1:
            return "review_required", Decimal("0.00")
        fee_token = fees[0][0] or fees[0][1]
        if _VALID_AMOUNT.fullmatch(fee_token) is None:
            return "review_required", Decimal("0.00")
        try:
            fee = (money(fee_token.replace(".", "").replace(",", "."))
                   if "," in fee_token else money(fee_token))
        except ValueError:
            return "review_required", Decimal("0.00")
        if fee < 0 or principal + fee > debit:
            return "review_required", Decimal("0.00")
    return "explicit", principal


def cash_principal(row):
    """Return cash principal of a confirmed withdrawal, retaining explicit noncash debit.

    No bank-name heuristics: callers must establish cash classification. Only
    labelled EUR amounts reduce the debit; malformed or contradictory evidence
    does not hide the posting. An explicit principal takes precedence over fees.
    """
    debit = max(-money(row["amount"]), Decimal("0.00"))
    columns = row.keys()  # sqlite3.Row membership checks values, not column names.
    text = " ".join(str(row[key] or "") for key in
                    ("category", "source_category", "counterparty", "description")
                    if key in columns)

    cash_matches = list(_CASH_AMOUNT.finditer(text))
    fee_matches = list(_CASH_FEE.finditer(text))
    # A bare withdrawal label is normal ATM evidence. An immediately following
    # amount cue must, however, have a complete valid match; otherwise the
    # full-debit fallback would silently hide malformed explicit evidence.
    for marker in _CASH_LABEL.finditer(text):
        following = text[marker.end():].lstrip(" :=\t")
        has_amount_cue = (re.match(r"EUR(?:\b|(?=[+-]?\d))", following, re.IGNORECASE)
                          is not None
                          or bool(following) and following[0] in "+-0123456789")
        if has_amount_cue and _CASH_AMOUNT.match(text, marker.start()) is None:
            return Decimal("0.00")
    if len(fee_matches) != len(list(_FEE_LABEL.finditer(text))):
        return Decimal("0.00")

    def amounts(matches):
        values = []
        for match in matches:
            before, after = match.groups()
            token = before or after
            if _VALID_AMOUNT.fullmatch(token) is None:
                return None
            try:
                values.append(money(token.replace(".", "").replace(",", ".")) if "," in token
                              else money(token))
            except ValueError:
                return None
        return values

    principals = amounts(cash_matches)
    fees = amounts(fee_matches)
    if principals is None or fees is None:
        return Decimal("0.00")
    # Multiple fee declarations can be duplicate or separate charges; ambiguity
    # keeps the full posting visible instead of guessing a sum.
    if len(fees) > 1:
        return Decimal("0.00")
    if principals:
        if len(set(principals)) != 1:
            return Decimal("0.00")
        principal = principals[0]
        if principal > debit or (fees and principal + sum(fees) > debit):
            return Decimal("0.00")
        return principal
    fee = fees[0] if fees else Decimal("0.00")
    return debit - fee if fee <= debit else Decimal("0.00")


def is_cash_withdrawal(row):
    """Recognize an explicit withdrawal purpose; a bank name alone is insufficient."""
    columns = row.keys()
    text = " ".join(str(row[key] or "") for key in
                    ("category", "source_category", "counterparty", "description")
                    if key in columns)
    if re.search(r"\b(?:Baufinanzierung|Kreditkarte|f(?:ä|ae)lliger\s+Belastungsbetrag)\b",
                 text, re.IGNORECASE):
        return False
    if any(str(row[key] or "").rsplit("/", 1)[-1].strip().casefold() in
           {"bargeld", "bargeldabhebung", "cash withdrawal", "cash_withdrawal"}
           for key in ("category", "source_category") if key in columns):
        return True
    if re.search(r"\bGA\s+Nr\.?(?:\s|\d|$)", text, re.IGNORECASE):
        return True
    return re.search(
        r"\b(?:Bargeld(?:abhebung|bezug)|Bargeldausz(?:ahlung)?\.?|Barauszahlung|Geldautomat|GA\s+Nr\.?|"
        r"cash\s+withdrawal|Auszahlung)\b", text, re.IGNORECASE) is not None
