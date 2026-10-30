"""Explicit server-only linking secret; never journal or return its value."""
import os
from pathlib import Path
import secrets


def configure(data):
    if type(data) is not dict or set(data) != {'token', 'confirmed'} or data['confirmed'] is not True:
        raise ValueError('assistant_confirmation_required')
    token = data['token']
    if (type(token) is not str or not 32 <= len(token) <= 128
            or not token.isascii() or any(not 33 <= ord(c) <= 126 for c in token)):
        raise ValueError('invalid_assistant_token')
    configured = os.environ.get('FINANCE_CONTROL_ASSISTANT_TOKEN_FILE')
    if not configured:
        raise ValueError('server_assistant_not_configured')
    path = Path(configured)
    if (not path.is_absolute() or path.name != 'assistant_token'
            or path.resolve() != path or path.is_symlink()):
        raise ValueError('invalid_assistant_configuration')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name('.assistant-' + secrets.token_hex(12))
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {'configured': True, 'ready': False, 'message': 'Verbindungsschlüssel gespeichert; Erreichbarkeit und Anmeldung separat prüfen.'}
