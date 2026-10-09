"""Consistent SQLite snapshot plus immutable staged source files in one local ZIP."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from threading import RLock

from .drive_api import DriveApiError
from .import_preview import outside_repository

_BACKUP_STATE_LOCK = RLock()


def _serialized_backup_state(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _BACKUP_STATE_LOCK:
            return function(*args, **kwargs)
    return wrapped


def create_backup_package(store, database):
    parent = outside_repository(database.parent)
    backups = outside_repository(parent / 'backups')
    exports = outside_repository(parent / 'exports')
    sources = outside_repository(parent / (database.stem + '-imports'))
    documents = outside_repository(parent / (database.stem + '-documents'))
    if any(not path.is_relative_to(parent) for path in (backups, exports, sources, documents)):
        raise ValueError('redirected_backup_directory')
    reporting_history = _reporting_history_file(parent)
    backups.mkdir(exist_ok=True)
    exports.mkdir(exist_ok=True)
    identity = secrets.token_hex(16)
    snapshot = backups / (identity + '.sqlite')
    store.backup(snapshot)
    destination = exports / (identity + '.zip')
    partial = exports / (identity + '.partial')
    manifest = {'version': 1, 'database_integrity': 'ok', 'cloud_upload': False, 'files': {}}
    paths = [(snapshot, 'database.sqlite')]
    if reporting_history is not None:
        paths.append((reporting_history, 'reporting-history.json'))
    if sources.exists():
        for path in sorted(sources.glob('*/*')):
            if not (re.fullmatch('[0-9a-f]{32}', path.parent.name)
                    and (path.name in {'source.xlsx', 'stage.json'}
                         or re.fullmatch(r'decision-[0-9a-f]{16}\.json', path.name))):
                continue
            resolved = outside_repository(path)
            if not resolved.is_relative_to(sources):
                raise ValueError('redirected_source_file')
            paths.append((resolved, 'sources/' + path.parent.name + '/' + path.name))
    if documents.exists():
        snapshot_db = sqlite3.connect(snapshot)
        try:
            document_ids = {str(row[0]) for row in snapshot_db.execute('SELECT id FROM classification_documents')}
        finally:
            snapshot_db.close()
        for path in sorted(documents.glob('*.txt')):
            if not re.fullmatch(r'[1-9][0-9]*\.txt', path.name):
                continue
            if path.stem not in document_ids:
                continue
            resolved = outside_repository(path)
            if not resolved.is_relative_to(documents):
                raise ValueError('redirected_document_cache')
            if resolved.stat().st_size > 2_000_000:
                raise ValueError('document_cache_too_large')
            paths.append((resolved, 'documents/' + path.name))
    with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, archive_name in paths:
            checksum = hashlib.sha256()
            with path.open('rb') as source, archive.open(archive_name, 'w') as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    checksum.update(chunk)
                    target.write(chunk)
            manifest['files'][archive_name] = checksum.hexdigest()
        archive.writestr('manifest.json', json.dumps(manifest, indent=2))
    with zipfile.ZipFile(partial) as archive:
        if archive.testzip() is not None:
            raise ValueError('archive_integrity_failed')
    partial.rename(destination)
    return {'download_url': '/exports/' + destination.name, 'integrity': 'ok',
            'files': len(paths), 'cloud_upload': False}


def _checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_checksum(path):
    digest = hashlib.md5()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _configured_target(target_directory=None, environ=None):
    environ = os.environ if environ is None else environ
    configured = target_directory or environ.get('FINANCE_CONTROL_DRIVE_BACKUP_DIRECTORY')
    if not configured:
        return None
    target = outside_repository(configured)
    if (target / '.git').exists():
        raise ValueError('drive_backup_directory_inside_repository')
    return target


def _configured_drive_api(environ=None):
    """Build the optional API target without touching credentials or Drive."""
    environ = os.environ if environ is None else environ
    root_folder_id = environ.get('FINANCE_CONTROL_DRIVE_FOLDER_ID')
    if not root_folder_id:
        return None
    from .drive_api import GoogleDriveApi
    return GoogleDriveApi(
        root_folder_id,
        account_id=environ.get('FINANCE_CONTROL_DRIVE_ACCOUNT_ID', 'google_drive'),
    )


def _local_packages(database):
    exports = outside_repository(database.parent / 'exports')
    if not exports.exists():
        return []
    return [outside_repository(path) for path in sorted(exports.glob('*.zip'))
            if re.fullmatch(r'[0-9a-f]{32}\.zip', path.name)]


def _sync_state_path(database):
    return outside_repository(database.parent / 'backup-sync-state.json')


def _read_sync_state(database):
    try:
        value = json.loads(_sync_state_path(database).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _write_sync_state(database, value):
    path = _sync_state_path(database)
    partial = path.with_name('.' + path.name + '.' + secrets.token_hex(8) + '.partial')
    try:
        with partial.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def _database_signature(database):
    signature = {}
    for suffix in ('', '-wal'):
        path = Path(str(database) + suffix)
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        signature[suffix or 'database'] = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    return signature


def _reporting_history_file(parent):
    """Return the validated optional reporting policy under this data directory."""
    candidate = parent / 'reporting-history.json'
    if not candidate.exists() and not candidate.is_symlink():
        return None
    resolved = outside_repository(candidate)
    if resolved.parent != parent or resolved.name != 'reporting-history.json':
        raise ValueError('redirected_reporting_history')
    if not resolved.is_file():
        raise ValueError('invalid_reporting_history_file')
    try:
        value = json.loads(resolved.read_text(encoding='utf-8'))
        from .reporting_history import validate
        validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError('invalid_reporting_history_file') from error
    return resolved


def _reporting_history_signature(parent):
    path = _reporting_history_file(parent)
    return _checksum(path) if path is not None else None


@_serialized_backup_state
def mark_backup_needed(database):
    """Mark a committed web write without inspecting private database content."""
    database = outside_repository(database)
    state = _read_sync_state(database)
    state['dirty_generation'] = secrets.token_hex(16)
    _write_sync_state(database, state)


def _publish_without_overwrite(partial, destination):
    """Atomically publish on supported platforms without replacing a peer's file."""
    if os.name == 'nt':
        os.rename(partial, destination)
        return
    os.link(partial, destination)
    partial.unlink()


