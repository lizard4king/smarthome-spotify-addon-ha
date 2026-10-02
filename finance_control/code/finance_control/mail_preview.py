"""Offline EML inspection through the separately installed invoice-mail-archive."""
import hashlib
import sys

from .import_preview import outside_repository

SUPPLIER_COMMIT = '4a8f43ff156dd3987647c89367c8e5aadf8a6bdc'
MAX_EML_BYTES = 16 * 1024 * 1024
MAX_TEXT_CHARS = 500_000
MAX_INVENTORY_TEXT_BYTES = 1_000_000
MAX_ATTACHMENTS = 100
DOCUMENT_TEXT_SCHEMA = 'document_text_candidate_v1'
DOCUMENT_TEXT_STATUSES = {
    'success', 'empty', 'unsupported', 'content_too_large',
    'page_limit_exceeded', 'pixel_limit_exceeded', 'text_limit_exceeded',
    'invalid_document', 'encrypted_document', 'ocr_unavailable',
    'ocr_timeout', 'extraction_error',
}
_DEFAULT_OCR = object()


class MailPreviewError(ValueError):
    """Safe message without personal source content."""


def _load_supplier():
    if sys.version_info < (3, 12):
        raise MailPreviewError('Die optionale Mail-Vorschau benötigt Python 3.12 oder neuer.')
    try:
        from invoice_mail_archive.invoice_likeness import assess_invoice_likeness
        from invoice_mail_archive.mail import parse_message
    except ImportError:
        raise MailPreviewError('invoice-mail-archive ist in dieser Umgebung nicht installiert.') from None
    return parse_message, assess_invoice_likeness


def _load_document_text_supplier():
    try:
        from invoice_mail_archive.document_text import (
            DocumentTextLimits,
            extract_document_text,
        )
    except ImportError:
        raise MailPreviewError(
            'Die installierte invoice-mail-archive-Version unterstützt keine Dokumenttextextraktion.'
        ) from None
    return extract_document_text, DocumentTextLimits


def _default_local_ocr():
    """Build the optional local-only OCR backend; absence fails closed."""
    try:
        from invoice_mail_archive.ocr_extraction import TesseractOcrBackend
    except ImportError:
        return None
    return TesseractOcrBackend()


