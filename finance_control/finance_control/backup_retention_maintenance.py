"""One-time, SHA-approved cleanup of legacy generated backup pairs.

Plans and journals live beside the private database, never in the repository.
This module does not inspect financial rows or contact Drive or a bank.
"""

import hashlib
import json
import os
import re
import secrets
import stat as statmod
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from .backup_package import (
    _BACKUP_STATE_LOCK, _checksum, _configured_drive_api, _configured_target,
    _sync_state_path, _write_sync_state, verify_backup_package,
)
from .drive_api import DriveApiError
from .import_preview import outside_repository

_ZIP = re.compile(r'[0-9a-f]{32}\.zip\Z')
_SQLITE = re.compile(r'[0-9a-f]{32}\.sqlite\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_RELATIVE = re.compile(r'(?:exports/[0-9a-f]{32}\.zip|backups/[0-9a-f]{32}\.sqlite)\Z')
_MAX_JSON_BYTES = 8 * 1024 * 1024


class RetentionPlanError(ValueError):
    """Stable, data-free reason for a rejected maintenance operation."""


def _root(database):
    database = outside_repository(Path(database))
    root = database.parent
    for name in ('exports', 'backups'):
        path = root / name
        if path.is_symlink() or (path.exists() and outside_repository(path).parent != root):
            raise RetentionPlanError('retention_directory_redirected')
    if (root / 'backup-sync-state.json').is_symlink():
        raise RetentionPlanError('retention_directory_redirected')
    return root


def _paths(root):
    exports, backups = root / 'exports', root / 'backups'
    zips = {p.name for p in exports.glob('*.zip') if _ZIP.fullmatch(p.name)}
    snapshots = {p.name for p in backups.glob('*.sqlite') if _SQLITE.fullmatch(p.name)}
    if any((exports / name).is_symlink() for name in zips) or any(
            (backups / name).is_symlink() for name in snapshots):
        raise RetentionPlanError('retention_file_changed')
    return sorted('exports/' + name for name in zips) + sorted(
        'backups/' + name for name in snapshots)


def _fingerprint(root, relative):
    path = root / relative
    if not isinstance(relative, str) or not _RELATIVE.fullmatch(relative):
        raise RetentionPlanError('retention_file_changed')
    if (path.is_symlink() or path.resolve().parent != (root / relative.split('/')[0]).resolve()):
        raise RetentionPlanError('retention_file_changed')
    try:
        status = path.lstat()
    except OSError as error:
        raise RetentionPlanError('retention_file_changed') from error
    if not statmod.S_ISREG(status.st_mode):
        raise RetentionPlanError('retention_file_changed')
    return {'size': status.st_size, 'mtime_ns': status.st_mtime_ns,
            'ctime_ns': status.st_ctime_ns, 'dev': status.st_dev, 'ino': status.st_ino}


def _metadata(root, relative):
    fingerprint = _fingerprint(root, relative)
    return {'path': relative, **fingerprint, 'sha256': _checksum(root / relative)}


def _verify_pair(root, zip_name, *, full=False):
    try:
        return _verify_pair_checked(root, zip_name, full=full)
    except RetentionPlanError:
        raise
    except Exception as error:  # Provider/archive diagnostics may contain private paths.
        raise RetentionPlanError('retention_pair_invalid') from error


def _verify_pair_checked(root, zip_name, *, full=False):
    stem = zip_name[:-4]
    archive_path = root / 'exports' / zip_name
    snapshot_path = root / 'backups' / (stem + '.sqlite')
    zip_meta = _metadata(root, 'exports/' + zip_name)
    snapshot_meta = _metadata(root, 'backups/' + snapshot_path.name)
    if full:
        checked = verify_backup_package(archive_path, root)
        if checked['sha256'] != zip_meta['sha256'] or checked['size'] != zip_meta['size']:
            raise RetentionPlanError('retention_file_changed')
    try:
        with zipfile.ZipFile(archive_path) as archive:
            manifest_info = archive.getinfo('manifest.json')
            if manifest_info.file_size > 1_000_000:
                raise RetentionPlanError('retention_manifest_invalid')
            manifest = json.loads(archive.read(manifest_info))
        if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
            raise RetentionPlanError('retention_manifest_invalid')
        expected = manifest['files'].get('database.sqlite')
    except (KeyError, TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise RetentionPlanError('retention_manifest_invalid') from error
    if (manifest.get('version') != 1 or not isinstance(expected, str)
            or not _SHA.fullmatch(expected)):
        raise RetentionPlanError('retention_manifest_invalid')
    if snapshot_meta['sha256'] != expected:
        raise RetentionPlanError('retention_snapshot_mismatch')
    return {'zip': zip_meta, 'snapshot': snapshot_meta}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _sync_parent(path):
    if os.name == 'nt':
        return
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private(path, value, *, immutable=False):
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(8) + '.partial')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            if path.exists():
                return
            if os.name == 'nt':
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        else:
            os.replace(temporary, path)
        _sync_parent(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path):
    descriptor = None
    try:
        if path.is_symlink():
            raise RetentionPlanError('retention_plan_redirected')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0))
        status = os.fstat(descriptor)
        if not statmod.S_ISREG(status.st_mode) or status.st_size > _MAX_JSON_BYTES:
            raise RetentionPlanError('retention_plan_invalid')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            payload = stream.read(_MAX_JSON_BYTES + 1)
        if len(payload) > _MAX_JSON_BYTES:
            raise RetentionPlanError('retention_plan_invalid')
        value = json.loads(payload.decode('utf-8'))
    except (OSError, ValueError, UnicodeError, RecursionError, TypeError) as error:
        raise RetentionPlanError('retention_plan_unavailable') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise RetentionPlanError('retention_plan_invalid')
    return value


