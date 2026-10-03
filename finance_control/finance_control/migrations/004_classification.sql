CREATE TABLE category_catalog (
 id TEXT PRIMARY KEY,
 label TEXT NOT NULL,
 transaction_type TEXT NOT NULL CHECK(transaction_type IN ('income','expense','transfer'))
);
INSERT INTO category_catalog VALUES
 ('EINNAHMEN_GEHALT', 'Gehalt', 'income'),
 ('EINNAHMEN_LEISTUNGEN', 'Leistungen', 'income'),
 ('EINNAHMEN_ERSTATTUNGEN', 'Erstattungen', 'income'),
 ('EINNAHMEN_UNTERHALT', 'Unterhalt', 'income'),
 ('EINNAHMEN_KAPITAL', 'Kapitalertraege', 'income'),
 ('EINNAHMEN_KAPITALRUECKZAHLUNG', 'Kapitalrueckzahlung', 'income'),
 ('EINNAHMEN_SONDERZAHLUNG', 'Sonderzahlung', 'income'),
 ('EINNAHMEN_SONSTIGE', 'Sonstige Einnahmen', 'income'),
 ('EINNAHMEN_UNKLAR', 'Einnahme unklar', 'income'),
 ('AUSGABEN_HAUSHALTSGRUENDUNG', 'Haushaltsgruendung (einmalig)', 'expense'),
 ('AUSGABEN_MIETE', 'Miete', 'expense'),
 ('AUSGABEN_NEBENKOSTEN', 'Nebenkosten', 'expense'),
 ('AUSGABEN_UNTERHALT', 'Unterhalt', 'expense'),
 ('AUSGABEN_DARLEHEN', 'Darlehen', 'expense'),
 ('AUSGABEN_BARGELD', 'Bargeld', 'expense'),
 ('AUSGABEN_VERSICHERUNGEN', 'Versicherungen', 'expense'),
 ('AUSGABEN_ENERGIE', 'Energie', 'expense'),
 ('AUSGABEN_KOMMUNIKATION', 'Kommunikation', 'expense'),
 ('AUSGABEN_ABONNEMENTS', 'Abonnements', 'expense'),
 ('AUSGABEN_LEBENSMITTEL', 'Lebensmittel', 'expense'),
 ('AUSGABEN_MOBILITAET', 'Mobilitaet', 'expense'),
 ('AUSGABEN_GESUNDHEIT', 'Gesundheit', 'expense'),
 ('AUSGABEN_RESTAURANTS', 'Restaurants', 'expense'),
 ('AUSGABEN_EINKAUF', 'Einkauf', 'expense'),
 ('AUSGABEN_URLAUB', 'Urlaub', 'expense'),
 ('AUSGABEN_GEBUEHREN', 'Gebuehren', 'expense'),
 ('AUSGABEN_SONSTIGE', 'Sonstige Ausgaben', 'expense'),
 ('AUSGABEN_UNKLAR', 'Ausgabe unklar', 'expense'),
 ('TRANSFER', 'Umbuchung', 'transfer'),
 ('INTERN_UNGEKLAERT', 'Interne Buchung ungeklaert', 'transfer');
CREATE TABLE transaction_context (
 account_id TEXT NOT NULL, external_id TEXT NOT NULL,
 counterparty TEXT NOT NULL, description TEXT NOT NULL,
 PRIMARY KEY(account_id, external_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transactions(account_id, external_id)
);
CREATE TABLE classification_overrides (
 account_id TEXT NOT NULL, external_id TEXT NOT NULL,
 category_id TEXT NOT NULL REFERENCES category_catalog(id),
 confirmed INTEGER NOT NULL CHECK(confirmed IN (0, 1)), revision INTEGER NOT NULL CHECK(revision >= 1),
 PRIMARY KEY(account_id, external_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transactions(account_id, external_id)
);
CREATE TABLE classification_audit (
 id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, action TEXT NOT NULL,
 account_id TEXT, external_id TEXT, document_id INTEGER, previous TEXT, current TEXT NOT NULL
);
CREATE TABLE classification_rules (
 id INTEGER PRIMARY KEY, counterparty_normalized TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('income','expense')),
 category_id TEXT NOT NULL REFERENCES category_catalog(id), revision INTEGER NOT NULL CHECK(revision >= 1),
 UNIQUE(counterparty_normalized, direction)
);
CREATE TABLE classification_documents (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('invoice','contract')),
 vendor TEXT NOT NULL, title TEXT NOT NULL, document_date TEXT, amount TEXT,
 currency TEXT, source_reference TEXT NOT NULL UNIQUE, warnings TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL CHECK(status IN ('unreviewed','confirmed')),
 revision INTEGER NOT NULL CHECK(revision >= 1)
);
CREATE TABLE classification_document_links (
 account_id TEXT NOT NULL, external_id TEXT NOT NULL,
 document_id INTEGER NOT NULL REFERENCES classification_documents(id),
 PRIMARY KEY(account_id, external_id, document_id),
 FOREIGN KEY(account_id, external_id) REFERENCES transactions(account_id, external_id)
);
