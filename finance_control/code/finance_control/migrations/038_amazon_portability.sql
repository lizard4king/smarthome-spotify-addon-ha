CREATE TABLE amazon_portability_documents (
 external_id TEXT PRIMARY KEY,
 content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
 document_id INTEGER NOT NULL REFERENCES classification_documents(id),
 related_external_id TEXT REFERENCES amazon_portability_documents(external_id),
 amount TEXT,
 currency TEXT,
 retrieved_at TEXT NOT NULL,
 source TEXT NOT NULL,
 source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
 CHECK ((amount IS NULL) = (currency IS NULL))
);
CREATE INDEX amazon_portability_documents_document_idx
 ON amazon_portability_documents(document_id, related_external_id);
CREATE TABLE amazon_portability_line_items (
 document_external_id TEXT NOT NULL REFERENCES amazon_portability_documents(external_id),
 external_id TEXT NOT NULL,
 content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
 description TEXT NOT NULL,
 quantity TEXT,
 product_reference TEXT,
 amount TEXT,
 currency TEXT,
 CHECK ((amount IS NULL) = (currency IS NULL)),
 PRIMARY KEY(document_external_id, external_id)
);
