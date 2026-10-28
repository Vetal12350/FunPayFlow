"""Synthetic archive/descriptor/UI tests: no live SSH, credentials or downloads."""
import hashlib
import importlib.util
import io
import json
from contextlib import nullcontext
from pathlib import Path
import stat
import tarfile
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'vps_update_prepare.py'
spec = importlib.util.spec_from_file_location('prepare_tests', SCRIPT)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
FILES = {'pyproject.toml': b'public metadata', 'uv.lock': b'pinned wheels',
         'src/funpayflow/main.py': b'raise RuntimeError("must never execute")'}
DIRS = ['src', 'src/funpayflow']


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(p.urllib.request, 'build_opener', lambda *a: pytest.fail('Unexpected network'))


def archive(extra=(), files=None):
    buffer = io.BytesIO()
    prefix = 'FunPayFlow-' + p.TARGET
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for name in [prefix, *(prefix + '/' + d for d in DIRS)]:
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            tar.addfile(member)
        for name, value in (FILES if files is None else files).items():
            member = tarfile.TarInfo(prefix + '/' + name)
            member.size = len(value)
            tar.addfile(member, io.BytesIO(value))
        for member in extra:
            tar.addfile(member, io.BytesIO(b'x' * member.size))
    return buffer.getvalue()


def test_exact_archive_manifest_without_extract_or_import(monkeypatch):
    monkeypatch.setattr(tarfile.TarFile, 'extractall', lambda *a, **k: pytest.fail('Extraction'))
    entries = {name: ['file', len(value), hashlib.sha256(value).hexdigest()]
               for name, value in FILES.items()}
    entries.update({name: ['dir'] for name in DIRS})
    expected = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert p.archive_digest(archive()) == expected
    assert p.archive_digest(archive(files={**FILES, 'uv.lock': b'different'})) != expected


@pytest.mark.parametrize('name,kind', [
    ('../escape', tarfile.REGTYPE), ('src/../escape', tarfile.REGTYPE),
    ('/absolute', tarfile.REGTYPE), ('src//escape', tarfile.REGTYPE),
    ('src/link', tarfile.SYMTYPE), ('src/hardlink', tarfile.LNKTYPE),
    ('src/pipe', tarfile.FIFOTYPE), ('src/device', tarfile.CHRTYPE),
    ('uv.lock', tarfile.REGTYPE), ('.env', tarfile.REGTYPE),
    ('.venv/config', tarfile.REGTYPE), ('private/token', tarfile.REGTYPE),
    ('missing-parent/file', tarfile.REGTYPE),
])
def test_archive_paths_links_duplicates_and_private_entries_refused(name, kind):
    member = tarfile.TarInfo('FunPayFlow-' + p.TARGET + '/' + name)
    member.type = kind
    member.linkname = '/outside'
    with pytest.raises(ValueError):
        p.archive_digest(archive([member]))


@pytest.mark.parametrize('data', [b'', b'corrupt', archive()[:-8]])
def test_corrupt_archive(data):
    with pytest.raises((ValueError, OSError, EOFError, tarfile.TarError)):
        p.archive_digest(data)


def test_wrong_target_missing_source_and_expansion_limits(monkeypatch):
    raw = archive()
    monkeypatch.setattr(p, 'TARGET', 'b'*40)
    with pytest.raises(ValueError):
        p.archive_digest(raw)
    monkeypatch.undo()
    with pytest.raises(ValueError):
        p.archive_digest(archive(files={'pyproject.toml': b'x'}))
    monkeypatch.setattr(p, 'MAX_SOURCE', 100)
    with pytest.raises(ValueError):
        p.archive_digest(raw)


def test_fixed_bounded_get_without_redirect_or_cache(monkeypatch):
    raw = archive()
    class Response(io.BytesIO):
        status = 200
        def geturl(self): return p.ARCHIVE_URL
    calls = []
    def open_url(url, **kwargs):
        calls.append((url, kwargs))
        return Response(raw)
    monkeypatch.setattr(p.urllib.request, 'build_opener', lambda handler: SimpleNamespace(open=open_url))
    assert p.trusted_source_digest() == p.archive_digest(raw)
    assert calls == [(p.ARCHIVE_URL, {'timeout': 10})]
    with pytest.raises(ValueError):
        p._NoRedirect().redirect_request(None, None, None, None, None, None)
    monkeypatch.setattr(p, 'MAX_ARCHIVE', 10)
    with pytest.raises(ValueError):
        p._fetch_archive()


