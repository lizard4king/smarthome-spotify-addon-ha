"""Household cockpit with optional verified Cloudflare identities and local assets."""
import argparse
import base64
from collections import OrderedDict, deque
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import monthly_review, overview, dashboard
from .core import Plan, Store
from .drive_api import DriveApiError
from .household import normalize_profile, template_profile
from .import_preview import outside_repository
from .person_attribution import person_label
from .planning import decimal_text, export_projection, month_end, parse_plan, projection
from .access_identity import IdentityError, configured_identity
from .administration import Administration, AdministrationError
from .server_banking import ServerBanking
from .server_balance_gateway import ServerBalanceGateway
from .server_balance_jobs import ServerBalanceJobs
from .server_transactions_gateway import ServerTransactionsGateway
from .server_postbank_jobs import ServerPostbankJobs
from .server_bank_refresh import ServerBankRefresh

MAX_REQUEST = 2 * 1024 * 1024
MAX_REJECTED_BODY = 64 * 1024
MAX_REQUEST_WORKERS = 8
REQUEST_SOCKET_TIMEOUT = 5
MIN_ACCESS_TOKEN = 16
ACCESS_COOKIE = 'fc_session'
ACCESS_HEADER = 'X-Finance-Access'
LOGIN_PATH = '/login'
LOGOUT_PATH = '/logout'
SESSION_LIFETIME = 30 * 24 * 3600
MAX_SESSIONS = 32
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW = 15 * 60
MAX_THROTTLE_CLIENTS = 256
LOGOUT_FORM = ('<form method="post" action="/logout" class="logout">'
               '<button type="submit" class="secondary">Abmelden</button></form>')
LOCAL_BIND_HOSTS = {'127.0.0.1', '::1', 'localhost'}
LOGIN_PAGE = (
    '<!doctype html><html lang="de"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>Anmeldung · Finance Control</title>'
    '<link rel="stylesheet" href="/login.css"></head><body>'
    '<main class="login-shell">'
    '<section class="login-intro" aria-labelledby="login-title">'
    '<span class="login-kicker">FINANCE CONTROL</span>'
    '<h1 id="login-title">Dein Finanz-Cockpit.</h1>'
    '<p>Ein geschützter Ort für Deinen Überblick, Deine Planung und Deine Buchungen.</p>'
    '<div class="login-assurance">'
    '<span class="login-assurance-mark" aria-hidden="true">✓</span>'
    '<span>Diese zusätzliche Anmeldung schützt Deine Finanzdaten.</span>'
    '</div></section>'
    '<section class="login-card" aria-labelledby="login-heading">'
    '<span class="login-card-kicker">COCKPIT-ANMELDUNG</span>'
    '<h2 id="login-heading">Willkommen zurück</h2>'
    '<p class="login-lead">Gib Deinen Cockpit-Schlüssel ein, um Finance Control zu öffnen.</p>'
    '@@ERROR@@'
    '<form method="post" action="/login">'
    '<label for="cockpit-key">Cockpit-Schlüssel</label>'
    '<input id="cockpit-key" type="password" name="token" autocomplete="current-password" '
    'aria-describedby="key-help@@ERROR_REF@@" @@INVALID@@autofocus required>'
    '<p id="key-help" class="field-help">Du findest ihn beim ersten Start im Finance-Control-App-Protokoll '
    'in Home Assistant. Notiere ihn dann oder lege ihn in der Add-on-Option access_token fest. Er ist kein Bankpasswort.</p>'
    '<button type="submit">Cockpit öffnen <span aria-hidden="true">→</span></button>'
    '</form></section></main></body></html>'
)


class SessionStore:
    """Random server-side sessions: revocable, expiring, and independent of the access token."""

    def __init__(self, lifetime=SESSION_LIFETIME, limit=MAX_SESSIONS, clock=time.time):
        self.lifetime = lifetime
        self.limit = limit
        self.clock = clock
        self._expiry = {}
        self._lock = threading.Lock()

    def create(self):
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            self._expiry = {key: end for key, end in self._expiry.items() if end > now}
            while len(self._expiry) >= self.limit:
                del self._expiry[min(self._expiry, key=self._expiry.get)]
            self._expiry[session_id] = now + self.lifetime
        return session_id

    def valid(self, session_id):
        with self._lock:
            end = self._expiry.get(session_id)
            if end is None:
                return False
            if end <= self.clock():
                del self._expiry[session_id]
                return False
            return True

    def revoke(self, session_id):
        with self._lock:
            self._expiry.pop(session_id, None)


class FailureThrottle:
    """Bounded sliding-window limit for failed credential checks."""

    def __init__(self, limit=LOGIN_FAILURE_LIMIT, window=LOGIN_FAILURE_WINDOW,
                 clock=time.monotonic, max_clients=MAX_THROTTLE_CLIENTS):
        if limit < 1 or window <= 0 or max_clients < 1:
            raise ValueError('Invalid login throttle settings')
        self.limit = limit
        self.window = window
        self.clock = clock
        self.max_clients = max_clients
        # Ordered by most recent failure; saturated capacity uses one shared bucket.
        self._events = OrderedDict()
        self._overflow = deque()
        self._lock = threading.Lock()

    def _expire(self, now):
        while self._events:
            client, events = next(iter(self._events.items()))
            if now - events[-1] < self.window:
                break
            self._events.popitem(last=False)
        while self._overflow and now - self._overflow[0] >= self.window:
            self._overflow.popleft()

    def _bucket(self, client):
        if client in self._events:
            return self._events[client]
        if len(self._events) >= self.max_clients:
            return self._overflow
        return None

    def retry_after(self, client):
        with self._lock:
            now = self.clock()
            self._expire(now)
            events = self._bucket(client)
            if events is None:
                return None
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) >= self.limit:
                return max(1, int(self.window - (now - events[0]) + 0.999))
            return None

    def record_failure(self, client):
        with self._lock:
            now = self.clock()
            self._expire(now)
            events = self._bucket(client)
            if events is None:
                self._events[client] = deque([now])
                return
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) < self.limit:
                events.append(now)
            if client in self._events:
                self._events.move_to_end(client)

    def reset(self, client):
        with self._lock:
            self._events.pop(client, None)


class BoundedHTTPServer(ThreadingMixIn, HTTPServer):
    """Limit concurrent requests and release idle connections after a timeout."""

    daemon_threads = True
    request_queue_size = MAX_REQUEST_WORKERS

    def __init__(self, server_address, handler_class):
        self._request_slots = threading.BoundedSemaphore(MAX_REQUEST_WORKERS)
        super().__init__(server_address, handler_class)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(REQUEST_SOCKET_TIMEOUT)
        return request, address

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def handle_error(self, request, client_address):
        # Peers may disconnect while a timed-out handler reads or writes.
        # Avoid printing request addresses for these expected socket failures.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)


def default_cutoff():
    return (datetime.now().astimezone().date().replace(day=1) - timedelta(days=1)).isoformat()


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid label')
    return value.strip()


