"""Read-only GitHub/worker monitor. Importing this module creates no window or process."""
from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser

_spec = importlib.util.spec_from_file_location("monitor_worker", Path(__file__).with_name("codex_issue_worker.py"))
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)
_vps_spec = importlib.util.spec_from_file_location("vps_update_status", Path(__file__).with_name("vps_update_status.py"))
vps = importlib.util.module_from_spec(_vps_spec)
_vps_spec.loader.exec_module(vps)
WINDOWS = os.name == "nt"
REPO = worker.REPO
BASE = f"https://github.com/{REPO}"
REQUIRED = {
    "offline-tests (ubuntu-latest)": ".github/workflows/ci.yml",
    "offline-tests (windows-latest)": ".github/workflows/ci.yml",
    "Owner journeys and regression QA": ".github/workflows/product-qa.yml",
    "Runtime and order reliability QA": ".github/workflows/product-qa.yml",
}
FAILURES = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
ISSUES = ["gh", "issue", "list", "-R", REPO, "--state", "open", "--label", worker.LABEL,
          "--author", worker.OWNER, "--limit", "100", "--json", "number,title,author,labels"]
PRS = ["gh", "pr", "list", "-R", REPO, "--state", "all", "--limit", "100", "--json",
       "number,url,headRefName,headRefOid,state,mergedAt"]


class Unavailable(Exception):
    pass


def number(value):
    return type(value) is int and 0 < value < 2**53


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value < 4102444800


def safe_link(value):
    return isinstance(value, str) and re.fullmatch(
        re.escape(BASE) + r"/(issues|pull)/[1-9][0-9]*", value) is not None