def _plan_path(root, sha):
    return root / f'backup-retention-plan-{sha}.json'


def _journal_path(root, sha):
    return root / f'backup-retention-journal-{sha}.json'


def _receipt_path(root, sha):
    return root / f'backup-retention-applied-{sha}.json'


def _guard(database, environ):
    try:
        configured = (_configured_target(environ=environ) is not None
                      or _configured_drive_api(environ) is not None)
    except (ValueError, DriveApiError) as error:
        raise RetentionPlanError('retention_drive_configuration_invalid') from error
    if configured:
        raise RetentionPlanError('retention_drive_configured')
    state_path = _sync_state_path(database)
    state = _load(state_path) if state_path.exists() else {}
    _validate_state(state)
    if state.get('retiring_packages'):
        raise RetentionPlanError('retention_rotation_incomplete')


def _validate_state(state):
    if not isinstance(state, dict) or any(
            key in state and not isinstance(state[key], dict)
            for key in ('verified_packages', 'synced_packages', 'automatic_packages',
                        'retiring_packages')):
        raise RetentionPlanError('retention_state_invalid')


def prepare_retention_plan(database, *, environ=None):
    """Persist and return a plan containing only generated backup metadata."""
    with _BACKUP_STATE_LOCK:
        root = _root(database)
        _guard(database, environ)
        if any(root.glob('backup-retention-journal-*.json')):
            raise RetentionPlanError('retention_prior_plan_incomplete')
        inventory = _paths(root)
        zip_names = {path.split('/')[1] for path in inventory if path.startswith('exports/')}
        snapshots = {path.split('/')[1][:-7] for path in inventory if path.startswith('backups/')}
        pairs = [_verify_pair(root, name) for name in sorted(zip_names)
                 if name[:-4] in snapshots]
        pairs.sort(key=lambda pair: (pair['zip']['mtime_ns'], pair['zip']['path']))
        if len(pairs) < 2:
            raise RetentionPlanError('retention_two_keepers_required')
        for pair in pairs[-2:]:
            if _verify_pair(root, Path(pair['zip']['path']).name, full=True) != pair:
                raise RetentionPlanError('retention_keeper_changed')
        core = {'version': 1, 'inventory': inventory,
                'keepers': pairs[-2:], 'delete': pairs[:-2]}
        sha = hashlib.sha256(_canonical(core)).hexdigest()
        path = _plan_path(root, sha)
        if path.exists():
            stored = _load(path)
            if stored.get('plan_sha256') != sha or {
                    key: stored.get(key) for key in core} != core:
                raise RetentionPlanError('retention_plan_conflict')
            return stored
        plan = {'plan_sha256': sha, 'generated_at': datetime.now(UTC).isoformat(), **core}
        _write_private(path, plan, immutable=True)
        return plan


