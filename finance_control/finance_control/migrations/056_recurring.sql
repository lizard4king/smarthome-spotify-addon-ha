CREATE TABLE recurring_rules (
 id INTEGER PRIMARY KEY,
 revision INTEGER NOT NULL CHECK(revision>=1),
 document_id INTEGER UNIQUE REFERENCES classification_documents(id),
 payload TEXT NOT NULL,
 active INTEGER NOT NULL CHECK(active IN (0,1)),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
