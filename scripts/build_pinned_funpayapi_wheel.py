"""CI-internal fixed FunPayAPI wheel builder; importing this module does no I/O.

Only for a disposable, unprivileged Linux GitHub-hosted job with no secrets,
read-only token, and checkout credential persistence disabled. Environment markers
are refusal guards, not a sandbox or proof of that provisioning contract.
Requires preinstalled pip, setuptools and wheel; installs no build dependencies.
"""
from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
import hashlib
import http.client
import io
import itertools
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile


SOURCE_URL = "https://files.pythonhosted.org/packages/fd/c5/880876330bcf636e3dc06c34738b1411a502a12d1e5344e128d9f7ca80f5/FunPayAPI-1.1.0.tar.gz"
SOURCE_SHA256 = "c09500b5b461ac13052b8663bfeffad38399f531e0d2d1ff955afa504eda36c5"
SOURCE_SIZE = 45587
PACKAGE = "FunPayAPI"
VERSION = "1.1.0"
SOURCE_ROOT = "FunPayAPI-1.1.0"
MAX_FILE = 8 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_ENTRIES = 2000
NETWORK_TIMEOUT = 30
BUILD_TIMEOUT = 180


class Refused(ValueError):
    """A fixed, public reason, never third-party output or a private path."""


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def verify_lock():
    lock = Path(__file__).resolve().parents[1] / "uv.lock"
    with lock.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    require(len(raw) <= 2 * 1024 * 1024, "lock size limit")
    packages = [p for p in tomllib.loads(raw.decode("utf-8"))["package"]
                if p["name"] == "funpayapi"]
    require(len(packages) == 1, "lock package mismatch")
    package = packages[0]
    sdist = package.get("sdist", {})
    require(package.get("version") == VERSION and sdist.get("url") == SOURCE_URL
            and sdist.get("hash") == "sha256:" + SOURCE_SHA256
            and sdist.get("size") == SOURCE_SIZE, "lock source mismatch")


def verify_source(data):
    require(len(data) == SOURCE_SIZE, "source size mismatch")
    require(hashlib.sha256(data).hexdigest() == SOURCE_SHA256, "source hash mismatch")


def download_source():
    # Direct HTTPS: no proxy, netrc, auth, redirects, cookies or retries.
    connection = http.client.HTTPSConnection("files.pythonhosted.org", timeout=NETWORK_TIMEOUT)
    deadline = time.monotonic() + NETWORK_TIMEOUT
    try:
        connection.request("GET", SOURCE_URL.removeprefix("https://files.pythonhosted.org"),
                           headers={"Accept-Encoding": "identity"})
        network_socket = connection.sock
        response = connection.getresponse()
        require(response.status == 200, "source HTTP status refused")
        require(response.getheader("Content-Encoding", "identity") == "identity",
                "source encoding refused")
        length = response.getheader("Content-Length")
        require(length is None or length == str(SOURCE_SIZE), "source length refused")
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "source download timeout")
            network_socket.settimeout(remaining)
            block = response.read1(min(16384, SOURCE_SIZE + 1 - len(data)))
            if not block:
                break
            data.extend(block)
            require(len(data) <= SOURCE_SIZE, "source download size limit")
        verify_source(data)
        return bytes(data)
    finally:
        if "response" in locals():
            response.close()
        connection.close()


def safe_name(name):
    require(isinstance(name, str) and 0 < len(name) <= 240, "archive path refused")
    parts = name.removesuffix("/").split("/")
    # Deny hidden files/directories altogether, including .env/.ssh/.aws/.git.
    require(all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", p)
                and p.lower() not in {"credentials", "id_rsa", "id_ed25519"}
                and not p.lower().endswith((".pem", ".key")) for p in parts),
            "archive path refused")
    return "/".join(parts)


def check_tree(entries):
    seen = {}
    for name, directory in entries:
        key = name.casefold()
        require(key not in seen, "duplicate archive path")
        seen[key] = directory
    for key in seen:
        parts = key.split("/")
        require(all(seen.get("/".join(parts[:i]), True) for i in range(1, len(parts))),
                "archive file/directory conflict")


