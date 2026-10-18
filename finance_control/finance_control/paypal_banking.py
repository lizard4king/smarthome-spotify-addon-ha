"""Personal PayPal AIS consent, bounded previews and reviewed ledger imports.

Enable Banking application keys and consent metadata live in the encrypted HA
vault. A verified active cockpit identity and a single-use state are both
required on return. Provider registration/own-account linking is done by its
owner, outside this application.
"""

from copy import deepcopy
import hashlib
import json
import re
import secrets
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from .core import Store
from .paypal_import import commit_import, normalize_transactions, preview_import

from .administration import AdministrationError
from .connectors.enable_banking import EnableBankingClient, EnableBankingError, canonical_uuid
from .paypal_balance_view import sanitize_balances
from .security.server_paypal_store import ServerPayPalStore

_FIELDS = {
    'paypal-state': set(),
    'paypal-prepare': {'confirmed'},
    'paypal-configure': {'confirmed', 'application_id'},
    'paypal-authorize': {'confirmed', 'country'},
    'paypal-disconnect': {'confirmed'},
    'paypal-read': {'account_uid', 'date_from', 'date_to'},
    'paypal-balances': set(),
    'paypal-import-targets': set(),
    'paypal-import-preview': {'account_uid', 'date_from', 'date_to', 'account_id', 'matches'},
    'paypal-import-commit': {'review_token', 'confirmed'},
}
_STATE_TTL = 900
_IMPORT_TTL = 600
_MAX_IMPORT_REVIEWS = 32


def _fail(code, status=400):
    raise AdministrationError(code, status)


def _uuid(value):
    try:
        return canonical_uuid(value, 'invalid_id')
    except EnableBankingError:
        _fail('paypal_invalid_id')


def _expiry(value):
    try:
        if type(value) is not str or len(value) > 64:
            raise ValueError
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result.timestamp()
    except (ValueError, OverflowError):
        _fail('paypal_invalid_response', 502)


def _access_expiry(result):
    access = result.get('access')
    if type(access) is not dict:
        _fail('paypal_invalid_response', 502)
    value = access.get('valid_until')
    _expiry(value)
    return value


def _origin(value):
    try:
        if type(value) is not str or len(value) > 512 or any(ord(c) < 33 for c in value):
            raise ValueError
        url = urlsplit(value)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.port not in (None, 443) or url.path or url.query or url.fragment
                or '\\' in value):
            raise ValueError
    except ValueError:
        _fail('paypal_https_required')
    return value


def _day(value):
    try:
        if type(value) is not str or len(value) != 10:
            raise ValueError
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except ValueError:
        _fail('invalid_period')


def _certificate(now):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Finance Control personal AIS')])
    stamp = datetime.fromtimestamp(now, timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(stamp - timedelta(minutes=5))
                   .not_valid_after(stamp + timedelta(days=365))
                   .sign(key, hashes.SHA256()))
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode('ascii'),
            certificate.public_bytes(serialization.Encoding.PEM).decode('ascii'))


