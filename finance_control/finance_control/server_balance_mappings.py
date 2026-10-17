"""Explicit owner-bound bindings for read-only server balance evidence.

These records provide no authority to import transactions or change the ledger.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from unicodedata import normalize

from .administration import AdministrationError, _id, _revision
from .bank_account_policy import bank_account_kind_supported
from .core import money, valid_identifier


_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_MASKED = re.compile(r'••••(?:[A-Za-z0-9]{4})?\Z')
_AMOUNT = re.compile(r'-?[0-9]+(?:\.[0-9]+)?\Z')
_CURRENCY = re.compile(r'[A-Z]{3}\Z')
_BANKS = frozenset({'POSTBANK', 'ING', 'NASPA'})
_KNOWN_INSTITUTIONS = {
    'postbank': 'POSTBANK', 'deutschepostbank': 'POSTBANK',
    'ing': 'ING', 'ingdiba': 'ING', 'ingdibaag': 'ING',
    'naspa': 'NASPA', 'nassauischessparkasse': 'NASPA',
    'nassauischesparkasse': 'NASPA',
}
_CONTEXT_FIELDS = frozenset({
    'job_id', 'owner_id', 'connection_id', 'revision', 'bank_id',
    'retrieved_at', 'accounts',
})


def _fail(code, status=400):
    raise AdministrationError(code, status)


def target_compatible(account, bank_id):
    """Check the existing bank-kind/currency policy and veto known-name conflicts.

    ``accounts.institution`` is free text. An unrecognized value is not treated
    as proof of bank identity; explicit user confirmation remains necessary.
    """
    if (not isinstance(account, (dict, sqlite3.Row)) or type(bank_id) is not str
            or bank_id not in _BANKS):
        return False
    try:
        institution = account['institution']
        kind, currency = account['kind'], account['currency']
    except (KeyError, IndexError, TypeError):
        return False
    if type(institution) is not str or not bank_account_kind_supported(bank_id, kind, currency):
        return False
    normalized = ''.join(
        character for character in normalize('NFKC', institution).casefold()
        if character.isalnum())
    institution_bank = _KNOWN_INSTITUTIONS.get(normalized)
    return institution_bank is None or institution_bank == bank_id


def _job_context(value, job_id, actor_id):
    if type(value) is not dict or set(value) != _CONTEXT_FIELDS:
        _fail('balance_mapping_invalid')
    if (value['job_id'] != job_id or value['owner_id'] != actor_id
            or type(value['connection_id']) is not str
            or type(value['revision']) is not int or value['revision'] < 1
            or type(value['bank_id']) is not str or value['bank_id'] not in _BANKS):
        _fail('balance_mapping_invalid')
    try:
        connection_id = _id(value['connection_id'])
        revision = _revision(value['revision'])
        retrieved = datetime.fromisoformat(value['retrieved_at'])
        if retrieved.utcoffset() is None:
            raise ValueError
        retrieved_at = retrieved.astimezone(UTC).isoformat()
    except (AdministrationError, TypeError, ValueError, OverflowError):
        _fail('balance_mapping_invalid')
    rows = value['accounts']
    if type(rows) not in (list, tuple) or len(rows) > 20:
        _fail('balance_mapping_invalid')
    by_fingerprint = {}
    for row in rows:
        if type(row) is not dict or set(row) != {
                'fingerprint', 'masked_account', 'amount', 'currency', 'booked_on'}:
            _fail('balance_mapping_invalid')
        fingerprint, masked = row['fingerprint'], row['masked_account']
        amount, currency, booked_on = row['amount'], row['currency'], row['booked_on']
        if (type(fingerprint) is not str or _FINGERPRINT.fullmatch(fingerprint) is None
                or fingerprint in by_fingerprint or type(masked) is not str
                or _MASKED.fullmatch(masked) is None or type(amount) is not str
                or len(amount) > 80 or _AMOUNT.fullmatch(amount) is None
                or type(currency) is not str or _CURRENCY.fullmatch(currency) is None
                or type(booked_on) is not str):
            _fail('balance_mapping_invalid')
        try:
            value_amount = money(Decimal(amount))
            if date.fromisoformat(booked_on).isoformat() != booked_on:
                raise ValueError
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            _fail('balance_mapping_invalid')
        by_fingerprint[fingerprint] = {
            'fingerprint': fingerprint, 'masked_account': masked,
            'amount': format(value_amount, '.2f'), 'currency': currency,
            'booked_on': booked_on,
        }
    return {
        'job_id': job_id, 'owner_id': actor_id, 'connection_id': connection_id,
        'revision': revision, 'bank_id': value['bank_id'],
        'retrieved_at': retrieved_at, 'accounts': by_fingerprint,
    }


class ServerBalanceMappings:
    """Persist explicit account mappings and immutable read-only evidence."""

    def __init__(self, administration, database):
        self.administration = administration
        self.database = database

    @staticmethod
    def _request(actor, data):
        if type(data) is not dict or set(data) != {'job_id', 'confirmed', 'mappings'}:
            _fail('balance_mapping_invalid')
        if data['confirmed'] is not True:
            _fail('confirmation_required')
        try:
            job_id = _id(data['job_id'])
        except AdministrationError:
            _fail('balance_mapping_invalid')
        actor_id = actor.get('id') if type(actor) is dict else None
        mappings = data['mappings']
        if type(mappings) is not list or not mappings or len(mappings) > 20:
            _fail('balance_mapping_invalid')
        by_fingerprint = {}
        targets = set()
        for mapping in mappings:
            if type(mapping) is not dict or set(mapping) != {'account_fingerprint', 'account_id'}:
                _fail('balance_mapping_invalid')
            fingerprint, account_id = mapping['account_fingerprint'], mapping['account_id']
            if (type(fingerprint) is not str or _FINGERPRINT.fullmatch(fingerprint) is None
                    or not valid_identifier(account_id)
                    or fingerprint in by_fingerprint or account_id in targets):
                _fail('balance_mapping_invalid')
            by_fingerprint[fingerprint] = account_id
            targets.add(account_id)
        return job_id, actor_id, by_fingerprint

    def _check_connection(self, db, actor, context):
        owner = self.administration._actor(db, actor)
        connection = db.execute(
            "SELECT id,user_id,bank_id,revision FROM app_bank_connections "
            "WHERE id=? AND status!='REVOKED'", (context['connection_id'],)).fetchone()
        if connection is None:
            _fail('unknown_connection', 404)
        if connection['user_id'] != owner['id'] or owner['id'] != context['owner_id']:
            _fail('forbidden', 403)
        if (connection['revision'] != context['revision']
                or connection['bank_id'] != context['bank_id']):
            _fail('stale_revision', 409)
        return owner

    def _check_targets(self, db, context, selections):
        for fingerprint, account_id in selections.items():
            evidence = context['accounts'].get(fingerprint)
            if evidence is None:
                _fail('balance_mapping_not_found', 404)
            account = db.execute(
                'SELECT id,institution,kind,owner,currency FROM accounts WHERE id=?',
                (account_id,)).fetchone()
            if (account is None or not target_compatible(account, context['bank_id'])
                    or account['currency'] != evidence['currency']):
                _fail('invalid_target')

            binding = db.execute(
                'SELECT provider,source_key FROM bank_source_accounts WHERE account_id=?',
                (account_id,)).fetchone()
            if binding is not None and binding['provider'] != context['bank_id']:
                _fail('source_binding_conflict', 409)
            refresh = db.execute(
                'SELECT connection_id,account_fingerprint,bank_id FROM server_bank_refresh_bindings '
                'WHERE account_id=?', (account_id,)).fetchone()
            if (refresh is not None and (refresh['connection_id'] != context['connection_id']
                    or refresh['account_fingerprint'] != fingerprint
                    or refresh['bank_id'] != context['bank_id'])):
                _fail('source_binding_conflict', 409)

            prior = db.execute(
                'SELECT owner_id,bank_id,account_id FROM server_bank_balance_bindings '
                'WHERE connection_id=? AND account_fingerprint=?',
                (context['connection_id'], fingerprint)).fetchone()
            if prior is not None and (prior['owner_id'] != context['owner_id']
                    or prior['bank_id'] != context['bank_id']
                    or prior['account_id'] != account_id):
                _fail('balance_mapping_conflict', 409)
            other = db.execute(
                'SELECT owner_id,connection_id,account_fingerprint FROM server_bank_balance_bindings '
                'WHERE account_id=? AND NOT (connection_id=? AND account_fingerprint=?)',
                (account_id, context['connection_id'], fingerprint)).fetchone()
            if other is not None:
                _fail('balance_mapping_conflict', 409)

    def commit(self, actor, data, job_context):
        """Confirm selected balances from a completed owner-bound in-memory job."""
        job_id, actor_id, selections = self._request(actor, data)
        context = _job_context(job_context, job_id, actor_id)
        if any(fingerprint not in context['accounts'] for fingerprint in selections):
            _fail('balance_mapping_not_found', 404)

        # Fail fast before waiting for SQLite's writer lock, then repeat every
        # identity/revision/metadata check inside the write transaction.
        with self.administration._connection() as db:
            self._check_connection(db, actor, context)
            self._check_targets(db, context, selections)

        now = datetime.now(UTC).isoformat()
        saved = []
        try:
            with self.administration._connection(write=True) as db:
                self._check_connection(db, actor, context)
                self._check_targets(db, context, selections)
                for fingerprint, account_id in selections.items():
                    row = context['accounts'][fingerprint]
                    current = db.execute(
                        'SELECT owner_id,connection_revision,bank_id,account_id '
                        'FROM server_bank_balance_bindings '
                        'WHERE connection_id=? AND account_fingerprint=?',
                        (context['connection_id'], fingerprint)).fetchone()
                    if current is None:
                        db.execute(
                            'INSERT INTO server_bank_balance_bindings '
                            '(connection_id,account_fingerprint,owner_id,connection_revision,'
                            'bank_id,account_id,created_at) VALUES (?,?,?,?,?,?,?)',
                            (context['connection_id'], fingerprint, context['owner_id'],
                             context['revision'], context['bank_id'], account_id, now))
                    else:
                        if (current['owner_id'] != context['owner_id']
                                or current['bank_id'] != context['bank_id']
                                or current['account_id'] != account_id):
                            _fail('balance_mapping_conflict', 409)
                        # Same explicit mapping can be reconfirmed after a valid
                        # credential revision; the old evidence remains immutable.
                        db.execute(
                            'UPDATE server_bank_balance_bindings SET connection_revision=? '
                            'WHERE connection_id=? AND account_fingerprint=?',
                            (context['revision'], context['connection_id'], fingerprint))
                    exists = db.execute(
                        'SELECT owner_id,connection_revision,bank_id,account_id,masked_account,'
                        'amount,currency,booked_on,retrieved_at FROM server_bank_balance_evidence '
                        'WHERE connection_id=? AND account_fingerprint=? AND job_id=?',
                        (context['connection_id'], fingerprint, job_id)).fetchone()
                    expected_proof = (context['owner_id'], context['revision'], context['bank_id'],
                                      account_id, row['masked_account'], row['amount'], row['currency'],
                                      row['booked_on'], context['retrieved_at'])
                    if exists is not None and tuple(exists) != expected_proof:
                        _fail('balance_mapping_conflict', 409)
                    if exists is None:
                        db.execute(
                            'INSERT INTO server_bank_balance_evidence '
                            '(connection_id,account_fingerprint,owner_id,connection_revision,'
                            'bank_id,job_id,account_id,masked_account,amount,currency,booked_on,'
                            'retrieved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            (context['connection_id'], fingerprint, context['owner_id'],
                             context['revision'], context['bank_id'], job_id, account_id,
                             row['masked_account'], row['amount'], row['currency'],
                             row['booked_on'], context['retrieved_at']))
                        saved.append(account_id)
        except sqlite3.IntegrityError as error:
            message = str(error).casefold()
            code = ('source_binding_conflict' if 'bank_source' in message
                    else 'balance_mapping_conflict')
            raise AdministrationError(code, 409) from None
        return {'saved': len(saved), 'account_ids': list(selections.values())}

    def bindings(self, actor, connection_id, revision):
        """List the caller's saved mappings for the current connection revision."""
        connection_id, revision = _id(connection_id), _revision(revision)
        with self.administration._connection() as db:
            owner = self.administration._actor(db, actor)
            connection = db.execute(
                "SELECT id,user_id,revision FROM app_bank_connections "
                "WHERE id=? AND status!='REVOKED'", (connection_id,)).fetchone()
            if connection is None:
                _fail('unknown_connection', 404)
            if connection['user_id'] != owner['id']:
                _fail('forbidden', 403)
            if connection['revision'] != revision:
                _fail('stale_revision', 409)
            return [dict(row) for row in db.execute(
                'SELECT b.account_fingerprint,b.account_id,a.display_name,a.currency,'
                'b.connection_revision FROM server_bank_balance_bindings b '
                'JOIN accounts a ON a.id=b.account_id '
                'WHERE b.connection_id=? AND b.owner_id=? ORDER BY b.account_id',
                (connection_id, owner['id']))]