class Cockpit:
    def __init__(self, database, *, demo=False, profile=None, reporting_history=None):
        self.database = outside_repository(database)
        self.demo = demo
        default_profile = (template_profile('household-shared', 'Synthetischer Haushalt')
                           if demo else None)
        raw_profile = profile if profile is not None else default_profile
        self.profile = normalize_profile(raw_profile) if raw_profile is not None else None
        from .reporting_history import validate
        history_path = self.database.parent / 'reporting-history.json'
        if reporting_history is None and not demo and history_path.is_file():
            if history_path.resolve().parent != self.database.parent:
                raise ValueError('Reporting history must remain in the local data directory')
            reporting_history = json.loads(history_path.read_text(encoding='utf-8'))
        self.reporting_history = (validate(reporting_history)
                                  if reporting_history is not None else None)
        self.token = secrets.token_urlsafe(32)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        store = Store(self.database)
        store.close()

    def export_file(self, content, suffix):
        folder = outside_repository(self.database.parent / 'exports')
        if not folder.is_relative_to(self.database.parent):
            raise ValueError('Export directory redirected')
        folder.mkdir(exist_ok=True)
        name = secrets.token_hex(16) + suffix
        with (folder / name).open('x', encoding='utf-8-sig' if suffix == '.csv' else 'utf-8', newline='') as stream:
            stream.write(content)
        return '/exports/' + name

    def state(self, as_of):
        from .backup_package import backup_sync_status
        store = Store(self.database)
        try:
            accounts = store.accounts()
            status = store.status(as_of) if accounts else None
            if self.profile is not None:
                people = self.profile['people']
                labels = {person['id']: person['label'] for person in people}
                accounts = [account | {
                    'owner_label': ('Gemeinsam' if account['owner'] == 'JOINT'
                                    else labels.get(account['owner'], account['owner'])),
                    'share_labels': {labels.get(owner, owner): share
                                     for owner, share in account['shares'].items()},
                } for account in accounts]
            try:
                dashboard_data = dashboard.build(
                    store, as_of, accounts, reporting_history=self.reporting_history)
            except dashboard.DashboardConfigurationError:
                dashboard_data = {
                    'as_of': as_of, 'month': as_of[:7], 'accounts': [],
                    'bookings': [], 'currencies': [],
                    'error': 'Die Übersicht ist nicht verfügbar. Kontenzuordnung der Berichtshistorie prüfen.',
                }
            backup_status = backup_sync_status(self.database)
            result = {'demo': self.demo, 'csrf': self.token, 'as_of': as_of,
                    'accounts': accounts, 'status': status, 'scenarios': store.scenario_records(),
                    'monthly_reviews': monthly_review.records(store),
                    'overview': overview.build(store, as_of, status),
                    'dashboard': dashboard_data,
                    'backup_sync': backup_status,
                    'drive_backup_configured': (backup_status['configured']
                                                and backup_status['error'] is None)}
            if self.profile is not None:
                result['profile_people'] = self.profile['people']
                result['profile_goals'] = self.profile['goals']
            else:
                result['profile_people'] = [
                    {'id': row['id'], 'label': person_label(row['id'])}
                    for row in store.db.execute('SELECT id FROM persons ORDER BY id')
                ]
                result['profile_goals'] = []
            return result
        finally:
            store.close()

    def action(self, route, data):
        store = Store(self.database)
        initial_changes = store.db.total_changes
        try:
            if route == '/api/purchases':
                from .purchases import list_purchases
                return list_purchases(store, data)
            if route.startswith('/api/transfer-correction-'):
                from . import transfer_corrections
                actions = {'suggestions': transfer_corrections.suggestions, 'save': transfer_corrections.save,
                           'single-leg-save': transfer_corrections.save_single_leg,
                           'get': transfer_corrections.get, 'revoke': transfer_corrections.revoke}
                action = route.removeprefix('/api/transfer-correction-')
                if action not in actions:
                    raise ValueError('unknown_transfer_correction_action')
                return actions[action](store, data)
            if route == '/api/approvals':
                from . import approval_queue
                root = outside_repository(self.database.parent / (self.database.stem + '-imports'))
                if not root.is_relative_to(self.database.parent):
                    raise ValueError('Import directory redirected')
                return approval_queue.list_items(store, root, data)
            if route.startswith('/api/payment-policy-'):
                from . import payment_policy
                actions = {'load': payment_policy.load, 'save': payment_policy.save}
                action = route.removeprefix('/api/payment-policy-')
                if action not in actions:
                    raise ValueError('unknown_payment_policy_action')
                return actions[action](store, data)
            if route in {'/api/budget-actual', '/api/budget-actual-details',
                         '/api/budget-actual-metadata'}:
                from . import plan_actual
                if route == '/api/budget-actual-metadata':
                    return plan_actual.metadata(store, reporting_history=self.reporting_history)
                if route == '/api/budget-actual':
                    people = (self.profile['people'] if self.profile is not None else None)
                    return plan_actual.compare_actual(
                        store, data, people=people, reporting_history=self.reporting_history)
                return plan_actual.actual_details(
                    store, data, reporting_history=self.reporting_history)
            if route == '/api/analytics-summary':
                from .analytics import category_summary
                return category_summary(store, data, people=(self.profile['people']
                                                            if self.profile is not None else None))
            if route.startswith('/api/wealth-'):
                from . import wealth
                goals = self.profile['goals'] if self.profile is not None else []
                people = self.profile['people'] if self.profile is not None else None
                if route == '/api/wealth-preview':
                    return wealth.preview(store, data, goals=goals, people=people)
                if route == '/api/wealth-save':
                    return wealth.save(store, data, goals=goals, people=people)
                if route == '/api/wealth-load':
                    return wealth.load(store, data)
                raise ValueError('unknown_wealth_action')
            if route.startswith('/api/budget-'):
                from . import budget
                if route == '/api/budget-export':
                    result = budget.export_snapshot(store, data)
                    return {'download_url': self.export_file(result['csv'], '.csv'),
                            'snapshot_url': self.export_file(json.dumps(result['snapshot'], ensure_ascii=False, indent=2), '.json')}
                actions = {'load': budget.load, 'save': budget.save,
                           'activate': budget.activate, 'calculate': budget.calculate,
                           'compare': budget.compare}
                action = route.removeprefix('/api/budget-')
                if action not in actions:
                    raise ValueError('unknown_budget_action')
                return actions[action](store, data)
            if route.startswith('/api/classification-'):
                from . import classification
                if route == '/api/classification-local-suggestion':
                    from .local_model import suggest
                    return suggest(store, data)
                if route == '/api/classification-model-review':
                    from .local_model import review_transactions
                    return review_transactions(store, [data])
                actions = {
                    'list': classification.list_transactions,
                    'transaction-get': classification.get_transaction,
                    'catalog': classification.category_catalog,
                    'category-create': classification.create_category,
                    'save': classification.save_classification,
                    'save-batch': classification.save_classifications,
                    'model-reject': classification.reject_model_suggestion,
                    'reset': classification.reset_classification,
                    'rule': classification.create_rule,
                    'suggestions': classification.suggestions,
                    'matches': classification.match_suggestions,
                    'document-matches': classification.document_match_suggestions,
                    'document-get': classification.get_document,
                    'documents': classification.list_documents,
                    'document-coverage': classification.document_coverage,
                    'document-create': classification.register_document,
                    'document-refresh': classification.refresh_document_candidate,
                    'document-save': classification.confirm_document,
                    'document-dismiss': classification.dismiss_document,
                    'document-duplicate': classification.mark_document_duplicate,
                    'document-auto-review': classification.auto_confirm_documents,
                    'document-auto-link': classification.auto_link_documents,
                    'confirm-link': classification.confirm_and_link_document,
                    'allocate': classification.allocate_document,
                    'link': classification.link_document,
                    'link-reject': classification.reject_document_link,
                    'unlink': classification.unlink_document,
                    'aggregates': classification.aggregates,
                }
                action = route.removeprefix('/api/classification-')
                if action == 'document-source':
                    from .document_intake import read_document_source
                    if set(data) != {'id'}:
                        raise ValueError('invalid_document_source')
                    return {'text': read_document_source(self.database, data['id'])}
                if action not in actions:
                    raise ValueError('unknown_classification_action')
                # Interactive amounts have a stricter text contract than source
                # adapters and previously stored numerical Decimal strings.
                if (action in {'document-create', 'document-refresh', 'document-save', 'confirm-link'}
                        and data.get('amount') is not None):
                    data = {**data, 'amount': format(decimal_text(data['amount']), '.2f')}
                return actions[action](store, data)
            if route.startswith('/api/intake-'):
                from . import intake_draft
                root = outside_repository(self.database.parent / (self.database.stem + '-imports'))
                if not root.is_relative_to(self.database.parent):
                    raise ValueError('Import directory redirected')
                if route == '/api/intake-load':
                    return intake_draft.load(store, root)
                if route == '/api/intake-save':
                    return intake_draft.save(store, root, data)
                if route == '/api/intake-rows':
                    return intake_draft.rows(store, root, data)
                if route == '/api/intake-activate':
                    household = (self.profile['household_id']
                                 if self.profile is not None else 'HOUSEHOLD')
                    people = ({person['id'] for person in self.profile['people']}
                              if self.profile is not None else {'ANDREAS', 'ERLENE'})
                    return intake_draft.activate(
                        store, root, data, household=household, people=people)
                if route == '/api/intake-resume':
                    from .finanzguru_import import load_batch
                    draft = intake_draft.load(store, root)['draft']
                    if not draft or not draft['activated']:
                        raise ValueError('Confirmed accounts required')
                    _, staged = load_batch(root, draft['batch'])
                    sheets = staged.get('format_report', {}).get('sheets', [])
                    return {'batch': draft['batch'], 'rows': staged['rows'],
                            'source_rows': draft['source_rows'],
                            'ignored_sheets': sum(s['profile'] != 'finanzguru' for s in sheets),
                            'blockers': staged['blockers'], 'issues': [],
                            'split_summary': staged.get('split_summary'),
                            'excluded_split_children': staged.get('excluded_split_children', 0),
                            'mapping': {a['source_account']: a['key'] for a in draft['accounts']}}
            if route == '/api/monthly-preview':
                return monthly_review.preview(store, data['period'])
            if route == '/api/monthly-save':
                return monthly_review.save(store, data['period'], data['review_token'], data.get('confirmed'))
            if route in {'/api/monthly-load', '/api/monthly-export'}:
                if type(data['id']) is not int or data['id'] < 1:
                    raise ValueError('Invalid monthly review ID')
                snapshot = monthly_review.get_review(store, data['id'])
                if route == '/api/monthly-load':
                    return snapshot
                return {'download_url': self.export_file(monthly_review.export_csv(snapshot), '.csv'),
                        'snapshot_url': self.export_file(json.dumps(snapshot, ensure_ascii=False, indent=2), '.json')}
            if route == '/api/backup-package':
                from .backup_package import (
                    create_backup_package,
                    sync_pending_backup_packages,
                )
                package = create_backup_package(store, self.database)
                return {**package, 'sync': sync_pending_backup_packages(self.database)}
            if route == '/api/backup-package-drive':
                from .backup_package import sync_backup_package
                return sync_backup_package(store, self.database)
            if route == '/api/backup-sync':
                from .backup_package import maintain_backup
                return maintain_backup(store, self.database)
            if route in {'/api/fg-stage', '/api/fg-preview', '/api/fg-commit'}:
                from .finanzguru_import import (
                    commit_batch,
                    preview_batch,
                    stage_workbook,
                )
                root = outside_repository(self.database.parent / (self.database.stem + '-imports'))
                if not root.is_relative_to(self.database.parent):
                    raise ValueError('Import directory redirected')
                if route == '/api/fg-stage':
                    try:
                        raw = base64.b64decode(data['content'], validate=True)
                        return stage_workbook(raw, root)
                    except Exception as error:
                        raise ValueError('XLSX intake failed') from error
                if route == '/api/fg-preview':
                    return preview_batch(store, root, data['batch'], data['choices'])
                if data.get('confirmed') is not True:
                    raise ValueError('Explicit import confirmation required')
                return commit_batch(store, root, data['batch'], data['choices'], data['review_token'])
            if route == '/api/bonsy-import':
                from .bonsy_import import import_workbook
                confirm_exclusions = data.get('confirm_exclusions', False)
                if type(confirm_exclusions) is not bool:
                    raise ValueError('invalid_confirm_exclusions')
                root = outside_repository(self.database.parent / (self.database.stem + '-imports'))
                if not root.is_relative_to(self.database.parent):
                    raise ValueError('Import directory redirected')
                root.mkdir(parents=True, exist_ok=True)
                try:
                    raw = base64.b64decode(data['content'], validate=True)
                except Exception as error:
                    raise ValueError('Bonsy XLSX intake failed') from error
                if not 0 < len(raw) <= 32 * 1024 * 1024:
                    raise ValueError('Bonsy XLSX intake failed')
                temporary = root / ('bonsy-' + secrets.token_hex(16) + '.xlsx')
                try:
                    temporary.write_bytes(raw)
                    return import_workbook(store, self.database, temporary,
                                           confirm_exclusions=confirm_exclusions)
                finally:
                    temporary.unlink(missing_ok=True)
            if route == '/api/bonsy-cash':
                from . import bonsy_cash
                return bonsy_cash.overview(store, data)
            if route == '/api/bonsy-cash-preview':
                from . import bonsy_cash
                return bonsy_cash.preview(store, data)
            if route == '/api/bonsy-cash-apply':
                from . import bonsy_cash
                return bonsy_cash.apply(store, data)
            if route == '/api/bonsy-cash-remove':
                from . import bonsy_cash
                return bonsy_cash.remove_allocations(store, data)
            if route == '/api/bonsy-voucher-payment':
                from .bonsy_vouchers import set_payment
                return set_payment(store, data)
            if route == '/api/accounts':
                existing = store.accounts()
                opening_date = date.fromisoformat(data['opening_date']).isoformat()
                if existing and any(a['opening_date'] != opening_date for a in existing):
                    raise ValueError('Common opening date required')
                owner = data['owner']
                if not isinstance(owner, str):
                    raise ValueError('Invalid account owner')
                known_people = ({person['id'] for person in self.profile['people']}
                                if self.profile is not None else {'ANDREAS', 'ERLENE'})
                supplied_shares = data.get('shares')
                if owner == 'JOINT':
                    if supplied_shares is None and 'andreas_percent' in data:
                        # Preserve the old two-person request while old clients are in use.
                        if known_people != {'ANDREAS', 'ERLENE'}:
                            raise ValueError('Explicit shares are required for a shared account')
                        share = decimal_text(data['andreas_percent']) / Decimal(100)
                        if not 0 < share < 1:
                            raise ValueError('Both shares must be positive')
                        supplied_shares = {'ANDREAS': str(share), 'ERLENE': str(1 - share)}
                    if not isinstance(supplied_shares, dict) or len(supplied_shares) < 2:
                        raise ValueError('A shared account requires at least two owners')
                    if any(person not in known_people for person in supplied_shares):
                        raise ValueError('Unknown account owner')
                    try:
                        weights = {person: Decimal(str(value))
                                   for person, value in supplied_shares.items()}
                    except (InvalidOperation, TypeError, ValueError) as error:
                        raise ValueError('Invalid ownership shares') from error
                    if (any(not weight.is_finite() or weight <= 0 for weight in weights.values())
                            or sum(weights.values(), Decimal(0)) != Decimal(1)):
                        raise ValueError('Ownership shares must be positive and sum to one')
                    shares = {person: str(value) for person, value in weights.items()}
                else:
                    if owner not in known_people:
                        raise ValueError('Owner is not available in this profile')
                    if supplied_shares is not None:
                        if not isinstance(supplied_shares, dict):
                            raise ValueError('Invalid ownership shares')
                        if any(person not in known_people for person in supplied_shares):
                            raise ValueError('Unknown account owner')
                        try:
                            sole_shares = {person: Decimal(str(value))
                                           for person, value in supplied_shares.items()}
                        except (InvalidOperation, TypeError, ValueError) as error:
                            raise ValueError('Invalid ownership shares') from error
                        if sole_shares != {owner: Decimal(1)}:
                            raise ValueError('A personal account must belong wholly to its owner')
                    shares = {owner: '1'}
                store.add_account(label(data['id']), owner, shares, decimal_text(data['opening']),
                                  opening_date, institution=label(data['institution']), kind=data['kind'],
                                  display_name=(label(data['display_name']) if data.get('display_name') else None),
                                  household=(self.profile['household_id'] if self.profile is not None
                                             else 'HOUSEHOLD'))
                return {'saved': True}
            if route == '/api/account-display-name':
                if not isinstance(data, dict) or set(data) != {'id', 'display_name', 'revision', 'confirmed'}:
                    raise ValueError('Invalid account display name request')
                if data['confirmed'] is not True:
                    raise ValueError('Explicit confirmation is required')
                return {'account': store.set_account_display_name(
                    data['id'], data['display_name'], data['revision'])}
            if route == '/api/import':
                source = data['csv']
                if not isinstance(source, str) or len(source) > MAX_REQUEST:
                    raise ValueError('Invalid CSV')
                return {'inserted': store.import_csv(source.removeprefix('\ufeff'))}
            if route in {'/api/forecast', '/api/save', '/api/export'}:
                if not store.accounts():
                    raise ValueError('At least one account required')
                cutoff = month_end(data['as_of']).isoformat()
                status = store.status(cutoff)
                plan = parse_plan(data['plan'])
                result = {'as_of': cutoff, 'opening': status['liquidity'],
                          'rows': projection(status['liquidity'], cutoff, plan)}
                if route == '/api/save':
                    store.save_scenario(label(data['name']), plan, status['liquidity'], cutoff)
                    result['saved'] = True
                elif route == '/api/export':
                    result['csv'] = export_projection(result['rows'])
                    result['download_url'] = self.export_file(result['csv'], '.csv')
                return result
            if route == '/api/compare':
                first, second = label(data['first']), label(data['second'])
                return {'rows': store.compare_scenarios(first, second)}
            if route == '/api/export-scenario':
                name = label(data['name'])
                saved = next(r for r in store.scenario_records() if r['name'] == name)
                plan = Plan(saved['income'], saved['expenses'], saved['reserve'], saved['one_offs'])
                rows = projection(saved['opening'], saved['as_of'], plan)
                csv_text = export_projection(rows)
                return {'csv': csv_text, 'snapshot': saved,
                        'download_url': self.export_file(csv_text, '.csv'),
                        'snapshot_url': self.export_file(json.dumps(saved, indent=2), '.json')}
            if route == '/api/backup':
                folder = self.database.parent / 'backups'
                if not folder.resolve().is_relative_to(self.database.parent):
                    raise ValueError('Backup directory redirected')
                folder.mkdir(exist_ok=True)
                filename = 'finance-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ-') + secrets.token_hex(4) + '.sqlite'
                store.backup(folder / filename)
                return {'saved': True, 'filename': filename, 'integrity': 'ok', 'cloud_upload': False}
            raise ValueError('Unknown action')
        finally:
            committed_change = store.db.total_changes != initial_changes and not store.db.in_transaction
            store.close()
            if committed_change:
                from .backup_package import mark_backup_needed
                try:
                    mark_backup_needed(self.database)
                except (OSError, ValueError):
                    pass


