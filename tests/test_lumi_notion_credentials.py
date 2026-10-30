"""Synthetic HA options credential boundary; no real tokens or network."""
import json
import os
from pathlib import PurePosixPath
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_voice import lumi_notion_credentials as credentials
from codex_voice import lumi_notion_runtime


RAW = json.dumps({'lumi_notion_read_token': 'synthetic-read',
                  'lumi_notion_write_token': 'synthetic-write'}).encode()


def info(mode, *, uid=0, gid=0, links=1, size=100):
    return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid, st_nlink=links, st_size=size)


@pytest.fixture
def filesystem(monkeypatch):
    """Model Linux metadata/descriptor APIs without changing Windows permissions."""
    events = []
    file = info(stat.S_IFREG | 0o644)
    directory = info(stat.S_IFDIR | 0o755)
    opened = iter([10, 11, 12])

    def open_file(path, flags, **kwargs):
        events.append(('open', path, flags, kwargs))
        return next(opened)

    def chmod(fd, mode):
        events.append(('chmod', fd, mode))
        file.st_mode = stat.S_IFREG | mode

    def read(fd, size):
        events.append(('read', fd, size))
        return RAW

    fake = SimpleNamespace(name='posix', geteuid=lambda: 0,
        O_RDONLY=0, O_DIRECTORY=1, O_NOFOLLOW=2, O_CLOEXEC=4, O_NONBLOCK=8,
        open=Mock(side_effect=open_file), fstat=lambda fd: file if fd == 12 else directory,
        fchmod=Mock(side_effect=chmod), read=Mock(side_effect=read),
        close=Mock(side_effect=lambda fd: events.append(('close', fd))))
    monkeypatch.setattr(credentials, 'os', fake)
    monkeypatch.setattr(credentials, 'Path', PurePosixPath)
    return fake, file, directory, events


def test_root_options_protected_before_read_and_descriptors_closed(filesystem):
    fake, file, _, events = filesystem
    result = credentials.load_credentials(PurePosixPath('/data/options.json'))
    assert result.read_token == 'synthetic-read'
    assert result.write_token == 'synthetic-write'
    assert 'synthetic' not in repr(result)
    assert file.st_uid == file.st_gid == 0
    assert stat.S_IMODE(file.st_mode) == 0o600
    assert events.index(('chmod', 12, 0o600)) < events.index(('read', 12, 16385))
    assert set(call.args[0] for call in fake.close.call_args_list) == {10, 11, 12}
    assert fake.open.call_args_list[-1].kwargs == {'dir_fd': 11}
    assert fake.open.call_args_list[-1].args[1] & fake.O_NOFOLLOW


@pytest.mark.parametrize('attribute,value', [
    ('st_uid', 10001), ('st_gid', 10001), ('st_nlink', 2), ('st_size', 16385),
    ('st_mode', stat.S_IFLNK | 0o777), ('st_mode', stat.S_IFIFO | 0o600)])
def test_untrusted_file_never_read(filesystem, attribute, value):
    fake, file, _, _ = filesystem
    setattr(file, attribute, value)
    with pytest.raises(RuntimeError, match='^Notion options protection failed$'):
        credentials.load_credentials(PurePosixPath('/data/options.json'))
    fake.read.assert_not_called()
    fake.fchmod.assert_not_called()


@pytest.mark.parametrize('attribute,value', [
    ('st_uid', 10001), ('st_gid', 10001), ('st_mode', stat.S_IFDIR | 0o777),
    ('st_mode', stat.S_IFLNK | 0o755)])
def test_untrusted_ancestor_never_opens_options(filesystem, attribute, value):
    fake, _, directory, _ = filesystem
    setattr(directory, attribute, value)
    with pytest.raises(RuntimeError, match='^Notion options protection failed$'):
        credentials.load_credentials(PurePosixPath('/data/options.json'))
    fake.read.assert_not_called()
    assert fake.open.call_count == 1


def test_nonroot_never_opens_options(filesystem):
    fake, _, _, _ = filesystem
    fake.geteuid = lambda: 10001
    assert credentials.load_credentials(PurePosixPath('/data/options.json')) == credentials.NotionCredentials()
    fake.open.assert_not_called()


