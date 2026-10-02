CREATE TABLE classification_model_reviews (
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 category_id TEXT NOT NULL REFERENCES category_catalog(id),
 confidence TEXT NOT NULL CHECK(confidence IN ('high','medium','low')),
 rationale TEXT NOT NULL,
 model TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','auto_confirmed')),
 reviewed_at TEXT NOT NULL,
 PRIMARY KEY(account_id, external_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transactions(account_id, external_id)
);

CREATE INDEX classification_model_reviews_status_idx
 ON classification_model_reviews(status, confidence);
