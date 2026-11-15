# Автообновление VPS: устройство и разовая настройка владельцем

Issue #156. Код: [deploy_agent.py](../scripts/deploy_agent.py), [build_deploy_release.py](../scripts/build_deploy_release.py), [deploy.yml](../.github/workflows/deploy.yml), heartbeat в [heartbeat.py](../src/funpayflow/heartbeat.py). Тесты офлайн: `tests/test_deploy_agent.py`, `tests/test_deploy_workflow.py`, `tests/test_heartbeat.py`.

**Этот документ не разрешает SSH, deployment или изменение production.** Шаги ниже выполняет владелец сам; см. [PRODUCTION](PRODUCTION.md).

## Как работает обновление

1. Запуск: телефон (GitHub → Actions → *Deploy to VPS* → Run workflow) или ПК (`gh workflow run deploy.yml -f sha=<sha> -f action=deploy`). Указывается точный SHA текущего `main`.
2. `verify` (без секретов): SHA = голова `main`, CI `offline-tests` зелёный, pytest + compileall, сборка wheels по хешам `uv.lock` (FunPayAPI собирается из проверенного sdist здесь, не на сервере), `release.tar` + digest.
3. `deploy` ждёт **одного подтверждения владельца** в environment `production` (видны SHA и digest).
4. SSH один раз отправляет `deploy <sha> <digest>` + архив. Агент проверяет digest, запускает отдельный worker (`systemd-run`) и отвечает `ACCEPTED`. Дальше CI только читает `status`.
5. Worker: распаковка и проверка wheels → venv `releases/<sha>` → `pip install --no-index --only-binary` → проверки (legacy-служба выключена, unit без drop-in и указывает на `current`) → backup SQLite (backup API + integrity_check) → атомарный `current → releases/<sha>` → `systemctl restart` → здоровье (служба active, без автоперезапусков 20 с, `health.json` с этим SHA и `runner=ok`).
6. Нездоров → автоматический откат на предыдущий релиз. Откат не удался → `NEEDS_OWNER`: новые операции блокируются до ручного разбора.

| Ситуация | Поведение |
| --- | --- |
| Обрыв SSH при отправке | `deploy` не повторяется; CI опрашивает `status`. `UNKNOWN_JOB` = не принято, можно запустить снова |
| Нет ответа 10 минут | Итог UNKNOWN, job красный. Не перезапускать deploy; смотреть `status` |
| Повторный запуск / параллельный | `BUSY` или `ALREADY_DEPLOYED`; GitHub `concurrency` не отменяет идущий |
| Прерванный worker | Никогда не продолжается: до переключения → `FAILED` (production не тронут), после → `NEEDS_OWNER` |
| Ручной откат | Workflow с `action=rollback` и SHA, который сейчас установлен; тоже через подтверждение |

Хранятся последние 3 релиза; backups в каталоге `backups` не удаляются автоматически. Откат кода не откатывает базу: схема меняется только добавлением, а восстановление backup теряет заказы после обновления, поэтому оно только ручное.

## Разовая настройка (владелец, вручную)

Пути — пример; используйте фактические. Значения секретов никуда не копируйте, кроме указанных мест.

1. **Проверить сервер** по [PRODUCTION](PRODUCTION.md) «Что заново проверить»: одна служба `funpayflow.service`, `funpay-bot.service` выключена (`disable --now`), архитектура x86_64, есть Python 3.13 с модулем `venv`.
2. **Пользователь для SSH** без пароля и прав: `sudo adduser --disabled-password --gecos "" funpayflow-deploy`.
3. **Агент** (root-owned, после ревью кода из `main`):
   `sudo install -o root -g root -m 0755 scripts/deploy_agent.py /usr/local/sbin/funpayflow-deploy-agent`
4. **Конфиг** `/etc/funpayflow-deploy/config.json` (root, 0600):
   ```json
   {"root": "/var/lib/funpayflow-deploy", "releases": "/opt/funpayflow/releases",
    "current": "/opt/funpayflow/current", "data_dir": "/root/FunPayFlow-data",
    "backups": "/var/backups/funpayflow", "service": "funpayflow.service",
    "forbidden_services": ["funpay-bot.service"], "python": "/usr/bin/python3.13",
    "keep_releases": 3, "health_timeout": 180, "health_stable": 20,
    "require_heartbeat": true, "min_free_mb": 1024}
   ```
   `data_dir` — фактический `FUNPAY_BOT_DATA_DIR` службы.
5. **sudo** (`sudo visudo -f /etc/sudoers.d/funpayflow-deploy`), ровно:
   ```
   Defaults!/usr/local/sbin/funpayflow-deploy-agent env_keep += "SSH_ORIGINAL_COMMAND"
   funpayflow-deploy ALL=(root) NOPASSWD: /usr/local/sbin/funpayflow-deploy-agent ""
   ```
6. **Ключ только для CI**: на своём ПК `ssh-keygen -t ed25519 -N "" -C funpayflow-deploy -f funpayflow_deploy`. Публичную часть — в `/home/funpayflow-deploy/.ssh/authorized_keys` (владелец пользователь, 0600):
   `restrict,command="sudo -n /usr/local/sbin/funpayflow-deploy-agent" ssh-ed25519 AAAA... funpayflow-deploy`
   Личный ключ `ubuntu` не использовать. Приватную часть после шага 8 удалить с ПК.
7. **Unit на `current`**: в `/etc/systemd/system/funpayflow.service` заменить путь кода на `/opt/funpayflow/current` (`WorkingDirectory=/opt/funpayflow/current`, `ExecStart=/opt/funpayflow/current/venv/bin/python -m funpayflow.main`), затем **только** `sudo systemctl daemon-reload` (без restart: работающий процесс остаётся на старом коде до первого обновления). Старый каталог кода не удалять — это ручной откат первого обновления.
8. **GitHub** → Settings → Environments → `production`: Required reviewers = вы; Deployment branches = только `main`. Секреты environment (не repository):
   `DEPLOY_SSH_KEY` (приватный ключ из шага 6), `DEPLOY_HOST`, `DEPLOY_USER=funpayflow-deploy`, `DEPLOY_KNOWN_HOSTS` (строка из `ssh-keyscan -t ed25519 <host>`, сверенная с отпечатком на сервере `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`).
9. **Первое обновление**: Run workflow с SHA текущего `main`, подтвердить. Предыдущего релиза у агента ещё нет, поэтому при сбое будет `NEEDS_OWNER`: вернуть старый путь в unit, `daemon-reload`, `restart`, удалить `/var/lib/funpayflow-deploy/jobs/<sha>.json`.

## Разбор `NEEDS_OWNER`

Агент больше ничего не делает. Проверить `systemctl status funpayflow.service`, куда указывает `current`, при необходимости вручную `ln -sfn` на рабочий релиз и `restart`. После восстановления удалить `jobs/<sha>.json` с этим состоянием — только тогда агент примет новую операцию. Логи и `.env` в GitHub/чат не публиковать.

## Остаточные риски

- Ключ CI даёт только эти три команды, но любой, кто получит его **и** сможет собрать архив, запустит свой код в службе. Ключ хранится только в environment `production`, доступен лишь после вашего подтверждения.
- Служба по историческим данным работает от root; перевод на отдельного пользователя — отдельный шаг, не требуется для автообновления.
- Ручной `rollback` выполняется в SSH-сессии; при обрыве во время него итог проверять по `status`.
- `actions/upload-artifact@v4` / `download-artifact@v4` закреплены тегом, не SHA; закрепить при ревью.
