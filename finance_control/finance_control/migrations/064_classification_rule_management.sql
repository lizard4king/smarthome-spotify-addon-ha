-- Existing rules remain active and unbounded until explicitly reviewed.
ALTER TABLE classification_rules ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1
 CHECK(typeof(enabled)='integer' AND enabled IN (0,1));
ALTER TABLE classification_rules ADD COLUMN valid_from TEXT
 CHECK(valid_from IS NULL OR (length(valid_from)=10 AND date(valid_from,'+0 days') IS NOT NULL AND date(valid_from,'+0 days')=valid_from));
ALTER TABLE classification_rules ADD COLUMN valid_to TEXT
 CHECK(valid_to IS NULL OR (length(valid_to)=10 AND date(valid_to,'+0 days') IS NOT NULL AND date(valid_to,'+0 days')=valid_to));
CREATE TRIGGER classification_rules_valid_range_insert BEFORE INSERT ON classification_rules
WHEN NEW.valid_from IS NOT NULL AND NEW.valid_to IS NOT NULL AND NEW.valid_from>NEW.valid_to
BEGIN SELECT RAISE(ABORT,'invalid rule date range'); END;
CREATE TRIGGER classification_rules_valid_range_update BEFORE UPDATE ON classification_rules
WHEN NEW.valid_from IS NOT NULL AND NEW.valid_to IS NOT NULL AND NEW.valid_from>NEW.valid_to
BEGIN SELECT RAISE(ABORT,'invalid rule date range'); END;
CREATE TRIGGER classification_rules_no_delete BEFORE DELETE ON classification_rules
BEGIN SELECT RAISE(ABORT,'classification rules cannot be deleted'); END;
CREATE TRIGGER classification_rules_no_replace BEFORE INSERT ON classification_rules
WHEN EXISTS (SELECT 1 FROM classification_rules r WHERE r.id=NEW.id OR
 (r.counterparty_normalized=NEW.counterparty_normalized AND r.direction=NEW.direction))
BEGIN SELECT RAISE(ABORT,'classification rules cannot be replaced'); END;

-- Identity-scoped latest decisions must not rescan all booking histories.
CREATE INDEX classification_audit_identity_latest_idx
 ON classification_audit(account_id,external_id,id DESC);
