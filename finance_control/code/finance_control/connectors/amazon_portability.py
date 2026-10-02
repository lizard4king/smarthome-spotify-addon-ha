"""Offline adapter for Amazon Data Portability physical orders and return payments.

``totalOwed`` belongs to the whole order, although Amazon repeats it in each
product record. Product records do not contain a trustworthy unit price.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from finance_control.providers import (
    ProviderBatch,
    ProviderCapability,
    ProviderConfig,
    ProviderDescriptor,
    ProviderError,
    SourceDocument,
    SourceLineItem,
    SourceProvenance,
)


PROVIDER_ID = "amazon-portability"
_ORDER_FIELDS = frozenset({"marketplace", "eventDate", "orderDate", "productName", "website",
                           "orderId", "currencyCode", "quantity", "asin", "productCondition",
                           "totalOwed"})
_REFUND_FIELDS = frozenset({"marketplace", "eventDate", "orderId", "amountRefunded"})


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError("Doppelter JSON-Feldname in Amazon-Daten.")
        result[key] = value
    return result


def _array(payload: bytes | str, allowed: frozenset[str]) -> tuple[list[dict[str, Any]], bytes]:
    if isinstance(payload, bytes):
        raw = payload
    elif isinstance(payload, str):
        raw = payload.encode("utf-8")
    else:
        raise ProviderError("Amazon-Payload muss JSON-Text sein.")
    try:
        rows = json.loads(raw, parse_float=Decimal, object_pairs_hook=_object)
    except (UnicodeError, ValueError, TypeError):
        raise ProviderError("Amazon-Payload ist kein gültiges JSON.") from None
    if not isinstance(rows, list) or any(type(row) is not dict or not row.keys() <= allowed for row in rows):
        raise ProviderError("Amazon-Payload hat eine ungültige Struktur.")
    return rows, raw


def _text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if type(value) is not str or not value.strip():
        raise ProviderError("Amazon-Datensatz enthält ein ungültiges Textfeld.")
    return value


def _timestamp(row: Mapping[str, Any], key: str) -> datetime:
    value = _text(row, key)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value) is None:
        raise ProviderError("Amazon-Datum ist nicht ISO-8601-UTC.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ProviderError("Amazon-Datum ist ungültig.") from None


def _marketplace(row: Mapping[str, Any]) -> str:
    value = _text(row, "marketplace")
    if re.fullmatch(r"[A-Z]{2}", value) is None:
        raise ProviderError("Amazon-Marketplace ist ungültig.")
    return value


def _currency(row: Mapping[str, Any]) -> str:
    value = _text(row, "currencyCode")
    if re.fullmatch(r"[A-Z]{3}", value) is None:
        raise ProviderError("Amazon-Währung ist ungültig.")
    return value


def _amount(value: Any, *, refund: bool) -> Decimal:
    # Physical orders use a JSON number; return payments use a decimal string.
    if refund:
        if type(value) is not str or re.fullmatch(r"\d+(?:\.\d+)?", value) is None:
            raise ProviderError("Amazon-Erstattung hat keinen gültigen Betrag.")
    elif type(value) not in (int, Decimal):
        raise ProviderError("Amazon-Bestellung hat keinen gültigen Betrag.")
    result = Decimal(value)
    if not result.is_finite() or result < 0 or (refund and result == 0):
        raise ProviderError("Amazon-Betrag ist ungültig.")
    return result


def _digest(parts: tuple[Any, ...]) -> str:
    return sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":"),
                             default=str).encode("utf-8")).hexdigest()[:24]


def parse_amazon_portability(
    orders_payload: bytes | str,
    return_payments_payload: bytes | str = b"[]",
    *,
    retrieved_at: datetime,
) -> ProviderBatch:
    """Parse two official schema arrays without network or file access.

    A return payment has no currency in Amazon's schema. It is accepted only
    when its order occurs in this same batch, supplying an unambiguous currency.
    """
    if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None or (
        retrieved_at.utcoffset() != UTC.utcoffset(retrieved_at)
    ):
        raise ProviderError("Quellzeit muss UTC enthalten.")
    orders, orders_raw = _array(orders_payload, _ORDER_FIELDS)
    refunds, refunds_raw = _array(return_payments_payload, _REFUND_FIELDS)
    documents: dict[str, SourceDocument] = {}
    items: dict[str, SourceLineItem] = {}
    order_context: dict[str, tuple[str, datetime]] = {}
    for row in orders:
        marketplace = _marketplace(row)
        _timestamp(row, "eventDate")
        order_timestamp = _timestamp(row, "orderDate")
        order_id = _text(row, "orderId")
        name = _text(row, "productName")
        asin = _text(row, "asin")
        for optional_text in ("website", "productCondition"):
            if optional_text in row:
                _text(row, optional_text)
        currency = _currency(row)
        quantity = row.get("quantity")
        if type(quantity) is not int or quantity <= 0:
            raise ProviderError("Amazon-Menge ist ungültig.")
        total = _amount(row.get("totalOwed"), refund=False)
        doc_id = f"amazon:order:{order_id}"
        context = (marketplace, order_timestamp)
        if doc_id in order_context and order_context[doc_id] != context:
            raise ProviderError("Widersprüchliche Amazon-Bestelldaten.")
        order_context[doc_id] = context
        document = SourceDocument(doc_id, f"Amazon-Bestellung {order_id}", order_timestamp.date(),
                                  amount=total, currency=currency)
        previous = documents.setdefault(doc_id, document)
        if previous != document:
            raise ProviderError("Widersprüchliche Amazon-Bestelldaten.")
        # No position index exists in the schema. Exact duplicate product
        # records are therefore treated as one source observation.
        item_id = f"amazon:item:{_digest((order_id, asin, name, quantity))}"
        items[item_id] = SourceLineItem(item_id, doc_id, name,
                                        quantity=Decimal(quantity), product_reference=asin)

    for row in refunds:
        marketplace = _marketplace(row)
        event_date = _timestamp(row, "eventDate")
        order_id = _text(row, "orderId")
        amount = _amount(row.get("amountRefunded"), refund=True)
        order = documents.get(f"amazon:order:{order_id}")
        if order is None or order.currency is None or order_context[order.external_id][0] != marketplace:
            raise ProviderError("Amazon-Erstattung ohne eindeutige Bestellwährung.")
        refund_id = f"amazon:refund:{_digest((marketplace, event_date.isoformat(), order_id, str(amount)))}"
        document = SourceDocument(refund_id, f"Amazon-Erstattung {order_id}",
                                  event_date.date(), amount=-amount, currency=order.currency,
                                  related_external_id=order.external_id)
        documents.setdefault(refund_id, document)

    checksum = sha256(b"orders\0" + orders_raw + b"\0return-payments\0" + refunds_raw).hexdigest()
    provenance = SourceProvenance(PROVIDER_ID, "amazon:data-portability:physical-orders+return-payments",
                                  "1", retrieved_at, checksum)
    return ProviderBatch(provenance, documents=tuple(documents.values()), line_items=tuple(items.values()))


class AmazonPortabilityAdapter:
    """Adapter with an injected, offline source of the two JSON arrays."""

    descriptor = ProviderDescriptor(PROVIDER_ID, "Amazon Data Portability", "1",
                                    frozenset({ProviderCapability.DOCUMENTS, ProviderCapability.LINE_ITEMS}))

    def __init__(self, payload_source: Callable[[], tuple[bytes | str, bytes | str]], *,
                 retrieved_at: Callable[[], datetime] | None = None) -> None:
        self._payload_source = payload_source
        self._retrieved_at = retrieved_at or (lambda: datetime.now(UTC))

    def fetch(self, config: ProviderConfig) -> ProviderBatch:
        if config.provider_id != PROVIDER_ID:
            raise ProviderError("Provider-Konfiguration passt nicht zu Amazon.")
        orders, return_payments = self._payload_source()
        return parse_amazon_portability(orders, return_payments, retrieved_at=self._retrieved_at())
