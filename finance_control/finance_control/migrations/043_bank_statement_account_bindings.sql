CREATE TABLE bank_statement_account_bindings (
    source_key TEXT PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE REFERENCES accounts(id)
);
