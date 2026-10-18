"""Opt-in local Windows status polling. No deployment or runtime imports."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import time

REPO = "Vetal12350/FunPayFlow"
OWNER = "Vetal12350"
LABEL = "vps-local"
PREFLIGHT = Path(__file__).resolve().with_name("vps_preflight.py")
# Reviewed preflight source, allowing only checkout line-ending conversion.
PREFLIGHT_SHA256 = "beb0a4f5bad8a3b7fcbcc098cc08c20f46870393d21cad0f80e2ce4fbb38e993"
MAX_NUMBER = 2_147_483_647
FAILURES = {"local", "github", "ssh", "timeout", "schema", "source"}
SERVICE = {"active", "inactive", "failed", "activating", "deactivating", "unknown"}
FIELDS = {
    "report": {"funpayflow_vps_preflight_v1"},
    "service": SERVICE, "legacy_service": SERVICE,
    "main_runtime_processes": {"none", "one", "multiple", "unknown"},
    "tracked_worktree": {"clean", "dirty", "unknown"},
    "data_directory": {"present", "missing"},
    "env_file": {"present", "missing"},
    "settings_file": {"present", "missing"},
    "sqlite": {"ok", "failed", "missing", "skipped_large", "unknown"},
    "disk_free": {"at_least_512_mb", "under_512_mb", "unknown"},
}


class Failure(Exception):
    def __init__(self, category: str):
        self.category = category if category in FAILURES else "local"
        super().__init__(self.category)


def decode_json(raw: bytes, limit: int, category: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        if len(raw) > limit:
            raise ValueError
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise Failure(category) from None


def windows_path(value: object) -> str:
    if (not isinstance(value, str) or not 4 <= len(value) <= 240
            or any(ord(c) < 32 or c in '"<>|?*%' for c in value)):
        raise Failure("local")
    path = PureWindowsPath(value)
    if (not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":"
            or not path.drive[0].isascii() or not path.drive[0].isalpha()
            or ":" in value[2:] or ".." in path.parts):
        raise Failure("local")
    return str(path)


def validate_config(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"host", "identity"}:
        raise Failure("local")
    try:
        host = ipaddress.IPv4Address(value["host"])
        if not host.is_global or host.is_multicast or str(host) != value["host"]:
            raise ValueError
    except (ValueError, TypeError, ipaddress.AddressValueError):
        raise Failure("local") from None
    return {"host": str(host), "identity": windows_path(value["identity"])}


def local_directory() -> Path:
    root = Path(windows_path(os.environ.get("LOCALAPPDATA"))) / "FunPayFlowVpsStatus"
    # Never store configuration or claims inside the source checkout.
    if root.resolve().is_relative_to(PREFLIGHT.parent.parent.resolve()):
        raise Failure("local")
    return root


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise Failure("local")
    return value


def configure(root: Path) -> None:
    config = validate_config({"host": input("VPS public IPv4: "),
                              "identity": input("Absolute SSH identity path (not key contents): ")})
    root.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never truncate existing local configuration.
    with (root / "config.json").open("x", encoding="utf-8") as stream:
        json.dump(config, stream)
        stream.flush()
        os.fsync(stream.fileno())


def invoke(command: list[str], *, category: str, payload: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(command, input=payload, capture_output=True,
                                shell=False, timeout=45, check=False)
    except subprocess.TimeoutExpired:
        raise Failure("timeout") from None
    except OSError:
        raise Failure(category) from None
    if result.returncode != 0:
        raise Failure(category)
    return result.stdout


def eligible(issue: object) -> bool:
    if not isinstance(issue, dict):
        return False
    number = issue.get("number")
    author = issue.get("author")
    labels = issue.get("labels")
    return (type(number) is int and 1 <= number <= MAX_NUMBER
            and issue.get("title") == "[VPS] status" and issue.get("body") == "status"
            and issue.get("state") == "OPEN"
            and isinstance(author, dict) and author.get("login") == OWNER
            and isinstance(labels, list) and len(labels) <= 100
            and all(isinstance(label, dict) and isinstance(label.get("name"), str)
                    and len(label["name"]) <= 50 for label in labels)
            and any(label["name"] == LABEL for label in labels))


def claim(root: Path, number: int) -> bool:
    directory = root / "processed"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        # Atomic exclusive claim handles concurrent controllers and restarts.
        # Even an incomplete marker prevents another execution.
        with (directory / f"{number}.done").open("xb") as stream:
            stream.write(b"claimed\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return False
    return True


def sanitized_report(raw: bytes) -> str:
    value = decode_json(raw, 4096, "schema")
    if not isinstance(value, dict) or set(value) != set(FIELDS) | {
            "code_commit", "read_only", "deployment_performed"}:
        raise Failure("schema")
    if value["read_only"] is not True or value["deployment_performed"] is not False:
        raise Failure("schema")
    for key, allowed in FIELDS.items():
        if not isinstance(value[key], str) or value[key] not in allowed:
            raise Failure("schema")
    commit = value["code_commit"]
    if not isinstance(commit, str) or not (commit in {"missing", "unknown"} or (
            len(commit) == 40 and all(c in "0123456789abcdef" for c in commit))):
        raise Failure("schema")
    # Construct only from validated enums; do not publish commits, paths or IDs.
    return (f"VPS status: service={value['service']}; legacy={value['legacy_service']}; "
            f"runtime={value['main_runtime_processes']}; worktree={value['tracked_worktree']}; "
            f"sqlite={value['sqlite']}; disk={value['disk_free']}. Read-only check.")


def status(config: dict) -> str:
    config = validate_config(config)
    source = read_bounded(PREFLIGHT, 65536)
    if hashlib.sha256(source.replace(b"\r\n", b"\n")).hexdigest() != PREFLIGHT_SHA256:
        raise Failure("source")
    command = ["ssh.exe", "-F", "NUL", "-T", "-o", "StrictHostKeyChecking=yes",
               "-o", "BatchMode=yes", "-o", "ForwardAgent=no",
               "-o", "ClearAllForwardings=yes", "-o", "ProxyCommand=none",
               "-o", "ProxyJump=none", "-o", "IdentitiesOnly=yes",
               "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
               "-o", "ConnectionAttempts=1", "-o", "ConnectTimeout=10",
               "-i", config["identity"], "-l", "root", config["host"], "python3 -"]
    return sanitized_report(invoke(command, category="ssh", payload=source))


def poll(root: Path, config: dict, post_results: bool) -> None:
    raw = invoke(["gh.exe", "issue", "list", "-R", REPO, "--state", "open",
                  "--author", OWNER, "--label", LABEL, "--limit", "50",
                  "--json", "number,title,body,author,labels,state"], category="github")
    issues = decode_json(raw, 262144, "github")
    if not isinstance(issues, list) or len(issues) > 50:
        raise Failure("github")
    for issue in issues:
        if not eligible(issue) or not claim(root, issue["number"]):
            continue
        try:
            report = status(config)
        except Failure as error:
            report = f"VPS status unavailable ({error.category})."
        except OSError:
            report = "VPS status unavailable (local)."
        print(report)
        if post_results:
            # Failure/UNKNOWN delivery is never retried, nor is the issue closed.
            invoke(["gh.exe", "issue", "comment", str(issue["number"]), "-R", REPO,
                    "--body", report], category="github")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--configure", action="store_true")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--post-results", action="store_true",
                        help="Opt in to sanitized GitHub comments; issues remain open")
    args = parser.parse_args(argv)
    if not (args.configure or args.once or args.watch):
        print("Disabled. Select --configure, --once or --watch explicitly.")
        return 0
    try:
        if os.name != "nt":
            raise Failure("local")
        root = local_directory()
        if args.configure:
            configure(root)
            print("Local configuration saved.")
            return 0
        config = validate_config(decode_json(read_bounded(root / "config.json", 4096),
                                             4096, "local"))
        while True:
            poll(root, config, args.post_results)
            if args.once:
                return 0
            time.sleep(60)
    except KeyboardInterrupt:
        return 0
    except (Failure, OSError, EOFError) as error:
        category = error.category if isinstance(error, Failure) else "local"
        print(f"VPS status unavailable ({category}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
