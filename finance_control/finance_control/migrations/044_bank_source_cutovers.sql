CREATE TABLE bank_source_accounts (
    source_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK(provider IN ('ING','NASPA','POSTBANK')),
    account_id TEXT NOT NULL UNIQUE REFERENCES accounts(id)
);

CREATE TABLE bank_monthly_adoptions (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    source_key TEXT NOT NULL REFERENCES bank_source_accounts(source_key),
    archive_sha256 TEXT NOT NULL,
    ledger_sha256 TEXT NOT NULL,
    booking_count INTEGER NOT NULL CHECK(booking_count >= 0),
    opening_balance TEXT NOT NULL,
    closing_balance TEXT NOT NULL,
    PRIMARY KEY(account_id, period_start),
    UNIQUE(source_key, period_start)
);

CREATE TRIGGER bank_monthly_adoptions_no_update
BEFORE UPDATE ON bank_monthly_adoptions
BEGIN SELECT RAISE(ABORT, 'monthly adoption records are immutable'); END;

CREATE TRIGGER bank_monthly_adoptions_no_delete
BEFORE DELETE ON bank_monthly_adoptions
BEGIN SELECT RAISE(ABORT, 'monthly adoption records are immutable'); END;
