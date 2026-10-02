-- Generic household semantics. Personal names and booking data stay outside Git.
INSERT INTO category_catalog(id,label,transaction_type,parent_id) VALUES
 ('FAMILIE_UNTERSTUETZUNG_ANGEHOERIGE', 'Unterstützung Angehörige', 'expense', 'FAMILIE_VERPFLICHTUNGEN'),
 ('MOBILITAET_REPARATUR', 'Reparatur und Instandhaltung', 'expense', 'MOBILITAET'),
 ('EINKOMMEN_STEUERERSTATTUNG', 'Steuererstattung', 'income', 'EINKOMMEN_LEISTUNGEN_ERSTATTUNGEN'),
 ('EINKOMMEN_KOSTENAUSGLEICH', 'Kostenausgleich', 'income', 'EINKOMMEN_LEISTUNGEN_ERSTATTUNGEN');
