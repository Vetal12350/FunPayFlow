"""Build and audit the public, source-based release ZIP without private data."""

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "main.py", "telegram.py", "funpay.py", "state.py", "feature_registry.py",
    "runtime_control.py", "runtime_events.py", "runtime_paths.py", "logger.py",
    "import_funpay_sales.py", "setup_config.py", "console_ui.py", "render_service.py",
    "pyproject.toml", "uv.lock", ".env.example", "README.md", "README.ru.md", "CHANGELOG.md",
    "SECURITY.md", "LICENSE", "Setup.bat", "Start.bat", "ResolveDataDir.bat",
    "install.sh", "systemd/funpayflow.service.in",
)
OPTIONAL_FILES: tuple[str, ...] = ()


def archive_name(source: str) -> str:
    """Map the flat source checkout to the public release layout."""
    if source in {"Setup.bat", "Start.bat", "README.md",
                  "README.ru.md", "CHANGELOG.md", "SECURITY.md", "LICENSE"}:
        return source
    if source == "install.sh" or source.startswith("systemd/"):
        return "linux/" + source
    return "app/" + source


def archive_files(files: tuple[str, ...]) -> dict[str, str]:
    result = {archive_name(name): name for name in files}
    # Hatchling needs the project readme inside the app's build root.
    result["app/README.md"] = "README.md"
    return result
MAX_MEMBER_BYTES = 10_000_000
MAX_ARCHIVE_BYTES = 50_000_000
_TOKEN = re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b")
_WINDOWS_HOME = re.compile(rb"[A-Za-z]:\\Users\\[^\\\s]+", re.I)
_PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_IPV4 = re.compile(rb"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_ASSIGNMENT = re.compile(
    rb"(?m)^\s*(FUNPAY_GOLDEN_KEY|BOT_TOKEN|PHPSESSID)\s*=\s*(.+)$"
)
_SESSION_LITERAL = re.compile(
    rb"(?i)\b(?:PHPSESSID|csrf_token)\s*[:=]\s*[\"'][A-Za-z0-9_-]{16,}"
)


class ReleaseError(RuntimeError):
    """Release cannot be built or audited safely."""


