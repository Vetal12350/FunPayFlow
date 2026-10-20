"""Opt-in local archive audit. No runtime imports or production mutations.

Only the SQLite member is materialized, in a private disposable directory.
See docs/VPS_BACKUP_AUDIT.md for deliberately conservative format support.
"""
from __future__ import annotations

from contextlib import closing
import gzip
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import sys
import tempfile
import time
import zipfile

BACKUPS = Path('/root/backups')
DATA = Path('/root/FunPayFlow-data')
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_MEMBER = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_ENTRIES = 2048
MAX_CANDIDATES = 128
CHUNK = 64 * 1024
REQUIRED = ('.env', 'bot_settings.json', 'state.sqlite3')
PREFIXES = ('', 'FunPayFlow-data/', 'root/FunPayFlow-data/')


class Rejected(Exception):
    """No archive-controlled text is attached to errors."""


def _require(value):
    if not value:
        raise Rejected


def _identity(s):
    # Windows stat/fstat disagree on legacy ctime; POSIX ctime detects changes.
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
            s.st_ctime_ns if os.name == 'posix' else 0)


def _private_directory(path):
    # Fail closed for linked ancestors as well as the final component.
    for part in (path, *path.parents):
        s = part.lstat()
        _require(stat.S_ISDIR(s.st_mode))
        if os.name == 'posix':
            _require(s.st_uid in (0, os.geteuid()))
            if part == path:
                _require(s.st_uid == os.geteuid() and not s.st_mode & 0o077)
            else:
                _require(not s.st_mode & 0o022 or s.st_mode & stat.S_ISVTX)


def _open_regular(path):
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
    if os.name == 'posix':
        _require(before.st_uid == os.geteuid() and not before.st_mode & 0o077)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
    stream = os.fdopen(fd, 'rb')
    try:
        _require(_identity(before) == _identity(os.fstat(stream.fileno())))
        return stream, before
    except Exception:
        stream.close()
        raise


def _name(name, directory=False):
    _require(len(name) <= 512 and '\\' not in name and ':' not in name)
    _require(not any(ord(c) < 32 or ord(c) == 127 for c in name))
    if directory and name.endswith('/'):
        name = name[:-1]
    _require(bool(name) and all(p not in ('', '.', '..') for p in name.split('/')))
    return name


def _known(name):
    for prefix in PREFIXES:
        for leaf in REQUIRED:
            if name == prefix + leaf:
                return leaf
    return None


def _sqlite(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1',
                                 uri=True, timeout=0)) as db:
        db.enable_load_extension(False)
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA cache_size=-2048')
        db.execute('PRAGMA mmap_size=0')
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_MEMBER)
        db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)
        budget = 20000

        def progress():
            nonlocal budget
            budget -= 1
            return int(budget <= 0)

        db.set_progress_handler(progress, 1000)
        for pragma in ('quick_check', 'integrity_check'):
            cursor = db.execute('PRAGMA ' + pragma + '(1)')
            _require(cursor.fetchone() == ('ok',) and cursor.fetchone() is None)


class Members:
    def __init__(self, temp_root):
        self.temp_root = temp_root
        self.names = set()
        self.found = set()
        self.total = 0
        self.sqlite_ok = False

    def consume(self, name, size, directory, stream):
        name = _name(name, directory)
        _require(name not in self.names and len(self.names) < MAX_ENTRIES)
        self.names.add(name)
        _require(0 <= size <= MAX_MEMBER and (not directory or size == 0))
        self.total += size
        _require(self.total <= MAX_EXPANDED)
        leaf = None if directory else _known(name)
        if leaf:
            _require(leaf not in self.found)
            self.found.add(leaf)
        # Reject embedded archives by suffix and content signature; never recurse.
        _require(directory or not name.lower().endswith(
            ('.gz', '.zip', '.zst', '.tar', '.bak', '.xz', '.bz2', '.7z', '.rar')))
        if leaf == 'state.sqlite3':
            with tempfile.TemporaryDirectory(prefix='fpf-audit-', dir=self.temp_root) as tmp:
                os.chmod(tmp, 0o700)
                target = Path(tmp) / 'database'
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    self._copy(stream, size, output)
                _sqlite(target.resolve())
                self.sqlite_ok = True
        else:
            self._copy(stream, size, None)

    @staticmethod
    def _copy(stream, size, output):
        remaining = size
        first = True
        while remaining:
            block = stream.read(min(CHUNK, remaining))
            _require(bool(block))
            if first:
                _require(not block.startswith((b'PK\x03\x04', b'\x1f\x8b', b'\x28\xb5\x2f\xfd',
                                               b'7z\xbc\xaf\x27\x1c', b'Rar!', b'BZh', b'\xfd7zXZ')))
                _require(block[257:262] != b'ustar')
                first = False
            remaining -= len(block)
            if output:
                output.write(block)


def _octal(raw):
    value = raw.strip(b'\0 ')
    _require(bool(value) and all(c in b'01234567' for c in value))
    return int(value, 8)