def _normalized_http_host(value, default_port):
    candidate = value.strip().lower()
    if candidate.count(':') >= 2 and not candidate.startswith('['):
        candidate = f'[{candidate}]'
    try:
        parsed = urlsplit('//' + candidate)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError('Ungültiger Hostname') from exc
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment):
        raise ValueError('Ungültiger Hostname')
    hostname = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    return f'{hostname}:{parsed_port if parsed_port is not None else default_port}'


def _normalized_origin(value):
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('Ungültiger Origin') from exc
    if (parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.path
            or parsed.query or parsed.fragment or '?' in value or '#' in value or '*' in value):
        raise ValueError('Ungültiger Origin')
    try:
        origin_host = _normalized_http_host(parsed.netloc, 443 if parsed.scheme == 'https' else 80)
    except ValueError as exc:
        raise ValueError('Ungültiger Origin') from exc
    # Compare origins using URL semantics: host names are case-insensitive and
    # omitted ports equal the protocol's default port.
    return f'{parsed.scheme}://{origin_host}'


def _server_administration(database):
    """Wire vault cleanup before exposing any administration HTTP handler."""
    administration = Administration(database)
    server_banking = ServerBanking(administration)
    administration.before_bank_revoke = server_banking.cleanup_connections
    return administration, server_banking


