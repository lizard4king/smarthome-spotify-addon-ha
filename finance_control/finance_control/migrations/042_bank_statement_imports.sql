CREATE TABLE bank_statement_imports (
    statement_key TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    archive_sha256 TEXT NOT NULL,
    source_category TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    booking_count INTEGER NOT NULL,
    opening_balance TEXT NOT NULL,
    closing_balance TEXT NOT NULL
);
