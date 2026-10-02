-- A proven transfer can be one-sided when the other account is outside the imported ledger.
CREATE TABLE transfer_single_legs (
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 reason TEXT NOT NULL,
 created_at TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision=1),
 PRIMARY KEY(account_id, external_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transactions(account_id, external_id)
);

CREATE TABLE transfer_single_leg_audit (
 id INTEGER PRIMARY KEY,
 occurred_at TEXT NOT NULL,
 action TEXT NOT NULL,
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 current TEXT NOT NULL
);
