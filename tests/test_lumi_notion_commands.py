from unittest.mock import Mock

import pytest

from codex_voice.lumi_notion_commands import NotionCommandService


PAGE = '11111111-1111-4111-8111-111111111111'
BLOCK = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


@pytest.fixture
def service():
    writer = Mock()
    writer.create_note.return_value = {'status': 'confirmed', 'operation_id': 'op'}
    writer.update_note.return_value = {'status': 'confirmed', 'operation_id': 'op'}
    return NotionCommandService(writer, {'Haus': PAGE})


def test_explicit_create_preserves_wire_request_id(service):
    reply = service.handle('Notiz in Haus speichern: Filter wechseln', 'wire-42', authorized=True)
    assert 'gespeichert' in reply
    service.writer.create_note.assert_called_once_with(
        PAGE, 'Filter wechseln', 'Filter wechseln', 'wire-42', explicit_instruction=True,
        title_already_in_text=True)


def test_explicit_update_requires_block_and_target(service):
    reply = service.handle(f'Notiz in Haus ändern Block {BLOCK}: Neuer Text', 'wire-42', authorized=True)
    assert 'geändert' in reply
    service.writer.update_note.assert_called_once_with(
        PAGE, BLOCK, 'Neuer Text', 'wire-42', explicit_instruction=True)


@pytest.mark.parametrize('question', [
    'Notiz speichern: Filter wechseln',
    'Speichere eine Notiz',
    'Notiz in Haus speichern:',
    'Notiz in Haus ändern: Neuer Text',
    'Notiz in Haus ändern Block ungültig: Neuer Text',
    'Kannst du eine Notiz in Haus speichern: Filter wechseln?',
    'Er hat gesagt: Notiz in Haus speichern: Filter wechseln',
    'Wenn ich dich morgen bitte, Notiz in Haus speichern: Filter wechseln',
])
def test_ambiguous_commands_are_intercepted_without_writes(service, question):
    assert service.handle(question, 'wire-42', authorized=True) is not None
    service.writer.create_note.assert_not_called()
    service.writer.update_note.assert_not_called()


def test_unrelated_question_reaches_normal_assistant(service):
    assert service.handle('Was steht in meinen Notizen?', 'wire-42', authorized=True) is None


@pytest.mark.parametrize('target', ['Fremd', PAGE, 'https://notion.so/example'])
def test_targets_cannot_be_supplied_outside_server_catalog(service, target):
    reply = service.handle(f'Notiz in {target} speichern: Text', 'wire-42', authorized=True)
    assert reply is not None
    assert 'gespeichert' not in reply
    service.writer.create_note.assert_not_called()


def test_no_authorization(service):
    assert 'Freigabe' in service.handle('Notiz in Haus speichern: Text', 'wire-42', authorized=False)
    service.writer.create_note.assert_not_called()


def test_no_request_id(service):
    assert 'Auftragskennung' in service.handle('Notiz in Haus speichern: Text', '', authorized=True)
    service.writer.create_note.assert_not_called()


@pytest.mark.parametrize('status', ['rejected', 'unknown', None, 'success'])
def test_only_confirmed_status_claims_success(service, status):
    service.writer.create_note.return_value = {'status': status}
    reply = service.handle('Notiz in Haus speichern: Text', 'wire-42', authorized=True)
    assert 'wurde in Notion gespeichert' not in reply


def test_transport_exception_does_not_leak_content(service):
    service.writer.create_note.side_effect = RuntimeError('secret token')
    reply = service.handle('Notiz in Haus speichern: Text', 'wire-42', authorized=True)
    assert 'unklar' in reply
    assert 'secret token' not in reply


def test_casefolded_alias_collision_fails_closed():
    with pytest.raises(ValueError):
        NotionCommandService(Mock(), {'Haus': PAGE, 'haus': PAGE})


@pytest.mark.parametrize('separator', ['mit Inhalt', 'mit dem Inhalt'])
@pytest.mark.parametrize('prefix', ['', 'Meine Frage ist ', 'Meine Frage ist: ',
                                    'meine frage ist : '])
def test_spoken_create_is_complete_and_preserves_exact_content(service, separator, prefix):
    question = prefix + f'Notiz in Haus speichern {separator} Bürotest Kobalt achtzehn.'
    assert 'gespeichert' in service.handle(question, 'wire-42', authorized=True)
    service.writer.create_note.assert_called_once_with(
        PAGE, 'Bürotest Kobalt achtzehn.', 'Bürotest Kobalt achtzehn.', 'wire-42',
        explicit_instruction=True, title_already_in_text=True)


