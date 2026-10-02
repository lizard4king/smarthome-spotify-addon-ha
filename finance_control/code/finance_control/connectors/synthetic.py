"""Deterministic offline provider for contract examples and synthetic tests."""

from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256

from finance_control.providers import (
    ProviderBatch,
    ProviderCapability,
    ProviderConfig,
    ProviderDescriptor,
    SourceAccount,
    SourceBalance,
    SourceProvenance,
    SourceTransaction,
)


class SyntheticProviderAdapter:
    descriptor = ProviderDescriptor(
        provider_id="synthetic",
        display_name="Synthetische Quelle",
        version="1",
        capabilities=frozenset({
            ProviderCapability.ACCOUNTS,
            ProviderCapability.BALANCES,
            ProviderCapability.TRANSACTIONS,
        }),
    )

    def fetch(self, config: ProviderConfig) -> ProviderBatch:
        payload = b"finance-control-synthetic-provider-v1"
        return ProviderBatch(
            provenance=SourceProvenance(
                provider_id=self.descriptor.provider_id,
                source="offline-fixture",
                version=self.descriptor.version,
                retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
                sha256=sha256(payload).hexdigest(),
            ),
            accounts=(SourceAccount("synthetic-checking", "Beispielkonto", "EUR"),),
            balances=(SourceBalance("synthetic-checking", Decimal("95.00"), "EUR", date(2026, 1, 2)),),
            transactions=(SourceTransaction(
                "synthetic-checking", "synthetic-transaction-1", date(2026, 1, 2),
                Decimal("-5.00"), "EUR", "Synthetischer Einkauf",
            ),),
        )
