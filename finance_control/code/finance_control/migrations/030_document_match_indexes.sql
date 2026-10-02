CREATE INDEX classification_documents_match_idx
 ON classification_documents(kind,currency,document_date,amount);
CREATE INDEX classification_audit_link_rejection_idx
 ON classification_audit(action,account_id,external_id,document_id);
CREATE INDEX transactions_document_match_idx
 ON transactions(currency,date,amount);