def _check_plan(root, plan, sha):
    if (set(plan) != {'plan_sha256', 'generated_at', 'version', 'inventory',
                      'keepers', 'delete'} or plan['plan_sha256'] != sha
            or not isinstance(plan['generated_at'], str) or not plan['generated_at']
            or type(plan['version']) is not int or plan['version'] != 1
            or not isinstance(plan['inventory'], list)
            or not isinstance(plan['keepers'], list) or len(plan['keepers']) != 2
            or not isinstance(plan['delete'], list)):
        raise RetentionPlanError('retention_plan_invalid')
    inventory = plan['inventory']
    if (any(not isinstance(value, str) or not _RELATIVE.fullmatch(value)
            for value in inventory) or len(inventory) != len(set(inventory))):
        raise RetentionPlanError('retention_plan_invalid')

    def valid_metadata(meta, prefix):
        if not isinstance(meta, dict) or set(meta) != {
                'path', 'size', 'mtime_ns', 'ctime_ns', 'dev', 'ino', 'sha256'}:
            raise RetentionPlanError('retention_plan_invalid')
        if (not isinstance(meta['path'], str)
                or not _RELATIVE.fullmatch(meta['path'])
                or not meta['path'].startswith(prefix)
                or not isinstance(meta['sha256'], str)
                or not _SHA.fullmatch(meta['sha256'])
                or any(type(meta[key]) is not int or meta[key] < 0
                       for key in ('size', 'mtime_ns', 'ctime_ns', 'dev', 'ino'))):
            raise RetentionPlanError('retention_plan_invalid')

    expected = []
    for pair in [*plan['keepers'], *plan['delete']]:
        if not isinstance(pair, dict) or set(pair) != {'zip', 'snapshot'}:
            raise RetentionPlanError('retention_plan_invalid')
        valid_metadata(pair['zip'], 'exports/')
        valid_metadata(pair['snapshot'], 'backups/')
        if (Path(pair['zip']['path']).stem != Path(pair['snapshot']['path']).stem):
            raise RetentionPlanError('retention_plan_invalid')
        expected.extend((pair['zip']['path'], pair['snapshot']['path']))
    if len(expected) != len(set(expected)) or not set(expected).issubset(inventory):
        raise RetentionPlanError('retention_plan_invalid')
    core = {key: plan[key] for key in ('version', 'inventory', 'keepers', 'delete')}
    if hashlib.sha256(_canonical(core)).hexdigest() != sha:
        raise RetentionPlanError('retention_plan_invalid')
    return core


def _check_keepers(root, plan):
    for pair in plan['keepers']:
        current = _verify_pair(root, Path(pair['zip']['path']).name, full=True)
        if current != pair:
            raise RetentionPlanError('retention_keeper_changed')


def _check_fingerprints(root, keepers, candidate):
    for meta in [candidate, *(part for pair in keepers
                              for part in (pair['zip'], pair['snapshot']))]:
        expected = {key: meta[key] for key in ('size', 'mtime_ns', 'ctime_ns', 'dev', 'ino')}
        if _fingerprint(root, meta['path']) != expected:
            raise RetentionPlanError('retention_file_changed')


