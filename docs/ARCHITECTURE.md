# Практическая карта FunPayFlow

Карта проверена по `76033af` (2026-10-16). Git и текущая реализация имеют приоритет над этим snapshot. Для сетевых инвариантов см. [FUNPAY_BEHAVIOR](FUNPAY_BEHAVIOR.md), для команд — [DEVELOPMENT](DEVELOPMENT.md).

## Где искать изменение

Пути в таблице относительны `src/funpayflow/`.

| Ответственность | Файлы / основные точки |
| --- | --- |
| Запуск и supervision | [main.py](../src/funpayflow/main.py): `run`, `_application_loop`, `main`, `_supervise_tasks` |
| FunPay Account, Runner, HTTP | [funpay.py](../src/funpayflow/funpay.py): `FunPayClient`, `listen_events`, `bump_lots` |
| Bump report rendering / chunk limit | `bump_reports.py`: pure logical-entry HTML rendering; delivery в `main.auto_bump_loop`, без изменения raise mechanics |
| Telegram UI, авторизация, callbacks, настройки | [telegram.py](../src/funpayflow/telegram.py): dispatcher `dp`, keyboards, `load_settings`, `save_settings`, module gates |
| События, replay и безопасные payload | [runtime_events.py](../src/funpayflow/runtime_events.py): `ActionEvent`, `QueuedCriticalEvent`, `critical_snapshot`, `hydrate_critical` |
| SQLite и миграции | [state.py](../src/funpayflow/state.py): `ReviewReceiptStore`, `StateError` |
| Product templates / routes / local drafts | [PRODUCT_TEMPLATES](PRODUCT_TEMPLATES.md); `product_templates.py`, `product_variants.py`, `game_routes.py`, `game_catalog.py` (lazy scoped memory TTL), `product_store.py`, `product_drafts.py`, `telegram_products.py`; та же SQLite, без publish и постоянного каталога |
| Restart и граница автоматического действия | [runtime_control.py](../src/funpayflow/runtime_control.py): `claim_restart`, `automatic_action_gate` |
| Встроенные модули | [feature_registry.py](../src/funpayflow/feature_registry.py): definitions, profiles, validation |
| Каталоги приватных данных | [runtime_paths.py](../src/funpayflow/runtime_paths.py) |
| Логи и фильтр FunPayAPI | [logger.py](../src/funpayflow/logger.py) |
| Официальный экспорт продаж, CLI engine | [import_funpay_sales.py](../src/funpayflow/import_funpay_sales.py); Telegram вызывает тот же engine |
| Installer / консоль RU/EN | [setup_config.py](../src/funpayflow/setup_config.py), [console_ui.py](../src/funpayflow/console_ui.py) |
| systemd unit из шаблона | [render_service.py](../src/funpayflow/render_service.py), [linux/](../linux/) |

## Запуск и runtime

Entry point в [pyproject.toml](../pyproject.toml): `funpayflow = funpayflow.main:run`. Поддержан также `python -m funpayflow.main`; корневого `main.py` нет.

1. `run()` удерживает OS process lock `bot.lock` на протяжении `_application_loop`.
2. `main()` определяет fresh/legacy установку и effective modules; проверяет обязательную конфигурацию, создаёт один `FunPayClient` и Telegram `Bot`.
3. Инициализируется Account; `FUNPAY_USER_ID` сверяется с авторизованным аккаунтом. Затем создаётся/проверяется SQLite и восстанавливается autobump progress.
4. `_supervise_tasks` запускает Telegram polling, consumer `notifications_loop`, refresh сессии; autobump и withdrawal polling добавляются по effective modules.
5. Consumer запускает единственный Runner producer в отдельном потоке. Остальные блокирующие Account-операции из async-кода выносятся через `asyncio.to_thread`; общий Account RLock сериализует доступ. Не добавляй независимый Account ради нового worker.
6. Настоящая ошибка критической задачи завершает supervision. При штатном restart останавливаются задачи и Runner, закрывается Telegram session; executor старого runtime полностью дренируется перед новым Account. OS lock остаётся у процесса.

Import `main.py` загружает dotenv, import Telegram загружает settings. Для исследования читай исходники; не импортируй runtime только ради introspection.

## События и persistence

Runner вызывает `get_updates`/`parse_updates`, затем публикует события в bounded RAM queue (256). Критические order/review snapshots сначала сохраняются в `critical_event_backlog`. Consumer помечает processing/done, использует persistent claim перед изменяющим действием и восстанавливает pending backlog. Переполнение/неисправность хранилища не превращаются в бесконечное бесконтрольное накопление памяти.

`ReviewReceiptStore` использует одну `state.sqlite3`: WAL, busy timeout, транзакции, проверку schema/integrity и идемпотентные расширения в `initialize()`. Основные группы данных: заказы и status observations; review observations/fingerprint receipts; review requests; withdrawals/legacy statistics; audit; critical backlog; `autobump_progress`. Существующий локальный `stats_log.json` может импортироваться при инициализации — это приватные данные.

Это не end-to-end exactly-once гарантия удалённых операций: crash после HTTP и до фиксации результата требует UNKNOWN-семантики. Не держать SQLite-транзакцию во время HTTP. Подробности claim/resume — в [FUNPAY_BEHAVIOR](FUNPAY_BEHAVIOR.md).

## Telegram, настройки и данные

`telegram.py` объединяет UI, проверки авторизации/rate limit, module gates, global/personal settings, состояние диалогов и health snapshot. Нижняя reply-клавиатура содержит «🛠 Главное меню»; управление функциями — inline. Перезапуск является действием управления runtime; отдельной Shutdown-кнопки нет.

`bot_settings.json` сохраняется через временный файл, flush/fsync и `os.replace`; `required=True` делает failure явным. Global switches не равны effective module configuration: изменение набора модулей применяется после restart. Мастер новой установки предварительно выбирает все десять модулей; до завершения setup effective modules отключены. Night mode/review request имеют отдельные выключатели. Fresh SAFE_MODE — OFF; сохранённый legacy выбор сохраняется. Сохранённый `auto_bump` тоже сохраняется, включая OFF.

Валюты: `order.currency` историческая; `primary_currency` — предпочтение пользователя; analytics filter может охватывать одну валюту или все с раздельными итогами. Конвертации и сложения смешанных валют нет. Hidden review observations сохраняются, но `review_hidden=1` исключён из пользовательской review analytics; NULL сохраняет legacy-поведение.

`FUNPAY_BOT_DATA_DIR` задаёт абсолютный каталог данных. Без переменной source checkout использует корень проекта, установленный пакет — рабочий каталог. Windows Setup/Start задают внешний per-user каталог; Linux installer предлагает внешний каталог. Lock ограничивает экземпляры **одного каталога данных**, а не всех процессов с одинаковыми credentials.

## Логирование и зависимости

Application logger пишет консоль и `logs/bot_YYYY-MM-DD.log`; DEBUG включается отдельно. Файловый sanitizer и allowlist logging FunPayAPI — дополнительная защита, не разрешение передавать сырые exceptions/response/credentials в лог.

Основные зависимости: aiogram (Telegram), FunPayAPI + requests/BeautifulSoup (FunPay), python-dotenv, Rich (installer UI); SQLite — стандартная библиотека. Точные версии берутся из `uv.lock`, не из нижних границ `pyproject.toml`.

Зависимости слоёв: `main` координирует Telegram + FunPay + state; Telegram вызывает FunPay/state; FunPay использует state/events/control; state хранит локальные данные и не является HTTP-клиентом. `runtime_paths` используется всеми владельцами приватных файлов. Это карта существующих границ, не предложение разнести слои новым рефакторингом.
