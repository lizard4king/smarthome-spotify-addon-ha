CREATE TABLE classification_document_links_new (
 account_id TEXT NOT NULL, external_id TEXT NOT NULL,
 document_id INTEGER NOT NULL REFERENCES classification_documents(id),
 allocated_amount TEXT NOT NULL CHECK(
   allocated_amount NOT GLOB '*[^0-9.]*'
   AND length(allocated_amount)>3
   AND substr(allocated_amount,-3,1)='.'
   AND substr(allocated_amount,1,length(allocated_amount)-3) NOT GLOB '*[^0-9]*'
   AND CAST(allocated_amount AS REAL)>0),
 allocation_type TEXT NOT NULL CHECK(allocation_type IN ('payment','refund')),
 PRIMARY KEY(account_id, external_id, document_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transactions(account_id, external_id)
);
INSERT INTO classification_document_links_new(account_id,external_id,document_id,allocated_amount,allocation_type)
SELECT l.account_id,l.external_id,l.document_id,
       CASE WHEN d.amount IS NOT NULL AND CAST(d.amount AS REAL)>0 THEN d.amount
            ELSE printf('%.2f', abs(CAST(t.amount AS REAL))) END,
       'payment'
FROM classification_document_links l
JOIN classification_documents d ON d.id=l.document_id
JOIN transactions t ON t.account_id=l.account_id AND t.external_id=l.external_id;
DROP TABLE classification_document_links;
ALTER TABLE classification_document_links_new RENAME TO classification_document_links;
