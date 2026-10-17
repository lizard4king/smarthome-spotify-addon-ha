"""Explicit publication of owner-private PayPal schedules into household forecasts.

The publication contains no principal, interest, payment history or owner identity.
A newer source snapshot invalidates publication until its owner reconfirms it.
"""

from datetime import UTC, datetime
from decimal import Decimal
import json
import re
import unicodedata

from .account_lifecycle import visible_on
from .administration import AdministrationError
from .paypal_credit import _ID


_ACTIONS = {'paypal-credit-forecast-view', 'paypal-credit-forecast-save',
            'paypal-credit-forecast-remove'}


def _invalid():
    raise AdministrationError('paypal_credit_forecast_invalid')


def _eligible(account, today, schedule=()):
    return (account is not None and account['kind'] == 'CHECKING'
            and account['currency'] == 'EUR' and account['opening_date'] <= today
            and visible_on(dict(account), today)
            and all(account['opening_date'] <= payment['date']
                    and visible_on(dict(account), payment['date']) for payment in schedule))


def _source(db, owner_id, loan_id):
    row = db.execute(
        'SELECT * FROM paypal_credit_snapshots WHERE loan_id=? AND owner_id=? '
        'ORDER BY revision DESC LIMIT 1', (loan_id, owner_id)).fetchone()
    if row is None:
        raise AdministrationError('forbidden', 403)
    return row


def _link(db, loan_id):
    return db.execute('SELECT * FROM paypal_credit_forecast_links WHERE loan_id=? '
                      'ORDER BY revision DESC LIMIT 1', (loan_id,)).fetchone()


def _public_link(link, source):
    if link is None:
        return None
    return {key: link[key] for key in
            ('revision', 'source_revision', 'account_id', 'reference')} | {
                'active': bool(link['active']),
                'stale': link['source_revision'] != source['revision']}


def dispatch(administration, actor, action, data):
    """Read or append one explicitly confirmed binding, always for the actor's plan."""
    if action not in _ACTIONS or type(data) is not dict:
        _invalid()
    fields = {'loan_id'} if action.endswith('-view') else {
        'loan_id', 'source_revision', 'revision', 'confirmed'}
    if action.endswith('-save'):
        fields |= {'account_id', 'reference'}
    if set(data) != fields:
        _invalid()
    loan_id = data['loan_id']
    if type(loan_id) is not str or _ID.fullmatch(loan_id) is None:
        _invalid()
    write = not action.endswith('-view')
    if write:
        if data['confirmed'] is not True:
            raise AdministrationError('confirmation_required')
        if (type(data['source_revision']) is not int or data['source_revision'] < 1
                or type(data['revision']) is not int or data['revision'] < 0):
            _invalid()
    today = datetime.now(UTC).date().isoformat()
    with administration._connection(write=write) as db:
        if not write:
            # Keep the displayed source, binding and account list on one read snapshot.
            db.execute('BEGIN')
        owner = administration._actor(db, actor)
        source = _source(db, owner['id'], loan_id)
        link = _link(db, loan_id)
        plan = json.loads(source['plan_json'])
        schedule = plan['schedule']
        if not write:
            accounts = [
                {'id': row['id'], 'label': row['display_name'] or row['id'],
                 'owner': row['owner'], 'currency': row['currency']}
                for row in db.execute('SELECT * FROM accounts ORDER BY id')
                if _eligible(row, today, schedule)]
            return {'loan_id': loan_id, 'source_revision': source['revision'],
                    'link': _public_link(link, source), 'accounts': accounts,
                    'plan': {'label': plan['label'], 'schedule': schedule}}
        if (source['revision'] != data['source_revision']
                or (0 if link is None else link['revision']) != data['revision']):
            raise AdministrationError('paypal_credit_forecast_stale', 409)
        if action.endswith('-save'):
            account_id, reference = data['account_id'], data['reference']
            if type(account_id) is not str or type(reference) is not str:
                _invalid()
            if any(unicodedata.category(char).startswith('C') for char in reference):
                _invalid()
            reference = reference.strip()
            identifying_text = re.sub(r'\bEUR\b|€', '', reference, flags=re.IGNORECASE)
            if (not 4 <= len(reference) <= 240
                    or not any(char.isalpha() for char in identifying_text)
                    or not any(char.isdigit() for char in reference)
                    or re.fullmatch(r'[\d\s.,+\-€]+(?:EUR)?', reference, re.IGNORECASE)):
                _invalid()
            account = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
            if not _eligible(account, today, schedule):
                raise AdministrationError('paypal_credit_forecast_account_invalid')
            active = True
        else:
            if link is None or not link['active']:
                _invalid()
            account_id, reference, active = link['account_id'], link['reference'], False
        revision = data['revision'] + 1
        db.execute(
            'INSERT INTO paypal_credit_forecast_links '
            '(loan_id,revision,owner_id,source_revision,account_id,reference,active,created_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (loan_id, revision, owner['id'], source['revision'], account_id, reference,
             int(active), datetime.now(UTC).isoformat().replace('+00:00', 'Z')))
        link = _link(db, loan_id)
        return {'loan_id': loan_id, 'source_revision': source['revision'],
                'link': _public_link(link, source)}


