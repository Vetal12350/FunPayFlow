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
            # Persist BEFORE showing popup: a crash may lose one popup, never replay it.
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


def main():
    # Tk is deliberately lazy: headless Linux tests can import every model function.
    import tkinter as tk
    from tkinter import ttk
    root = worker.home()
    app = tk.Tk()
    from tkinter import font
    app.title("FunPayFlow — Центр задач")
    app.geometry("1150x700")
    app.minsize(980, 640)
    for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
        font.nametofont(name).configure(size=11)
    style = ttk.Style(app)
    style.theme_use("clam")
    style.configure("TFrame", background="#f3f5f7")
    style.configure("TLabel", background="#f3f5f7", foreground="#253448")
    style.configure("Title.TLabel", font=("Segoe UI", 17, "bold"))
    style.configure("Accent.TLabel", foreground="#225c90", font=("Segoe UI", 12, "bold"))
    style.configure("Treeview", font=("Segoe UI", 11), rowheight=max(34, font.nametofont("TkDefaultFont").metrics("linespace") + 12))
    style.configure("Treeview.Heading", font=("Segoe UI", 11, "bold"))
    app.configure(background="#f3f5f7")
    body = ttk.Frame(app, padding=16)
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(6, weight=1)
    ttk.Label(body, text="FunPayFlow · Центр задач", style="Title.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(body, text="Задачи создаются в ChatGPT и появляются здесь после регистрации в GitHub.", wraplength=940).grid(row=1, column=0, sticky="w", pady=(4, 8))
    status = tk.StringVar(value="Worker: состояние неизвестно | GitHub: ожидание")
    ttk.Label(body, textvariable=status, wraplength=940).grid(row=2, column=0, sticky="w")
    phase_text = tk.StringVar(value="Последний этап: неизвестно")
    ttk.Label(body, textvariable=phase_text, wraplength=940).grid(row=3, column=0, sticky="w", pady=(2, 8))
    card = ttk.LabelFrame(body, text="Задача", padding=12)
    card.grid(row=4, column=0, sticky="ew", pady=(0, 10))
    summary = tk.StringVar(value="Задач пока нет. Отправьте задачу в ChatGPT.")
    ttk.Label(card, textvariable=summary, style="Accent.TLabel", wraplength=920).pack(anchor="w")
    ttk.Label(card, text="Codex сообщает только крупные этапы: отдельные правки и процент готовности недоступны.",
              wraplength=920).pack(anchor="w", pady=(6, 8))
    indicators = ttk.Frame(card)
    indicators.pack(fill="x")
    check_vars = {}
    check_widgets = {}
    for index, (name, label) in enumerate(CHECK_LABELS.items()):
        indicators.columnconfigure(index % 2, weight=1)
        value = tk.StringVar(value=f"{label}: Неизвестно")
        check_vars[name] = value
        label_widget = ttk.Label(indicators, textvariable=value)
        label_widget.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 12), pady=2)
        check_widgets[name] = label_widget
    ttk.Label(body, text="Задачи и история · до 100 записей (выберите строку для просмотра)").grid(row=5, column=0, sticky="w")
    history = ttk.Frame(body)
    history.grid(row=6, column=0, sticky="nsew", pady=(5, 8))
    columns = ("issue", "stage", "pr")
    tree = ttk.Treeview(history, columns=columns, show="headings", height=6, selectmode="browse")
    for name, label, width in (("issue", "Задача", 100), ("stage", "Состояние", 720), ("pr", "PR", 100)):
        tree.heading(name, text=label)
        tree.column(name, width=width, minwidth=80, stretch=name == "stage")
    scrollbar = ttk.Scrollbar(history, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    tree.pack(fill="both", expand=True)
    tree.tag_configure("merged", foreground="#237249")
    details = tk.Text(body, height=3, wrap="word", state="disabled", relief="flat", font="TkTextFont")
    details.grid(row=7, column=0, sticky="ew")
    buttons = ttk.Frame(body)
    buttons.grid(row=8, column=0, sticky="ew", pady=(8, 0))
    result_queue = queue.Queue(maxsize=2)
    live_queue = queue.Queue(maxsize=1)
    launch_queue = queue.Queue(maxsize=1)
    stop = threading.Event()
    launch = threading.Event()
    launch_busy = threading.Event()
    notification_store = Notifications(root)
    snapshot = {}
    children = []
    popups = []
    last = None
    last_launch = ""

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

    def popup(message, link):
        window = tk.Toplevel(app)
        window.title("FunPayFlow · Уведомление")
        ttk.Label(window, text=message).pack(padx=20, pady=12)
        ttk.Button(window, text="Открыть GitHub", command=lambda: webbrowser.open(link)).pack(pady=8)
        window.attributes("-topmost", True)
        app.bell()
        popups.append(window)
        # Bounded, non-modal local Windows popups. Never block the refresh loop.
        if len(popups) > 4:
            old = popups.pop(0)
            if old.winfo_exists():
                old.destroy()
        window.after(20000, window.destroy)

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
                    if notification_store.claim(key):
                        popup(message, link)
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
                    if notification_store.claim(f"pr:{prn}"):
                        popup(f"Задача #{n}: создан PR #{prn}", job["pr_url"])
                if job["phase"] == "blocked" and notification_store.claim(f"blocked:{n}"):
                    popup(f"Задача #{n}: Worker заблокирован", f"{BASE}/issues/{n}")
        except queue.Empty:
            pass
        if last is not None:
            now = time.time()
            live = last["live"]
            status.set(worker_status_text(live, last["online"], now))
            phase_text.set(phase_status_text(live, now, snapshot))
            active = worker_active(live, now)
            start_button.configure(text="Worker запущен" if active else "Запустить Worker",
                                   state="disabled" if active or launch.is_set() or launch_busy.is_set() else "normal")
            selection = tree.selection()
            scroll_position = tree.yview()[0]
            tree.delete(*tree.get_children())
            visible = dict(sorted(snapshot.items(), reverse=True)[:100])
            for n, row in visible.items():
                text = task_text(row, live, now)
                if not last["online"] and row.get("pr_url"):
                    text += " (данные ранее)"
                tree.insert("", "end", iid=str(n), values=(f"#{n}", text, f"#{row['pr']}" if row.get("pr") else "—"),
                            tags=("merged",) if row.get("pr_state") == "MERGED" else ())
            for selected in selection:
                if tree.exists(selected):
                    tree.selection_add(selected)
            tree.yview_moveto(scroll_position)
            heading, featured = featured_task(visible, live, now)
            selected = tree.selection()
            if selected:
                heading, featured = "Выбранная задача", visible[int(selected[0])]
            card.configure(text=heading)
            if featured:
                pr_label = f" · PR #{featured['pr']}" if featured.get("pr") else ""
                cache_label = " · данные GitHub ранее" if not last["online"] and featured.get("pr_url") else ""
                summary.set(f"Задача #{featured['issue']}{pr_label} · {task_text(featured, live, now)}{cache_label}")
            else:
                summary.set("Задач пока нет. Отправьте задачу в ChatGPT; Worker проверяет очередь GitHub.")
            for name, value in check_vars.items():
                state = (featured or {}).get("checks", {}).get(name)
                value.set(f"{CHECK_LABELS[name]}: {CHECK_TEXT.get(state, 'Неизвестно')}")
                check_widgets[name].configure(foreground="#237249" if state == "success" else
                                              "#a02f34" if state in FAILURES else "#253448")
            lines = []
            notice = launch_notice(last_launch, live, now)
            if notice:
                lines.append(notice)
            events = (live or {}).get("events", [])[-2:]
            for event in events:
                lines.append(f"{time.strftime('%H:%M:%S', time.localtime(event['timestamp']))} · Задача #{event['issue'] or '—'} · {PHASE_TEXT.get(event['phase'], 'Неизвестно')}")
            if not lines:
                lines.append("Ожидание событий Worker. Открытие окна не запускает Worker.")
            if not notification_store.available:
                lines.append("Уведомления отключены: хранилище защиты от повторов недоступно.")
            details.config(state="normal")
            details.delete("1.0", "end")
            details.insert("1.0", "\n".join(lines))
            details.config(state="disabled")
        app.after(1000, render)

    def open_selected():
        selected = tree.selection()
        link = snapshot.get(int(selected[0]), {}).get("link") if selected else BASE
        if link == BASE or safe_link(link):
            webbrowser.open(link)

    def request_launch():
        if not launch.is_set() and not launch_busy.is_set() and not worker_active((last or {}).get("live"), time.time()):
            launch.set()
            start_button.configure(state="disabled")

    start_button = ttk.Button(buttons, text="Запустить Worker", command=request_launch)
    start_button.pack(side="left")
    ttk.Button(buttons, text="Открыть GitHub", command=open_selected).pack(side="left")
    def close():
        stop.set()
        app.destroy()  # Worker remains independent; never terminate external processes.
    app.protocol("WM_DELETE_WINDOW", close)
    threading.Thread(target=poll_launch, daemon=True).start()
    threading.Thread(target=poll_live, daemon=True).start()
    threading.Thread(target=poll, daemon=True).start()
    render()
    app.mainloop()


if __name__ == "__main__":
    main()