def extract_source(data, destination):
    verify_source(data)  # Also enforced for callers other than the downloader.
    files, entries, total = [], [], 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            require(len(entries) < MAX_ENTRIES, "source entry limit")
            name = safe_name(member.name)
            require(name == SOURCE_ROOT or name.startswith(SOURCE_ROOT + "/"),
                    "source root mismatch")
            require((member.isdir() or member.isreg()) and not member.issparse(),
                    "source entry type refused")
            require(0 <= member.size <= MAX_FILE and (not member.isdir() or member.size == 0),
                    "source file size limit")
            total += member.size
            require(total <= MAX_EXPANDED, "source expanded size limit")
            entries.append((name, member.isdir()))
            if member.isreg():
                with archive.extractfile(member) as stream:
                    content = stream.read(MAX_FILE + 1)
                require(len(content) == member.size, "source truncated")
                files.append((name, content))
    check_tree(entries)
    # Validate everything before writing; destination is a new private temp dir.
    require(destination.is_dir() and not any(destination.iterdir()), "source destination refused")
    for name, content in files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
    root = destination / SOURCE_ROOT
    require((root / "setup.py").is_file(), "conventional setuptools source required")
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        build = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("build-system", {})
        require(build.get("build-backend") in {"setuptools.build_meta", "setuptools.build_meta:__legacy__"}
                and "backend-path" not in build, "build backend refused")
    return root


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def tags(value):
    require(re.fullmatch(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*-[a-z0-9_]+(?:\.[a-z0-9_]+)*-[a-z0-9_]+(?:\.[a-z0-9_]+)*", value),
            "invalid wheel tag")
    groups = [part.split(".") for part in value.split("-")]
    require(all(len(group) <= 16 for group in groups), "wheel tag limit")
    return {"-".join(parts) for parts in itertools.product(*groups)}


def headers(data):
    message = BytesParser(policy=policy.default).parsebytes(data)
    require(not message.defects, "invalid wheel headers")
    return message


def single(message, key):
    values = message.get_all(key, [])
    require(len(values) == 1, "missing or duplicate wheel header")
    return str(values[0])


def validate_wheel(filename, data):
    require(len(data) <= MAX_FILE, "wheel size limit")
    fields = filename.removesuffix(".whl").split("-")
    require(filename.endswith(".whl") and len(fields) in {5, 6}
            and canonical(fields[0]) == "funpayapi" and fields[1] == VERSION,
            "wheel filename mismatch")
    if len(fields) == 6:
        require(re.fullmatch(r"[0-9][A-Za-z0-9_]*", fields[2]), "wheel build tag refused")
    filename_tags = tags("-".join(fields[-3:]))
    entries, contents, total = [], {}, 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(len(archive.infolist()) <= MAX_ENTRIES, "wheel entry limit")
        for member in archive.infolist():
            name = safe_name(member.orig_filename)
            require(member.orig_filename == member.filename, "normalized wheel path refused")
            kind = stat.S_IFMT(member.external_attr >> 16)
            require(kind in ({0, stat.S_IFDIR} if member.is_dir() else {0, stat.S_IFREG})
                    and not member.flag_bits & 1, "wheel entry type refused")
            require(0 <= member.file_size <= MAX_FILE, "wheel file size limit")
            total += member.file_size
            require(total <= MAX_EXPANDED, "wheel expanded size limit")
            entries.append((name, member.is_dir()))
            if not member.is_dir():
                with archive.open(member) as stream:
                    content = stream.read(MAX_FILE + 1)
                require(len(content) == member.file_size, "wheel truncated")
                contents[name] = content  # Reading also verifies CRC.
    check_tree(entries)
    dist_infos = {name.split("/")[0] for name, _ in entries
                  if name.split("/")[0].endswith(".dist-info")}
    require(len(dist_infos) == 1, "wheel dist-info count")
    dist_info = dist_infos.pop()
    identity = dist_info.removesuffix(".dist-info").split("-")
    require(len(identity) == 2 and canonical(identity[0]) == "funpayapi"
            and identity[1] == VERSION, "wheel dist-info mismatch")
    require(all(not part.endswith(".dist-info") or part == dist_info
                for name, _ in entries for part in name.split("/")), "nested dist-info refused")
    require(all(dist_info + "/" + name in contents for name in ("METADATA", "WHEEL", "RECORD")),
            "wheel metadata missing")
    metadata = headers(contents[dist_info + "/METADATA"])
    require(canonical(single(metadata, "Name")) == "funpayapi"
            and single(metadata, "Version") == VERSION, "wheel metadata identity mismatch")
    wheel = headers(contents[dist_info + "/WHEEL"])
    require(single(wheel, "Wheel-Version") == "1.0", "wheel format refused")
    pure = single(wheel, "Root-Is-Purelib")
    require(pure in {"true", "false"}, "wheel purelib header refused")
    wheel_tags = set()
    for tag in wheel.get_all("Tag", []):
        wheel_tags.update(tags(str(tag)))
    require(wheel_tags == filename_tags, "wheel tags mismatch")
    if pure == "true" or any(tag.endswith("-any") for tag in wheel_tags):
        require(all(not name.lower().endswith((".so", ".pyd", ".dll", ".dylib"))
                    and ".data/platlib/" not in name for name in contents),
                "wheel contains platform content with pure/universal tags")
    return {"package": PACKAGE, "version": VERSION, "filename": filename,
            "source_sha256": SOURCE_SHA256, "wheel_sha256": hashlib.sha256(data).hexdigest(),
            "wheel_tags": sorted(wheel_tags)}


def real_directory(path):
    require(path.is_absolute(), "absolute CI directory required")
    require(all(not part.is_symlink() for part in (path, *path.parents)),
            "symlink directory refused")
    require(path.is_dir(), "CI directory missing")
    return path.resolve()


def ci_context():
    require(sys.platform == "linux" and os.geteuid() != 0, "unprivileged Linux CI required")
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("CI") == "true", "disposable GitHub-hosted CI required")
    root = real_directory(Path(os.environ.get("RUNNER_TEMP", "")))
    require(root != Path("/") and root.stat().st_uid == os.geteuid()
            and not root.stat().st_mode & 0o022, "private runner temp required")
    return root


