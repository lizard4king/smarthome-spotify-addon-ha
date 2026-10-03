-- Locally owned household categories learned from explicit user decisions.
INSERT INTO category_catalog(id,label,transaction_type,parent_id) VALUES
 ('FREIZEIT_SPORT_FITNESS', 'Sport und Fitness', 'expense', 'FREIZEIT_MEDIEN_REISEN'),
 ('MOBILITAET_FAHRZEUGPFLEGE', 'Fahrzeugpflege', 'expense', 'MOBILITAET'),
 ('MOBILITAET_RASTSTAETTE', 'Raststätte', 'expense', 'MOBILITAET'),
 ('ALLTAG_BACKWAREN', 'Backwaren', 'expense', 'LEBENSMITTEL_HAUSHALT'),
 ('KOMMUNIKATION_POST_VERSAND', 'Post und Versand', 'expense', 'KOMMUNIKATION_DIGITALES'),
 ('WOHNEN_KAUTIONSBUERGSCHAFT', 'Kautionsbürgschaft', 'expense', 'WOHNEN'),
 ('FAMILIE_TASCHENGELD', 'Taschengeld', 'expense', 'FAMILIE_VERPFLICHTUNGEN'),
 ('EINKOMMEN_GESUNDHEITSERSTATTUNG', 'Gesundheitserstattung', 'income', 'EINKOMMEN_LEISTUNGEN_ERSTATTUNGEN');
