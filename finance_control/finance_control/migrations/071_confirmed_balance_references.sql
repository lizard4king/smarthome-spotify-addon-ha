CREATE TABLE confirmed_balance_references (
 id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 amount TEXT NOT NULL, currency TEXT NOT NULL, booked_on TEXT NOT NULL,
 source_reference TEXT NOT NULL, basis TEXT NOT NULL CHECK(basis IN ('statement_day_end','owner_confirmed_day')),
 actor TEXT NOT NULL, confirmed_at TEXT NOT NULL
);
CREATE INDEX confirmed_balance_reference_account ON confirmed_balance_references(account_id,booked_on,id);
CREATE TABLE confirmed_balance_reference_ledger (
 reference_id INTEGER NOT NULL REFERENCES confirmed_balance_references(id),
 external_id TEXT NOT NULL, date TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,
 PRIMARY KEY(reference_id,external_id)
);
CREATE TRIGGER confirmed_balance_references_no_update BEFORE UPDATE ON confirmed_balance_references BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER confirmed_balance_references_no_delete BEFORE DELETE ON confirmed_balance_references BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER confirmed_balance_reference_ledger_no_update BEFORE UPDATE ON confirmed_balance_reference_ledger BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER confirmed_balance_reference_ledger_no_delete BEFORE DELETE ON confirmed_balance_reference_ledger BEGIN SELECT RAISE(ABORT,'append_only'); END;
