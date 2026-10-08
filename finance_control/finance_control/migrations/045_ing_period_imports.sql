-- Mutable checkpoint for a strictly append-only source prefix within a month.
CREATE TABLE ing_period_imports (
    period_key TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    source_key TEXT NOT NULL REFERENCES bank_source_accounts(source_key),
    month_start TEXT NOT NULL,
    as_of TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL,
    source_category TEXT NOT NULL,
    opening_balance TEXT NOT NULL,
    closing_balance TEXT NOT NULL,
    currency TEXT NOT NULL,
    row_payload_json TEXT NOT NULL,
    booking_count INTEGER NOT NULL CHECK(booking_count >= 0),
    UNIQUE(account_id, month_start),
    UNIQUE(source_key, month_start)
);
