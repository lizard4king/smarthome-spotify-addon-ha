-- Household categories derived from explicit booking descriptions.
INSERT INTO category_catalog(id,label,transaction_type,parent_id) VALUES
 ('VERSICHERUNG_HAUSRAT', 'Hausratversicherung', 'expense', 'VERSICHERUNG_VORSORGE'),
 ('VERSICHERUNG_HAFTPFLICHT', 'Haftpflichtversicherung', 'expense', 'VERSICHERUNG_VORSORGE'),
 ('VERSICHERUNG_RECHTSSCHUTZ', 'Rechtsschutzversicherung', 'expense', 'VERSICHERUNG_VORSORGE'),
 ('VERSICHERUNG_KREDITABSICHERUNG', 'Kreditabsicherung', 'expense', 'VERSICHERUNG_VORSORGE'),
 ('VERSICHERUNG_LEBEN', 'Lebensversicherung', 'expense', 'VERSICHERUNG_VORSORGE'),
 ('FREIZEIT_KINO', 'Kino', 'expense', 'FREIZEIT_MEDIEN_REISEN'),
 ('WOHNEN_BAUMARKT', 'Baumarkt', 'expense', 'WOHNEN'),
 ('MOBILITAET_FAHRDIENST', 'Taxi und Fahrdienst', 'expense', 'MOBILITAET'),
 ('FINANZEN_SOLLZINSEN', 'Sollzinsen', 'expense', 'FINANZEN');
