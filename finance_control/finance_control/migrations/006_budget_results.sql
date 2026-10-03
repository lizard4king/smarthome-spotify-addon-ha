ALTER TABLE budget_snapshots ADD COLUMN calculation TEXT CHECK(calculation IS NULL OR json_valid(calculation));
ALTER TABLE budget_snapshots ADD COLUMN base_revision INTEGER REFERENCES budget_snapshots(revision);
