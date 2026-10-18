"""Explicit-instruction-only Notion writes; deliberately not a model tool.

The caller supplies a server-owned token and allowlist. A request ID is durably
reserved before any request: interrupted or ambiguous operations require human
reconciliation and are never automatically submitted again.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

try:
    from .lumi_knowledge import KnowledgeStore
    from .lumi_notion_sync import API_ROOT, NOTION_VERSION, _NoRedirect, _page_uuid
except ImportError:  # direct script invocation from codex_voice/
    from lumi_knowledge import KnowledgeStore
    from lumi_notion_sync import API_ROOT, NOTION_VERSION, _NoRedirect, _page_uuid

MAX_TEXT = 1800
MAX_RESPONSE_BYTES = 262144
TIMEOUT_SECONDS = 5.0
MAX_TOTAL_SECONDS = 15.0


def _transport(method, url, headers, body, timeout):
    request = Request(url, data=body, headers=headers, method=method)
    try:
        return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)
    except HTTPError as response:
        return response


class NotionWriter:
    """Bounded paragraph creation/replacement on explicitly approved pages."""

    def __init__(self, database, token, allowed_page_ids, *, transport=_transport,
                 monotonic=time.monotonic):
        if not isinstance(token, str) or not token or any(c in token for c in '\r\n'):
            raise ValueError('Notion write token is required')
        if not isinstance(allowed_page_ids, (list, tuple, set, frozenset)):
            raise ValueError('explicit page allowlist required')
        if len(allowed_page_ids) > 10000:
            raise ValueError('too many allowed pages')
        self._allowed = frozenset(_page_uuid(item) for item in allowed_page_ids)
        self._store = KnowledgeStore(database)
        self._token = token
        self._transport = transport
        self._monotonic = monotonic
        with closing(self._store._connect()) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS notion_write_operations ('
                               'request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, '
                               'result TEXT NOT NULL)')
            connection.commit()

    def create_note(self, page_id, title, text, request_id, *, explicit_instruction=False):
        """Append one note paragraph; title and body remain plain text."""
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError('title must contain 1 to 200 characters')
        return self._write('create', page_id, None, title + '\n' + text
                           if isinstance(text, str) else text, request_id,
                           explicit_instruction)

    def update_note(self, page_id, block_id, text, request_id, *, explicit_instruction=False):
        """Replace exactly one direct paragraph after verifying its page parent."""
        return self._write('update', page_id, _page_uuid(block_id), text, request_id,
                           explicit_instruction)

    def _write(self, operation, page_id, block_id, text, request_id, explicit):
        page_id = _page_uuid(page_id)
        if explicit is not True or page_id not in self._allowed:
            raise ValueError('explicit instruction and approved page are required')
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError('note must contain 1 to 1800 characters including title')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id):
            raise ValueError('request_id must contain 8 to 128 safe characters')
        digest = hashlib.sha256(json.dumps([operation, page_id, block_id, text],
                                          ensure_ascii=True).encode()).hexdigest()
        unknown = {'status': 'unknown', 'operation_id': request_id}
        connection = self._store._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT digest,result FROM notion_write_operations '
                                     'WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise ValueError('request_id already belongs to different content')
                return json.loads(row['result'])
            connection.execute('INSERT INTO notion_write_operations VALUES(?,?,?)',
                               (request_id, digest, json.dumps(unknown)))
            connection.commit()
        finally:
            connection.close()
        deadline = self._monotonic() + MAX_TOTAL_SECONDS
        result = unknown
        try:
            if operation == 'update':
                status, block = self._request('GET', f'/blocks/{block_id}', None, deadline)
                if status != 200 or not self._is_paragraph(block, page_id, block_id):
                    result = {'status': 'rejected', 'operation_id': request_id}
                    return self._save(request_id, result)
            paragraph = {'rich_text': [{'type': 'text', 'text': {'content': text}}]}
            if operation == 'create':
                payload = {'children': [{'object': 'block', 'type': 'paragraph',
                                         'paragraph': paragraph}]}
                path = f'/blocks/{page_id}/children'
            else:
                payload = {'paragraph': paragraph}
                path = f'/blocks/{block_id}'
            status, document = self._request('PATCH', path, payload, deadline)
            if 400 <= status < 500 and status not in {408, 409, 429}:
                result = {'status': 'rejected', 'operation_id': request_id}
            elif 200 <= status < 300:
                if operation == 'create':
                    children = document.get('results')
                    if (document.get('object') != 'list' or not isinstance(children, list)
                            or len(children) != 1):
                        raise ValueError('unexpected append response')
                    document = children[0]
                returned_id = _page_uuid(document.get('id'))
                if not self._is_paragraph(document, page_id, block_id or returned_id):
                    raise ValueError('unexpected block identity')
                rich_text = document.get('paragraph', {}).get('rich_text')
                if not isinstance(rich_text, list) or ''.join(
                    item.get('plain_text', '') for item in rich_text
                    if isinstance(item, dict)
                ) != text:
                    raise ValueError('unexpected returned text')
                result = {'status': 'confirmed', 'operation_id': request_id,
                          'page_id': page_id, 'block_id': returned_id,
                          'url': f'https://www.notion.so/{page_id.replace("-", "")}'
                                 f'#{returned_id.replace("-", "")}'}
        except Exception:
            # No server exception details, content, or credentials leave this boundary.
            result = unknown
        return self._save(request_id, result)

    def _save(self, request_id, result):
        with closing(self._store._connect()) as connection:
            connection.execute('UPDATE notion_write_operations SET result=? WHERE request_id=?',
                               (json.dumps(result), request_id))
            connection.commit()
        return result

    @staticmethod
    def _is_paragraph(document, page_id, block_id):
        if not isinstance(document, dict):
            return False
        try:
            identity = _page_uuid(document.get('id'))
            parent = document.get('parent', {})
            parent_id = _page_uuid(parent.get('page_id'))
        except (ValueError, AttributeError):
            return False
        return (document.get('object') == 'block' and identity == block_id
                and document.get('type') == 'paragraph'
                and parent.get('type') == 'page_id' and parent_id == page_id
                and document.get('archived', False) is False
                and document.get('in_trash', False) is False)

    def _request(self, method, path, payload, deadline):
        # Paths are assembled exclusively from validated UUIDs, never caller URLs.
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError('deadline exceeded')
        url = API_ROOT + path
        headers = {'Authorization': 'Bearer ' + self._token,
                   'Notion-Version': NOTION_VERSION, 'Content-Type': 'application/json'}
        body = None if payload is None else json.dumps(payload).encode('utf-8')
        response = self._transport(method, url, headers, body, min(TIMEOUT_SECONDS, remaining))
        try:
            if callable(getattr(response, 'geturl', None)) and response.geturl() != url:
                raise ValueError('redirect rejected')
            status = int(response.status)
            raw = bytearray()
            while len(raw) <= MAX_RESPONSE_BYTES:
                if self._monotonic() >= deadline:
                    raise TimeoutError('deadline exceeded')
                chunk = response.read(min(16384, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError('response too large')
            if not 200 <= status < 300:
                return status, {}
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise ValueError('invalid response')
            return status, document
        finally:
            response.close()
