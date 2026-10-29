-- Initiator of a refresh; connection ownership and credentials remain unchanged.
ALTER TABLE server_bank_refresh_bindings ADD COLUMN last_triggered_by TEXT REFERENCES app_users(id);
