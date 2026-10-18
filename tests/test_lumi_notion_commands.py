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


OTHER_PAGE = '22222222-2222-4222-8222-222222222222'
DATED_TITLE = 'Lumi Integrationstest 2026-10-18'


def dated_service(targets=None):
    writer = Mock()
    writer.create_note.return_value = {'status': 'confirmed'}
    writer.update_note.return_value = {'status': 'confirmed'}
    return NotionCommandService(writer, targets if targets is not None else {DATED_TITLE: PAGE})


@pytest.mark.parametrize('date_text', [
    '2026-10-18', '2026 10 18', '18.10.2026', '18 10 2026',
    '18. Oktober 2026', '18 Oktober 2026', '18 OKTOBER 2026',
])
@pytest.mark.parametrize('prefix', ['Lumi Integrationstest', 'Lumi Integrations Test',
                                    'LUMI   INTEGRATIONSTEST'])
@pytest.mark.parametrize('separator', [': ', ' mit Inhalt ', ' mit dem Inhalt '])
def test_complete_date_variants_and_word_segmentation_are_equivalent(date_text, prefix, separator):
    service = dated_service()
    question = f'Notiz in {prefix} {date_text} speichern{separator}Bürotest Kobalt achtzehn.'
    assert 'gespeichert' in service.handle(question, 'wire-42', authorized=True)
    service.writer.create_note.assert_called_once_with(
        PAGE, 'Bürotest Kobalt achtzehn.', 'Bürotest Kobalt achtzehn.', 'wire-42',
        explicit_instruction=True, title_already_in_text=True)


@pytest.mark.parametrize('target', [
    'Lumi Integrationstest 2026-10-19',
    'Lumi Integrationstest 19. Oktober 2026',
    'Lumi Integrationstest 2025-10-18',
    'Lumi Integrationstest',
    'Integrationstest 2026-10-18',
    'Lumi 2026-10-18',
    'Lumi Integrationstest 2026-02-30',
    'Lumi Integrationstest 30. Februar 2026',
    'Lumi Integrationstest 18.13.2026',
    'Lumi Integrationstest 00.10.2026',
    'Lumi Integrationstest 18. Oktober 0000',
    'Lumi Integrationstest 18. Oktober 26',
    'Lumi Integrationstest 18. Oktober',
    'Lumi Integrationstest Oktober 2026',
    'Lumi Integrationstest heute',
    'Lumi Integrationstest morgen',
    'Lumi Integrationstest letzten Sonntag',
    'Lumi Integrationstest achtzehnter Oktober zweitausendsechsundzwanzig',
    'Lumi Integrationstest2026-10-18',
    'Lumi-Integrationstest 2026-10-18',
    'Lumi Integrations Test 2026-10-18 Zusatz',
])
def test_different_incomplete_invalid_relative_or_partial_targets_never_write(target):
    service = dated_service()
    reply = service.handle(f'Notiz in {target} speichern mit Inhalt Text', 'wire-42', authorized=True)
    assert 'gespeichert' not in reply
    service.writer.create_note.assert_not_called()
    service.writer.update_note.assert_not_called()


@pytest.mark.parametrize('date_text', ['2024-02-29', '29.02.2024', '29 Februar 2024'])
def test_valid_leap_day_is_equivalent(date_text):
    service = dated_service({'Test 2024-02-29': PAGE})
    assert 'gespeichert' in service.handle(
        f'Notiz in Test {date_text} speichern mit Inhalt Text', 'wire-42', authorized=True)


@pytest.mark.parametrize('title', ['Test 2026-02-29', 'Test 31. April 2026', 'Test 18.10.0000'])
def test_recognized_invalid_calendar_suffix_is_not_even_an_exact_text_target(title):
    service = dated_service({title: PAGE})
    reply = service.handle(f'Notiz in {title} speichern mit Inhalt Text', 'wire-42', authorized=True)
    assert 'gespeichert' not in reply
    service.writer.create_note.assert_not_called()


@pytest.mark.parametrize('month,number', [
    ('Januar', '01'), ('Februar', '02'), ('Ma\u0308rz', '03'), ('April', '04'),
    ('Mai', '05'), ('Juni', '06'), ('Juli', '07'), ('August', '08'),
    ('September', '09'), ('Oktober', '10'), ('November', '11'), ('Dezember', '12'),
])
def test_complete_german_month_names_use_valid_calendar_dates(month, number):
    service = dated_service({f'Test 2026-{number}-18': PAGE})
    assert 'gespeichert' in service.handle(
        f'Notiz in Test 18. {month} 2026 speichern mit Inhalt Text', 'wire-42', authorized=True)


