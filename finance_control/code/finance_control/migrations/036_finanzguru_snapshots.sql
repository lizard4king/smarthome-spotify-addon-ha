CREATE TABLE finanzguru_snapshot_heads (
 source_account TEXT PRIMARY KEY,
 target_account TEXT NOT NULL REFERENCES accounts(id),
 exported_at TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,
 import_id INTEGER NOT NULL REFERENCES imports(id)
);

CREATE TABLE finanzguru_snapshot_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 occurred_at TEXT NOT NULL,
 source_account TEXT NOT NULL,
 target_account TEXT NOT NULL,
 exported_at TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('inserted','updated','removed','accepted')),
 account_id TEXT,
 external_id TEXT,
 previous TEXT,
 current TEXT
);

CREATE TRIGGER finanzguru_snapshot_events_no_update
BEFORE UPDATE ON finanzguru_snapshot_events
BEGIN SELECT RAISE(ABORT, 'snapshot events are immutable'); END;

CREATE TRIGGER finanzguru_snapshot_events_no_delete
BEFORE DELETE ON finanzguru_snapshot_events
BEGIN SELECT RAISE(ABORT, 'snapshot events are immutable'); END;
