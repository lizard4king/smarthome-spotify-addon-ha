-- Manually recorded PayPal credit plans are owner-bound immutable snapshots.
CREATE TABLE paypal_credit_snapshots (
    loan_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    owner_id TEXT NOT NULL REFERENCES app_users(id),
    source TEXT NOT NULL CHECK (source = 'manual_paypal_portal'),
    created_at TEXT NOT NULL,
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    PRIMARY KEY (loan_id, revision)
);

CREATE INDEX paypal_credit_snapshots_owner ON paypal_credit_snapshots(owner_id, loan_id, revision);

CREATE TRIGGER paypal_credit_snapshot_owner_immutable
BEFORE INSERT ON paypal_credit_snapshots
WHEN EXISTS (
    SELECT 1 FROM paypal_credit_snapshots
    WHERE loan_id=NEW.loan_id AND owner_id!=NEW.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'paypal_credit_owner_immutable');
END;

CREATE TRIGGER paypal_credit_snapshot_no_update
BEFORE UPDATE ON paypal_credit_snapshots
BEGIN
    SELECT RAISE(ABORT, 'paypal_credit_snapshot_immutable');
END;

CREATE TRIGGER paypal_credit_snapshot_no_delete
BEFORE DELETE ON paypal_credit_snapshots
BEGIN
    SELECT RAISE(ABORT, 'paypal_credit_snapshot_immutable');
END;
