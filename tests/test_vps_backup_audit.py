"""Synthetic fixtures only; run with --basetemp=tests/.offline-tmp/backup-audit."""
import importlib.util
import ast
import gzip
from contextlib import closing, nullcontext
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tarfile
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'vps_backup_audit.py'
spec = importlib.util.spec_from_file_location('backup_audit', SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.fixture
def roots(tmp_path):
    backups, data, scratch = (tmp_path / n for n in ('backups', 'data', 'scratch'))
    for p in (backups, data, scratch):
        p.mkdir(mode=0o700)
    return backups, data, scratch


def inspect(roots):
    backups, data, scratch = roots
    report = audit.audit(backups=backups, data=data, temp_root=scratch)
    assert list(scratch.iterdir()) == []
    return report


def payloads(roots):
    path = roots[2] / 'synthetic.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE fake (value TEXT)')
        db.execute("INSERT INTO fake VALUES ('synthetic only')")
        db.commit()
    value = path.read_bytes()
    path.unlink()
    return [('.env', b'FAKE_ONLY=never-a-real-secret'),
            ('bot_settings.json', b'{"fake": true}'), ('state.sqlite3', value)]


def archive(roots, entries, fmt='tar', name=None):
    path = roots[0] / (name or ('synthetic.tar.gz' if fmt == 'tar' else 'synthetic.zip'))
    if fmt == 'tar':
        with tarfile.open(path, 'w:gz', format=tarfile.USTAR_FORMAT) as out:
            for name, value in entries:
                if isinstance(name, tarfile.TarInfo):
                    info = name
                else:
                    info = tarfile.TarInfo(name)
                    info.size = len(value)
                out.addfile(info, io.BytesIO(value))
    else:
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as out:
            for name, value in entries:
                out.writestr(name, value)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
@pytest.mark.parametrize('prefix', audit.PREFIXES)
def test_complete_candidate_is_not_restore_proof(roots, fmt, prefix):
    archive(roots, [(prefix + n, v) for n, v in payloads(roots)], fmt)
    result = inspect(roots)
    assert result['audit_complete'] and not result['restore_verified']
    assert result['results'][0]['verified']


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
def test_required_members_from_mixed_roots_are_rejected(roots, fmt):
    payload = dict(payloads(roots))
    mixed = [
        ('.env', payload['.env']),
        ('FunPayFlow-data/bot_settings.json', payload['bot_settings.json']),
        ('root/FunPayFlow-data/state.sqlite3', payload['state.sqlite3']),
    ]
    archive(roots, mixed, fmt)
    result = inspect(roots)['results'][0]
    assert result['rejected'] and not result['verified']


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
def test_optional_members_do_not_influence_required_root(roots, fmt):
    payload = [('unrelated/info.txt', b'synthetic'), *[
        ('FunPayFlow-data/' + name, value) for name, value in payloads(roots)
    ]]
    archive(roots, payload, fmt)
    assert inspect(roots)['results'][0]['verified']


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
@pytest.mark.parametrize('missing', audit.REQUIRED)
def test_missing_components(roots, fmt, missing):
    archive(roots, [(n, v) for n, v in payloads(roots) if n != missing], fmt)
    result = inspect(roots)['results'][0]
    assert not result['verified'] and not result['rejected']


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
@pytest.mark.parametrize('name', ['/private', '../private', 'x/../../private',
                                  'x\\private', 'C:/private', './private',
                                  'x//private', 'x\nprivate'])
def test_member_paths(roots, fmt, name):
    path = archive(roots, [(name, b'fake')], fmt)
    if fmt == 'zip' and '\\' in name:
        # ZipInfo normalizes native separators on Windows while writing.
        path.write_bytes(path.read_bytes().replace(name.replace('\\', '/').encode(),
                                                  name.encode()))
    assert inspect(roots)['results'][0]['rejected']


@pytest.mark.parametrize('kind', [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                                  tarfile.FIFOTYPE, tarfile.XHDTYPE])
def test_tar_links_and_special_metadata(roots, kind):
    info = tarfile.TarInfo('fake')
    info.type = kind
    info.linkname = 'private'
    archive(roots, [(info, b'')])
    assert inspect(roots)['results'][0]['rejected']


