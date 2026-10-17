-- Rebuild under the controlled foreign-key-disabled migration boundary.
-- Child decisions retain their logical source identity across booking revisions.

CREATE TABLE transaction_keys (
 account_id TEXT NOT NULL REFERENCES accounts(id), external_id TEXT NOT NULL,
 PRIMARY KEY(account_id,external_id)
);
INSERT INTO transaction_keys SELECT account_id,external_id FROM transactions;

CREATE TEMP TABLE migration062_transaction_context AS SELECT * FROM transaction_context;

CREATE TEMP TABLE migration062_transfer_correction_members AS SELECT * FROM transfer_correction_members;

CREATE TEMP TABLE migration062_classification_model_reviews AS SELECT * FROM classification_model_reviews;

CREATE TEMP TABLE migration062_classification_document_links AS SELECT * FROM classification_document_links;

CREATE TEMP TABLE migration062_transfer_single_legs AS SELECT * FROM transfer_single_legs;

CREATE TEMP TABLE migration062_payment_mail_events AS SELECT * FROM payment_mail_events;

CREATE TEMP TABLE migration062_bonsy_cash_allocations AS SELECT * FROM bonsy_cash_allocations;

CREATE TEMP TABLE migration062_classification_model_rejections AS SELECT * FROM classification_model_rejections;

CREATE TEMP TABLE migration062_classification_overrides AS SELECT * FROM classification_overrides;

CREATE TEMP TABLE migration062_booking_contract_links AS SELECT * FROM booking_contract_links;

CREATE TEMP TABLE migration062_rejected_document_links AS SELECT * FROM rejected_document_links;

CREATE TEMP TABLE migration062_transactions AS SELECT * FROM transactions;

DROP TABLE transaction_context;

DROP TABLE transfer_correction_members;

DROP TABLE classification_model_reviews;

DROP TABLE classification_document_links;

DROP TABLE transfer_single_legs;

DROP TABLE payment_mail_events;

DROP TABLE bonsy_cash_allocations;

DROP TABLE classification_model_rejections;

DROP TABLE classification_overrides;

DROP TABLE booking_contract_links;

DROP TABLE rejected_document_links;

DROP TABLE transactions;

CREATE TABLE transactions (
 account_id TEXT NOT NULL REFERENCES accounts(id), external_id TEXT NOT NULL,
 date TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,
 category TEXT NOT NULL, transfer_id TEXT NOT NULL,
 import_id INTEGER NOT NULL REFERENCES imports(id),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(revision)='integer' AND revision>=1),
 voided_at TEXT, voided_by_event INTEGER REFERENCES finanzguru_snapshot_events(id),
 void_reason TEXT CHECK(void_reason IN ('superseded','voided')), voided_by TEXT,
 PRIMARY KEY(account_id,external_id,revision),
 FOREIGN KEY(account_id,external_id) REFERENCES transaction_keys(account_id,external_id),
 CHECK((voided_at IS NULL AND voided_by_event IS NULL AND void_reason IS NULL AND voided_by IS NULL)
    OR (voided_at IS NOT NULL AND length(trim(voided_at))>0 AND voided_by_event IS NOT NULL
        AND void_reason IS NOT NULL AND voided_by IS NOT NULL AND length(trim(voided_by))>0))
);
INSERT INTO transactions(account_id,external_id,date,amount,currency,category,transfer_id,import_id)
SELECT account_id,external_id,date,amount,currency,category,transfer_id,import_id FROM migration062_transactions;
DROP TABLE migration062_transactions;
CREATE INDEX transactions_document_match_idx
 ON transactions(currency,date,amount);
CREATE UNIQUE INDEX transactions_active_key ON transactions(account_id,external_id) WHERE voided_at IS NULL;
CREATE UNIQUE INDEX transactions_void_event ON transactions(voided_by_event) WHERE voided_by_event IS NOT NULL;
CREATE VIEW active_transaction_versions AS
SELECT * FROM transactions WHERE voided_at IS NULL;
CREATE VIEW active_transactions AS
SELECT account_id,external_id,date,amount,currency,category,transfer_id,import_id
FROM active_transaction_versions;