@pytest.mark.parametrize('query', ['Lumi Integrationstest 2026-10-18',
                                   'Lumi Integrations Test 18. Oktober 2026',
                                   'LumiIntegrationstest 18 10 2026'])
@pytest.mark.parametrize('reverse_order', [False, True])
def test_different_pages_with_duplicate_canonical_date_key_block_every_spelling(query, reverse_order):
    items = [(DATED_TITLE, PAGE), ('Lumi Integrations Test 18. Oktober 2026', OTHER_PAGE)]
    service = dated_service(dict(reversed(items) if reverse_order else items))
    reply = service.handle(f'Notiz in {query} speichern mit Inhalt Text', 'wire-42', authorized=True)
    assert 'gespeichert' not in reply
    service.writer.create_note.assert_not_called()


def test_multiple_canonical_aliases_for_same_page_remain_unambiguous():
    service = dated_service({DATED_TITLE: PAGE, 'Lumi Integrations Test 18. Oktober 2026': PAGE})
    assert 'gespeichert' in service.handle(
        'Notiz in LumiIntegrationstest 18.10.2026 speichern mit Inhalt Text', 'wire-42', authorized=True)
    service.writer.create_note.assert_called_once()


@pytest.mark.parametrize('title', ['Café Notizen', 'Cafe\u0301 Notizen', 'CAFÉNOTIZEN'])
def test_nondated_complete_titles_use_nfc_casefold_and_whitespace_only(title):
    service = dated_service({'Café Notizen': PAGE})
    assert 'gespeichert' in service.handle(
        f'Notiz in {title} speichern mit Inhalt Text', 'wire-42', authorized=True)


@pytest.mark.parametrize('title', ['CaféNotizen', 'CAFÉ NOTIZEN', 'Cafe\u0301 Notizen'])
def test_nondated_canonical_collision_blocks_even_exact_title(title):
    service = dated_service({'Café Notizen': PAGE, 'Cafe\u0301Notizen': OTHER_PAGE})
    reply = service.handle(f'Notiz in {title} speichern mit Inhalt Text', 'wire-42', authorized=True)
    assert 'gespeichert' not in reply
    service.writer.create_note.assert_not_called()


def test_date_and_text_keys_cannot_collide_by_concatenation():
    service = dated_service({'Test20261018': OTHER_PAGE, 'Test 2026-10-18': PAGE})
    service.handle('Notiz in Test20261018 speichern mit Inhalt Text', 'wire-42', authorized=True)
    assert service.writer.create_note.call_args.args[0] == OTHER_PAGE
    service.handle('Notiz in Test 18. Oktober 2026 speichern mit Inhalt Text', 'wire-43', authorized=True)
    assert service.writer.create_note.call_args.args[0] == PAGE


def test_target_normalization_never_changes_note_content():
    service = dated_service()
    content = 'Café  Integrations Test 18. Oktober 2026\nNicht heute: 2026-02-30;  два пробела.'
    assert 'gespeichert' in service.handle(
        f'Notiz in Lumi Integrations Test 18. Oktober 2026 speichern mit Inhalt {content}',
        'wire-42', authorized=True)
    assert service.writer.create_note.call_args.args[2] == content


def test_normalized_target_update_still_requires_explicit_block_identity():
    service = dated_service()
    assert 'geändert' in service.handle(
        f'Notiz in Lumi Integrations Test 18. Oktober 2026 ändern Block {BLOCK}: Neuer Text',
        'wire-42', authorized=True)
    service.writer.update_note.assert_called_once_with(
        PAGE, BLOCK, 'Neuer Text', 'wire-42', explicit_instruction=True)


@pytest.mark.parametrize('authorized,request_id', [(False, 'wire-42'), (True, ''), (True, '   ')])
def test_normalized_target_keeps_authorization_and_request_id_guards(authorized, request_id):
    service = dated_service()
    reply = service.handle('Notiz in Lumi Integrations Test 18. Oktober 2026 speichern mit Inhalt Text',
                           request_id, authorized=authorized)
    assert 'gespeichert' not in reply
    service.writer.create_note.assert_not_called()


@pytest.mark.parametrize('status', ['unknown', 'rejected', None, 'success'])
def test_normalized_target_never_upgrades_an_unconfirmed_write(status):
    service = dated_service()
    service.writer.create_note.return_value = {'status': status}
    reply = service.handle('Notiz in Lumi Integrations Test 18. Oktober 2026 speichern mit Inhalt Text',
                           'wire-42', authorized=True)
    assert 'wurde in Notion gespeichert' not in reply
