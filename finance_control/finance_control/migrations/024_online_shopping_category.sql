-- Marketplace purchases can contain several unrelated receipt lines. Keep the
-- basket as one household expense while preserving its receipt items separately.
INSERT INTO category_catalog(id,label,transaction_type,parent_id) VALUES
 ('ALLTAG_ONLINE_EINKAUF', 'Online-Einkauf', 'expense', 'LEBENSMITTEL_HAUSHALT');
