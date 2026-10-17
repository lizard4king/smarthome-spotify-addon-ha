-- Ordinary bank controls are append-only observations, separate from the latest cache.
CREATE TABLE bank_balance_observations (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    source_key TEXT NOT NULL REFERENCES bank_source_accounts(source_key),
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    booked_on TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    UNIQUE(account_id,source_key,amount,currency,booked_on,retrieved_at)
);
CREATE INDEX bank_balance_observations_account
    ON bank_balance_observations(account_id,booked_on,retrieved_at);
-- Only the surviving last control can be recovered from the old cache.
INSERT INTO bank_balance_observations(account_id,source_key,amount,currency,booked_on,retrieved_at)
    SELECT account_id,source_key,amount,currency,booked_on,retrieved_at FROM bank_account_balances;
-- REPLACE can bypass DELETE triggers when recursive_triggers is disabled.
CREATE TRIGGER bank_balance_observations_no_replace
BEFORE INSERT ON bank_balance_observations
WHEN EXISTS (SELECT 1 FROM bank_balance_observations old WHERE old.id=NEW.id)
 OR EXISTS (SELECT 1 FROM bank_balance_observations old
    WHERE old.account_id=NEW.account_id AND old.source_key=NEW.source_key
      AND old.amount=NEW.amount AND old.currency=NEW.currency
      AND old.booked_on=NEW.booked_on AND old.retrieved_at=NEW.retrieved_at)
BEGIN SELECT RAISE(ABORT, 'bank observations are immutable'); END;
CREATE TRIGGER bank_balance_observations_no_update
BEFORE UPDATE ON bank_balance_observations
BEGIN SELECT RAISE(ABORT, 'bank observations are immutable'); END;
CREATE TRIGGER bank_balance_observations_no_delete
BEFORE DELETE ON bank_balance_observations
BEGIN SELECT RAISE(ABORT, 'bank observations are immutable'); END;

ALTER TABLE wealth_snapshots ADD COLUMN differences_confirmed INTEGER;
ALTER TABLE wealth_snapshots ADD COLUMN differences_confirmed_by TEXT;
ALTER TABLE wealth_snapshots ADD COLUMN differences_confirmed_at TEXT;
CREATE TRIGGER wealth_snapshots_confirmation_valid
BEFORE INSERT ON wealth_snapshots
WHEN NOT (
    (NEW.differences_confirmed IS 1
     AND NEW.differences_confirmed_by IS NOT NULL AND length(trim(NEW.differences_confirmed_by))>0
     AND NEW.differences_confirmed_at IS NOT NULL AND length(trim(NEW.differences_confirmed_at))>0)
    OR ((NEW.differences_confirmed IS NULL OR NEW.differences_confirmed IS 0)
        AND NEW.differences_confirmed_by IS NULL AND NEW.differences_confirmed_at IS NULL)
)
BEGIN SELECT RAISE(ABORT, 'invalid wealth difference confirmation'); END;

-- Preserve immutable wealth confirmations against SQLite REPLACE as well.
CREATE TRIGGER wealth_snapshots_no_replace
BEFORE INSERT ON wealth_snapshots
WHEN EXISTS (SELECT 1 FROM wealth_snapshots old WHERE old.id=NEW.id OR old.revision=NEW.revision)
BEGIN SELECT RAISE(ABORT, 'wealth snapshots are immutable'); END;