def _copy_verified_package(source, database, target, *, allow_existing=True, verified=None):
    verified = verify_backup_package(source, database.parent) if verified is None else verified
    destination = target / f'finance-control-backup-{source.name}'
    if destination.exists():
        if (allow_existing and destination.is_file()
                and destination.stat().st_size == verified['size']
                and _checksum(destination) == verified['sha256']):
            return {**verified, 'filename': destination.name, 'copied': False}
        raise FileExistsError('drive_backup_already_exists')
    partial = target / f'.{source.name}.{secrets.token_hex(8)}.partial'
    try:
        with source.open('rb') as input_stream, partial.open('xb') as output_stream:
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b''):
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if partial.stat().st_size != verified['size'] or _checksum(partial) != verified['sha256']:
            raise ValueError('drive_backup_copy_mismatch')
        copied = verify_backup_package(partial, database.parent)
        if copied != verified:
            raise ValueError('drive_backup_final_verification_failed')
        _publish_without_overwrite(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
    return {**verified, 'filename': destination.name, 'copied': True}


def _drive_backup_entries(drive):
    try:
        entries = drive.list_files('04_Sicherungen')
    except DriveApiError as error:
        if error.code == 'drive_folder_not_found':
            return {}
        raise
    return {entry.relative_path: entry for entry in entries}


def _upload_verified_package(source, database, drive, *, allow_existing=True,
                             verified=None, existing=None):
    """Publish one immutable package below the configured Drive API root."""
    verified = verify_backup_package(source, database.parent) if verified is None else verified
    filename = f'finance-control-backup-{source.name}'
    relative = f'04_Sicherungen/{filename}'
    existing = _drive_backup_entries(drive) if existing is None else existing
    if relative in existing:
        entry = existing[relative]
        if (allow_existing and entry.size_bytes == verified['size']
                and entry.md5_checksum == _md5_checksum(source)):
            return {**verified, 'filename': filename, 'copied': False,
                    'md5_checksum': entry.md5_checksum}
        raise FileExistsError('drive_backup_already_exists')
    uploaded = drive.upload_file(relative, source)
    # GoogleDriveApi.upload_file already compares the Drive MD5 with a streamed
    # local MD5. Re-reading a potentially multi-GB package here adds no check.
    if uploaded.size_bytes != verified['size']:
        raise ValueError('drive_backup_upload_mismatch')
    return {**verified, 'filename': filename, 'copied': True,
            'md5_checksum': uploaded.md5_checksum}


def verify_backup_package(path, restore_directory):
    """Verify archive members and an extracted SQLite snapshot."""
    path = outside_repository(path)
    restore_directory = outside_repository(restore_directory)
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError('archive_integrity_failed')
        try:
            manifest = json.loads(archive.read('manifest.json'))
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError('invalid_backup_manifest') from error
        files = manifest.get('files')
        if (manifest.get('version') != 1 or manifest.get('database_integrity') != 'ok'
                or not isinstance(files, dict) or 'database.sqlite' not in files
                or len(archive.namelist()) != len(files) + 1
                or set(archive.namelist()) != {*files, 'manifest.json'}):
            raise ValueError('invalid_backup_manifest')
        for name, expected in files.items():
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9._/-]+', name)
                    or name.startswith('/') or '..' in Path(name).parts
                    or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)):
                raise ValueError('invalid_backup_manifest')
            digest = hashlib.sha256()
            with archive.open(name) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ValueError('backup_checksum_mismatch')
        with tempfile.TemporaryDirectory(dir=restore_directory) as temporary:
            restored = Path(temporary) / 'database.sqlite'
            with archive.open('database.sqlite') as source, restored.open('xb') as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            connection = sqlite3.connect(restored)
            try:
                if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('restored_database_integrity_failed')
            finally:
                connection.close()
    return {'sha256': _checksum(path), 'size': path.stat().st_size, 'files': len(files)}


