"""Read and validate supplier candidates; never treat extraction as approval."""
import json
import re
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType

from .core import money
from .import_preview import outside_repository

FIELDS = {'invoice_number', 'invoice_date', 'vendor_name', 'gross_total',
          'net_total', 'vat_total', 'currency', 'order_number'}
EXTRACTOR_VERSIONS = {'invoice_rules_explicit_total_v2', 'invoice_rules_explicit_total_v3'}


class InvoiceContractError(ValueError):
    """Safe error code without source values."""


@dataclass(frozen=True, repr=False)
class InvoiceCandidate:
    document_hash: str
    text_hash: str
    normalized_text_hash: str
    values: dict = field(repr=False)
    warnings: tuple[str, ...] = ()

    def summary(self):
        return {'schema_version': 'invoice_value_candidate_v1', 'review_status': 'needs_review',
                'populated_fields': sum(v is not None for v in self.values.values()),
                'warnings': list(self.warnings), 'ledger_written': False}


def parse_invoice_candidate(payload):
    try:
        expected = {'schema_version', 'extractor_version', 'source_document_sha256',
                    'source_text_sha256', 'normalized_text_sha256', 'evidence_scope', 'review_status', 'fields', 'warnings'}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError()
        if (payload['schema_version'] != 'invoice_value_candidate_v1'
                or payload['extractor_version'] not in EXTRACTOR_VERSIONS
                or payload['review_status'] != 'needs_review'
                or payload['evidence_scope'] != 'document_and_text'):
            raise ValueError()
        for key in ['source_document_sha256', 'source_text_sha256', 'normalized_text_sha256']:
            if not isinstance(payload[key], str) or not re.fullmatch('[0-9a-f]{64}', payload[key]):
                raise ValueError()
        if not isinstance(payload['fields'], dict) or set(payload['fields']) != FIELDS:
            raise ValueError()
        values = {}
        for name, item in payload['fields'].items():
            if not isinstance(item, dict) or set(item) != {'value', 'status', 'source_text_sha256', 'normalized_text_sha256', 'method'}:
                raise ValueError()
            value = item['value']
            if (item['source_text_sha256'] != payload['source_text_sha256']
                    or item['normalized_text_sha256'] != payload['normalized_text_sha256']
                    or item['method'] != 'heuristic_document'
                    or item['status'] != ('missing' if value is None else 'candidate')):
                raise ValueError()
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                    raise ValueError()
                if name in {'gross_total', 'net_total', 'vat_total'}:
                    value = money(value)
                elif name == 'invoice_date':
                    parsed = date.fromisoformat(value)
                    if parsed.isoformat() != value:
                        raise ValueError()
                    value = parsed
            values[name] = value
        warnings = payload['warnings']
        allowed = {'amount_ambiguous', 'not_invoice_like', 'gross_total_missing', 'currency_missing', 'net_vat_gross_mismatch'}
        if not isinstance(warnings, list) or any(not isinstance(x, str) or x not in allowed for x in warnings):
            raise ValueError()
        # Independently validate required warning semantics rather than trusting the producer.
        verified = set()
        if values['gross_total'] is None:
            verified.add('gross_total_missing')
        if values['currency'] is None:
            verified.add('currency_missing')
        elif values['currency'] != 'EUR':
            verified.add('unsupported_currency')
        gross, net, vat = (values[n] for n in ['gross_total', 'net_total', 'vat_total'])
        if all(x is not None for x in [gross, net, vat]) and gross != net + vat:
            verified.add('net_vat_gross_mismatch')
        return InvoiceCandidate(payload['source_document_sha256'], payload['source_text_sha256'],
                                payload['normalized_text_sha256'],
                                MappingProxyType(values), tuple(sorted(verified | set(warnings))))
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise InvoiceContractError('invalid_invoice_candidate_contract') from None


def read_invoice_candidate(path):
    path = outside_repository(path)
    with path.open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise InvoiceContractError('invoice_candidate_too_large')
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InvoiceContractError('duplicate_json_key')
            result[key] = value
        return result
    try:
        payload = json.loads(raw, object_pairs_hook=unique_keys)
    except InvoiceContractError:
        raise
    except (ValueError, UnicodeError):
        raise InvoiceContractError('invalid_invoice_candidate_json') from None
    return parse_invoice_candidate(payload)


def payment_suggestions(candidate, transactions, *, max_days):
    """Unconfirmed one-invoice/one-outflow proposals. Window is explicitly supplied."""
    if type(max_days) is not int or max_days < 0:
        raise ValueError('Nonnegative day window required')
    value, issued = candidate.values['gross_total'], candidate.values['invoice_date']
    if candidate.warnings or value is None or value <= 0 or issued is None:
        return []
    results = []
    for transaction in transactions:
        transaction = dict(transaction)
        if (transaction['currency'] != candidate.values['currency'] or transaction.get('transfer_id')
                or money(transaction['amount']) != -value):
            continue
        days = (date.fromisoformat(transaction['date']) - issued).days
        if 0 <= days <= max_days:
            results.append({'account_id': transaction['account_id'],
                            'external_id': transaction['external_id'], 'days_after_invoice': days,
                            'status': 'needs_review', 'basis': 'amount_currency_date_only'})
    return results
