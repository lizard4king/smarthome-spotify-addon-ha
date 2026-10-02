-- The category catalogue is owned locally.  Importer categories remain on
-- transactions as source metadata and are never used to choose a category.
CREATE TEMP TABLE legacy_category_ids AS SELECT id FROM category_catalog;
ALTER TABLE category_catalog ADD COLUMN parent_id TEXT REFERENCES category_catalog(id);

INSERT INTO category_catalog(id,label,transaction_type,parent_id) VALUES
 ('WOHNEN', 'Wohnen', 'expense', NULL),
 ('LEBENSMITTEL_HAUSHALT', 'Lebensmittel und Haushalt', 'expense', NULL),
 ('MOBILITAET', 'Mobilität', 'expense', NULL),
 ('GESUNDHEIT', 'Gesundheit', 'expense', NULL),
 ('VERSICHERUNG_VORSORGE', 'Versicherung und Vorsorge', 'expense', NULL),
 ('FAMILIE_VERPFLICHTUNGEN', 'Familie und Verpflichtungen', 'expense', NULL),
 ('FREIZEIT_MEDIEN_REISEN', 'Freizeit, Medien und Reisen', 'expense', NULL),
 ('KOMMUNIKATION_DIGITALES', 'Kommunikation und Digitales', 'expense', NULL),
 ('FINANZEN', 'Finanzen', 'expense', NULL),
 ('SONSTIGE_UNKLAR', 'Sonstiges und unklar', 'expense', NULL),
 ('EINKOMMEN_ARBEIT', 'Einkommen aus Arbeit', 'income', NULL),
 ('EINKOMMEN_LEISTUNGEN_ERSTATTUNGEN', 'Leistungen und Erstattungen', 'income', NULL),
 ('VERMOEGEN', 'Vermögen', 'income', NULL),
 ('EINKOMMEN_SONSTIGES', 'Sonstige Einnahmen und unklar', 'income', NULL),
 ('UMBUCHUNGEN', 'Umbuchungen', 'transfer', NULL),
 ('WOHNEN_EINRICHTUNG', 'Einrichtung', 'expense', 'WOHNEN'),
 ('ALLTAG_DROGERIE', 'Drogerie', 'expense', 'LEBENSMITTEL_HAUSHALT'),
 ('ALLTAG_HAUSHALT', 'Haushalt', 'expense', 'LEBENSMITTEL_HAUSHALT'),
 ('MOBILITAET_KRAFTSTOFF', 'Kraftstoff', 'expense', 'MOBILITAET'),
 ('MOBILITAET_FAHRZEUGRATE', 'Fahrzeugrate', 'expense', 'MOBILITAET'),
 ('MOBILITAET_FAHRZEUGVERSICHERUNG', 'Fahrzeugversicherung', 'expense', 'MOBILITAET'),
 ('MOBILITAET_WARTUNG', 'Wartung', 'expense', 'MOBILITAET'),
 ('MOBILITAET_OEPNV_PARKEN', 'ÖPNV und Parken', 'expense', 'MOBILITAET'),
 ('GESUNDHEIT_BEHANDLUNG', 'Behandlung', 'expense', 'GESUNDHEIT'),
 ('GESUNDHEIT_MEDIKAMENTE', 'Medikamente', 'expense', 'GESUNDHEIT'),
 ('GESUNDHEIT_KRANKENVERSICHERUNG', 'Krankenversicherung', 'expense', 'GESUNDHEIT'),
 ('FREIZEIT_RESTAURANT', 'Restaurant', 'expense', 'FREIZEIT_MEDIEN_REISEN'),
 ('FREIZEIT_STREAMING_DIGITAL', 'Streaming und Digitales', 'expense', 'FREIZEIT_MEDIEN_REISEN'),
 ('FREIZEIT_AUSFLUEGE', 'Ausflüge', 'expense', 'FREIZEIT_MEDIEN_REISEN'),
 ('FREIZEIT_REISEN', 'Reisen', 'expense', 'FREIZEIT_MEDIEN_REISEN'),
 ('FINANZEN_KREDIT', 'Kredit', 'expense', 'FINANZEN'),
 ('EINKOMMEN_LOHNERSATZ', 'Lohnersatz', 'income', 'EINKOMMEN_LEISTUNGEN_ERSTATTUNGEN'),
 ('EINKOMMEN_ERSTATTUNG', 'Erstattung', 'income', 'EINKOMMEN_LEISTUNGEN_ERSTATTUNGEN');

