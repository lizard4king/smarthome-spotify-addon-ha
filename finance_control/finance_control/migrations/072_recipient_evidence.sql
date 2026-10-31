-- Operator-reviewed statement evidence; ledger/context remain unchanged.
CREATE TABLE recipient_evidence (
 account_id TEXT NOT NULL, external_id TEXT NOT NULL,
 context_fingerprint TEXT NOT NULL, recipient TEXT NOT NULL,
 evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL,
 PRIMARY KEY(account_id,external_id),
 FOREIGN KEY(account_id,external_id) REFERENCES transaction_keys(account_id,external_id)
);
CREATE TRIGGER recipient_evidence_no_update BEFORE UPDATE ON recipient_evidence
BEGIN SELECT RAISE(ABORT,'recipient_evidence_immutable'); END;
CREATE TRIGGER recipient_evidence_no_delete BEFORE DELETE ON recipient_evidence
BEGIN SELECT RAISE(ABORT,'recipient_evidence_immutable'); END;
CREATE TABLE recipient_evidence_audit (
 id INTEGER PRIMARY KEY, preview_digest TEXT NOT NULL, actor TEXT NOT NULL,
 created_at TEXT NOT NULL, inserted_count INTEGER NOT NULL,
 financial_data_unchanged INTEGER NOT NULL CHECK(financial_data_unchanged=1)
);
