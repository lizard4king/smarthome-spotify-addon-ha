CREATE TABLE payment_review_decisions (
 proposal_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('match','recurring','amount')),
 token TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('accept','reject')),
 payload TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
