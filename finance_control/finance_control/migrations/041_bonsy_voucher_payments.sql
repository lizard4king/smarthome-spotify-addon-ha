CREATE TABLE bonsy_voucher_payments (
 entry_id TEXT PRIMARY KEY REFERENCES bonsy_receipts(entry_id) ON DELETE CASCADE,
 amount TEXT NOT NULL CHECK(
   amount NOT GLOB '*[^0-9.]*'
   AND length(amount)>3
   AND substr(amount,-3,1)='.'
   AND substr(amount,1,length(amount)-3) NOT GLOB '*[^0-9]*'
   AND CAST(amount AS REAL)>0),
 confirmed_at TEXT NOT NULL
);
