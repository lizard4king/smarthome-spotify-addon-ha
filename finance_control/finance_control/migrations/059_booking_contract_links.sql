CREATE TABLE booking_contract_links (
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 rule_id INTEGER REFERENCES recurring_rules(id),
 decision TEXT NOT NULL CHECK(decision IN ('confirmed','rejected')),
 revision INTEGER NOT NULL CHECK(revision>=1),
 updated_at TEXT NOT NULL,
 PRIMARY KEY(account_id,external_id),
 FOREIGN KEY(account_id,external_id) REFERENCES transactions(account_id,external_id),
 CHECK(decision!='confirmed' OR rule_id IS NOT NULL)
);