def forecast_schedules(store, *, as_of=None):
    """Return only explicitly published schedules, with unusable bindings marked.

    A stale publication retains its original account and source schedule solely
    for diagnostics. Consumers must exclude every schedule with missing_fields.
    Inactive links and inactive app users publish nothing, even older active rows.
    Account lifetime is evaluated at the requested forecast cutoff; direct callers
    default to today. Exact installment dates must still fit the account lifetime.
    """
    today = datetime.now(UTC).date().isoformat() if as_of is None else as_of
    rows = store.db.execute(
        'SELECT l.*,s.plan_json,(SELECT MAX(revision) FROM paypal_credit_snapshots '
        'WHERE loan_id=l.loan_id) AS current_source_revision '
        'FROM paypal_credit_forecast_links l '
        'JOIN (SELECT loan_id,MAX(revision) AS revision FROM paypal_credit_forecast_links '
        'GROUP BY loan_id) latest ON latest.loan_id=l.loan_id AND latest.revision=l.revision '
        'JOIN app_users u ON u.id=l.owner_id AND u.active=1 '
        'JOIN paypal_credit_snapshots s ON s.loan_id=l.loan_id AND s.revision=l.source_revision '
        'WHERE l.active=1 ORDER BY l.loan_id').fetchall()
    schedules = []
    for row in rows:
        plan = json.loads(row['plan_json'])
        payments = [{'date': payment['date'],
                     'amount': format(-Decimal(payment['amount']), '.2f')}
                    for payment in plan['schedule']]
        missing, warnings = [], []
        if row['source_revision'] != row['current_source_revision']:
            missing.append('source_revision')
            warnings.append('paypal_credit_source_stale')
        account = store.db.execute('SELECT * FROM accounts WHERE id=?',
                                   (row['account_id'],)).fetchone()
        if not _eligible(account, today, plan['schedule']):
            missing.append('account_id')
            warnings.append('paypal_credit_account_ineligible')
        schedules.append({
            'id': 'paypal-credit:' + row['loan_id'], 'revision': row['revision'],
            'document_id': None, 'document_revision': None,
            'label': 'PayPal · ' + plan['label'], 'account_id': row['account_id'],
            'currency': plan['currency'], 'amount': None,
            'valid_from': payments[0]['date'] if payments else None,
            'valid_to': payments[-1]['date'] if payments else None,
            'day_of_month': None, 'interval_months': None,
            'reference': row['reference'], 'active': True,
            'estimated': False, 'date_estimated': False, 'amount_estimated': False,
            'basis': 'confirmed_paypal_credit', 'missing_fields': missing,
            'phases': [], 'exact_payments': payments, 'warnings': warnings})
    return schedules
