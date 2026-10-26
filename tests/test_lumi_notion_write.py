import io
import json

import pytest

from codex_voice.lumi_notion_write import NotionWriter
from codex_voice.lumi_notion_commands import NotionCommandService

PAGE = '11111111-1111-1111-1111-111111111111'
BLOCK = '22222222-2222-2222-2222-222222222222'
OTHER = '33333333-3333-3333-3333-333333333333'


def block(text='Titel\nInhalt', parent=PAGE):
    return {'object': 'block', 'id': BLOCK, 'type': 'paragraph',
            'parent': {'type': 'page_id', 'page_id': parent},
            'paragraph': {'rich_text': [{'plain_text': text}]}}


class Response(io.BytesIO):
    def __init__(self, document, status=200, url=None):
        super().__init__(json.dumps(document).encode())
        self.status = status
        self.url = url

    def geturl(self):
        return self.url


def transport_factory(responses, calls):
    def transport(method, url, headers, body, timeout):
        calls.append((method, url, body, timeout))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        document, status, redirect = response
        return Response(document, status, redirect or url)
    return transport


def writer(tmp_path, responses, calls, allowed=None):
    return NotionWriter(tmp_path / 'private' / 'operations.db', 'synthetic-token',
                        [PAGE] if allowed is None else allowed,
                        transport=transport_factory(responses, calls))


def test_confirmed_create_is_persistent_and_bound_to_content(tmp_path):
    calls = []
    service = writer(tmp_path, [({'object': 'list', 'results': [block()]}, 200, None)], calls)
    result = service.create_note(PAGE, 'Titel', 'Inhalt', 'request_001', explicit_instruction=True)
    assert result['status'] == 'confirmed'
    assert result['block_id'] == BLOCK
    payload = json.loads(calls[0][2])
    assert payload['children'][0]['paragraph']['rich_text'][0]['text']['content'] == 'Titel\nInhalt'
    reopened = writer(tmp_path, [], calls)
    assert reopened.create_note(PAGE, 'Titel', 'Inhalt', 'request_001',
                                explicit_instruction=True) == result
    assert len(calls) == 1
    with pytest.raises(ValueError, match='different content'):
        reopened.create_note(PAGE, 'Titel', 'Anders', 'request_001', explicit_instruction=True)


@pytest.mark.parametrize('content', ['Filter wechseln', 'Filter wechseln\nDanach prüfen.',
                                    'L' * 140 + '\nZweite Zeile.'])
def test_command_create_stores_exact_content_once_and_retains_deduplication(tmp_path, content):
    calls = []
    service = writer(tmp_path, [({'object': 'list', 'results': [block(content)]}, 200, None)], calls)
    commands = NotionCommandService(service, {'Haus': PAGE})
    question = 'Notiz in Haus speichern: ' + content
    answer = commands.handle(question, 'request_exact', authorized=True)
    assert 'gespeichert' in answer
    payload = json.loads(calls[0][2])
    assert payload['children'] == [{'object': 'block', 'type': 'paragraph', 'paragraph': {
        'rich_text': [{'type': 'text', 'text': {'content': content}}]}}]
    reopened = NotionCommandService(writer(tmp_path, [], calls), {'Haus': PAGE})
    assert reopened.handle(question, 'request_exact', authorized=True) == answer
    assert len(calls) == 1
    with pytest.raises(ValueError, match='different content'):
        service.create_note(PAGE, content.splitlines()[0][:120], content + '!', 'request_exact',
                            explicit_instruction=True, title_already_in_text=True)


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_title_already_in_text_requires_boolean(tmp_path, value):
    calls = []
    service = writer(tmp_path, [], calls)
    with pytest.raises(ValueError, match='must be a boolean'):
        service.create_note(PAGE, 'Titel', 'Inhalt', 'request_bool', explicit_instruction=True,
                            title_already_in_text=value)
    assert not calls


@pytest.mark.parametrize('response', [TimeoutError('secret'),
    ({'object': 'list', 'results': [block(parent=OTHER)]}, 200, None),
    ({'object': 'list', 'results': [block()]}, 200, 'https://attacker.invalid'),
    ({}, 503, None), ({}, 429, None),
    ({'object': 'list', 'results': [block(text='unexpected')]}, 200, None)])
def test_uncertain_write_never_repeated(tmp_path, response):
    calls = []
    service = writer(tmp_path, [response], calls)
    result = service.create_note(PAGE, 'Titel', 'Inhalt', 'request_002', explicit_instruction=True)
    assert result == {'status': 'unknown', 'operation_id': 'request_002'}
    assert writer(tmp_path, [], calls).create_note(PAGE, 'Titel', 'Inhalt', 'request_002',
                                                  explicit_instruction=True) == result
    assert len(calls) == 1


