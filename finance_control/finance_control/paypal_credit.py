"""Owner-bound manual PayPal credit-plan snapshots; no ledger or provider access."""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
from uuid import uuid4

from .administration import AdministrationError


_SOURCE = 'manual_paypal_portal'
_MAX_PLANS = 20
_MAX_ROWS = 360
_MAX_AMOUNT = Decimal('999999999999.99')
_CENT = re.compile(r'(?:0|[1-9][0-9]{0,11})\.[0-9]{2}\Z', re.ASCII)
_APR = re.compile(r'(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,4})?\Z', re.ASCII)
_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z', re.ASCII)
_PLAN_FIELDS = {
    'label', 'as_of', 'currency', 'net_credit', 'total_interest', 'total_payable',
    'paid_total', 'remaining_total', 'apr', 'outstanding_principal', 'payments', 'schedule',
    'regular_plan_confirmed',
}


def _invalid():
    raise AdministrationError('paypal_credit_invalid')


def _date(value, *, maximum=None, minimum=None):
    if type(value) is not str:
        _invalid()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _invalid()
    if parsed.isoformat() != value or (maximum is not None and parsed > maximum) or (
            minimum is not None and parsed < minimum):
        _invalid()
    return parsed


def _amount(value, *, positive=False):
    if type(value) is not str or _CENT.fullmatch(value) is None:
        _invalid()
    parsed = Decimal(value)
    if parsed > _MAX_AMOUNT or (positive and parsed <= 0):
        _invalid()
    return parsed


def _canonical_plan(raw, today):
    if type(raw) is not dict or set(raw) != _PLAN_FIELDS:
        _invalid()
    if raw['regular_plan_confirmed'] is not True:
        _invalid()
    label = raw['label']
    if (type(label) is not str or not 1 <= len(label.strip()) <= 120
            or any(ord(character) < 32 for character in label)):
        _invalid()
    label = label.strip()
    as_of = _date(raw['as_of'], maximum=today)
    if raw['currency'] != 'EUR':
        _invalid()

    net_credit = _amount(raw['net_credit'], positive=True)
    total_interest = _amount(raw['total_interest'])
    total_payable = _amount(raw['total_payable'], positive=True)
    paid_total = _amount(raw['paid_total'])
    remaining_total = _amount(raw['remaining_total'])
    outstanding = raw['outstanding_principal']
    outstanding_value = None if outstanding is None else _amount(outstanding)
    apr_raw = raw['apr']
    if type(apr_raw) is not str or _APR.fullmatch(apr_raw) is None:
        _invalid()
    try:
        apr = Decimal(apr_raw)
    except InvalidOperation:
        _invalid()
    if not apr.is_finite() or apr > Decimal('1000'):
        _invalid()

    if (net_credit + total_interest != total_payable
            or paid_total + remaining_total != total_payable
            or (outstanding_value is not None and outstanding_value > net_credit)):
        _invalid()

    payments_raw, schedule_raw = raw['payments'], raw['schedule']
    if type(payments_raw) is not list or len(payments_raw) > _MAX_ROWS:
        _invalid()
    if type(schedule_raw) is not list or len(schedule_raw) > _MAX_ROWS:
        _invalid()

    payments = []
    previous_day = None
    known_interest = Decimal('0.00')
    all_interest_known = True
    for payment in payments_raw:
        if type(payment) is not dict or set(payment) != {'date', 'amount', 'interest'}:
            _invalid()
        payment_day = _date(payment['date'], maximum=as_of)
        if previous_day is not None and payment_day <= previous_day:
            _invalid()
        previous_day = payment_day
        amount = _amount(payment['amount'], positive=True)
        interest_raw = payment['interest']
        interest = None if interest_raw is None else _amount(interest_raw)
        if interest is None:
            all_interest_known = False
        else:
            if interest > amount:
                _invalid()
            known_interest += interest
        payments.append({'date': payment_day.isoformat(), 'amount': format(amount, '.2f'),
                         'interest': None if interest is None else format(interest, '.2f')})

    schedule = []
    previous_day = None
    for installment in schedule_raw:
        if type(installment) is not dict or set(installment) != {'date', 'amount'}:
            _invalid()
        due_day = _date(installment['date'], minimum=as_of)
        if previous_day is not None and due_day <= previous_day:
            _invalid()
        previous_day = due_day
        amount = _amount(installment['amount'], positive=True)
        schedule.append({'date': due_day.isoformat(), 'amount': format(amount, '.2f')})

    if (sum((_amount(item['amount']) for item in payments), Decimal('0.00')) != paid_total
            or sum((_amount(item['amount']) for item in schedule), Decimal('0.00')) != remaining_total
            or known_interest > total_interest):
        _invalid()
    if payments and schedule and payments[-1]['date'] >= schedule[0]['date']:
        _invalid()

    paid_interest = known_interest if all_interest_known else None
    paid_principal = paid_total - known_interest if all_interest_known else None
    if paid_principal is not None and paid_principal > net_credit:
        _invalid()
    apr_text = format(apr, 'f')
    if '.' in apr_text:
        apr_text = apr_text.rstrip('0').rstrip('.')
    plan = {
        'label': label, 'as_of': as_of.isoformat(), 'currency': 'EUR',
        'net_credit': format(net_credit, '.2f'), 'total_interest': format(total_interest, '.2f'),
        'total_payable': format(total_payable, '.2f'), 'paid_total': format(paid_total, '.2f'),
        'remaining_total': format(remaining_total, '.2f'), 'apr': apr_text,
        'outstanding_principal': (None if outstanding_value is None
                                  else format(outstanding_value, '.2f')),
        'regular_plan_confirmed': True,
        'payments': payments, 'schedule': schedule,
    }
    derived = {
        'total_installments': len(payments) + len(schedule),
        'paid_installments': len(payments),
        'next_due_date': schedule[0]['date'] if schedule else None,
        'next_payment_amount': schedule[0]['amount'] if schedule else None,
        'paid_interest': None if paid_interest is None else format(paid_interest, '.2f'),
        'paid_principal': None if paid_principal is None else format(paid_principal, '.2f'),
    }
    return plan, derived


