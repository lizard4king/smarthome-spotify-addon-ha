-- Early local builds of migration 031 used cross-table triggers. Keep the
-- compatibility cleanup explicit; application writes enforce the same guard
-- without breaking controlled legacy link-table rebuilds.
DROP TRIGGER IF EXISTS bonsy_cash_requires_unlinked_receipt;
DROP TRIGGER IF EXISTS bonsy_direct_link_requires_no_cash;
