# Linux Codex Issue Worker: переносимость и проект изоляции

Это **source-only изменение и проект будущего размещения**, не разрешение на запуск
на существующем VPS. Установка, systemd unit, запуск сервиса, SSH, перенос данных,
изменения production и credentials в эту задачу не входят. Рекомендуемая архитектура —
отдельная VM/VPS для worker, без production-данных и доступа к production-сети.
Общий VPS нельзя считать безопасным до независимой проверки изоляции и разделения
GitHub-авторизации. Эти два вопроса остаются **блокерами реальной установки**.

## Что поддержано в исходниках

`scripts/codex_issue_worker.py` выбирает Linux `/proc` вместо Windows PowerShell.
Windows CIM-проверка, исключение родительского Python redirector и Windows lock
сохранены. На других ОС inventory завершается фиксированной ошибкой.

Linux inventory рассматривает процессы с владельцем proc-каталога, равным effective
UID worker; real/effective UID самого worker должны совпадать. Чужие argv/exe не
читаются. Возвращается только boolean; ошибки не содержат PID, путей, argv или вывода
дочерних процессов. Основной single-instance guard — удерживаемый ядром `flock`
на одном доверенном `worker.lock`, а не PID-файл. Каталог состояния и lock нельзя
давать изменять модельному процессу: замена inode или другой state root нарушают
предпосылки этого guard.

Дополнительная проверка старых workers имеет намеренно узкий контракт:

- Нужен полный доверенный procfs в текущих PID/mount namespaces: доступный `self`,
  читаемый `mountinfo`, обычный mount `/proc`, без `hidepid`, `subset` и PID overmounts.
  Недостающий self, недоступные/исчезнувшие PID, повреждённые данные и смена UID или
  starttime при чтении блокируют старт, без повторного сканирования.
- Обход ограничен 8192 entries (включая не-PID), 65536 bytes на файл, 256 argv и
  проверяемым между операциями бюджетом 5 секунд. Это не прерывание зависшего kernel
  I/O: procfs и разрешаемые script paths должны быть на доверенной локальной FS.
- Поддерживается прямой запуск `python`, `python2/3` или `python3.<version>` со
  script argument; допустимы отдельные `-B`, `-E`, `-I`, `-s`, `-S`, `-u`, `-O`, `-OO`
  и разделитель `--`. `exe` должен соответствовать Python, argv0 — имени Python.
  Script path разрешается через `/proc/<pid>/cwd` или `root`, включая symlinks.
  Другой chroot или mount namespace того же UID блокируется до разрешения пути.
  `codex_issue_worker.py` с другим абсолютным путём также считается worker; совпадение
  имени только в аргументах другого Python-скрипта не считается worker.
- Python `-m`, `-c`, stdin, неизвестные interpreter options, удалённый executable,
  недоступный script и wrappers с явным упоминанием worker дают фиксированный blocker:
  `Linux worker inventory unavailable or ambiguous; startup blocked`.
  В частности, запуск через остающийся `uv run` / shell runner не является
  поддерживаемой service-схемой; нужен прямой интерпретатор и script path.
  Пустой cmdline, в том числе у zombie того же UID, также блокирует старт.

Это проверка совместимости известных legacy-запусков, **не обнаружение произвольного
вредоносного кода**. Переименованный/встроенный worker в неизвестном runner, изменённый
argv, процессы другого UID, другой PID namespace, скрытый procfs и гонка появления
нового legacy worker после обхода не исключаются таким механизмом. Proc ownership
также зависит от dumpability/identity процесса. Поэтому отдельный неизменяемый UID,
единый контролируемый способ запуска и независимо проверенный procfs обязательны;
один scan не доказывает system-wide отсутствие второго worker. Сложные или непроверенные
launcher/namespace-схемы остаются блокером, обходов через pidfile или privileges нет.

Остальной pipeline использует argv с `shell=False`: Git, `gh`, `codex`, `uv` из
доверенного PATH. Linux не требует `powershell.exe`, `codex.cmd` или `taskkill.exe`.
Флаги `--once` / `--watch`, отдельные checkouts, проверка автора/label/title, строго
ограниченные model/effort, фиксированные ошибки CLI и предварительная запись `started`
сохранены. Недоступный профиль не заменяется другой моделью и не повторяется.
`started`, `blocked` и завершённые issue не запускаются снова после restart;
историческая #89 не сбрасывается и не переигрывается.

Heartbeat остаётся локальным `live_status.json` с интервалом 5 секунд и ограниченными
public metadata; это не systemd notify и не remote Control Center transport. Windows
Control Center остаётся прежним локальным UI. При timeout/interrupt worker завершает
созданную им process group Codex; Linux SIGTERM переводится в orderly unwind этого
же пути. Родительская сессия не сигнализируется. Потомки, которые сами сделали
`setsid`, не удерживаются process-group механизмом: для реальной службы необходим
независимый cgroup supervisor. Процесс-группа не является sandbox.

## Credentials и границы доверия

