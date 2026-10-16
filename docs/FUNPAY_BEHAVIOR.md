# FunPay: подтверждённые инварианты

Проверено по `76033af` на 2026-10-16. Главные источники: [funpay.py](../src/funpayflow/funpay.py), [main.py](../src/funpayflow/main.py), [state.py](../src/funpayflow/state.py), [telegram.py](../src/funpayflow/telegram.py). Это описание текущего кода, не гарантия внешнего API.

## Polling и recovery

Один `FunPayClient` владеет одним Account и одним Runner producer thread. `start_runner()` не создаёт второй активный producer. После завершения того же producer его повторный старт отвергается; transient recovery происходит внутри `listen_events`, без пересоздания Account/Runner.

`get_updates()` и `parse_updates()` выполняются под общим Account RLock. Normal poll interval — не менее 15 секунд. Классификатор `_classify_runner_error` применяется к этому polling flow; его нельзя механически переносить на modifying HTTP.

| Ошибка polling | Текущее поведение |
| --- | --- |
| requests Timeout/ReadTimeout/ConnectTimeout/ConnectionError; transient SSLError | RECOVERABLE, если не обнаружен известный certificate verification failure |
| Поддержанные urllib3 timeout/protocol/SSL и типизированный MaxRetryError.reason; TimeoutError/reset/aborted/broken pipe | Явная классификация типов; произвольный wrapper не считается transient по наличию текста |
| RequestFailedError HTTP 408/429/500/502/503/504 | RECOVERABLE; не весь диапазон 5xx, 501 остаётся UNKNOWN |
| UnauthorizedError, HTTP 401/403 | AUTH, прекращение producer с сохранением fatal supervision path |
| Известный certificate verification / hostname mismatch | FATAL; не отключать TLS verification |
| Queue overflow, перечисленные внутренние ошибки типов/инвариантов | FATAL |
| Неизвестное исключение | UNKNOWN, без автоматического recovery |

Certificate detection ограниченно обходит типизированные причины/args/context и использует известные markers как fallback; arbitrary exception text не становится основой общего retry classifier. Сырые строки ошибок/HTTP/headers не логируются: `_safe_runner_error_detail` возвращает фиксированные диагностические labels.

Recoverable backoff: **5 → 10 → 20 → 40 → 60 → 60… секунд**, без лимита «8 попыток». Ожидание через stop-aware `Event.wait`; нет tight loop. Успешный poll сбрасывает consecutive errors и возвращает health в `healthy`. Успех другой операции не сбрасывает polling counter. В терминальной non-recoverable ветке counter также обнуляется перед выходом — это не успешное восстановление.

На transient ошибки health становится `backoff`; очередь и SQLite backlog остаются. Consumer и Telegram продолжают работать. Настоящая гибель producer приводит к `notifications_loop` RuntimeError и остановке supervision; не подавлять этот путь как будто любая ошибка временная.

Граница: первоначальный `initialize_account()` в `main()` не имеет этого бесконечного retry-loop. Failure завершает startup; recoverable startup failure не вызывает технической Telegram-рассылки. Это не обещание автономной начальной авторизации при любой недоступности сети.

## Persistent autobump

`main.auto_bump_loop` использует тот же Account. Сохранённый ручной ON/OFF сохраняется при restart; новая установка имеет default ON. Transient сбой, UNKNOWN и recovery не записывают OFF. SAFE_MODE, restart request и известный Runner backoff блокируют автоматические действия, не меняя предпочтение пользователя.

Цель поднятия — **game/category ID**, а не list index и не отдельный Telegram item. `bump_lots` строит snapshot категорий текущего профиля, исключая currency-subcategories; `account.raise_lots(game_id)` поднимает разделы соответствующей категории.

В одной SQLite таблице `autobump_progress` сохраняются `game_id`, `cycle_id`, `position`, `attempt_id`, `state`, `eligible_at`. Position задаёт порядок внутри snapshot; identity — `game_id`. Следующий pending target определяется сохранёнными состояниями. Уже подтверждённые элементы не повторяются из-за изменения порядка профиля/restart. Новые категории попадают в следующий snapshot; исчезнувший pending target помечается skipped.

```text
pending -> claimed (commit ДО dispatch lots/raise)
        -> success / cooldown / ambiguous
```

