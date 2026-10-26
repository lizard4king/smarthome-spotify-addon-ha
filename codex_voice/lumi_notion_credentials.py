"""Read HA password options as root, before dropping the server's privileges."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat


MAX_OPTIONS_BYTES = 16 * 1024


@dataclass(frozen=True)
class NotionCredentials:
    read_token: str = field(default='', repr=False)
    write_token: str = field(default='', repr=False)


def _valid_file(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_gid == 0
            and info.st_nlink == 1 and info.st_size <= MAX_OPTIONS_BYTES)


def _parse(raw):
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate option')
            result[key] = value
        return result

    data = json.loads(raw, object_pairs_hook=unique_fields)
    if not isinstance(data, dict):
        raise ValueError('invalid options')
    values = {}
    for key in ('read_token', 'write_token'):
        value = data.get('lumi_notion_' + key, '')
        if (not isinstance(value, str) or len(value) > 512
                or any(not 33 <= ord(char) <= 126 for char in value)):
            raise ValueError('invalid token')
        values[key] = value
    return NotionCredentials(**values) if values['read_token'] else NotionCredentials()


def load_credentials(options_file=Path('/data/options.json')):
    """Protect Supervisor's options in place; return only in-memory credentials.

    There is no environment fallback. Missing or malformed token options disable
    credentials. Unsafe/unprotectable existing files stop startup without values
    in the error. Supervisor supplies options at each container startup.
    """
    descriptor = parent = None
    try:
        if os.name != 'posix' or os.geteuid() != 0:
            return NotionCredentials()
        path = Path(options_file)
        if not path.is_absolute() or '..' in path.parts:
            raise RuntimeError('Notion options protection failed')
        # Descriptor traversal prevents following a symlink or trusting a directory
        # writable by Codex, which could replace the protected options afterwards.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        parent = os.open('/', flags)
        for component in (*path.parent.parts[1:], None):
            info = os.fstat(parent)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
                    or info.st_mode & 0o022):
                raise RuntimeError('Notion options protection failed')
            if component is not None:
                child = os.open(component, flags, dir_fd=parent)
                os.close(parent)
                parent = child
        descriptor = os.open(path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        if not _valid_file(os.fstat(descriptor)):
            raise RuntimeError('Notion options protection failed')
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if not _valid_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError('Notion options protection failed')
        raw = os.read(descriptor, MAX_OPTIONS_BYTES + 1)
        if len(raw) > MAX_OPTIONS_BYTES:
            raise RuntimeError('Notion options protection failed')
        try:
            return _parse(raw)
        except (ValueError, TypeError, UnicodeError):
            return NotionCredentials()
    except FileNotFoundError:
        return NotionCredentials()
    except OSError:
        raise RuntimeError('Notion options protection failed') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)
