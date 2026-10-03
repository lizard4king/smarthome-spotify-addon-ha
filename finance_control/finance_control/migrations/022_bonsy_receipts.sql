CREATE TABLE bonsy_imports (
 source_sha256 TEXT PRIMARY KEY
  CHECK(length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
 imported_at TEXT NOT NULL,
 receipt_count INTEGER NOT NULL CHECK(receipt_count>=0),
 product_count INTEGER NOT NULL CHECK(product_count>=0)
);

CREATE TABLE bonsy_receipts (
 entry_id TEXT PRIMARY KEY,
 document_id INTEGER NOT NULL UNIQUE REFERENCES classification_documents(id),
 source_sha256 TEXT NOT NULL REFERENCES bonsy_imports(source_sha256),
 occurred_at TEXT NOT NULL,
 vendor TEXT NOT NULL,
 branch TEXT,
 total TEXT NOT NULL,
 currency TEXT NOT NULL CHECK(currency='EUR')
);

CREATE TABLE bonsy_products (
 entry_id TEXT NOT NULL REFERENCES bonsy_receipts(entry_id) ON DELETE CASCADE,
 line_number INTEGER NOT NULL CHECK(line_number>=1),
 product_name TEXT NOT NULL,
 source_category TEXT,
 paid_price TEXT,
 quantity TEXT,
 unit TEXT,
 unit_price TEXT,
 discount TEXT,
 discount_name TEXT,
 PRIMARY KEY(entry_id,line_number)
);

CREATE INDEX bonsy_receipts_document ON bonsy_receipts(document_id);
