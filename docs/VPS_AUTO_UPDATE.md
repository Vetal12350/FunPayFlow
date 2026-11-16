# Автообновление VPS: устройство и разовая настройка владельцем

Issue #156. Код: [deploy_agent.py](../scripts/deploy_agent.py), [build_deploy_release.py](../scripts/build_deploy_release.py), [deploy.yml](../.github/workflows/deploy.yml), heartbeat в [heartbeat.py](../src/funpayflow/heartbeat.py). Тесты офлайн: `tests/test_deploy_agent.py`, `tests/test_deploy_workflow.py`, `tests/test_heartbeat.py`.

**Этот документ не разрешает SSH, deployment или изменение production.** Шаги ниже выполняет владелец сам; см. [PRODUCTION](PRODUCTION.md).

## Как работает обновление

1. Запуск: телефон (GitHub → Actions → *Deploy to VPS* → Run workflow) или ПК (`gh workflow run deploy.yml -f sha=<sha> -f action=deploy`). Указывается точный SHA текущего `main`.
2. `verify` (без секретов): SHA = голова `main`, CI `offline-tests` зелёный, pytest + compileall, сборка wheels по хешам `uv.lock` (FunPayAPI собирается из проверенного sdist здесь, не на сервере), `release.tar` + digest.
3. `deploy` выполняется в environment `production` (секреты доступны только ему). **Решение владельца — merge в `main`**: установить можно только текущую голову `main`, поэтому отдельный Approve не требуется. При желании включить Required reviewers (см. шаг 8) — тогда добавится подтверждение в GitHub.
4. SSH один раз отправляет `deploy <sha> <digest>` + архив. Агент проверяет digest, запускает отдельный worker (`systemd-run`) и отвечает `ACCEPTED`. Дальше CI только читает `status`.
5. Worker: распаковка и проверка wheels → venv `releases/<sha>` → `pip install --no-index --only-binary` → проверки (legacy-служба выключена, unit без drop-in и указывает на `current`) → backup SQLite (backup API + integrity_check) → атомарный `current → releases/<sha>` → `systemctl restart` → здоровье (служба active, без автоперезапусков 20 с, `health.json` с этим SHA и `runner=ok`).
6. Нездоров → автоматический откат на предыдущий релиз. Откат не удался → `NEEDS_OWNER`: новые операции блокируются до ручного разбора.

| Ситуация | Поведение |
| --- | --- |
| Обрыв SSH при отправке | `deploy` не повторяется; CI опрашивает `status`. `UNKNOWN_JOB` = не принято, можно запустить снова |
| Нет ответа 10 минут | Итог UNKNOWN, job красный. Не перезапускать deploy; смотреть `status` |
| Повторный запуск / параллельный | `BUSY` или `ALREADY_DEPLOYED`; GitHub `concurrency` не отменяет идущий |
| Прерванный worker | Никогда не продолжается: до переключения → `FAILED` (production не тронут), после → `NEEDS_OWNER` |
| Ручной откат | Workflow с `action=rollback` и SHA, который сейчас установлен |

Хранятся последние 3 релиза; backups в каталоге `backups` не удаляются автоматически. Откат кода не откатывает базу: схема меняется только добавлением, а восстановление backup теряет заказы после обновления, поэтому оно только ручное.

## Разовая настройка (владелец, одна команда)

Скрипт [vps_bootstrap.sh](../scripts/vps_bootstrap.sh) делает всё сам и **не перезапускает бота**,
не читает `.env`, повторный запуск безопасен. `<COMMIT>` — коммит `main`, который назовёт агент.

1. На сервере (под `ubuntu`):
   ```bash
   curl -fsSLo /tmp/fpf-bootstrap.sh https://raw.githubusercontent.com/Vetal12350/FunPayFlow/<COMMIT>/scripts/vps_bootstrap.sh
   sudo bash /tmp/fpf-bootstrap.sh --ref <COMMIT>
   ```
   Скрипт проверяет: одна `funpayflow.service`, `funpay-bot.service` не активна, нет drop-in,
   `FUNPAY_BOT_DATA_DIR` задан, бот на Python 3.13, x86_64. Затем: пользователь
   `funpayflow-deploy` с forced command, агент того же коммита, конфиг, правило sudo
   (проверено `visudo`), текущий код регистрируется как первый релиз (цель отката даже
   для первого обновления), в юните меняется **только** `ExecStart` на
   `/opt/funpayflow/current/venv/bin/python` (копия `*.before-autoupdate`), `daemon-reload`.
   В конце печатает 4 секрета.
2. **GitHub** → Settings → Environments → `production` (создать): Deployment branches —
   только `main`. Environment secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_KNOWN_HOSTS`,
   `DEPLOY_SSH_KEY` — скопировать из вывода. Required reviewers не включать
   (решение владельца — merge), включить, если merge делает не только владелец.
3. Удалить приватный ключ с сервера командой, которую напечатал скрипт.
4. Первое обновление — когда владелец рядом с ПК (первый перезапуск на новом пути).

Откат настройки: `sudo cp <unit>.before-autoupdate <unit> && sudo systemctl daemon-reload`.

## Разбор `NEEDS_OWNER`

Агент больше ничего не делает. Проверить `systemctl status funpayflow.service`, куда указывает `current`, при необходимости вручную `ln -sfn` на рабочий релиз и `restart`. После восстановления удалить `jobs/<sha>.json` с этим состоянием — только тогда агент примет новую операцию. Логи и `.env` в GitHub/чат не публиковать.

## Остаточные риски

- Ключ CI даёт только эти три команды, но любой, кто получит его **и** сможет собрать архив, запустит свой код в службе. Ключ хранится только в environment `production` и доступен только workflow из `main`. Без Required reviewers обновить может любой, кто способен запустить workflow в репозитории: держите доступ на запись только у себя и merge делайте сами.
- Служба по историческим данным работает от root; перевод на отдельного пользователя — отдельный шаг, не требуется для автообновления.
- Ручной `rollback` выполняется в SSH-сессии; при обрыве во время него итог проверять по `status`.
- `actions/upload-artifact@v4` / `download-artifact@v4` закреплены тегом, не SHA; закрепить при ревью.
