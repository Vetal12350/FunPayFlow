"""Offline only. All scratch must use --basetemp=tests/.offline-tmp/backup-create."""
from contextlib import closing, contextmanager
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'vps_backup_create.py'
spec = importlib.util.spec_from_file_location('backup_create', SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@pytest.fixture(autouse=True)
def windows_synthetic_permissions(monkeypatch):
    # CPython 3.13 mkdir(0700) replaces Windows ACLs and can exclude the Worker's
    # sandbox token. Linux permissions are tested through FakeLinux below.
    # Scope the translation to synthetic scratch; production Linux is untouched.
    if os.name == 'nt':
        original = os.mkdir
        scratch = SCRIPT.parents[1] / 'tests' / '.offline-tmp'

        def mkdir(path, mode=0o777, *, dir_fd=None):
            if mode == 0o700 and Path(path).absolute().is_relative_to(scratch):
                mode = 0o777
            return original(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, 'mkdir', mkdir)


class FakeLinux:
    """Simulated Linux metadata/service/mount APIs; never run production probes."""
    def __init__(self):
        self.status = 'inactive'
        self.processes = []
        self.readonly = False  # Supported v2 layout is a single writable ext4.
        self.authorization = True
        self.calls = []
        self.metadata = {}
        self.available = 100 * 1024**3
        self.busy = False
        self.published = []

    def validate(self, path, s, directory):
        mode = stat.S_IFMT(s.st_mode) | (0o700 if directory else 0o600)
        # Simulate Linux permissions, including on Windows, without trusting ACLs.
        values = dict(st_uid=0, st_mode=mode, st_nlink=s.st_nlink)
        values.update(self.metadata.get(path.name, {}))
        backup.secure_stat(SimpleNamespace(**values), directory)

    def paths(self, data, backups):
        self.calls.append('paths')
        for path in (data, backups):
            assert path.is_absolute() and path not in (backup.DATA, backup.BACKUPS)
            self.validate(path, path.lstat(), True)

    def authorized(self):
        self.calls.append('authorized')
        backup.require(self.authorization, 'maintenance_required')

    def quiescent(self, data):
        self.calls.append('quiescent')
        backup.require(self.status == 'inactive' and not self.processes, 'not_quiescent')

    def frozen(self, data):
        self.calls.append('frozen')
        backup.require(not self.readonly, 'source_not_frozen')

    def space(self, path, needed):
        self.calls.append('space')
        backup.require(needed <= self.available, 'insufficient_space')

    @contextmanager
    def lock(self, path):
        self.calls.append('lock')
        backup.require(not self.busy, 'busy')
        yield

    def sync_dir(self, path):
        self.calls.append('fsync')

    def publish(self, source, target):
        self.calls.append('publish')
        # Fake Linux renameat2(RENAME_NOREPLACE), portable to Windows test hosts.
        if target.exists():
            raise FileExistsError
        os.rename(source, target)
        self.published.append(target)


@pytest.fixture
def fixture(tmp_path):
    data, backups = tmp_path / 'data', tmp_path / 'backups'
    data.mkdir(mode=0o700)
    backups.mkdir(mode=0o700)
    values = {'.env': b'FAKE_ONLY=synthetic-marker-not-a-credential',
              'bot_settings.json': b'{"synthetic":true}',
              'stats_log.json': b'{"synthetic_count":4}',
              'progress.json': b'{"synthetic_cursor":19}',
              'bot.lock': b'999999'}
    for name, value in values.items():
        (data / name).write_bytes(value)
        (data / name).chmod(0o600)
    (data / 'imports').mkdir(mode=0o700)
    (data / 'empty').mkdir(mode=0o700)
    (data / 'imports' / 'synthetic asset.json').write_bytes(b'{"synthetic_import":3}')
    (data / 'imports' / 'synthetic asset.json').chmod(0o600)
    with closing(sqlite3.connect(data / 'state.sqlite3')) as db:
        db.execute('CREATE TABLE synthetic (value TEXT)')
        db.execute("INSERT INTO synthetic VALUES ('committed-before-backup')")
        db.commit()
    (data / 'state.sqlite3').chmod(0o600)
    return data, backups, FakeLinux()


def run(fixture):
    data, backups, platform = fixture
    return backup.create(data=data, backups=backups, platform=platform)


def assert_failed(fixture, status=None):
    existing = list(fixture[1].glob('snapshot-*'))
    result = run(fixture)
    assert result['status'] != 'created'
    assert not result['restore_verified']
    assert result['files'] == result['bytes'] == 0
    if status:
        assert result['status'] == status
    assert not fixture[2].published
    assert list(fixture[1].glob('snapshot-*')) == existing
    return result


def test_complete_backup_and_isolated_restore(fixture):
    data, backups, platform = fixture
    result = run(fixture)
    assert result['status'] == 'created', result
    assert result['restore_verified'] and result['files'] == 6
    assert len(platform.published) == 1
    bundle = platform.published[0]
    assert list(backups.iterdir()) == [bundle]
    manifest = json.loads((bundle / 'manifest.json').read_bytes())
    assert manifest['runtime_sha'] == manifest['code_compatibility'] == 'unknown'
    assert manifest['verification_scope'] == 'offline-filesystem-and-sqlite-only'
    assert manifest['excluded_transients'] == ['bot.lock']
    assert set(manifest['entries']) == {
        '.env', 'bot_settings.json', 'state.sqlite3', 'stats_log.json',
        'progress.json', 'imports', 'imports/synthetic asset.json', 'empty'}
    assert hashlib.sha256((bundle / 'data.tar').read_bytes()).hexdigest() == \
        manifest['archive_sha256']
    assert b'synthetic-marker' not in (bundle / 'manifest.json').read_bytes()
    assert platform.calls.count('quiescent') == 3
    assert platform.calls.index('publish') > platform.calls.index('fsync')
    # A second, separate extraction is offline and never invokes bot code.
    isolated = backups / 'isolated-test'
    owned = backup.Owned(platform)
    owned.mkdir(isolated)
    backup.archive_restore(bundle / 'data.tar', isolated, manifest['entries'],
                           owned, platform, backup.Budget())
    assert (isolated / 'progress.json').read_bytes() == (data / 'progress.json').read_bytes()
    assert not (isolated / 'bot.lock').exists()
    with closing(sqlite3.connect(isolated / 'state.sqlite3')) as db:
        assert db.execute('SELECT value FROM synthetic').fetchall() == \
            [('committed-before-backup',)]
    owned.cleanup()


@pytest.mark.parametrize('status,processes', [
    ('active', []), ('unknown', []), ('failed', []), ('inactive', ['one']),
    ('inactive', ['one', 'two'])])
def test_quiescence_before_private_reads_or_snapshot(fixture, monkeypatch, status, processes):
    fixture[2].status, fixture[2].processes = status, processes
    monkeypatch.setattr(backup, 'inventory', lambda *a: pytest.fail('private source read'))
    assert_failed(fixture, 'not_quiescent')
    assert list(fixture[1].iterdir()) == []
    assert 'lock' not in fixture[2].calls


def test_unsupported_layout_before_private_read(fixture, monkeypatch):
    fixture[2].readonly = True
    monkeypatch.setattr(backup, 'inventory', lambda *a: pytest.fail('private source read'))
    assert_failed(fixture, 'source_not_frozen')
    assert list(fixture[1].iterdir()) == []


def test_exclusive_lock_before_inventory(fixture, monkeypatch):
    fixture[2].busy = True
    monkeypatch.setattr(backup, 'inventory', lambda *a: pytest.fail('private source read'))
    assert_failed(fixture, 'busy')


def test_rw_same_device_requires_coordinator_before_any_payload_or_artifact(fixture, monkeypatch):
    assert fixture[0].stat().st_dev == fixture[1].stat().st_dev
    fixture[2].authorization = False
    monkeypatch.setattr(backup, 'inventory', lambda *a: pytest.fail('private source read'))
    assert_failed(fixture, 'maintenance_required')
    assert fixture[2].calls == ['authorized']
    assert list(fixture[1].iterdir()) == []


@pytest.mark.parametrize('phase', ['archive_restore', 'publish'])
def test_coordinator_loss_during_snapshot(fixture, monkeypatch, phase):
    target = backup if phase == 'archive_restore' else fixture[2]
    original = getattr(target, phase)

    def lose(*args):
        original(*args)
        fixture[2].authorization = False

    monkeypatch.setattr(target, phase, lose)
    result = run(fixture)
    assert result['status'] == ('publication_unknown' if phase == 'publish'
                                else 'maintenance_required')
    assert not result['restore_verified']
    if phase == 'publish':
        assert (fixture[2].published[0] / 'data.tar').exists()
        assert list(fixture[1].glob('.pending-*'))
    else:
        assert list(fixture[1].iterdir()) == []


def test_attestation_cannot_change_or_extend_lease(monkeypatch):
    evidence = iter([(200, b'one'), (200, b'two')])
    monkeypatch.setattr(backup, 'maintenance_evidence', lambda: next(evidence))
    linux = backup.Linux()
    linux.authorized()
    with pytest.raises(backup.Refused) as rejected:
        linux.authorized()
    assert rejected.value.code == 'maintenance_required'


def test_linux_same_device_paths_and_rw_flags_are_simulated(monkeypatch):
    # Exercise actual Linux methods rather than only the injected FakeLinux.
    class FakePath(PurePosixPath):
        def lstat(self):
            return SimpleNamespace(st_dev=7)

        stat = lstat

    data, backups = FakePath('/root/FunPayFlow-data'), FakePath('/root/backups')
    monkeypatch.setattr(backup, 'DATA', data)
    monkeypatch.setattr(backup, 'BACKUPS', backups)
    monkeypatch.setattr(backup, 'Path', FakePath)
    monkeypatch.setattr(backup, 'protected', lambda *a: None)
    monkeypatch.setattr(backup, 'secure_stat', lambda *a: None)
    monkeypatch.setattr(backup, 'read_bounded', lambda *a:
                        b'10 1 0:7 / / rw,relatime - ext4 /dev/fake rw,commit=30\n')
    monkeypatch.setattr(backup, 'os', SimpleNamespace(
        statvfs=lambda p: SimpleNamespace(f_flag=0), ST_RDONLY=1,
        major=lambda dev: 0, minor=lambda dev: dev))
    linux = backup.Linux()
    linux.paths(data, backups)
    linux.frozen(data)


@pytest.mark.parametrize('fault', [None, 'file_limit', 'churn', 'hardlink', 'symlink', 'public'])
def test_metadata_only_source_inode_walk(fixture, monkeypatch, fault):
    data = fixture[0]
    real_secure = backup.secure_stat

    def synthetic_stat(s, directory):
        real_secure(SimpleNamespace(st_uid=0, st_nlink=s.st_nlink,
                    st_mode=(stat.S_IFLNK | 0o600 if fault == 'symlink' and not directory
                             else stat.S_IFMT(s.st_mode) | (0o700 if directory else
                                  0o644 if fault == 'public' else 0o600))), directory)

    monkeypatch.setattr(backup, 'secure_stat', synthetic_stat)
    monkeypatch.setattr(backup, 'consume', lambda *a: pytest.fail('payload read'))
    if fault == 'file_limit':
        monkeypatch.setattr(backup, 'MAX_ENTRIES', 1)
    elif fault == 'hardlink':
        os.link(data / 'progress.json', data / 'linked')
    elif fault == 'churn':
        original = backup.os.scandir

        @contextmanager
        def changing(path):
            with original(path) as entries:
                yield entries
            if path == data:
                (data / 'late-file').write_bytes(b'synthetic')

        monkeypatch.setattr(backup.os, 'scandir', changing)
    if fault == 'churn':
        # On Windows, a directory's stat identity may remain unchanged when a
        # new child appears. source_inodes can therefore miss the mutation
        # within one walk; the caller also compares its pre/post inode sets.
        # Accept either immediate Refused or detection by the second walk.
        try:
            before = backup.source_inodes(data)
        except backup.Refused:
            return
        after = backup.source_inodes(data)
        assert before != after
    elif fault:
        with pytest.raises(backup.Refused):
            backup.source_inodes(data)
    else:
        expected = {(p.stat().st_dev, p.stat().st_ino) for p in (data, *data.rglob('*'))}
        assert backup.source_inodes(data) == expected


def test_space_admission_accounts_for_wal_expansion(fixture, monkeypatch):
    needed = []
    monkeypatch.setattr(fixture[2], 'space', lambda path, count: needed.append(count))
    assert run(fixture)['status'] == 'created'
    assert needed[0] >= backup.MAX_FILE * 4 + backup.RESERVE


def test_nested_persistent_settings_progress_and_history_preserved(fixture):
    data = fixture[0]
    (data / 'settings').mkdir()
    (data / 'settings' / 'nested.json').write_bytes(b'{"synthetic_progress":2}')
    (data / 'orders-history.json').write_bytes(b'[{"synthetic_order":1}]')
    assert run(fixture)['status'] == 'created'
    manifest = json.loads((fixture[2].published[0] / 'manifest.json').read_bytes())
    for name in ('settings/nested.json', 'orders-history.json'):
        assert manifest['entries'][name]['sha256'] == hashlib.sha256((data / name).read_bytes()).hexdigest()


@pytest.mark.parametrize('missing', sorted(backup.REQUIRED))
def test_missing_essential_fails(fixture, missing):
    (fixture[0] / missing).unlink()
    assert_failed(fixture, 'unsafe_source')


@pytest.mark.parametrize('attributes', [
    {'st_uid': 1000}, {'st_mode': stat.S_IFREG | 0o644},
    {'st_mode': stat.S_IFREG | 0o660}, {'st_mode': stat.S_IFREG | 0o700},
    {'st_mode': stat.S_IFLNK | 0o600}, {'st_mode': stat.S_IFSOCK | 0o600},
    {'st_mode': stat.S_IFIFO | 0o600}, {'st_mode': stat.S_IFCHR | 0o600},
    {'st_nlink': 2}, {'st_mode': stat.S_IFREG | 0o200},
])
def test_unsafe_source_metadata(fixture, attributes):
    fixture[2].metadata['progress.json'] = attributes
    assert_failed(fixture, 'unsafe_source')


@pytest.mark.parametrize('attributes', [
    {'st_uid': 42}, {'st_mode': stat.S_IFDIR | 0o755},
    {'st_mode': stat.S_IFLNK | 0o700}, {'st_mode': stat.S_IFDIR | 0o600},
])
def test_unsafe_directory_metadata(fixture, attributes):
    fixture[2].metadata['imports'] = attributes
    assert_failed(fixture, 'unsafe_source')


def test_actual_hardlinks_rejected(fixture):
    os.link(fixture[0] / 'progress.json', fixture[0] / 'linked')
    assert_failed(fixture, 'unsafe_source')


@pytest.mark.parametrize('name', [
    '../escape', '/absolute', 'C:/drive', 'bad\\name', 'a//b', 'a/./b',
    'a/../b', 'bad\nname', 'nul', 'CON.txt', 'trailing.', 'trailing ',
    'nonascii-\u0436', 'x' * 101, 'x/' * 13 + 'end'])
def test_unusual_names_refuse(name):
    with pytest.raises(backup.Refused):
        backup.safe_name(name)


def test_portable_case_collisions(fixture, monkeypatch):
    # Pure enumeration fake also exercises Windows, where case aliases collapse.
    original = os.scandir

    @contextmanager
    def collision(path):
        with original(path) as entries:
            entries = list(entries)
        if path == fixture[0]:
            entries.append(SimpleNamespace(name='PROGRESS.JSON'))
        yield iter(entries)

    monkeypatch.setattr(backup.os, 'scandir', collision)
    assert_failed(fixture, 'unsafe_source')


def test_low_space_creates_no_snapshot(fixture):
    fixture[2].available = 1
    assert_failed(fixture, 'insufficient_space')
    assert list(fixture[1].iterdir()) == []


@pytest.mark.parametrize('limit', ['MAX_ENTRIES', 'MAX_TOTAL', 'MAX_FILE', 'MAX_DEPTH',
                                  'MAX_ARCHIVE', 'MAX_MANIFEST'])
def test_resource_limits(fixture, monkeypatch, limit):
    monkeypatch.setattr(backup, limit, 1)
    assert_failed(fixture, 'limit_exceeded')
    assert list(fixture[1].iterdir()) == []


def test_deadline(fixture, monkeypatch):
    monkeypatch.setattr(backup, 'SECONDS', -1)
    assert_failed(fixture, 'limit_exceeded')


def test_corrupt_db_and_hot_journal(fixture):
    (fixture[0] / 'state.sqlite3').write_bytes(b'private-invalid-database')
    assert_failed(fixture)
    assert list(fixture[1].iterdir()) == []
    (fixture[0] / 'state.sqlite3-journal').write_bytes(b'synthetic-hot-journal')
    assert_failed(fixture, 'unsafe_source')


def test_wal_commits_survive_without_restoring_shm(fixture):
    data = fixture[0]
    # Reader simulates a frozen filesystem retaining WAL. No live production DB.
    with closing(sqlite3.connect(data / 'state.sqlite3')) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("INSERT INTO synthetic VALUES ('committed-in-wal')")
        db.commit()
        assert (data / 'state.sqlite3-wal').stat().st_size > 0
        result = run(fixture)
        assert result['status'] == 'created', result
    bundle = fixture[2].published[0]
    manifest = json.loads((bundle / 'manifest.json').read_bytes())
    assert manifest['excluded_transients'] == sorted(backup.TRANSIENT)
    isolated = fixture[1] / 'wal-check'
    owned = backup.Owned(fixture[2])
    owned.mkdir(isolated)
    backup.archive_restore(bundle / 'data.tar', isolated, manifest['entries'], owned,
                           fixture[2], backup.Budget())
    with closing(sqlite3.connect(isolated / 'state.sqlite3')) as db:
        assert db.execute('SELECT value FROM synthetic').fetchall() == [
            ('committed-before-backup',), ('committed-in-wal',)]
    assert not (isolated / 'state.sqlite3-wal').exists()
    owned.cleanup()


def test_sqlite_never_opens_original_data(fixture, monkeypatch):
    original = sqlite3.connect

    def connect(path, *args, **kwargs):
        assert str(fixture[0]).replace('\\', '/') not in str(path).replace('\\', '/')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(backup.sqlite3, 'connect', connect)
    assert run(fixture)['status'] == 'created'


@pytest.mark.parametrize('damage', ['magic', 'version', 'page_size', 'header_checksum',
                                  'frame_checksum', 'salt', 'truncated', 'tail'])
def test_invalid_wal_never_silently_drops_commits(fixture, monkeypatch, damage):
    data = fixture[0]
    original = backup.sqlite_snapshot

    def corrupt(input_db, *args):
        wal = Path(str(input_db) + '-wal')
        raw = bytearray(wal.read_bytes())
        offsets = dict(magic=0, version=4, page_size=8, header_checksum=24,
                       frame_checksum=48, salt=40)
        if damage in offsets:
            raw[offsets[damage]] ^= 1
        elif damage == 'truncated':
            raw = raw[:-1]
        else:
            raw.extend(b'uncommitted-or-stale')
        wal.write_bytes(raw)
        return original(input_db, *args)

    monkeypatch.setattr(backup, 'sqlite_snapshot', corrupt)
    with closing(sqlite3.connect(data / 'state.sqlite3')) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("INSERT INTO synthetic VALUES ('committed-in-wal')")
        db.commit()
        assert_failed(fixture, 'verification_failed')


def test_empty_wal_is_safe(fixture):
    (fixture[0] / 'state.sqlite3-wal').write_bytes(b'')
    assert run(fixture)['status'] == 'created'


@pytest.mark.parametrize('change', ['file', 'add', 'remove', 'service', 'mount'])
def test_post_snapshot_rechecks(fixture, monkeypatch, change):
    original = backup.archive_restore

    def changed(*args):
        original(*args)
        if change == 'file':
            (fixture[0] / 'progress.json').write_bytes(b'changed')
        elif change == 'add':
            (fixture[0] / 'new-progress').write_bytes(b'new')
        elif change == 'remove':
            (fixture[0] / 'progress.json').unlink()
        elif change == 'service':
            fixture[2].status = 'active'
        else:
            fixture[2].readonly = True

    monkeypatch.setattr(backup, 'archive_restore', changed)
    assert_failed(fixture)
    assert list(fixture[1].iterdir()) == []


def test_changed_file_during_copy(fixture, monkeypatch):
    original = backup.consume

    def mutate(path, platform, budget, output=None, **kwargs):
        result = original(path, platform, budget, output, **kwargs)
        if path == fixture[0] / 'progress.json' and output is not None:
            path.write_bytes(b'changed-after-read')
        return result

    monkeypatch.setattr(backup, 'consume', mutate)
    assert_failed(fixture, 'unsafe_source')


@pytest.mark.parametrize('phase', ['archive_create', 'archive_restore', 'sqlite_snapshot'])
def test_interrupted_artifact_is_not_published(fixture, monkeypatch, phase):
    original = getattr(backup, phase)

    def interrupted(*args):
        original(*args)
        raise OSError('PRIVATE path or credential must not escape')

    monkeypatch.setattr(backup, phase, interrupted)
    assert_failed(fixture, 'io_failed')
    assert list(fixture[1].iterdir()) == []


@pytest.mark.parametrize('phase', ['copy', 'archive'])
def test_partial_writes_are_cleaned_without_publication(fixture, monkeypatch, phase):
    if phase == 'copy':
        original = backup.consume

        def partial(path, platform, budget, output=None, **kwargs):
            if path == fixture[0] / 'progress.json' and output is not None:
                output.write(b'partial')
                raise OSError('synthetic interruption')
            return original(path, platform, budget, output, **kwargs)

        monkeypatch.setattr(backup, 'consume', partial)
    else:
        def partial_archive(path, tree, entries, owned, platform, budget):
            with owned.file(path) as stream:
                stream.write(b'partial-tar-header')
                raise KeyboardInterrupt

        monkeypatch.setattr(backup, 'archive_create', partial_archive)
    assert_failed(fixture, 'io_failed')
    assert list(fixture[1].iterdir()) == []


def test_open_identity_race_refuses(fixture, monkeypatch):
    original = backup.os.fstat

    def switched(fd):
        s = original(fd)
        return SimpleNamespace(st_dev=s.st_dev, st_ino=s.st_ino + 1, st_size=s.st_size,
                               st_mtime_ns=s.st_mtime_ns, st_ctime_ns=s.st_ctime_ns,
                               st_mode=s.st_mode, st_nlink=s.st_nlink)

    monkeypatch.setattr(backup.os, 'fstat', switched)
    with pytest.raises(backup.Refused):
        backup.open_read(fixture[0] / 'progress.json', fixture[2])


@pytest.mark.parametrize('damage', ['payload', 'header', 'truncated', 'trailing', 'path'])
def test_tampered_archive_refuses_and_cleans(fixture, monkeypatch, damage):
    original = backup.archive_create

    def corrupt(path, *args):
        original(path, *args)
        value = bytearray(path.read_bytes())
        if damage == 'payload':
            value[512] ^= 1
        elif damage == 'header':
            value[148] ^= 1
        elif damage == 'truncated':
            value = value[:-512]
        elif damage == 'trailing':
            value.extend(b'bad')
        else:
            value[0:3] = b'../'
        path.write_bytes(value)

    monkeypatch.setattr(backup, 'archive_create', corrupt)
    assert_failed(fixture, 'verification_failed')
    assert list(fixture[1].iterdir()) == []


def test_staged_sqlite_integrity_failure(fixture, monkeypatch):
    original = backup.check_db
    count = 0

    def fail_on_recovery(path, budget):
        nonlocal count
        count += 1
        original(path, budget)
        if count == 2:
            raise backup.Refused('verification_failed')

    monkeypatch.setattr(backup, 'check_db', fail_on_recovery)
    assert_failed(fixture, 'verification_failed')
    assert count == 2


def test_no_overwrite_or_retention_deletion(fixture, monkeypatch):
    token = 'a' * 32
    monkeypatch.setattr(backup.uuid, 'uuid4', lambda: SimpleNamespace(hex=token))
    old = fixture[1] / ('snapshot-' + token)
    old.mkdir()
    (old / 'keep').write_bytes(b'old-backup-unchanged')
    assert_failed(fixture)
    assert (old / 'keep').read_bytes() == b'old-backup-unchanged'
    assert old in list(fixture[1].iterdir())
    assert list(fixture[1].glob('.pending-*/bundle/data.tar'))


def test_existing_pending_directory_never_reused(fixture, monkeypatch):
    token = 'b' * 32
    monkeypatch.setattr(backup.uuid, 'uuid4', lambda: SimpleNamespace(hex=token))
    old = fixture[1] / ('.pending-' + token)
    old.mkdir()
    (old / 'keep').write_bytes(b'old-interrupted-backup')
    assert_failed(fixture, 'io_failed')
    assert (old / 'keep').read_bytes() == b'old-interrupted-backup'


def test_cleanup_refuses_replaced_inode(fixture, monkeypatch):
    original = backup.archive_create

    def replace(path, *args):
        original(path, *args)
        replacement = path.with_name('replacement')
        replacement.write_bytes(b'do-not-delete')
        os.replace(replacement, path)
        raise OSError

    monkeypatch.setattr(backup, 'archive_create', replace)
    assert_failed(fixture, 'cleanup_failed')
    paths = list(fixture[1].glob('.pending-*/bundle/data.tar'))
    assert len(paths) == 1 and paths[0].read_bytes() == b'do-not-delete'


def test_fsync_failure_after_publication_is_unknown(fixture, monkeypatch):
    original = fixture[2].sync_dir

    def fail(path):
        original(path)
        if path == fixture[1]:
            raise OSError

    monkeypatch.setattr(fixture[2], 'sync_dir', fail)
    result = run(fixture)
    assert result['status'] == 'publication_unknown'
    assert not result['restore_verified']
    assert len(fixture[2].published) == 1
    assert (fixture[2].published[0] / 'data.tar').exists()


@pytest.mark.parametrize('arguments', [[], ['create', '/private'], ['--help'],
                                      ['create', '--dry-run'], ['stop'], ['audit']])
def test_cli_fixed_action_only(arguments, monkeypatch, capsys):
    monkeypatch.setattr(backup, 'trusted_installation', lambda: pytest.fail('trust invoked'))
    assert backup.main(arguments) == 2
    assert json.loads(capsys.readouterr().out) == backup.report('invalid_arguments')


def test_checkout_never_authorizes_production(monkeypatch, capsys):
    monkeypatch.setattr(backup, 'create', lambda **kw: pytest.fail('production entered'))
    assert backup.main(['create']) == 1
    assert json.loads(capsys.readouterr().out) == backup.report('untrusted_installation')


@pytest.mark.parametrize('fault', [
    None, 'wrong_script', 'argv_alias', 'nonroot', 'not_isolated', 'site_enabled',
    'script_writable', 'ancestor_writable', 'symlink', 'hardlink', 'checkout',
    'receipt_hash', 'receipt_commit', 'receipt_extra', 'extra_argument',
    'missing_no_site', 'untrusted_interpreter'])
def test_trusted_installation_with_simulated_linux(monkeypatch, fault):
    installed = '/usr/local/libexec/funpayflow-backup-create.py'
    receipt_path = '/usr/local/libexec/funpayflow-backup-create.reviewed.json'
    code = b'synthetic reviewed bytes'
    receipt = {'reviewed_commit': 'a' * 40, 'script_sha256': hashlib.sha256(code).hexdigest()}
    if fault == 'receipt_hash':
        receipt['script_sha256'] = 'b' * 64
    if fault == 'receipt_commit':
        receipt['reviewed_commit'] = 'not-a-pin'
    if fault == 'receipt_extra':
        receipt['command'] = 'forbidden'

    class FakePath(PurePosixPath):
        def resolve(self):
            if fault == 'symlink' and str(self) == installed:
                return FakePath('/checkout/script.py')
            return self

        def exists(self):
            if self.name == '.git':
                return fault == 'checkout' and str(self.parent) == '/usr/local'
            return True

        def lstat(self):
            directory = not (str(self).endswith(('.py', '.json'))
                             or str(self) == '/usr/bin/python3.13')
            mode = (stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | 0o644)
            if str(self) == '/usr/bin/python3.13':
                mode = stat.S_IFREG | 0o755
            if fault == 'script_writable' and str(self) == installed:
                mode |= 0o002
            if fault == 'ancestor_writable' and str(self) == '/usr/local':
                mode |= 0o020
            return SimpleNamespace(st_mode=mode, st_uid=0,
                                   st_nlink=2 if fault == 'hardlink' and str(self) == installed else 1)

    command = [b'/usr/bin/python3.13', b'-I', b'-S', installed.encode(), b'create', b'']
    if fault == 'extra_argument':
        command.insert(-1, b'--override')
    if fault == 'missing_no_site':
        command.remove(b'-S')
    values = {'/proc/self/cmdline': b'\0'.join(command), installed: code,
              receipt_path: json.dumps(receipt).encode()}
    monkeypatch.setattr(backup, 'Path', FakePath)
    monkeypatch.setattr(backup, 'INSTALLED', FakePath(installed))
    monkeypatch.setattr(backup, 'RECEIPT', FakePath(receipt_path))
    monkeypatch.setattr(backup, '__file__', '/checkout/script.py' if fault == 'wrong_script'
                        else installed)
    monkeypatch.setattr(backup, 'sys', SimpleNamespace(
        platform='linux', flags=SimpleNamespace(isolated=fault != 'not_isolated',
        no_site=fault != 'site_enabled', ignore_environment=True),
        argv=['/alias.py' if fault == 'argv_alias' else installed, 'create'],
        executable='/tmp/python3.13' if fault == 'untrusted_interpreter'
        else '/usr/bin/python3.13', path=['/usr/lib/python3.13']))
    for name in ('getuid', 'geteuid', 'getgid', 'getegid'):
        monkeypatch.setattr(backup.os, name, lambda: 1000 if fault == 'nonroot' else 0,
                            raising=False)
    monkeypatch.setattr(backup, 'read_bounded', lambda path, maximum: values[str(path)])
    if fault is None:
        backup.trusted_installation()
    else:
        with pytest.raises(backup.Refused) as rejected:
            backup.trusted_installation()
        assert rejected.value.code == 'untrusted_installation'


@pytest.mark.parametrize('fault', [None, 'active', 'legacy_active', 'two_bots', 'unknown',
                                 'writer', 'reader', 'mapping', 'readonly_mapping',
                                 'unrelated_mapping', 'fd_path', 'mapped_path',
                                 'cwd', 'pid_changed', 'namespace'])
def test_linux_quiescence_apis_are_simulated(monkeypatch, fault):
    linux = backup.Linux()
    metadata = SimpleNamespace(st_dev=7, st_ino=1 if fault in ('writer', 'reader') else 9)
    data = PurePosixPath('/synthetic/data')
    linux.evidence = (200,)
    monkeypatch.setattr(linux, 'authorized', lambda: None)
    monkeypatch.setattr(backup, 'source_inodes', lambda data: {(7, 1)})
    inactive = (b'LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
                b'MainPID=0\nControlPID=0\nJob=\n')
    probes = []

    def status(unit):
        probes.append(unit)
        if fault == 'active' and unit == backup.UNITS[0] or \
                fault == 'legacy_active' and unit == backup.UNITS[1]:
            return inactive.replace(b'ActiveState=inactive', b'ActiveState=active')
        return inactive

    monkeypatch.setattr(backup, 'systemd_status', status)
    monkeypatch.setattr(backup, 'protected', lambda *a, **kw: None)
    calls = 0

    def pids():
        nonlocal calls
        calls += 1
        return {'100', '200', '201'} | ({'999'} if calls > 1 and fault == 'pid_changed' else set())

    monkeypatch.setattr(linux, 'pids', pids)

    def readlink(path):
        value = str(path).replace('\\', '/')
        if '/ns/' in value:
            return 'other' if fault == 'namespace' and '/self/' in value else 'namespace'
        if fault == 'cwd' and value.endswith('/cwd'):
            return '/synthetic/data'
        if fault == 'fd_path' and value == 'synthetic-fd':
            return '/synthetic/data/not-yet-in-inventory (deleted)'
        return '/usr/bin/synthetic-idle'

    @contextmanager
    def scandir(path):
        yield iter([SimpleNamespace(path='synthetic-fd', name='3')])

    monkeypatch.setattr(backup, 'os', SimpleNamespace(
        readlink=readlink, stat=lambda *a, **kw: metadata, scandir=scandir,
        getpid=lambda: 100, makedev=lambda a, b: b, fsencode=os.fsencode, fsdecode=os.fsdecode,
        O_ACCMODE=3, O_RDONLY=0))

    def read(path, maximum):
        value = str(path).replace('\\', '/')
        if value.endswith('/comm'):
            return b'systemd\n'
        if fault == 'unknown' and value.endswith('/cmdline'):
            raise PermissionError
        if value.endswith('/cmdline'):
            return b'python3 -m funpayflow.main' if fault == 'two_bots' else b'synthetic-idle'
        if value.endswith('/stat'):
            return b'200 (synthetic) S 1 1 1 1 1 0'
        if '/fdinfo/' in value:
            return b'flags:\t02\n' if fault == 'writer' else b'flags:\t00\n'
        if value.endswith('/maps'):
            if fault == 'unrelated_mapping':
                return b'0-1 rw-s 0000 00:07 9 /var/log/synthetic'
            if fault == 'mapped_path':
                return b'0-1 rw-s 0000 00:07 9 /synthetic/data/new-file (deleted)'
            return (b'0-1 r--s 0000 00:07 1 /synthetic' if fault == 'readonly_mapping'
                    else b'0-1 rw-s 0000 00:07 1 /synthetic' if fault == 'mapping' else b'')
        raise AssertionError('unexpected fake API')

    monkeypatch.setattr(backup, 'read_bounded', read)
    if fault in (None, 'unrelated_mapping'):
        linux.quiescent(data)
        assert probes == list(backup.UNITS)
    else:
        with pytest.raises((backup.Refused, PermissionError)):
            linux.quiescent(data)


@pytest.mark.parametrize('busy', [False, True])
def test_linux_lock_has_permanent_inode_with_fake_flock(fixture, monkeypatch, busy):
    linux = backup.Linux()
    monkeypatch.setattr(linux, 'validate', fixture[2].validate)
    fake_os = SimpleNamespace(**{name: getattr(os, name) for name in
                              ('open', 'close', 'fstat', 'O_RDWR', 'O_CREAT', 'O_EXCL')})
    fake_os.name = os.name
    fake_os.O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
    fake_os.O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
    monkeypatch.setattr(backup, 'os', fake_os)
    calls = []

    def flock(fd, flags):
        calls.append(flags)
        if busy:
            raise BlockingIOError

    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(
        flock=flock, LOCK_EX=2, LOCK_NB=4))
    if busy:
        with pytest.raises(backup.Refused) as rejected:
            with linux.lock(fixture[1]):
                pytest.fail('busy lock entered')
        assert rejected.value.code == 'busy'
    else:
        with linux.lock(fixture[1]):
            first = (fixture[1] / '.backup-create.lock').stat().st_ino
        with linux.lock(fixture[1]):
            assert (fixture[1] / '.backup-create.lock').stat().st_ino == first
    assert calls and set(calls) == {6}
    assert (fixture[1] / '.backup-create.lock').exists()


