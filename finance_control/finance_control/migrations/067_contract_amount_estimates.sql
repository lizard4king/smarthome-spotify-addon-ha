CREATE TABLE contract_amount_estimates (
 id INTEGER PRIMARY KEY,
 rule_id INTEGER NOT NULL REFERENCES recurring_rules(id),
 rule_revision INTEGER NOT NULL,
 payload TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(rule_id,rule_revision)
);

CREATE TRIGGER contract_amount_estimates_no_update
BEFORE UPDATE ON contract_amount_estimates
BEGIN
 SELECT RAISE(ABORT, 'contract_amount_estimate_immutable');
END;

CREATE TRIGGER contract_amount_estimates_no_delete
BEFORE DELETE ON contract_amount_estimates
BEGIN
 SELECT RAISE(ABORT, 'contract_amount_estimate_immutable');
END;
