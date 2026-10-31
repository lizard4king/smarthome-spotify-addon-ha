"""Offline checks for immediate, private, local launch greetings."""
import re

import pytest

from music_alexa.lumi_greeting import (
    MAX_PRINCIPALS,
    MAX_REQUESTS_PER_PRINCIPAL,
    MAX_TTL_SECONDS,
    LocalGreetings,
)


PERSON = "a" * 64
OTHER = "b" * 64


def test_greetings_are_natural_short_german_and_have_substantial_variety():
    greetings = LocalGreetings()
    selected = [greetings.select(PERSON, f"request-{i}") for i in range(64)]

    assert len(set(selected)) >= 12
    assert all(0 < len(text) <= 120 for text in selected)
    assert all("?" not in text and "<" not in text and ">" not in text for text in selected)
    assert all(not re.search(r"\b(?:Hallo,? ich bin Lumi|Was möchtest du|Was kann ich für dich tun)\b", text, re.I)
               for text in selected)
    assert all(not re.search(r"\b(?:wieder|heute|gestern|morgen|gerade|Erlene|Andreas)\b", text, re.I)
               for text in selected)
    assert "Hallo. Schön, dich zu hören." in selected
    assert "Da bist du ja. Ich bin ganz Ohr." in selected
    assert "Ah, Gesellschaft. Sehr angenehm." in selected
    assert "Schön, dass du da bist." in selected
    assert all(selected[i] != selected[i - 1] for i in range(1, len(selected)))


def test_request_replay_is_stable_and_does_not_change_last_new_request_choice():
    greetings = LocalGreetings()
    first = greetings.select(PERSON, "request-a")
    second = greetings.select(PERSON, "request-b")

    assert greetings.select(PERSON, "request-a") == first
    assert second != first
    third = greetings.select(PERSON, "request-c")
    assert third != second


def test_principals_are_isolated_and_ids_are_not_retained_in_plaintext():
    greetings = LocalGreetings()
    greetings.select(PERSON, "synthetic-request-id")
    greetings.select(OTHER, "synthetic-request-id")

    assert len(greetings._principals) == 2
    assert "synthetic-request-id" not in repr(greetings._principals)
    assert set(greetings._principals) == {PERSON, OTHER}


@pytest.mark.parametrize("principal", ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64, None, 7])
def test_rejects_nonopaque_or_malformed_principals(principal):
    with pytest.raises(ValueError):
        LocalGreetings().select(principal, "request")


@pytest.mark.parametrize("request_id", ["", "x" * 1025, None, 42])
def test_rejects_empty_oversized_or_nontext_request_ids(request_id):
    with pytest.raises(ValueError):
        LocalGreetings().select(PERSON, request_id)


def test_cache_is_bounded_per_principal_and_principal_lru_is_bounded():
    greetings = LocalGreetings()
    for index in range(MAX_REQUESTS_PER_PRINCIPAL + 5):
        greetings.select(PERSON, f"request-{index}")
    assert len(greetings._principals[PERSON].requests) == MAX_REQUESTS_PER_PRINCIPAL

    for index in range(MAX_PRINCIPALS + 1):
        greetings.select(f"{index:064x}", "same-request")
    assert len(greetings._principals) == MAX_PRINCIPALS
    assert "0" * 64 not in greetings._principals


def test_entries_expire_without_sliding_replay_expiry():
    now = [10.0]
    greetings = LocalGreetings(clock=lambda: now[0])
    first = greetings.select(PERSON, "request-a")
    now[0] += MAX_TTL_SECONDS - 1
    assert greetings.select(PERSON, "request-b") != first
    now[0] = 10.0 + MAX_TTL_SECONDS
    greetings.select(PERSON, "request-c")
    assert len(greetings._principals[PERSON].requests) == 2
    now[0] = 10.0 + 2 * MAX_TTL_SECONDS - 1
    greetings.select(PERSON, "request-d")
    now[0] = 10.0 + 3 * MAX_TTL_SECONDS - 1
    greetings.select(OTHER, "expire-old-state")
    assert PERSON not in greetings._principals
    after_expiry = greetings.select(PERSON, "request-a")
    assert after_expiry == first
    assert len(greetings._principals[PERSON].requests) == 1


def test_invalid_clock_is_rejected():
    for value in (None, True, "later", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            LocalGreetings(clock=lambda value=value: value).select(PERSON, "request")
