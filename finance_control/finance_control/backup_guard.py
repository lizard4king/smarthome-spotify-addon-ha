"""Secret-free, read-only backup precondition for the Finance HA app."""
import json
import os
from pathlib import Path
import stat

APPROVED_ADDON_VERSION = '0.2.84'


def _read_object(path, limit):
    """Read a bounded regular file; refuse redirected files on the Linux app."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Invalid backup guard input')
        raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Invalid backup guard input')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid backup guard input')
    return value


def backup_guard_status(options_file=None, build_manifest=None):
    """Re-read actual options every call. Return only a fixed public contract.

    Paths are server-side injections, never query parameters. Missing paths on
    the desktop fail closed. No application state, bank access or secret output.
    """
    denied = (503, {'schema_version': 1, 'backup_safe': False})
    if options_file is None or build_manifest is None:
        return denied
    try:
        options = _read_object(Path(options_file), 64 * 1024)
        manifest = _read_object(Path(build_manifest), 1024 * 1024)
        if (type(options.get('access_token')) is not str
                or options['access_token'] != ''
                or manifest.get('addon_version') != APPROVED_ADDON_VERSION):
            return denied
    except (OSError, ValueError, TypeError):
        return denied
    return 200, {'schema_version': 1, 'backup_safe': True,
                 'addon_version': APPROVED_ADDON_VERSION}
