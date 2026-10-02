CREATE TABLE monthly_reviews (
 id INTEGER PRIMARY KEY,
 period TEXT NOT NULL CHECK(period GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
 revision INTEGER NOT NULL CHECK(revision > 0),
 created_at TEXT NOT NULL,
 source_digest TEXT NOT NULL CHECK(length(source_digest) = 64),
 payload TEXT NOT NULL CHECK(json_valid(payload)),
 UNIQUE(period, revision)
);