@pytest.mark.parametrize('returncode', [0, -1])
def test_publication_uses_noreplace_syscall_only(monkeypatch, returncode):
    calls = []

    class Rename:
        def __call__(self, *args):
            calls.append(args)
            return returncode

    rename = Rename()
    monkeypatch.setattr(backup.ctypes, 'CDLL', lambda *a, **kw: SimpleNamespace(renameat2=rename))
    source, target = Path('/synthetic/pending'), Path('/synthetic/snapshot')
    if returncode:
        with pytest.raises(backup.Refused):
            backup.Linux.publish(source, target)
    else:
        backup.Linux.publish(source, target)
    assert calls == [(-100, os.fsencode(source), -100, os.fsencode(target), 1)]


@pytest.mark.parametrize('fault', [None, 'overflow', 'timeout', 'nonzero'])
def test_systemctl_invocation_bounded_and_read_only(monkeypatch, fault):
    calls = []

    class Process:
        stdout = SimpleNamespace(fileno=lambda: 42)
        alive = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self, timeout):
            self.alive = False
            return 1 if fault == 'nonzero' else 0

        def poll(self):
            return None if self.alive else 0

        def kill(self):
            calls.append('kill-query-child')
            self.alive = False

    def popen(command, **kwargs):
        assert command == ['/usr/bin/systemctl', '--system', '--no-pager', 'show',
                           'funpayflow.service',
                           '--property=LoadState,ActiveState,SubState,MainPID,ControlPID,Job']
        assert kwargs['env'] == {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                                 'SYSTEMD_PAGER': ''}
        assert 'shell' not in kwargs
        calls.append('query')
        return Process()

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def register(self, *args):
            pass

        def select(self, timeout):
            return [] if fault == 'timeout' else [True]

    blocks = iter([b'x' * 4097 if fault == 'overflow' else b'synthetic', b''])
    monkeypatch.setattr(backup.subprocess, 'Popen', popen)
    monkeypatch.setattr(backup.selectors, 'DefaultSelector', Selector)
    monkeypatch.setattr(backup.os, 'read', lambda fd, n: next(blocks))
    if fault:
        with pytest.raises(backup.Refused):
            backup.systemd_status(backup.UNITS[0])
    else:
        assert backup.systemd_status(backup.UNITS[0]) == b'synthetic'
    assert calls == (['query', 'kill-query-child'] if fault in ('overflow', 'timeout')
                     else ['query'])


