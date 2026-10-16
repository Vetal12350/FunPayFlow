# Разработка и проверки

Команды выполняются из корня source checkout. Требуются `uv` и Python 3.13+; entry point и зависимости заданы в [pyproject.toml](../pyproject.toml). Проверенный pipeline находится в [ci.yml](../.github/workflows/ci.yml).

## Подготовка и Git

```text
git status --short
git branch --show-current
git log -1 --oneline
uv sync --locked --dev
```

Sync может обращаться к package registry. Если сеть запрещена и всё уже закешировано, поддерживается `uv sync --offline --locked --dev`; отсутствие cache нужно сообщить, а не менять lock ради обхода. Сохраняй чужие изменения; подходящую чистую worktree можно переиспользовать после проверки её владельца/состояния. Commit/push/merge и разрушительные операции требуют прямого разрешения пользователя.

## Проверки без реальных аккаунтов

В проекте pytest запускает в том числе `unittest.TestCase`/`IsolatedAsyncioTestCase`. Используются mocks/fakes, временные каталоги/SQLite, подмена clock/sleep, thread barriers и AST-извлечение отдельных функций `main.py`. `tests/log_isolation.py` направляет application logs во временный каталог. Fixtures — синтетические; реальные settings, экспорт, логи или БД не нужны.

Выбери область, а не запускай весь suite после каждого редактирования:

| Область | Первые релевантные тесты в `tests/` |
| --- | --- |
| Autobump, cursor, toggle, UNKNOWN | `test_autobump_resume.py`, `test_autobump_startup.py` |
| Polling, TLS, backoff | `test_transient_funpay_recovery.py` |
| Supervision/restart/backlog | `test_runtime_restart.py`, `test_runtime_core.py` |
| Telegram, модули, health | `test_modular_features.py`, `test_operations_center.py`, `test_telegram_communication.py` |
| Product templates / routes / bulk drafts | `test_product_templates.py`, `test_product_variants.py`, `test_product_variant_integration.py`, `test_game_routes.py`, `test_product_store.py`, `test_product_drafts.py`, `test_telegram_products.py`, `test_product_compatibility.py` |
| Reviews/automation | `test_pre_live_integration.py`, `test_communication_automation.py` |
| Заказы/аналитика/import | `test_order_history.py`, `test_sales_analytics.py`, `test_historical_sales_import.py`, `test_telegram_sales_import.py` |
| Установка/релиз | `test_public_installation.py`, `test_public_release_foundation.py`, `test_release_package.py` |

Например, для recovery/autobump:

```text
uv run --no-sync pytest -q tests/test_autobump_resume.py tests/test_autobump_startup.py tests/test_transient_funpay_recovery.py
```

После code changes — relevant targeted tests, затем полный suite и проверки затронутой packaging/install части:

```text
uv run --no-sync pytest -q tests
uv run --no-sync pytest -q tests/test_release_package.py tests/test_public_installation.py
uv run --no-sync python -m compileall -q src/funpayflow tests scripts
uv run --no-sync python tests/packaging_smoke.py
git diff --check
git diff --stat
git diff
git status --short
```

В Linux CI дополнительно выполняется `bash -n linux/install.sh`. `tests/packaging_smoke.py` офлайн собирает wheel/sdist во временном каталоге, проверяет приватность, устанавливает wheel и импортирует его вне checkout; production runtime не запускается. Отдельных настроенных Ruff/Black/mypy/lint jobs сейчас нет — не придумывай обязательный lint-command.

Новые network tests не должны создавать лоты, отправлять сообщения или вызывать реальные HTTP endpoints. Моделируй успешный ответ, failure до/после dispatch, cancellation и повторный запуск с той же временной БД. Для SQLite проверяй результат новой connection/store instance, а не только RAM. Не ослабляй старый assertion ради зелёного результата: сначала отличи новую согласованную семантику от regression.

Для docs-only правок достаточно проверки фактов, ссылок, scope и whitespace; полный runtime suite без изменения кода обычно не нужен. `git diff` не показывает untracked docs: их нужно проверить отдельно. В отчёте укажи реальные команды/count и skips/непроверенные платформы; не переноси старый PASS в новый отчёт.

## Ручной запуск приложения — отдельная разрешённая операция

Следующие команды **не являются офлайн-проверками**. Они настраивают приватные данные или запускают реальные FunPay/Telegram соединения. Выполнять только по явному запросу на локальный запуск, с отдельным разрешённым аккаунтом/каталогом, без второго процесса с теми же credentials.

```text
uv run --no-sync python -m funpayflow.setup_config --data-dir <absolute-private-directory>
uv run --no-sync funpayflow
```

Альтернативный entry point: `uv run --no-sync python -m funpayflow.main`. Перед запуском задай **тот же** абсолютный `FUNPAY_BOT_DATA_DIR`, который передан setup: например, `$env:FUNPAY_BOT_DATA_DIR = 'C:\FunPayFlow-dev-data'` в PowerShell или `export FUNPAY_BOT_DATA_DIR=/absolute/private-directory` в shell. Без него source checkout использует проектный каталог. Не проси пользователя прислать значения секретов; ввод производится локально.

Публичная установка: [VPS_INSTALL](VPS_INSTALL.md) и README. Выпуск: существующий [RELEASE_CHECKLIST](RELEASE_CHECKLIST.md), [release.yml](../.github/workflows/release.yml), `scripts/build_release.py` (allowlist ZIP/audit/checksum). Release builder, tag/release и deployment не входят в обычную проверку задачи; опубликованный `v1.0.0` не пересоздавать.
