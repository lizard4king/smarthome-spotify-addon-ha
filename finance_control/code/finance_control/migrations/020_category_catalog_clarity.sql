-- Hide historical duplicate category IDs while keeping immutable budget
-- snapshots readable through an explicit alias table.
CREATE TABLE category_aliases (
 alias_id TEXT PRIMARY KEY,
 canonical_id TEXT NOT NULL REFERENCES category_catalog(id)
);

INSERT INTO category_aliases(alias_id,canonical_id) VALUES
 ('EINNAHMEN_ERSTATTUNGEN', 'EINKOMMEN_ERSTATTUNG'),
 ('AUSGABEN_RESTAURANTS', 'FREIZEIT_RESTAURANT'),
 ('FINANZEN_GEBUEHREN', 'AUSGABEN_GEBUEHREN');

UPDATE classification_overrides SET category_id='EINKOMMEN_ERSTATTUNG'
 WHERE category_id='EINNAHMEN_ERSTATTUNGEN';
UPDATE classification_rules SET category_id='EINKOMMEN_ERSTATTUNG'
 WHERE category_id='EINNAHMEN_ERSTATTUNGEN';
UPDATE classification_overrides SET category_id='FREIZEIT_RESTAURANT'
 WHERE category_id='AUSGABEN_RESTAURANTS';
UPDATE classification_rules SET category_id='FREIZEIT_RESTAURANT'
 WHERE category_id='AUSGABEN_RESTAURANTS';
UPDATE classification_overrides SET category_id='AUSGABEN_GEBUEHREN'
 WHERE category_id='FINANZEN_GEBUEHREN';
UPDATE classification_rules SET category_id='AUSGABEN_GEBUEHREN'
 WHERE category_id='FINANZEN_GEBUEHREN';

DELETE FROM category_catalog WHERE id IN (
 'EINNAHMEN_ERSTATTUNGEN', 'AUSGABEN_RESTAURANTS', 'FINANZEN_GEBUEHREN'
);

UPDATE category_catalog SET label='Gebühren' WHERE id='AUSGABEN_GEBUEHREN';
UPDATE category_catalog SET label='Sonstiger Einkauf' WHERE id='AUSGABEN_EINKAUF';
UPDATE category_catalog SET label='Haushaltswaren' WHERE id='ALLTAG_HAUSHALT';
UPDATE category_catalog SET label='Sonstige Abonnements' WHERE id='AUSGABEN_ABONNEMENTS';
UPDATE category_catalog SET label='Sonstige Gesundheit' WHERE id='AUSGABEN_GESUNDHEIT';
UPDATE category_catalog SET label='Sonstige Kommunikation' WHERE id='AUSGABEN_KOMMUNIKATION';
UPDATE category_catalog SET label='Sonstige Mobilität' WHERE id='AUSGABEN_MOBILITAET';
UPDATE category_catalog SET label='Sonstige Versicherungen' WHERE id='AUSGABEN_VERSICHERUNGEN';
UPDATE category_catalog SET label='Sonstige Leistungen' WHERE id='EINNAHMEN_LEISTUNGEN';
UPDATE category_catalog SET label='Haushaltsgründung (einmalig)' WHERE id='AUSGABEN_HAUSHALTSGRUENDUNG';
UPDATE category_catalog SET label='Kapitalerträge' WHERE id='EINNAHMEN_KAPITAL';
UPDATE category_catalog SET label='Kapitalrückzahlung' WHERE id='EINNAHMEN_KAPITALRUECKZAHLUNG';
UPDATE category_catalog SET label='Interne Buchung ungeklärt' WHERE id='INTERN_UNGEKLAERT';