def build_wheel(source, work, wheel_dir):
    ci_context()
    # Never inherit token, proxy, pip config, PYTHONPATH, HOME or repo credentials.
    home = work / "home"
    home.mkdir()
    environment = {"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
                   "HOME": str(home), "TMPDIR": str(work), "LANG": "C.UTF-8",
                   "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1",
                   "PIP_NO_INPUT": "1", "PYTHONNOUSERSITE": "1"}
    subprocess.run([sys.executable, "-I", "-m", "pip", "--isolated", "wheel",
                    "--use-pep517", "--no-deps", "--no-build-isolation", "--no-index", "--no-cache-dir",
                    "--disable-pip-version-check", "--wheel-dir", str(wheel_dir), "."],
                   cwd=source, env=environment, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=BUILD_TIMEOUT, check=True)


def run(output=None):
    runner_temp = ci_context()  # Before lock read, network, writes or hooks.
    verify_lock()
    output = real_directory(Path(output)) if output is not None else runner_temp / "funpayapi-wheel"
    require(output != runner_temp and output.is_relative_to(runner_temp), "output outside runner temp")
    if output.exists():
        output = real_directory(output)
        require(not any(output.iterdir()), "output must be empty")
    else:
        output.mkdir(mode=0o700)
    output_identity = output.stat()
    with tempfile.TemporaryDirectory(prefix="funpayapi-build-", dir=runner_temp) as temporary:
        work = Path(temporary)
        extracted, wheels = work / "source", work / "wheels"
        extracted.mkdir()
        wheels.mkdir()
        source = extract_source(download_source(), extracted)
        build_wheel(source, work, wheels)
        artifacts = list(wheels.iterdir())
        require(len(artifacts) == 1, "expected exactly one wheel")
        artifact = artifacts[0]
        info = artifact.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_FILE,
                "wheel artifact refused")
        with artifact.open("rb") as stream:
            data = stream.read(MAX_FILE + 1)
        manifest = validate_wheel(artifact.name, data)
        current = real_directory(output).stat()
        require((current.st_dev, current.st_ino) == (output_identity.st_dev, output_identity.st_ino)
                and not any(output.iterdir()), "output changed during build")
        with (output / artifact.name).open("xb") as stream:
            stream.write(data)
        with (output / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", help="existing empty absolute directory under RUNNER_TEMP")
    args = parser.parse_args(argv)
    try:
        manifest = run(args.output)
    except Refused as error:
        print("Refused: " + str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Failed: download, archive or build error (raw logs suppressed)", file=sys.stderr)
        return 1
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