@pytest.mark.parametrize('kind', [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR])
def test_zip_special_members(roots, kind):
    info = zipfile.ZipInfo('fake')
    info.create_system = 3
    info.external_attr = (kind | 0o600) << 16
    archive(roots, [(info, b'fake')], 'zip')
    assert inspect(roots)['results'][0]['rejected']


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
def test_duplicates(roots, fmt):
    with pytest.warns(UserWarning) if fmt == 'zip' else nullcontext():
        archive(roots, [('fake', b'a'), ('fake', b'b')], fmt)
    assert inspect(roots)['results'][0]['rejected']


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
def test_duplicate_required_across_prefixes(roots, fmt):
    archive(roots, [('.env', b'a'), ('root/FunPayFlow-data/.env', b'b')], fmt)
    assert inspect(roots)['results'][0]['rejected']


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
def test_truncation(roots, fmt):
    path = archive(roots, payloads(roots), fmt)
    path.write_bytes(path.read_bytes()[:-10])
    assert inspect(roots)['results'][0]['rejected']


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
def test_invalid_database_and_redaction(roots, fmt, capsys):
    archive(roots, [('state.sqlite3', b'PRIVATE-CONTENT-DO-NOT-PRINT')], fmt,
            name='PRIVATE-FILENAME.' + ('tar.gz' if fmt == 'tar' else 'zip'))
    result = inspect(roots)
    assert result['results'][0]['rejected']
    text = json.dumps(result)
    assert 'PRIVATE' not in text and str(roots[0]) not in text
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
@pytest.mark.parametrize('name,value', [('inner.zip', b'x'), ('disguised', b'PK\x03\x04x'),
                                       ('disguised', b'\x1f\x8bx')])
def test_nested_archives(roots, fmt, name, value):
    archive(roots, [(name, value)], fmt)
    assert inspect(roots)['results'][0]['rejected']


@pytest.mark.parametrize('limit', ['MAX_MEMBER', 'MAX_EXPANDED', 'MAX_ARCHIVE', 'MAX_ENTRIES'])
@pytest.mark.parametrize('fmt', ['tar', 'zip'])
def test_resource_caps(roots, monkeypatch, limit, fmt):
    archive(roots, [('one', b'abcdef'), ('two', b'abcdef')], fmt)
    monkeypatch.setattr(audit, limit, 1)
    assert inspect(roots)['results'][0]['rejected']


def test_zip_bomb_ratio(roots):
    archive(roots, [('bomb', b'0' * 1000000)], 'zip')
    assert inspect(roots)['results'][0]['rejected']


@pytest.mark.parametrize('suffix', ['tar.zst', 'bak'])
def test_unavailable_formats_not_guessed(roots, suffix):
    archive(roots, payloads(roots), name='fake.' + suffix)
    result = inspect(roots)['results'][0]
    assert result['unsupported'] and not result['verified']


def test_unknown_and_directory_bound(roots, monkeypatch):
    (roots[0] / 'unknown').write_bytes(b'fake')
    assert inspect(roots)['unknown_entries'] == 1
    monkeypatch.setattr(audit, 'MAX_CANDIDATES', 0)
    assert not inspect(roots)['audit_complete']


def test_no_live_reads_and_no_temp_inside_data(roots, monkeypatch):
    archive(roots, payloads(roots))
    (roots[1] / 'state.sqlite3').write_bytes(b'never open live database')
    original = sqlite3.connect

    def guarded(path, *args, **kwargs):
        assert str(roots[1]) not in str(path)
        assert 'mode=ro&immutable=1' in path
        return original(path, *args, **kwargs)

    monkeypatch.setattr(audit.sqlite3, 'connect', guarded)
    assert inspect(roots)['results'][0]['verified']
    assert not audit.audit(backups=roots[0], data=roots[1], temp_root=roots[1])['audit_complete']


def test_cleanup_after_sqlite_failure(roots, monkeypatch):
    archive(roots, payloads(roots))

    def fail(path):
        assert path.stat().st_mode & 0o077 == 0 if os.name == 'posix' else True
        raise RuntimeError('PRIVATE exception')

    monkeypatch.setattr(audit, '_sqlite', fail)
    assert inspect(roots)['results'][0]['rejected']


def test_changed_archive_rejected(roots, monkeypatch):
    path = archive(roots, payloads(roots))
    original = audit._tar

    def changed(stream, members):
        original(stream, members)
        path.touch()
        s = path.stat()
        os.utime(path, ns=(s.st_atime_ns, s.st_mtime_ns + 1000000000))

    monkeypatch.setattr(audit, '_tar', changed)
    assert inspect(roots)['results'][0]['rejected']


