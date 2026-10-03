CREATE TABLE wealth_snapshots (
 id INTEGER PRIMARY KEY,
 revision INTEGER NOT NULL UNIQUE CHECK(revision > 0),
 as_of TEXT NOT NULL CHECK(as_of GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
 created_at TEXT NOT NULL,
 source_digest TEXT NOT NULL CHECK(length(source_digest) = 64),
 payload TEXT NOT NULL CHECK(json_valid(payload))
);

CREATE TRIGGER wealth_snapshots_no_update
BEFORE UPDATE ON wealth_snapshots
BEGIN
 SELECT RAISE(ABORT, 'wealth snapshots are immutable');
END;

CREATE TRIGGER wealth_snapshots_no_delete
BEFORE DELETE ON wealth_snapshots
BEGIN
 SELECT RAISE(ABORT, 'wealth snapshots are immutable');
END;