@pytest.mark.parametrize('prefix', ['Meine Frage ist ', 'Meine Frage ist: '])
def test_fixed_introduction_preserves_historical_colon_form(service, prefix):
    assert 'gespeichert' in service.handle(
        prefix + 'Notiz in Haus speichern: Filter wechseln', 'wire-42', authorized=True)
    service.writer.create_note.assert_called_once()


@pytest.mark.parametrize('question', [
    'Notiz in Haus speichern mit Inhalt',
    'Notiz in Haus speichern mit dem Inhalt',
    'Notiz in Haus speichern mit Inhalt   ',
    'Notiz in Haus speichern mit dem Inhalt   ',
    'Meine Frage ist: Notiz in Haus speichern mit Inhalt   ',
    'Notiz in Haus speichern:   ',
    'Notiz in Haus speichern Doppelpunkt Text',
    'Notiz in Haus speichern Text',
    '"Notiz in Haus speichern mit Inhalt Text"',
    '‚Notiz in Haus speichern mit Inhalt Text‘',
    'Er hat gesagt: Notiz in Haus speichern mit Inhalt Text',
    'Wenn ich dich morgen bitte, Notiz in Haus speichern mit Inhalt Text',
    'Kannst du eine Notiz in Haus speichern mit Inhalt Text?',
    'Meine Frage lautet: Notiz in Haus speichern mit Inhalt Text',
    'Meine Frage ist: Meine Frage ist: Notiz in Haus speichern mit Inhalt Text',
])
def test_incomplete_or_embedded_spoken_commands_never_write(service, question):
    reply = service.handle(question, 'wire-42', authorized=True)
    assert 'Es wurde nichts geschrieben.' in reply
    assert 'zusammen in einem Satz' in reply
    service.writer.create_note.assert_not_called()
    service.writer.update_note.assert_not_called()


def test_fixed_introduction_requires_a_word_boundary(service):
    assert service.handle('Meine Frage istNotiz in Haus speichern mit Inhalt Text',
                          'wire-42', authorized=True) is None
    service.writer.create_note.assert_not_called()
    service.writer.update_note.assert_not_called()


@pytest.mark.parametrize('target', ['Fremd', 'Hau', PAGE, 'https://notion.so/example'])
def test_spoken_form_does_not_expand_targets(service, target):
    reply = service.handle(f'Notiz in {target} speichern mit Inhalt Text',
                           'wire-42', authorized=True)
    assert 'gespeichert' not in reply
    service.writer.create_note.assert_not_called()


def test_date_in_title_is_not_reinterpreted():
    writer = Mock()
    service = NotionCommandService(writer, {'Lumi Integrationstest 2026-10-18': PAGE})
    reply = service.handle(
        'Notiz in Lumi Integrationstest achtzehnter Oktober zweitausendsechsundzwanzig '
        'speichern mit Inhalt Text', 'wire-42', authorized=True)
    assert 'Ziel ist nicht freigegeben' in reply
    writer.create_note.assert_not_called()


def test_spoken_form_requires_authorization(service):
    reply = service.handle('Notiz in Haus speichern mit Inhalt Text', 'wire-42', authorized=False)
    assert 'Freigabe' in reply
    service.writer.create_note.assert_not_called()


@pytest.mark.parametrize('request_id', ['', '   '])
def test_spoken_form_requires_nonblank_request_id(service, request_id):
    reply = service.handle('Notiz in Haus speichern mit Inhalt Text', request_id, authorized=True)
    assert 'Auftragskennung' in reply
    service.writer.create_note.assert_not_called()


def test_followup_does_not_complete_previous_incomplete_command(service):
    first = service.handle('Notiz in Haus speichern mit Inhalt', 'wire-42', authorized=True)
    assert 'Es wurde nichts geschrieben.' in first
    assert service.handle('Bürotest Kobalt achtzehn.', 'wire-43', authorized=True) is None
    service.writer.create_note.assert_not_called()
    service.writer.update_note.assert_not_called()


@pytest.mark.parametrize('outcome', [{'status': 'unknown'}, None, {}, {'status': 'success'}])
def test_spoken_form_never_confirms_unknown_outcome(service, outcome):
    service.writer.create_note.return_value = outcome
    reply = service.handle('Notiz in Haus speichern mit Inhalt Text', 'wire-42', authorized=True)
    assert 'Schreibstatus ist unklar' in reply
    assert 'wurde in Notion gespeichert' not in reply