class FakeOS:
    """Descriptor-only fake tree; never opens the real /root on any OS."""
    O_RDONLY, O_NOFOLLOW, O_NONBLOCK, O_DIRECTORY = 0, 1, 2, 4
    def __init__(self):
        self.base = '/root/.FunPayFlow-source-0affd678'
        self.files = {self.base + '/' + name: data for name, data in FILES.items()}
        self.dirs = {'/', '/root', self.base, *(self.base + '/' + name for name in DIRS)}
        self.fds, self.positions, self.opened, self.reads = {}, {}, [], []
        self.bad, self.uid, self.race = {}, 0, False
    def getuid(self): return self.uid
    def geteuid(self): return self.uid
    def open(self, path, flags, dir_fd=None):
        assert flags & self.O_NOFOLLOW
        if dir_fd is not None:
            path = self.fds[dir_fd].rstrip('/') + '/' + path
        self.opened.append(path)
        fd = max(self.fds, default=0) + 1
        self.fds[fd], self.positions[fd] = path, 0
        return fd
    def close(self, fd):
        del self.fds[fd]
    def st(self, path):
        data = dict(st_uid=0, st_mode=stat.S_IFDIR | 0o755 if path in self.dirs else stat.S_IFREG | 0o644,
                    st_nlink=1, st_dev=1, st_ino=hash(path), st_size=len(self.files.get(path, b'')),
                    st_mtime_ns=0, st_ctime_ns=0)
        data.update(self.bad.get(path, {}))
        return SimpleNamespace(**data)
    def stat(self, path, *, dir_fd, follow_symlinks):
        assert follow_symlinks is False
        return self.st(self.fds[dir_fd].rstrip('/') + '/' + path)
    def fstat(self, fd):
        value = self.st(self.fds[fd])
        if self.race and self.positions[fd]:
            value.st_mtime_ns = 1
        return value
    def scandir(self, fd):
        prefix = self.fds[fd].rstrip('/') + '/'
        return nullcontext(iter(SimpleNamespace(name=path[len(prefix):]) for path in self.dirs | self.files.keys()
                                if path.startswith(prefix) and path != prefix and '/' not in path[len(prefix):]))
    def read(self, fd, size):
        name = self.fds[fd]
        self.reads.append(name)
        position = self.positions[fd]
        self.positions[fd] += size
        return self.files[name][position:position + size]


def collector(fake):
    ns = {}
    exec(compile(p.SOURCE_COLLECTOR, '<reviewed collector>', 'exec'), ns)
    ns['os'] = fake
    return ns['stage_digest']()


def test_remote_digest_matches_archive_and_closes_every_descriptor():
    fake = FakeOS()
    assert collector(fake) == p.archive_digest(archive())
    assert not fake.fds
    assert set(fake.reads) == set(fake.files)
    assert all(name.startswith(fake.base + '/') for name in fake.reads)


@pytest.mark.parametrize('path,change', [
    ('/root', {'st_uid': 1000}), ('/root', {'st_mode': stat.S_IFLNK | 0o777}),
    ('/root/.FunPayFlow-source-0affd678', {'st_mode': stat.S_IFDIR | 0o777}),
    ('/root/.FunPayFlow-source-0affd678/uv.lock', {'st_nlink': 2}),
    ('/root/.FunPayFlow-source-0affd678/uv.lock', {'st_mode': stat.S_IFLNK | 0o777}),
    ('/root/.FunPayFlow-source-0affd678/uv.lock', {'st_size': p.MAX_FILE + 1}),
])
def test_remote_unsafe_ownership_link_and_size_unknown(path, change):
    fake = FakeOS()
    fake.bad[path] = change
    assert collector(fake) == 'UNKNOWN'
    assert path not in fake.reads
    assert not fake.fds


def test_remote_refuses_credentials_environment_extra_files_and_races():
    fake = FakeOS()
    fake.files[fake.base + '/.env'] = b'SYNTHETIC_SECRET'
    assert collector(fake) == 'UNKNOWN'
    assert fake.base + '/.env' not in fake.reads
    fake = FakeOS()
    fake.dirs.add(fake.base + '/.venv')
    assert collector(fake) == 'UNKNOWN'
    fake = FakeOS()
    fake.files[fake.base + '/unexpected'] = b'x'
    assert collector(fake) != p.archive_digest(archive())
    fake = FakeOS()
    fake.race = True
    assert collector(fake) == 'UNKNOWN' and not fake.fds


def test_disabled_prepare_has_no_io_even_when_approved_or_reopened(monkeypatch):
    def forbidden(*a, **k): pytest.fail('Unexpected I/O')
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(p, 'trusted_source_digest', forbidden)
    monkeypatch.setattr(p.urllib.request, 'build_opener', forbidden)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    for _ in range(3):
        assert p.prepare_once()['status'] == 'NOT_PREPARED_APPROVAL_REQUIRED'
        assert p.prepare_once(approved=False)['status'] == 'NOT_PREPARED_APPROVAL_REQUIRED'
        assert p.prepare_once(approved=True) == p.capability() == {
            'status': p.ISOLATION_REQUIRED, 'target': p.TARGET, 'deploy_ready': 'NO'}
    assert p.prepare_once(approved='yes')['status'] == 'NOT_PREPARED_APPROVAL_REQUIRED'
    with pytest.raises(TypeError):
        p.prepare_once(command='unsafe')


def test_real_gui_gate_calls_backend_and_stays_disabled(monkeypatch):
    cc_spec = importlib.util.spec_from_file_location('prepare_ui_tests', SCRIPT.with_name('control_center.py'))
    cc = importlib.util.module_from_spec(cc_spec)
    cc_spec.loader.exec_module(cc)
    calls, button, label = [], [], []
    original = cc.vps.prepare.capability
    def backend():
        calls.append('capability')
        return original()
    monkeypatch.setattr(cc.vps.prepare, 'capability', backend)
    monkeypatch.setattr(cc.vps.prepare, 'prepare_once', lambda **kw: pytest.fail('Startup prepare dispatch'))
    for _ in range(2):
        cc.prepare_panel(SimpleNamespace(configure=lambda **kw: button.append(kw)),
                         SimpleNamespace(configure=lambda **kw: label.append(kw)))
    assert calls == ['capability', 'capability']
    assert button == [{'state': 'disabled'}] * 2
    assert p.TARGET in label[-1]['text'] and p.ISOLATION_REQUIRED in label[-1]['text']
    assert 'DEPLOY_READY=NO' in label[-1]['text']
