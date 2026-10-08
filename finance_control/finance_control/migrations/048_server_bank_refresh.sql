-- Owner-bound refresh selections only. No credentials, bank references or raw bookings.
CREATE TABLE server_bank_refresh_bindings (
    connection_id TEXT PRIMARY KEY REFERENCES app_bank_connections(id),
    owner_id TEXT NOT NULL REFERENCES app_users(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    account_fingerprint TEXT NOT NULL CHECK (
        length(account_fingerprint)=64 AND account_fingerprint NOT GLOB '*[^0-9a-f]*'),
    tan_method TEXT CHECK (tan_method IS NULL OR
                           (length(tan_method) BETWEEN 1 AND 8 AND tan_method NOT GLOB '*[^0-9]*')),
    tan_medium TEXT CHECK (tan_medium IS NULL OR length(tan_medium) BETWEEN 1 AND 32),
    bank_id TEXT NOT NULL CHECK (bank_id IN ('ING','POSTBANK')),
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_status TEXT NOT NULL DEFAULT 'setup_required'
        CHECK (last_status IN ('setup_required','running','updated','error')),
    last_code TEXT,
    last_inserted INTEGER CHECK (last_inserted IS NULL OR last_inserted>=0)
);

CREATE INDEX server_bank_refresh_owner ON server_bank_refresh_bindings(owner_id);
