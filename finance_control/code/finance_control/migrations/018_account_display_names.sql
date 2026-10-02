ALTER TABLE accounts ADD COLUMN display_name TEXT;
ALTER TABLE accounts ADD COLUMN display_name_revision INTEGER NOT NULL DEFAULT 0;

CREATE TABLE account_display_name_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 account_id TEXT NOT NULL REFERENCES accounts(id),
 revision INTEGER NOT NULL,
 previous_display_name TEXT,
 display_name TEXT NOT NULL,
 changed_at TEXT NOT NULL,
 UNIQUE(account_id, revision)
);
