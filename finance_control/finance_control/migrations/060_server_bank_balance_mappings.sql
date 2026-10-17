-- Confirmed read-only balance bindings and immutable evidence; never a booking import grant.
CREATE TABLE server_bank_balance_bindings (
    connection_id TEXT NOT NULL REFERENCES app_bank_connections(id),
    account_fingerprint TEXT NOT NULL CHECK (
        length(account_fingerprint)=64 AND account_fingerprint NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL REFERENCES app_users(id),
    connection_revision INTEGER NOT NULL CHECK (connection_revision > 0),
    bank_id TEXT NOT NULL CHECK (bank_id IN ('POSTBANK','ING','NASPA')),
    account_id TEXT NOT NULL UNIQUE REFERENCES accounts(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (connection_id, account_fingerprint)
);

CREATE TABLE server_bank_balance_evidence (
    id INTEGER PRIMARY KEY,
    connection_id TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    owner_id TEXT NOT NULL REFERENCES app_users(id),
    connection_revision INTEGER NOT NULL CHECK (connection_revision > 0),
    bank_id TEXT NOT NULL CHECK (bank_id IN ('POSTBANK','ING','NASPA')),
    job_id TEXT NOT NULL CHECK (
        length(job_id)=32 AND job_id NOT GLOB '*[^0-9a-f]*'),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    masked_account TEXT NOT NULL CHECK (
        masked_account='••••' OR
        (length(masked_account)=8 AND substr(masked_account,1,4)='••••')),
    amount TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (length(currency)=3 AND currency GLOB '[A-Z][A-Z][A-Z]'),
    booked_on TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    FOREIGN KEY (connection_id, account_fingerprint)
        REFERENCES server_bank_balance_bindings(connection_id, account_fingerprint),
    UNIQUE (connection_id, account_fingerprint, job_id)
);

CREATE INDEX server_bank_balance_evidence_account
    ON server_bank_balance_evidence(account_id, booked_on, retrieved_at);

CREATE TRIGGER server_bank_balance_evidence_no_update
BEFORE UPDATE ON server_bank_balance_evidence
BEGIN SELECT RAISE(ABORT, 'bank balance evidence is immutable'); END;

CREATE TRIGGER server_bank_balance_evidence_no_delete
BEFORE DELETE ON server_bank_balance_evidence
BEGIN SELECT RAISE(ABORT, 'bank balance evidence is immutable'); END;
