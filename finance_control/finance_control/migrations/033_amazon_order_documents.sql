CREATE TABLE amazon_order_documents (
 order_number TEXT PRIMARY KEY,
 document_id INTEGER NOT NULL UNIQUE REFERENCES classification_documents(id)
);
