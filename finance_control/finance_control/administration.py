"""Local administration metadata for verified web identities.

The caller must verify the identity token before resolve_identity. This module
stores no banking credentials and never contacts a bank.
"""

import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from finance_control.core import Store


BANKS = (
    {'id': 'POSTBANK', 'name': 'Postbank', 'bank_code': '50010060', 'logo': '/bank-postbank.svg'},
    {'id': 'ING', 'name': 'ING', 'bank_code': '50010517', 'logo': '/bank-ing.svg'},
    {'id': 'NASPA', 'name': 'Nassauische Sparkasse', 'bank_code': '51050015',
     'logo': '/bank-sparkasse.png'},
)
_DOMAIN_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
_EMAIL = re.compile(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@' +
                    _DOMAIN_LABEL + r'(?:\.' + _DOMAIN_LABEL + r')+\Z')


class AdministrationError(Exception):
    def __init__(self, code, status=400):
        self.code = code
        self.status = status
        super().__init__(code)


def _email(value):
    if not isinstance(value, str) or len(value) > 254 or not value.isascii() or not _EMAIL.fullmatch(value):
        raise AdministrationError('invalid_email')
    return value.casefold()


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100 or any(ord(c) < 32 for c in value):
        raise AdministrationError('invalid_display_name')
    return value.strip()


def _revision(value):
    if type(value) is not int or value < 1:
        raise AdministrationError('invalid_revision')
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise AdministrationError('invalid_id')
    return value


def _public(row):
    return {key: row[key] for key in ('id', 'email', 'display_name', 'role', 'revision')}


