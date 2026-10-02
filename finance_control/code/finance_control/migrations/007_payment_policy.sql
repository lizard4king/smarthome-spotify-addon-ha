CREATE TABLE payment_policy_snapshots (
 revision INTEGER PRIMARY KEY CHECK(revision > 0),
 created_at TEXT NOT NULL,
 payload TEXT NOT NULL CHECK(json_valid(payload)),
 base_revision INTEGER REFERENCES payment_policy_snapshots(revision)
);

CREATE TRIGGER payment_policy_snapshots_no_update
BEFORE UPDATE ON payment_policy_snapshots
BEGIN
 SELECT RAISE(ABORT, 'payment policy snapshots are immutable');
END;

CREATE TRIGGER payment_policy_snapshots_no_delete
BEFORE DELETE ON payment_policy_snapshots
BEGIN
 SELECT RAISE(ABORT, 'payment policy snapshots are immutable');
END;