def sync_backup_package(store, database, target_directory=None, environ=None):
    """Create, verify and atomically copy a package into a configured sync folder."""
    target = _configured_target(target_directory, environ)
    drive = None if target is not None else _configured_drive_api(environ)
    if target is None and drive is None:
        raise ValueError('drive_backup_directory_not_configured')
    database = outside_repository(database)
    if target is not None:
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise ValueError('drive_backup_directory_invalid')

    package = create_backup_package(store, database)
    source = outside_repository(database.parent / package['download_url'].lstrip('/'))
    exports = outside_repository(database.parent / 'exports')
    if source.parent != exports or not source.is_file():
        raise ValueError('backup_package_path_invalid')
    copied = (_copy_verified_package(source, database, target, allow_existing=False)
              if target is not None
              else _upload_verified_package(source, database, drive, allow_existing=False))
    return {'filename': copied['filename'], 'integrity': 'ok', 'sha256': copied['sha256'],
            'size': copied['size'], 'files': copied['files'], 'drive_copy': True,
            'cloud_upload': target is None}


def backup_sync_status(database, target_directory=None, environ=None):
    """Report the local outbox from receipts without touching a remote filesystem."""
    database = outside_repository(database)
    packages = _local_packages(database)
    state = _read_sync_state(database)
    try:
        target = _configured_target(target_directory, environ)
        drive = None if target is not None else _configured_drive_api(environ)
    except (ValueError, DriveApiError):
        target = None
        drive = None
        error = 'invalid_configuration'
    else:
        error = state.get('last_error')
    synced = state.get('synced_packages', {})
    if not isinstance(synced, dict):
        synced = {}
    pending = sum(path.name not in synced for path in packages)
    configured = target is not None or drive is not None or error == 'invalid_configuration'
    return {'configured': configured, 'reachable': bool(state.get('last_reachable', False)),
            'pending': pending, 'local_packages': len(packages),
            'last_drive_sync': state.get('last_drive_sync'), 'error': error}


