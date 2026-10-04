"""Single-user localhost cockpit, standard-library server, no external assets."""
import argparse
import base64
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import monthly_review, overview
from .core import Plan, Store
from .drive_api import DriveApiError
from .household import normalize_profile, template_profile
from .import_preview import outside_repository
from .person_attribution import person_label
from .planning import decimal_text, export_projection, month_end, parse_plan, projection

MAX_REQUEST = 2 * 1024 * 1024
MAX_REJECTED_BODY = 64 * 1024


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
            backup_status = backup_sync_status(self.database)
            result = {'demo': self.demo, 'csrf': self.token, 'as_of': as_of,
                    'accounts': accounts, 'status': status, 'scenarios': store.scenario_records(),
                    'monthly_reviews': monthly_review.records(store),
                    'overview': overview.build(store, as_of, status),
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


def make_server(app, port=8785, allowed_hosts=None, host='127.0.0.1', allowed_origins=None):
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
            url = urlsplit(self.path)
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
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
                return self.reply(200, files('finance_control').joinpath('static', name).read_bytes(), mime)
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
            if url.path == '/api/budget-actual-metadata':
                return self.reply(200, app.action(url.path, {}))
            return self.reply(404, {'error': 'Nicht gefunden.'})

        def do_POST(self):
            if (not self.trusted_host() or not self.trusted_origin()
                    or not secrets.compare_digest(self.headers.get('X-Finance-Token', ''), app.token)):
                self.discard_rejected_body()
                return self.reply(403, {'error': 'Anfrage nicht freigegeben. Seite neu laden.'})
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
                return self.reply(200, app.action(urlsplit(self.path).path, data))
            except sqlite3.IntegrityError:
                return self.reply(409, {'error': 'Name oder Kennung existiert bereits. Nichts überschrieben.'})
            except DriveApiError:
                return self.reply(503, {
                    'error': 'Drive-Sicherung derzeit nicht erreichbar. Das lokale Sicherungspaket bleibt erhalten.'})
            except (ValueError, KeyError, TypeError, ArithmeticError, StopIteration) as error:
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

    server = HTTPServer((host, port), Handler)
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


def main():
    parser = argparse.ArgumentParser(description='Lokales Finance-Control-Cockpit')
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
    server = make_server(app, args.port, args.allowed_hosts, args.host, args.allowed_origins)
    print(f'Finance Control: http://{args.host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
