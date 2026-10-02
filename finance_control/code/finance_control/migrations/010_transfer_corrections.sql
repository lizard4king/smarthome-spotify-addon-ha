CREATE TABLE transfer_correction_pairs (
 id INTEGER PRIMARY KEY,
 created_at TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision=1)
);

CREATE TABLE transfer_correction_members (
 pair_id INTEGER NOT NULL REFERENCES transfer_correction_pairs(id),
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision=1),
 PRIMARY KEY(pair_id, account_id, external_id),
 UNIQUE(account_id, external_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transactions(account_id, external_id)
);

CREATE TABLE transfer_correction_audit (
 id INTEGER PRIMARY KEY,
 occurred_at TEXT NOT NULL,
 action TEXT NOT NULL,
 pair_id INTEGER NOT NULL REFERENCES transfer_correction_pairs(id),
 current TEXT NOT NULL
);
