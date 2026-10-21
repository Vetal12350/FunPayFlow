#!/usr/bin/python3 -I
"""Standalone, opt-in backup primitive. See docs/VPS_BACKUP_CREATE.md.

Importing this module does nothing. Production entry requires a protected pinned
installation AND a live trusted maintenance coordinator. No service
mutation, mount operation, bot import, network access, or source write exists here.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import time
import uuid

DATA = Path('/root/FunPayFlow-data')
BACKUPS = Path('/root/backups')
INSTALLED = Path('/usr/local/libexec/funpayflow-backup-create.py')
RECEIPT = Path('/usr/local/libexec/funpayflow-backup-create.reviewed.json')
COORDINATOR = Path('/usr/local/libexec/funpayflow-maintenance.py')
COORDINATOR_RECEIPT = Path('/usr/local/libexec/funpayflow-maintenance.reviewed.json')
MAINTENANCE = Path('/run/funpayflow-maintenance')
AUTHORIZATION = MAINTENANCE / 'backup.json'
MAINTENANCE_LOCK = MAINTENANCE / 'exclusive.lock'
UNITS = ('funpayflow.service', 'funpay-bot.service')
REQUIRED = {'.env', 'bot_settings.json', 'state.sqlite3'}
TRANSIENT = {'bot.lock', 'state.sqlite3-wal', 'state.sqlite3-shm'}
MAX_ENTRIES = 2048
MAX_DEPTH = 12
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_ARCHIVE = MAX_TOTAL + MAX_ENTRIES * 1024 + 1024
MAX_MANIFEST = 2 * 1024 * 1024
RESERVE = 512 * 1024 * 1024
CHUNK = 64 * 1024
SECONDS = 120
MAX_PROCESSES = 4096
MAX_FDS = 16384
STATUSES = {
    'created', 'invalid_arguments', 'untrusted_installation', 'not_quiescent',
    'source_not_frozen', 'maintenance_required', 'unsafe_source', 'limit_exceeded',
    'insufficient_space',
    'busy', 'verification_failed', 'io_failed', 'cleanup_failed',
    'publication_unknown',
}


class Refused(Exception):
    def __init__(self, code='unsafe_source'):
        self.code = code if code in STATUSES else 'io_failed'
        super().__init__()  # Never attach private data to exceptions.


def require(condition, code='unsafe_source'):
    if not condition:
        raise Refused(code)


def identity(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
            s.st_ctime_ns if os.name == 'posix' else 0, s.st_mode, s.st_nlink)


def inode(s):
    return s.st_dev, s.st_ino, s.st_uid, stat.S_IFMT(s.st_mode)


def secure_stat(s, directory, private=True):
    require((stat.S_ISDIR if directory else stat.S_ISREG)(s.st_mode))
    require(s.st_uid == 0 and not s.st_mode & (0o077 if private else 0o022))
    require(not s.st_mode & 0o7000)
    if not directory:
        require(s.st_nlink == 1 and not s.st_mode & 0o111)
        require(bool(s.st_mode & 0o400))
    else:
        require(s.st_mode & 0o500 == 0o500)


class Budget:
    def __init__(self):
        self.end = time.monotonic() + SECONDS

    def check(self):
        require(time.monotonic() < self.end, 'limit_exceeded')


def safe_name(name):
    # A deliberately narrow, portable ustar subset. Refuse, never silently omit.
    require(isinstance(name, str) and 0 < len(name.encode('utf-8')) <= 100)
    require(not any(c in name for c in '\\:<>"|?*'))
    require(all(32 <= ord(c) < 127 for c in name))
    parts = name.split('/')
    require(len(parts) <= MAX_DEPTH, 'limit_exceeded')
    require(all(p not in ('', '.', '..') and not p.endswith((' ', '.'))
                for p in parts))
    require(all(p.split('.')[0].upper() not in
                {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)),
                 *(f'LPT{i}' for i in range(10))} for p in parts))
    return name


def open_read(path, platform):
    before = path.lstat()
    platform.validate(path, before, False)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
    stream = os.fdopen(fd, 'rb')
    try:
        require(identity(before) == identity(os.fstat(fd)))
        return stream, before
    except BaseException:
        stream.close()
        raise


def consume(path, platform, budget, output=None, limit=MAX_FILE):
    stream, before = open_read(path, platform)
    digest = hashlib.sha256()
    with stream:
        require(before.st_size <= limit, 'limit_exceeded')
        remaining = before.st_size
        while remaining:
            budget.check()
            block = stream.read(min(CHUNK, remaining))
            require(bool(block))
            digest.update(block)
            if output is not None:
                output.write(block)
            remaining -= len(block)
        require(stream.read(1) == b'')
        require(identity(before) == identity(os.fstat(stream.fileno())))
        require(identity(before) == identity(path.lstat()))
    return before.st_size, digest.hexdigest()


def inventory(root, platform, budget):
    result, folded, total = {}, set(), 0
    base = root.lstat()
    platform.validate(root, base, True)

    def visit(directory, prefix):
        nonlocal total
        budget.check()
        before = directory.lstat()
        platform.validate(directory, before, True)
        require(before.st_dev == base.st_dev)
        with os.scandir(directory) as entries:
            for entry in entries:
                budget.check()
                require(len(result) < MAX_ENTRIES, 'limit_exceeded')
                name = safe_name(prefix + entry.name)
                require(name.casefold() not in folded)
                folded.add(name.casefold())
                path = directory / entry.name
                s = path.lstat()
                is_dir = stat.S_ISDIR(s.st_mode)
                platform.validate(path, s, is_dir)
                require(s.st_dev == base.st_dev)
                result[name] = dict(directory=is_dir, identity=identity(s))
                if is_dir:
                    visit(path, name + '/')
                else:
                    require(s.st_size <= MAX_FILE, 'limit_exceeded')
                    total += s.st_size
                    require(total <= MAX_TOTAL, 'limit_exceeded')
                    size, digest = consume(path, platform, budget)
                    result[name].update(size=size, sha256=digest)
                require(identity(s) == identity(path.lstat()))
        require(identity(before) == identity(directory.lstat()))

    visit(root, '')
    require(REQUIRED <= result.keys())
    require(all(not result[n]['directory'] for n in REQUIRED))
    require(all(not result[n]['directory'] for n in TRANSIENT if n in result))
    # Hot rollback journals require a separate reviewed recovery procedure.
    require('state.sqlite3-journal' not in result)
    return identity(base), result, total


class Owned:
    """Track only objects created by this invocation; never recursive-delete."""
    def __init__(self, platform):
        self.platform, self.objects = platform, []

    def remember(self, path):
        s = path.lstat()
        self.platform.validate(path, s, stat.S_ISDIR(s.st_mode))
        self.objects.append((path, inode(s)))

    def mkdir(self, path):
        path.mkdir(mode=0o700)
        self.remember(path)
        return path

    def file(self, path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            self.remember(path)
            require(inode(os.fstat(fd)) == self.objects[-1][1])
            return os.fdopen(fd, 'wb')
        except BaseException:
            os.close(fd)
            raise

    def write(self, path, value):
        with self.file(path) as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())

    def forget(self, root):
        self.objects = [(p, i) for p, i in self.objects if not p.is_relative_to(root)]

    def cleanup(self):
        # Verify ALL surviving identities and parents before deleting any object.
        surviving = []
        known = dict(self.objects)
        for path, expected in self.objects:
            try:
                s = path.lstat()
            except FileNotFoundError:
                continue  # SQLite may remove its own precreated sidecars.
            require(inode(s) == expected, 'cleanup_failed')
            self.platform.validate(path, s, stat.S_ISDIR(s.st_mode))
            for parent in path.parents:
                if parent in known:
                    require(inode(parent.lstat()) == known[parent], 'cleanup_failed')
            surviving.append((path, s))
        for path, s in reversed(surviving):
            if stat.S_ISDIR(s.st_mode):
                path.rmdir()  # Refuse unexpected files, including unknown journals.
            else:
                require(s.st_nlink == 1, 'cleanup_failed')
                path.unlink()


def configure_db(db, budget):
    db.enable_load_extension(False)
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA mmap_size=0')
    db.execute('PRAGMA cache_size=-2048')
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_FILE)
    db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)
    steps = 20000

    def progress():
        nonlocal steps
        steps -= 1
        budget.check()
        return int(steps <= 0)

    db.set_progress_handler(progress, 1000)


def check_db(path, budget):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1',
                                 uri=True, timeout=0)) as db:
        configure_db(db, budget)
        for pragma in ('quick_check', 'integrity_check'):
            budget.check()
            cursor = db.execute('PRAGMA ' + pragma + '(1)')
            require(cursor.fetchone() == ('ok',) and cursor.fetchone() is None,
                    'verification_failed')


def validate_wal(input_db, platform, budget):
    """Reject damaged/ambiguous WAL rather than letting SQLite ignore a tail.

    Format reference: https://www.sqlite.org/fileformat2.html#wal_file_format
    This strict subset also refuses legitimate reused WALs with stale tails.
    """
    wal = Path(str(input_db) + '-wal')
    if not wal.exists() or wal.stat().st_size == 0:
        return
    db_stream, _ = open_read(input_db, platform)
    with db_stream:
        db_header = db_stream.read(100)
    require(len(db_header) == 100 and db_header[:16] == b'SQLite format 3\0'
            and db_header[18:20] == b'\2\2', 'verification_failed')
    page_size = int.from_bytes(db_header[16:18], 'big')
    if page_size == 1:
        page_size = 65536
    require(512 <= page_size <= 65536 and page_size & (page_size - 1) == 0,
            'verification_failed')
    stream, initial = open_read(wal, platform)
    with stream:
        require(32 < initial.st_size <= MAX_FILE and
                (initial.st_size - 32) % (24 + page_size) == 0, 'verification_failed')
        raw = stream.read(32)
        magic, version, size, _, salt1, salt2, check1, check2 = struct.unpack('>8I', raw)
        require(magic in (0x377f0682, 0x377f0683) and version == 3007000
                and size == page_size, 'verification_failed')
        endian = '<' if magic == 0x377f0682 else '>'

        def checksum(value, first=0, second=0):
            for x, y in struct.iter_unpack(endian + '2I', value):
                first = (first + x + second) & 0xffffffff
                second = (second + y + first) & 0xffffffff
            return first, second

        checks = checksum(raw[:24])
        require(checks == (check1, check2), 'verification_failed')
        committed = 0
        for _ in range((initial.st_size - 32) // (24 + page_size)):
            budget.check()
            frame, page = stream.read(24), stream.read(page_size)
            require(len(frame) == 24 and len(page) == page_size, 'verification_failed')
            number, committed, a, b, c, d = struct.unpack('>6I', frame)
            require(0 < number <= MAX_FILE // page_size and
                    committed <= MAX_FILE // page_size and (a, b) == (salt1, salt2),
                    'verification_failed')
            checks = checksum(page, *checksum(frame[:8], *checks))
            require(checks == (c, d), 'verification_failed')
        require(committed > 0 and stream.read(1) == b'', 'verification_failed')
        require(identity(initial) == identity(os.fstat(stream.fileno())))
        require(identity(initial) == identity(wal.lstat()))


def sqlite_snapshot(input_db, target, owned, budget):
    # Source is a private byte-for-byte clone, including committed WAL. SQLite
    # may rebuild SHM here; it NEVER opens the original data directory.
    validate_wal(input_db, owned.platform, budget)
    owned.write(target, b'')
    # Reserve known SQLite sidecar names exclusively before SQLite opens them.
    for suffix in ('-wal', '-shm', '-journal'):
        path = Path(str(input_db) + suffix)
        if not path.exists():
            owned.write(path, b'')
    for suffix in ('-wal', '-shm', '-journal'):
        owned.write(Path(str(target) + suffix), b'')
    with closing(sqlite3.connect(input_db.as_uri() + '?mode=ro', uri=True,
                                 timeout=0)) as source:
        configure_db(source, budget)
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        with closing(sqlite3.connect(target, timeout=0)) as dest:
            dest.execute('PRAGMA trusted_schema=OFF')
            dest.execute('PRAGMA journal_mode=OFF')
            dest.execute('PRAGMA synchronous=FULL')

            def progress(status, remaining, pages):
                budget.check()
                require(status in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE),
                        'verification_failed')
                require(pages * page_size <= MAX_FILE, 'limit_exceeded')

            source.backup(dest, pages=128, progress=progress, sleep=0)
            dest.execute('PRAGMA journal_mode=DELETE')
    require(target.stat().st_size <= MAX_FILE, 'limit_exceeded')
    # Remove only our known sidecars, outside the archive tree inventory.
    check_db(target, budget)
    fd = os.open(target, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'r+b') as stream:
        os.fsync(stream.fileno())


def header(name, entry):
    info = tarfile.TarInfo(name + '/' if entry['directory'] else name)
    info.type = tarfile.DIRTYPE if entry['directory'] else tarfile.REGTYPE
    info.mode = 0o700 if entry['directory'] else 0o600
    info.size = 0 if entry['directory'] else entry['size']
    return info.tobuf(format=tarfile.USTAR_FORMAT, encoding='ascii', errors='strict')


def archive_create(path, tree, entries, owned, platform, budget):
    with owned.file(path) as out:
        for name, entry in sorted(entries.items()):
            budget.check()
            out.write(header(name, entry))
            if not entry['directory']:
                size, digest = consume(tree / name, platform, budget, out)
                require((size, digest) == (entry['size'], entry['sha256']),
                        'verification_failed')
                out.write(bytes((-size) % 512))
        out.write(bytes(1024))
        require(out.tell() <= MAX_ARCHIVE, 'limit_exceeded')
        out.flush()
        os.fsync(out.fileno())


def archive_restore(path, recovery, entries, owned, platform, budget):
    """Strict expected-header parser; never extract archive-controlled paths."""
    stream, before = open_read(path, platform)
    with stream:
        require(before.st_size <= MAX_ARCHIVE, 'limit_exceeded')
        for name, entry in sorted(entries.items()):
            budget.check()
            safe_name(name)
            require(stream.read(512) == header(name, entry), 'verification_failed')
            target = recovery / name
            if entry['directory']:
                owned.mkdir(target)
                continue
            require(0 <= entry['size'] <= MAX_FILE, 'limit_exceeded')
            digest, remaining = hashlib.sha256(), entry['size']
            with owned.file(target) as output:
                while remaining:
                    budget.check()
                    block = stream.read(min(CHUNK, remaining))
                    require(bool(block), 'verification_failed')
                    remaining -= len(block)
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            require(digest.hexdigest() == entry['sha256'], 'verification_failed')
            require(stream.read((-entry['size']) % 512) ==
                    bytes((-entry['size']) % 512), 'verification_failed')
        require(stream.read(1024) == bytes(1024), 'verification_failed')
        require(stream.read(1) == b'', 'verification_failed')
        require(identity(before) == identity(os.fstat(stream.fileno())))
        require(identity(before) == identity(path.lstat()))
    check_db(recovery / 'state.sqlite3', budget)


def portable_entries(items):
    return {n: {k: v for k, v in e.items() if k != 'identity'}
            for n, e in items.items()}


def report(status, files=0, byte_count=0):
    return dict(status=status, files=files, bytes=byte_count,
                restore_verified=status == 'created')


def create(*, data, backups, platform):
    """Dependency-injected synthetic-test API. No production defaults here."""
    owned, budget = Owned(platform), Budget()
    result = report('io_failed')
    publication_attempted = False
    previous_mask = os.umask(0o077)
    try:
        data, backups = Path(data).absolute(), Path(backups).absolute()
        require(not data.is_relative_to(backups) and not backups.is_relative_to(data))
        platform.authorized()
        platform.paths(data, backups)
        platform.quiescent(data)
        platform.frozen(data)
        with platform.lock(backups):
            platform.authorized()
            platform.quiescent(data)
            platform.frozen(data)
            baseline = inventory(data, platform, budget)
            # WAL page numbers can expand a tiny input to MAX_FILE. Account for
            # the standalone clone, tree, recovery and archive, not just input bytes.
            platform.space(backups, baseline[2] * 5 + MAX_FILE * 4 + RESERVE
                           + MAX_MANIFEST * 2 + MAX_ENTRIES * 2048)
            token = uuid.uuid4().hex
            work = owned.mkdir(backups / ('.pending-' + token))
            incoming = owned.mkdir(work / 'input')
            tree = owned.mkdir(work / 'tree')
            recovery = owned.mkdir(work / 'recovery')
            bundle = owned.mkdir(work / 'bundle')
            for name, entry in sorted(baseline[1].items()):
                budget.check()
                if entry['directory']:
                    owned.mkdir(tree / name)
                elif name not in TRANSIENT:
                    target = incoming / name if name == 'state.sqlite3' else tree / name
                    with owned.file(target) as out:
                        require(consume(data / name, platform, budget, out) ==
                                (entry['size'], entry['sha256']))
                        out.flush()
                        os.fsync(out.fileno())
            if 'state.sqlite3-wal' in baseline[1]:
                with owned.file(incoming / 'state.sqlite3-wal') as out:
                    entry = baseline[1]['state.sqlite3-wal']
                    require(consume(data / 'state.sqlite3-wal', platform, budget, out)
                            == (entry['size'], entry['sha256']))
                    out.flush()
                    os.fsync(out.fileno())
            # Keep SQLite's potential sidecars out of the archive tree.
            db_stage = incoming / 'standalone.sqlite3'
            sqlite_snapshot(incoming / 'state.sqlite3', db_stage, owned, budget)
            with owned.file(tree / 'state.sqlite3') as out:
                consume(db_stage, platform, budget, out)
                out.flush()
                os.fsync(out.fileno())
            staged = inventory(tree, platform, budget)
            entries = portable_entries(staged[1])
            expected_names = baseline[1].keys() - TRANSIENT
            require(entries.keys() == expected_names, 'verification_failed')
            original_entries = portable_entries(baseline[1])
            for name in expected_names - {'state.sqlite3'}:
                require(entries[name] == original_entries[name],
                        'verification_failed')
            archive = bundle / 'data.tar'
            archive_create(archive, tree, entries, owned, platform, budget)
            archive_size, archive_hash = consume(
                archive, platform, budget, limit=MAX_ARCHIVE)
            archive_restore(archive, recovery, entries, owned, platform, budget)
            recovered = inventory(recovery, platform, budget)
            require(portable_entries(recovered[1]) == entries, 'verification_failed')
            manifest = dict(format='funpayflow-backup-v2', runtime_sha='unknown',
                            restore_verified=True,
                            verification_scope='offline-filesystem-and-sqlite-only',
                            code_compatibility='unknown', entries=entries,
                            excluded_transients=sorted(TRANSIENT & baseline[1].keys()),
                            archive_sha256=archive_hash, archive_bytes=archive_size)
            encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
            require(len(encoded) <= MAX_MANIFEST, 'limit_exceeded')
            manifest_path = bundle / 'manifest.json'
            owned.write(manifest_path, encoded)
            require(consume(manifest_path, platform, budget, limit=MAX_MANIFEST) ==
                    (len(encoded), hashlib.sha256(encoded).hexdigest()),
                    'verification_failed')
            platform.frozen(data)
            require(inventory(data, platform, budget) == baseline)
            platform.quiescent(data)
            platform.frozen(data)
            require(consume(archive, platform, budget, limit=MAX_ARCHIVE) ==
                    (archive_size, archive_hash), 'verification_failed')
            platform.paths(data, backups)
            platform.sync_dir(bundle)
            budget.check()
            platform.authorized()
            publication_attempted = True
            platform.publish(bundle, backups / ('snapshot-' + token))
            owned.forget(bundle)
            platform.sync_dir(backups)
            platform.authorized()
            result = report('created', sum(not e['directory'] for e in entries.values()),
                            recovered[2])
    except Refused as exc:
        result = report('publication_unknown' if publication_attempted else exc.code)
    except (Exception, KeyboardInterrupt):
        result = report('publication_unknown' if publication_attempted else 'io_failed')
    finally:
        try:
            if result['status'] != 'publication_unknown':
                owned.cleanup()
        except (Exception, KeyboardInterrupt):
            result = report('publication_unknown' if publication_attempted else 'cleanup_failed')
        os.umask(previous_mask)
    return result


def read_bounded(path, maximum):
    with open(path, 'rb') as stream:
        value = stream.read(maximum + 1)
    require(len(value) <= maximum, 'limit_exceeded')
    return value


def protected(path, executable=False):
    """No symlinks, hardlinked files, writable ancestors, or checkout parents."""
    require(path.is_absolute() and path == path.resolve(), 'untrusted_installation')
    for part in reversed((path, *path.parents)):
        s = part.lstat()
        if part == path and executable:
            require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1)
            require(s.st_uid == 0 and not s.st_mode & 0o6022)
        else:
            secure_stat(s, stat.S_ISDIR(s.st_mode), private=False)
        if stat.S_ISDIR(s.st_mode):
            require(not (part / '.git').exists(), 'untrusted_installation')


def trusted_installation():
    try:
        require(sys.platform == 'linux' and os.getuid() == os.geteuid() == 0)
        require(os.getgid() == os.getegid() == 0)
        require(sys.flags.isolated and sys.flags.no_site and sys.flags.ignore_environment)
        require(Path(__file__) == INSTALLED and Path(sys.argv[0]) == INSTALLED)
        protected(INSTALLED)
        protected(RECEIPT)
        executable = Path(sys.executable).resolve()
        require(re.fullmatch(r'/usr/bin/python3(?:\.\d+)?', str(executable)) is not None)
        protected(executable, executable=True)
        for item in sys.path:
            path = Path(item)
            require(path.is_absolute())
            protected(path if path.exists() else path.parent)
        command = read_bounded('/proc/self/cmdline', 4096).split(b'\0')
        require(command[-1] == b'' and len(command) == 6)
        require(Path(os.fsdecode(command[0])).resolve() == executable)
        require(command[1:-1] == [b'-I', b'-S', bytes(INSTALLED), b'create'])
        receipt = json.loads(read_bounded(RECEIPT, 1024))
        require(set(receipt) == {'reviewed_commit', 'script_sha256'})
        require(re.fullmatch('[0-9a-f]{40}', receipt['reviewed_commit']) is not None)
        require(re.fullmatch('[0-9a-f]{64}', receipt['script_sha256']) is not None)
        require(hashlib.sha256(read_bounded(INSTALLED, 128 * 1024)).hexdigest()
                == receipt['script_sha256'])
    except Exception:
        raise Refused('untrusted_installation') from None


def inactive(value):
    """Strict parser for the ONLY systemctl query used below."""
    fields = {}
    for line in value.decode('ascii', 'strict').splitlines():
        key, sep, val = line.partition('=')
        require(sep and key not in fields, 'not_quiescent')
        fields[key] = val
    require(fields == dict(LoadState='loaded', ActiveState='inactive', SubState='dead',
                           MainPID='0', ControlPID='0', Job=''), 'not_quiescent')


def systemd_status(unit):
    require(unit in UNITS, 'not_quiescent')
    command = ['/usr/bin/systemctl', '--system', '--no-pager', 'show', unit,
               '--property=LoadState,ActiveState,SubState,MainPID,ControlPID,Job']
    # Never a shell; fixed command, fixed environment, bounded pipe and deadline.
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin',
                          'LANG': 'C', 'LC_ALL': 'C', 'SYSTEMD_PAGER': ''}) as process:
        try:
            deadline, output = time.monotonic() + 5, bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, 'not_quiescent')
                    require(selector.select(remaining), 'not_quiescent')
                    block = os.read(process.stdout.fileno(), 4097 - len(output))
                    if not block:
                        break
                    output.extend(block)
                    require(len(output) <= 4096, 'not_quiescent')
            require(process.wait(timeout=max(0.001, deadline - time.monotonic())) == 0,
                    'not_quiescent')
            return bytes(output)
        finally:
            if process.poll() is None:
                process.kill()  # Only our read-only systemctl subprocess.
                process.wait(timeout=2)


def frozen_mount(raw, device):
    """Validate the supported single rw ext4 root layout; NOT immutability.

    Kept under the original name for the injected platform API. Quiescence is
    supplied by the separate coordinator, never by these mount observations.
    """
    found = False
    for line in raw.decode('ascii', 'strict').splitlines():
        left, separator, right = line.partition(' - ')
        fields, after = left.split(), right.split()
        require(separator and len(fields) >= 6 and len(after) == 3, 'source_not_frozen')
        mount = fields[4]
        require('\\' not in mount, 'source_not_frozen')
        path = Path(mount)
        sensitive = any(path == target or path in target.parents
                        or path.is_relative_to(target) for target in (DATA, BACKUPS))
        require(mount == '/' or not sensitive, 'source_not_frozen')
        if fields[2] == device:
            require(mount == '/' and fields[3] == '/' and after[0] == 'ext4'
                    and 'rw' in fields[5].split(',') and 'rw' in after[2].split(','),
                    'source_not_frozen')
        if mount == '/':
            require(fields[2] == device and not found, 'source_not_frozen')
            found = True
    require(found, 'source_not_frozen')


def process_safe(command):
    lowered = command.lower()
    require(not any(marker in lowered for marker in
                    (b'funpay', b'securebot', b'python', b'pypy')), 'not_quiescent')


def pinned_digest(script, receipt_path):
    protected(script)
    protected(receipt_path)
    receipt = json.loads(read_bounded(receipt_path, 1024))
    require(set(receipt) == {'reviewed_commit', 'script_sha256'})
    require(re.fullmatch('[0-9a-f]{40}', receipt['reviewed_commit']) is not None)
    digest = hashlib.sha256(read_bounded(script, 128 * 1024)).hexdigest()
    require(digest == receipt['script_sha256'])
    return digest


def process_identity(pid):
    fields = read_bounded(Path('/proc') / str(pid) / 'stat', 4096).rsplit(b') ', 1)[1].split()
    require(len(fields) >= 20 and fields[0] not in (b'Z', b'X'))
    return int(fields[1]), int(fields[19])  # PPID and kernel start ticks (field 22).


def maintenance_evidence():
    """Consume, never create, a coordinator's live authorization.

    Trust rests on the separately reviewed root coordinator/installer, not the
    JSON booleans. FD 3 MUST be its inherited, still exclusively flocked OFD.
    A plain marker, CLI option, environment variable or a helper-created lock
    cannot pass. Root capable of impersonating the installer is out of scope.
    """
    try:
        import fcntl
        parent, start = process_identity(os.getpid())
        require(parent == os.getppid() and parent > 1)
        _, parent_start = process_identity(parent)
        coordinator_hash = pinned_digest(COORDINATOR, COORDINATOR_RECEIPT)
        helper_hash = pinned_digest(INSTALLED, RECEIPT)
        executable = Path(sys.executable).resolve()
        root = Path('/proc') / str(parent)
        require(Path(os.readlink(root / 'exe')) == executable)
        for namespace in ('pid', 'mnt', 'user'):
            require(os.readlink(root / 'ns' / namespace) ==
                    os.readlink('/proc/self/ns/' + namespace))
        command = read_bounded(root / 'cmdline', 4096).split(b'\0')
        require(len(command) == 6 and command[1:] ==
                [b'-I', b'-S', bytes(COORDINATOR), b'maintain-backup', b''])
        require(Path(os.fsdecode(command[0])).resolve() == executable)
        credentials = read_bounded(root / 'status', 16384).splitlines()
        for key in (b'Uid:', b'Gid:'):
            require([line.split()[1:] for line in credentials if line.startswith(key)]
                    == [[b'0'] * 4])
        for path in (MAINTENANCE, AUTHORIZATION, MAINTENANCE_LOCK):
            protected(path)
            secure_stat(path.lstat(), path == MAINTENANCE)
        lock_stat = MAINTENANCE_LOCK.lstat()
        require(inode(os.fstat(3)) == inode(lock_stat))
        # fdinfo exposes the lock attached to THIS inherited open description,
        # including its original owner PID. A different process's lock is not enough.
        locks = [line.split() for line in read_bounded('/proc/self/fdinfo/3', 4096).splitlines()
                 if line.startswith(b'lock:')]
        require(len(locks) == 1 and len(locks[0]) == 9)
        lock = locks[0]
        require(lock[2:6] == [b'FLOCK', b'ADVISORY', b'WRITE', str(parent).encode()]
                and lock[7:] == [b'0', b'EOF'])
        major, minor, number = lock[6].split(b':')
        require((int(major, 16), int(minor, 16), int(number)) ==
                (os.major(lock_stat.st_dev), os.minor(lock_stat.st_dev), lock_stat.st_ino))
        fd = os.open(MAINTENANCE_LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            require(inode(os.fstat(fd)) == inode(lock_stat))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise Refused('maintenance_required')
        finally:
            os.close(fd)
        before = AUTHORIZATION.lstat()
        raw = read_bounded(AUTHORIZATION, 4096)
        require(identity(before) == identity(AUTHORIZATION.lstat()))
        evidence = json.loads(raw)
        require(raw == json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode())
        require(type(evidence.get('version')) is int)
        require(all(evidence.get(key) is True for key in
                    ('services_inhibited', 'writers_quiescent', 'recovery_armed')))
        now = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        expires = evidence.get('expires_boottime_ns')
        require(type(expires) is int and now < expires <= now + (SECONDS + 30) * 10**9)
        boot = read_bounded('/proc/sys/kernel/random/boot_id', 64).strip().decode('ascii')
        require(re.fullmatch('[0-9a-f-]{36}', boot) is not None)
        require(evidence == dict(version=1, helper_pid=os.getpid(), helper_start=start,
                coordinator_pid=parent, coordinator_start=parent_start,
                helper_sha256=helper_hash, coordinator_sha256=coordinator_hash,
                boot_id=boot, expires_boottime_ns=expires, units=list(UNITS),
                data=str(DATA), backups=str(BACKUPS), services_inhibited=True,
                writers_quiescent=True, recovery_armed=True))
        require(process_identity(os.getpid()) == (parent, start))
        require(process_identity(parent)[1] == parent_start)
        return parent, raw, identity(before), inode(lock_stat)
    except Exception:
        raise Refused('maintenance_required') from None


def source_inodes(data):
    """Bounded metadata-only walk before any private payload is read."""
    result, budget = set(), Budget()
    device = data.lstat().st_dev

    def visit(path, depth):
        budget.check()
        require(depth <= MAX_DEPTH and len(result) < MAX_ENTRIES + 1, 'limit_exceeded')
        s = path.lstat()
        directory = stat.S_ISDIR(s.st_mode)
        secure_stat(s, directory)
        require(s.st_dev == device)
        result.add((s.st_dev, s.st_ino))
        if directory:
            with os.scandir(path) as entries:
                for entry in entries:
                    visit(path / entry.name, depth + 1)
        require(identity(s) == identity(path.lstat()))

    visit(data, 0)
    return result


def references_data(path, data):
    # /proc links may end in " (deleted)". Prefix checks deliberately include
    # that case; inode checks additionally cover renamed/mapped aliases.
    return path == str(data) or path.startswith(str(data) + '/')


class Linux:
    def __init__(self):
        self.evidence = None

    def authorized(self):
        current = maintenance_evidence()
        require(self.evidence is None or self.evidence == current, 'maintenance_required')
        self.evidence = current

    def validate(self, path, s, directory):
        secure_stat(s, directory)

    def paths(self, data, backups):
        require(data == DATA and backups == BACKUPS)
        for path in (data, backups):
            protected(path)
            secure_stat(path.lstat(), True)
        require(data.stat().st_dev == backups.stat().st_dev, 'source_not_frozen')

    def frozen(self, data):
        require(not os.statvfs(data).f_flag & os.ST_RDONLY, 'source_not_frozen')
        require(not os.statvfs(BACKUPS).f_flag & os.ST_RDONLY, 'source_not_frozen')
        s = data.stat()
        frozen_mount(read_bounded('/proc/self/mountinfo', 1024 * 1024),
                     f'{os.major(s.st_dev)}:{os.minor(s.st_dev)}')

    def quiescent(self, data):
        self.authorized()
        for namespace in ('pid', 'mnt', 'user'):
            require(os.readlink('/proc/self/ns/' + namespace) ==
                    os.readlink('/proc/1/ns/' + namespace), 'not_quiescent')
        require(read_bounded('/proc/1/comm', 64).strip() == b'systemd', 'not_quiescent')
        protected(Path('/usr/bin/systemctl'), executable=True)
        for unit in UNITS:
            inactive(systemd_status(unit))
        objects = source_inodes(data)
        before = self.pids()
        for pid in before:
            if pid == str(os.getpid()):
                continue
            root = Path('/proc') / pid
            coordinator = self.evidence[0]
            if pid != str(coordinator):
                process_safe(read_bounded(root / 'cmdline', 16384))
            # Include executable/cwd aliases; failures/disappearing processes
            # are ambiguous and refuse. Kernel threads have no exe/cwd.
            proc_stat = read_bounded(root / 'stat', 4096).rsplit(b') ', 1)[1].split()
            kernel_thread = bool(int(proc_stat[6]) & 0x00200000)
            if not kernel_thread:
                executable = os.readlink(root / 'exe')
                cwd = os.readlink(root / 'cwd')
                require(not references_data(cwd, data), 'not_quiescent')
                if pid != str(coordinator):
                    process_safe(os.fsencode(executable))
                    process_safe(os.fsencode(cwd))
            with os.scandir(root / 'fd') as entries:
                for index, entry in enumerate(entries):
                    require(index < MAX_FDS, 'limit_exceeded')
                    s = os.stat(entry.path)
                    require((s.st_dev, s.st_ino) not in objects and
                            not references_data(os.readlink(entry.path), data),
                            'not_quiescent')
            maps = read_bounded(root / 'maps', 4 * 1024 * 1024)
            for line in maps.splitlines():
                fields = line.split(None, 5)
                require(len(fields) >= 5, 'not_quiescent')
                major, minor = (int(v, 16) for v in fields[3].split(b':'))
                require((os.makedev(major, minor), int(fields[4])) not in objects
                        and not (len(fields) == 6 and references_data(
                            os.fsdecode(fields[5]), data)), 'not_quiescent')
        require(self.pids() == before, 'not_quiescent')
        require(source_inodes(data) == objects, 'not_quiescent')
        self.authorized()

    @staticmethod
    def pids():
        result = set()
        with os.scandir('/proc') as entries:
            for index, entry in enumerate(entries):
                require(index < MAX_PROCESSES + 1024, 'limit_exceeded')
                if entry.name.isdecimal():
                    result.add(entry.name)
                    require(len(result) <= MAX_PROCESSES, 'limit_exceeded')
        return result

    @staticmethod
    def space(path, needed):
        s = os.statvfs(path)
        require(not s.f_flag & os.ST_RDONLY and s.f_bavail * s.f_frsize >= needed
                and s.f_favail >= MAX_ENTRIES * 4 + 64, 'insufficient_space')

    @contextmanager
    def lock(self, backups):
        import fcntl
        path = backups / '.backup-create.lock'
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            before = path.lstat()
            self.validate(path, before, False)
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            if identity(before) != identity(os.fstat(fd)):
                os.close(fd)
                raise Refused('busy')
        try:
            self.validate(path, os.fstat(fd), False)
            require(inode(path.lstat()) == inode(os.fstat(fd)), 'busy')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Refused('busy') from None
            yield
            require(inode(path.lstat()) == inode(os.fstat(fd)), 'busy')
        finally:
            os.close(fd)
        # Permanent lock inode: never unlink and accidentally split flock users.

    @staticmethod
    def sync_dir(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def publish(source, target):
        # Atomic directory publication without replacement, including empty dirs.
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
            raise Refused('io_failed')


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    result = report('invalid_arguments')
    if args == ['create']:
        try:
            trusted_installation()
            # Process alarm supplements cooperative budgets; it cannot interrupt
            # kernel D-state IO or guarantee recovery. The coordinator must own
            # an independent deadline. Killed processes can leave pending files.
            def expired(signum, frame):
                raise Refused('limit_exceeded')

            signal.signal(signal.SIGALRM, expired)
            signal.signal(signal.SIGTERM, expired)
            signal.alarm(SECONDS)
            try:
                result = create(data=DATA, backups=BACKUPS, platform=Linux())
            finally:
                signal.alarm(0)
        except Refused as exc:
            result = report(exc.code)
        except (Exception, KeyboardInterrupt):
            result = report('io_failed')
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'created' else 2 if args != ['create'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
