-- Explicit owner-confirmed publication of the minimal remaining payment schedule.
CREATE TABLE paypal_credit_forecast_links (
    loan_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    owner_id TEXT NOT NULL REFERENCES app_users(id),
    source_revision INTEGER NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    reference TEXT NOT NULL CHECK (length(reference) BETWEEN 4 AND 240),
    active INTEGER NOT NULL CHECK (active IN (0,1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (loan_id,revision),
    FOREIGN KEY (loan_id,source_revision) REFERENCES paypal_credit_snapshots(loan_id,revision)
);
CREATE TRIGGER paypal_credit_forecast_owner_consistent
BEFORE INSERT ON paypal_credit_forecast_links
WHEN NOT EXISTS (
    SELECT 1 FROM paypal_credit_snapshots
    WHERE loan_id=NEW.loan_id AND revision=NEW.source_revision AND owner_id=NEW.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'paypal_credit_forecast_owner_mismatch');
END;
CREATE TRIGGER paypal_credit_forecast_no_update
BEFORE UPDATE ON paypal_credit_forecast_links
BEGIN
    SELECT RAISE(ABORT, 'paypal_credit_forecast_immutable');
END;
CREATE TRIGGER paypal_credit_forecast_no_delete
BEFORE DELETE ON paypal_credit_forecast_links
BEGIN
    SELECT RAISE(ABORT, 'paypal_credit_forecast_immutable');
END;
