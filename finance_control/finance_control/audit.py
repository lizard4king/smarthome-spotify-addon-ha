"""Trusted operation identity; never derive attribution from request JSON.

Authenticated user IDs take precedence over the import/local fallbacks. The
context belongs to one Store connection, never to shared process-global state.
"""
from contextlib import contextmanager
from functools import wraps
import re


_ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z')


def validated_actor(value):
    if type(value) is not str or _ACTOR.fullmatch(value) is None:
        raise ValueError('invalid_audit_actor')
    return value


def actor_for(store):
    return validated_actor(getattr(store, 'audit_actor', 'system:local'))


@contextmanager
def import_actor(store):
    """Label an unattended import, preserving an authenticated initiating user."""
    previous = actor_for(store)
    store.audit_actor = 'system:import' if previous == 'system:local' else previous
    try:
        yield
    finally:
        store.audit_actor = previous


def import_operation(function):
    """Scope a store-first import entry point without leaking identity afterwards."""
    @wraps(function)
    def imported(store, *args, **kwargs):
        with import_actor(store):
            return function(store, *args, **kwargs)
    return imported
