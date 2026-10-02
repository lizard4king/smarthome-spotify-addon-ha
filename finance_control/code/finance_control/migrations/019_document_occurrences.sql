CREATE TABLE classification_document_fingerprints (
 document_id INTEGER PRIMARY KEY REFERENCES classification_documents(id) ON DELETE CASCADE,
 document_sha256 TEXT NOT NULL UNIQUE
  CHECK(length(document_sha256)=64 AND document_sha256 NOT GLOB '*[^0-9a-f]*'),
 UNIQUE(document_id, document_sha256)
);

CREATE TABLE classification_document_occurrences (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 document_id INTEGER NOT NULL,
 document_sha256 TEXT NOT NULL,
 source_sha256 TEXT NOT NULL
  CHECK(length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
 source_reference TEXT NOT NULL UNIQUE,
 FOREIGN KEY(document_id, document_sha256)
  REFERENCES classification_document_fingerprints(document_id, document_sha256)
  ON DELETE CASCADE,
 UNIQUE(document_sha256, source_sha256)
);

CREATE INDEX classification_document_occurrences_document
 ON classification_document_occurrences(document_id);