`codex_environment()` сохраняет существующее удаление API/GitHub/deployment env keys.
Это уменьшает наследование секретов, но **не запрещает** модельному локальному коду
читать файлы того же OS account, Git credential helpers, gh config/keyring, процессы
или доступную host filesystem. `--sandbox workspace-write`, prompt-запреты и удаление
env keys сами по себе не доказывают отделение от GitHub credentials или production.
Произвольный отчёт модели также нельзя считать проверенным или безопасным для публикации.

Текущий worker запускает trusted orchestration и Codex под одним аккаунтом; независимого
credential broker здесь **нет**. Даже на отдельной VM нужен отдельный проект разделения:
broker с минимальными GitHub capabilities вне доступного модели UID/filesystem/IPC,
либо эквивалентная независимо проверенная граница. Broker должен валидировать операции,
репозиторий, branch и diff, а не принимать произвольный shell/argv от модели. Нельзя
передавать модели write token, credential helper или доступ к broker state/lock.
Codex login требует отдельного анализа доступа; реальный CLI/login/model availability
не проверяются офлайн. Несовместимость CLI вне этих четырёх файлов — документируемый
блокер, а не повод отключить sandbox, approval policy или Windows protection.

## Предпосылки для обсуждения общего production VPS

Предпочтительнее отдельная VM/VPS. Следующее — критерии будущей независимой проверки,
а не инструкция конфигурации работающего сервера:

| Область | Обязательное доказательство до установки |
| --- | --- |
| Identity | Отдельный непривилегированный service UID без sudo/capabilities/общих production-групп; model UID отдельно от broker; нет возможности менять identity или namespaces для обхода политики. |
| Production | Реальный отказ чтения и записи `/root/FunPayFlow-data`, `.env`, production source, backups, логов и runtime state, включая aliases, mounts и inherited descriptors. |
| Credentials | GitHub broker/credential separation проверена независимо; минимальная repo write scope для ветки/PR, без admin, workflows, releases, deploy/merge; никаких GitHub credentials в модельной FS или environment. |
| Filesystem | Только отдельные worktree и ограниченный private tmp writable для job; trusted runner, state/lock и broker недоступны модели; нет произвольных host paths и привилегированных mounts. |
| Host access | Нет SSH keys/agent, Docker/container socket, host root, production network endpoints или доступа к другим процессам через `/proc`; политика сети проверена отдельно от env sanitization. |
| Resources | Измеренные CPU/RAM/PIDs limits, disk quota/free-space reserve, disk I/O caps, лимиты времени и логов, чтобы job не мог остановить production. Один job одновременно. |
| Lifecycle | Проверенный cgroup cleanup всех descendants; systemd restart/backoff/start limits и независимый watchdog по свежести/прогрессу. Один `WatchdogSec` без notify не реализует watchdog. Shutdown deadline должен позволять cleanup; затем supervisor завершает только worker cgroup. |
| Failure policy | Auth failure не запускает job; неверный model/effort отклоняется до claim; CLI/model failure блокирует claimed job без fallback/retry. Ограниченный restart, уведомление оператору, ручной разбор state; нет автоматического сброса processed issue. |
| Review | Только обычный PR с human review, без autodeploy/automerge. Все четыре Windows/Linux CI и Product/Core QA должны быть green плюс независимый ChatGPT security review до owner-approved merge. |

Совместимость procfs visibility и запрета чтения чужих процессов должна быть специально
проверена: текущая inventory не принимает `hidepid`/subset. Изоляция с отдельным namespace
не может одновременно обещать видимость всех legacy workers хоста. Если требования
невозможно совместить и доказать, общий VPS исключается; выбирается отдельная VM.
Никакие из этих свойств не подтверждаются одним PASS unit-тестов.

## Только локальная офлайн-самопроверка

В уже подготовленном source-only окружении с Python 3.13 и pytest, без установки
зависимостей и без аккаунтов:

```text
python -B -c "from pathlib import Path; Path('tests/.offline-tmp').mkdir(exist_ok=True)"
python -B -m pytest -q tests/test_codex_issue_worker.py --basetemp=tests/.offline-tmp/linux-worker -p no:cacheprovider
```

Тесты используют синтетический procfs под tmp_path, mocked UID/symlink/process/signal
metadata, fake GitHub/Codex, локальный Git и OS lock. Реальный `/proc` не перечисляется.
Scratch остаётся под `tests/.offline-tmp/`. `--check`, `--once`, `--watch` самого worker
**не являются офлайн-самопроверкой**: штатный pipeline может обращаться к GitHub/Codex,
клонировать исходники и публиковать PR. Здесь эти действия только mocked.

Нативные Linux flock/process signals, конкретный CLI и production isolation требуют
отдельных подтверждений. Локальный mocked Linux PASS на Windows нельзя выдавать за
Linux CI PASS. В этой source-only задаче CI не запускается через GitHub и независимый
ChatGPT review остаётся внешним merge gate; настоящий worker/служба не запускается.

## Отдельные будущие задачи

Каждый этап требует отдельного разрешения владельца: (1) read-only capacity/permissions
inspection CPU/RAM/SSD; (2) credential broker или отдельная VM и доказательство изоляции;
(3) pinned trusted installation с systemd/resource limits; (4) remote status transport;
(5) контролируемый cutover с Windows в idle, один worker и сохранение processed state.
Этот документ не разрешает ни один этап и не содержит команд установки/старта VPS.
