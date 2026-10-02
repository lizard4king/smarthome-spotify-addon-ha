"""Purely local booking-first filter for mail invoice candidates."""
from datetime import date, timedelta

from .classification import document_recipient_matches
from .core import money
from .transfer_corrections import effective_transfer_id


def booking_match_statuses(store, documents, max_days=45):
    """Classify document identities as unique, unmatched or ambiguous.

    A unique result requires both one matching open payment for the document and
    one distinct document identity for that payment.  This function never writes.
    """
    if type(max_days) is not int or not 0 <= max_days <= 45:
        raise ValueError('invalid_booking_match_days')
    if not isinstance(documents, dict):
        raise TypeError('invalid_booking_candidates')

    payments = []
    rows = store.db.execute(
        "SELECT t.*,coalesce(c.counterparty,'') AS counterparty,"
        "coalesce(c.description,'') AS description FROM transactions t "
        "LEFT JOIN transaction_context c USING(account_id,external_id) "
        "WHERE t.date>='2026-01-01' AND t.currency='EUR' "
        "AND NOT EXISTS (SELECT 1 FROM classification_document_links l "
        "JOIN classification_documents d ON d.id=l.document_id "
        "WHERE l.account_id=t.account_id AND l.external_id=t.external_id "
        "AND d.kind='invoice') ORDER BY t.date,t.account_id,t.external_id"
    )
    for row in rows:
        if money(row['amount']) < 0 and not effective_transfer_id(store, row):
            payments.append(row)

    matches = {}
    for identity, document in documents.items():
        if (not isinstance(identity, str) or not isinstance(document, dict)
                or document.get('amount') is None or document.get('currency') != 'EUR'
                or document.get('document_date') is None):
            matches[identity] = []
            continue
        issued = date.fromisoformat(document['document_date'])
        amount = money(document['amount'])
        matches[identity] = []
        for payment in payments:
            booked = date.fromisoformat(payment['date'])
            if (money(payment['amount']) == -amount
                    and 0 <= (booked - issued).days <= max_days
                    and document_recipient_matches(
                        document['vendor'], payment['counterparty'], payment['description'],
                        document['title'])):
                matches[identity].append((payment['account_id'], payment['external_id']))

    payment_documents = {}
    for identity, candidates in matches.items():
        for payment in candidates:
            payment_documents.setdefault(payment, set()).add(identity)
    for payment in payments:
        key = (payment['account_id'], payment['external_id'])
        payment_date = date.fromisoformat(payment['date'])
        existing_rows = store.db.execute(
            "SELECT d.*,f.document_sha256 FROM classification_documents d "
            "LEFT JOIN classification_document_fingerprints f ON f.document_id=d.id "
            "WHERE d.kind='invoice' AND d.amount=? AND d.currency='EUR' "
            "AND d.document_date IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM classification_document_links l WHERE l.document_id=d.id)",
            (format(-money(payment['amount']), '.2f'),),
        ).fetchall()
        for existing in existing_rows:
            issued = date.fromisoformat(existing['document_date'])
            if (not issued <= payment_date <= issued + timedelta(days=max_days)
                    or not document_recipient_matches(
                        existing['vendor'], payment['counterparty'], payment['description'],
                        existing['title'])):
                continue
            document_id = existing['id']
            equivalent = {
                identity for identity in documents
                if (existing['document_sha256'] == identity
                    or existing['source_reference'] == f'mail-document:{identity}'
                    or existing['source_reference'].endswith(':' + identity))
            }
            if not equivalent:
                payment_documents.setdefault(key, set()).add(f'existing:{document_id}')

    result = {}
    for identity, candidates in matches.items():
        if not candidates:
            result[identity] = {'status': 'unmatched', 'booking': None}
        elif len(candidates) != 1 or len(payment_documents[candidates[0]]) != 1:
            result[identity] = {'status': 'ambiguous', 'booking': None}
        else:
            result[identity] = {'status': 'unique', 'booking': candidates[0]}
    return result
