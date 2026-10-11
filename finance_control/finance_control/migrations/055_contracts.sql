CREATE TABLE contract_metadata (
 document_id INTEGER PRIMARY KEY REFERENCES classification_documents(id),
 reference TEXT NOT NULL,
 valid_from TEXT NOT NULL,
 valid_to TEXT,
 origin TEXT NOT NULL CHECK(origin IN ('manual','check24')),
 phases TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>=1),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TRIGGER contract_metadata_contract_only
BEFORE INSERT ON contract_metadata
WHEN NOT EXISTS (SELECT 1 FROM classification_documents
                 WHERE id=NEW.document_id AND kind='contract' AND status='confirmed')
BEGIN
 SELECT RAISE(ABORT, 'confirmed_contract_required');
END;
CREATE TRIGGER contract_metadata_update_contract_only
BEFORE UPDATE OF document_id ON contract_metadata
WHEN NOT EXISTS (SELECT 1 FROM classification_documents
                 WHERE id=NEW.document_id AND kind='contract' AND status='confirmed')
BEGIN
 SELECT RAISE(ABORT, 'confirmed_contract_required');
END;
