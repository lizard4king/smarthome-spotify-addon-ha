"""Cent-conserving attribution by stored account ownership, never spending causation."""
from decimal import ROUND_DOWN, Decimal


def person_label(person_id):
    """Use human labels for the two legacy profile ids when no profile is available."""
    return {'ANDREAS': 'Andreas', 'ERLENE': 'Erlene'}.get(person_id, person_id)


def account_allocations(store, people):
    """Attribute personal accounts to their owner and joint accounts to JOINT.

    Stored household shares describe financing, not the account used for a
    posting. Missing or unknown owners remain unresolved rather than guessed.
    """
    return {
        account['id']: {account['owner'] if account['owner'] == 'JOINT'
                        or account['owner'] in people else None: Decimal(1)}
        for account in store.accounts()
    }


def split_cents(value, shares):
    """Conserve each booking's cents; largest remainder, stable person-id ties."""
    cents = abs(value) * 100
    exact = {person: cents * share for person, share in shares.items()}
    allocated = {person: part.to_integral_value(rounding=ROUND_DOWN)
                 for person, part in exact.items()}
    remainder = int(cents - sum(allocated.values()))
    order = sorted(shares, key=lambda person: (-(exact[person] - allocated[person]), person or ''))
    for person in order[:remainder]:
        allocated[person] += 1
    sign = Decimal(-1) if value < 0 else Decimal(1)
    return {person: sign * part / 100 for person, part in allocated.items()}
