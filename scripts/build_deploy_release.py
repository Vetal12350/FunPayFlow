"""Build the VPS release archive consumed by scripts/deploy_agent.py (CI only).

Runs in the credential-free GitHub Actions `verify` job, never on the server:

    python scripts/build_deploy_release.py --sha <40 hex> --out <empty dir>

1. `uv export --frozen --no-dev` -> requirements with sha256 hashes from uv.lock;
2. `pip wheel --require-hashes` -> wheels for every locked dependency (sdists such
   as FunPayAPI are hash-verified first, then built here, not on production);
3. `uv build --wheel` -> the funpayflow wheel of the checked-out commit;
4. manifest.json {sha, wheels: {path: sha256}} + deterministic release.tar;
5. prints `DIGEST=<sha256 of release.tar>`.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"[0-9a-f]{40}")
WHEEL_NAME = re.compile(r"[A-Za-z0-9_.+-]{1,200}\.whl")


def assemble(sha: str, wheel_dir: Path, archive: Path) -> str:
    """Write manifest + wheels into a deterministic tar; return its sha256."""
    if not SHA_RE.fullmatch(sha):
        raise ValueError("sha must be 40 lowercase hex characters")
    wheels = sorted(p for p in wheel_dir.iterdir() if p.is_file())
    if not wheels or any(not WHEEL_NAME.fullmatch(p.name) for p in wheels):
        raise ValueError("wheel directory must contain only .whl files")
    if not any(p.name.startswith("funpayflow-") for p in wheels):
        raise ValueError("funpayflow wheel missing")
    entries = {f"wheels/{p.name}": p.read_bytes() for p in wheels}
    manifest = {"sha": sha, "wheels": {name: hashlib.sha256(data).hexdigest()
                                       for name, data in entries.items()}}
    files = {"manifest.json": json.dumps(manifest, sort_keys=True, indent=1).encode(),
             **entries}
    with tarfile.open(archive, "w:", format=tarfile.PAX_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def collect_wheels(source: Path, wheels: Path) -> None:
    """Move exactly the *.whl files of `source` into `wheels`; at least one."""
    built = sorted(source.glob("*.whl"))
    if not built:
        raise ValueError("project wheel was not built")
    for wheel in built:
        wheel.replace(wheels / wheel.name)


def run(*argv: str) -> None:
    subprocess.run(argv, cwd=ROOT, check=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if not SHA_RE.fullmatch(args.sha):
        parser.error("--sha must be 40 lowercase hex characters")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()
    if head != args.sha:
        parser.error("checkout HEAD does not match --sha")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    wheels = out / "wheels"
    req = out / "requirements.txt"
    run("uv", "export", "--frozen", "--no-dev", "--no-emit-project",
        "--format", "requirements-txt", "--output-file", str(req))
    run(sys.executable, "-m", "pip", "wheel", "--require-hashes", "--no-deps",
        "--disable-pip-version-check", "--wheel-dir", str(wheels), "-r", str(req))
    # uv build also writes a .gitignore into its out-dir: build the project
    # wheel separately and move only the .whl into the release wheel set.
    project = out / "project"
    run("uv", "build", "--wheel", "--out-dir", str(project))
    collect_wheels(project, wheels)
    digest = assemble(args.sha, wheels, out / "release.tar")
    print(f"DIGEST={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