Claim и результат сохраняются короткими транзакциями; network send не находится внутри SQLite-транзакции. После завершения snapshot `cycle_id`/`position` очищаются, но cooldown deadlines остаются. Успех имеет существующий cooldown 7200 секунд. Положительный `RaiseError.wait_time` задаёт реальный cooldown; без него допускается только существующий узкий parser старого «Подождите N единиц». Неизвестный RaiseError не считается доказанным success/cooldown.

При ручном OFF progress замораживается. Следующий ручной ON начинает новый snapshot, сохраняя deadlines уже начатых/завершённых запросов. Thread-safe setting/version и повторные проверки после подготовки/claim не дают старому OFF→ON worker продолжить прежний проход. Проверяемую отмену **до dispatch** можно вернуть из claimed в pending.

## UNKNOWN — не blind retry

Timeout/TLS/connection failure после начала `lots/raise` не доказывает, что FunPay не выполнил изменение. Текущая консервативная семантика сохраняется и для ConnectTimeout после transport boundary.

- Target атомарно получает `ambiguous` и deadline не ранее текущего времени + 7200 секунд; настройка autobump остаётся ON.
- Worker делает stop-aware паузу 60 секунд; остальные pending категории продолжаются позже, когда известный Runner backoff закончился.
- Этот target нельзя немедленно повторять после reconnect или в новом snapshot до `eligible_at`.
- При restart незавершённый claimed переводится в ambiguous с безопасным окном не менее 7200 секунд **от восстановления**. Даже claim перед фактически неотправленным запросом после crash может дать консервативный пропуск.
- После normal interval target снова может попасть в обычный цикл. Это ограничение повторов в unsafe window, не доказательство remote outcome и не вечная exactly-once гарантия.
- Недоступная SQLite/ошибка обязательной записи — StateError/fatal path; нельзя отправить HTTP без durable claim. Уже сохранённый claimed защищает следующий запуск при failure финальной записи.

Не обходить existing late action gate, общий Account RLock и [runtime_control](../src/funpayflow/runtime_control.py). Уже начатый HTTP нельзя надёжно отменить. Часы `eligible_at` — wall clock: корректное системное время важно для resume.

## События, UI и observability

Критические snapshots попадают в SQLite до RAM publication; replay использует modifying claims. Не очищать backlog/queue ради reconnect, не переносить review fingerprints в volatile-only дедупликацию. Night replies защищены от self-echo; review requests имеют отдельный persistent claim. Для изменения этих путей читать соответствующие функции и тесты отдельно.

Технические reconnect/recovery/startup/обычная stop-рассылки **удалены**, а не только coalesced. Ошибки профиля/pretransport не отправляются как успешный bump report. Локальные логи, health/status и retry counters сохраняются; successful poll recovery логируется.

Критическое завершение runtime/авторизация/сертификат/SQLite может вызвать `_send_runtime_notice`: одна ограниченная по времени попытка на получателя, failure Telegram не заменяет первичную ошибку. `claim_critical_notice` хранит последнюю фиксированную категорию и timestamp в atomic settings, подавляя повтор этой категории на 30 минут, в том числе между процессами. Другая категория заменяет единственный marker: отдельной истории таймеров для всех категорий нет. Если сохранение settings недоступно, долговременная dedup не гарантируется. До проверки обязательной конфигурации/создания Bot отправка уведомления также не гарантируется.

Business notifications — заказы, сообщения, отзывы, подтверждённые поднятия — сохраняются с существующими персональными настройками. Сообщение о ручном переключении autobump остаётся пользовательской обратной связью.

## Исторический контекст и текущие отличия

| Ранее упоминавшаяся семантика | Подтверждено сейчас |
| --- | --- |
| SSLError fatal / предел 8 recoverable попыток | Исправлено: transient SSL recoverable, capped indefinite retry; certificate failures отдельно |
| Курсор и cooldown только в RAM | SQLite `autobump_progress` уже реализован |
| UNKNOWN выключает autobump / startup всегда включает | Больше не так: stored preference сохраняется; UNKNOWN откладывает target |
| Yellow/green reconnect coalescing | Рассылки удалены; 30-minute dedup относится к critical категориям |
| «Ограниченные повторы» в кратком README | Это не спецификация polling budget: см. фактический backoff выше |

Regression entry points: `tests/test_autobump_resume.py`, `test_autobump_startup.py`, `test_transient_funpay_recovery.py`, `test_runtime_core.py`, `test_runtime_restart.py`, `test_operations_center.py`.