def test_cli_rejects_paths_and_safe_import(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected filesystem access')

    monkeypatch.setattr(audit, 'audit', forbidden)
    assert audit.main(['audit', '--data', 'private']) == 2
    assert json.loads(capsys.readouterr().out) == {'invalid_arguments': True}
    monkeypatch.setattr(os, 'scandir', forbidden)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_cli_output_and_failure_exit(monkeypatch, capsys):
    monkeypatch.setattr(audit, 'audit', lambda: {'audit_complete': False})
    assert audit.main(['audit']) == 1
    assert json.loads(capsys.readouterr().out) == {'audit_complete': False}


def test_permission_failure_and_archive_links(roots):
    path = archive(roots, payloads(roots))
    if os.name == 'posix':
        path.chmod(0o644)
        assert inspect(roots)['results'][0]['rejected']
        path.chmod(0o600)
    linked = roots[0] / 'linked.tar.gz'
    os.link(path, linked)
    assert all(r['rejected'] for r in inspect(roots)['results'])


def test_corrupt_gzip_crc(roots):
    path = archive(roots, payloads(roots))
    value = bytearray(path.read_bytes())
    value[-8] ^= 255
    path.write_bytes(value)
    assert inspect(roots)['results'][0]['rejected']


def test_corrupt_tar_checksum(roots):
    path = archive(roots, payloads(roots))
    raw = bytearray(gzip.decompress(path.read_bytes()))
    raw[0] ^= 1
    path.write_bytes(gzip.compress(raw))
    assert inspect(roots)['results'][0]['rejected']


def test_zip_checksum_and_malformed_compression(roots):
    path = roots[0] / 'fake.zip'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as out:
        out.writestr('fake', b'SYNTHETIC-CRC-MARKER')
    path.chmod(0o600)
    path.write_bytes(path.read_bytes().replace(b'SYNTHETIC-CRC-MARKER', b'CORRUPTED-CRC-MARKER'))
    assert inspect(roots)['results'][0]['rejected']
    path = archive(roots, [('fake', b'fake')])
    path.write_bytes(b'not a gzip stream')
    assert all(r['rejected'] for r in inspect(roots)['results'])


@pytest.mark.parametrize('fmt', ['tar', 'zip'])
def test_normal_directories(roots, fmt):
    if fmt == 'tar':
        info = tarfile.TarInfo('FunPayFlow-data/')
        info.type = tarfile.DIRTYPE
    else:
        info = zipfile.ZipInfo('FunPayFlow-data/')
    archive(roots, [(info, b'')] + [('FunPayFlow-data/' + n, v) for n, v in payloads(roots)], fmt)
    assert inspect(roots)['results'][0]['verified']


def test_no_network_process_or_bot_imports():
    tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(n.name.split('.')[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module.split('.')[0])
    assert modules <= {'__future__', 'contextlib', 'gzip', 'json', 'os', 'pathlib',
                       'sqlite3', 'stat', 'struct', 'sys', 'tempfile', 'time', 'zipfile'}
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in {'extract', 'extractall', 'system', 'popen', 'backup'}
                   for n in ast.walk(tree))


def test_sqlite_vm_budget_and_query_only(roots, monkeypatch):
    archive(roots, payloads(roots))
    statements = []

    class FakeConnection:
        def enable_load_extension(self, value):
            assert value is False

        def execute(self, sql):
            statements.append(sql)
            if sql.startswith('PRAGMA quick_check'):
                assert sum(self.progress() for _ in range(20000)) == 1
                raise sqlite3.OperationalError('synthetic interrupted')

        def setlimit(self, *args):
            pass

        def set_progress_handler(self, callback, steps):
            assert steps == 1000
            self.progress = callback

        def close(self):
            statements.append('closed')

    monkeypatch.setattr(audit.sqlite3, 'connect', lambda *a, **kw: FakeConnection())
    assert inspect(roots)['results'][0]['rejected']
    assert statements == ['PRAGMA query_only=ON', 'PRAGMA trusted_schema=OFF',
                          'PRAGMA cache_size=-2048', 'PRAGMA mmap_size=0',
                          'PRAGMA quick_check(1)', 'closed']


@pytest.mark.parametrize('target', ['file', 'directory'])
def test_symlinks_rejected_before_open(roots, monkeypatch, target):
    from types import SimpleNamespace

    def linked(path):
        return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_nlink=1)

    monkeypatch.setattr(Path, 'lstat', linked)
    with pytest.raises(audit.Rejected):
        if target == 'file':
            audit._open_regular(roots[0] / 'fake.tar.gz')
        else:
            audit._private_directory(roots[0])


def test_sqlite_requires_both_checks(roots, monkeypatch):
    archive(roots, payloads(roots))
    checked = []

    class Cursor:
        def __init__(self, value):
            self.rows = iter([value, None])

        def fetchone(self):
            return next(self.rows)

    class Connection:
        def enable_load_extension(self, value):
            pass

        def setlimit(self, *args):
            pass

        def set_progress_handler(self, *args):
            pass

        def execute(self, sql):
            checked.append(sql)
            return Cursor(('ok',) if 'quick_check' in sql else ('fake invalid',))

        def close(self):
            checked.append('closed')

    monkeypatch.setattr(audit.sqlite3, 'connect', lambda *a, **kw: Connection())
    assert inspect(roots)['results'][0]['rejected']
    assert checked[-3:] == ['PRAGMA quick_check(1)', 'PRAGMA integrity_check(1)', 'closed']


def test_future_plan_has_required_gates():
    text = (SCRIPT.parents[1] / 'docs' / 'VPS_BACKUP_AUDIT.md').read_text(encoding='utf-8')
    for marker in ('sqlite3.Connection.backup', 'WAL', 'bounded service stop',
                   'cross-file', 'fsync', 'retention', 'UNKNOWN', 'isolated recovery'):
        assert marker in text
