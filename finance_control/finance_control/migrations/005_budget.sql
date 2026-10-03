CREATE TABLE budget_snapshots (
 revision INTEGER PRIMARY KEY CHECK(revision > 0),
 created_at TEXT NOT NULL,
 payload TEXT NOT NULL CHECK(json_valid(payload))
);

CREATE TRIGGER budget_snapshots_no_update
BEFORE UPDATE ON budget_snapshots
BEGIN
 SELECT RAISE(ABORT, 'budget snapshots are immutable');
END;

CREATE TRIGGER budget_snapshots_no_delete
BEFORE DELETE ON budget_snapshots
BEGIN
 SELECT RAISE(ABORT, 'budget snapshots are immutable');
END;
