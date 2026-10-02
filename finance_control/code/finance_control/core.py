"""Small deterministic domain and SQLite boundary."""
import csv
import hashlib
import io
import json
import re
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path

_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')


def valid_identifier(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def valid_person_id(value):
    return valid_identifier(value) and value.casefold() != 'joint'


def money(value):
    if isinstance(value, (float, bool)):
        raise ValueError('Use Decimal, integer or decimal text')
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('Invalid amount') from exc
    if not result.is_finite() or abs(result) > Decimal('999999999999.99'):
        raise ValueError('Amount outside supported range')
    if result != result.quantize(Decimal('0.01')):
        raise ValueError('Amount must be finite and cent-exact')
    return result


@dataclass(frozen=True)
class Plan:
    income: Decimal
    expenses: Decimal
    reserve: Decimal = Decimal('0')
    # (month offset, signed amount), offset starts at one
    one_offs: tuple = ()

    def __post_init__(self):
        for field in ('income', 'expenses', 'reserve'):
            value = money(getattr(self, field))
            if value < 0:
                raise ValueError('Plan amounts must be nonnegative')
            object.__setattr__(self, field, value)
        entries = tuple((month, money(amount)) for month, amount in self.one_offs)
        if any(type(month) is not int or month < 1 for month, _ in entries):
            raise ValueError('Invalid month offset')
        object.__setattr__(self, 'one_offs', entries)


def forecast(opening, plan, months=12):
    if type(months) is not int or months < 1:
        raise ValueError('Positive horizon required')
    balance = money(opening)
    result = []
    for month in range(1, months + 1):
        flow = plan.income - plan.expenses + sum(
            (value for offset, value in plan.one_offs if offset == month), Decimal(0))
        balance += flow
        result.append({'month': month, 'cashflow': flow, 'liquidity': balance,
                       'below_reserve': balance < plan.reserve,
                       'shortfall': balance < 0})
    return result


def scenario(base, **overrides):
    return replace(base, **overrides)


class Store:
    def __init__(self, path=':memory:', *, readonly=False):
        if type(readonly) is not bool:
            raise ValueError('readonly must be boolean')
        if readonly and str(path) == ':memory:':
            raise ValueError('readonly requires an existing database file')
        self.path = None if str(path) == ':memory:' else Path(path).expanduser().resolve()
        self.db = sqlite3.connect(
            f'{self.path.as_uri()}?mode=ro' if readonly else
            (self.path if self.path is not None else path), uri=readonly)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        if readonly:
            self.db.execute('PRAGMA query_only=ON')
            return
        self.db.execute('CREATE TABLE IF NOT EXISTS schema_versions (version TEXT PRIMARY KEY)')
        for resource in sorted(files('finance_control').joinpath('migrations').iterdir(), key=lambda p: p.name):
            if resource.name.endswith('.sql') and not self.db.execute(
                    'SELECT 1 FROM schema_versions WHERE version=?', (resource.name,)).fetchone():
                rebuild_accounts = resource.name == '037_generic_households.sql'
                try:
                    if rebuild_accounts:
                        # Rebuilding a referenced SQLite table requires this pragma outside a transaction.
                        self.db.commit()
                        self.db.execute('PRAGMA foreign_keys=OFF')
                        if self.db.execute('PRAGMA foreign_keys').fetchone()[0] != 0:
                            raise RuntimeError('Could not disable foreign keys for account rebuild')
                    self.db.executescript('BEGIN;\n' + resource.read_text(encoding='utf-8'))
                    self.db.execute('INSERT INTO schema_versions VALUES (?)', (resource.name,))
                    if rebuild_accounts and self.db.execute('PRAGMA foreign_key_check').fetchone():
                        raise RuntimeError('Account rebuild left invalid foreign keys')
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
                finally:
                    if rebuild_accounts:
                        self.db.execute('PRAGMA foreign_keys=ON')
                        if self.db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                            raise RuntimeError('Could not restore foreign key enforcement')

    def close(self):
        self.db.close()

    def accounts(self):
        return [dict(row) | {'shares': {share['person_id']: share['share'] for share in
                self.db.execute('SELECT * FROM ownership WHERE account_id=?', (row['id'],))}}
                for row in self.db.execute('SELECT * FROM accounts ORDER BY id')]

    def scenario_records(self):
        return [{'name': row['id'], **json.loads(row['payload'])}
                for row in self.db.execute('SELECT * FROM scenarios ORDER BY id')]

    def backup(self, path):
        """Caller owns private-path validation; destination must not exist."""
        from pathlib import Path
        path = Path(path)
        with path.open('xb'):
            pass
        target = sqlite3.connect(path)
        try:
            self.db.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup integrity check failed')
        finally:
            target.close()

    def add_account(self, account_id, owner, shares, opening, opening_date,
                    institution='SYNTHETIC', kind='CHECKING', currency='EUR', display_name=None,
                    household='HOUSEHOLD'):
        amount = money(opening)
        opening_date = date.fromisoformat(opening_date).isoformat()
        if currency != 'EUR' or kind not in {'CHECKING', 'SAVINGS', 'CREDIT_CARD', 'DEPOT'}:
            raise ValueError('Unsupported currency or account kind')
        if not isinstance(shares, dict) or not shares:
            raise ValueError('Invalid ownership')
        if not valid_identifier(household):
            raise ValueError('Invalid household ID')
        try:
            weights = {person: Decimal(str(share)) for person, share in shares.items()}
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError('Invalid ownership share') from exc
        if any(not valid_person_id(person) or not share.is_finite()
               or share <= 0 for person, share in weights.items()):
            raise ValueError('Invalid ownership share')
        if (sum(weights.values()) != 1
                or (owner == 'JOINT' and len(weights) < 2)
                or (owner != 'JOINT' and weights != {owner: Decimal(1)})):
            raise ValueError('Ownership shares must sum to one and match owner')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO households(id) VALUES (?)', (household,))
            self.db.executemany('INSERT OR IGNORE INTO persons(id) VALUES (?)',
                                [(person,) for person in weights])
            self.db.execute('INSERT OR IGNORE INTO institutions VALUES (?)', (institution,))
            self.db.execute(
                'INSERT INTO accounts(id,institution,kind,owner,household,currency,opening,opening_date,display_name) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (account_id, institution, kind, owner, household, currency,
                 str(amount), opening_date, display_name))
            self.db.executemany('INSERT INTO ownership VALUES (?,?,?)',
                                [(account_id, person, str(share)) for person, share in weights.items()])

    def set_account_display_name(self, account_id, display_name, revision):
        if (not isinstance(account_id, str) or not account_id.strip()
                or len(account_id) > 120 or any(ord(char) < 32 for char in account_id)):
            raise ValueError('Invalid account ID')
        if (not isinstance(display_name, str) or not display_name.strip()
                or len(display_name.strip()) > 120
                or any(ord(char) < 32 for char in display_name)):
            raise ValueError('Invalid account display name')
        if type(revision) is not int or revision < 0:
            raise ValueError('Invalid account display name revision')
        display_name = display_name.strip()
        account_id = account_id.strip()
        with self.db:
            current = self.db.execute(
                'SELECT display_name,display_name_revision FROM accounts WHERE id=?',
                (account_id,)).fetchone()
            if current is None:
                raise ValueError('Unknown account')
            if current['display_name_revision'] != revision:
                raise ValueError('Account display name revision is stale')
            next_revision = revision + 1
            changed_at = datetime.now(UTC).isoformat()
            updated = self.db.execute(
                'UPDATE accounts SET display_name=?,display_name_revision=? '
                'WHERE id=? AND display_name_revision=?',
                (display_name, next_revision, account_id, revision))
            if updated.rowcount != 1:
                raise ValueError('Account display name revision is stale')
            self.db.execute(
                'INSERT INTO account_display_name_audit('
                'account_id,revision,previous_display_name,display_name,changed_at) VALUES (?,?,?,?,?)',
                (account_id, next_revision, current['display_name'], display_name, changed_at))
        return {'id': account_id, 'display_name': display_name, 'display_name_revision': next_revision}

    def import_csv(self, source, *, manage_transaction=True):
        reader = csv.DictReader(io.StringIO(source))
        columns = ['external_id', 'account_id', 'date', 'amount', 'currency', 'category', 'transfer_id']
        if reader.fieldnames != columns:
            raise ValueError('Unexpected CSV columns')
        rows = list(reader)
        digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
        inserted = 0
        transaction = self.db if manage_transaction else nullcontext()
        with transaction:
            if self.db.execute('SELECT 1 FROM imports WHERE digest=?', (digest,)).fetchone():
                return 0
            import_id = self.db.execute('INSERT INTO imports(digest,imported_at,source) VALUES (?,?,?)',
                                       (digest, datetime.now(UTC).isoformat(), source)).lastrowid
            for row in rows:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError('Malformed CSV row')
                if not row['external_id'].strip() or not row['category'].strip():
                    raise ValueError('ID and category required')
                account = self.db.execute('SELECT * FROM accounts WHERE id=?', (row['account_id'],)).fetchone()
                if not account or row['currency'] != account['currency']:
                    raise ValueError('Unknown account or currency mismatch')
                booked = date.fromisoformat(row['date'])
                if booked <= date.fromisoformat(account['opening_date']):
                    raise ValueError('Transaction must follow opening date')
                amount = format(money(row['amount']), '.2f')
                values = (row['account_id'], row['external_id'], booked.isoformat(), amount,
                          row['currency'], row['category'], row['transfer_id'])
                existing = self.db.execute('SELECT account_id,external_id,date,amount,currency,category,transfer_id '
                                           'FROM transactions WHERE account_id=? AND external_id=?', values[:2]).fetchone()
                if existing:
                    if tuple(existing) != values:
                        raise ValueError('Conflicting external transaction ID')
                    continue
                self.db.execute('INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)', (*values, import_id))
                inserted += 1
            transfers = self.db.execute("SELECT * FROM transactions WHERE transfer_id != ''").fetchall()
            for transfer_id in {row['transfer_id'] for row in transfers}:
                pair = [row for row in transfers if row['transfer_id'] == transfer_id]
                if (len(pair) != 2 or pair[0]['account_id'] == pair[1]['account_id']
                        or pair[0]['currency'] != pair[1]['currency']
                        or sum(money(row['amount']) for row in pair) != 0):
                    raise ValueError('Transfer requires two opposite postings on different accounts')
        return inserted

    def status(self, as_of):
        cutoff = date.fromisoformat(as_of)
        as_of = cutoff.isoformat()
        accounts = self.db.execute('SELECT * FROM accounts').fetchall()
        if any(date.fromisoformat(row['opening_date']) > cutoff for row in accounts):
            raise ValueError('Status predates an opening balance')
        if len({row['opening_date'] for row in accounts}) > 1:
            raise ValueError('Common opening date required in Phase 1')
        balances = {row['id']: money(row['opening']) for row in accounts}
        income = expenses = Decimal(0)
        for row in self.db.execute(
                'SELECT t.*,m.pair_id AS correction_pair_id,c.description FROM transactions t '
                'LEFT JOIN transfer_correction_members m '
                'ON m.account_id=t.account_id AND m.external_id=t.external_id '
                'LEFT JOIN transaction_context c '
                'ON c.account_id=t.account_id AND c.external_id=t.external_id '
                'WHERE t.date <= ?', (as_of,)):
            amount = money(row['amount'])
            balances[row['account_id']] += amount
            from .transfer_corrections import effective_transfer_id
            if not effective_transfer_id(self, row):
                income += max(amount, Decimal(0))
                expenses += max(-amount, Decimal(0))
        by_owner = {row['owner']: Decimal(0) for row in accounts}
        for row in accounts:
            if row['kind'] != 'DEPOT':
                by_owner[row['owner']] += balances[row['id']]
        return {'currency': 'EUR', 'as_of': as_of, 'balances': balances,
                'liquidity_by_owner': by_owner, 'liquidity': sum(by_owner.values(), Decimal(0)),
                'income': income, 'expenses': expenses, 'net_cashflow': income - expenses}

    def save_scenario(self, name, plan, opening, as_of):
        as_of = date.fromisoformat(as_of).isoformat()
        opening = money(opening)
        payload = json.dumps({'version': 1, 'income': str(plan.income), 'expenses': str(plan.expenses),
                              'reserve': str(plan.reserve),
                              'one_offs': [(month, str(amount)) for month, amount in plan.one_offs],
                              'opening': str(opening), 'as_of': as_of, 'engine': 'monthly-v1',
                              'result': forecast(opening, plan)}, default=str)
        with self.db:
            self.db.execute('INSERT INTO scenarios VALUES (?,?)', (name, payload))

    def load_scenario(self, name):
        row = self.db.execute('SELECT payload FROM scenarios WHERE id=?', (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        values = json.loads(row[0])
        if values.pop('version') != 1:
            raise ValueError('Unsupported scenario version')
        for key in ('opening', 'as_of', 'engine', 'result'):
            values.pop(key)
        return Plan(**values)

    def compare_scenarios(self, first, second):
        saved = []
        for name in (first, second):
            row = self.db.execute('SELECT payload FROM scenarios WHERE id=?', (name,)).fetchone()
            if row is None:
                raise KeyError(name)
            saved.append(json.loads(row[0]))
        if any(saved[0][key] != saved[1][key] for key in ('opening', 'as_of', 'engine', 'version')):
            raise ValueError('Scenario comparison requires the same baseline and engine')
        return [{'month': a['month'], 'first': money(a['liquidity']),
                 'second': money(b['liquidity']),
                 'difference': money(b['liquidity']) - money(a['liquidity'])}
                for a, b in zip(saved[0]['result'], saved[1]['result'])]

    def delete_scenario(self, name):
        with self.db:
            self.db.execute('DELETE FROM scenarios WHERE id=?', (name,))
