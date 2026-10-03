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
 FOREIGN KEY(account_id,external_id) REFERENCES transactions(account_id,external_id),
 UNIQUE(source_key,event_status)
);
CREATE INDEX payment_mail_events_booking_idx
 ON payment_mail_events(account_id,external_id,event_date);
CREATE INDEX payment_mail_events_reference_idx
 ON payment_mail_events(provider,provider_reference,event_date);
