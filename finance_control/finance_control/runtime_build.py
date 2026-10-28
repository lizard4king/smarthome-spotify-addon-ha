"""Bounded, secret-free metadata from the manifest inside the running app."""
from pathlib import Path
import re

from .backup_guard import _read_object


def build_status(build_manifest=None):
    """Return only validated build fields, never a path or arbitrary manifest data."""
    denied = (503, {'schema_version': 1, 'available': False})
    if build_manifest is None:
        return denied
    try:
        manifest = _read_object(Path(build_manifest), 1024 * 1024)
        version = manifest.get('addon_version')
        if type(version) is not str or re.fullmatch(r'\d+\.\d+\.\d+', version) is None:
            return denied
        result = {'schema_version': 1, 'available': True, 'addon_version': version}
        for name, length in (('source_commit', 40), ('source_tree_sha256', 64),
                             ('addon_tree_sha256', 64)):
            value = manifest.get(name)
            if type(value) is not str or re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is None:
                return denied
            result[name] = value
        for name in ('dirty_source_sha256', 'dirty_deployment_sha256'):
            value = manifest.get(name)
            if value is not None and (type(value) is not str
                                      or re.fullmatch('[0-9a-f]{64}', value) is None):
                return denied
            result[name] = value
        source_files = manifest.get('files')
        if not isinstance(source_files, dict) or not source_files:
            return denied
        if any(type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None
               for value in source_files.values()):
            return denied
        if type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
            return denied
        result['file_count'] = len(source_files)
        return 200, result
    except (OSError, ValueError, TypeError):
        return denied
