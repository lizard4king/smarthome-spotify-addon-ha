-- Confirmed local card targets, repeat-safe booking provenance and current balance evidence.
ALTER TABLE server_cards ADD COLUMN connection_revision INTEGER;
UPDATE server_cards SET connection_revision=(SELECT revision FROM app_bank_connections c WHERE c.id=connection_id);
ALTER TABLE server_cards ADD COLUMN tan_method TEXT;
ALTER TABLE server_cards ADD COLUMN tan_medium TEXT;
ALTER TABLE server_cards ADD COLUMN last_attempt_at TEXT;
ALTER TABLE server_cards ADD COLUMN last_success_at TEXT;
ALTER TABLE server_cards ADD COLUMN last_status TEXT;
ALTER TABLE server_cards ADD COLUMN last_code TEXT;
ALTER TABLE server_cards ADD COLUMN last_inserted INTEGER;
ALTER TABLE server_cards ADD COLUMN last_triggered_by TEXT REFERENCES app_users(id);
CREATE TABLE card_sources (
 source_key TEXT PRIMARY KEY, card_id TEXT NOT NULL,
 connection_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 connection_revision INTEGER NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(id),
 UNIQUE(card_id,connection_revision)
);
CREATE TABLE card_booking_links (
 source_key TEXT NOT NULL REFERENCES card_sources(source_key), bank_key TEXT NOT NULL,
 account_id TEXT NOT NULL, external_id TEXT NOT NULL,
 booking_date TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,
 PRIMARY KEY(source_key,bank_key), UNIQUE(source_key,account_id,external_id),
 FOREIGN KEY(account_id,external_id) REFERENCES transaction_keys(account_id,external_id)
);
CREATE INDEX card_booking_target ON card_booking_links(account_id,external_id);
CREATE TABLE card_balance_observations (
 id INTEGER PRIMARY KEY, source_key TEXT NOT NULL REFERENCES card_sources(source_key),
 account_id TEXT NOT NULL REFERENCES accounts(id), amount TEXT NOT NULL,
 currency TEXT NOT NULL, booked_on TEXT NOT NULL, retrieved_at TEXT NOT NULL,
 UNIQUE(source_key,account_id,amount,currency,booked_on,retrieved_at)
);