def test_missing_options_preserve_startup_without_credentials(filesystem):
    fake, _, _, _ = filesystem
    fake.open.side_effect = FileNotFoundError('synthetic missing options')
    assert credentials.load_credentials(PurePosixPath('/data/options.json')) == credentials.NotionCredentials()


@pytest.mark.parametrize('failure', ['open', 'chmod', 'mode', 'read-bound'])
def test_failed_protection_is_fatal_and_sanitized(filesystem, failure):
    fake, _, _, _ = filesystem
    if failure == 'open':
        fake.open.side_effect = OSError('synthetic secret must never appear')
    elif failure == 'chmod':
        fake.fchmod.side_effect = OSError('synthetic secret must never appear')
    elif failure == 'mode':
        fake.fchmod.side_effect = None
    else:
        fake.read.side_effect = None
        fake.read.return_value = b'x' * 16385
    with pytest.raises(RuntimeError, match='^Notion options protection failed$'):
        credentials.load_credentials(PurePosixPath('/data/options.json'))


@pytest.mark.parametrize('raw', [b'{}', b'{"other_lumi_option": true}',
    b'{"lumi_notion_write_token": "synthetic-write"}'])
def test_old_configuration_and_missing_read_token_disable_credentials(raw):
    assert credentials._parse(raw) == credentials.NotionCredentials()


@pytest.mark.parametrize('raw', [b'[]', b'null', b'not-json', b'\xff',
    b'{"lumi_notion_read_token": 12}', b'{"lumi_notion_read_token": "bad\\nvalue"}',
    b'{"lumi_notion_read_token": "a", "lumi_notion_read_token": "b"}',
    json.dumps({'lumi_notion_read_token': 'a' * 513}).encode()])
def test_invalid_options_rejected_without_echo(raw):
    with pytest.raises((ValueError, UnicodeError)):
        credentials._parse(raw)


def test_malformed_tokens_disable_credentials_after_file_protection(filesystem):
    fake, file, _, _ = filesystem
    fake.read.side_effect = None
    fake.read.return_value = b'{"lumi_notion_read_token": 12}'
    assert credentials.load_credentials(PurePosixPath('/data/options.json')) == credentials.NotionCredentials()
    assert stat.S_IMODE(file.st_mode) == 0o600


def test_runtime_ignores_environment_and_accepts_only_explicit_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv('LUMI_NOTION_READ_TOKEN', 'environment-must-not-be-used')
    monkeypatch.setenv('LUMI_NOTION_WRITE_TOKEN', 'environment-must-not-be-used')
    settings = SimpleNamespace(notion_write_enabled=True)
    instance = lumi_notion_runtime.prepare_runtime(tmp_path, settings, Mock())
    assert instance._read_token == instance._write_token == ''
    instance = lumi_notion_runtime.prepare_runtime(tmp_path, settings, Mock(),
        read_token='synthetic-read', write_token='synthetic-write')
    assert instance._read_token == 'synthetic-read'
    assert instance._write_token == 'synthetic-write'


@pytest.mark.skipif(os.name != 'posix', reason='Linux UID and permission test')
def test_real_root_protection_denies_codex_uid():
    if os.geteuid() != 0:
        pytest.skip('requires isolated root Linux runner')
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    # /tmp is intentionally untrusted. /run is traversable by the test UID, so
    # denial proves file permissions rather than an unrelated private ancestor.
    with tempfile.TemporaryDirectory(prefix='synthetic-notion-', dir='/run') as directory:
        Path(directory).chmod(0o755)
        path = Path(directory) / 'options.json'
        path.write_bytes(RAW)
        path.chmod(0o644)
        assert credentials.load_credentials(path).read_token == 'synthetic-read'
        code = """import os, sys
os.setgroups([])
os.setgid(10001)
os.setuid(10001)
for mode in ('rb', 'wb'):
    try:
        open(sys.argv[1], mode)
    except PermissionError:
        continue
    raise SystemExit('unexpected options access')
try:
    os.unlink(sys.argv[1])
except PermissionError:
    pass
else:
    raise SystemExit('unexpected options replacement access')
"""
        result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
        assert result.returncode == 0, result.stderr.decode()