def make_server(app, port=8785, allowed_hosts=None, host='127.0.0.1', allowed_origins=None,
                access_token=None, identity_verifier=None, bootstrap_emails=(), bank_product_id=None):
    """Create the cockpit server.

    ``access_token=None`` keeps the historic behaviour (Host/Origin/CSRF checks only),
    suitable for loopback use or a front-end that already authenticates users.
    A token of at least ``MIN_ACCESS_TOKEN`` characters enables application-level
    authentication for every route except ``/health``.
    """
    if access_token is not None and len(access_token) < MIN_ACCESS_TOKEN:
        raise ValueError('Zugriffstoken zu kurz.')
    sessions = SessionStore()
    failures = FailureThrottle()
    dispatch_lock = threading.Lock()
    if identity_verifier is not None:
        administration, server_banking = _server_administration(app.database)
    else:
        administration = server_banking = None
    bank_jobs = (ServerBalanceJobs(ServerBalanceGateway(administration, bank_product_id))
                 if administration is not None else None)
    postbank_jobs = (ServerPostbankJobs(
        ServerTransactionsGateway(administration, bank_product_id), app.database)
                    if administration is not None else None)
    bank_refresh = (ServerBankRefresh(postbank_jobs, administration, app.database)
                    if administration is not None else None)
    trusted_hosts = {f'127.0.0.1:{port}'}
    auto_port_hosts = set(trusted_hosts)
    for allowed_host in allowed_hosts or []:
        trusted = _normalized_http_host(allowed_host, port)
        trusted_hosts.add(trusted)
        if trusted.endswith(f':{port}'):
            auto_port_hosts.add(trusted)
    trusted_origins = {_normalized_origin(origin) for origin in (allowed_origins or [])}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No account names, source content, query strings or tokens in logs.

        def reply(self, status, value, kind='application/json; charset=utf-8'):
            body = json.dumps(value, default=str, ensure_ascii=False).encode() if isinstance(value, dict) else value
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def client_key(self):
            # Only the socket peer is trusted. Users behind one proxy share its limit.
            return self.client_address[0]

        def session_id(self):
            for part in self.headers.get('Cookie', '').split(';'):
                name, _, value = part.strip().partition('=')
                if name == ACCESS_COOKIE and value:
                    return value
            return ''

        def authorized(self):
            if access_token is None:
                return True
            candidates = [self.headers.get(ACCESS_HEADER, '')]
            authorization = self.headers.get('Authorization', '')
            if authorization.startswith('Bearer '):
                candidates.append(authorization[7:].strip())
            candidates = [candidate for candidate in candidates if candidate]
            if candidates:
                client = self.client_key()
                if failures.retry_after(client) is not None:
                    return False
                if any(secrets.compare_digest(candidate.encode(), access_token.encode())
                       for candidate in candidates):
                    return True
                failures.record_failure(client)
                return False
            return sessions.valid(self.session_id())

        def require_identity(self, *, discard_body=False, recheck=False):
            self.management_user = None
            if identity_verifier is None:
                return True
            try:
                if recheck:
                    # The signature was verified before waiting for the dispatch lock.
                    # Recheck only local membership so a removed user cannot proceed.
                    identity = getattr(self, 'verified_identity', None)
                else:
                    identity = identity_verifier.verify(self.headers.get('Cf-Access-Jwt-Assertion', ''))
                    self.verified_identity = identity
                self.management_user = administration.resolve_identity(identity, bootstrap_emails)
                return True
            except (IdentityError, AdministrationError) as error:
                if discard_body:
                    self.discard_rejected_body()
                self.management_error(error)
                return False
            except sqlite3.Error:
                # Identity checks run before route handling, including static GETs.
                # A busy private store must fail closed with an HTTP response.
                if discard_body:
                    self.discard_rejected_body()
                self.management_error(AdministrationError('identity_store_unavailable', 503))
                return False

        def management_error(self, error):
            messages = {
                'identity_required': 'Deine Cloudflare-Anmeldung fehlt oder ist abgelaufen. Bitte erneut anmelden.',
                'identity_provider_unavailable': 'Cloudflare-Anmeldung kann derzeit nicht geprüft werden. Bitte später erneut versuchen.',
                'identity_store_unavailable': 'Deine Benutzerfreigabe kann derzeit nicht geprüft werden. Bitte später erneut versuchen.',
                'invalid_identity': 'Die angemeldete Benutzeridentität ist ungültig.',
                'unknown_identity': 'Deine Anmeldung ist noch nicht für dieses Cockpit freigegeben.',
                'inactive_user': 'Dieses Benutzerkonto wurde entfernt. Bitte den Administrator kontaktieren.',
                'identity_conflict': 'Die Anmeldung stimmt nicht mit der gespeicherten Benutzerzuordnung überein.',
                'unauthorized': 'Deine Anmeldung ist nicht mehr freigegeben. Bitte erneut anmelden.',
                'forbidden': 'Du darfst diese Änderung nicht vornehmen.',
                'last_admin': 'Der letzte aktive Administrator kann nicht entfernt werden.',
                'duplicate_email': 'Diese E-Mail-Adresse ist bereits registriert.',
                'stale_revision': 'Der Eintrag wurde inzwischen geändert. Bitte neu laden und erneut bestätigen.',
                'confirmation_required': 'Bitte bestätige die Änderung ausdrücklich.',
                'unknown_user': 'Dieses Benutzerkonto ist nicht mehr aktiv.',
                'unknown_connection': 'Dieser Bankzugang ist nicht mehr eingetragen.',
                'administration_disabled': 'Für die Verwaltung muss die persönliche Cloudflare-Anmeldung eingerichtet sein.',
                'server_banking_unsupported': 'Serverzugänge werden nur auf der Linux-Serverinstallation unterstützt.',
                'server_banking_unavailable': 'Der sichere Serverzugang ist derzeit nicht verfügbar. Bitte den gespeicherten Stand prüfen.',
                'invalid_bank_credentials': 'Bankkennung oder Passwort ist leer oder zu lang. Bitte lokal in dieser Maske neu eingeben.',
                'bank_read_busy': 'Ein Bankabruf läuft bereits. Bitte dessen Ergebnis abwarten.',
                'bank_product_unavailable': 'Die FinTS-Produktkennung fehlt in der Servereinrichtung.',
                'unknown_bank_job': 'Dieser Bankabruf ist nicht mehr verfügbar.',
            }
            return self.reply(error.status, {'error': messages.get(error.code, 'Eingaben prüfen und erneut bestätigen.'),
                                              'code': error.code})

        def login_page(self, status, error=False):
            page = (LOGIN_PAGE.replace('@@ERROR@@',
                    '<p id="login-error" class="login-error" role="alert">'
                    'Cockpit-Schlüssel ungültig. Bitte prüfe Deine Eingabe.</p>' if error else '')
                    .replace('@@ERROR_REF@@', ' login-error' if error else '')
                    .replace('@@INVALID@@', 'aria-invalid="true" ' if error else ''))
            return self.reply(status, page.encode(), 'text/html; charset=utf-8')

        def login_required(self):
            if urlsplit(self.path).path.startswith('/api/'):
                return self.reply(401, {'error': 'Anmeldung erforderlich.'})
            return self.login_page(401)

        def handle_login(self):
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                size = -1
            if not 0 < size <= 2048:
                self.discard_rejected_body()
                return self.reply(400, {'error': 'Anfrage ungültig.'})
            form = parse_qs(self.rfile.read(size).decode('utf-8', 'replace'))
            supplied = form.get('token', [''])[0]
            client = self.client_key()
            wait = failures.retry_after(client)
            if wait is not None:
                return self.reply_throttled(wait)
            if access_token is None or not secrets.compare_digest(supplied.encode(), access_token.encode()):
                failures.record_failure(client)
                return self.login_page(401, error=True)
            failures.reset(client)
            cookie = (f'{ACCESS_COOKIE}={sessions.create()}; Path=/; HttpOnly; SameSite=Lax; '
                      f'Max-Age={SESSION_LIFETIME}')
            if self.secure_request():
                cookie += '; Secure'
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', cookie)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def secure_request(self):
            return (self.headers.get('X-Forwarded-Proto', '').casefold() == 'https'
                    or self.headers.get('Origin', '').startswith('https://'))

        def reply_throttled(self, wait):
            body = json.dumps({'error': 'Zu viele fehlgeschlagene Anmeldungen. Bitte später erneut versuchen.'}).encode()
            self.send_response(429)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Retry-After', str(wait))
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def handle_logout(self):
            self.discard_rejected_body()
            sessions.revoke(self.session_id())
            cookie = f'{ACCESS_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'
            if self.secure_request():
                cookie += '; Secure'
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', cookie)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def trusted_host(self):
            try:
                host = _normalized_http_host(self.headers.get('Host', ''), self.server.server_port)
            except ValueError:
                return False
            return host in trusted_hosts

        def trusted_origin(self):
            origin = self.headers.get('Origin', '')
            try:
                parsed = urlsplit(origin)
            except ValueError:
                return False
            if parsed.scheme == 'https':
                try:
                    normalized = _normalized_origin(origin)
                    origin_host = _normalized_http_host(parsed.netloc, 443)
                except ValueError:
                    return False
                trusted_hostnames = {host.rsplit(':', 1)[0].strip('[]') for host in trusted_hosts}
                return normalized in trusted_origins and origin_host.rsplit(':', 1)[0].strip('[]') in trusted_hostnames
            try:
                host = _normalized_http_host(parsed.netloc, self.server.server_port)
            except ValueError:
                return False
            return (parsed.scheme == 'http' and host in trusted_hosts and not parsed.path
                    and not parsed.query and not parsed.fragment)

        def discard_rejected_body(self):
            """Drain small rejected requests so Windows can deliver the 403 reliably."""
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return
            if 0 < size <= MAX_REJECTED_BODY:
                previous_timeout = self.connection.gettimeout()
                deadline = time.monotonic() + 0.5
                try:
                    while size > 0:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self.connection.settimeout(remaining)
                        chunk = self.rfile.read1(size)
                        if not chunk:
                            break
                        size -= len(chunk)
                except OSError:
                    pass
                finally:
                    self.connection.settimeout(previous_timeout)

        def do_GET(self):
            if urlsplit(self.path).path == '/health':
                return self.reply(200, {'status': 'ok'})
            if not self.trusted_host():
                return self.reply(403, {'error': 'Lokaler Zugriff erforderlich.'})
            if urlsplit(self.path).path == '/login.css':
                return self.reply(200, files('finance_control').joinpath('static', 'login.css').read_bytes(),
                                  'text/css; charset=utf-8')
            if not self.authorized():
                return self.login_required()
            if not self.require_identity():
                return
            url = urlsplit(self.path)
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/dashboard.js': ('dashboard.js', 'text/javascript; charset=utf-8'),
                      '/dashboard.css': ('dashboard.css', 'text/css; charset=utf-8'),
                      '/administration.js': ('administration.js', 'text/javascript; charset=utf-8'),
                      '/administration.css': ('administration.css', 'text/css; charset=utf-8'),
                      '/server-banking.js': ('server-banking.js', 'text/javascript; charset=utf-8'),
                      '/server-banking.css': ('server-banking.css', 'text/css; charset=utf-8'),
                      '/postbank-transactions.js': ('postbank-transactions.js', 'text/javascript; charset=utf-8'),
                      '/postbank-transactions.css': ('postbank-transactions.css', 'text/css; charset=utf-8'),
                      '/bank-refresh.js': ('bank-refresh.js', 'text/javascript; charset=utf-8'),
                      '/bank-refresh.css': ('bank-refresh.css', 'text/css; charset=utf-8'),
                      '/brand-adobe.ico': ('brand-adobe.ico', 'image/x-icon'),
                      '/brand-aramark.png': ('brand-aramark.png', 'image/png'),
                      '/brand-arbeitsagentur.png': ('brand-arbeitsagentur.png', 'image/png'),
                      '/brand-babbel.svg': ('brand-babbel.svg', 'image/svg+xml'),
                      '/brand-bauhaus.jpg': ('brand-bauhaus.jpg', 'image/jpeg'),
                      '/brand-beitragsservice.ico': ('brand-beitragsservice.ico', 'image/x-icon'),
                      '/brand-bolt.svg': ('brand-bolt.svg', 'image/svg+xml'),
                      '/brand-c-and-a.svg': ('brand-c-and-a.svg', 'image/svg+xml'),
                      '/brand-cewe.png': ('brand-cewe.png', 'image/png'),
                      '/brand-check24.png': ('brand-check24.png', 'image/png'),
                      '/brand-decathlon.ico': ('brand-decathlon.ico', 'image/x-icon'),
                      '/brand-disneyplus.png': ('brand-disneyplus.png', 'image/png'),
                      '/brand-douglas.svg': ('brand-douglas.svg', 'image/svg+xml'),
                      '/brand-enbw.ico': ('brand-enbw.ico', 'image/x-icon'),
                      '/brand-eventim.ico': ('brand-eventim.ico', 'image/x-icon'),
                      '/brand-fressnapf.png': ('brand-fressnapf.png', 'image/png'),
                      '/brand-galeria.ico': ('brand-galeria.ico', 'image/x-icon'),
                      '/brand-holiday-inn.png': ('brand-holiday-inn.png', 'image/png'),
                      '/brand-home24.ico': ('brand-home24.ico', 'image/x-icon'),
                      '/brand-immoscout24.png': ('brand-immoscout24.png', 'image/png'),
                      '/brand-intersport.svg': ('brand-intersport.svg', 'image/svg+xml'),
                      '/brand-lightricks.png': ('brand-lightricks.png', 'image/png'),
                      '/brand-lime.webp': ('brand-lime.webp', 'image/webp'),
                      '/brand-local-aartal-apotheke.png': ('brand-local-aartal-apotheke.png', 'image/png'),
                      '/brand-local-apotheke-werkstadt.jpg': ('brand-local-apotheke-werkstadt.jpg', 'image/jpeg'),
                      '/brand-local-baerentreff.svg': ('brand-local-baerentreff.svg', 'image/svg+xml'),
                      '/brand-local-bereket-center.png': ('brand-local-bereket-center.png', 'image/png'),
                      '/brand-local-dorea.svg': ('brand-local-dorea.svg', 'image/svg+xml'),
                      '/brand-local-moos.png': ('brand-local-moos.png', 'image/png'),
                      '/brand-local-soonwald.png': ('brand-local-soonwald.png', 'image/png'),
                      '/brand-local-unica-doener.png': ('brand-local-unica-doener.png', 'image/png'),
                      '/brand-microsoft.ico': ('brand-microsoft.ico', 'image/x-icon'),
                      '/brand-netto-marken-discount.webp': ('brand-netto-marken-discount.webp', 'image/webp'),
                      '/brand-openai.svg': ('brand-openai.svg', 'image/svg+xml'),
                      '/brand-shop-apotheke.png': ('brand-shop-apotheke.png', 'image/png'),
                      '/brand-temu.ico': ('brand-temu.ico', 'image/x-icon'),
                      '/brand-thalia.png': ('brand-thalia.png', 'image/png'),
                      '/brand-thomann.png': ('brand-thomann.png', 'image/png'),
                      '/brand-totalenergies.ico': ('brand-totalenergies.ico', 'image/x-icon'),
                      '/brand-zattoo.svg': ('brand-zattoo.svg', 'image/svg+xml'),
                      '/bank-postbank.svg': ('bank-postbank.svg', 'image/svg+xml'),
                      '/bank-sparkasse.png': ('bank-sparkasse.png', 'image/png'),
                      '/bank-ing.svg': ('bank-ing.svg', 'image/svg+xml'),
                      '/bank-paypal.png': ('bank-paypal.png', 'image/png'),
                      '/lucide-LICENSE.txt': ('lucide-LICENSE.txt', 'text/plain; charset=utf-8'),
                      '/counterparties.js': ('counterparties.js', 'text/javascript; charset=utf-8'),
                      '/counterparties.css': ('counterparties.css', 'text/css; charset=utf-8'),
                      '/purchases.js': ('purchases.js', 'text/javascript; charset=utf-8'),
                      '/purchases.css': ('purchases.css', 'text/css; charset=utf-8'),
                      '/brand-aldi-sued.ico': ('brand-aldi-sued.ico', 'image/x-icon'),
                      '/brand-amazon.ico': ('brand-amazon.ico', 'image/x-icon'),
                      '/brand-dm.png': ('brand-dm.png', 'image/png'),
                      '/brand-google.ico': ('brand-google.ico', 'image/x-icon'),
                      '/brand-kaufland.png': ('brand-kaufland.png', 'image/png'),
                      '/brand-lidl.svg': ('brand-lidl.svg', 'image/svg+xml'),
                      '/brand-mediamarkt.png': ('brand-mediamarkt.png', 'image/png'),
                      '/brand-obi.png': ('brand-obi.png', 'image/png'),
                      '/brand-rewe.ico': ('brand-rewe.ico', 'image/x-icon'),
                      '/simple-icons-LICENSE.txt': ('simple-icons-LICENSE.txt', 'text/plain; charset=utf-8'),
                      '/brand-edeka.svg': ('brand-edeka.svg', 'image/svg+xml'),
                      '/brand-penny.svg': ('brand-penny.svg', 'image/svg+xml'),
                      '/brand-rossmann.svg': ('brand-rossmann.svg', 'image/svg+xml'),
                      '/brand-muller.svg': ('brand-muller.svg', 'image/svg+xml'),
                      '/brand-ikea.svg': ('brand-ikea.svg', 'image/svg+xml'),
                      '/brand-otto.svg': ('brand-otto.svg', 'image/svg+xml'),
                      '/brand-zalando.svg': ('brand-zalando.svg', 'image/svg+xml'),
                      '/brand-ebay.svg': ('brand-ebay.svg', 'image/svg+xml'),
                      '/brand-saturn.svg': ('brand-saturn.svg', 'image/svg+xml'),
                      '/brand-dhl.svg': ('brand-dhl.svg', 'image/svg+xml'),
                      '/brand-hermes.svg': ('brand-hermes.svg', 'image/svg+xml'),
                      '/brand-fedex.svg': ('brand-fedex.svg', 'image/svg+xml'),
                      '/brand-ups.svg': ('brand-ups.svg', 'image/svg+xml'),
                      '/brand-deutschebahn.svg': ('brand-deutschebahn.svg', 'image/svg+xml'),
                      '/brand-lufthansa.svg': ('brand-lufthansa.svg', 'image/svg+xml'),
                      '/brand-ryanair.svg': ('brand-ryanair.svg', 'image/svg+xml'),
                      '/brand-easyjet.svg': ('brand-easyjet.svg', 'image/svg+xml'),
                      '/brand-bookingdotcom.svg': ('brand-bookingdotcom.svg', 'image/svg+xml'),
                      '/brand-airbnb.svg': ('brand-airbnb.svg', 'image/svg+xml'),
                      '/brand-uber.svg': ('brand-uber.svg', 'image/svg+xml'),
                      '/brand-ubereats.svg': ('brand-ubereats.svg', 'image/svg+xml'),
                      '/brand-vodafone.svg': ('brand-vodafone.svg', 'image/svg+xml'),
                      '/brand-o2.svg': ('brand-o2.svg', 'image/svg+xml'),
                      '/brand-netflix.svg': ('brand-netflix.svg', 'image/svg+xml'),
                      '/brand-spotify.svg': ('brand-spotify.svg', 'image/svg+xml'),
                      '/brand-youtube.svg': ('brand-youtube.svg', 'image/svg+xml'),
                      '/brand-playstation.svg': ('brand-playstation.svg', 'image/svg+xml'),
                      '/brand-steam.svg': ('brand-steam.svg', 'image/svg+xml'),
                      '/brand-apple.svg': ('brand-apple.svg', 'image/svg+xml'),
                      '/brand-dropbox.svg': ('brand-dropbox.svg', 'image/svg+xml'),
                      '/brand-github.svg': ('brand-github.svg', 'image/svg+xml'),
                      '/brand-mcdonalds.svg': ('brand-mcdonalds.svg', 'image/svg+xml'),
                      '/brand-burgerking.svg': ('brand-burgerking.svg', 'image/svg+xml'),
                      '/brand-kfc.svg': ('brand-kfc.svg', 'image/svg+xml'),
                      '/brand-starbucks.svg': ('brand-starbucks.svg', 'image/svg+xml'),
                      '/brand-aral.svg': ('brand-aral.svg', 'image/svg+xml'),
                      '/brand-shell.svg': ('brand-shell.svg', 'image/svg+xml'),
                      '/brand-bmw.svg': ('brand-bmw.svg', 'image/svg+xml'),
                      '/brand-volkswagen.svg': ('brand-volkswagen.svg', 'image/svg+xml'),
                      '/brand-tesla.svg': ('brand-tesla.svg', 'image/svg+xml'),
                      '/brand-n26.svg': ('brand-n26.svg', 'image/svg+xml'),
                      '/brand-commerzbank.svg': ('brand-commerzbank.svg', 'image/svg+xml'),
                      '/brand-accor.ico': ('brand-accor.ico', 'image/x-icon'),
                      '/brand-adidas.svg': ('brand-adidas.svg', 'image/svg+xml'),
                      '/brand-adyen.svg': ('brand-adyen.svg', 'image/svg+xml'),
                      '/brand-alternate.png': ('brand-alternate.png', 'image/png'),
                      '/brand-amedes.svg': ('brand-amedes.svg', 'image/svg+xml'),
                      '/brand-arag.svg': ('brand-arag.svg', 'image/svg+xml'),
                      '/brand-audible.svg': ('brand-audible.svg', 'image/svg+xml'),
                      '/brand-avast.svg': ('brand-avast.svg', 'image/svg+xml'),
                      '/brand-avia.ico': ('brand-avia.ico', 'image/x-icon'),
                      '/brand-bett1.ico': ('brand-bett1.ico', 'image/x-icon'),
                      '/brand-bhw.svg': ('brand-bhw.svg', 'image/svg+xml'),
                      '/brand-blume2000.ico': ('brand-blume2000.ico', 'image/x-icon'),
                      '/brand-bnp-paribas.png': ('brand-bnp-paribas.png', 'image/png'),
                      '/brand-boc.jpg': ('brand-boc.jpg', 'image/jpeg'),
                      '/brand-buhl.svg': ('brand-buhl.svg', 'image/svg+xml'),
                      '/brand-cardif.png': ('brand-cardif.png', 'image/png'),
                      '/brand-chin-thai-limburg.png': ('brand-chin-thai-limburg.png', 'image/png'),
                      '/brand-cinemaxx.png': ('brand-cinemaxx.png', 'image/png'),
                      '/brand-cineplex.svg': ('brand-cineplex.svg', 'image/svg+xml'),
                      '/brand-congstar.png': ('brand-congstar.png', 'image/png'),
                      '/brand-consors-finanz.png': ('brand-consors-finanz.png', 'image/png'),
                      '/brand-contipark.png': ('brand-contipark.png', 'image/png'),
                      '/brand-dak.ico': ('brand-dak.ico', 'image/x-icon'),
                      '/brand-debeka.svg': ('brand-debeka.svg', 'image/svg+xml'),
                      '/brand-dehner.png': ('brand-dehner.png', 'image/png'),
                      '/brand-deutsche-bank.svg': ('brand-deutsche-bank.svg', 'image/svg+xml'),
                      '/brand-deutschepost.svg': ('brand-deutschepost.svg', 'image/svg+xml'),
                      '/brand-devk.png': ('brand-devk.png', 'image/png'),
                      '/brand-dkv.png': ('brand-dkv.png', 'image/png'),
                      '/brand-drillisch.ico': ('brand-drillisch.ico', 'image/x-icon'),
                      '/brand-easypark.png': ('brand-easypark.png', 'image/png'),
                      '/brand-eni.ico': ('brand-eni.ico', 'image/x-icon'),
                      '/brand-ergo.ico': ('brand-ergo.ico', 'image/x-icon'),
                      '/brand-ernstings-family.png': ('brand-ernstings-family.png', 'image/png'),
                      '/brand-esso.ico': ('brand-esso.ico', 'image/x-icon'),
                      '/brand-expedia.svg': ('brand-expedia.svg', 'image/svg+xml'),
                      '/brand-fleurop.svg': ('brand-fleurop.svg', 'image/svg+xml'),
                      '/brand-floraprima.png': ('brand-floraprima.png', 'image/png'),
                      '/brand-fraport.png': ('brand-fraport.png', 'image/png'),
                      '/brand-globus.svg': ('brand-globus.svg', 'image/svg+xml'),
                      '/brand-gmx.ico': ('brand-gmx.ico', 'image/x-icon'),
                      '/brand-handm.svg': ('brand-handm.svg', 'image/svg+xml'),
                      '/brand-hannoversche.png': ('brand-hannoversche.png', 'image/png'),
                      '/brand-heide-park.png': ('brand-heide-park.png', 'image/png'),
                      '/brand-hem.png': ('brand-hem.png', 'image/png'),
                      '/brand-hilton.svg': ('brand-hilton.svg', 'image/svg+xml'),
                      '/brand-hiltonhotelsandresorts.svg': ('brand-hiltonhotelsandresorts.svg', 'image/svg+xml'),
                      '/brand-hp.svg': ('brand-hp.svg', 'image/svg+xml'),
                      '/brand-huk-coburg.ico': ('brand-huk-coburg.ico', 'image/x-icon'),
                      '/brand-huk24.ico': ('brand-huk24.ico', 'image/x-icon'),
                      '/brand-jamara.png': ('brand-jamara.png', 'image/png'),
                      '/brand-jet.png': ('brand-jet.png', 'image/png'),
                      '/brand-jetbrains.svg': ('brand-jetbrains.svg', 'image/svg+xml'),
                      '/brand-jobrad.ico': ('brand-jobrad.ico', 'image/x-icon'),
                      '/brand-klarmobil.ico': ('brand-klarmobil.ico', 'image/x-icon'),
                      '/brand-klarna.ico': ('brand-klarna.ico', 'image/x-icon'),
                      '/brand-kleinanzeigen.svg': ('brand-kleinanzeigen.svg', 'image/svg+xml'),
                      '/brand-local-2theloo.png': ('brand-local-2theloo.png', 'image/png'),
                      '/brand-local-alex.svg': ('brand-local-alex.svg', 'image/svg+xml'),
                      '/brand-local-auto-bach.png': ('brand-local-auto-bach.png', 'image/png'),
                      '/brand-local-bad-kreuznach-stadtwerke.png': ('brand-local-bad-kreuznach-stadtwerke.png', 'image/png'),
                      '/brand-local-bolero.ico': ('brand-local-bolero.ico', 'image/x-icon'),
                      '/brand-local-bowlhouse-limburg.png': ('brand-local-bowlhouse-limburg.png', 'image/png'),
                      '/brand-local-deutsche-kautionskasse.png': ('brand-local-deutsche-kautionskasse.png', 'image/png'),
                      '/brand-local-dpv.png': ('brand-local-dpv.png', 'image/png'),
                      '/brand-local-emser-therme.png': ('brand-local-emser-therme.png', 'image/png'),
                      '/brand-local-europabad.svg': ('brand-local-europabad.svg', 'image/svg+xml'),
                      '/brand-local-extrablatt.png': ('brand-local-extrablatt.png', 'image/png'),
                      '/brand-local-fitseveneleven.jpg': ('brand-local-fitseveneleven.jpg', 'image/jpeg'),
                      '/brand-local-goettingen-stadtwerke.png': ('brand-local-goettingen-stadtwerke.png', 'image/png'),
                      '/brand-local-grobe.png': ('brand-local-grobe.png', 'image/png'),
                      '/brand-local-gym100-limburg.svg': ('brand-local-gym100-limburg.svg', 'image/svg+xml'),
                      '/brand-local-habakuk.png': ('brand-local-habakuk.png', 'image/png'),
                      '/brand-local-hinnerbaecker.png': ('brand-local-hinnerbaecker.png', 'image/png'),
                      '/brand-local-imo.png': ('brand-local-imo.png', 'image/png'),
                      '/brand-local-jumpnfun-arena.png': ('brand-local-jumpnfun-arena.png', 'image/png'),
                      '/brand-local-lohners.png': ('brand-local-lohners.png', 'image/png'),
                      '/brand-local-minera.svg': ('brand-local-minera.svg', 'image/svg+xml'),
                      '/brand-local-muehlenbaeckerei.png': ('brand-local-muehlenbaeckerei.png', 'image/png'),
                      '/brand-local-myers.jpg': ('brand-local-myers.jpg', 'image/jpeg'),
                      '/brand-local-neusehland.svg': ('brand-local-neusehland.svg', 'image/svg+xml'),
                      '/brand-local-phaeno.png': ('brand-local-phaeno.png', 'image/png'),
                      '/brand-local-phantasialand.png': ('brand-local-phantasialand.png', 'image/png'),
                      '/brand-local-photolini.png': ('brand-local-photolini.png', 'image/png'),
                      '/brand-local-rhein-main-therme.svg': ('brand-local-rhein-main-therme.svg', 'image/svg+xml'),
                      '/brand-local-ruch.png': ('brand-local-ruch.png', 'image/png'),
                      '/brand-local-ruedesheim-seilbahn.png': ('brand-local-ruedesheim-seilbahn.png', 'image/png'),
                      '/brand-local-serways.png': ('brand-local-serways.png', 'image/png'),
                      '/brand-local-ssp.svg': ('brand-local-ssp.svg', 'image/svg+xml'),
                      '/brand-local-tank-rast.png': ('brand-local-tank-rast.png', 'image/png'),
                      '/brand-local-taunus-wunderland.png': ('brand-local-taunus-wunderland.png', 'image/png'),
                      '/brand-local-thiele.png': ('brand-local-thiele.png', 'image/png'),
                      '/brand-local-timberjacks.png': ('brand-local-timberjacks.png', 'image/png'),
                      '/brand-local-tournesol.svg': ('brand-local-tournesol.svg', 'image/svg+xml'),
                      '/brand-local-zeiss.png': ('brand-local-zeiss.png', 'image/png'),
                      '/brand-lowa.png': ('brand-lowa.png', 'image/png'),
                      '/brand-mcfit.png': ('brand-mcfit.png', 'image/png'),
                      '/brand-mcpaper.jpg': ('brand-mcpaper.jpg', 'image/jpeg'),
                      '/brand-mercure.svg': ('brand-mercure.svg', 'image/svg+xml'),
                      '/brand-mixmarkt.ico': ('brand-mixmarkt.ico', 'image/x-icon'),
                      '/brand-multisafepay.ico': ('brand-multisafepay.ico', 'image/x-icon'),
                      '/brand-nanu-nana.png': ('brand-nanu-nana.png', 'image/png'),
                      '/brand-nordsee.png': ('brand-nordsee.png', 'image/png'),
                      '/brand-norma.ico': ('brand-norma.ico', 'image/x-icon'),
                      '/brand-norton.png': ('brand-norton.png', 'image/png'),
                      '/brand-oneandone.png': ('brand-oneandone.png', 'image/png'),
                      '/brand-otelo.png': ('brand-otelo.png', 'image/png'),
                      '/brand-parkster.webp': ('brand-parkster.webp', 'image/webp'),
                      '/brand-paybyphone.ico': ('brand-paybyphone.ico', 'image/x-icon'),
                      '/brand-payone.png': ('brand-payone.png', 'image/png'),
                      '/brand-poco.png': ('brand-poco.png', 'image/png'),
                      '/brand-pvs-dental.png': ('brand-pvs-dental.png', 'image/png'),
                      '/brand-qpark.ico': ('brand-qpark.ico', 'image/x-icon'),
                      '/brand-rabot.png': ('brand-rabot.png', 'image/png'),
                      '/brand-raisin.png': ('brand-raisin.png', 'image/png'),
                      '/brand-ratepay.svg': ('brand-ratepay.svg', 'image/svg+xml'),
                      '/brand-refurbed.ico': ('brand-refurbed.ico', 'image/x-icon'),
                      '/brand-reservix.ico': ('brand-reservix.ico', 'image/x-icon'),
                      '/brand-revolut.svg': ('brand-revolut.svg', 'image/svg+xml'),
                      '/brand-samsung.svg': ('brand-samsung.svg', 'image/svg+xml'),
                      '/brand-schaefer-dein-baecker.jpg': ('brand-schaefer-dein-baecker.jpg', 'image/jpeg'),
                      '/brand-schoeffel.ico': ('brand-schoeffel.ico', 'image/x-icon'),
                      '/brand-simmel.png': ('brand-simmel.png', 'image/png'),
                      '/brand-stiftung-warentest.ico': ('brand-stiftung-warentest.ico', 'image/x-icon'),
                      '/brand-stripe.svg': ('brand-stripe.svg', 'image/svg+xml'),
                      '/brand-sumup.svg': ('brand-sumup.svg', 'image/svg+xml'),
                      '/brand-tamaris.ico': ('brand-tamaris.ico', 'image/x-icon'),
                      '/brand-tamoil.ico': ('brand-tamoil.ico', 'image/x-icon'),
                      '/brand-targobank.png': ('brand-targobank.png', 'image/png'),
                      '/brand-tchibo.ico': ('brand-tchibo.ico', 'image/x-icon'),
                      '/brand-tedi.png': ('brand-tedi.png', 'image/png'),
                      '/brand-tegut.svg': ('brand-tegut.svg', 'image/svg+xml'),
                      '/brand-tfbank.ico': ('brand-tfbank.ico', 'image/x-icon'),
                      '/brand-tk.ico': ('brand-tk.ico', 'image/x-icon'),
                      '/brand-toom.ico': ('brand-toom.ico', 'image/x-icon'),
                      '/brand-trustedshops.svg': ('brand-trustedshops.svg', 'image/svg+xml'),
                      '/brand-ubigi.ico': ('brand-ubigi.ico', 'image/x-icon'),
                      '/brand-united-domains.svg': ('brand-united-domains.svg', 'image/svg+xml'),
                      '/brand-vattenfall.ico': ('brand-vattenfall.ico', 'image/x-icon'),
                      '/brand-vinted.svg': ('brand-vinted.svg', 'image/svg+xml'),
                      '/brand-vr.svg': ('brand-vr.svg', 'image/svg+xml'),
                      '/brand-webde.ico': ('brand-webde.ico', 'image/x-icon'),
                      '/brand-winsim.ico': ('brand-winsim.ico', 'image/x-icon'),
                      '/tabs.js': ('tabs.js', 'text/javascript; charset=utf-8'),
                      '/finanzguru.js': ('finanzguru.js', 'text/javascript; charset=utf-8'),
                      '/monthly.js': ('monthly.js', 'text/javascript; charset=utf-8'),
                      '/approvals.js': ('approvals.js', 'text/javascript; charset=utf-8'),
                      '/analytics.js': ('analytics.js', 'text/javascript; charset=utf-8'),
                      '/classification.js': ('classification.js', 'text/javascript; charset=utf-8'),
                      '/booking_editor.js': ('booking_editor.js', 'text/javascript; charset=utf-8'),
                      '/booking_editor.css': ('booking_editor.css', 'text/css; charset=utf-8'),
                      '/classification_mobile.css': ('classification_mobile.css', 'text/css; charset=utf-8'),
                      '/bonsy.js': ('bonsy.js', 'text/javascript; charset=utf-8'),
                      '/budget.js': ('budget.js', 'text/javascript; charset=utf-8'),
                      '/plan_actual.js': ('plan_actual.js', 'text/javascript; charset=utf-8'),
                      '/wealth.js': ('wealth.js', 'text/javascript; charset=utf-8'),
                      '/budget.css': ('budget.css', 'text/css; charset=utf-8'),
                      '/intake.js': ('intake.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if url.path in assets:
                name, mime = assets[url.path]
                content = files('finance_control').joinpath('static', name).read_bytes()
                if url.path == '/' and access_token is not None:
                    content = content.replace(b'</header>', LOGOUT_FORM.encode() + b'</header>', 1)
                return self.reply(200, content, mime)
            with dispatch_lock:
                if not self.require_identity(recheck=True):
                    return
                return self.dynamic_get(url)

        def dynamic_get(self, url):
            if re.fullmatch(r'/exports/[0-9a-f]{32}\.(csv|json|zip)', url.path):
                path = (app.database.parent / 'exports' / url.path.rsplit('/', 1)[1]).resolve()
                if not path.is_relative_to(app.database.parent) or not path.is_file():
                    return self.reply(404, {'error': 'Export nicht gefunden.'})
                content = path.read_bytes()
                self.send_response(200)
                mime = {'.csv': 'text/csv; charset=utf-8', '.json': 'application/json', '.zip': 'application/zip'}
                self.send_header('Content-Type', mime[path.suffix])
                self.send_header('Content-Disposition', 'attachment; filename="finance-export' + path.suffix + '"')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(content)
                return
            if url.path == '/api/state':
                try:
                    as_of = parse_qs(url.query).get('as_of', [default_cutoff()])[0]
                    date.fromisoformat(as_of)
                    return self.reply(200, app.state(as_of))
                except (ValueError, sqlite3.Error):
                    return self.reply(400, {'error': 'Stichtag liegt vor einem Eröffnungssaldo oder ist ungültig.'})
            if url.path == '/api/administration':
                if administration is None:
                    return self.reply(200, {'enabled': False})
                try:
                    return self.reply(200, {'enabled': True, **administration.state(self.management_user)})
                except AdministrationError as error:
                    return self.management_error(error)
            if url.path == '/api/budget-actual-metadata':
                return self.reply(200, app.action(url.path, {}))
            return self.reply(404, {'error': 'Nicht gefunden.'})

        def do_POST(self):
            if urlsplit(self.path).path == LOGIN_PATH:
                if not self.trusted_host() or not self.trusted_origin():
                    self.discard_rejected_body()
                    return self.reply(403, {'error': 'Anfrage nicht freigegeben. Seite neu laden.'})
                return self.handle_login()
            if urlsplit(self.path).path == LOGOUT_PATH:
                if not self.trusted_host() or not self.trusted_origin():
                    self.discard_rejected_body()
                    return self.reply(403, {'error': 'Anfrage nicht freigegeben. Seite neu laden.'})
                return self.handle_logout()
            if self.trusted_host() and not self.authorized():
                self.discard_rejected_body()
                return self.login_required()
            if (not self.trusted_host() or not self.trusted_origin()
                    or not secrets.compare_digest(self.headers.get('X-Finance-Token', ''), app.token)):
                self.discard_rejected_body()
                return self.reply(403, {'error': 'Anfrage nicht freigegeben. Seite neu laden.'})
            if not self.require_identity(discard_body=True):
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                large_uploads = {'/api/fg-stage', '/api/bonsy-import'}
                limit = (45 * 1024 * 1024
                         if urlsplit(self.path).path in large_uploads else MAX_REQUEST)
                if not 0 < size <= limit or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('Invalid request')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise TypeError('Invalid request')
                with dispatch_lock:
                    if not self.require_identity(recheck=True):
                        return
                    return self.dispatch_action(data)
            except AdministrationError as error:
                return self.management_error(error)
            except sqlite3.IntegrityError:
                return self.reply(409, {'error': 'Name oder Kennung existiert bereits. Nichts überschrieben.'})
            except DriveApiError:
                return self.reply(503, {
                    'error': 'Drive-Sicherung derzeit nicht erreichbar. Das lokale Sicherungspaket bleibt erhalten.'})
            except (ValueError, KeyError, TypeError, ArithmeticError, StopIteration) as error:
                if (urlsplit(self.path).path == '/api/fg-commit' and
                        isinstance(error, ValueError) and str(error) == 'review_stale_or_blocked'):
                    return self.reply(409, {'error': 'review_stale_or_blocked'})
                if urlsplit(self.path).path.startswith('/api/classification-'):
                    if isinstance(error, ValueError) and str(error) == 'stale_revision':
                        return self.reply(409, {
                            'error': 'Buchung oder Beleg wurde inzwischen geändert. Bitte neu laden.'})
                    return self.reply(400, {'error': 'Zuordnung prüfen und neu laden. Kategorie muss zur Buchungsrichtung passen. Rechnungen erst anhand der Quelle bestätigen; Betrag muss genau zur Ausgabe passen. Bereits verknüpfte Rechnungswerte sind gesperrt.'})
                if urlsplit(self.path).path.startswith('/api/intake-'):
                    return self.reply(400, {'error': 'Importentwurf prüfen: Salden müssen centgenau und ausdrücklich bestätigt sein. Bei geändertem Entwurf neu laden; Quellen und bestehende Konten werden nicht überschrieben.'})
                if urlsplit(self.path).path.startswith('/api/monthly-'):
                    return self.reply(400, {'error': 'Monatsprüfung nicht möglich. Konten benötigen einen gemeinsamen Eröffnungsstichtag vor dem Monat. Vorschau neu laden und Vollständigkeit bestätigen. Ein unveränderter Stand kann nicht erneut gespeichert werden.'})
                if urlsplit(self.path).path.startswith('/api/wealth-'):
                    return self.reply(400, {'error': 'Vermögensstand nicht verarbeitet. Eingaben, Eigentümer, Stichtage und aktuelle Revision prüfen; danach eine neue Vorschau berechnen.'})
                return self.reply(400, {'error': 'Eingaben prüfen: Pflichtfelder, Beträge, CSV-Vertrag, gemeinsamer Stichtag und Monatsende. Vergleich benötigt denselben Ausgangsbestand.'})
            except (OSError, sqlite3.Error):
                return self.reply(500, {'error': 'Lokaler Speichervorgang fehlgeschlagen. Bitte erneut prüfen.'})

        def dispatch_action(self, data):
            if urlsplit(self.path).path.startswith('/api/administration/'):
                if administration is None:
                    return self.management_error(AdministrationError('administration_disabled', 403))
                action = urlsplit(self.path).path.removeprefix('/api/administration/')
                if action in {'bank-credentials-state', 'bank-credentials-save', 'bank-credentials-delete'}:
                    return self.reply(200, server_banking.dispatch(self.management_user, action, data))
                if action == 'bank-balances-read':
                    return self.reply(200, bank_jobs.start(self.management_user, data))
                if action == 'bank-balances-state':
                    return self.reply(200, bank_jobs.state(self.management_user, data))
                if action == 'postbank-targets':
                    return self.reply(200, postbank_jobs.targets(self.management_user, data))
                if action == 'postbank-read':
                    return self.reply(200, postbank_jobs.start(self.management_user, data))
                if action == 'postbank-state':
                    return self.reply(200, postbank_jobs.state(self.management_user, data))
                if action == 'postbank-commit':
                    return self.reply(200, bank_refresh.commit_and_remember(
                        self.management_user, data))
                if action == 'bank-refresh-start':
                    return self.reply(200, bank_refresh.start(self.management_user, data))
                if action == 'bank-refresh-state':
                    return self.reply(200, bank_refresh.state(self.management_user, data))
                result = administration.change(self.management_user, action, data)
                return self.reply(200, result if result.get('removed') else {'enabled': True, **result})
            return self.reply(200, app.action(urlsplit(self.path).path, data))

    server = BoundedHTTPServer((host, port), Handler)
    if port == 0:
        for entry in auto_port_hosts:
            trusted_hosts.discard(entry)
            trusted_hosts.add(entry.rsplit(':', 1)[0] + f':{server.server_port}')
    return server


def seed_demo(app):
    store = Store(app.database)
    try:
        if not store.accounts():
            cutoff = default_cutoff()
            people = app.profile['people']
            household = app.profile['household_id']
            for index, person in enumerate(people, 1):
                account_id = 'Demo-Giro' if index == 1 else f'Demo-Giro-{index}'
                store.add_account(account_id, person['id'], {person['id']: '1'},
                                  str(1000 + 500 * index), cutoff,
                                  display_name=f'Girokonto Person {index}', household=household)
            if len(people) > 1:
                # Use exact decimal shares whose sum remains exactly one.
                equal_share = (Decimal(1) / Decimal(len(people))).quantize(Decimal('0.0001'))
                shares = {person['id']: format(equal_share, 'f')
                          for person in people[:-1]}
                shares[people[-1]['id']] = format(
                    Decimal(1) - sum((Decimal(value) for value in shares.values()), Decimal(0)), 'f')
                store.add_account('Demo-Gemeinsam', 'JOINT', shares, '1000', cutoff,
                                  display_name='Gemeinsames Konto', household=household)
    finally:
        store.close()


def _load_addon_options(path):
    """Read the Home Assistant add-on options file; ignore anything unusable."""
    try:
        options = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return options if isinstance(options, dict) else {}


def resolve_access_token(bind_host, options, environ, *, no_app_auth=False, token_file=None):
    """Return ``(token_or_None, source)`` for the application-level login.

    Secure by default: a network-facing bind needs a token. Opt-out only through
    ``--no-app-auth`` or the add-on option ``require_app_token: false`` (for
    deployments fully protected by Cloudflare Access). A loopback bind keeps the
    historic token-free behaviour.
    """
    if no_app_auth or options.get('require_app_token') is False:
        return None, 'disabled'
    configured = str(options.get('access_token') or environ.get('FINANCE_ACCESS_TOKEN') or '').strip()
    if len(configured) >= MIN_ACCESS_TOKEN:
        return configured, 'configured'
    if bind_host in LOCAL_BIND_HOSTS:
        return None, 'local'
    stored = ''
    if token_file is not None:
        try:
            stored = Path(token_file).read_text(encoding='utf-8').strip()
        except OSError:
            stored = ''
        if len(stored) >= MIN_ACCESS_TOKEN:
            return stored, 'stored'
    token = secrets.token_urlsafe(24)
    if token_file is not None:
        try:
            descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                handle.write(token)
        except OSError:
            pass
    return token, 'generated'


def main():
    parser = argparse.ArgumentParser(description='Lokales Finance-Control-Cockpit')
    parser.add_argument('--options-file', metavar='FILE',
                        help='Home-Assistant-Add-on-Optionen (JSON) mit access_token und require_app_token')
    parser.add_argument('--no-app-auth', action='store_true',
                        help='Anwendungs-Login ausdrücklich abschalten (nur hinter Cloudflare Access o. Ä.)')
    parser.add_argument('--allowed-host', action='append', default=[], dest='allowed_hosts',
                        help='Zusätzlicher Host-Header eines lokalen Reverse-Proxys; mehrfach möglich')
    parser.add_argument('--allowed-origin', action='append', default=[], dest='allowed_origins',
                        help='Explizit erlaubter HTTP(S)-Origin ohne Pfad; mehrfach möglich')
    parser.add_argument('--host', default='127.0.0.1',
                        help='Bind-Adresse; standardmäßig nur lokal, für einen geschützten Reverse-Proxy z. B. 0.0.0.0')
    parser.add_argument('--port', type=int, default=8785)
    parser.add_argument('--demo', action='store_true', help='Separate synthetische Datenbank')
    parser.add_argument('--data-directory', metavar='DIRECTORY',
                        help='Bestehenden lokalen Datenordner verwenden')
    args = parser.parse_args()
    profile = None
    if args.data_directory is not None:
        if args.demo:
            parser.error('--data-directory kann nicht mit --demo verwendet werden')
        try:
            root = outside_repository(args.data_directory)
        except ValueError as exc:
            parser.exit(2, str(exc) + '\n')
        if not root.is_dir() or not (root / 'finance.sqlite').is_file():
            parser.exit(2, 'Initialisierter Datenordner nicht gefunden.\n')
        database = outside_repository(root / 'finance.sqlite')
        if database.parent != root:
            parser.exit(2, 'Umgeleitete Datenbank nicht zulässig.\n')
        try:
            profile = json.loads((root / 'profile.json').read_text(encoding='utf-8'))
            normalize_profile(profile)
        except (OSError, ValueError, KeyError, TypeError):
            parser.exit(2, 'Gültiges Profil im Datenordner nicht gefunden.\n')
    else:
        local = Path(os.environ['LOCALAPPDATA']).resolve()
        root = (local / 'FinanceControl' / 'data').resolve()
        # Allow Windows app-container redirection within LocalAppData, not external sync paths.
        if not root.is_relative_to(local):
            parser.exit(2, 'Umgeleiteter Datenordner nicht zulässig.\n')
        database = root / ('cockpit-demo.sqlite' if args.demo else 'finance.sqlite')
        if database.resolve().parent != root:
            parser.exit(2, 'Umgeleitete Datenbank nicht zulässig.\n')
        profile_path = root / 'profile.json'
        if not args.demo and profile_path.is_file():
            try:
                profile = json.loads(profile_path.read_text(encoding='utf-8'))
                normalize_profile(profile)
            except (OSError, ValueError, KeyError, TypeError):
                parser.exit(2, 'Gültiges Profil im Datenordner nicht gefunden.\n')
    app = Cockpit(database, demo=args.demo, profile=profile)
    if args.demo:
        seed_demo(app)
    options = _load_addon_options(args.options_file) if args.options_file else {}
    access_token, token_source = resolve_access_token(
        args.host, options, os.environ, no_app_auth=args.no_app_auth,
        token_file=root / 'app_access_token' if root.is_dir() else None)
    try:
        identity_verifier, bootstrap_emails = configured_identity(options)
    except ValueError as error:
        parser.exit(2, f'{error}\n')
    server = make_server(app, args.port, args.allowed_hosts, args.host, args.allowed_origins,
                         access_token=access_token, identity_verifier=identity_verifier,
                         bootstrap_emails=bootstrap_emails, bank_product_id=options.get('fints_product_id'))
    if token_source == 'generated':
        # Printed once, when the token is created. Later starts reuse the stored file silently so
        # the token does not reappear in every rotated add-on log.
        print(f'Finance-Control-Zugriffstoken (neu erzeugt; Option access_token hat Vorrang): '
              f'{access_token}', flush=True)
    elif token_source == 'stored':
        print('Finance Control: gespeichertes Zugriffstoken wird verwendet (Datei app_access_token '
              'im Datenordner oder Option access_token).', flush=True)
    elif token_source == 'disabled':
        print('Finance Control: Anwendungs-Login deaktiviert; Zugriffsschutz muss vorgelagert sein.',
              flush=True)
    print(f'Finance Control: http://{args.host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
