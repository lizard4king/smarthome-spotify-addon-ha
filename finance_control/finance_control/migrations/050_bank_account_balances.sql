-- The last explicitly reported bank balance is separate from period controls.
CREATE TABLE bank_account_balances (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id),
    source_key TEXT NOT NULL REFERENCES bank_source_accounts(source_key),
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    booked_on TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);