def _validated_document_text(content, mime_type, *, ocr=None):
    """Call and independently validate the versioned bytes-only supplier contract."""
    extract, limits_type = _load_document_text_supplier()
    expected_document_hash = hashlib.sha256(content).hexdigest()
    expected_mime_type = str(mime_type or '').split(';', 1)[0].strip().casefold()
    expected_mime_type = expected_mime_type or 'application/octet-stream'
    try:
        candidate = extract(
            content,
            mime_type,
            limits=limits_type(
                max_content_bytes=MAX_EML_BYTES,
                max_pages=100,
                max_text_chars=MAX_TEXT_CHARS,
            ),
            ocr=ocr,
        )
        payload = candidate.model_dump(mode='json')
    except Exception:  # noqa: BLE001 - supplier errors must not expose document content
        raise MailPreviewError('Dokument konnte nicht sicher ausgewertet werden.') from None
    required = {
        'schema_version', 'record_type', 'document_sha256', 'mime_type', 'text',
        'text_sha256', 'method', 'page_count', 'status', 'quality', 'warnings',
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise MailPreviewError('Unbekannter Dokumenttext-Vertrag.')
    quality = payload.get('quality')
    expected_quality = {
        'char_count', 'word_count', 'line_count', 'mean_confidence',
        'low_confidence_ratio', 'peak_image_pixels',
    }
    if (payload.get('schema_version') != DOCUMENT_TEXT_SCHEMA
            or payload.get('record_type') != 'document_text_candidate'
            or payload.get('document_sha256') != expected_document_hash
            or payload.get('mime_type') != expected_mime_type
            or payload.get('status') not in DOCUMENT_TEXT_STATUSES
            or payload.get('method') not in {None, 'pymupdf', 'ocr'}
            or type(payload.get('page_count')) is not int
            or payload['page_count'] < 0
            or not isinstance(quality, dict)
            or set(quality) != expected_quality
            or any(type(quality.get(key)) is not int or quality[key] < 0
                   for key in ('char_count', 'word_count', 'line_count'))
            or not isinstance(payload.get('warnings'), list)
            or not all(isinstance(item, str) for item in payload['warnings'])):
        raise MailPreviewError('Ungültiger Dokumenttext-Vertrag.')
    mean_confidence = quality.get('mean_confidence')
    low_confidence_ratio = quality.get('low_confidence_ratio')
    peak_image_pixels = quality.get('peak_image_pixels')
    if ((mean_confidence is not None
         and (isinstance(mean_confidence, bool) or not isinstance(mean_confidence, (int, float))
              or not 0 <= mean_confidence <= 100))
            or (low_confidence_ratio is not None
                and (isinstance(low_confidence_ratio, bool)
                     or not isinstance(low_confidence_ratio, (int, float))
                     or not 0 <= low_confidence_ratio <= 1))
            or (peak_image_pixels is not None
                and (type(peak_image_pixels) is not int or peak_image_pixels < 1))):
        raise MailPreviewError('Ungültiger Dokumenttext-Vertrag.')
    text = payload.get('text')
    if payload['status'] == 'success':
        if (not isinstance(text, str) or not text
                or payload.get('method') not in {'pymupdf', 'ocr'}
                or payload['page_count'] < 1
                or payload.get('text_sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest()
                or quality['char_count'] != len(text)
                or len(text) > MAX_TEXT_CHARS):
            raise MailPreviewError('Ungültiger Dokumenttext-Vertrag.')
    elif (text is not None or payload.get('text_sha256') is not None
          or any(quality[key] for key in ('char_count', 'word_count', 'line_count'))):
        raise MailPreviewError('Ungültiger Dokumenttext-Vertrag.')
    return payload


def inspect_eml(path, *, ocr=_DEFAULT_OCR):
    """No accounts, mailbox connections, extraction to disk, HTML rendering or ledger writes."""
    path = outside_repository(path)
    if path.stat().st_size > MAX_EML_BYTES:
        raise MailPreviewError('EML-Datei überschreitet 16 MiB.')
    raw = path.read_bytes()
    return inspect_raw(raw, ocr=ocr)


def inspect_raw(raw, *, include_candidates=False, ocr=_DEFAULT_OCR):
    """Inspect stored RFC822 bytes; candidate values are for private output only."""
    if not isinstance(raw, bytes) or len(raw) > MAX_EML_BYTES:
        raise MailPreviewError('Ungültige Maildaten oder mehr als 16 MiB.')
    parse_message, assess = _load_supplier()
    if ocr is _DEFAULT_OCR:
        ocr = _default_local_ocr()
    try:
        message = parse_message(raw)
        if len(message.attachments) > MAX_ATTACHMENTS:
            raise MailPreviewError('Zu viele Anhänge.')
        documents = []

        def inspect_text(text, kind, digest, *, extraction=None):
            if len(text) > MAX_TEXT_CHARS:
                raise MailPreviewError('Text überschreitet die Vorschaugrenze.')
            likeness = assess(text)
            document = {'kind': kind, 'sha256': digest,
                        'status': 'invoice_candidate' if likeness.is_invoice_like else 'not_invoice_like',
                        'requires_review': True}
            if extraction is not None:
                document['extraction'] = extraction
                document['warnings'] = list(extraction['warnings'])
                if extraction['method'] == 'ocr':
                    document['warnings'].append('ocr_output_unreviewed')
            documents.append(document)
            if include_candidates and likeness.is_invoice_like:
                from invoice_mail_archive.invoice_values import (
                    extract_invoice_candidate,
                )

                from .invoice_import import parse_invoice_candidate
                payload = extract_invoice_candidate(text, source_document_sha256=digest)
                validated = parse_invoice_candidate(payload)
                if (validated.document_hash != digest
                        or validated.text_hash != hashlib.sha256(text.encode('utf-8')).hexdigest()):
                    raise MailPreviewError('Ungültiger Rechnungskandidat.')
                documents[-1]['candidate'] = payload
                documents[-1]['validation_warnings'] = list(validated.warnings)
                # Private inventory output only; bounded above and hash-bound by the candidate.
                if len(text.encode('utf-8')) <= MAX_INVENTORY_TEXT_BYTES:
                    documents[-1]['source_text'] = text
            return document

        if message.plaintext:
            inspect_text(message.plaintext, 'mail_body', hashlib.sha256(raw).hexdigest())
        for attachment in message.attachments:
            digest = hashlib.sha256(attachment.content).hexdigest()
            mime_type = str(attachment.mime_type or '').split(';', 1)[0].strip().casefold()
            # Do not interpret user-controlled filenames as paths or execute attachments.
            if mime_type == 'text/plain':
                try:
                    text = attachment.content.decode('utf-8-sig', errors='strict')
                except UnicodeDecodeError:
                    documents.append({'kind': 'attachment', 'sha256': digest,
                                      'status': 'unsupported_text_encoding', 'requires_review': True})
                else:
                    inspect_text(text, 'text_attachment', digest)
            elif mime_type == 'application/pdf' or mime_type.startswith('image/'):
                extracted = _validated_document_text(
                    attachment.content, mime_type, ocr=ocr,
                )
                provenance = {
                    'schema_version': extracted['schema_version'],
                    'method': extracted['method'],
                    'status': extracted['status'],
                    'page_count': extracted['page_count'],
                    'quality': extracted['quality'],
                    'warnings': extracted['warnings'],
                }
                if extracted['status'] == 'success':
                    inspect_text(extracted['text'], 'document_attachment', digest,
                                 extraction=provenance)
                else:
                    documents.append({'kind': 'attachment', 'sha256': digest,
                                      'status': extracted['status'], 'requires_review': True,
                                      'extraction': provenance,
                                      'warnings': list(extracted['warnings'])})
            else:
                documents.append({'kind': 'attachment', 'sha256': digest,
                                  'status': 'unsupported', 'requires_review': True})
        return {'source_sha256': hashlib.sha256(raw).hexdigest(),
                'supplier_expected_commit': SUPPLIER_COMMIT,
                'supplier_revision_verified': False,
                'ledger_written': False, 'mailbox_accessed': False,
                'attachments': len(message.attachments), 'documents': documents,
                'html_present': bool(message.html), 'html_rendered': False,
                'invoice_candidates': sum(d['status'] == 'invoice_candidate' for d in documents),
                'warnings': ['candidate_detection_only_no_amount_or_payment_confirmation']}
    except MailPreviewError:
        raise
    except Exception:  # noqa: BLE001 - supplier failures must not expose private mail content
        raise MailPreviewError('Mail konnte nicht sicher ausgewertet werden.') from None
