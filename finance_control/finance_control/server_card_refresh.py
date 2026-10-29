"""C.12 refresh inside the existing shared worker, using approved local mappings."""
from contextlib import closing
from datetime import date, timedelta
from hashlib import sha256

from .administration import AdministrationError
from .core import Store
from .server_cards import _bounded_result


class ServerCardRefresh:
    def __init__(self, administration, gateway, database):
        self.administration, self.gateway, self.database = administration, gateway, database

    def rows(self, initiator):
        with self.administration._connection() as db:
            self.administration._actor(db, initiator)
            rows = db.execute(
                "SELECT c.id,c.label,c.bank_id,c.user_id AS connection_owner,c.revision AS connection_revision,"
                "k.owner_id AS binding_owner,k.connection_revision AS binding_revision,k.card_id,"
                "k.account_id,a.display_name AS account_name,k.masked_number,"
                "COALESCE(k.tan_method,(SELECT b.tan_method FROM server_bank_refresh_bindings b "
                "WHERE b.connection_id=c.id AND b.revision=c.revision AND b.owner_id=k.owner_id AND b.tan_method IS NOT NULL ORDER BY b.account_id LIMIT 1)) AS tan_method,"
                "CASE WHEN k.tan_method IS NOT NULL THEN k.tan_medium ELSE (SELECT b.tan_medium FROM server_bank_refresh_bindings b "
                "WHERE b.connection_id=c.id AND b.revision=c.revision AND b.owner_id=k.owner_id AND b.tan_method IS NOT NULL ORDER BY b.account_id LIMIT 1) END AS tan_medium,"
                "k.last_attempt_at,k.last_success_at,k.last_status,k.last_code,k.last_inserted,"
                "NULL AS last_diagnostic "
                "FROM server_cards k JOIN app_bank_connections c ON c.id=k.connection_id "
                "JOIN app_users u ON u.id=k.owner_id JOIN accounts a ON a.id=k.account_id "
                "WHERE c.status!='REVOKED' AND u.active=1 ORDER BY k.rowid").fetchall()
        return [dict(row) for row in rows]

    def bank_actor(self, initiator, row, db=None):
        if db is None:
            with self.administration._connection() as connection:
                return self.bank_actor(initiator, row, connection)
        self.administration._actor(db, initiator)
        owner = db.execute(
            "SELECT u.* FROM app_users u JOIN app_bank_connections c ON c.user_id=u.id "
            "JOIN server_cards k ON k.connection_id=c.id AND k.owner_id=u.id "
            "JOIN accounts a ON a.id=k.account_id "
            "WHERE u.active=1 AND u.subject IS NOT NULL AND c.status!='REVOKED' "
            "AND c.bank_id='POSTBANK' AND c.id=? AND c.revision=? AND k.connection_revision=c.revision "
            "AND k.card_id=? AND k.account_id=? AND k.masked_number=? "
            "AND a.kind='CREDIT_CARD' AND a.currency='EUR' "
            "AND NOT EXISTS (SELECT 1 FROM server_cards other JOIN app_bank_connections oc ON oc.id=other.connection_id "
            "WHERE other.account_id=k.account_id AND other.card_id!=k.card_id AND oc.status!='REVOKED')",
            (row['id'], row['connection_revision'], row['card_id'], row['account_id'], row['masked_number'])).fetchone()
        if owner is None or owner['id'] != row['binding_owner']:
            raise AdministrationError('stale_revision', 409)
        return dict(owner)

    def valid(self, initiator, row):
        try:
            self.bank_actor(initiator, row)
            return True
        except AdministrationError:
            return False

    def plan(self, row, today):
        with closing(Store(self.database, readonly=True)) as store:
            account = store.db.execute('SELECT opening_date FROM accounts WHERE id=?', (row['account_id'],)).fetchone()
            latest = store.db.execute('SELECT MAX(date) FROM active_transactions WHERE account_id=?',
                                      (row['account_id'],)).fetchone()[0]
        opening = date.fromisoformat(account['opening_date']) + timedelta(days=1)
        # Include the last stored day, so later same-day bookings are not skipped.
        start = max(opening, today - timedelta(days=90))
        if latest:
            start = max(start, min(date.fromisoformat(latest), today))
        return [(min(start, today).isoformat(), today.isoformat())]

    def record(self, row, *, status, now, code=None, inserted=None, attempt=False,
               diagnostic=None, initiated_by=None):
        with self.administration._connection(write=True) as db:
            if attempt:
                db.execute('UPDATE server_cards SET last_attempt_at=?,last_status=?,last_code=NULL,'
                           'last_inserted=NULL,last_triggered_by=? WHERE card_id=? AND connection_revision=?',
                           (now.isoformat(), 'running', initiated_by, row['card_id'], row['binding_revision']))
            else:
                db.execute('UPDATE server_cards SET last_status=?,last_code=?,last_inserted=?,last_attempt_at=?,'
                           'last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END '
                           'WHERE card_id=? AND connection_revision=?',
                           (status, code, inserted, now.isoformat(), status == 'updated', now.isoformat(),
                            row['card_id'], row['binding_revision']))

    def run(self, initiator, row, planned, now):
        inserted = 0
        try:
            owner = self.bank_actor(initiator, row)
            start, end = planned[0]
            request = {'id': row['id'], 'revision': row['connection_revision'], 'confirmed': True,
                       'card_id': row['card_id'], 'tan_method': row['tan_method'],
                       'tan_medium': row['tan_medium'], 'start': start, 'end': end}
            result = _bounded_result(self.gateway.read(owner, request), request)
            if result['status'] in ('needs_method', 'needs_medium'):
                raise AdministrationError('authorization_required', 409)
            if result['status'] == 'error':
                raise AdministrationError(result['code'], 409)
            if result['card_masked'] != row['masked_number']:
                raise AdministrationError('invalid_bank_result', 409)
            from .card_ledger_import import apply
            with closing(Store(self.database, actor='user:' + initiator['id'])) as store:
                store.db.execute('BEGIN IMMEDIATE')
                try:
                    # The writer lock makes the final recheck and import indivisible with revocation.
                    self.bank_actor(initiator, row, store.db)
                    source_key = sha256(('c12:' + row['card_id'] + ':' + row['account_id'] + ':' + str(row['connection_revision'])).encode()).hexdigest()
                    old = store.db.execute('SELECT * FROM card_sources WHERE source_key=?', (source_key,)).fetchone()
                    facts = (source_key, row['card_id'], row['id'], row['binding_owner'],
                             row['connection_revision'], row['account_id'])
                    if old is not None and tuple(old) != facts:
                        raise AdministrationError('stale_revision', 409)
                    store.db.execute('INSERT OR IGNORE INTO card_sources VALUES (?,?,?,?,?,?)', facts)
                    outcome = apply(store, source_key, row['account_id'], result, now().isoformat())
                    store.db.commit()
                    inserted = outcome['inserted']
                except Exception:
                    store.db.rollback()
                    raise
            # Let the normal server backup scheduler handle the one committed change.
            from .backup_package import mark_backup_needed
            mark_backup_needed(self.database)
            self.record(row, status='updated', now=now(), inserted=inserted,
                        code='card_balance_unavailable' if not outcome['balance_saved'] else None)
        except AdministrationError as error:
            from .server_bank_refresh import _code
            status = 'setup_required' if error.code == 'authorization_required' else 'error'
            self.record(row, status=status, now=now(), code=_code(error.code), inserted=inserted)
        except ValueError:
            self.record(row, status='error', now=now(), code='card_import_review_required', inserted=inserted)
        except Exception:
            self.record(row, status='error', now=now(), code='bank_import_unavailable', inserted=inserted)
