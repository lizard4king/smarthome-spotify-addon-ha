-- Only fixed, validated diagnostic values are written by the refresh service.
ALTER TABLE server_bank_refresh_bindings
    ADD COLUMN last_diagnostic TEXT CHECK (
        last_diagnostic IS NULL OR length(last_diagnostic) <= 128);
