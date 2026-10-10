"""Guard the public CSV file upload without changing internal bank import paths."""

import csv
import io


_COLUMNS = ['external_id', 'account_id', 'date', 'amount', 'currency', 'category', 'transfer_id']
_BOUND_ERROR = 'CSV file import rejected: an account uses a direct bank source'


class DirectBankSourceError(ValueError):
    """A file upload cannot write an account whose source is the bank."""


def import_file_csv(store, source):
    """Import one complete standard CSV file only when none of its targets is bank-bound.

    The check precedes the core's digest replay shortcut. An immediate SQLite
    transaction keeps the binding check and all postings in one write transaction.
    Internal bank importers continue to call ``Store.import_csv`` directly.
    """
    if not isinstance(source, str):
        raise ValueError('Invalid CSV file')
    reader = csv.DictReader(io.StringIO(source))
    if reader.fieldnames != _COLUMNS:
        raise ValueError('Unexpected CSV columns')
    rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError('Malformed CSV row')
    targets = {row['account_id'] for row in rows}

    db = store.db
    if db.in_transaction:
        raise ValueError('CSV file import requires an idle transaction')
    db.execute('BEGIN IMMEDIATE')
    try:
        if targets and any(account_id in targets for (account_id,) in db.execute(
                'SELECT account_id FROM bank_source_accounts')):
            raise DirectBankSourceError(_BOUND_ERROR)
        inserted = store.import_csv(source, manage_transaction=False)
        db.commit()
        return inserted
    except BaseException:
        db.rollback()
        raise