CREATE TABLE transaction_context (
 account_id TEXT NOT NULL, external_id TEXT NOT NULL,
 counterparty TEXT NOT NULL, description TEXT NOT NULL,
 PRIMARY KEY(account_id, external_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transaction_keys(account_id, external_id)
);

INSERT INTO transaction_context SELECT * FROM migration062_transaction_context;
DROP TABLE migration062_transaction_context;

CREATE TABLE transfer_correction_members (
 pair_id INTEGER NOT NULL REFERENCES transfer_correction_pairs(id),
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision=1),
 PRIMARY KEY(pair_id, account_id, external_id),
 UNIQUE(account_id, external_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transaction_keys(account_id, external_id)
);

INSERT INTO transfer_correction_members SELECT * FROM migration062_transfer_correction_members;
DROP TABLE migration062_transfer_correction_members;

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
 FOREIGN KEY(account_id, external_id) REFERENCES transaction_keys(account_id, external_id)
);

INSERT INTO classification_model_reviews SELECT * FROM migration062_classification_model_reviews;
DROP TABLE migration062_classification_model_reviews;

CREATE TABLE "classification_document_links" (
 account_id TEXT NOT NULL, external_id TEXT NOT NULL,
 document_id INTEGER NOT NULL REFERENCES classification_documents(id),
 allocated_amount TEXT NOT NULL CHECK(
   allocated_amount NOT GLOB '*[^0-9.]*'
   AND length(allocated_amount)>3
   AND substr(allocated_amount,-3,1)='.'
   AND substr(allocated_amount,1,length(allocated_amount)-3) NOT GLOB '*[^0-9]*'
   AND CAST(allocated_amount AS REAL)>0),
 allocation_type TEXT NOT NULL CHECK(allocation_type IN ('payment','refund','evidence')),
 PRIMARY KEY(account_id, external_id, document_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transaction_keys(account_id, external_id)
);

INSERT INTO classification_document_links SELECT * FROM migration062_classification_document_links;
DROP TABLE migration062_classification_document_links;

CREATE TABLE transfer_single_legs (
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 reason TEXT NOT NULL,
 created_at TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision=1),
 PRIMARY KEY(account_id, external_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transaction_keys(account_id, external_id)
);

INSERT INTO transfer_single_legs SELECT * FROM migration062_transfer_single_legs;
DROP TABLE migration062_transfer_single_legs;

CREATE TABLE payment_mail_events (
 id INTEGER PRIMARY KEY,
 source_key TEXT NOT NULL CHECK(length(source_key)=64),
 provider TEXT NOT NULL CHECK(provider IN ('klarna','paypal')),
 event_status TEXT NOT NULL CHECK(event_status IN ('authorization','processing','paid','payment_plan')),
 event_date TEXT NOT NULL CHECK(length(event_date)=10),
 amount TEXT,
 currency TEXT,
 provider_reference TEXT,
 account_id TEXT,
 external_id TEXT,
 match_status TEXT NOT NULL CHECK(match_status IN ('unique','ambiguous','unmatched','conflict','stale')),
 CHECK((amount IS NULL AND currency IS NULL) OR (
   currency='EUR' AND amount NOT GLOB '*[^0-9.]*'
   AND length(amount)>3 AND substr(amount,-3,1)='.'
 )),
 CHECK((account_id IS NULL)=(external_id IS NULL)),
 FOREIGN KEY(account_id,external_id) REFERENCES transaction_keys(account_id,external_id),
 UNIQUE(source_key,event_status)
);

INSERT INTO payment_mail_events SELECT * FROM migration062_payment_mail_events;
DROP TABLE migration062_payment_mail_events;

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
 FOREIGN KEY(account_id,external_id) REFERENCES transaction_keys(account_id,external_id)
);

INSERT INTO bonsy_cash_allocations SELECT * FROM migration062_bonsy_cash_allocations;
DROP TABLE migration062_bonsy_cash_allocations;