def project_version(root: Path = ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    version = project["version"]
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ReleaseError("Invalid project version.")
    with (root / "uv.lock").open("rb") as stream:
        locked = [item for item in tomllib.load(stream)["package"]
                  if item.get("name") == project["name"]]
    if len(locked) != 1 or locked[0].get("version") != version:
        raise ReleaseError("Project version and uv.lock disagree.")
    return version


def release_basename(version: str) -> str:
    if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ReleaseError("Invalid release version.")
    return f"FunPayFlow-v{version}"


def verify_tag(tag: str, root: Path = ROOT) -> None:
    if tag != f"v{project_version(root)}":
        raise ReleaseError("Git tag does not match project version.")


def release_files(root: Path = ROOT, *, require_license: bool = False) -> tuple[str, ...]:
    result = list(REQUIRED_FILES)
    for name in result:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ReleaseError("A required release file is missing or unsafe.")
    return tuple(sorted(result))


def _private_name(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if (not parts or name.startswith("/") or "\\" in name
            or any(part in {"", ".", ".."} for part in parts)):
        return True
    lowered = [part.lower() for part in parts]
    filename = lowered[-1]
    if any(part in {".git", ".github", ".venv", "venv", "logs", "tests",
                    "__pycache__", ".pytest_cache", "imports", "exports", "private"}
           for part in lowered):
        return True
    return (filename in {".env", "bot_settings.json", "bot.lock", "stats_log.json",
                         "installer_language.txt",
                         ".install-data-dir"}
            or (filename.startswith(".env.") and filename != ".env.example")
            or "sqlite3" in filename or filename.endswith((".zip", ".csv", ".bak",
                                                              ".backup", ".log", ".tmp")))


def _audit_text(name: str, content: bytes, private_markers: tuple[str, ...]) -> None:
    if _TOKEN.search(content) or _PRIVATE_KEY.search(content) or _SESSION_LITERAL.search(content):
        raise ReleaseError("Credential-shaped value found in release text.")
    if _WINDOWS_HOME.search(content) or b"/root/SecureBot" in content:
        raise ReleaseError("Owner-specific path found in release text.")
    lowered = content.lower()
    for marker in private_markers:
        if marker and marker.encode("utf-8").lower() in lowered:
            raise ReleaseError("Owner-specific marker found in release text.")
    for match in _ASSIGNMENT.finditer(content):
        value = match.group(2).strip().strip(b"\"'")
        if (value and not value.startswith(b"<")
                and not value.lower().startswith((b"os.getenv", b"none", b"fake",
                                                  b"dummy", b"example", b"synthetic"))):
            raise ReleaseError("Credential assignment found in release text.")
    for match in _IPV4.finditer(content):
        ip = match.group()
        if (ip not in {b"120.0.0.0", b"127.0.0.1", b"0.0.0.0"}
                and not ip.startswith((b"192.0.2.", b"198.51.100.", b"203.0.113."))):
            raise ReleaseError("IP address found in release text.")


def audit_archive(archive: Path, *, version: str, private_markers: tuple[str, ...] = ()) -> None:
    """Inspect every member; report categories only, never matched values."""
    top = release_basename(version)
    expected = set(archive_files(REQUIRED_FILES))
    seen: set[str] = set()
    total = 0
    metadata: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                prefix = top + "/"
                if not info.filename.startswith(prefix) or info.is_dir():
                    raise ReleaseError("Unexpected archive member.")
                name = info.filename[len(prefix):]
                if (name in seen or name not in expected | {archive_name(n) for n in OPTIONAL_FILES}
                        or _private_name(name)):
                    raise ReleaseError("Unexpected or private archive member.")
                seen.add(name)
                total += info.file_size
                if (info.file_size > MAX_MEMBER_BYTES or total > MAX_ARCHIVE_BYTES
                        or (info.external_attr >> 16) & 0o170000 != 0o100000):
                    raise ReleaseError("Unsafe archive member type or size.")
                with bundle.open(info) as stream:
                    content = stream.read(MAX_MEMBER_BYTES + 1)
                if len(content) != info.file_size:
                    raise ReleaseError("Invalid archive member size.")
                _audit_text(name, content, private_markers)
                if name in {"app/pyproject.toml", "app/uv.lock"}:
                    metadata[name] = content
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, ReleaseError):
            raise
        raise ReleaseError("Release archive cannot be read.") from None
    if not expected.issubset(seen):
        raise ReleaseError("Release archive is missing required files.")
    try:
        project = tomllib.loads(metadata["app/pyproject.toml"].decode("utf-8"))["project"]
        locked = [item for item in tomllib.loads(metadata["app/uv.lock"].decode("utf-8"))["package"]
                  if item.get("name") == project["name"]]
        if (project["version"] != version or len(locked) != 1
                or locked[0].get("version") != version):
            raise ReleaseError("Archive version metadata disagrees.")
    except (KeyError, UnicodeError, tomllib.TOMLDecodeError):
        raise ReleaseError("Archive version metadata is invalid.") from None


def _release_bytes(name: str, path: Path) -> bytes:
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return raw.replace(b"\n", b"\r\n") if name.lower().endswith(".bat") else raw


def build_release(root: Path = ROOT, output_dir: Path | None = None, *,
                  private_markers: tuple[str, ...] = (),
                  require_license: bool = False) -> tuple[Path, Path]:
    version = project_version(root)
    files = release_files(root, require_license=require_license)
    top = release_basename(version)
    out = Path(output_dir) if output_dir is not None else root / "dist"
    out.mkdir(parents=True, exist_ok=True)
    archive = out / f"{top}.zip"
    checksum = out / f"{top}.zip.sha256"
    temporary = None
    checksum_temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".release-", suffix=".tmp",
                                         dir=out, delete=False) as scratch:
            temporary = Path(scratch.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as bundle:
            for destination, source in sorted(archive_files(files).items()):
                info = zipfile.ZipInfo(f"{top}/{destination}", date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o100755 if source == "install.sh" else 0o100644) << 16
                bundle.writestr(info, _release_bytes(source, root / source), compress_type=zipfile.ZIP_DEFLATED,
                                compresslevel=9)
        audit_archive(temporary, version=version, private_markers=private_markers)
        os.replace(temporary, archive)
        temporary = None
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", newline="\n",
                                         prefix=".release-sha-", suffix=".tmp",
                                         dir=out, delete=False) as scratch:
            checksum_temporary = Path(scratch.name)
            scratch.write(f"{digest}  {archive.name}\n")
        os.replace(checksum_temporary, checksum)
        checksum_temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if checksum_temporary is not None:
            checksum_temporary.unlink(missing_ok=True)
    return archive, checksum


def verify_checksum(archive: Path, checksum: Path) -> None:
    expected = checksum.read_text(encoding="ascii").strip().split("  ", 1)
    if len(expected) != 2 or expected[1] != archive.name:
        raise ReleaseError("Release checksum manifest is invalid.")
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if expected[0] != actual:
        raise ReleaseError("Release checksum does not match ZIP.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline public release builder and audit")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--output-dir", type=Path)
    build.add_argument("--private-marker", action="append", default=[])
    build.add_argument("--require-license", action="store_true")
    audit = sub.add_parser("audit")
    audit.add_argument("archive", type=Path)
    audit.add_argument("--private-marker", action="append", default=[])
    check = sub.add_parser("verify-tag")
    check.add_argument("tag")
    args = parser.parse_args()
    try:
        if args.command == "build":
            archive, checksum = build_release(output_dir=args.output_dir,
                private_markers=tuple(args.private_marker), require_license=args.require_license)
            verify_checksum(archive, checksum)
            print(f"Built and audited: {archive.name} ({archive.stat().st_size} bytes)")
            print(f"SHA-256: {checksum.name}")
        elif args.command == "audit":
            audit_archive(args.archive, version=project_version(),
                          private_markers=tuple(args.private_marker))
            verify_checksum(args.archive, args.archive.with_name(args.archive.name + ".sha256"))
            print("Archive and checksum audit: PASS")
        else:
            verify_tag(args.tag)
            print("Tag/version match: PASS")
    except (OSError, KeyError, ReleaseError):
        print("Release preflight failed. No private content was printed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