def apply_approved_retention_plan(database, sha, *, environ=None):
    """Apply only a stored exact plan; resume only its journaled file intent."""
    with _BACKUP_STATE_LOCK:
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise RetentionPlanError('retention_approval_invalid')
        root = _root(database)
        _guard(database, environ)
        if _receipt_path(root, sha).exists():
            receipt = _load(_receipt_path(root, sha))
            if (set(receipt) != {'plan_sha256', 'completed_at', 'deleted_files'}
                    or receipt['plan_sha256'] != sha
                    or not isinstance(receipt['completed_at'], str)
                    or not receipt['completed_at']
                    or type(receipt['deleted_files']) is not int
                    or receipt['deleted_files'] < 0):
                raise RetentionPlanError('retention_receipt_invalid')
            journal_path = _journal_path(root, sha)
            if journal_path.is_symlink():
                raise RetentionPlanError('retention_journal_invalid')
            journal_path.unlink(missing_ok=True)
            _sync_parent(journal_path)
            return {'status': 'already_applied', 'plan_sha256': sha}
        for other in root.glob('backup-retention-journal-*.json'):
            if other != _journal_path(root, sha):
                raise RetentionPlanError('retention_other_plan_incomplete')
        plan = _load(_plan_path(root, sha))
        core = _check_plan(root, plan, sha)
        journal_path = _journal_path(root, sha)
        journal = _load(journal_path) if journal_path.exists() else {
            'plan_sha256': sha, 'completed': [], 'intent': None}
        if (set(journal) != {'plan_sha256', 'completed', 'intent'}
                or journal['plan_sha256'] != sha
                or not isinstance(journal['completed'], list)
                or any(not isinstance(value, str) for value in journal['completed'])
                or (journal['intent'] is not None
                    and not isinstance(journal['intent'], str))):
            raise RetentionPlanError('retention_journal_invalid')
        deletions = [meta for pair in core['delete']
                     for meta in (pair['zip'], pair['snapshot'])]
        allowed = {meta['path'] for meta in deletions}
        done = set(journal['completed'])
        intent = journal.get('intent')
        if (len(done) != len(journal['completed']) or not done.issubset(allowed)
                or intent not in (None, *allowed)):
            raise RetentionPlanError('retention_journal_invalid')
        if intent is not None and intent not in done and not (root / intent).exists():
            done.add(intent)
            journal['completed'] = sorted(done)
            journal['intent'] = None
            _write_private(journal_path, journal)
        expected_inventory = set(core['inventory']) - done
        if set(_paths(root)) != expected_inventory:
            raise RetentionPlanError('retention_inventory_changed')
        _check_keepers(root, core)
        # Preflight every remaining deletion member before the first unlink.
        for meta in deletions:
            if meta['path'] not in done and _metadata(root, meta['path']) != meta:
                raise RetentionPlanError('retention_file_changed')
        if not journal_path.exists():
            _write_private(journal_path, journal)
        for meta in deletions:
            relative = meta['path']
            if relative in done:
                continue
            if set(_paths(root)) != set(core['inventory']) - done:
                raise RetentionPlanError('retention_inventory_changed')
            _check_fingerprints(root, core['keepers'], meta)
            journal['intent'] = relative
            _write_private(journal_path, journal)
            (root / relative).unlink()
            _sync_parent(root / relative)
            done.add(relative)
            journal['completed'] = sorted(done)
            journal['intent'] = None
            _write_private(journal_path, journal)
        _check_keepers(root, core)
        state_path = _sync_state_path(database)
        state = _load(state_path) if state_path.exists() else {}
        _validate_state(state)
        for pair in core['delete']:
            name = Path(pair['zip']['path']).name
            for key in ('automatic_packages', 'verified_packages', 'synced_packages'):
                if isinstance(state.get(key), dict):
                    state[key].pop(name, None)
        automatic = {}
        for pair in core['keepers']:
            name = Path(pair['zip']['path']).name
            automatic[name] = datetime.fromtimestamp(
                pair['zip']['mtime_ns'] / 1_000_000_000, UTC).isoformat()
        state['automatic_packages'] = automatic
        if state.get('last_local_package') not in automatic:
            state['last_local_package'] = Path(core['keepers'][-1]['zip']['path']).name
        _write_sync_state(database, state)
        _sync_parent(_sync_state_path(database))
        _write_private(_receipt_path(root, sha), {
            'plan_sha256': sha, 'completed_at': datetime.now(UTC).isoformat(),
            'deleted_files': len(deletions)})
        journal_path.unlink(missing_ok=True)
        _sync_parent(journal_path)
        return {'status': 'applied', 'plan_sha256': sha, 'deleted_files': len(deletions)}