CREATE TABLE classification_model_rejections (
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 category_id TEXT NOT NULL REFERENCES category_catalog(id),
 PRIMARY KEY(account_id, external_id, category_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transaction_keys(account_id, external_id)
);

INSERT INTO classification_model_rejections SELECT * FROM migration062_classification_model_rejections;
DROP TABLE migration062_classification_model_rejections;

CREATE TABLE "classification_overrides" (
 account_id TEXT NOT NULL, external_id TEXT NOT NULL,
 category_id TEXT REFERENCES category_catalog(id),
 confirmed INTEGER NOT NULL CHECK(confirmed IN (0, 1)),
 revision INTEGER NOT NULL CHECK(revision >= 1),
 CHECK((confirmed=0 AND category_id IS NULL) OR (confirmed=1 AND category_id IS NOT NULL)),
 PRIMARY KEY(account_id, external_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transaction_keys(account_id, external_id)
);

INSERT INTO classification_overrides SELECT * FROM migration062_classification_overrides;
DROP TABLE migration062_classification_overrides;

CREATE TABLE booking_contract_links (
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 rule_id INTEGER REFERENCES recurring_rules(id),
 decision TEXT NOT NULL CHECK(decision IN ('confirmed','rejected')),
 revision INTEGER NOT NULL CHECK(revision>=1),
 updated_at TEXT NOT NULL,
 PRIMARY KEY(account_id,external_id),
 FOREIGN KEY(account_id,external_id) REFERENCES transaction_keys(account_id,external_id),
 CHECK(decision!='confirmed' OR rule_id IS NOT NULL)
);

INSERT INTO booking_contract_links SELECT * FROM migration062_booking_contract_links;
DROP TABLE migration062_booking_contract_links;

CREATE TABLE rejected_document_links (
    account_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    document_id INTEGER NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    actor TEXT NOT NULL DEFAULT 'system:local',
    changed_at TEXT,
    PRIMARY KEY(account_id, external_id, document_id),
    FOREIGN KEY(account_id, external_id)
        REFERENCES transaction_keys(account_id, external_id),
    FOREIGN KEY(document_id) REFERENCES classification_documents(id)
);

INSERT INTO rejected_document_links SELECT * FROM migration062_rejected_document_links;
DROP TABLE migration062_rejected_document_links;

CREATE INDEX classification_model_reviews_status_idx
 ON classification_model_reviews(status, confidence);

CREATE INDEX payment_mail_events_booking_idx
 ON payment_mail_events(account_id,external_id,event_date);

CREATE INDEX payment_mail_events_reference_idx
 ON payment_mail_events(provider,provider_reference,event_date);

CREATE INDEX bonsy_cash_allocations_transaction
 ON bonsy_cash_allocations(account_id,external_id);

-- Register source identities for explicit original-column synthetic inserts as well.
CREATE TRIGGER transactions_register_key BEFORE INSERT ON transactions
BEGIN
 INSERT OR IGNORE INTO transaction_keys(account_id,external_id) VALUES (NEW.account_id,NEW.external_id);
END;
CREATE TRIGGER transactions_insert_active_only BEFORE INSERT ON transactions
WHEN NEW.voided_at IS NOT NULL OR EXISTS (
 SELECT 1 FROM transactions WHERE account_id=NEW.account_id AND external_id=NEW.external_id AND voided_at IS NULL)
 OR NEW.revision != COALESCE((
 SELECT MAX(revision)+1 FROM transactions WHERE account_id=NEW.account_id AND external_id=NEW.external_id),1)
BEGIN SELECT RAISE(ABORT, 'new transaction must be the next active revision'); END;
CREATE TRIGGER transactions_no_delete BEFORE DELETE ON transactions
BEGIN SELECT RAISE(ABORT, 'transaction history is immutable'); END;
CREATE TRIGGER transactions_update_guard BEFORE UPDATE ON transactions
WHEN OLD.voided_at IS NOT NULL
 OR NEW.account_id IS NOT OLD.account_id OR NEW.external_id IS NOT OLD.external_id
 OR NEW.date IS NOT OLD.date OR NEW.amount IS NOT OLD.amount
 OR NEW.currency IS NOT OLD.currency OR NEW.import_id IS NOT OLD.import_id
 OR NEW.revision IS NOT OLD.revision
 OR (NEW.voided_at IS NOT NULL AND (NEW.category IS NOT OLD.category OR NEW.transfer_id IS NOT OLD.transfer_id))
BEGIN SELECT RAISE(ABORT, 'transaction history is immutable'); END;
CREATE TRIGGER transactions_void_proof BEFORE UPDATE ON transactions
WHEN NEW.voided_at IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM finanzguru_snapshot_events e
 WHERE e.id=NEW.voided_by_event AND e.account_id=OLD.account_id AND e.external_id=OLD.external_id
   AND e.target_account=OLD.account_id
   AND ((NEW.void_reason='superseded' AND e.action='updated') OR (NEW.void_reason='voided' AND e.action='removed'))
)
BEGIN SELECT RAISE(ABORT, 'transaction void requires matching snapshot event'); END;