def test_output_sanitized(fixture, monkeypatch, capsys):
    def fail():
        raise OSError('PRIVATE FILENAME SECRET TOKEN')

    monkeypatch.setattr(backup, 'trusted_installation', fail)
    assert backup.main(['create']) == 1
    output = capsys.readouterr()
    assert output.err == ''
    assert json.loads(output.out) == backup.report('io_failed')
    assert 'PRIVATE' not in output.out
    assert run(fixture)['status'] == 'created'
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('replacement', [
    b'ActiveState=active', b'SubState=running', b'MainPID=123', b'ControlPID=45',
    b'LoadState=not-found', b'Job=22', b'Extra=unknown'])
def test_service_parser_refuses_ambiguity(replacement):
    fields = dict(LoadState='loaded', ActiveState='inactive', SubState='dead',
                  MainPID='0', ControlPID='0', Job='')
    key, value = replacement.decode().split('=')
    fields[key] = value
    with pytest.raises(backup.Refused):
        backup.inactive('\n'.join(f'{k}={v}' for k, v in fields.items()).encode())


def test_service_parser_exact_success():
    backup.inactive(b'LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
                    b'MainPID=0\nControlPID=0\nJob=\n')
    for value in (b'', b'MainPID=0\nMainPID=0\n', b'\xff'):
        with pytest.raises((backup.Refused, UnicodeError)):
            backup.inactive(value)


