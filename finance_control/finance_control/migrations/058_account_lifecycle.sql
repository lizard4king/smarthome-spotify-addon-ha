-- A closing date hides an account from current views without deleting its history.
ALTER TABLE accounts ADD COLUMN closed_on TEXT;
ALTER TABLE accounts ADD COLUMN lifecycle_revision INTEGER NOT NULL DEFAULT 0;

CREATE TABLE account_lifecycle_audit (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    revision INTEGER NOT NULL CHECK(revision > 0),
    previous_closed_on TEXT,
    closed_on TEXT,
    changed_at TEXT NOT NULL,
    PRIMARY KEY(account_id, revision)
);