class PayPalCredit:
    """Persist manually entered plan snapshots using the administration SQLite store."""

    def __init__(self, administration):
        self.administration = administration

    def dispatch(self, actor, action, data):
        if action == 'paypal-credit-list':
            if type(data) is not dict or data:
                _invalid()
            with self.administration._connection() as db:
                owner = self.administration._actor(db, actor)
                rows = db.execute(
                    'SELECT s.loan_id,s.revision,s.source,s.created_at,s.plan_json '
                    'FROM paypal_credit_snapshots s JOIN ('
                    'SELECT loan_id,MAX(revision) AS revision FROM paypal_credit_snapshots '
                    'WHERE owner_id=? GROUP BY loan_id) latest '
                    'ON latest.loan_id=s.loan_id AND latest.revision=s.revision '
                    'WHERE s.owner_id=? ORDER BY s.created_at DESC,s.loan_id',
                    (owner['id'], owner['id'])).fetchall()
                plans = []
                for row in rows:
                    plan = json.loads(row['plan_json'])
                    _, derived = _canonical_plan(plan, date.fromisoformat(plan['as_of']))
                    plans.append({'loan_id': row['loan_id'], 'revision': row['revision'],
                                  'source': row['source'], 'created_at': row['created_at'],
                                  'plan': plan, 'derived': derived})
                return {'plans': plans}

        if action != 'paypal-credit-save' or type(data) is not dict:
            _invalid()
        if data.get('confirmed') is not True:
            raise AdministrationError('confirmation_required')
        if set(data) != {'loan_id', 'revision', 'confirmed', 'plan'}:
            _invalid()
        today = datetime.now(UTC).date()
        plan, derived = _canonical_plan(data['plan'], today)
        loan_id, revision = data['loan_id'], data['revision']
        is_new = loan_id is None
        if is_new:
            if revision is not None:
                _invalid()
            loan_id = str(uuid4())
        else:
            if type(loan_id) is not str or _ID.fullmatch(loan_id) is None:
                _invalid()
            if type(revision) is not int or revision < 1:
                _invalid()

        created_at = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
        payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self.administration._connection(write=True) as db:
            owner = self.administration._actor(db, actor)
            if is_new:
                count = db.execute(
                    'SELECT COUNT(DISTINCT loan_id) FROM paypal_credit_snapshots WHERE owner_id=?',
                    (owner['id'],)).fetchone()[0]
                if count >= _MAX_PLANS:
                    _invalid()
                next_revision = 1
            else:
                current = db.execute(
                    'SELECT MAX(revision) AS revision FROM paypal_credit_snapshots '
                    'WHERE loan_id=? AND owner_id=?', (loan_id, owner['id'])).fetchone()['revision']
                if current is None:
                    raise AdministrationError('forbidden', 403)
                if current != revision:
                    raise AdministrationError('paypal_credit_stale', 409)
                previous = db.execute(
                    'SELECT plan_json FROM paypal_credit_snapshots '
                    'WHERE loan_id=? AND owner_id=? AND revision=?',
                    (loan_id, owner['id'], current)).fetchone()
                previous_plan = json.loads(previous['plan_json'])
                if (date.fromisoformat(plan['as_of']) < date.fromisoformat(previous_plan['as_of'])
                        or any(plan[field] != previous_plan[field] for field in
                               ('net_credit', 'total_interest', 'total_payable', 'apr'))):
                    _invalid()
                if Decimal(plan['paid_total']) < Decimal(previous_plan['paid_total']):
                    _invalid()
                new_payments = {item['date']: item for item in plan['payments']}
                for old_payment in previous_plan['payments']:
                    new_payment = new_payments.get(old_payment['date'])
                    if (new_payment is None or new_payment['amount'] != old_payment['amount']
                            or (old_payment['interest'] is not None
                                and new_payment['interest'] != old_payment['interest'])):
                        _invalid()
                next_revision = current + 1
            db.execute(
                'INSERT INTO paypal_credit_snapshots '
                '(loan_id,revision,owner_id,source,created_at,plan_json) VALUES (?,?,?,?,?,?)',
                (loan_id, next_revision, owner['id'], _SOURCE, created_at, payload))
        return {'loan_id': loan_id, 'revision': next_revision, 'source': _SOURCE,
                'created_at': created_at, 'plan': plan, 'derived': derived}
