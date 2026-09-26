"""Build and import the installed wheel outside the checkout, without network."""

import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MODULES = {f"funpayflow/{name}" for name in (
    "__init__.py", "main.py", "telegram.py", "funpay.py", "state.py",
    "feature_registry.py", "runtime_control.py", "runtime_events.py",
    "logger.py", "import_funpay_sales.py", "runtime_paths.py",
    "console_ui.py", "setup_config.py", "render_service.py",
)}


def _run(*args: str, cwd: Path, env: dict[str, str]) -> None:
    result = subprocess.run(args, cwd=cwd, env=env, text=True,
                            encoding="utf-8", capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed:\n{result.stderr[-3000:]}")


def _private_artifact(name: str) -> bool:
    parts = tuple(part.lower() for part in PurePosixPath(name).parts[1:])
    if not parts:
        return False
    filename = parts[-1]
    return (any(part in {"logs", "imports", "exports", "private", "secrets"}
                for part in parts[:-1])
            or filename in {".env", ".install-data-dir"}
            or (filename.startswith(".env.") and filename != ".env.example")
            or filename.startswith("bot_settings")
            or (filename.startswith("state") and ".sqlite3" in filename)
            or filename.endswith((".zip", ".csv", ".bak", ".backup", ".log")))


def main() -> None:
    environment = dict(os.environ)
    environment["UV_OFFLINE"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="funpay-package-smoke-") as folder:
        temporary = Path(folder).resolve()
        if temporary.parent != Path(tempfile.gettempdir()).resolve():
            raise RuntimeError("Unsafe temporary build directory.")
        source = temporary / "isolated source"
        package = source / "src" / "funpayflow"
        package.parent.mkdir(parents=True)
        shutil.copytree(ROOT / "src" / "funpayflow", package,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
            shutil.copyfile(ROOT / name, source / name)
        dist = temporary / "dist"
        _run("uv", "build", "--offline", "--out-dir", str(dist),
             cwd=source, env=environment)
        wheels = list(dist.glob("*.whl"))
        archives = list(dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(archives) != 1:
            raise RuntimeError("Expected one wheel and one source archive.")
        with zipfile.ZipFile(wheels[0]) as wheel:
            if not MODULES.issubset(wheel.namelist()):
                raise RuntimeError("Wheel is missing runtime modules.")
        with tarfile.open(archives[0], "r:gz") as archive:
            if any(_private_artifact(member.name) for member in archive.getmembers()):
                raise RuntimeError("Private artifact found in source archive.")

        virtualenv = temporary / "venv"
        _run("uv", "venv", "--offline", "--python", "3.13", str(virtualenv),
             cwd=ROOT, env=environment)
        environment["UV_PROJECT_ENVIRONMENT"] = str(virtualenv)
        _run("uv", "sync", "--offline", "--frozen", "--no-install-project",
             "--no-dev", cwd=source, env=environment)
        python = (virtualenv / "Scripts" / "python.exe" if os.name == "nt"
                  else virtualenv / "bin" / "python")
        _run("uv", "pip", "install", "--offline", "--no-deps", "--python",
             str(python), str(wheels[0]), cwd=ROOT, env=environment)
        smoke = (
            "from importlib.metadata import distribution\n"
            "from pathlib import Path\n"
            "import sys\n"
            "root = Path(sys.argv[1]).resolve()\n"
            "assert root not in [Path(p).resolve() for p in sys.path if p]\n"
            "entry = next(e for e in distribution('funpayflow').entry_points "
            "if e.name == 'funpayflow')\n"
            "assert entry.value == 'funpayflow.main:run' and callable(entry.load())\n"
            "from funpayflow import telegram, state, feature_registry, runtime_control, "
            "runtime_events, import_funpay_sales, runtime_paths, "
            "console_ui, setup_config\n"
            "assert runtime_paths.CODE_DIR == Path.cwd().resolve()\n"
            "print('isolated wheel import: PASS')\n"
        )
        _run(str(python), "-I", "-c", smoke, str(ROOT),
             cwd=temporary, env=environment)
    print("offline packaging smoke: PASS")


if __name__ == "__main__":
    main()
