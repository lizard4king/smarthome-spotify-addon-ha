"""Bounded, side-effect-free routing of unconfirmed classification proposals.

Backends are injected callables. This module performs no transport, persistence,
financial decision, or confirmation; backend failure codes must be trustworthy.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re


class BackendFailure(Exception):
    """A backend failed with a machine-readable, independently classified code."""

    def __init__(self, code, *, fallback_reason=None):
        if type(code) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code):
            raise ValueError('invalid_backend_failure_code')
        self.code = code
        if fallback_reason not in (None, 'usage_limit_reached', 'quota_exhausted'):
            raise ValueError('invalid_fallback_reason')
        self.fallback_reason = fallback_reason
        super().__init__(code)


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or len(value) > maximum or (not empty and not value.strip())
            or any(ord(ch) < 32 for ch in value)):
        raise ValueError('invalid_classification_input')


def _input(payload):
    if type(payload) is not dict or set(payload) != {'task', 'transaction', 'allowed_categories'}:
        raise ValueError('invalid_classification_input')
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid_classification_input') from exc
    if len(encoded) > 6000 or payload['task'] != 'classify':
        raise ValueError('invalid_classification_input')
    transaction = payload['transaction']
    if type(transaction) is not dict or set(transaction) != {
            'counterparty', 'description', 'amount', 'currency', 'date', 'direction'}:
        raise ValueError('invalid_classification_input')
    for key in ('counterparty', 'description'):
        _text(transaction[key], 500, empty=True)
    amount = transaction['amount']
    if type(amount) is not str or not re.fullmatch(r'[+-]?[0-9]{1,12}(?:\.[0-9]{1,2})?', amount):
        raise ValueError('invalid_classification_input')
    try:
        if not Decimal(amount).is_finite():
            raise ValueError('invalid_classification_input')
    except InvalidOperation as exc:
        raise ValueError('invalid_classification_input') from exc
    if type(transaction['currency']) is not str or not re.fullmatch(r'[A-Z]{3}', transaction['currency']):
        raise ValueError('invalid_classification_input')
    day = transaction['date']
    try:
        if type(day) is not str or date.fromisoformat(day).isoformat() != day:
            raise ValueError('invalid_classification_input')
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid_classification_input') from exc
    if transaction['direction'] not in ('income', 'expense'):
        raise ValueError('invalid_classification_input')
    categories = payload['allowed_categories']
    if type(categories) is not list or not categories or len(categories) > 100:
        raise ValueError('invalid_classification_input')
    ids = set()
    for category in categories:
        if type(category) is not dict or set(category) != {'id', 'label'}:
            raise ValueError('invalid_classification_input')
        _text(category['id'], 128)
        _text(category['label'], 200)
        if category['id'] in ids:
            raise ValueError('invalid_classification_input')
        ids.add(category['id'])
    return ids


def _answer(answer, ids):
    if type(answer) is not dict or set(answer) != {'category', 'confidence', 'reason'}:
        raise ValueError('invalid_backend_response')
    if (type(answer['category']) is not str or answer['category'] not in ids
            or type(answer['confidence']) is not str
            or answer['confidence'] not in ('low', 'medium', 'high')
            or type(answer['reason']) is not str or not answer['reason'].strip()
            or len(answer['reason']) > 300 or any(ord(ch) < 32 for ch in answer['reason'])):
        raise ValueError('invalid_backend_response')
    return dict(answer)


def classify(payload, *, codex, qwen):
    """Return a validated proposal; fallback only on an explicit quota failure."""
    ids = _input(payload)
    engine = 'codex_cli'
    fallback_reason = None
    # Protect the caller and the fallback from mutations made by an injected backend.
    original = deepcopy(payload)
    try:
        answer = codex(deepcopy(original))
    except BackendFailure as exc:
        if exc.code not in {'usage_limit_reached', 'quota_exhausted'}:
            raise
        fallback_reason = exc.code
        engine = 'qwen'
        try:
            answer = qwen(deepcopy(original))
        except BackendFailure as error:
            raise BackendFailure(error.code, fallback_reason=fallback_reason) from None
    try:
        validated = _answer(answer, ids)
    except ValueError:
        raise BackendFailure('invalid_backend_response', fallback_reason=fallback_reason) from None
    proposal = {'status': 'proposed', 'engine': engine, **validated}
    if fallback_reason is not None:
        proposal['fallback_reason'] = fallback_reason
    return proposal
