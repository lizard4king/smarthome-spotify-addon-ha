-- Local application identities and bank setup metadata; no credentials or ledger changes.
CREATE TABLE app_users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    subject TEXT UNIQUE,
    CHECK (length(email) BETWEEN 3 AND 254),
    CHECK (length(display_name) BETWEEN 1 AND 100)
);

CREATE TABLE app_bank_connections (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES app_users(id),
    bank_id TEXT NOT NULL CHECK (bank_id IN ('POSTBANK', 'ING', 'NASPA')),
    label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 100),
    status TEXT NOT NULL CHECK (status IN ('LOCAL_SETUP_REQUIRED', 'REVOKED')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)
);

CREATE INDEX app_bank_connections_user_id ON app_bank_connections(user_id);
