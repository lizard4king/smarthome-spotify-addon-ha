-- Additional household categories confirmed by identifiable specialist merchants.
INSERT INTO category_catalog(id,label,transaction_type,parent_id) VALUES
 ('FAMILIE_HAUSTIERE', 'Haustiere', 'expense', 'FAMILIE_VERPFLICHTUNGEN'),
 ('WOHNEN_GARTEN', 'Garten', 'expense', 'WOHNEN');
