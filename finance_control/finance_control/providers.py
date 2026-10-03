"""Read-only, source-neutral contract for financial data providers.

This module only describes and validates data. It does not access a database,
credentials, banks, or the network.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, TypeVar


class ProviderError(ValueError):
    """A safe, public provider-contract error."""


class ProviderCapability(StrEnum):
    ACCOUNTS = "accounts"
    BALANCES = "balances"
    TRANSACTIONS = "transactions"
    DOCUMENTS = "documents"
    LINE_ITEMS = "line_items"
    POSITIONS = "positions"


def _nonempty(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _money(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _currency(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) is not None


def _calendar_day(value: date) -> bool:
    return type(value) is date


_T = TypeVar("_T")


def _deduplicate(
    rows: tuple[_T, ...],
    identity: Callable[[_T], Hashable],
    conflict_message: str,
) -> tuple[_T, ...]:
    unique: dict[Hashable, _T] = {}
    for row in rows:
        key = identity(row)
        if key in unique:
            if unique[key] != row:
                raise ProviderError(conflict_message)
            continue
        unique[key] = row
    return tuple(unique.values())


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    display_name: str
    version: str
    capabilities: frozenset[ProviderCapability]

    def __post_init__(self) -> None:
        if not all(_nonempty(value) for value in (self.provider_id, self.display_name, self.version)):
            raise ProviderError("Provider-Metadaten sind unvollständig.")
        if not isinstance(self.capabilities, frozenset) or not all(
            isinstance(value, ProviderCapability) for value in self.capabilities
        ):
            raise ProviderError("Provider-Fähigkeiten sind ungültig.")


@dataclass(frozen=True)
class AccountLink:
    external_account_id: str
    internal_account_id: str

    def __post_init__(self) -> None:
        if not _nonempty(self.external_account_id) or not _nonempty(self.internal_account_id):
            raise ProviderError("Kontozuordnung ist unvollständig.")


_SECRET_KEY = re.compile(
    r"password|passwd|secret|token|api.?key|access.?key|private.?key|credential|"
    r"(?<![a-z])(?:pin|tan)(?![a-z])|auth|bearer|session",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    profile_id: str
    enabled: bool = True
    account_links: tuple[AccountLink, ...] = ()
    options: Mapping[str, str | int | float | bool | None] = field(default_factory=dict, repr=False)
    credential_ref: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not _nonempty(self.provider_id) or not _nonempty(self.profile_id):
            raise ProviderError("Provider-Konfiguration ist unvollständig.")
        if type(self.enabled) is not bool:
            raise ProviderError("Provider-Aktivierung ist ungültig.")
        if not isinstance(self.account_links, tuple) or not all(
            isinstance(link, AccountLink) for link in self.account_links
        ):
            raise ProviderError("Kontozuordnungen sind ungültig.")
        external_ids = [link.external_account_id for link in self.account_links]
        internal_ids = [link.internal_account_id for link in self.account_links]
        if len(external_ids) != len(set(external_ids)) or len(internal_ids) != len(set(internal_ids)):
            raise ProviderError("Konten sind innerhalb eines Provider-Profils mehrfach zugeordnet.")
        if self.credential_ref is not None:
            if not isinstance(self.credential_ref, str) or not self.credential_ref.startswith("wincred:"):
                raise ProviderError("Credential-Referenz ist ungültig.")
            alias = self.credential_ref.removeprefix("wincred:")
            if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", alias) is None:
                raise ProviderError("Credential-Referenz ist ungültig.")
        if not isinstance(self.options, Mapping):
            raise ProviderError("Provider-Optionen sind ungültig.")
        options = dict(self.options)
        if any(
            not _nonempty(key) or _SECRET_KEY.search(key) or type(value) not in (str, int, float, bool, type(None))
            for key, value in options.items()
        ):
            raise ProviderError("Provider-Optionen enthalten ungültige oder geheime Felder.")
        object.__setattr__(self, "options", MappingProxyType(options))


@dataclass(frozen=True)
class SourceProvenance:
    provider_id: str
    source: str
    version: str
    retrieved_at: datetime
    sha256: str

    def __post_init__(self) -> None:
        if not all(_nonempty(value) for value in (self.provider_id, self.source, self.version)):
            raise ProviderError("Quellnachweis ist unvollständig.")
        if not isinstance(self.retrieved_at, datetime) or self.retrieved_at.tzinfo is None or (
            self.retrieved_at.utcoffset() != UTC.utcoffset(self.retrieved_at)
        ):
            raise ProviderError("Quellzeit muss UTC enthalten.")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256) is None:
            raise ProviderError("Quellprüfsumme muss SHA-256 sein.")


@dataclass(frozen=True)
class SourceAccount:
    external_account_id: str
    display_name: str
    currency: str


@dataclass(frozen=True)
class SourceBalance:
    external_account_id: str
    amount: Decimal
    currency: str
    booked_on: date


@dataclass(frozen=True)
class SourceTransaction:
    external_account_id: str
    external_id: str
    booked_on: date
    amount: Decimal
    currency: str
    description: str = ""
    internal_account_id: str | None = None

    def identity(self, provider_id: str) -> tuple[str, str, str]:
        """Stable source identity, independent of the local account mapping."""
        return provider_id, self.external_account_id, self.external_id


@dataclass(frozen=True)
class SourceDocument:
    external_id: str
    title: str
    issued_on: date | None = None
    external_account_id: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    related_external_id: str | None = None


@dataclass(frozen=True)
class SourceLineItem:
    external_id: str
    document_external_id: str
    description: str
    amount: Decimal | None = None
    currency: str | None = None
    quantity: Decimal | None = None
    product_reference: str | None = None


@dataclass(frozen=True)
class SourcePosition:
    external_account_id: str
    external_id: str
    name: str
    quantity: Decimal
    currency: str | None = None
    value: Decimal | None = None
    internal_account_id: str | None = None


def _link_internal_account(
    row: SourceTransaction | SourcePosition,
    mapping: Mapping[str, str],
) -> SourceTransaction | SourcePosition:
    if row.internal_account_id is not None:
        raise ProviderError("Provider darf keine interne Kontozuordnung liefern.")
    internal_id = mapping.get(row.external_account_id)
    if internal_id is None:
        raise ProviderError("Externes Konto ist nicht zugeordnet.")
    return replace(row, internal_account_id=internal_id)


@dataclass(frozen=True)
class ProviderBatch:
    provenance: SourceProvenance
    accounts: tuple[SourceAccount, ...] = ()
    balances: tuple[SourceBalance, ...] = ()
    transactions: tuple[SourceTransaction, ...] = ()
    documents: tuple[SourceDocument, ...] = ()
    line_items: tuple[SourceLineItem, ...] = ()
    positions: tuple[SourcePosition, ...] = ()


class ProviderAdapter(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def fetch(self, config: ProviderConfig) -> ProviderBatch: ...


class ProviderRegistry:
    """Explicit adapter allowlist and validation boundary; no persistence."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        self._descriptors: dict[str, ProviderDescriptor] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        try:
            descriptor = adapter.descriptor
        except Exception:  # noqa: BLE001 - adapter errors may contain private configuration
            raise ProviderError("Provider-Beschreibung ist ungültig.") from None
        if not isinstance(descriptor, ProviderDescriptor):
            raise ProviderError("Provider-Beschreibung ist ungültig.")
        if descriptor.provider_id in self._adapters:
            raise ProviderError("Provider ist bereits registriert.")
        self._adapters[descriptor.provider_id] = adapter
        self._descriptors[descriptor.provider_id] = descriptor

    def collect(self, config: ProviderConfig) -> ProviderBatch | None:
        if not config.enabled:
            return None
        adapter = self._adapters.get(config.provider_id)
        if adapter is None:
            raise ProviderError("Provider ist nicht registriert.")
        descriptor = self._descriptors[config.provider_id]
        try:
            batch = adapter.fetch(config)
        except Exception:  # noqa: BLE001 - adapter exceptions may contain private source data
            # Never leak source payload, option values, or credential material.
            raise ProviderError("Provider-Abruf ist fehlgeschlagen.") from None
        if not isinstance(batch, ProviderBatch):
            raise ProviderError("Provider hat keinen gültigen Batch geliefert.")
        if not isinstance(batch.provenance, SourceProvenance):
            raise ProviderError("Quellnachweis ist ungültig.")
        if batch.provenance.provider_id != descriptor.provider_id or batch.provenance.version != descriptor.version:
            raise ProviderError("Quellnachweis passt nicht zum Provider.")
        content = {
            ProviderCapability.ACCOUNTS: batch.accounts,
            ProviderCapability.BALANCES: batch.balances,
            ProviderCapability.TRANSACTIONS: batch.transactions,
            ProviderCapability.DOCUMENTS: batch.documents,
            ProviderCapability.LINE_ITEMS: batch.line_items,
            ProviderCapability.POSITIONS: batch.positions,
        }
        if any(rows and capability not in descriptor.capabilities for capability, rows in content.items()):
            raise ProviderError("Provider liefert nicht deklarierte Daten.")
        expected = {
            ProviderCapability.ACCOUNTS: SourceAccount,
            ProviderCapability.BALANCES: SourceBalance,
            ProviderCapability.TRANSACTIONS: SourceTransaction,
            ProviderCapability.DOCUMENTS: SourceDocument,
            ProviderCapability.LINE_ITEMS: SourceLineItem,
            ProviderCapability.POSITIONS: SourcePosition,
        }
        if any(not isinstance(rows, tuple) or not all(isinstance(row, expected[capability]) for row in rows)
               for capability, rows in content.items()):
            raise ProviderError("Provider-Batch enthält ungültige Datensätze.")
        mapping = {link.external_account_id: link.internal_account_id for link in config.account_links}
        if any(
            not _nonempty(row.external_account_id)
            or not _nonempty(row.display_name)
            or not _currency(row.currency)
            for row in batch.accounts
        ):
            raise ProviderError("Quellkonten sind ungültig.")
        accounts = _deduplicate(
            batch.accounts,
            lambda row: row.external_account_id,
            "Widersprüchliche Quellkontoidentität im Batch.",
        )
        account_ids = {row.external_account_id for row in accounts}
        if any(
            not _nonempty(row.external_account_id)
            or not _money(row.amount)
            or not _currency(row.currency)
            or not _calendar_day(row.booked_on)
            for row in batch.balances
        ):
            raise ProviderError("Saldo ist ungültig.")
        balances = _deduplicate(
            batch.balances,
            lambda row: (row.external_account_id, row.booked_on, row.currency),
            "Widersprüchliche Saldoidentität im Batch.",
        )
        if any(row.external_account_id not in account_ids for row in balances):
            raise ProviderError("Saldo verweist auf kein gültiges Quellkonto.")
        if any(
            not _nonempty(row.external_id)
            or not _nonempty(row.title)
            or (row.issued_on is not None and not _calendar_day(row.issued_on))
            or (row.external_account_id is not None and not _nonempty(row.external_account_id))
            or ((row.amount is None) != (row.currency is None))
            or (row.amount is not None and not _money(row.amount))
            or (row.currency is not None and not _currency(row.currency))
            or (row.related_external_id is not None and not _nonempty(row.related_external_id))
            for row in batch.documents
        ):
            raise ProviderError("Quellbelege sind ungültig.")
        documents = _deduplicate(
            batch.documents,
            lambda row: row.external_id,
            "Widersprüchliche Belegidentität im Batch.",
        )
        document_ids = {row.external_id for row in documents}
        if any(row.external_account_id is not None and row.external_account_id not in account_ids
               for row in documents):
            raise ProviderError("Beleg verweist auf kein Quellkonto.")
        if any(row.related_external_id is not None and row.related_external_id not in document_ids
               for row in documents):
            raise ProviderError("Beleg verweist auf keinen gültigen Quellbeleg.")
        if any(
            not _nonempty(row.external_id)
            or not _nonempty(row.document_external_id)
            or not _nonempty(row.description)
            or ((row.amount is None) != (row.currency is None))
            or (row.amount is not None and not _money(row.amount))
            or (row.currency is not None and not _currency(row.currency))
            or (row.quantity is not None and (not _money(row.quantity) or row.quantity <= 0))
            or (row.product_reference is not None and not _nonempty(row.product_reference))
            for row in batch.line_items
        ):
            raise ProviderError("Belegposition ist ungültig.")
        line_items = _deduplicate(
            batch.line_items,
            lambda row: (row.document_external_id, row.external_id),
            "Widersprüchliche Belegpositionsidentität im Batch.",
        )
        if any(row.document_external_id not in document_ids for row in line_items):
            raise ProviderError("Belegposition verweist auf keinen gültigen Beleg.")
        if any(
            not _nonempty(row.external_account_id)
            or not _nonempty(row.external_id)
            or not _calendar_day(row.booked_on)
            or not _money(row.amount)
            or not _currency(row.currency)
            or not isinstance(row.description, str)
            for row in batch.transactions
        ):
            raise ProviderError("Transaktion ist unvollständig.")
        transactions = _deduplicate(
            batch.transactions,
            lambda row: row.identity(descriptor.provider_id),
            "Widersprüchliche Transaktionsidentität im Batch.",
        )
        mapped = []
        for row in transactions:
            if row.external_account_id not in account_ids:
                raise ProviderError("Transaktion ist unvollständig.")
            mapped.append(_link_internal_account(row, mapping))
        if any(
            not _nonempty(row.external_account_id)
            or not _nonempty(row.external_id)
            or not _nonempty(row.name)
            or not _money(row.quantity)
            or (row.value is not None and not _money(row.value))
            or (row.currency is not None and not _currency(row.currency))
            or (row.value is not None and row.currency is None)
            for row in batch.positions
        ):
            raise ProviderError("Position ist unvollständig.")
        source_positions = _deduplicate(
            batch.positions,
            lambda row: (row.external_account_id, row.external_id),
            "Widersprüchliche Positionsidentität im Batch.",
        )
        positions = []
        for row in source_positions:
            if row.external_account_id not in account_ids:
                raise ProviderError("Position ist unvollständig.")
            positions.append(_link_internal_account(row, mapping))
        return replace(
            batch,
            accounts=accounts,
            balances=balances,
            transactions=tuple(mapped),
            documents=documents,
            line_items=line_items,
            positions=tuple(positions),
        )
