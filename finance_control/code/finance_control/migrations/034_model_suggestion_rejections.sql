-- A rejection belongs to this booking and category, independent of model/version
-- or rationale. Retain it when the user later classifies or resets the booking.
CREATE TABLE classification_model_rejections (
 account_id TEXT NOT NULL,
 external_id TEXT NOT NULL,
 category_id TEXT NOT NULL REFERENCES category_catalog(id),
 PRIMARY KEY(account_id, external_id, category_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transactions(account_id, external_id)
);

-- Preserve decisions made before persistent rejection storage existed.
INSERT OR IGNORE INTO classification_model_rejections
SELECT a.account_id,a.external_id,c.id
FROM classification_audit a
JOIN transactions t ON t.account_id=a.account_id AND t.external_id=a.external_id
LEFT JOIN category_aliases alias ON alias.alias_id=json_extract(a.previous,'$.category_id')
JOIN category_catalog c ON c.id=COALESCE(alias.canonical_id,json_extract(a.previous,'$.category_id'))
WHERE a.action='model_suggestion_rejected' AND json_valid(a.previous);

DELETE FROM classification_model_reviews
WHERE status='proposed' AND EXISTS (
 SELECT 1 FROM classification_model_rejections r
 WHERE r.account_id=classification_model_reviews.account_id
 AND r.external_id=classification_model_reviews.external_id
 AND r.category_id=classification_model_reviews.category_id
);

-- The shared revision prevents old confirmation tabs from undoing the rejection.
-- Unconfirmed overrides are revision anchors, not category decisions.
CREATE TABLE classification_overrides_rebuilt (
 account_id TEXT NOT NULL, external_id TEXT NOT NULL,
 category_id TEXT REFERENCES category_catalog(id),
 confirmed INTEGER NOT NULL CHECK(confirmed IN (0, 1)),
 revision INTEGER NOT NULL CHECK(revision >= 1),
 CHECK((confirmed=0 AND category_id IS NULL) OR (confirmed=1 AND category_id IS NOT NULL)),
 PRIMARY KEY(account_id, external_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transactions(account_id, external_id)
);
INSERT INTO classification_overrides_rebuilt
SELECT account_id,external_id,CASE WHEN confirmed=1 THEN category_id END,confirmed,revision
FROM classification_overrides;
DROP TABLE classification_overrides;
ALTER TABLE classification_overrides_rebuilt RENAME TO classification_overrides;

INSERT INTO classification_overrides
SELECT r.account_id,r.external_id,NULL,0,1
FROM classification_model_rejections r
GROUP BY r.account_id,r.external_id
ON CONFLICT(account_id,external_id) DO UPDATE SET revision=revision+1;
