CREATE TABLE persons (id TEXT PRIMARY KEY);
INSERT INTO persons VALUES ('ANDREAS'), ('ERLENE');
CREATE TABLE households (id TEXT PRIMARY KEY);
INSERT INTO households VALUES ('HOUSEHOLD');
CREATE TABLE institutions (id TEXT PRIMARY KEY);
CREATE TABLE accounts (
 id TEXT PRIMARY KEY, institution TEXT NOT NULL REFERENCES institutions(id),
 kind TEXT NOT NULL, owner TEXT NOT NULL CHECK(owner IN ('ANDREAS','ERLENE','JOINT')),
 household TEXT NOT NULL REFERENCES households(id), currency TEXT NOT NULL,
 opening TEXT NOT NULL, opening_date TEXT NOT NULL
);
CREATE TABLE ownership (
 account_id TEXT REFERENCES accounts(id), person_id TEXT REFERENCES persons(id),
 share TEXT NOT NULL, PRIMARY KEY(account_id, person_id)
);
CREATE TABLE imports (id INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL, imported_at TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE transactions (
 account_id TEXT NOT NULL REFERENCES accounts(id), external_id TEXT NOT NULL,
 date TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,
 category TEXT NOT NULL, transfer_id TEXT NOT NULL,
 import_id INTEGER NOT NULL REFERENCES imports(id),
 PRIMARY KEY(account_id, external_id)
);
CREATE TABLE scenarios (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