class PayPalBanking:
    def __init__(self, administration, store_factory=None, client_factory=None, clock=None):
        self.administration = administration
        self.store_factory = store_factory or ServerPayPalStore
        self.client_factory = client_factory or EnableBankingClient
        self.clock = clock or time.time
        self._locks = WeakValueDictionary()
        self._locks_guard = threading.Lock()
        self._import_reviews = {}
        self._reviews_guard = threading.Lock()

    def _drop_import_review(self, owner):
        with self._reviews_guard:
            self._import_reviews.pop(owner, None)

    def _remember_import_review(self, owner, review):
        prepared = deepcopy(review)
        now = self.clock()
        with self._reviews_guard:
            self._import_reviews = {key: value for key, value in self._import_reviews.items()
                                    if value['expires_at'] > now and key != owner}
            # Bound sensitive RAM even if many owners leave a review unattended.
            if len(self._import_reviews) >= _MAX_IMPORT_REVIEWS:
                oldest = min(self._import_reviews, key=lambda key: self._import_reviews[key]['expires_at'])
                self._import_reviews.pop(oldest)
            self._import_reviews[owner] = prepared

    def _take_import_review(self, owner, token):
        if type(token) is not str or re.fullmatch(r'[A-Za-z0-9_-]{43}', token) is None:
            _fail('paypal_import_stale', 409)
        with self._reviews_guard:
            review = self._import_reviews.get(owner)
            if review and review['expires_at'] <= self.clock():
                self._import_reviews.pop(owner)
                review = None
            if not review or not secrets.compare_digest(review['token'], token):
                _fail('paypal_import_stale', 409)
            # Consume before provider I/O; transport failures also need a new review.
            return self._import_reviews.pop(owner)

    def _owner_lock(self, owner):
        # Strong references held by callers keep an owner's lock alive while
        # requests run or wait. Idle owners leave no permanent lock registry.
        with self._locks_guard:
            lock = self._locks.get(owner)
            if lock is None:
                lock = threading.RLock()
                self._locks[owner] = lock
            return lock

    def _owner(self, actor):
        with self.administration._connection() as db:
            return self.administration._actor(db, actor)['id']

    def _store(self):
        if sys.platform != 'linux':
            _fail('paypal_unsupported', 503)
        return self.store_factory()

    def _client(self, record):
        if not record or not record.get('application_id'):
            _fail('paypal_setup_required', 409)
        return self.client_factory(record['application_id'], record['private_key'])

    def _save(self, actor, store, owner, record):
        # Membership and the vault mutation share the user-removal writer lock.
        # A removal cannot slip between the final check and this write.
        with self.administration._connection(write=True) as db:
            self.administration._actor(db, actor)
            store.save(owner, record)

    def _delete(self, actor, store, owner):
        with self.administration._connection(write=True) as db:
            self.administration._actor(db, actor)
            store.delete(owner)

    def _public(self, record, origin):
        session = record.get('session') if record else None
        authorized = session and _expiry(session['expires_at']) > self.clock()
        return {
            'supported': sys.platform == 'linux',
            'setup': ('AUTHORIZED' if authorized else 'APPLICATION_READY'
                      if record and record.get('application_id') else 'CERTIFICATE_READY'
                      if record else 'UNCONFIGURED'),
            'certificate_pem': record['certificate'] if record else None,
            'application_id': record.get('application_id') if record else None,
            'callback_url': record['callback_url'] if record else origin + '/paypal-return',
            'expires_at': session['expires_at'] if session else None,
            'accounts': session['accounts'] if authorized else [],
            'last_read': None,
        }

    def dispatch(self, actor, action, data, origin):
        if action not in _FIELDS or type(data) is not dict or set(data) != _FIELDS[action]:
            _fail('invalid_action')
        if 'confirmed' in data and data['confirmed'] is not True:
            _fail('confirmation_required')
        owner = self._owner(actor)
        origin = _origin(origin)
        if action == 'paypal-state' and sys.platform != 'linux':
            return self._public(None, origin)
        try:
            with self._owner_lock(owner):
                self._owner(actor)
                store = self._store()
                record = store.load(owner)
                if record and record['callback_url'] != origin + '/paypal-return':
                    _fail('paypal_origin_changed', 409)
                if action == 'paypal-state':
                    self._owner(actor)
                    return self._public(record, origin)
                if action == 'paypal-prepare':
                    if not record:
                        key, certificate = _certificate(self.clock())
                        record = {'version': 1, 'private_key': key, 'certificate': certificate,
                                  'callback_url': origin + '/paypal-return', 'application_id': None,
                                  'pending': None, 'session': None}
                        self._save(actor, store, owner, record)
                elif action == 'paypal-configure':
                    if not record:
                        _fail('paypal_setup_required', 409)
                    application_id = _uuid(data['application_id'])
                    if record.get('pending') or record.get('session'):
                        _fail('paypal_disconnect_required', 409)
                    record['application_id'] = application_id
                    self._save(actor, store, owner, record)
                elif action == 'paypal-authorize':
                    return self._authorize(actor, owner, store, record, data['country'])
                elif action == 'paypal-disconnect':
                    self._drop_import_review(owner)
                    if record and record.get('session'):
                        self._client(record).close_session(record['session']['session_id'])
                    self._delete(actor, store, owner)
                    record = None
                elif action == 'paypal-read':
                    return self._read(actor, record, data)
                elif action == 'paypal-balances':
                    return self._balances(actor, record)
                elif action == 'paypal-import-targets':
                    return self._import_targets(actor, owner)
                elif action == 'paypal-import-preview':
                    return self._import_preview(actor, owner, record, data)
                elif action == 'paypal-import-commit':
                    return self._import_commit(actor, owner, record, data)
                self._owner(actor)
                return self._public(record, origin)
        except AdministrationError:
            raise
        except EnableBankingError as error:
            code = ('paypal_access_expired' if error.status in (401, 403)
                    else 'paypal_provider_failed')
            _fail(code, 502)
        except Exception:
            _fail('paypal_unavailable', 503)

    def _authorize(self, actor, owner, store, record, country):
        if country not in ('DE', 'LU'):
            _fail('paypal_invalid_country')
        client = self._client(record)
        application = client.application()
        if (application.get('kid') != record['application_id']
                or application.get('environment') != 'PRODUCTION'
                or application.get('active') is not True
                or type(application.get('redirect_urls')) is not list
                or record['callback_url'] not in application.get('redirect_urls', [])
                or type(application.get('services')) is not list
                or 'AIS' not in application.get('services', [])):
            _fail('paypal_application_not_ready', 409)
        providers = client.providers().get('aspsps')
        if type(providers) is not list:
            _fail('paypal_invalid_response', 502)
        candidates = [p for p in providers if type(p) is dict
                      and str(p.get('name', '')).casefold() == 'paypal' and p.get('country') == country]
        if len(candidates) != 1:
            _fail('paypal_country_unavailable', 409)
        validity = candidates[0].get('maximum_consent_validity')
        if type(validity) is not int or validity < 60:
            _fail('paypal_invalid_response', 502)
        if record.get('session'):
            # Never orphan an old provider grant when starting another consent.
            client.close_session(record['session']['session_id'])
            record['session'] = None
            self._save(actor, store, owner, record)
        state = secrets.token_urlsafe(32)
        provider = {'name': candidates[0]['name'], 'country': country}
        pending = {'digest': hashlib.sha256(state.encode('ascii')).hexdigest(),
                   'expires_at': self.clock() + _STATE_TTL, 'provider': provider}
        expires = datetime.fromtimestamp(self.clock() + min(90 * 86400, validity) - 10,
                                        timezone.utc).isoformat()
        result = client.start_authorization(provider, state, record['callback_url'], expires)
        record['pending'] = pending
        self._save(actor, store, owner, record)
        return {'authorization_url': result['url']}

    def complete(self, actor, query):
        """Called only after Host, cockpit authentication and identity checks."""
        owner = self._owner(actor)
        try:
            if type(query) is not str or len(query) > 8192:
                _fail('paypal_invalid_return')
            try:
                values = parse_qs(query, keep_blank_values=True, max_num_fields=8,
                                  errors='strict')
            except (ValueError, UnicodeError):
                _fail('paypal_invalid_return')
            if any(len(v) != 1 for v in values.values()):
                _fail('paypal_invalid_return')
            state = values.get('state', [''])[0]
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', state):
                _fail('paypal_invalid_return')
            with self._owner_lock(owner):
                store = self._store()
                record = store.load(owner)
                pending = record.get('pending') if record else None
                if (not pending or pending['expires_at'] <= self.clock()
                        or not secrets.compare_digest(pending['digest'],
                                                      hashlib.sha256(state.encode('ascii')).hexdigest())):
                    _fail('paypal_return_expired', 409)
                # Consume before exchanging the authorization code, including errors.
                record['pending'] = None
                self._save(actor, store, owner, record)
                if 'error' in values or not values.get('code', [''])[0]:
                    _fail('paypal_consent_failed', 409)
                result = self._client(record).authorize_session(values['code'][0])
                session = self._session_result(result, pending['provider'])
                record['session'] = session
                self._save(actor, store, owner, record)
        except AdministrationError:
            raise
        except EnableBankingError:
            _fail('paypal_provider_failed', 502)
        except Exception:
            _fail('paypal_unavailable', 503)

    def _session_result(self, result, provider):
        if (type(result) is not dict or result.get('aspsp') != provider
                or result.get('psu_type') != 'personal'):
            _fail('paypal_invalid_response', 502)
        session_id = _uuid(result.get('session_id'))
        expiry = _access_expiry(result)
        if _expiry(expiry) <= self.clock():
            _fail('paypal_access_expired', 409)
        accounts = result.get('accounts')
        if type(accounts) is not list or not 1 <= len(accounts) <= 8:
            _fail('paypal_invalid_response', 502)
        minimized = []
        for account in accounts:
            if type(account) is not dict or account.get('usage') not in (None, 'PRIV'):
                _fail('paypal_personal_only', 409)
            uid = _uuid(account.get('uid'))
            name, currency = account.get('name') or 'PayPal', account.get('currency')
            if (type(name) is not str or len(name) > 256
                    or type(currency) is not str or not re.fullmatch('[A-Z]{3}', currency)):
                _fail('paypal_invalid_response', 502)
            minimized.append({'uid': uid, 'name': name, 'currency': currency})
        if len({a['uid'] for a in minimized}) != len(minimized):
            _fail('paypal_invalid_response', 502)
        return {'session_id': session_id, 'expires_at': expiry, 'provider': provider,
                'accounts': minimized}

    def _read(self, actor, record, data, *, import_identity=False):
        session = record.get('session') if record else None
        if not session or _expiry(session['expires_at']) <= self.clock():
            _fail('paypal_access_expired', 409)
        uid = _uuid(data['account_uid'])
        if uid not in {a['uid'] for a in session['accounts']}:
            _fail('forbidden', 403)
        start, end = _day(data['date_from']), _day(data['date_to'])
        today = datetime.fromtimestamp(self.clock(), ZoneInfo('Europe/Berlin')).date()
        if not start <= end <= today or (end - start).days >= 90:
            _fail('invalid_period')
        client = self._client(record)
        current = client.session(session['session_id'])
        if (type(current) is not dict or type(current.get('accounts')) is not list
                or not all(type(uid) is str for uid in current['accounts'])):
            _fail('paypal_invalid_response', 502)
        current_expiry = _access_expiry(current)
        if (current.get('status') != 'AUTHORIZED' or current.get('aspsp') != session['provider']
                or current.get('psu_type') != 'personal'
                or set(current['accounts']) != {a['uid'] for a in session['accounts']}
                or _expiry(current_expiry) <= self.clock()):
            _fail('paypal_access_expired', 409)
        source_key = None
        if import_identity:
            account = next(a for a in session['accounts'] if a['uid'] == uid)
            if account['currency'] not in ('EUR', 'XXX'):
                _fail('paypal_import_invalid')
            identities = current.get('accounts_data')
            if type(identities) is not list or not all(type(a) is dict for a in identities):
                _fail('paypal_import_identity', 409)
            selected = [a for a in identities if a.get('uid') == uid]
            if len(selected) != 1:
                _fail('paypal_import_identity', 409)
            identity = selected[0].get('identification_hash')
            if (type(identity) is not str or not 1 <= len(identity) <= 512
                    or identity != identity.strip() or any(not c.isprintable() for c in identity)
                    or sum(a.get('identification_hash') == identity for a in identities) != 1):
                _fail('paypal_import_identity', 409)
            source_key = 'paypal:' + hashlib.sha256((identity + '|EUR').encode('utf-8')).hexdigest()
        balances = client.balances(uid).get('balances')
        transactions = client.transactions(uid, start.isoformat(), end.isoformat()).get('transactions')
        if (type(balances) is not list or len(balances) > 100
                or not all(type(b) is dict for b in balances)
                or type(transactions) is not list or len(transactions) > 10_000
                or not all(type(t) is dict for t in transactions)):
            _fail('paypal_invalid_response', 502)
        result = {'balances': balances, 'transactions': transactions,
                  'complete': True, 'imported': False}
        if len(json.dumps(result, allow_nan=False).encode('utf-8')) > 8_000_000:
            _fail('paypal_invalid_response', 502)
        self._owner(actor)
        return (result, source_key) if import_identity else result

    @staticmethod
    def _import_error(error):
        code = str(error)
        if code in {'stale_paypal_preview', 'paypal_registry_ledger_conflict'}:
            _fail('paypal_import_stale', 409)
        if code in {'paypal_source_binding_conflict', 'paypal_foreign_bank_binding'}:
            _fail('paypal_import_identity', 409)
        if code == 'paypal_direct_transaction_changed':
            _fail('paypal_import_changed', 409)
        _fail('paypal_import_invalid')

    def _import_targets(self, actor, owner):
        with self.administration._connection() as db:
            self.administration._actor(db, actor)
            targets = [dict(row) for row in db.execute(
                "SELECT id,COALESCE(NULLIF(display_name,''),id) label,currency FROM accounts "
                "WHERE institution='PAYPAL' AND kind='CHECKING' AND currency='EUR' "
                "AND closed_on IS NULL ORDER BY id")]
            bindings = [dict(row) for row in db.execute(
                'SELECT account_id FROM paypal_source_accounts WHERE owner_id=? ORDER BY account_id',
                (owner,))]
        return {'targets': targets, 'bindings': bindings}

    def _import_preview(self, actor, owner, record, data):
        # Starting another preview invalidates the previous review even if the read fails.
        self._drop_import_review(owner)
        matches, target = data['matches'], data['account_id']
        if (type(target) is not str or not 1 <= len(target) <= 120
                or any(not char.isprintable() for char in target)
                or type(matches) is not dict or len(matches) > 10_000
                or any(type(key) is not str or not 1 <= len(key) <= 256
                       or (value is not None and (type(value) is not str or not 1 <= len(value) <= 512))
                       for key, value in matches.items())):
            _fail('paypal_import_invalid')
        read_data = {key: data[key] for key in ('account_uid', 'date_from', 'date_to')}
        result, source_key = self._read(actor, record, read_data, import_identity=True)
        try:
            rows = normalize_transactions(result['transactions'], data['date_from'], data['date_to'])
            with_store = Store(self.administration.database, readonly=True, actor=owner)
            try:
                view = preview_import(with_store, owner_id=owner, source_key=source_key,
                                      account_id=data['account_id'], rows=rows, matches=data['matches'])
            finally:
                with_store.close()
        except ValueError as error:
            self._import_error(error)
        token = secrets.token_urlsafe(32)
        self._owner(actor)
        self._remember_import_review(owner, {
            'token': token, 'expires_at': self.clock() + _IMPORT_TTL, 'read_data': read_data,
            'source_key': source_key, 'account_id': data['account_id'], 'rows': rows,
            'matches': data['matches'], 'revision': view['revision']})
        return {**view, 'review_token': token, 'expires_in': _IMPORT_TTL}

    def _import_commit(self, actor, owner, record, data):
        review = self._take_import_review(owner, data['review_token'])
        result, source_key = self._read(actor, record, review['read_data'], import_identity=True)
        try:
            rows = normalize_transactions(result['transactions'],
                                          review['read_data']['date_from'],
                                          review['read_data']['date_to'])
        except ValueError:
            _fail('paypal_import_changed', 409)
        if source_key != review['source_key'] or rows != review['rows']:
            _fail('paypal_import_changed', 409)
        self._owner(actor)
        ledger = Store(self.administration.database, actor=owner)
        try:
            # Each source cutover needs its own consistent local snapshot.
            # Independent first imports may both create backups intentionally;
            # do not hold SQLite's writer lock during backup or provider I/O.
            if not ledger.db.execute('SELECT 1 FROM paypal_source_accounts WHERE source_key=?',
                                     (source_key,)).fetchone():
                from .backup_package import create_backup_package
                create_backup_package(ledger, self.administration.database)
            try:
                committed = commit_import(
                    ledger, owner_id=owner, source_key=source_key,
                    account_id=review['account_id'], rows=review['rows'],
                    matches=review['matches'], revision=review['revision'], actor=owner,
                    guard=lambda db: self.administration._actor(db, actor))
            except ValueError as error:
                self._import_error(error)
        finally:
            ledger.close()
        from .backup_package import mark_backup_needed
        try:
            mark_backup_needed(self.administration.database)
        except (OSError, ValueError):
            pass
        return {**committed, 'imported': True}

    def _balances(self, actor, record):
        """Read current wallet balances for all consented accounts, never transactions."""
        session = record.get('session') if record else None
        if not session or _expiry(session['expires_at']) <= self.clock():
            _fail('paypal_access_expired', 409)
        accounts = session.get('accounts')
        if type(accounts) is not list or not 1 <= len(accounts) <= 8:
            _fail('paypal_invalid_response', 502)
        try:
            uids = [canonical_uuid(account['uid'], 'invalid_id') for account in accounts]
        except (KeyError, TypeError, EnableBankingError):
            _fail('paypal_invalid_response', 502)
        if len(set(uids)) != len(uids) or any(
                type(account.get('name')) is not str or not account['name'].strip()
                or len(account['name']) > 256 or type(account.get('currency')) is not str
                or re.fullmatch(r'[A-Z]{3}', account['currency']) is None
                for account in accounts):
            _fail('paypal_invalid_response', 502)
        client = self._client(record)
        current = client.session(session['session_id'])
        if (type(current) is not dict or type(current.get('accounts')) is not list
                or not all(type(uid) is str for uid in current['accounts'])):
            _fail('paypal_invalid_response', 502)
        current_expiry = _access_expiry(current)
        if (current.get('status') != 'AUTHORIZED' or current.get('aspsp') != session['provider']
                or current.get('psu_type') != 'personal'
                or len(current['accounts']) != len(uids)
                or set(current['accounts']) != set(uids)
                or _expiry(current_expiry) <= self.clock()):
            _fail('paypal_access_expired', 409)
        raw = [client.balances(uid) for uid in uids]
        retrieved_at = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        try:
            sanitized = [{'uid': uid, 'name': account['name'],
                          'currency': account['currency'],
                          'balances': sanitize_balances(response, account['currency'], retrieved_at)}
                         for uid, account, response in zip(uids, accounts, raw)]
        except ValueError:
            _fail('paypal_invalid_response', 502)
        self._owner(actor)
        return {'accounts': sanitized, 'retrieved_at': retrieved_at}

    def cleanup_owner(self, owner):
        """User removal deletes local access material, never financial history.

        No network while holding the administration SQLite transaction. The
        external consent can additionally be revoked in the provider portal.
        """
        self._drop_import_review(owner)
        if sys.platform != 'linux':
            return
        try:
            # Caller is the administration's validated user-removal transaction.
            # Do not acquire the service lock here: a reader rechecks membership
            # after provider I/O, and SQLite already serializes user removals.
            self._store().delete(owner)
        except Exception:
            _fail('paypal_unavailable', 503)
