"""Bounded, read-only parser for booked ISO 20022 CAMT entries."""

from datetime import date
from decimal import Decimal, InvalidOperation
import re
import xml.etree.ElementTree as ET

from .fints_readonly import Booking, BankReadError, BankErrorCode, _bank_reference_candidate


_MAX_BYTES = 16 * 1024 * 1024
_MAX_STREAMS = 128
_NAMESPACE_RE = re.compile(
    r'urn:iso:std:iso:20022:tech:xsd:camt\.(052|053|054)\.001\.(?:0[2-8])', re.ASCII
)
_DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}', re.ASCII)
_DECIMAL_RE = re.compile(r'\d+(?:\.\d+)?', re.ASCII)


def _invalid():
    raise BankReadError(code=BankErrorCode.DATA_FORMAT)


def _children(parent, name):
    return [child for child in list(parent) if child.tag == name]


def _one(parent, name, *, required=False):
    found = _children(parent, name)
    if len(found) > 1 or (required and len(found) != 1):
        _invalid()
    return found[0] if found else None


def _text(node):
    if node is None or list(node) or node.text is None:
        _invalid()
    return node.text


def _entry_status(entry, q):
    status = _one(entry, q('Sts'), required=True)
    if list(status):
        code = _one(status, q('Cd'), required=True)
        value = _text(code)
    else:
        value = _text(status)
    if value not in ('BOOK', 'PDNG'):
        _invalid()
    return value


def _booked_date(entry, q):
    booking = _one(entry, q('BookgDt'), required=True)
    node = _one(booking, q('Dt'), required=True)
    value = _text(node)
    if not _DATE_RE.fullmatch(value):
        _invalid()
    try:
        return date.fromisoformat(value)
    except ValueError:
        _invalid()


def _amount(entry, q):
    amount_node = _one(entry, q('Amt'), required=True)
    currency = amount_node.attrib.get('Ccy')
    if type(currency) is not str or not re.fullmatch(r'[A-Z]{3}', currency, re.ASCII):
        _invalid()
    raw = _text(amount_node)
    if not _DECIMAL_RE.fullmatch(raw):
        _invalid()
    integer, separator, fractional = raw.partition('.')
    fractional = fractional.rstrip('0')
    if len(integer) > 18 or len(fractional) > 2:
        _invalid()
    canonical = integer + ('.' + fractional if separator and fractional else '')
    try:
        amount = Decimal(canonical)
    except InvalidOperation:
        _invalid()
    if not amount.is_finite() or amount < 0:
        _invalid()
    direction_node = _one(entry, q('CdtDbtInd'), required=True)
    direction = _text(direction_node)
    if direction == 'DBIT':
        amount = amount.copy_negate()
    elif direction != 'CRDT':
        _invalid()
    return amount, currency


def _external_id(entry, q):
    account_service_ref = _one(entry, q('AcctSvcrRef'))
    if account_service_ref is not None:
        if list(account_service_ref):
            _invalid()
        candidate = _bank_reference_candidate(account_service_ref.text)
        if candidate is not None:
            return candidate
    entry_ref = _one(entry, q('NtryRef'))
    if entry_ref is not None:
        if list(entry_ref):
            _invalid()
        value = entry_ref.text
        if value is None:
            return None
        if len(value) <= 245:
            candidate = _bank_reference_candidate(value)
            if candidate is not None and len(candidate) <= 256:
                return 'camt-entry:' + value
    return None


def _parse_document(payload):
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', payload, re.IGNORECASE):
        _invalid()
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError):
        _invalid()
    if not isinstance(root.tag, str) or not root.tag.startswith('{') or '}' not in root.tag:
        _invalid()
    namespace, local_name = root.tag[1:].split('}', 1)
    match = _NAMESPACE_RE.fullmatch(namespace)
    if match is None or local_name != 'Document' or match.group(1) == '054':
        _invalid()
    q = lambda local: '{' + namespace + '}' + local
    container_name = 'BkToCstmrStmt' if match.group(1) == '053' else 'BkToCstmrAcctRpt'
    item_name = 'Stmt' if match.group(1) == '053' else 'Rpt'
    container = _one(root, q(container_name), required=True)
    reports = _children(container, q(item_name))
    if not reports:
        _invalid()
    bookings = []
    for report in reports:
        for entry in _children(report, q('Ntry')):
            status = _entry_status(entry, q)
            if status == 'PDNG':
                continue
            amount, currency = _amount(entry, q)
            bookings.append(Booking(
                external_id=_external_id(entry, q),
                amount=amount,
                currency=currency,
                booked_on=_booked_date(entry, q),
            ))
    return bookings


def parse_booked_camt(streams):
    """Parse one or more bounded CAMT 052/053 XML byte streams into bookings."""
    if isinstance(streams, (bytes, bytearray, str)):
        _invalid()
    try:
        iterator = iter(streams)
    except TypeError:
        _invalid()
    result = []
    total = 0
    count = 0
    while True:
        try:
            payload = next(iterator)
        except StopIteration:
            break
        except Exception:
            _invalid()
        count += 1
        if count > _MAX_STREAMS or type(payload) is not bytes or not payload:
            _invalid()
        if b'\x00' in payload:
            _invalid()
        total += len(payload)
        if total > _MAX_BYTES:
            _invalid()
        result.extend(_parse_document(payload))
    return tuple(result)