class Administration:
    def __init__(self, database, before_bank_revoke=None):
        self.database = Path(database).expanduser().resolve()
        self.before_bank_revoke = before_bank_revoke
        with closing(Store(self.database)):
            pass

    @contextmanager
    def _connection(self, *, write=False):
        with closing(sqlite3.connect(self.database, timeout=10, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            try:
                if write:
                    db.execute('BEGIN IMMEDIATE')
                yield db
                if write:
                    db.commit()
                    if db.total_changes:
                        from .backup_package import mark_backup_needed
                        try:
                            mark_backup_needed(self.database)
                        except (OSError, ValueError):
                            pass
            except Exception:
                if write:
                    db.rollback()
                raise

    @staticmethod
    def _actor(db, actor):
        if not isinstance(actor, dict):
            raise AdministrationError('unauthorized', 401)
        user_id, subject = actor.get('id'), actor.get('subject')
        if not isinstance(user_id, str) or not isinstance(subject, str) or not subject:
            raise AdministrationError('unauthorized', 401)
        row = db.execute('SELECT * FROM app_users WHERE id=? AND subject=? AND active=1',
                         (user_id, subject)).fetchone()
        if row is None or actor.get('email') != row['email']:
            raise AdministrationError('unauthorized', 401)
        return row

    def resolve_identity(self, identity, bootstrap_emails=()):
        if not isinstance(identity, dict) or not isinstance(identity.get('sub'), str) or not identity['sub'] or len(identity['sub']) > 256:
            raise AdministrationError('invalid_identity', 401)
        subject = identity['sub']
        try:
            email = _email(identity.get('email'))
        except AdministrationError as error:
            raise AdministrationError('invalid_identity', 401) from error
        bootstrap = {_email(value) for value in bootstrap_emails}
        # Bound active identities are read on every request. Only first binding or
        # bootstrap needs a writer lock; the write path rechecks after acquiring it.
        with self._connection() as db:
            bound = db.execute('SELECT * FROM app_users WHERE subject=?', (subject,)).fetchone()
            if bound is not None and bound['active'] and bound['email'] == email:
                return dict(bound)
        with self._connection(write=True) as db:
            row = db.execute('SELECT * FROM app_users WHERE subject=? OR email=?',
                             (subject, email)).fetchall()
            if len(row) > 1 or (row and (row[0]['email'] != email or row[0]['subject'] not in (None, subject))):
                raise AdministrationError('identity_conflict', 403)
            if row:
                user = row[0]
                if not user['active']:
                    raise AdministrationError('inactive_user', 403)
                if user['subject'] is None:
                    db.execute('UPDATE app_users SET subject=? WHERE id=?', (subject, user['id']))
            else:
                if email not in bootstrap or db.execute('SELECT 1 FROM app_users LIMIT 1').fetchone():
                    raise AdministrationError('unknown_identity', 403)
                user_id = uuid4().hex
                db.execute('INSERT INTO app_users (id,email,display_name,role,subject) VALUES (?,?,?,?,?)',
                           (user_id, email, email.split('@')[0][:100], 'admin', subject))
            return dict(db.execute('SELECT * FROM app_users WHERE subject=?', (subject,)).fetchone())

    def _state(self, db, actor):
        users = []
        if actor['role'] == 'admin':
            users = [_public(row) | {'active': bool(row['active'])} for row in
                     db.execute('SELECT * FROM app_users ORDER BY email')]
        connections = [dict(row) for row in db.execute(
            "SELECT id,bank_id,label,status,revision FROM app_bank_connections "
            "WHERE user_id=? AND status!='REVOKED' ORDER BY rowid", (actor['id'],))]
        return {'me': _public(actor), 'users': users, 'banks': [dict(bank) for bank in BANKS],
                'connections': connections, 'shared_household': True}

    def state(self, user):
        with self._connection() as db:
            return self._state(db, self._actor(db, user))

    def change(self, user_id, action, data):
        fields = {
            'profile-save': {'display_name', 'revision', 'confirmed'},
            'user-add': {'email', 'display_name', 'role', 'confirmed'},
            'user-remove': {'id', 'revision', 'confirmed'},
            'user-restore': {'id', 'revision', 'confirmed'},
            'bank-add': {'bank_id', 'label', 'confirmed'},
            'bank-remove': {'id', 'revision', 'confirmed'},
        }
        if action not in fields or not isinstance(data, dict) or set(data) != fields[action]:
            raise AdministrationError('invalid_action')
        if data['confirmed'] is not True:
            raise AdministrationError('confirmation_required')
        with self._connection(write=True) as db:
            actor = self._actor(db, user_id)
            if action == 'profile-save':
                if actor['revision'] != _revision(data['revision']):
                    raise AdministrationError('stale_revision', 409)
                db.execute('UPDATE app_users SET display_name=?,revision=revision+1 WHERE id=?',
                           (_name(data['display_name']), actor['id']))
            elif action == 'user-add':
                if actor['role'] != 'admin':
                    raise AdministrationError('forbidden', 403)
                email = _email(data['email'])
                name = _name(data['display_name'])
                if not isinstance(data['role'], str) or data['role'] not in ('member', 'admin'):
                    raise AdministrationError('invalid_role')
                if db.execute('SELECT 1 FROM app_users WHERE email=?', (email,)).fetchone():
                    raise AdministrationError('duplicate_email', 409)
                db.execute('INSERT INTO app_users (id,email,display_name,role) VALUES (?,?,?,?)',
                           (uuid4().hex, email, name, data['role']))
            elif action == 'user-remove':
                target = db.execute('SELECT * FROM app_users WHERE id=? AND active=1',
                                    (_id(data['id']),)).fetchone()
                if target is None:
                    raise AdministrationError('unknown_user', 404)
                if actor['role'] != 'admin' and actor['id'] != target['id']:
                    raise AdministrationError('forbidden', 403)
                if target['revision'] != _revision(data['revision']):
                    raise AdministrationError('stale_revision', 409)
                if target['role'] == 'admin' and db.execute(
                        "SELECT count(*) FROM app_users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                    raise AdministrationError('last_admin', 409)
                connections = [dict(row) for row in db.execute(
                    "SELECT id,user_id FROM app_bank_connections "
                    "WHERE user_id=? AND status!='REVOKED' ORDER BY rowid", (target['id'],))]
                if self.before_bank_revoke is not None:
                    self.before_bank_revoke(connections)
                db.execute('UPDATE app_users SET active=0,revision=revision+1 WHERE id=?', (target['id'],))
                db.execute("UPDATE app_bank_connections SET status='REVOKED',revision=revision+1 "
                           "WHERE user_id=? AND status!='REVOKED'", (target['id'],))
                if target['id'] == actor['id']:
                    return {'removed': True}
            elif action == 'user-restore':
                if actor['role'] != 'admin':
                    raise AdministrationError('forbidden', 403)
                target = db.execute('SELECT * FROM app_users WHERE id=? AND active=0',
                                    (_id(data['id']),)).fetchone()
                if target is None:
                    raise AdministrationError('unknown_user', 404)
                if target['revision'] != _revision(data['revision']):
                    raise AdministrationError('stale_revision', 409)
                db.execute('UPDATE app_users SET active=1,revision=revision+1 WHERE id=?',
                           (target['id'],))
            elif action == 'bank-add':
                if not isinstance(data['bank_id'], str) or data['bank_id'] not in {bank['id'] for bank in BANKS}:
                    raise AdministrationError('invalid_bank')
                db.execute('INSERT INTO app_bank_connections (id,user_id,bank_id,label,status) VALUES (?,?,?,?,?)',
                           (uuid4().hex, actor['id'], data['bank_id'], _name(data['label']),
                            'LOCAL_SETUP_REQUIRED'))
            elif action == 'bank-remove':
                connection = db.execute('SELECT * FROM app_bank_connections WHERE id=? AND status!=?',
                                        (_id(data['id']), 'REVOKED')).fetchone()
                if connection is None:
                    raise AdministrationError('unknown_connection', 404)
                if connection['user_id'] != actor['id']:
                    raise AdministrationError('forbidden', 403)
                if connection['revision'] != _revision(data['revision']):
                    raise AdministrationError('stale_revision', 409)
                if self.before_bank_revoke is not None:
                    self.before_bank_revoke([{'id': connection['id'], 'user_id': connection['user_id']}])
                db.execute("UPDATE app_bank_connections SET status='REVOKED',revision=revision+1 WHERE id=?",
                           (connection['id'],))
            fresh = self._actor(db, user_id)
            return self._state(db, fresh)
