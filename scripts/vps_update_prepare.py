"""Fixed-target source verification and an explicitly disabled prepare boundary.

No build identity or isolation policy is installed by this checkout. In particular,
root SSH plus a venv is not isolation. prepare_once never starts SSH, installs a
package, creates a stage, or returns ENV_PREPARED. Importing is passive.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
import time
import urllib.request

TARGET = "0affd6784f2f903fa69a38ceb783a30b0816594e"
REPO = "Vetal12350/FunPayFlow"
ARCHIVE_URL = f"https://codeload.github.com/{REPO}/tar.gz/{TARGET}"
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_SOURCE = 64 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_ENTRIES = 4096
ISOLATION_REQUIRED = "NOT_PREPARED_ISOLATION_REQUIRED"
BLOCKER = ("Нет проверенной изоляции non-root сборки от production и credentials; "
           "root SSH и venv её не обеспечивают. DEPLOY_READY=NO.")


def prepare_once(*, approved: bool = False) -> dict[str, str]:
    """Fixed operation, no caller-selected SHA/path/command and no I/O, even approved.

    The approval contract is reserved for a separately reviewed executor. Approval
    cannot turn this prerequisite into an unsafe root build. No retry state or local
    markers are needed because there is no dispatch and no mutation at all.
    """
    if type(approved) is not bool or not approved:
        return {"status": "NOT_PREPARED_APPROVAL_REQUIRED", "target": TARGET, "deploy_ready": "NO"}
    return capability()


def capability() -> dict[str, str]:
    """Pure GUI gate; never infer capability from stage presence or GitHub data."""
    return {"status": ISOLATION_REQUIRED, "target": TARGET, "deploy_ready": "NO"}


def _safe_name(name: str) -> bool:
    parts = name.split("/")
    return (bool(re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", name))
            and all(p not in {"", ".", "..", ".git", ".env", ".venv", "venv",
                              "site-packages", "__pycache__", "private", "secrets"}
                    and (not p.startswith(".env.") or p == ".env.example") for p in parts))


def _digest(entries: dict) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("archive redirect refused")


def _fetch_archive() -> bytes:
    # Fixed public endpoint, GET only, no auth/config/issue-supplied URL, no disk cache.
    opener = urllib.request.build_opener(_NoRedirect())
    deadline = time.monotonic() + 20
    chunks, size = [], 0
    with opener.open(ARCHIVE_URL, timeout=10) as response:
        if response.status != 200 or response.geturl() != ARCHIVE_URL:
            raise ValueError()
        while True:
            block = response.read(65536)
            size += len(block)
            if size > MAX_ARCHIVE or time.monotonic() > deadline:
                raise ValueError()
            if not block:
                return b"".join(chunks)
            chunks.append(block)


def archive_digest(raw: bytes) -> str:
    """Hash bounded exact-target archive entries in memory; never extract or execute.

    Refuse links, special entries, duplicate/ambiguous paths and missing parents.
    This is a source-byte attestation from the fixed HTTPS origin, not build approval.
    """
    if not raw or len(raw) > MAX_ARCHIVE:
        raise ValueError()
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
        expanded = compressed.read(MAX_SOURCE + 1)
    if len(expanded) > MAX_SOURCE:
        raise ValueError()
    prefix = "FunPayFlow-" + TARGET
    entries, root_seen = {}, False
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        for member in archive:
            if member.name == prefix and member.isdir() and not root_seen:
                root_seen = True
                continue
            if not member.name.startswith(prefix + "/"):
                raise ValueError()
            name = member.name[len(prefix) + 1:]
            if not _safe_name(name) or name in entries or len(entries) >= MAX_ENTRIES:
                raise ValueError()
            if member.isdir():
                entries[name] = ["dir"]
            elif member.isreg() and not member.sparse and 0 <= member.size <= MAX_FILE:
                with archive.extractfile(member) as stream:
                    data = stream.read(MAX_FILE + 1)
                if len(data) != member.size:
                    raise ValueError()
                entries[name] = ["file", len(data), hashlib.sha256(data).hexdigest()]
            else:
                raise ValueError()
    if not root_seen or not all(entries.get(p, [None])[0] == "file" for p in (
            "pyproject.toml", "uv.lock", "src/funpayflow/main.py")):
        raise ValueError()
    for name in entries:
        parts = name.split("/")
        if any(entries.get("/".join(parts[:i])) != ["dir"] for i in range(1, len(parts))):
            raise ValueError()
    return _digest(entries)


def trusted_source_digest() -> str:
    return archive_digest(_fetch_archive())


# Reviewed stdlib only; sent as part of the fixed status collector, never a server
# filename or source module. Returns only a digest/UNKNOWN, never names or bytes.
# Directory descriptors + O_NOFOLLOW prevent traversal through changed symlinks.
SOURCE_COLLECTOR = r'''
import hashlib, json, os, re, stat, time

def stage_digest():
    deadline = time.monotonic() + 8
    entries, total = {}, 0
    def owned(st, directory):
        if (st.st_uid != 0 or st.st_mode & 0o022 or
            not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)) or
            (not directory and st.st_nlink != 1)):
            raise ValueError()
    def names(fd):
        with os.scandir(fd) as iterator:
            for entry in iterator:
                yield entry.name
    def walk(fd, prefix=''):
        nonlocal total
        for part in names(fd):
            name = prefix + part
            parts = name.split('/')
            if (not re.fullmatch(r'[A-Za-z0-9_./-]{1,240}', name) or
                any(p in {'', '.', '..', '.git', '.env', '.venv', 'venv', 'site-packages',
                          '__pycache__', 'private', 'secrets'} or
                    (p.startswith('.env.') and p != '.env.example') for p in parts) or
                len(entries) >= 4096 or time.monotonic() > deadline):
                raise ValueError()
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            directory = stat.S_ISDIR(before.st_mode)
            owned(before, directory)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError()
                owned(opened, directory)
                if directory:
                    entries[name] = ['dir']
                    walk(child, name + '/')
                else:
                    if opened.st_size > 8*1024*1024:
                        raise ValueError()
                    digest, size = hashlib.sha256(), 0
                    while True:
                        data = os.read(child, 65536)
                        size += len(data)
                        total += len(data)
                        if size > 8*1024*1024 or total > 64*1024*1024 or time.monotonic() > deadline:
                            raise ValueError()
                        if not data:
                            break
                        digest.update(data)
                    if size != opened.st_size:
                        raise ValueError()
                    entries[name] = ['file', size, digest.hexdigest()]
                after = os.fstat(child)
                if (opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError()
            finally:
                os.close(child)
    try:
        if os.geteuid() != 0 or os.getuid() != 0:
            return 'UNKNOWN'
        # Every ancestor is opened without following symlinks; no production data is read.
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            owned(os.fstat(fd), True)
            for part in ('root', '.FunPayFlow-source-0affd678'):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                owned(os.fstat(fd), True)
            walk(fd)
        finally:
            os.close(fd)
        return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    except Exception:
        return 'UNKNOWN'
'''
