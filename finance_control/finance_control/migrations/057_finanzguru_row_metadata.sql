-- Keep source-row evidence separate from ledger amounts and preserve its exact provenance.
CREATE TABLE finanzguru_row_metadata (
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
    source_account TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    external_id TEXT NOT NULL,
    booked_on TEXT NOT NULL,
    currency TEXT NOT NULL,
    balance_amount TEXT,
    contract_id TEXT,
    contract_label TEXT,
    cadence_raw TEXT,
    interval_months INTEGER,
    PRIMARY KEY(source_sha256, source_account, source_reference),
    CHECK(balance_amount IS NULL OR length(balance_amount) > 0),
    CHECK(interval_months IS NULL OR interval_months > 0)
);
