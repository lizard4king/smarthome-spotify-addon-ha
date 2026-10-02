-- Categories for common household purchases identified from merchant evidence.
INSERT INTO category_catalog(id,label,transaction_type,parent_id) VALUES
 ('ALLTAG_KLEIDUNG', 'Kleidung', 'expense', 'LEBENSMITTEL_HAUSHALT'),
 ('MOBILITAET_LADEN', 'Elektroauto laden', 'expense', 'MOBILITAET'),
 ('FAMILIE_SPIELZEUG', 'Spielzeug', 'expense', 'FAMILIE_VERPFLICHTUNGEN');
