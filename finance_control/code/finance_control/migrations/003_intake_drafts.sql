CREATE TABLE intake_drafts (
 id INTEGER PRIMARY KEY CHECK(id = 1),
 batch TEXT NOT NULL UNIQUE,
 source_sha256 TEXT NOT NULL,
 payload TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision >= 1),
 activated INTEGER NOT NULL DEFAULT 0 CHECK(activated IN (0, 1))
);
