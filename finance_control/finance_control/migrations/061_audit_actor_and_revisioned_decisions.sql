-- Keep legacy authors explicit while assigning local writes a useful default.
ALTER TABLE classification_audit ADD COLUMN actor TEXT NOT NULL DEFAULT 'system:local';
ALTER TABLE account_display_name_audit ADD COLUMN actor TEXT NOT NULL DEFAULT 'system:local';
ALTER TABLE account_lifecycle_audit ADD COLUMN actor TEXT NOT NULL DEFAULT 'system:local';
ALTER TABLE transfer_correction_audit ADD COLUMN actor TEXT NOT NULL DEFAULT 'system:local';
ALTER TABLE transfer_single_leg_audit ADD COLUMN actor TEXT NOT NULL DEFAULT 'system:local';

UPDATE classification_audit SET actor='unknown-legacy';
UPDATE account_display_name_audit SET actor='unknown-legacy';
UPDATE account_lifecycle_audit SET actor='unknown-legacy';
UPDATE transfer_correction_audit SET actor='unknown-legacy';
UPDATE transfer_single_leg_audit SET actor='unknown-legacy';

CREATE TRIGGER classification_audit_no_update
BEFORE UPDATE ON classification_audit
BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;
CREATE TRIGGER classification_audit_no_delete
BEFORE DELETE ON classification_audit
BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;

CREATE TRIGGER account_display_name_audit_no_update
BEFORE UPDATE ON account_display_name_audit
BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;
CREATE TRIGGER account_display_name_audit_no_delete
BEFORE DELETE ON account_display_name_audit
BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;

CREATE TRIGGER account_lifecycle_audit_no_update
BEFORE UPDATE ON account_lifecycle_audit
BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;
CREATE TRIGGER account_lifecycle_audit_no_delete
BEFORE DELETE ON account_lifecycle_audit
BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;

CREATE TRIGGER transfer_correction_audit_no_update
BEFORE UPDATE ON transfer_correction_audit
BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;
CREATE TRIGGER transfer_correction_audit_no_delete
BEFORE DELETE ON transfer_correction_audit
BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;

CREATE TRIGGER transfer_single_leg_audit_no_update
BEFORE UPDATE ON transfer_single_leg_audit
BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;
CREATE TRIGGER transfer_single_leg_audit_no_delete
BEFORE DELETE ON transfer_single_leg_audit
BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;

-- Rejections are durable positive decisions. There is intentionally no undo API.
CREATE TABLE rejected_document_links (
    account_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    document_id INTEGER NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    actor TEXT NOT NULL DEFAULT 'system:local',
    changed_at TEXT,
    PRIMARY KEY(account_id, external_id, document_id),
    FOREIGN KEY(account_id, external_id)
        REFERENCES transactions(account_id, external_id),
    FOREIGN KEY(document_id) REFERENCES classification_documents(id)
);

INSERT INTO rejected_document_links(
    account_id,external_id,document_id,revision,actor,changed_at)
SELECT a.account_id,a.external_id,a.document_id,1,'unknown-legacy',MIN(a.occurred_at)
FROM classification_audit a
JOIN transactions t ON t.account_id=a.account_id AND t.external_id=a.external_id
JOIN classification_documents d ON d.id=a.document_id
WHERE a.action='document_link_rejected'
GROUP BY a.account_id,a.external_id,a.document_id;

-- The warning marker is the source of current duplicate state. The event log
-- supplies a target only when its latest event proves a valid canonical id.
CREATE TABLE document_duplicate_state (
    document_id INTEGER PRIMARY KEY REFERENCES classification_documents(id),
    duplicate_of INTEGER REFERENCES classification_documents(id),
    is_duplicate INTEGER NOT NULL CHECK(is_duplicate IN (0,1)),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    actor TEXT NOT NULL DEFAULT 'system:local',
    changed_at TEXT,
    CHECK(duplicate_of IS NULL OR duplicate_of != document_id),
    CHECK(is_duplicate=1 OR duplicate_of IS NULL)
);

WITH latest_duplicate_event AS (
    SELECT document_id,MAX(id) AS event_id
    FROM classification_audit
    WHERE action IN ('document_marked_duplicate','document_duplicate_restored')
      AND document_id IS NOT NULL
    GROUP BY document_id
), duplicate_rows AS (
    SELECT d.id AS document_id,
           CASE WHEN json_valid(d.warnings) THEN EXISTS (
               SELECT 1 FROM json_each(d.warnings)
               WHERE type='text' AND value='duplicate_source_document'
           ) ELSE 0 END AS is_duplicate,
           a.action AS latest_action,
           a.current AS latest_current,
           a.occurred_at AS latest_occurred_at
    FROM classification_documents d
    LEFT JOIN latest_duplicate_event e ON e.document_id=d.id
    LEFT JOIN classification_audit a ON a.id=e.event_id
    WHERE (CASE WHEN json_valid(d.warnings) THEN EXISTS (
               SELECT 1 FROM json_each(d.warnings)
               WHERE type='text' AND value='duplicate_source_document'
           ) ELSE 0 END)=1
       OR e.event_id IS NOT NULL
)
INSERT INTO document_duplicate_state(
    document_id,duplicate_of,is_duplicate,revision,actor,changed_at)
SELECT r.document_id,
       CASE WHEN r.is_duplicate=1
                  AND r.latest_action='document_marked_duplicate'
                  AND json_valid(r.latest_current)
                  AND json_type(r.latest_current,'$.duplicate_of')='integer'
                  AND json_extract(r.latest_current,'$.duplicate_of') != r.document_id
                  AND EXISTS (
                      SELECT 1 FROM classification_documents target
                      WHERE target.id=json_extract(r.latest_current,'$.duplicate_of'))
            THEN json_extract(r.latest_current,'$.duplicate_of')
            ELSE NULL END,
       r.is_duplicate,1,'unknown-legacy',r.latest_occurred_at
FROM duplicate_rows r;
