-- Further categories for identifiable household spending without Finanzguru semantics.
INSERT INTO category_catalog(id,label,transaction_type,parent_id) VALUES
 ('GESUNDHEIT_HILFSMITTEL', 'Brille und Hilfsmittel', 'expense', 'GESUNDHEIT'),
 ('MOBILITAET_KFZ_STEUER', 'Kfz-Steuer', 'expense', 'MOBILITAET'),
 ('FAMILIE_GESCHENKE', 'Geschenke', 'expense', 'FAMILIE_VERPFLICHTUNGEN'),
 ('FREIZEIT_BILDUNG', 'Bildung und Lernen', 'expense', 'FREIZEIT_MEDIEN_REISEN');
