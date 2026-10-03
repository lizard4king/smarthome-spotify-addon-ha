-- Repair installations that already applied the first category hierarchy
-- before equivalent legacy and new leaf IDs were deduplicated.
UPDATE classification_overrides SET category_id='EINNAHMEN_GEHALT'
 WHERE category_id='EINKOMMEN_GEHALT';
UPDATE classification_rules SET category_id='EINNAHMEN_GEHALT'
 WHERE category_id='EINKOMMEN_GEHALT';
UPDATE classification_overrides SET category_id='EINNAHMEN_SONDERZAHLUNG'
 WHERE category_id='EINKOMMEN_SONDERZAHLUNG';
UPDATE classification_rules SET category_id='EINNAHMEN_SONDERZAHLUNG'
 WHERE category_id='EINKOMMEN_SONDERZAHLUNG';
UPDATE classification_overrides SET category_id='EINNAHMEN_UNTERHALT'
 WHERE category_id='EINKOMMEN_UNTERHALT';
UPDATE classification_rules SET category_id='EINNAHMEN_UNTERHALT'
 WHERE category_id='EINKOMMEN_UNTERHALT';
UPDATE classification_overrides SET category_id='AUSGABEN_UNTERHALT'
 WHERE category_id='VERPFLICHTUNGEN_UNTERHALT';
UPDATE classification_rules SET category_id='AUSGABEN_UNTERHALT'
 WHERE category_id='VERPFLICHTUNGEN_UNTERHALT';
UPDATE classification_overrides SET category_id='AUSGABEN_LEBENSMITTEL'
 WHERE category_id='ALLTAG_LEBENSMITTEL';
UPDATE classification_rules SET category_id='AUSGABEN_LEBENSMITTEL'
 WHERE category_id='ALLTAG_LEBENSMITTEL';
UPDATE classification_overrides SET category_id='AUSGABEN_ENERGIE'
 WHERE category_id='WOHNEN_ENERGIE';
UPDATE classification_rules SET category_id='AUSGABEN_ENERGIE'
 WHERE category_id='WOHNEN_ENERGIE';
UPDATE classification_overrides SET category_id='AUSGABEN_MIETE'
 WHERE category_id='WOHNEN_MIETE';
UPDATE classification_rules SET category_id='AUSGABEN_MIETE'
 WHERE category_id='WOHNEN_MIETE';
UPDATE classification_overrides SET category_id='AUSGABEN_NEBENKOSTEN'
 WHERE category_id='WOHNEN_NEBENKOSTEN';
UPDATE classification_rules SET category_id='AUSGABEN_NEBENKOSTEN'
 WHERE category_id='WOHNEN_NEBENKOSTEN';

DELETE FROM category_catalog WHERE id IN (
 'EINKOMMEN_GEHALT', 'EINKOMMEN_SONDERZAHLUNG', 'EINKOMMEN_UNTERHALT',
 'VERPFLICHTUNGEN_UNTERHALT', 'ALLTAG_LEBENSMITTEL', 'WOHNEN_ENERGIE',
 'WOHNEN_MIETE', 'WOHNEN_NEBENKOSTEN'
);

CREATE UNIQUE INDEX IF NOT EXISTS category_catalog_parent_label_uq
 ON category_catalog(parent_id, label);