def test_update_checks_parent_before_mutating(tmp_path):
    calls = []
    service = writer(tmp_path, [(block(parent=OTHER), 200, None)], calls)
    result = service.update_note(PAGE, BLOCK, 'Neu', 'request_003', explicit_instruction=True)
    assert result['status'] == 'rejected'
    assert [call[0] for call in calls] == ['GET']


def test_update_only_exact_paragraph(tmp_path):
    calls = []
    service = writer(tmp_path, [(block(), 200, None), (block('Neu'), 200, None)], calls)
    result = service.update_note(PAGE, BLOCK, 'Neu', 'request_004', explicit_instruction=True)
    assert result['status'] == 'confirmed'
    assert [call[0] for call in calls] == ['GET', 'PATCH']
    assert json.loads(calls[-1][2]) == {'paragraph': {'rich_text': [
        {'type': 'text', 'text': {'content': 'Neu'}}]}}
    assert all(call[3] <= 5 for call in calls)


def test_update_mismatched_success_id_remains_unknown(tmp_path):
    calls = []
    mismatched = block('Neu')
    mismatched['id'] = OTHER
    service = writer(tmp_path, [(block(), 200, None), (mismatched, 200, None)], calls)
    assert service.update_note(PAGE, BLOCK, 'Neu', 'request_badid',
                               explicit_instruction=True)['status'] == 'unknown'


def test_explicit_instruction_allowlist_and_limits(tmp_path):
    calls = []
    service = writer(tmp_path, [], calls)
    for page, text, explicit in [(PAGE, 'Text', False), (OTHER, 'Text', True),
                                 ('https://attacker.invalid', 'Text', True),
                                 (PAGE, 'x' * 1801, True)]:
        with pytest.raises(ValueError):
            service.create_note(page, 'Titel', text, 'request_005', explicit_instruction=explicit)
    assert not calls


def test_revocation_blocks_even_previous_confirmation(tmp_path):
    calls = []
    service = writer(tmp_path, [({'object': 'list', 'results': [block()]}, 200, None)], calls)
    service.create_note(PAGE, 'Titel', 'Inhalt', 'request_006', explicit_instruction=True)
    revoked = writer(tmp_path, [], calls, allowed=[])
    with pytest.raises(ValueError, match='approved page'):
        revoked.create_note(PAGE, 'Titel', 'Inhalt', 'request_006', explicit_instruction=True)
    assert len(calls) == 1


def test_definite_rejection_is_content_free(tmp_path):
    calls = []
    service = writer(tmp_path, [({'message': 'synthetic-token'}, 403, None)], calls)
    result = service.create_note(PAGE, 'Titel', 'Inhalt', 'request_007', explicit_instruction=True)
    assert result == {'status': 'rejected', 'operation_id': 'request_007'}


def test_inflight_request_is_reserved_before_network(tmp_path):
    calls = []
    second = writer(tmp_path, [], calls)

    def transport(method, url, headers, body, timeout):
        duplicate = second.create_note(PAGE, 'Titel', 'Inhalt', 'request_008',
                                        explicit_instruction=True)
        assert duplicate == {'status': 'unknown', 'operation_id': 'request_008'}
        calls.append(method)
        return Response({'object': 'list', 'results': [block()]}, url=url)

    first = NotionWriter(tmp_path / 'private' / 'operations.db', 'synthetic-token',
                         [PAGE], transport=transport)
    assert first.create_note(PAGE, 'Titel', 'Inhalt', 'request_008',
                             explicit_instruction=True)['status'] == 'confirmed'
    assert calls == ['PATCH']


def test_oversized_response_is_unknown(tmp_path):
    calls = []
    service = writer(tmp_path, [({'padding': 'x' * 262145}, 200, None)], calls)
    assert service.create_note(PAGE, 'Titel', 'Inhalt', 'request_009',
                               explicit_instruction=True)['status'] == 'unknown'


def test_deadline_prevents_request(tmp_path):
    calls = []
    clock = iter([0.0, 16.0])
    service = NotionWriter(tmp_path / 'private' / 'operations.db', 'synthetic-token',
                         [PAGE], transport=transport_factory([], calls),
                         monotonic=lambda: next(clock))
    assert service.create_note(PAGE, 'Titel', 'Inhalt', 'request_010',
                               explicit_instruction=True)['status'] == 'unknown'
    assert not calls
