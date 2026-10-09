# mypy: disable-error-code="import-untyped"
"""Offline document intake for stored mail bytes; no mailbox access."""
import hashlib
import os
import secrets
import sqlite3
from html.parser import HTMLParser
from pathlib import Path

from .classification import auto_confirm_documents, auto_link_documents, register_document
from .import_preview import outside_repository

MAX_RAW_BYTES = 16 * 1024 * 1024
MAX_TEXT_CHARS = 500_000
MAX_ATTACHMENTS = 100
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 20


class DocumentIntakeError(ValueError):
    """Safe validation error without source content."""


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {'script', 'style', 'noscript'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag.lower() in {'script', 'style', 'noscript'} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _html_text(value):
    parser = _PlainHTML()
    parser.feed(value)
    parser.close()
    return ' '.join(part.strip() for part in parser.parts if part.strip())


def _supplier(supplier):
    if supplier is None:
        try:
            from invoice_mail_archive.invoice_values import (
                extract_invoice_candidate,
            )
            from invoice_mail_archive.mail import (
                parse_message,
            )
        except ImportError as error:
            raise DocumentIntakeError('invoice-mail-archive ist nicht installiert.') from error
        try:
            from invoice_mail_archive.invoice_likeness import (
                assess_invoice_likeness,
            )
        except ImportError:
            assess_invoice_likeness = None
        return parse_message, extract_invoice_candidate, assess_invoice_likeness
    if isinstance(supplier, (tuple, list)) and len(supplier) == 2:
        return supplier[0], supplier[1], supplier[2] if len(supplier) > 2 else None
    try:
        return supplier.parse_message, supplier.extract_invoice_candidate, getattr(supplier, 'assess_invoice_likeness', None)
    except AttributeError as error:
        raise DocumentIntakeError('Ungültiger synthetischer Mail-Lieferant.') from error


def _pdf_text(content, pdf_extractor):
    if len(content) > MAX_PDF_BYTES:
        return '', ['pdf_too_large']
    if pdf_extractor is not None:
        try:
            value = str(pdf_extractor(content, MAX_PDF_PAGES, MAX_TEXT_CHARS))
            return value[:MAX_TEXT_CHARS], ['text_too_large'] if len(value) > MAX_TEXT_CHARS else []
        except Exception:  # noqa: BLE001 - optional PDF supplier is untrusted input.
            return '', ['pdf_extraction_failed']
    try:
        import pymupdf as fitz
    except ImportError:
        return '', ['pdf_extraction_unavailable']
    try:
        document = fitz.open(stream=content, filetype='pdf')
        try:
            if document.page_count > MAX_PDF_PAGES:
                return '', ['pdf_too_many_pages']
            parts, size = [], 0
            for index in range(document.page_count):
                part = document.load_page(index).get_text()
                if size + len(part) > MAX_TEXT_CHARS:
                    parts.append(part[:MAX_TEXT_CHARS - size])
                    return ''.join(parts), ['text_too_large']
                parts.append(part)
                size += len(part)
            text = ''.join(parts)
        finally:
            document.close()
        return text[:MAX_TEXT_CHARS], []
    except Exception:  # noqa: BLE001 - optional PDF parser failures become safe warnings.
        return '', ['pdf_extraction_failed']


def _candidate(text, document_hash, extract, *, blocked=False):
    warnings = []
    values = {'vendor_name': None, 'invoice_number': None, 'invoice_date': None,
              'gross_total': None, 'currency': None}
    if blocked or not text.strip():
        return values, ['empty_source']
    try:
        from .invoice_import import parse_invoice_candidate
        payload = extract(text, source_document_sha256=document_hash)
        if (payload.get('source_document_sha256') != document_hash
                or payload.get('source_text_sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest()):
            return values, ['candidate_hash_mismatch']
        parsed = parse_invoice_candidate(payload)
        for key in values:
            value = parsed.values.get(key)
            values[key] = None if value is None else str(value)
        warnings.extend(parsed.warnings)
    except Exception:  # noqa: BLE001 - candidate supplier failures must not expose source data.
        warnings.append('candidate_extraction_failed')
    return values, sorted(set(warnings))


def inspect_documents(raw, *, supplier=None, pdf_extractor=None):
    """Inspect RFC822 bytes and bounded document text; every result remains unreviewed."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RAW_BYTES:
        raise DocumentIntakeError('Ungültige Maildaten oder Größenlimit überschritten.')
    parse_message, extract, assess = _supplier(supplier)
    mail_hash = hashlib.sha256(raw).hexdigest()
    try:
        message = parse_message(raw)
        attachments = list(getattr(message, 'attachments', ()))
        if len(attachments) > MAX_ATTACHMENTS:
            raise DocumentIntakeError('Zu viele Anhänge.')
    except DocumentIntakeError:
        raise
    except Exception as error:
        raise DocumentIntakeError('Mail konnte nicht sicher ausgewertet werden.') from error
    sources = []
    if getattr(message, 'plaintext', None):
        sources.append(('mail_body', str(message.plaintext).encode('utf-8'), str(message.plaintext), []))
    if getattr(message, 'html', None):
        text = _html_text(str(message.html))
        sources.append(('mail_html', text.encode('utf-8'), text, ['html_converted_to_plain']))
    for attachment in attachments:
        content = getattr(attachment, 'content', b'')
        mime = str(getattr(attachment, 'mime_type', '')).lower()
        if not isinstance(content, bytes):
            continue
        if mime == 'text/plain':
            try:
                text = content.decode('utf-8-sig')
                sources.append(('text_attachment', content, text, []))
            except UnicodeDecodeError:
                sources.append(('text_attachment', content, '', ['unsupported_text_encoding']))
        elif mime == 'text/html':
            text = _html_text(content.decode('utf-8', errors='replace'))
            sources.append(('html_attachment', content, text, ['html_converted_to_plain']))
        elif mime == 'application/pdf':
            text, warnings = _pdf_text(content, pdf_extractor)
            sources.append(('pdf_attachment', content, text, warnings))
    subject = getattr(message, 'subject', None)
    documents, seen = [], set()
    for kind, digest_bytes, text, initial_warnings in sources:
        document_hash = hashlib.sha256(digest_bytes).hexdigest()
        if document_hash in seen:
            continue
        seen.add(document_hash)
        if len(text) > MAX_TEXT_CHARS:
            initial_warnings = list(initial_warnings) + ['text_too_large']
        text = text[:MAX_TEXT_CHARS]
        if not text.strip() and kind != 'pdf_attachment':
            continue
        normalized = ' '.join(text.casefold().split())
        normalized_subject = ' '.join(subject.casefold().split()) if isinstance(subject, str) else ''
        explicitly_not_invoice = 'bitte beachten sie, dass dies keine rechnung ist' in normalized
        booking_confirmation = (
            kind in {'mail_body', 'mail_html'}
            and 'buchung' in normalized_subject
            and ('bestätigt' in normalized_subject or 'bestaetigt' in normalized_subject)
            and all(marker in normalized for marker in ('buchungsnummer', 'anreise', 'abreise'))
        )
        if explicitly_not_invoice or booking_confirmation:
            continue
        if assess is not None and text.strip():
            try:
                likeness = assess(text)
                if not (likeness if isinstance(likeness, bool) else likeness.is_invoice_like):
                    continue
            except Exception:  # noqa: BLE001,S112 - failed likeness assessment is unsafe; drop candidate.
                continue
        values, warnings = _candidate(text, document_hash, extract, blocked='text_too_large' in initial_warnings)
        title = values['invoice_number']
        if title is None and isinstance(subject, str) and subject.strip():
            title = subject.strip()[:240]
            warnings.append('title_from_header_needs_review')
        documents.append({'source_reference': f'mail:{mail_hash}:{document_hash}', 'kind': 'invoice',
                           'vendor': values['vendor_name'], 'title': title,
                           'date': values['invoice_date'],
                           'amount': values['gross_total'] if values['gross_total'] is not None and values['currency'] == 'EUR' else None,
                           'currency': values['currency'] if values['gross_total'] is not None and values['currency'] == 'EUR' else None,
                           'warnings': sorted(set(initial_warnings + warnings)),
                           'status': 'unreviewed', 'text': text})
    return documents


def _document_root(database):
    database = outside_repository(Path(database))
    root = outside_repository(database.parent / (database.stem + '-documents'))
    if not root.is_relative_to(database.parent):
        raise DocumentIntakeError('Dokumentenordner umgeleitet.')
    root.mkdir(parents=True, exist_ok=True)
    return database, root


def _cache(root, document_id, text, *, replace_orphan=False):
    if type(document_id) is not int or document_id < 1 or not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise DocumentIntakeError('Ungültiger Dokumentcache.')
    target = outside_repository(root / f'{document_id}.txt')
    if not target.is_relative_to(root):
        raise DocumentIntakeError('Dokumentcache umgeleitet.')
    temporary = root / f'.{document_id}.{secrets.token_hex(8)}.tmp'
    if target.exists():
        existing = target.read_text(encoding='utf-8')
        if existing == text:
            return
        if existing and not replace_orphan:
            raise DocumentIntakeError('Dokumentcache bereits mit anderem Inhalt vorhanden.')
    try:
        with temporary.open('x', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def cache_document_source(database, document_id, text):
    """Persist validated plaintext in the external cache belonging to a database."""
    _, root = _document_root(database)
    _cache(root, document_id, text)


def import_documents(store, database, documents):
    """Register and cache each new candidate before its database commit.

    Filesystem and SQLite commits cannot be atomic across a process crash. A
    crash may leave an unreferenced cache, which a later new ID may replace.
    Earlier documents in the same call remain committed after a later failure.
    """
    if not isinstance(documents, list):
        raise DocumentIntakeError('Dokumentliste erwartet.')
    _, root = _document_root(database)
    results = []
    new_ids = []
    for item in documents:
        if not isinstance(item, dict) or item.get('status') != 'unreviewed':
            raise DocumentIntakeError('Nur unreviewte Dokumente dürfen registriert werden.')
        amount = item.get('amount')
        currency = item.get('currency')
        if amount is None or currency != 'EUR':
            amount, currency = None, None
        data = {'kind': item.get('kind'), 'vendor': item.get('vendor') or 'Unbekannt',
                'title': item.get('title') or 'Unbenanntes Dokument', 'document_date': item.get('date'),
                'amount': amount, 'currency': currency,
                'source_reference': item.get('source_reference'), 'warnings': item.get('warnings', []),
                'status': 'unreviewed'}
        source = data['source_reference']
        existing = store.db.execute('SELECT * FROM classification_documents WHERE source_reference=?', (source,)).fetchone()
        if existing is None:
            text = item.get('text', '')

            def cache_before_commit(document_id):
                _cache(root, document_id, text, replace_orphan=True)

            def remove_uncommitted_cache(document_id):
                target = root / f'{document_id}.txt'
                try:
                    # A rejected redirect must not be followed while cleaning up.
                    if target.is_symlink():
                        return
                    resolved = outside_repository(target)
                    if (resolved != target or not resolved.is_relative_to(root)
                            or not resolved.is_file()):
                        return
                    if resolved.read_text(encoding='utf-8') == text:
                        resolved.unlink()
                except (OSError, ValueError, UnicodeError):
                    # Preserve the original cache/write error; an orphan can be
                    # replaced on a later attempt using the same uncommitted ID.
                    return

            result = register_document(
                store, data, before_commit=cache_before_commit,
                on_failure=remove_uncommitted_cache)['document']
            new_ids.append(result['id'])
        else:
            result = dict(existing)
            _cache(root, result['id'], item.get('text', ''))
        results.append(result)
    auto_review = auto_confirm_documents(store, {'confirmed': True, 'ids': new_ids})
    by_id = {item['id']: item for item in auto_review['confirmed']}
    results = [by_id.get(item['id'], item) for item in results]
    # A new document may provide the missing evidence for exactly one booking.
    # Link first, so the local category review can use the confirmed receipt title.
    auto_links = auto_link_documents(store, {'confirmed': True, 'ids': new_ids})
    from .local_model import review_transactions
    keys = [{'account_id': link['account_id'], 'external_id': link['external_id']}
            for link in auto_links['linked']]
    try:
        model_review = review_transactions(store, keys)
    except Exception:
        model_review = {'status': 'failed', 'counts': {}, 'reviewed': 0,
                        'message': 'Lokale Modellprüfung fehlgeschlagen; Belegimport blieb erhalten.'}
    return {'documents': results, 'registered': len(results), 'auto_review': auto_review,
            'auto_links': auto_links,
            'model_review': model_review}


def read_document_source(database, document_id):
    database, root = _document_root(database)
    if type(document_id) is not int or document_id < 1:
        raise DocumentIntakeError('Ungültige Dokumentkennung.')
    connection = sqlite3.connect(database)
    try:
        if connection.execute('SELECT 1 FROM classification_documents WHERE id=?', (document_id,)).fetchone() is None:
            raise DocumentIntakeError('Unbekanntes Dokument.')
    finally:
        connection.close()
    path = outside_repository(root / f'{document_id}.txt')
    if not path.is_relative_to(root) or not path.is_file():
        raise DocumentIntakeError('Dokumentquelle nicht vorhanden.')
    with path.open('r', encoding='utf-8') as source:
        value = source.read(MAX_TEXT_CHARS + 1)
    if len(value) > MAX_TEXT_CHARS:
        raise DocumentIntakeError('Dokumentquelle überschreitet das Textlimit.')
    return value
