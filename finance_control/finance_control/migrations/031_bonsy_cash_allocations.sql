CREATE TABLE bonsy_cash_allocations (
 entry_id TEXT NOT NULL REFERENCES bonsy_receipts(entry_id),
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 allocated_amount TEXT NOT NULL CHECK(
   allocated_amount NOT GLOB '*[^0-9.]*'
   AND length(allocated_amount)>3
   AND substr(allocated_amount,-3,1)='.'
   AND CAST(allocated_amount AS REAL)>0),
 confirmed_at TEXT NOT NULL,
 PRIMARY KEY(entry_id,account_id,external_id),
 FOREIGN KEY(account_id,external_id) REFERENCES transactions(account_id,external_id)
);

CREATE INDEX bonsy_cash_allocations_transaction
 ON bonsy_cash_allocations(account_id,external_id);
