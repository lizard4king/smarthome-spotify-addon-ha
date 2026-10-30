"""Offline privacy and lifecycle checks for the small greeting cache."""
import json

import pytest

from codex_voice.lumi_assistant import (
    GREETING_FALLBACK, GreetingCache, LumiSettings, greeting_context,
    greeting_prompt, memory_scope,
)

PERSON = ('person', 'a' * 64)
OTHER = ('person', 'b' * 64)
SESSION = ('session', 'a' * 64)
TEXT = 'Gut, dich zu hören. Der blaue Filter hat seinen Platz gefunden.'


def test_cache_isolated_by_authorized_scope_and_session():
    cache = GreetingCache()
    settings = LumiSettings(True, frozenset({PERSON[1]}))
    approved = memory_scope({'person_key': PERSON[1], 'session_key': 'c' * 64}, settings)
    unknown = memory_scope({'person_key': OTHER[1], 'session_key': SESSION[1]}, settings)
    cache.put(approved, 'same allowed context', TEXT)
    assert cache.get(approved, 'same allowed context') == TEXT
    assert cache.get(unknown, 'same allowed context') is None
    assert cache.get(OTHER, 'same allowed context') is None
    cache.put(SESSION, 'session context', 'Schön, dich zu hören. Der Tag hat noch Platz.')
    assert cache.get(PERSON, 'same allowed context') == TEXT
    assert cache.get(('session', 'c' * 64), 'session context') is None


def test_cache_retains_only_context_fingerprint_and_invalidates_on_changed_context():
    cache = GreetingCache()
    private_context = 'synthetic-private-context-keep-out-of-cache'
    cache.put(PERSON, private_context, TEXT)
    assert private_context not in repr(cache.__dict__)
    entry = cache._entries[PERSON]
    assert len(entry.context_fingerprint) == 64
    assert cache.get(PERSON, 'new context') is None
    assert cache.get(PERSON, private_context) is None


def test_expiry_checked_at_lookup_and_hits_do_not_extend_it():
    now = [0.0]
    cache = GreetingCache(clock=lambda: now[0])
    cache.put(PERSON, 'context', TEXT)
    now[0] = 299
    assert cache.get(PERSON, 'context') == TEXT
    now[0] = 300
    assert cache.get(PERSON, 'context') is None
    assert not cache._entries


def test_capacity_evicts_least_recently_used_scope_and_invalidation_is_local():
    cache = GreetingCache(max_entries=2)
    cache.put(PERSON, 'context', TEXT)
    cache.put(OTHER, 'context', TEXT)
    assert cache.get(PERSON, 'context') == TEXT
    cache.put(SESSION, 'context', TEXT)
    assert cache.get(OTHER, 'context') is None
    cache.invalidate(PERSON)
    cache.invalidate(PERSON)
    assert cache.get(PERSON, 'context') is None
    assert cache.get(SESSION, 'context') == TEXT


@pytest.mark.parametrize('greeting', [None, '', 'Was möchtest du wissen?', '<speak>Hallo</speak>',
                                    'Sage Antwort.', 'A' * 181, GREETING_FALLBACK])
def test_only_valid_generated_greetings_are_cached(greeting):
    cache = GreetingCache()
    cache.put(PERSON, 'context', greeting)
    assert cache.get(PERSON, 'context') is None


def test_disabled_memory_never_creates_a_global_cache_entry():
    cache = GreetingCache()
    scope = memory_scope({'person_key': PERSON[1], 'session_key': SESSION[1]}, LumiSettings())
    cache.put(scope, 'context', TEXT)
    assert cache.get(scope, 'context') is None
    assert not cache._entries


@pytest.mark.parametrize('scope', [('person', ''), ('session', 'A' * 64), ('none', 'a' * 64),
                                 ('device', 'a' * 64), ['person', 'a' * 64], None])
def test_unvalidated_scope_shapes_are_rejected(scope):
    cache = GreetingCache()
    with pytest.raises(ValueError):
        cache.get(scope, 'context')
    with pytest.raises(ValueError):
        cache.put(scope, 'context', TEXT)
    with pytest.raises(ValueError):
        cache.invalidate(scope)


@pytest.mark.parametrize('options', [{'max_entries': 0}, {'max_entries': 33}, {'max_entries': True},
                                  {'ttl_seconds': 0}, {'ttl_seconds': 301},
                                  {'ttl_seconds': float('inf')}, {'ttl_seconds': float('nan')}])
def test_cache_limits_cannot_exceed_fixed_bounds(options):
    with pytest.raises(ValueError):
        GreetingCache(**options)


def test_context_fingerprint_input_matches_only_the_history_in_the_prompt():
    turns = [{'question': 'Ältere Frage', 'answer': 'Alte Antwort', 'created_at': 1},
             {'question': 'Der Filter ist blau.', 'answer': 'Das halte ich fest.', 'created_at': 2}]
    prompt_data = json.loads(greeting_prompt(turns, scope='person').split('\n', 1)[1])
    assert json.loads(greeting_context(turns)) == prompt_data['history']
    oversized = [{'question': 'Alt', 'answer': 'Private ältere Details'},
                 {'question': 'Neu', 'answer': 'X' * 2000}]
    assert json.loads(greeting_context(oversized)) == []
