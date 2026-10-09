"""Calendar labels and explicit input validation around the existing monthly engine."""
import calendar
import csv
import io
from datetime import date

from .core import Plan, forecast, money, parse_money_input


def month_end(value):
    day = date.fromisoformat(value)
    if day.day != calendar.monthrange(day.year, day.month)[1]:
        raise ValueError('Für volle Planmonate bitte einen Monatsend-Stichtag wählen.')
    return day


def decimal_text(value):
    """Accept a decimal comma or decimal point, never ambiguous thousands grouping."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Betrag fehlt.')
    try:
        return parse_money_input(value)
    except ValueError as error:
        raise ValueError(
            'Betrag benötigt höchstens zwei Nachkommastellen ohne Tausendertrennung.'
        ) from error


def parse_plan(values):
    entries = values.get('one_offs', [])
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError('Maximal 100 Einmalzahlungen.')
    one_offs = []
    for entry in entries:
        offset = entry['month']
        if type(offset) is not int or not 1 <= offset <= 12:
            raise ValueError('Einmalzahlungen benötigen einen Planmonat von 1 bis 12.')
        one_offs.append((offset, decimal_text(entry['amount'])))
    return Plan(decimal_text(values['income']), decimal_text(values['expenses']),
                decimal_text(values['reserve']), tuple(one_offs))


def projection(opening, as_of, plan):
    cutoff = month_end(as_of)
    rows = forecast(opening, plan)
    balance = money(opening)
    for row in rows:
        year, month0 = divmod(cutoff.year * 12 + cutoff.month - 1 + row['month'], 12)
        row.update(period=f'{year:04d}-{month0 + 1:02d}', opening=balance,
                   income=plan.income, expenses=plan.expenses,
                   one_offs=sum((amount for offset, amount in plan.one_offs
                                 if offset == row['month']), money('0')),
                   reserve=plan.reserve)
        balance = row['liquidity']
    return rows


def export_projection(rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=';', lineterminator='\r\n')
    writer.writerow(['Monat', 'Anfang_EUR', 'Einnahmen_EUR', 'Ausgaben_EUR',
                     'Einmalzahlungen_EUR', 'Cashflow_EUR', 'Ende_EUR', 'Reserve_EUR'])
    for row in rows:
        writer.writerow([row['period']] + [format(row[key], '.2f').replace('.', ',') for key in
                        ('opening', 'income', 'expenses', 'one_offs', 'cashflow', 'liquidity', 'reserve')])
    return output.getvalue()