def gh_json(argv):
    """Only these read-only calls can reach gh; untrusted strings never become options."""
    api = (len(argv) == 5 and argv[:3] == ["gh", "api", "--method=GET"]
           and argv[3] == "--" and re.fullmatch(
               re.escape(f"repos/{REPO}/actions/") +
               r"(runs\?head_sha=[0-9a-f]{40}&event=pull_request&per_page=100|"
               r"runs/[1-9][0-9]*/attempts/[1-9][0-9]*/jobs\?per_page=100)", argv[4]))
    if argv != ISSUES and argv != PRS and not api:
        raise Unavailable("Call not allowlisted")
    try:
        result = subprocess.run(argv, shell=False, timeout=20, check=False,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or len(result.stdout) > 2_000_000:
            raise Unavailable("GitHub unavailable")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise Unavailable("GitHub unavailable") from None


def api(endpoint):
    return gh_json(["gh", "api", "--method=GET", "--", f"repos/{REPO}/actions/{endpoint}"])


def checks_for(pr):
    sha = pr.get("headRefOid")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Unavailable("Unknown PR head")
    response = api(f"runs?head_sha={sha}&event=pull_request&per_page=100")
    runs = bounded_page(response, "workflow_runs")
    selected = {}
    for run in runs:
        if not isinstance(run, dict):
            raise Unavailable("Malformed run")
        path = run.get("path")
        if (path not in set(REQUIRED.values()) or run.get("head_sha") != sha
                or run.get("event") != "pull_request"):
            continue
        associations = run.get("pull_requests")
        if not isinstance(associations, list):
            raise Unavailable("Workflow PR association unavailable")
        if associations:
            # A nonempty association list must explicitly name this PR.
            if not any(isinstance(p, dict) and p.get("number") == pr["number"]
                       for p in associations):
                continue
        else:
            # GitHub may clear pull_requests after a PR is merged. In that
            # narrow case independently bind the exact SHA, branch and repo.
            # SHA, event and workflow path were already validated above.
            if (run.get("head_branch") != pr.get("headRefName")
                    or not isinstance(run.get("repository"), dict)
                    or run["repository"].get("full_name") != REPO
                    or not isinstance(run.get("head_repository"), dict)
                    or run["head_repository"].get("full_name") != REPO):
                continue
        if not number(run.get("id")) or not number(run.get("run_attempt")):
            raise Unavailable("Malformed run identity")
        if path not in selected or (run["id"], run["run_attempt"]) > (
                selected[path]["id"], selected[path]["run_attempt"]):
            selected[path] = run
    statuses = dict.fromkeys(REQUIRED, "waiting")
    for path, run in selected.items():
        jobs = bounded_page(api(f"runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"), "jobs")
        for name, required_path in REQUIRED.items():
            if path != required_path:
                continue
            matches = [j for j in jobs if isinstance(j, dict) and j.get("name") == name]
            if len(matches) != 1:
                continue
            job = matches[0]
            if job.get("status") == "completed":
                conclusion = job.get("conclusion")
                statuses[name] = conclusion if conclusion in FAILURES | {"success", "skipped", "neutral"} else "unknown"
            elif job.get("status") in {"queued", "in_progress", "waiting", "pending"}:
                statuses[name] = job["status"]
    return statuses


def bounded_page(response, key):
    if (not isinstance(response, dict) or type(response.get("total_count")) is not int
            or not 0 <= response["total_count"] <= 100
            or not isinstance(response.get(key), list)
            or len(response[key]) != response["total_count"]):
        raise Unavailable("Incomplete GitHub page")
    return response[key]


def read_live(root):
    """Read only the public status protocol, never state.json, reports, logs or bot data."""
    try:
        with (root / "live_status.json").open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != worker.STATUS_VERSION:
            raise ValueError
        clean = {"phase": data["phase"], "heartbeat": data["heartbeat"],
                 "phase_started": data["phase_started"], "issue": data.get("issue"),
                 "events": [], "jobs": {}}
        if (clean["phase"] not in worker.PHASES or not timestamp(clean["heartbeat"])
                or not timestamp(clean["phase_started"]) or
                (clean["issue"] is not None and not number(clean["issue"]))):
            raise ValueError
        for key, job in list(data.get("jobs", {}).items())[-100:]:
            if not re.fullmatch(r"[1-9][0-9]{0,14}", key) or not isinstance(job, dict):
                continue
            if job.get("phase") in worker.PHASES:
                clean["jobs"][int(key)] = {"phase": job["phase"]}
                if worker.pr_url(job.get("pr_url")):
                    clean["jobs"][int(key)]["pr_url"] = job["pr_url"]
        for event in data.get("events", [])[-30:]:
            if (isinstance(event, dict) and event.get("phase") in worker.PHASES and
                    timestamp(event.get("timestamp")) and
                    (event.get("issue") is None or number(event["issue"]))):
                clean["events"].append({k: event[k] for k in ("phase", "issue", "timestamp")})
        return clean
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def health(live, now):
    if live is None:
        return "unknown (no compatible status)"
    age = now - live["heartbeat"]
    if not 0 <= age <= 30:
        return "stale (worker health unknown)"
    if live["phase"] in {"stopped", "unavailable"}:
        return live["phase"]
    return "heartbeat observed"


PHASE_TEXT = {
    "waiting": "Ожидание задач", "Queued": "В очереди GitHub",
    "claimed": "Задача принята", "clone": "Подготовка копии проекта",
    "dependencies": "Подготовка зависимостей", "codex_running": "Codex работает",
    "branch_pr": "Создание ветки и PR", "pr_created": "PR создан",
    "blocked": "Worker заблокирован", "unavailable": "Worker недоступен",
    "stopped": "Worker остановлен",
}
CHECK_LABELS = {
    "offline-tests (windows-latest)": "Windows CI",
    "offline-tests (ubuntu-latest)": "Linux CI",
    "Owner journeys and regression QA": "Product QA",
    "Runtime and order reliability QA": "Core QA",
}
CHECK_TEXT = {
    "success": "✓ Успешно", "failure": "Ошибка", "cancelled": "Отменено",
    "timed_out": "Время истекло", "action_required": "Нужно действие",
    "startup_failure": "Ошибка запуска", "stale": "Устарело",
    "skipped": "Пропущено", "neutral": "Без результата", "waiting": "Ожидание",
    "queued": "В очереди", "pending": "Ожидание", "in_progress": "Выполняется",
}


def worker_active(live, now):
    return health(live, now) == "heartbeat observed"


def worker_status_text(live, online, now):
    condition = health(live, now)
    label = {"heartbeat observed": "активен, heartbeat свежий", "stopped": "остановлен",
             "unavailable": "недоступен"}.get(condition, "состояние неизвестно")
    if condition == "heartbeat observed" and live["phase"] == "waiting":
        label = "ожидает задачи, heartbeat свежий"
    value = f"Worker: {label} | GitHub: {'обновлён' if online else 'недоступен; данные ранее'}"
    if live:
        age = now - live["heartbeat"]
        value += f" | Heartbeat: {int(age)} с назад" if age >= 0 else " | Heartbeat: время в будущем"
    return value


def phase_status_text(live, now, rows=None):
    if not live:
        return "Последний этап: неизвестно"
    row = (rows or {}).get(live.get("issue"), {})
    if row.get("pr_state") in {"MERGED", "CLOSED"}:
        return f"Задача #{row['issue']}: {stage(row)} · этап Worker сохранён в истории"
    phase = PHASE_TEXT.get(live["phase"], "Неизвестно")
    if worker_active(live, now) and live["phase"] not in {"pr_created", "blocked"}:
        return f"Наблюдаемый этап: {phase} · {max(0, int(now - live['phase_started']))} с"
    return f"Последний записанный этап: {phase} (текущее выполнение не подтверждено)"


def pr_state(pr):
    """Only consistent GitHub lifecycle fields count; no inference from green checks."""
    state, merged = pr.get("state"), pr.get("mergedAt")
    if isinstance(state, str) and state in {"OPEN", "CLOSED"} and "mergedAt" in pr and merged is None:
        return state
    if (state == "MERGED" and isinstance(merged, str) and
            re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", merged)):
        try:
            time.strptime(merged, "%Y-%m-%dT%H:%M:%SZ")
            return state
        except ValueError:
            pass
    return "UNKNOWN"


def stage(row):
    if row.get("pr_url") or row.get("pr"):
        state = row.get("pr_state", "UNKNOWN")
        if state == "MERGED":
            return "Слито в main"
        if state == "CLOSED":
            return "Закрыто без merge"
        if state != "OPEN":
            return "Неизвестно"
        if row["phase"] == "blocked":
            return "Worker заблокирован"
        if "checks" not in row:
            return "Неизвестно"
        checks = row["checks"]
        if any(value in FAILURES for value in checks.values()):
            return "Ошибка проверок"
        if set(checks) == set(REQUIRED) and all(value == "success" for value in checks.values()):
            return "Ожидает проверки кода"
        if any(value in {"queued", "pending", "in_progress"} for value in checks.values()):
            return "Проверки выполняются"
        return "Ожидание проверок"
    return PHASE_TEXT.get(row["phase"], "Неизвестно")


def task_text(row, live, now):
    text = stage(row)
    if row.get("pr_url") or row.get("pr") or row["phase"] in {"Queued", "blocked"}:
        return text
    if (worker_active(live, now) and live.get("issue") == row["issue"]
            and live["phase"] == row["phase"]):
        return text
    return f"Последний этап: {text} · выполнение не подтверждено"


def featured_task(rows, live, now):
    current = (live or {}).get("issue")
    if worker_active(live, now) and current in rows:
        row = rows[current]
        if (row.get("pr_state") not in {"MERGED", "CLOSED"} and row["phase"] == live["phase"]
                and row["phase"] in {"claimed", "clone", "dependencies", "codex_running", "branch_pr"}):
            return "Текущая задача", row
    if rows:
        return "Последняя задача", rows[max(rows)]
    return "Задач пока нет", None


def launch_notice(message, live, now):
    if not message:
        return ""
    if worker_active(live, now):
        return "Ранее: запуск не был подтверждён. Сейчас Worker активен по heartbeat."
    return message


class Notifications:
    def __init__(self, root):
        self.path = root / "monitor_notifications.json"
        self.available = True
        try:
            if self.path.exists():
                with self.path.open("rb") as stream:
                    raw = stream.read(1_000_001)
                data = json.loads(raw) if len(raw) <= 1_000_000 else None
                if not isinstance(data, list) or not all(isinstance(x, str) and len(x) <= 160 for x in data):
                    raise ValueError
                self.seen = set(data)
            else:
                self.seen = set()
        except (OSError, ValueError, TypeError):
            self.seen = set()
            self.available = False  # Fail closed rather than replay every notification.

    def claim(self, key):
        if len(self.seen) >= 10000:
            self.available = False
        if not self.available or key in self.seen:
            return False
        self.seen.add(key)
        try:
            # Persist BEFORE sounding: a crash may lose one sound, never replay it.
            worker.state_write(self.path, sorted(self.seen))
        except OSError:
            self.available = False
            return False
        return True


def collect(root):
    live = read_live(root)
    rows = {n: {"issue": n, **job} for n, job in (live or {}).get("jobs", {}).items()}
    online = True
    notices = []
    try:
        issues, prs = gh_json(ISSUES), gh_json(PRS)
        if not isinstance(issues, list) or not isinstance(prs, list):
            raise Unavailable("Malformed listing")
        for item in issues:
            if (isinstance(item, dict) and number(item.get("number")) and
                    isinstance(item.get("title"), str) and item["title"].startswith(worker.PREFIX)
                    and isinstance(item.get("author"), dict) and item["author"].get("login") == worker.OWNER
                    and any(isinstance(x, dict) and x.get("name") == worker.LABEL for x in item.get("labels", []))):
                rows.setdefault(item["number"], {"issue": item["number"], "phase": "Queued"})
        matched = []
        for pr in prs:
            if not isinstance(pr, dict) or not number(pr.get("number")):
                continue
            branch = pr.get("headRefName", "")
            match = re.fullmatch(r"codex/issue-([1-9][0-9]{0,14})", branch) if isinstance(branch, str) else None
            if not match or pr.get("url") != f"{BASE}/pull/{pr['number']}":
                continue
            n = int(match[1])
            # A branch name alone is not proof this PR was created by the worker.
            row = rows.get(n)
            if row is None or row.get("pr_url") != pr["url"]:
                continue
            if not isinstance(pr.get("headRefOid"), str) or not re.fullmatch(r"[0-9a-f]{40}", pr["headRefOid"]):
                continue
            row.update(pr=pr["number"], link=pr["url"], pr_state=pr_state(pr))
            matched.append((row, pr))
        # Bound each refresh; older PRs remain visibly waiting rather than falsely green.
        for row, pr in sorted(matched, key=lambda pair: pair[1]["number"], reverse=True)[:10]:
            try:
                checks = checks_for(pr)
                row["checks"] = checks
                sha = pr["headRefOid"]
                if row["phase"] == "blocked":
                    row["audit"] = "blocked (worker)"
                if any(s in FAILURES for s in checks.values()):
                    row["audit"] = "blocked (checks)"
                    notices.append((f"failure:{pr['number']}:{sha}", f"PR #{pr['number']}: ошибка CI/QA", pr["url"]))
                elif all(s == "success" for s in checks.values()):
                    row["audit"] = "blocked (worker)" if row["phase"] == "blocked" else "pending human/ChatGPT review"
                    notices.append((f"green:{pr['number']}:{sha}", f"PR #{pr['number']}: все четыре проверки успешны", pr["url"]))
                else:
                    row["audit"] = "waiting for checks"
            except Unavailable:
                row["audit"] = "unavailable"
                online = False
    except (Unavailable, TypeError, AttributeError):
        online = False
    for n, row in rows.items():
        url = row.get("pr_url")
        if url:
            row["link"] = url
            prn = url.rsplit("/", 1)[1]
            notices.append((f"pr:{prn}", f"Задача #{n}: создан PR #{prn}", url))
        if row["phase"] == "blocked":
            link = f"{BASE}/issues/{n}"
            notices.append((f"blocked:{n}", f"Задача #{n}: Worker заблокирован", link))
        if row.get("pr_state") in {"MERGED", "CLOSED", "UNKNOWN"}:
            row["audit"] = stage(row)
        row.setdefault("link", f"{BASE}/issues/{n}")
    return {"live": live, "rows": rows, "online": online, "notices": notices}


def start_worker(root):
    """No pull, install, shell, or user-supplied command. Old installations fail closed."""
    if not WINDOWS or sys.version_info[:2] != (3, 13):
        raise Unavailable("Windows Python 3.13 required")
    installed = root / "source" / "scripts" / "codex_issue_worker.py"
    try:
        if installed.read_bytes() != Path(worker.__file__).read_bytes():
            raise Unavailable("Installed worker needs trusted source update")
        with worker.instance_lock(root):
            if worker.other_workers():
                raise Unavailable("Existing worker detected; attach only")
            # Child acquires the lock itself. A competing launch will be rejected there.
        return subprocess.Popen([sys.executable, str(installed), "--watch", "--interval", "90"],
                                shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, worker.WorkerError):
        raise Unavailable("Worker active or startup safety check unavailable") from None


def sound_notification(app, store, key):
    """Called only by the UI thread; claim before a best-effort, single short bell."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Notifications require the UI main thread")
    if not store.claim(key):
        return False
    try:
        app.bell()
    except Exception:
        pass  # Desktop audio/Tk can be unavailable. Never replay a claimed event.
    return True


def observed_timeline(live, issue):
    events = [event for event in (live or {}).get("events", []) if event["issue"] == issue][-5:]
    return "\n".join(
        f"{time.strftime('%H:%M:%S', time.localtime(event['timestamp']))}  ·  {PHASE_TEXT[event['phase']]}"
        for event in events) or "Нет сохранённых событий для этой задачи."


class VpsCheck:
    """One in-flight observation; only the Tk thread touches widgets or schedules."""
    def __init__(self, app, button, result):
        self.app, self.button, self.result = app, button, result
        self.queue = queue.SimpleQueue()
        self.busy = False
        self.closed = threading.Event()

    def start(self):
        if self.busy or self.closed.is_set():
            return
        self.busy = True
        self.button.configure(state="disabled")
        self.result.configure(text="VPS: проверка выполняется…")
        def work():
            try:
                value = vps.check_once()
            except Exception:
                value = vps.unknown()
            if not self.closed.is_set():
                self.queue.put(value)
        threading.Thread(target=work, daemon=True).start()
        self.app.after(100, self.poll)

    def poll(self):
        if self.closed.is_set():
            return
        try:
            value = self.queue.get_nowait()
        except queue.Empty:
            self.app.after(100, self.poll)
            return
        self.busy = False
        self.button.configure(state="normal")
        self.result.configure(text="VPS: " + value["status"] + "\n" + " · ".join(
            f"{key}={value[key]}" for key in vps.FIELDS))


def main():
    # Optional dependency must be checked BEFORE accessing worker state or starting threads.
    try:
        import customtkinter as ctk
    except ImportError:
        print("Control Center requires optional CustomTkinter==6.0.0. "
              "Run scripts/setup_control_center_ui.ps1 explicitly, then start_control_center.bat.", file=sys.stderr)
        return 1
    if ctk.__version__ != "6.0.0":
        print("Control Center requires CustomTkinter==6.0.0; run the optional UI setup.", file=sys.stderr)
        return 1
    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")
    app = ctk.CTk()
    app.title("FunPayFlow — Центр задач")
    app.geometry("1150x700")
    app.minsize(760, 480)
    app.update_idletasks()
    # CTk geometry is logical; screen/widget sizes are physical pixels at Windows DPI.
    screen_ratio = 1150 / max(1, app.winfo_width())
    width = min(1150, max(760, int((app.winfo_screenwidth() - 40) * screen_ratio)))
    height = min(700, max(480, int((app.winfo_screenheight() - 48) * screen_ratio)))
    app.geometry(f"{width}x{height}")
    app.configure(fg_color=("#edf3fa", "#111b29"))
    app.grid_columnconfigure(0, weight=1)
    app.grid_rowconfigure(2, weight=1)
    ink = ("#23364e", "#e0ebf7")
    muted = ("#526780", "#a5b8ce")
    surface = ("#ffffff", "#1a293b")
    accent = "#2686cf"

    def label(parent, text="", **kwargs):
        return ctk.CTkLabel(parent, text=text, anchor="w", justify="left",
                            text_color=ink, font=("Segoe UI", 14), **kwargs)

    header = ctk.CTkFrame(app, fg_color="transparent")
    header.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 6))
    header.grid_columnconfigure(0, weight=1)
    title = label(header, "FunPayFlow · Центр задач")
    title.configure(font=("Segoe UI", 24, "bold"))
    title.grid(row=0, column=0, sticky="w")
    themes = {"Системная": "System", "Светлая": "Light", "Тёмная": "Dark"}
    theme = ctk.CTkOptionMenu(header, values=list(themes), width=145,
                            command=lambda value: ctk.set_appearance_mode(themes[value]))
    theme.grid(row=0, column=1, padx=(8, 0))
    status = label(header, "Worker: состояние неизвестно | GitHub: ожидание", wraplength=900)
    status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
    phase_text = label(app, "Задачи создаются в ChatGPT → GitHub. Здесь — только наблюдение.", wraplength=900)
    phase_text.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))
    content = ctk.CTkFrame(app, fg_color="transparent")
    content.grid(row=2, column=0, sticky="nsew", padx=20)
    content.grid_columnconfigure(0, weight=2, minsize=230)
    content.grid_columnconfigure(1, weight=3)
    content.grid_rowconfigure(0, weight=1)
    history = ctk.CTkScrollableFrame(content, corner_radius=16, fg_color=surface,
                                    label_text="Задачи и история · до 100", label_anchor="w")
    history.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
    history.grid_columnconfigure(0, weight=1)
    card = ctk.CTkScrollableFrame(content, corner_radius=16, fg_color=surface)
    card.grid(row=0, column=1, sticky="nsew")
    card.grid_columnconfigure(0, weight=1)
    heading = label(card, "Задач пока нет")
    heading.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
    summary = label(card, "Отправьте задачу в ChatGPT.", wraplength=500)
    summary.configure(font=("Segoe UI", 19, "bold"))
    summary.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
    check_widgets = {}
    for index, (name, text) in enumerate(CHECK_LABELS.items(), 2):
        item = ctk.CTkFrame(card, corner_radius=12, fg_color=("#f0f5fb", "#22364c"))
        item.grid(row=index, column=0, sticky="ew", padx=12, pady=3)
        value = label(item, f"{text}: Неизвестно", wraplength=470)
        value.pack(fill="x", padx=12, pady=8)
        check_widgets[name] = value
    label(card, "Наблюдаемые этапы · без процента готовности").grid(
        row=6, column=0, sticky="ew", padx=12, pady=(16, 4))
    timeline = label(card, "Нет сохранённых событий.", wraplength=500)
    timeline.grid(row=7, column=0, sticky="ew", padx=12)
    event_indicator = label(card, "Событий пока нет", wraplength=500)
    event_indicator.configure(text_color=muted)
    event_indicator.grid(row=8, column=0, sticky="ew", padx=12, pady=12)
    footer = ctk.CTkFrame(app, fg_color="transparent")
    footer.grid(row=3, column=0, sticky="ew", padx=20, pady=12)
    details = label(footer, "Открытие окна не запускает Worker.", wraplength=900)
    details.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
    root = worker.home()
    result_queue = queue.Queue(maxsize=2)
    live_queue = queue.Queue(maxsize=1)
    launch_queue = queue.Queue(maxsize=1)
    stop = threading.Event()
    launch = threading.Event()
    launch_busy = threading.Event()
    notification_store = Notifications(root)
    snapshot = {}
    children = []
    last = None
    last_launch = ""
    selected = None
    history_buttons = {}
    notice_history = {}  # Safe fixed messages only, bounded by visible task history below.

    def poll_live():
        while not stop.is_set():
            value = read_live(root)
            try:
                live_queue.get_nowait()
            except queue.Empty:
                pass
            live_queue.put_nowait(value)
            stop.wait(1)

    def poll_launch():
        while not stop.is_set():
            if launch.wait(1):
                launch_busy.set()
                launch.clear()
                try:
                    children.append(start_worker(root))
                    message = "Запуск запрошен; ожидается heartbeat Worker"
                except Unavailable:
                    message = "Сначала проверьте уже запущенный Worker. Запуск не подтверждён проверкой безопасности."
                try:
                    launch_queue.get_nowait()
                except queue.Empty:
                    pass
                launch_queue.put_nowait(message)
                launch_busy.clear()
            for child in children[:]:
                if child.poll() is not None:
                    children.remove(child)

    def poll():
        while not stop.is_set():
            try:
                result = collect(root)
            except Exception:
                result = {"live": read_live(root), "rows": {}, "online": False, "notices": []}
            # Drop old snapshots; main thread always gets recent observations.
            try:
                result_queue.put_nowait(result)
            except queue.Full:
                pass
            stop.wait(5)

    def notify(key, message, link):
        notice_history[key] = (message, link)
        if len(notice_history) > 400:
            del notice_history[next(iter(notice_history))]
        sound_notification(app, notification_store, key)

    def select_task(issue):
        nonlocal selected
        selected = issue
        display()

    def display():
        if last is None:
            return
        now = time.time()
        live = last["live"]
        status.configure(text=worker_status_text(live, last["online"], now))
        phase_text.configure(text=phase_status_text(live, now, snapshot))
        active = worker_active(live, now)
        start_button.configure(text="Worker запущен" if active else "Запустить Worker",
                               state="disabled" if active or launch.is_set() or launch_busy.is_set() else "normal")
        visible = dict(sorted(snapshot.items(), reverse=True)[:100])
        # Keep widgets during refresh so focus, selection and scroll position survive.
        for n in list(history_buttons):
            if n not in visible:
                history_buttons.pop(n).destroy()
        for index, (n, row) in enumerate(visible.items()):
            if n not in history_buttons:
                history_buttons[n] = ctk.CTkButton(history, anchor="w", height=74, corner_radius=12,
                    command=lambda issue=n: select_task(issue), text_color=ink, hover_color=("#dcecff", "#314d6b"))
            text = task_text(row, live, now)
            if not last["online"] and row.get("pr_url"):
                text += " · данные ранее"
            button = history_buttons[n]
            short = text if len(text) <= 34 else text[:31] + "…"
            task_links = {f"{BASE}/issues/{n}", row.get("pr_url")}
            marker = " · •" if any(link in task_links for _, link in notice_history.values()) else ""
            button.configure(text=f"#{n}" + (f" · PR #{row['pr']}" if row.get("pr") else "") + marker + f"\n{short}",
                             fg_color=("#dcecff", "#314d6b") if selected == n else ("#f0f5fb", "#22364c"))
            button.grid(row=index, column=0, sticky="ew", padx=4, pady=4)
        empty.configure(text="Отправьте задачу в ChatGPT.\nЗдесь появится очередь GitHub." if not visible else "")
        empty.grid(row=len(visible), column=0, sticky="ew", padx=4)
        caption, featured = featured_task(visible, live, now)
        if selected in visible:
            caption, featured = "Выбранная задача", visible[selected]
        heading.configure(text=caption)
        summary.configure(text=(f"Задача #{featured['issue']} · {task_text(featured, live, now)}"
                                if featured else "Отправьте задачу в ChatGPT; Worker проверяет очередь GitHub."))
        for name, widget in check_widgets.items():
            state = (featured or {}).get("checks", {}).get(name)
            widget.configure(text=f"{CHECK_LABELS[name]}: {CHECK_TEXT.get(state, 'Неизвестно')}",
                             text_color=("#207547", "#70d69d") if state == "success" else
                             ("#ae343c", "#ff9299") if state in FAILURES else ink)
        timeline.configure(text=observed_timeline(live, (featured or {}).get("issue")))
        links = {f"{BASE}/issues/{n}" for n in visible}
        links.update(row.get("pr_url") for row in visible.values())
        for key in list(notice_history):
            if notice_history[key][1] not in links:
                del notice_history[key]
        task_links = {f"{BASE}/issues/{featured['issue']}", featured.get("pr_url")} if featured else set()
        messages = [message for message, link in notice_history.values() if link in task_links][-4:]
        event_indicator.configure(text="События\n" + "\n".join(messages) if messages else "Событий для задачи пока нет")
        notice = launch_notice(last_launch, live, now)
        if not notification_store.available:
            notice += " · Звуки отключены: хранилище защиты от повторов недоступно."
        if not last["online"]:
            notice += " · GitHub недоступен; результаты могут быть сохранёнными ранее."
        details.configure(text=notice.strip(" ·") or "Звук — только для новых событий. Worker работает независимо от окна.")

    def render():
        nonlocal last, snapshot, last_launch
        try:
            last_launch = launch_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            while True:
                result = result_queue.get_nowait()
                last = result
                if result["online"]:
                    snapshot = result["rows"]
                else:
                    snapshot.update(result["rows"])
                for key, message, link in result["notices"]:
                    notify(key, message, link)
        except queue.Empty:
            pass
        try:
            fresh_live = live_queue.get_nowait()
            if last is None:
                last = {"live": fresh_live, "online": False}
            else:
                last["live"] = fresh_live
            for n, job in (fresh_live or {}).get("jobs", {}).items():
                row = snapshot.setdefault(n, {"issue": n, "link": f"{BASE}/issues/{n}"})
                row.update(job)
                if job.get("pr_url"):
                    row["link"] = job["pr_url"]
                    prn = job["pr_url"].rsplit("/", 1)[1]
                    notify(f"pr:{prn}", f"Задача #{n}: создан PR #{prn}", job["pr_url"])
                if job["phase"] == "blocked":
                    notify(f"blocked:{n}", f"Задача #{n}: Worker заблокирован", f"{BASE}/issues/{n}")
        except queue.Empty:
            pass
        snapshot = dict(sorted(snapshot.items(), reverse=True)[:100])
        display()
        app.after(1000, render)

    def open_selected():
        _, featured = featured_task(snapshot, (last or {}).get("live"), time.time())
        row = snapshot.get(selected, featured or {})
        link = row.get("link", BASE)
        if link == BASE or safe_link(link):
            webbrowser.open(link)

    def request_launch():
        if not launch.is_set() and not launch_busy.is_set() and not worker_active((last or {}).get("live"), time.time()):
            launch.set()
            start_button.configure(state="disabled")

    empty = label(history, "", wraplength=250)
    start_button = ctk.CTkButton(footer, text="Запустить Worker", corner_radius=10,
                               fg_color=accent, command=request_launch)
    start_button.grid(row=1, column=0, sticky="w")
    ctk.CTkButton(footer, text="Открыть GitHub", corner_radius=10, command=open_selected).grid(
        row=1, column=1, sticky="w", padx=10)

    vps_area = ctk.CTkFrame(app, corner_radius=12, fg_color=surface)
    vps_area.grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 12))
    vps_area.grid_columnconfigure(1, weight=1)
    vps_button = ctk.CTkButton(vps_area, text="Проверить VPS", corner_radius=10)
    vps_button.grid(row=0, column=0, padx=12, pady=8)
    vps_result = label(vps_area, "VPS: UNKNOWN · проверка только по кнопке", wraplength=740)
    vps_result.grid(row=0, column=1, sticky="ew", padx=12)
    vps_check = VpsCheck(app, vps_button, vps_result)
    vps_button.configure(command=vps_check.start)
    vps_pipeline = label(vps_area, "Проверить → Исходники готовы (только source_staged=YES) → Зависимости НЕ готовы → "
          "Совместимость/резервная копия НЕ проверены → Деплой недоступен\n"
          "Source-only 0affd678 не готов к деплою; наличие backup-каталога не подтверждает резервную копию.",
          wraplength=950)
    vps_pipeline.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 8))

    def resize(event):
        if event.widget is app:
            ratio = int(app.geometry().split("x")[0]) / max(1, app.winfo_width())
            width = max(220, int(card.winfo_width() * ratio) - 50)
            for widget in (summary, timeline, event_indicator, *check_widgets.values()):
                widget.configure(wraplength=width)
            for widget in (status, phase_text, details):
                widget.configure(wraplength=max(400, int(header.winfo_width() * ratio) - 20))
            vps_width = int(vps_area.winfo_width() * ratio)
            vps_result.configure(wraplength=max(220, vps_width - 200))
            vps_pipeline.configure(wraplength=max(300, vps_width - 24))
    app.bind("<Configure>", resize, add="+")

    def browse(direction):
        ids = sorted(snapshot, reverse=True)
        if ids:
            index = ids.index(selected) if selected in ids else (-1 if direction > 0 else len(ids))
            select_task(ids[max(0, min(len(ids) - 1, index + direction))])
    app.bind("<Down>", lambda event: browse(1), add="+")
    app.bind("<Up>", lambda event: browse(-1), add="+")

    def close():
        stop.set()
        vps_check.closed.set()
        app.destroy()  # Worker remains independent; never terminate external processes.
    app.protocol("WM_DELETE_WINDOW", close)
    threading.Thread(target=poll_launch, daemon=True).start()
    threading.Thread(target=poll_live, daemon=True).start()
    threading.Thread(target=poll, daemon=True).start()
    render()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
