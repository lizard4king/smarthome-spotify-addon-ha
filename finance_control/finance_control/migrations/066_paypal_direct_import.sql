CREATE TABLE paypal_source_accounts (
 source_key TEXT PRIMARY KEY,
 owner_id TEXT NOT NULL REFERENCES app_users(id),
 account_id TEXT NOT NULL UNIQUE REFERENCES accounts(id)
);
CREATE TABLE paypal_source_transactions (
 source_key TEXT NOT NULL REFERENCES paypal_source_accounts(source_key),
 entry_reference TEXT NOT NULL CHECK(length(trim(entry_reference)) BETWEEN 1 AND 256),
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 normalized_json TEXT NOT NULL,
 PRIMARY KEY(source_key,entry_reference),
 UNIQUE(account_id,external_id),
 FOREIGN KEY(account_id,external_id) REFERENCES transaction_keys(account_id,external_id)
);
CREATE TABLE paypal_import_receipts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_key TEXT NOT NULL REFERENCES paypal_source_accounts(source_key),
 account_id TEXT NOT NULL REFERENCES accounts(id),
 owner_id TEXT NOT NULL REFERENCES app_users(id),
 import_id INTEGER NOT NULL REFERENCES imports(id),
 occurred_at TEXT NOT NULL,
 actor TEXT NOT NULL,
 revision TEXT NOT NULL,
 receipt_json TEXT NOT NULL
);
CREATE TRIGGER paypal_receipts_no_update BEFORE UPDATE ON paypal_import_receipts
BEGIN SELECT RAISE(ABORT,'PayPal receipts are immutable'); END;
CREATE TRIGGER paypal_receipts_no_delete BEFORE DELETE ON paypal_import_receipts
BEGIN SELECT RAISE(ABORT,'PayPal receipts are immutable'); END;
CREATE TRIGGER paypal_registry_no_update BEFORE UPDATE ON paypal_source_transactions
BEGIN SELECT RAISE(ABORT,'PayPal registry is immutable'); END;
CREATE TRIGGER paypal_registry_no_delete BEFORE DELETE ON paypal_source_transactions
BEGIN SELECT RAISE(ABORT,'PayPal registry is immutable'); END;
CREATE TRIGGER paypal_binding_no_update BEFORE UPDATE ON paypal_source_accounts
BEGIN SELECT RAISE(ABORT,'PayPal binding is immutable'); END;
CREATE TRIGGER paypal_binding_no_delete BEFORE DELETE ON paypal_source_accounts
BEGIN SELECT RAISE(ABORT,'PayPal binding is immutable'); END;