UPDATE category_catalog SET parent_id='WOHNEN'
 WHERE id IN ('AUSGABEN_HAUSHALTSGRUENDUNG', 'AUSGABEN_MIETE', 'AUSGABEN_NEBENKOSTEN', 'AUSGABEN_ENERGIE');
UPDATE category_catalog SET parent_id='LEBENSMITTEL_HAUSHALT'
 WHERE id IN ('AUSGABEN_LEBENSMITTEL', 'AUSGABEN_EINKAUF');
UPDATE category_catalog SET parent_id='MOBILITAET' WHERE id='AUSGABEN_MOBILITAET';
UPDATE category_catalog SET parent_id='GESUNDHEIT' WHERE id='AUSGABEN_GESUNDHEIT';
UPDATE category_catalog SET parent_id='VERSICHERUNG_VORSORGE' WHERE id='AUSGABEN_VERSICHERUNGEN';
UPDATE category_catalog SET parent_id='FAMILIE_VERPFLICHTUNGEN' WHERE id='AUSGABEN_UNTERHALT';
UPDATE category_catalog SET parent_id='FREIZEIT_MEDIEN_REISEN'
 WHERE id IN ('AUSGABEN_ABONNEMENTS', 'AUSGABEN_RESTAURANTS', 'AUSGABEN_URLAUB');
UPDATE category_catalog SET parent_id='KOMMUNIKATION_DIGITALES' WHERE id='AUSGABEN_KOMMUNIKATION';
UPDATE category_catalog SET parent_id='FINANZEN'
 WHERE id IN ('AUSGABEN_DARLEHEN', 'AUSGABEN_BARGELD', 'AUSGABEN_GEBUEHREN');
UPDATE category_catalog SET parent_id='SONSTIGE_UNKLAR'
 WHERE id IN ('AUSGABEN_SONSTIGE', 'AUSGABEN_UNKLAR');
UPDATE category_catalog SET parent_id='EINKOMMEN_ARBEIT'
 WHERE id IN ('EINNAHMEN_GEHALT', 'EINNAHMEN_SONDERZAHLUNG');
UPDATE category_catalog SET parent_id='EINKOMMEN_LEISTUNGEN_ERSTATTUNGEN'
 WHERE id IN ('EINNAHMEN_LEISTUNGEN', 'EINNAHMEN_ERSTATTUNGEN', 'EINNAHMEN_UNTERHALT');
UPDATE category_catalog SET parent_id='VERMOEGEN'
 WHERE id IN ('EINNAHMEN_KAPITAL', 'EINNAHMEN_KAPITALRUECKZAHLUNG');
UPDATE category_catalog SET parent_id='EINKOMMEN_SONSTIGES'
 WHERE id IN ('EINNAHMEN_SONSTIGE', 'EINNAHMEN_UNKLAR');
UPDATE category_catalog SET parent_id='UMBUCHUNGEN'
 WHERE id IN ('TRANSFER', 'INTERN_UNGEKLAERT');

-- Preserve extensions from older installations instead of silently hiding
-- them from the two-level catalogue.  Known IDs above keep their explicit
-- assignment; unknown legacy leaves get the matching local fallback parent.
UPDATE category_catalog SET parent_id=CASE transaction_type
 WHEN 'expense' THEN 'SONSTIGE_UNKLAR'
 WHEN 'income' THEN 'EINKOMMEN_SONSTIGES'
 ELSE 'UMBUCHUNGEN' END
 WHERE parent_id IS NULL AND id IN (SELECT id FROM legacy_category_ids);
DROP TABLE legacy_category_ids;

-- A local category label must be unique within its parent.  This protects
-- future catalogue extensions while preserving every existing category ID.
CREATE UNIQUE INDEX category_catalog_parent_label_uq
 ON category_catalog(parent_id, label);
