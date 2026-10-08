-- Keep existing 12-column period registries compatible with explicit aliases.
ALTER TABLE ing_period_imports ADD COLUMN ledger_ids_json TEXT NOT NULL DEFAULT '[]';
