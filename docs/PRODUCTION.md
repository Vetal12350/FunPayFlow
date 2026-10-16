# Production: границы и исторический snapshot

**Production actions require explicit user approval.** Этот документ не разрешает SSH, deployment, backup production, restart/stop служб, изменение данных, commit/push/merge, tag или release. Получи прямой запрос на соответствующую операцию; обычная разработка и чтение этого файла не являются разрешением.

Опубликованный tag/Release `v1.0.0` неизменяем: не пересоздавать и не заменять assets. Для общего процесса установки/обновления используй существующие [VPS_INSTALL](VPS_INSTALL.md) и [RELEASE_CHECKLIST](RELEASE_CHECKLIST.md); здесь зафиксированы проектные ограничения без дублирования пошаговой установки.

## Исторический snapshot владельца — 2026-10-13

Следующие сведения переданы владельцем, **не проверены текущим подключением к VPS** и не являются гарантией состояния на сегодня:

| Назначение | Последнее известное значение |
| --- | --- |
| NEW code | `/root/FunPayFlow-next` |
| Private data | `/root/FunPayFlow-data` |
| Unit | `/etc/systemd/system/funpayflow.service` |
| NEW service | `funpayflow.service` |
| OLD installation, retained for rollback | `/root/SecureBot` |
| OLD service | `funpay-bot.service` |

Старая установка была оставлена для rollback. Наличие каталогов/служб, их актуальные commits, backups и факт текущей активности кодом репозитория не подтверждены. Эти root-пути — историческая owner-specific схема, не рекомендация запускать новую установку от root: tracked installer рассчитан на обычного пользователя, sudo используется для systemd-интеграции.

## Что заново проверить перед разрешённым deployment

1. Уточнить у владельца целевую машину, release/commit и допустимый scope. Самостоятельно не подключаться «только проверить».
2. Проверить состояния **обоих** units, реальные процессы и число экземпляров; определить фактические code/data paths, WorkingDirectory, пользователя, ExecStart и текущий commit. Не выводить EnvironmentFile/значения environment.
3. Проверить наличие конфигурации и permissions без чтения секретных значений; соответствие пользователя службы каталогу данных. Runtime тоже сверяет `FUNPAY_USER_ID` с Account.
4. Найти/проверить backups и rollback plan. Для согласованной копии SQLite использовать остановленных writers или SQLite backup API; копирование только `state.sqlite3` при активном WAL не является надёжным backup.
5. Проверять SQLite integrity на безопасной согласованной копии; не писать в production ради проверки миграции. Отдельно проверить совместимость state/settings с откатываемым кодом — наличие старого каталога ещё не доказывает безопасный rollback.
6. До переключения гарантировать один процесс с конкретными FunPay/Telegram credentials. OLD и NEW нельзя запускать одновременно. `bot.lock` в одном data directory не защищает от второй установки с другим каталогом.
7. Запускать/останавливать/переключать службы только в согласованном порядке; после этого проверить один процесс, правильный commit/data directory, health и безопасные логи. Не публиковать приватные journal/application logs.

## Что подтверждено tooling репозитория

- [linux/install.sh](../linux/install.sh) принимает `--code-dir`/`--data-dir`; проверяет раздельность каталогов, задаёт data permissions `700`, `.env` — `600`, выполняет locked uv install/setup.
- `--systemd` устанавливает unit и выполняет `enable --now`: это **изменяющее production действие**, не диагностическая команда.
- [render_service.py](../src/funpayflow/render_service.py) валидирует/экранирует пути шаблона [funpayflow.service.in](../linux/systemd/funpayflow.service.in).
- Шаблон использует `python -m funpayflow.main`, EnvironmentFile и `FUNPAY_BOT_DATA_DIR`, SIGTERM, `TimeoutStopSec=40`, `Restart=on-failure`, `RestartSec=10`.
- systemd restart остаётся crash protection. Временный active Runner outage восстанавливается в процессе; startup failure, auth/fatal/storage failures — отдельные случаи. См. [FUNPAY_BEHAVIOR](FUNPAY_BEHAVIOR.md).
- Public Windows Setup/Start используют `%LOCALAPPDATA%\FunPayFlow`, fallback `%USERPROFILE%\AppData\Local\FunPayFlow`, либо явный `FUNPAY_BOT_DATA_DIR`; Linux installer предлагает `~/.local/share/funpayflow`. Не подменять эти defaults историческими VPS-путями.

Не сохранять в документацию содержимое `.env`, tokens, cookies, session data, private keys, приватные экспорты или реальные пользовательские записи. Для отчёта достаточно paths без секретных значений, commits, количества процессов и результатов проверок.
