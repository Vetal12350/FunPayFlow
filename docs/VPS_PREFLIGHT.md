# Приватная предварительная проверка VPS без изменения production

Использовать **перед** обновлением production, чтобы не полагаться на историческую документацию.
Это не deployment, не live NodeForm test и не резервная копия.

## Разовый запуск через существующий SSH с Windows

Команда использует локальный checkout Codex Worker только как источник публичного скрипта:

~~~powershell
$src = "$env:LOCALAPPDATA\FunPayFlowCodexWorker\source"
git -C $src pull --ff-only
Get-Content -Raw -Encoding UTF8 "$src\scripts\vps_preflight.py" |
  ssh -T -i "$env:USERPROFILE\.ssh\id_ed25519" root@SERVER_IP "python3 -"
~~~

Вместо SERVER_IP укажи адрес VPS из панели провайдера. Нельзя отключать
проверку SSH host key или копировать приватные SSH ключи в чат. Если ключ имеет
другое имя, исправь путь только локально в PowerShell. Команда git pull должна
успешно завершиться; source checkout должен быть доверенным origin
https://github.com/Vetal12350/FunPayFlow. Не запускай непроверенный код.

Скрипт печатает **только** одну JSON-строку без секретов и абсолютных путей:

- service / legacy_service: active, inactive, failed либо unknown.
- main_runtime_processes: none, one, multiple либо unknown. Считается ожидаемый
  python -m funpayflow.main; альтернативные способы запуска нужно проверять отдельно.
- code_commit: публичный 40-значный Git SHA или missing/unknown.
- tracked_worktree: clean, dirty либо unknown — без имён файлов.
- data_directory / env_file / settings_file: present или missing.
- sqlite: ok, failed, unknown, missing, skipped_large.
- disk_free: приблизительная категория относительно 512 МиБ.
- read_only=true; deployment_performed=false.

Скрипт не открывает содержимое .env или настроек, не выводит приватные данные
SQLite и использует readonly SQLite URI + PRAGMA quick_check для БД до 256 МиБ.
Проверка может кратко занять SQLite read lock, это не гарантия здоровья Runner.
С большими БД нужен отдельный consistency/backup check.

## После preflight

1. Проверить фактические code/data paths, единственный Runner, один активный
   unit, текущий commit и возможность восстановления.
2. Создать согласованный backup data/settings/SQLite и unit с закрытым writer
   или SQLite backup API; отдельно проверить integrity и rollback.
3. Готовить новый код вне работающей копии и без изменения приватных данных;
   не пересоздавать релиз v1.0.0.
4. Контролируемо остановить единственный runtime, обновить один service,
   сохранить manual OFF, SAFE_MODE, расписание и прогресс autobump.
5. Проверить service, процесс, версию и безопасные diagnostic reasons.
   Если есть проблемы — откатить; не запускать две службы одновременно.
6. Для NodeForm 976/986 получить allowlisted код через нормальный
   existing-Account read-only GET уже после развёртывания диагностики.
   Offline tests не подтверждают совместимость live FunPay HTML.

Результат preflight **не является разрешением** на слепой deploy/restart.
Для следующей операции нужно фактическое состояние сервера. Не публикуй
приватные SSH журналы, env, database или raw FunPay HTML.
