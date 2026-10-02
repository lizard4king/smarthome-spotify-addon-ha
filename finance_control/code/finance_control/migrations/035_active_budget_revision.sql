CREATE TABLE budget_activations (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 revision INTEGER NOT NULL REFERENCES budget_snapshots(revision),
 activated_at TEXT NOT NULL
);

CREATE TRIGGER budget_activations_no_update
BEFORE UPDATE ON budget_activations
BEGIN
 SELECT RAISE(ABORT, 'budget activations are immutable');
END;

CREATE TRIGGER budget_activations_no_delete
BEFORE DELETE ON budget_activations
BEGIN
 SELECT RAISE(ABORT, 'budget activations are immutable');
END;

-- Pin the legacy behavior (latest snapshot is active) at migration time so a
-- later draft save cannot silently change the active plan.
INSERT INTO budget_activations(revision,activated_at)
SELECT MAX(revision),strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
FROM budget_snapshots
HAVING MAX(revision) IS NOT NULL;