def _tar(stream, members):
    # A strict ustar subset avoids PAX/GNU metadata allocation and path rewriting.
    with gzip.GzipFile(fileobj=stream, mode='rb') as gz:
        expanded = 0

        def read(n):
            nonlocal expanded
            expanded += n
            _require(expanded <= MAX_EXPANDED)
            result = gz.read(n)
            _require(len(result) == n)
            return result

        class Payload:
            def read(self, n):
                return read(n)

        while True:
            header = read(512)
            if header == bytes(512):
                _require(read(512) == bytes(512))
                # Consume the compression trailer (CRC/length) and all padding.
                while True:
                    block = gz.read(CHUNK)
                    if not block:
                        return
                    expanded += len(block)
                    _require(expanded <= MAX_EXPANDED and not any(block))
            _require(_octal(header[148:156]) == sum(header[:148]) + 256 + sum(header[156:]))
            _require(header[257:263] in (b'ustar\0', b'ustar '))
            _require(header[156:157] in (b'0', b'\0', b'5'))
            _require(not header[157:257].strip(b'\0'))
            name = header[:100].split(b'\0', 1)[0]
            prefix = header[345:500].split(b'\0', 1)[0]
            if prefix:
                name = prefix + b'/' + name
            size = _octal(header[124:136])
            members.consume(name.decode('utf-8', 'strict'), size,
                            header[156:157] == b'5', Payload())
            _require(not any(read((-size) % 512)))


def _zip(stream, members):
    # Bound central-directory allocation BEFORE constructing ZipFile. ZIP64,
    # multi-disk archives, comments and trailing data are outside this subset.
    stream.seek(-22, 2)
    end = stream.read(22)
    sig, disk, cd_disk, count_disk, count, length, offset, comment = struct.unpack('<4s4H2IH', end)
    _require(sig == b'PK\x05\x06' and disk == cd_disk == comment == 0)
    _require(count == count_disk and count <= MAX_ENTRIES)
    _require(length <= MAX_ENTRIES * 1024 and offset + length == stream.tell() - 22)
    stream.seek(0)
    with zipfile.ZipFile(stream) as archive:
        _require(len(archive.infolist()) == count)
        for info in archive.infolist():
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            directory = info.is_dir()
            _require(kind in (0, stat.S_IFDIR if directory else stat.S_IFREG))
            _require(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED))
            _require(not info.flag_bits & 1 and not info.extra and not info.comment)
            _require(info.orig_filename == info.filename)
            _require(info.file_size <= MAX_MEMBER)
            _require(info.file_size <= max(1, info.compress_size) * 200)
            with archive.open(info) as payload:
                members.consume(info.filename, info.file_size, directory, payload)
                _require(payload.read(1) == b'')  # includes CRC verification


def _candidate(path, temp_root, now):
    result = dict(verified=False, unsupported=False, rejected=False,
                  env_present=False, settings_present=False, sqlite_present=False,
                  sqlite_checks_ok=False, size_band=0, age_band=0)
    try:
        with closing(_open_regular(path)[0]) as stream:
            initial = os.fstat(stream.fileno())
            result['size_band'] = 1 if initial.st_size < 1024 * 1024 else 2 if initial.st_size < 32 * 1024 * 1024 else 3
            age = max(0, now - initial.st_mtime)
            result['age_band'] = 1 if age < 86400 else 2 if age < 7 * 86400 else 3 if age < 30 * 86400 else 4
            _require(0 < initial.st_size <= MAX_ARCHIVE)
            members = Members(temp_root)
            if path.name.endswith('.tar.gz'):
                _tar(stream, members)
            elif path.name.endswith('.zip'):
                _zip(stream, members)
            else:
                result['unsupported'] = True
                return result
            _require(_identity(initial) == _identity(os.fstat(stream.fileno())))
            _require(_identity(initial) == _identity(path.lstat()))
            result.update(env_present='.env' in members.found,
                          settings_present='bot_settings.json' in members.found,
                          sqlite_present='state.sqlite3' in members.found,
                          sqlite_checks_ok=members.sqlite_ok)
            result['verified'] = len(members.found) == 3 and members.sqlite_ok
    except Exception:
        result['rejected'] = True
    return result


def audit(*, backups=BACKUPS, data=DATA, temp_root=None, now=None):
    """Trusted Python API for synthetic tests; CLI never accepts paths.

    Data is a containment boundary only: no live data file is opened.
    """
    report = dict(audit_complete=False, restore_verified=False, candidates=0,
                  unknown_entries=0, results=[])
    try:
        backups, data = Path(backups).absolute(), Path(data).absolute()
        _private_directory(backups)
        # gettempdir() probes by writing files; keep the CLI default read-only
        # until an actual known SQLite member needs its private temporary copy.
        temp = Path('/tmp' if temp_root is None else temp_root).resolve()
        _require(not temp.is_relative_to(data.resolve()))
        _require(not temp.is_relative_to(backups.resolve()))
        # No listdir or sorted() of an unbounded attacker-controlled directory.
        with os.scandir(backups) as entries:
            for index, entry in enumerate(entries):
                _require(index < MAX_CANDIDATES)
                if not entry.name.endswith(('.tar.gz', '.tar.zst', '.zip', '.bak')):
                    report['unknown_entries'] += 1
                    continue
                report['candidates'] += 1
                report['results'].append(_candidate(Path(entry.path), temp, time.time() if now is None else now))
        report['audit_complete'] = True
    except Exception:
        pass
    return report


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args != ['audit']:
        print(json.dumps({'invalid_arguments': True}))
        return 2
    result = audit()
    print(json.dumps(result, sort_keys=True))
    return 0 if (result['audit_complete'] and result['unknown_entries'] == 0
                 and result['candidates'] > 0
                 and all(r['verified'] for r in result['results'])) else 1


if __name__ == '__main__':
    raise SystemExit(main())
