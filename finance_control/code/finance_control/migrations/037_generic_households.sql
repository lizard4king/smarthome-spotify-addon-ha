-- Store.__init__ runs this migration with foreign_keys=OFF outside the transaction,
-- then checks every foreign key before committing and restores enforcement.
CREATE TABLE accounts_rebuilt (
 id TEXT PRIMARY KEY,
 institution TEXT NOT NULL REFERENCES institutions(id),
 kind TEXT NOT NULL,
 owner TEXT NOT NULL CHECK(
  length(owner) BETWEEN 1 AND 64
  AND owner GLOB '[A-Za-z0-9]*'
  AND owner NOT GLOB '*[^A-Za-z0-9_.-]*'),
 household TEXT NOT NULL REFERENCES households(id),
 currency TEXT NOT NULL,
 opening TEXT NOT NULL,
 opening_date TEXT NOT NULL,
 display_name TEXT,
 display_name_revision INTEGER NOT NULL DEFAULT 0
);

INSERT INTO accounts_rebuilt(
 id,institution,kind,owner,household,currency,opening,opening_date,
 display_name,display_name_revision)
SELECT id,institution,kind,owner,household,currency,opening,opening_date,
       display_name,display_name_revision
FROM accounts;

DROP TABLE accounts;
ALTER TABLE accounts_rebuilt RENAME TO accounts;