@_serialized_backup_state
def sync_pending_backup_packages(database, target_directory=None, environ=None):
    """Flush the local immutable outbox; an unavailable target is a normal offline state."""
    database = outside_repository(database)
    packages = _local_packages(database)
    state = _read_sync_state(database)
    try:
        target = _configured_target(target_directory, environ)
        drive = None if target is not None else _configured_drive_api(environ)
    except (ValueError, DriveApiError):
        state.update({'last_reachable': False, 'last_error': 'invalid_configuration'})
        _write_sync_state(database, state)
        return {**backup_sync_status(database, target_directory, environ), 'synced': 0}
    if target is None and drive is None:
        state.update({'last_reachable': False, 'last_error': None})
        _write_sync_state(database, state)
        return {**backup_sync_status(database, target_directory, environ), 'synced': 0}
    if target is not None:
        try:
            if not target.is_dir():
                raise OSError('drive_backup_directory_unavailable')
        except OSError:
            state.update({'last_reachable': False, 'last_error': None})
            _write_sync_state(database, state)
            return {**backup_sync_status(database, target_directory, environ), 'synced': 0}

    verified_packages = state.setdefault('verified_packages', {})
    synced_packages = state.setdefault('synced_packages', {})
    copied = 0
    errors = []
    try:
        drive_entries = _drive_backup_entries(drive) if drive is not None else None
    except DriveApiError as error:
        state.update({'last_reachable': False, 'last_error': error.code})
        _write_sync_state(database, state)
        return {**backup_sync_status(database, target_directory, environ), 'synced': 0}
    for source in packages:
        try:
            # A cached restore check is reusable only for the exact current bytes.
            cached = verified_packages.get(source.name)
            source_size = source.stat().st_size
            source_hash = _checksum(source)
            if (isinstance(cached, dict) and cached.get('size') == source_size
                    and cached.get('sha256') == source_hash):
                verified = cached
            else:
                verified = verify_backup_package(source, database.parent)
            if verified_packages.get(source.name) != verified:
                verified_packages[source.name] = verified
                _write_sync_state(database, state)
            receipt = synced_packages.get(source.name)
            if target is not None:
                destination = target / f'finance-control-backup-{source.name}'
                if (isinstance(receipt, dict) and receipt.get('sha256') == verified['sha256']
                        and destination.is_file() and destination.stat().st_size == verified['size']
                        and _checksum(destination) == verified['sha256']):
                    continue
                copied_result = _copy_verified_package(source, database, target, verified=verified)
            else:
                filename = f'finance-control-backup-{source.name}'
                relative = f'04_Sicherungen/{filename}'
                remote = drive_entries.get(relative)
                if (isinstance(receipt, dict)
                        and receipt.get('sha256') == verified['sha256']
                        and receipt.get('size') == verified['size']
                        and receipt.get('md5_checksum')
                        and remote is not None
                        and remote.size_bytes == verified['size']
                        and remote.md5_checksum == receipt['md5_checksum']):
                    continue
                copied_result = _upload_verified_package(
                    source, database, drive, verified=verified, existing=drive_entries)
            synced_packages[source.name] = {
                'sha256': copied_result['sha256'], 'size': copied_result['size']}
            if copied_result.get('md5_checksum'):
                synced_packages[source.name]['md5_checksum'] = copied_result['md5_checksum']
            copied += int(copied_result['copied'])
            _write_sync_state(database, state)
        except (ValueError, FileExistsError, zipfile.BadZipFile):
            # A stale receipt cannot make a corrupt source or conflicting target
            # disappear from the pending count on the next status request.
            synced_packages.pop(source.name, None)
            _write_sync_state(database, state)
            errors.append(source.name)
            continue
        except OSError:
            state.update({'last_reachable': False, 'last_error': None})
            _write_sync_state(database, state)
            return {**backup_sync_status(database, target_directory, environ), 'synced': copied}
        except DriveApiError as error:
            if error.code in {
                    'drive_file_exists', 'drive_duplicate_name',
                    'drive_created_item_not_unique'}:
                errors.append(source.name)
                continue
            state.update({'last_reachable': False, 'last_error': error.code})
            _write_sync_state(database, state)
            return {**backup_sync_status(database, target_directory, environ), 'synced': copied}
    now = datetime.now(UTC).isoformat()
    state.update({'last_reachable': True,
                  'last_error': 'package_verification_or_target_conflict' if errors else None})
    if copied or (packages and not errors):
        state['last_drive_sync'] = now
    _write_sync_state(database, state)
    return {**backup_sync_status(database, target_directory, environ), 'synced': copied}


@_serialized_backup_state
def maintain_backup(store, database, target_directory=None, environ=None):
    """Create one local package for a changed database and opportunistically flush it."""
    database = outside_repository(database)
    state = _read_sync_state(database)
    starting_generation = state.get('dirty_generation')
    signature = _database_signature(database)
    reporting_history_signature = _reporting_history_signature(database.parent)
    created = False
    package = None
    dirty = state.get('dirty_generation') != state.get('backed_up_generation')
    if (dirty or state.get('database_signature') != signature
            or state.get('reporting_history_signature') != reporting_history_signature
            or not _local_packages(database)):
        package = create_backup_package(store, database)
        verified = verify_backup_package(
            outside_repository(database.parent / package['download_url'].lstrip('/')),
            database.parent)
        state = _read_sync_state(database)
        # SQLite backup may checkpoint a WAL file. Persist the post-backup state so
        # the next maintenance pass does not create a duplicate package.
        state['database_signature'] = _database_signature(database)
        state['reporting_history_signature'] = _reporting_history_signature(database.parent)
        state['backed_up_generation'] = starting_generation
        state['last_local_backup'] = datetime.now(UTC).isoformat()
        state['last_local_package'] = package['download_url'].rsplit('/', 1)[-1]
        state.setdefault('verified_packages', {})[state['last_local_package']] = verified
        _write_sync_state(database, state)
        created = True
    result = sync_pending_backup_packages(database, target_directory, environ)
    result.update({'created': created, 'download_url': package['download_url'] if package else None,
                   'last_local_backup': _read_sync_state(database).get('last_local_backup')})
    return result