@pytest.mark.parametrize('command', [b'python bot.py', b'/venv/bin/python3 app',
                                    b'funpayflow', b'/root/SecureBot/run',
                                    b'pypy main.py', b'FunPay-bot'])
def test_process_aliases_and_unidentified_python_refuse(command):
    with pytest.raises(backup.Refused):
        backup.process_safe(command)


def test_single_rw_ext4_root_mount():
    good = b'10 1 8:2 / / rw,relatime - ext4 /dev/fake rw,commit=30\n'
    backup.frozen_mount(good, '8:2')
    bad = [good.replace(b'fake rw', b'fake ro'),
           good.replace(b'/ rw', b'/ ro'),
           good.replace(b'/ / rw', b'/subdir / rw'),
           good.replace(b'ext4', b'nfs'),
           good.replace(b'ext4', b'xfs'),
           good + b'11 1 8:2 / /alias rw - ext4 /dev/fake rw\n',
           good + b'11 10 8:3 / /root/FunPayFlow-data/nested ro - ext4 /dev/fake ro\n',
           good + b'11 10 8:3 / /root/backups rw - ext4 /dev/other rw\n',
           good + b'11 10 8:3 / /root rw - ext4 /dev/other rw\n',
           b'', b'invalid']
    for value in bad:
        with pytest.raises(backup.Refused):
            backup.frozen_mount(value, '8:2')


