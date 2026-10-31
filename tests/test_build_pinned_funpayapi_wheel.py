"""Offline fixed-source CI helper regression tests; no downloads or build hooks."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
from unittest.mock import Mock
import warnings
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_pinned_funpayapi_wheel.py"
spec = importlib.util.spec_from_file_location("pinned_funpayapi_builder", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
ROOT = builder.SOURCE_ROOT
FILENAME = "FunPayAPI-1.1.0-py2.py3-none-any.whl"
DIST = "FunPayAPI-1.1.0.dist-info"


@pytest.fixture(autouse=True)
def forbid_external_effects(monkeypatch):
    monkeypatch.setattr(builder.http.client, "HTTPSConnection",
                        Mock(side_effect=AssertionError("real network forbidden")))
    monkeypatch.setattr(builder.subprocess, "run",
                        Mock(side_effect=AssertionError("real build forbidden")))


def tar_bytes(entries=None):
    if entries is None:
        entries = [(ROOT + "/setup.py", b"# synthetic, never executed\n", tarfile.REGTYPE)]
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(data)
            if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                member.linkname = "../../outside"
            archive.addfile(member, io.BytesIO(data))
    return stream.getvalue()


def pin_synthetic(monkeypatch, data):
    monkeypatch.setattr(builder, "SOURCE_SIZE", len(data))
    monkeypatch.setattr(builder, "SOURCE_SHA256", hashlib.sha256(data).hexdigest())


def wheel_bytes(changes=None, extra=()):
    files = {
        "FunPayAPI/__init__.py": b"# synthetic\n",
        DIST + "/METADATA": b"Metadata-Version: 2.1\nName: FunPayAPI\nVersion: 1.1.0\n\n",
        DIST + "/WHEEL": b"Wheel-Version: 1.0\nGenerator: synthetic\nRoot-Is-Purelib: true\nTag: py2-none-any\nTag: py3-none-any\n\n",
        DIST + "/RECORD": b"",
    }
    files.update(changes or {})
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # Intentional duplicate ZIP fixtures.
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in [*files.items(), *extra]:
                if data is not None:
                    if isinstance(name, str) and ("\\" in name or "\x00" in name):
                        # ZipInfo normalizes Windows separators at construction;
                        # preserve the hostile raw ZIP filename for this fixture.
                        raw = zipfile.ZipInfo()
                        raw.filename = raw.orig_filename = name
                        name = raw
                    archive.writestr(name, data)
    return stream.getvalue()


def fake_network(monkeypatch, data, status=200, length=None, encoding="identity"):
    response = Mock(status=status)
    response.getheader.side_effect = lambda key, default=None: {
        "Content-Length": length, "Content-Encoding": encoding}.get(key, default)
    pending = io.BytesIO(data)
    response.read1.side_effect = pending.read
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(builder.http.client, "HTTPSConnection", factory)
    return factory, connection, response


def test_import_is_passive(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(builder.os, "getenv", Mock(side_effect=AssertionError("environment read")))
        patch.setattr(builder.Path, "open", Mock(side_effect=AssertionError("file read")))
        patch.setattr(builder.tempfile, "TemporaryDirectory", Mock(side_effect=AssertionError("temp write")))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


def test_constants_match_checkout_lock():
    builder.verify_lock()


def test_lock_drift_refused(monkeypatch):
    monkeypatch.setattr(builder, "SOURCE_SIZE", 1)
    with pytest.raises(builder.Refused, match="lock source mismatch"):
        builder.verify_lock()


@pytest.mark.parametrize("data", [b"", b"x" * 45588, b"x" * 45587], ids=["empty", "oversized", "tampered"])
def test_source_tamper_before_tar_open(monkeypatch, tmp_path, data):
    opened = Mock(side_effect=AssertionError("must verify before parsing"))
    monkeypatch.setattr(builder.tarfile, "open", opened)
    with pytest.raises(builder.Refused, match="source (size|hash) mismatch"):
        builder.extract_source(data, tmp_path)
    opened.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_download_fixed_request_and_exact_digest(monkeypatch):
    data = tar_bytes()
    pin_synthetic(monkeypatch, data)
    factory, connection, response = fake_network(monkeypatch, data, length=str(len(data)))
    assert builder.download_source() == data
    factory.assert_called_once_with("files.pythonhosted.org", timeout=30)
    connection.request.assert_called_once_with(
        "GET", builder.SOURCE_URL.removeprefix("https://files.pythonhosted.org"),
        headers={"Accept-Encoding": "identity"})
    response.close.assert_called_once()
    connection.close.assert_called_once()


@pytest.mark.parametrize("case", ["redirect", "oversize", "length", "encoding", "tamper", "timeout"])
def test_download_fail_closed_no_retry(monkeypatch, case):
    data = tar_bytes()
    pin_synthetic(monkeypatch, data)
    served = data + b"x" if case == "oversize" else data
    if case == "tamper":
        served = b"x" * len(data)
    factory, connection, _ = fake_network(
        monkeypatch, served, status=302 if case == "redirect" else 200,
        length="9999999" if case == "length" else None,
        encoding="gzip" if case == "encoding" else "identity")
    if case == "timeout":
        monkeypatch.setattr(builder.time, "monotonic", Mock(side_effect=[0, 31]))
    with pytest.raises(builder.Refused):
        builder.download_source()
    factory.assert_called_once()
    connection.request.assert_called_once()
    connection.close.assert_called_once()


@pytest.mark.parametrize("name", ["../evil", "/absolute", "C:/drive", "a\\evil", "a//b",
                                 "a/./b", "a/../b", ROOT + "/.env",
                                 ROOT + "/.aws/credentials", ROOT + "/id_rsa",
                                 ROOT + "/private.pem", ROOT + "/.git/config", "other/setup.py"])
def test_tar_unsafe_paths_no_writes(monkeypatch, tmp_path, name):
    data = tar_bytes([(name, b"x", tarfile.REGTYPE)])
    pin_synthetic(monkeypatch, data)
    with pytest.raises(builder.Refused):
        builder.extract_source(data, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                                 tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_tar_links_devices_refused(monkeypatch, tmp_path, kind):
    data = tar_bytes([(ROOT + "/evil", b"", kind)])
    pin_synthetic(monkeypatch, data)
    with pytest.raises(builder.Refused, match="entry type"):
        builder.extract_source(data, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("names", [[ROOT + "/a", ROOT + "/a"],
                                    [ROOT + "/a", ROOT + "/A"],
                                    [ROOT + "/a/b", ROOT + "/a"]])
def test_tar_duplicates_and_file_ancestors(monkeypatch, tmp_path, names):
    data = tar_bytes([(name, b"x", tarfile.REGTYPE) for name in names])
    pin_synthetic(monkeypatch, data)
    with pytest.raises(builder.Refused):
        builder.extract_source(data, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("limit", ["MAX_FILE", "MAX_EXPANDED", "MAX_ENTRIES"])
def test_tar_resource_bounds(monkeypatch, tmp_path, limit):
    data = tar_bytes()
    pin_synthetic(monkeypatch, data)
    monkeypatch.setattr(builder, limit, 0)
    with pytest.raises(builder.Refused):
        builder.extract_source(data, tmp_path)


def test_safe_extraction_never_extractall(monkeypatch, tmp_path):
    data = tar_bytes()
    pin_synthetic(monkeypatch, data)
    monkeypatch.setattr(tarfile.TarFile, "extractall", Mock(side_effect=AssertionError("unsafe extract")))
    source = builder.extract_source(data, tmp_path)
    assert source == tmp_path / ROOT
    assert (source / "setup.py").read_bytes() == b"# synthetic, never executed\n"


def test_custom_backend_refused(monkeypatch, tmp_path):
    data = tar_bytes([(ROOT + "/setup.py", b"", tarfile.REGTYPE),
                      (ROOT + "/pyproject.toml", b'[build-system]\nbuild-backend="evil"', tarfile.REGTYPE)])
    pin_synthetic(monkeypatch, data)
    with pytest.raises(builder.Refused, match="backend refused"):
        builder.extract_source(data, tmp_path)


def test_wheel_validates_tags_and_hash():
    data = wheel_bytes()
    manifest = builder.validate_wheel(FILENAME, data)
    assert manifest == {"package": "FunPayAPI", "version": "1.1.0", "filename": FILENAME,
                        "source_sha256": builder.SOURCE_SHA256,
                        "wheel_sha256": hashlib.sha256(data).hexdigest(),
                        "wheel_tags": ["py2-none-any", "py3-none-any"]}


@pytest.mark.parametrize("file,content", [
    ("METADATA", b"Name: Other\nVersion: 1.1.0\n"),
    ("METADATA", b"Name: FunPayAPI\nVersion: 2.0\n"),
    ("METADATA", b"Name: FunPayAPI\nName: FunPayAPI\nVersion: 1.1.0\n"),
    ("METADATA", b"Name: FunPayAPI\n"),
    ("METADATA", None), ("RECORD", None), ("WHEEL", None),
    ("WHEEL", b"Wheel-Version: 2.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"),
    ("WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: maybe\nTag: py3-none-any\n"),
    ("WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\n"),
    ("WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"),
    ("WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3\n"),
])
def test_wheel_invalid_metadata_and_tags(file, content):
    with pytest.raises(builder.Refused):
        builder.validate_wheel(FILENAME, wheel_bytes({DIST + "/" + file: content}))


@pytest.mark.parametrize("name", ["../escape", "/escape", "C:/escape", "a\\escape", ".env",
                                 "a/../escape", "a//b", "a\x00evil", "a/credentials", "a/.ssh/id_rsa",
                                 "Other-1.1.0.dist-info/METADATA", "nested/evil.dist-info/METADATA",
                                 "FunPayAPI/native.so", "FunPayAPI-1.1.0.data/platlib/a.py"])
def test_wheel_unsafe_members(name):
    with pytest.raises(builder.Refused):
        builder.validate_wheel(FILENAME, wheel_bytes(extra=[(name, b"x")]))


@pytest.mark.parametrize("extra", [[("FunPayAPI/__init__.py", b"x")],
                                   [("funpayapi/__INIT__.py", b"x")],
                                   [("FunPayAPI", b"x")]])
def test_wheel_duplicate_and_conflicting_members(extra):
    with pytest.raises(builder.Refused):
        builder.validate_wheel(FILENAME, wheel_bytes(extra=extra))


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFIFO])
def test_wheel_special_types(mode):
    member = zipfile.ZipInfo("FunPayAPI/evil")
    member.create_system = 3
    member.external_attr = (mode | 0o777) << 16
    with pytest.raises(builder.Refused, match="entry type"):
        builder.validate_wheel(FILENAME, wheel_bytes(extra=[(member, b"target")]))


@pytest.mark.parametrize("limit", ["MAX_FILE", "MAX_EXPANDED", "MAX_ENTRIES"])
def test_wheel_resource_bounds(monkeypatch, limit):
    monkeypatch.setattr(builder, limit, 1)
    with pytest.raises(builder.Refused):
        builder.validate_wheel(FILENAME, wheel_bytes())


@pytest.mark.parametrize("filename", ["Other-1.1.0-py3-none-any.whl",
                                     "FunPayAPI-2.0-py3-none-any.whl",
                                     "FunPayAPI-1.1.0-py3.whl", "../" + FILENAME,
                                     "FunPayAPI-1.1.0-py3-none-any.zip"])
def test_wheel_filename_refused(filename):
    with pytest.raises(builder.Refused):
        builder.validate_wheel(filename, wheel_bytes())


@pytest.mark.parametrize("platform,uid,hosted", [("linux", 0, True), ("win32", 1000, True),
                                               ("linux", 1000, False)])
def test_ci_guards_before_io(monkeypatch, platform, uid, hosted):
    monkeypatch.setattr(builder.sys, "platform", platform)
    monkeypatch.setattr(builder.os, "geteuid", lambda: uid, raising=False)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted" if hosted else "self-hosted")
    lock = Mock(side_effect=AssertionError("must guard before I/O"))
    monkeypatch.setattr(builder, "verify_lock", lock)
    with pytest.raises(builder.Refused):
        builder.run()
    lock.assert_not_called()


def prepare_run(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "ci_context", lambda: tmp_path.resolve())
    monkeypatch.setattr(builder, "verify_lock", Mock())
    data = tar_bytes()
    pin_synthetic(monkeypatch, data)
    download = Mock(return_value=data)
    monkeypatch.setattr(builder, "download_source", download)
    def fake_build(command, **kwargs):
        assert command[:5] == [builder.sys.executable, "-I", "-m", "pip", "--isolated"]
        assert all(flag in command for flag in ("--use-pep517", "--no-deps", "--no-build-isolation", "--no-index"))
        assert kwargs["env"].keys() == {"PATH", "HOME", "TMPDIR", "LANG", "PIP_CONFIG_FILE",
                                         "PIP_NO_INDEX", "PIP_NO_INPUT", "PYTHONNOUSERSITE"}
        assert kwargs["timeout"] == builder.BUILD_TIMEOUT
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["check"] is True
        wheel_dir = Path(command[command.index("--wheel-dir") + 1])
        assert wheel_dir.is_relative_to(tmp_path)
        assert kwargs["cwd"].is_relative_to(tmp_path)
        (wheel_dir / FILENAME).write_bytes(wheel_bytes())
    build = Mock(side_effect=fake_build)
    monkeypatch.setattr(builder.subprocess, "run", build)
    return download, build


def test_success_outputs_only_wheel_and_manifest(monkeypatch, tmp_path):
    _, build = prepare_run(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-do-not-forward")
    monkeypatch.setenv("PYTHONPATH", "synthetic-do-not-forward")
    output = tmp_path / "artifacts"
    output.mkdir()
    manifest = builder.run(str(output.resolve()))
    build.assert_called_once()
    assert sorted(p.name for p in output.iterdir()) == [FILENAME, "manifest.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["artifacts"]
    assert json.loads((output / "manifest.json").read_text()) == manifest
    assert manifest["wheel_sha256"] == hashlib.sha256((output / FILENAME).read_bytes()).hexdigest()


@pytest.mark.parametrize("case", ["outside", "nonempty", "relative", "root", "missing", "symlink"])
def test_output_write_bounds_before_download(monkeypatch, tmp_path, case):
    download, build = prepare_run(monkeypatch, tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    if case == "outside":
        output = tmp_path.parent
    elif case == "nonempty":
        (output / "sentinel").write_bytes(b"preserve")
    elif case == "relative":
        output = Path("relative")
    elif case == "root":
        output = tmp_path
    elif case == "missing":
        output = tmp_path / "missing"
    elif case == "symlink":
        original = builder.Path.is_symlink
        monkeypatch.setattr(builder.Path, "is_symlink", lambda p: p == output or original(p))
    with pytest.raises(builder.Refused):
        builder.run(str(output))
    download.assert_not_called()
    build.assert_not_called()
    sentinel = tmp_path / "output/sentinel"
    if case == "nonempty":
        assert sentinel.read_bytes() == b"preserve"


@pytest.mark.parametrize("case", ["none", "multiple", "invalid", "directory", "hardlink", "failure", "timeout", "output_changed"])
def test_failed_build_never_publishes(monkeypatch, tmp_path, case):
    prepare_run(monkeypatch, tmp_path)
    def fake_build(command, **kwargs):
        if case in {"failure", "timeout"}:
            if case == "timeout":
                raise subprocess.TimeoutExpired(command, 180)
            raise subprocess.CalledProcessError(1, command, stderr="private raw logs")
        wheel_dir = Path(command[command.index("--wheel-dir") + 1])
        if case == "none":
            return
        if case == "directory":
            (wheel_dir / FILENAME).mkdir()
            return
        artifact = wheel_dir / FILENAME
        artifact.write_bytes(b"invalid" if case == "invalid" else wheel_bytes())
        if case == "multiple":
            (wheel_dir / "second.whl").write_bytes(b"x")
        if case == "hardlink":
            os.link(artifact, wheel_dir.parent / "linked-wheel")
        if case == "output_changed":
            (tmp_path / "funpayapi-wheel/manifest.json").write_text("sentinel")
    monkeypatch.setattr(builder.subprocess, "run", Mock(side_effect=fake_build))
    assert builder.main([]) == 1
    assert not (tmp_path / "funpayapi-wheel" / FILENAME).exists()
    if case == "output_changed":
        assert (tmp_path / "funpayapi-wheel/manifest.json").read_text() == "sentinel"
    else:
        assert list((tmp_path / "funpayapi-wheel").iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["funpayapi-wheel"]


def test_cli_bounded_and_errors_sanitized(monkeypatch, capsys):
    with pytest.raises(SystemExit) as error:
        builder.main(["--url", "https://example.invalid"])
    assert error.value.code == 2
    with pytest.raises(SystemExit):
        builder.main(["one", "two"])
    capsys.readouterr()
    monkeypatch.setattr(builder, "run", Mock(side_effect=RuntimeError("private raw logs")))
    assert builder.main([]) == 1
    captured = capsys.readouterr()
    assert "private" not in captured.err
    assert captured.out == ""
