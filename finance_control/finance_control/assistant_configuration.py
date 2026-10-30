"""Explicit server-only linking secret; never journal or return its value."""
import os
from pathlib import Path
import secrets


def configure(data, *, replace_existing=True):
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
        if replace_existing:
            os.replace(temporary, path)
        else:
            try:
                # Atomic create-only publication: never replace an owner's key.
                os.link(temporary, path)
            except FileExistsError:
                existing = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(existing, 'rb') as stream:
                    previous = stream.read(129)
                if not secrets.compare_digest(previous, token.encode('ascii')):
                    raise ValueError('assistant_already_configured') from None
    finally:
        temporary.unlink(missing_ok=True)
    return {'configured': True, 'ready': False, 'message': 'Verbindungsschlüssel gespeichert; Erreichbarkeit und Anmeldung separat prüfen.'}


def provision(token):
    """Supervisor-only first provisioning; retries with the same key are safe."""
    configure({'token': token, 'confirmed': True}, replace_existing=False)
    return {'configured': True}