@pytest.mark.parametrize('kind,mode,uid,links', [
    (stat.S_IFREG, 0o622, 0, 1), (stat.S_IFREG, 0o600, 1000, 1),
    (stat.S_IFREG, 0o600, 0, 2), (stat.S_IFLNK, 0o600, 0, 1),
    (stat.S_IFDIR, 0o777, 0, 2), (stat.S_IFDIR, 0o2700, 0, 2)])
def test_trust_permission_policy(kind, mode, uid, links):
    s = SimpleNamespace(st_mode=kind | mode, st_uid=uid, st_nlink=links)
    with pytest.raises(backup.Refused):
        backup.secure_stat(s, kind == stat.S_IFDIR, private=False)


@pytest.mark.parametrize('fault', [
    None, 'missing', 'untrusted_coordinator', 'untrusted_helper', 'parent', 'dead',
    'namespace', 'exe', 'command', 'uid', 'gid', 'public', 'hardlink', 'symlink',
    'fd_inode', 'lock_absent', 'lock_shared', 'lock_other_pid', 'lock_wrong_inode',
    'unlocked', 'lock_replaced', 'changed', 'expired', 'long_lease', 'wrong_boot',
    'wrong_pid', 'wrong_start', 'wrong_parent_start', 'wrong_digest', 'extra',
    'duplicate', 'services_uninhibited', 'writers_not_quiescent', 'no_recovery',
    'boolean_version', 'numeric_inhibition',
])
def test_live_coordinator_authorization_protocol(monkeypatch, fault):
    # All /proc, descriptors, ownership, flock and time here are synthetic.
    # No parent inspection, real process scan or real maintenance file is used.
    base_os = backup.os
    lock_stat = SimpleNamespace(st_dev=7, st_ino=42, st_uid=0, st_nlink=1,
                               st_mode=stat.S_IFREG | 0o600, st_size=200,
                               st_mtime_ns=1, st_ctime_ns=1)
    lstat_calls = 0

    class FakePath(PurePosixPath):
        def resolve(self):
            return self

        def lstat(self):
            nonlocal lstat_calls
            values = dict(vars(lock_stat))
            if str(self) == '/run/funpayflow-maintenance':
                values['st_mode'] = stat.S_IFDIR | 0o700
            if self.name == 'backup.json':
                lstat_calls += 1
                if fault == 'missing':
                    raise FileNotFoundError
                if fault == 'public':
                    values['st_mode'] |= 0o004
                if fault == 'hardlink':
                    values['st_nlink'] = 2
                if fault == 'symlink':
                    values['st_mode'] = stat.S_IFLNK | 0o600
                if fault == 'changed' and lstat_calls >= 3:
                    values['st_mtime_ns'] += 1
            return SimpleNamespace(**values)

    paths = ('DATA', 'BACKUPS', 'INSTALLED', 'RECEIPT', 'COORDINATOR',
             'COORDINATOR_RECEIPT', 'MAINTENANCE', 'AUTHORIZATION', 'MAINTENANCE_LOCK')
    for name in paths:
        monkeypatch.setattr(backup, name, FakePath(getattr(backup, name).as_posix()))
    monkeypatch.setattr(backup, 'Path', FakePath)
    monkeypatch.setattr(backup, 'protected', lambda *a, **kw: None)

    def pin(script, receipt):
        if (fault == 'untrusted_coordinator' and script == backup.COORDINATOR
                or fault == 'untrusted_helper' and script == backup.INSTALLED):
            raise backup.Refused()
        return 'a' * 64 if script == backup.INSTALLED else 'b' * 64

    monkeypatch.setattr(backup, 'pinned_digest', pin)
    monkeypatch.setattr(backup, 'sys', SimpleNamespace(executable='/usr/bin/python3.13'))
    now = 10**10
    boot = '11111111-1111-1111-1111-111111111111'
    evidence = dict(version=1, helper_pid=101, helper_start=300,
                    coordinator_pid=100, coordinator_start=200,
                    helper_sha256='a' * 64, coordinator_sha256='b' * 64,
                    boot_id=boot, expires_boottime_ns=now + backup.SECONDS * 10**9,
                    units=list(backup.UNITS), data=str(backup.DATA), backups=str(backup.BACKUPS),
                    services_inhibited=True, writers_quiescent=True, recovery_armed=True)
    changes = {
        'expired': ('expires_boottime_ns', now),
        'long_lease': ('expires_boottime_ns', now + 1000 * 10**9),
        'wrong_boot': ('boot_id', 'old-boot'), 'wrong_pid': ('helper_pid', 102),
        'wrong_start': ('helper_start', 301),
        'wrong_parent_start': ('coordinator_start', 201),
        'wrong_digest': ('helper_sha256', 'c' * 64), 'extra': ('extra', True),
        'services_uninhibited': ('services_inhibited', False),
        'writers_not_quiescent': ('writers_quiescent', False),
        'no_recovery': ('recovery_armed', False),
        'boolean_version': ('version', True),
        'numeric_inhibition': ('services_inhibited', 1),
    }
    if fault in changes:
        key, value = changes[fault]
        evidence[key] = value
    raw = json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()
    if fault == 'duplicate':
        raw = b'{"version":0,' + raw[1:]

    def proc_stat(pid, parent, start):
        return str(pid).encode() + b' (synthetic name) ' + b' '.join(
            [b'Z' if fault == 'dead' else b'S', str(parent).encode()]
            + [b'0'] * 17 + [str(start).encode()])

    lock = b'lock:\t1: FLOCK ADVISORY WRITE 100 00:07:42 0 EOF\n'
    for kind, old, new in [('lock_shared', b'WRITE', b'READ'),
                           ('lock_other_pid', b'100', b'999'),
                           ('lock_wrong_inode', b':42', b':43')]:
        if fault == kind:
            lock = lock.replace(old, new)

    def read(path, maximum):
        value = str(path)
        if value == '/proc/101/stat':
            return proc_stat(101, 99 if fault == 'parent' else 100, 300)
        if value == '/proc/100/stat':
            return proc_stat(100, 1, 200)
        if value == '/proc/100/cmdline':
            return (b'forbidden' if fault == 'command' else
                    b'/usr/bin/python3.13\0-I\0-S\0' + bytes(backup.COORDINATOR)
                    + b'\0maintain-backup\0')
        if value == '/proc/100/status':
            return (b'Uid:\t0\t0\t0\t1000\n' if fault == 'uid' else b'Uid:\t0\t0\t0\t0\n') + (
                b'Gid:\t0\t0\t0\t1000\n' if fault == 'gid' else b'Gid:\t0\t0\t0\t0\n')
        if value == '/proc/self/fdinfo/3':
            return b'' if fault == 'lock_absent' else lock
        if value == str(backup.AUTHORIZATION):
            return raw
        if value == '/proc/sys/kernel/random/boot_id':
            return boot.encode()
        raise AssertionError('unexpected synthetic read')

    def readlink(path):
        if str(path).endswith('/exe'):
            return '/untrusted/python' if fault == 'exe' else '/usr/bin/python3.13'
        return 'other' if fault == 'namespace' and '/100/' in str(path) else 'same'

    def fstat(fd):
        if fault == 'fd_inode' and fd == 3 or fault == 'lock_replaced' and fd == 4:
            return SimpleNamespace(**(vars(lock_stat) | {'st_ino': 43}))
        return lock_stat

    def flock(fd, flags):
        assert fd == 4 and flags == 6
        if fault != 'unlocked':
            raise BlockingIOError

    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4))
    monkeypatch.setattr(backup, 'read_bounded', read)
    monkeypatch.setattr(backup, 'time', SimpleNamespace(clock_gettime_ns=lambda clock: now,
                                                      CLOCK_BOOTTIME=7))
    monkeypatch.setattr(backup, 'os', SimpleNamespace(
        name=base_os.name, getpid=lambda: 101, getppid=lambda: 100, fstat=fstat,
        readlink=readlink, fsdecode=os.fsdecode, major=lambda dev: 0, minor=lambda dev: dev,
        open=lambda *a: 4, close=lambda fd: None, O_RDONLY=0, O_NOFOLLOW=1, O_NONBLOCK=2))
    if fault is None:
        result = backup.maintenance_evidence()
        assert result[:2] == (100, raw)
    else:
        with pytest.raises(backup.Refused) as rejected:
            backup.maintenance_evidence()
        assert rejected.value.code == 'maintenance_required'


def test_no_import_side_effects_or_network_bot_gui():
    tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(n.name.split('.')[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module.split('.')[0])
    assert imports <= {'__future__', 'contextlib', 'ctypes', 'hashlib', 'json',
                       'os', 'pathlib', 're', 'selectors', 'signal', 'sqlite3',
                       'stat', 'struct', 'subprocess', 'sys', 'tarfile', 'time', 'uuid', 'fcntl'}
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in {'extract', 'extractall', 'rmtree', 'system',
                                       'restart', 'reload', 'start', 'stop'}
                   for n in ast.walk(tree))
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    evidence = ast.parse(SCRIPT.read_text(encoding='utf-8'))
    # Only the destination backup lock may be created by the helper. The
    # coordinator protocol has no write, mkdir, token generation or command run.
    function = next(n for n in evidence.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'maintenance_evidence')
    assert not any(isinstance(n, ast.Attribute) and n.attr in
                   {'write', 'write_bytes', 'mkdir', 'O_CREAT', 'Popen', 'run', 'uuid4'}
                   for n in ast.walk(function))
