# QA-автотестировщик FunPayFlow (офлайн)

Это автоматический запуск проверяемых сценариев, а не ИИ с доступом к реальному
Telegram/FunPay. Он воспроизводит действия владельца через Telegram-обработчики,
используя имитации ответов FunPay и временную SQLite. Не требует `.env`,
production-ключей, второго экземпляра бота или публикации лотов.

## Что проверяется

- GAME_KEY: создать локальный черновик Cyberpunk/Elden Ring по синтетической форме.
- Порядок Telegram-кнопок, независимость двух полей с одинаковым названием.
- Количество и цена, Preview → Save → Reopen → Create Similar → Save.
- Изменение одного поля клона без изменения исходного черновика.
- GET-only чтение формы, безопасная диагностика, старые кнопки, неудачный Refresh,
  совместимость старых черновиков и варианты из существующих regression-тестов.

**Ограничение:** синтетические HTML-fixtures не гарантируют совместимость с
текущей живой формой FunPay. Для этого понадобится отдельный разрешённый
read-only live smoke через существующую авторизованную сессию бота.
Проверки ничего не публикуют и не изменяют на FunPay.

## Запуск

```bash
uv sync --locked --dev
uv run --no-sync python scripts/qa_smoke.py --suite product
uv run --no-sync python scripts/qa_smoke.py --suite core
uv run --no-sync python scripts/qa_smoke.py --suite full
```

Результаты: `.qa-reports/summary.md` (читаемый отчёт) и
`.qa-reports/results.xml` (локальный JUnit-файл; не выкладывать сырой отчёт,
если там могут оказаться приватные данные). Команда завершается с ошибкой,
если pytest упал, превысил лимит времени, не выдал тесты или отсутствуют файлы.

В режиме **core** дополнительно проверяются независимые критичные контракты:
autobump resume/startup/schedule, polling/network recovery, runtime restart,
order history, sales analytics, historical import и communication automation.
Каждая подсистема получает отдельную строку отчёта PASS/FAIL/SKIP.
При ошибке теста job завершается FAIL; нулевой набор тестов тоже FAIL.

Workflow **Product QA** автоматически запускает обе независимые QA job
(product user journeys и core reliability) для релевантных Pull Request.
Также его можно запустить вручную через **GitHub → Actions → Product QA →
Run workflow** и выбрать `product`, `core` или `full`. В workflow только
`contents: read`, без секретов; отчёт выводится в GitHub Actions Summary.
Существующий CI не меняется.

Автотестировщик выполняет написанные сценарии, а не генерирует новые сам.
При появлении новой функции следует добавить её пользовательский тест в этот
набор. Не добавлять в workflow реальные операции изменения лотов.
