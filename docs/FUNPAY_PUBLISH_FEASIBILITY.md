# Создание лота FunPay: evidence и блокер

## NEW-form → envelope: только offline, 2026-10-27

`create_wire_contract.py` содержит чистую `assemble_new_request(PreparedLot,
NewFormFacts, now=...)`: без HTML parser, HTTP, Account, persistence или live switch.
Вызывающий код передаёт полный проверенный synthetic form inventory как tuple,
сохраняя duplicate controls; произвольный HTML и production forms не принимаются.
Профиль `synthetic-new-v1` требует точный fresh URL node 986, action offerSave,
POST/form encoding, совпадающие timestamp/fingerprint, currency/options и CSRF.
Blank `offer_id`, `SYNTHETIC_MANUAL`, `currency=USD/EUR/RUB`, пустые visibility
controls — **fixture facts**, не доказанные значения реальной NEW-формы.
Поля не угадываются: missing/duplicate/unknown/edit controls дают fixed error.
Envelope immutable, bounded, разделяет transient service и business tuples;
policy фиксирована: `lots/offerSave`, POST, form encoding, redirects=False,
max_attempts=1. Secret-bearing tuples предназначены только для памяти;
repr скрывает значения, сериализации/логирования/сохранения адаптер не выполняет.
Локально перечитанный FunPayAPI 1.1.0 подтверждает offerSave/form encoding и
`location=trade`, но его edit-only getter и save без ID не используются.
Cardinal source не загружался (offline); сторонний код не копировался.
Fake seam существует только в тестах: durable claim и safety gates не менялись;
success без independently verified exact NEW ID, redirect/replay остаются UNKNOWN.
Response parser отсутствует. Envelope не разрешает первую или повторную отправку.
Остаются live blockers: полный документ node 986, NEW identity, manual/currency/
visibility и session-CSRF binding, transport без replay, response ID и exact read-back.
Три SELECT остаются owner report, не verified full-document evidence. Следующий
шаг требует отдельного owner-approved read-only proof, затем отдельного write opt-in.
`CURRENT_STATE` оценён: production snapshot прежний; этот файл вне scope задачи.

## Publisher v1 — offline vertical slice, 2026-10-27

**Работает сейчас:** один сохранённый product draft `GAME_KEY` / Elden Ring,
category 274 / COMMON node 986 → проверенный business payload → синтетический
transport → exact-ID read-back → durable `CONFIRMED` либо `UNKNOWN`.
Реальная публикация остаётся **OFF**. Это реализация offline пути, а не новое
наблюдение FunPay или подтверждение create-контракта. Исторический аудит ниже
сохраняется; его рекомендация не добавлять каркас относится к прежнему этапу.

- [lot_publisher.py](../src/funpayflow/lot_publisher.py) использует существующий
  scoped `ProductStore` и сохранённые `NodeDraftService` drafts. Проверяет owner,
  account и экземпляр Account, draft/route/template revisions и snapshots,
  node/category/type, полный явный выбор options, fingerprint, возраст схемы
  0–300 секунд, RU/EN тексты, цену и quantity=1. Variant drafts пока отклоняются.
  Валюта отсутствует в существующем draft: в harness она явно задаётся при
  подготовке и должна совпасть с `FormContext`; нет подразумеваемой валюты или
  конвертации. Поддержаны только synthetic USD/EUR/RUB.
- Business allowlist не содержит identity, CSRF, goods, cookies или скрытых
  controls. `SYNTHETIC_NEW` / `SYNTHETIC_MANUAL` — **только mock-протокол**, не
  реальные значения `offer_id` / `auto_delivery`. `FormContext` с обычным
  response/cache/unverified provenance не допускается. Service controls живут
  только в памяти, не входят в prepared summary или SQLite.
- `SyntheticTransport.submit_once` всегда бросает `LIVE_DISABLED`; реализация
  заменяется только в тестах. Live Account отвергается ещё до чтения профиля.
  Ни HTTP-адаптера, ни settings/env switch, ни Telegram publish callback нет.
  `PreparedLot.summary()` возвращает только фиксированные безопасные метаданные.
  Telegram flow оставлен прежним: без доказанного form/currency контракта он
  не должен выдавать synthetic preparation за готовность реального предложения.
- В существующей SQLite добавлена только `lot_publish_attempts`, с UNIQUE scope
  `(owner_id, account_id, draft_id)`, **независимо от revision**. `BEGIN IMMEDIATE`
  повторно читает текущие scoped snapshots и фиксирует `UNKNOWN/CLAIMED` до
  границы transport. Первая и поздняя проверки используют общий Account RLock,
  runtime action gate и обязательный `late_check` (по умолчанию закрыт).
  Отмена/изменение после claim консервативно расходует попытку. Claim никогда
  не удаляется, не истекает и не становится retryable; crash уже оставляет
  UNKNOWN без recovery rewrite. Гарантия относится к одному saved draft ID;
  это не серверная идемпотентность и не дедупликация разных/клонированных drafts.
- Mock моделирует одну отправку в `lots/offerSave` с redirects=False и
  max_attempts=1. Missing/conflicting ID, redirect/resend, exception, timeout,
  crash или ошибка result commit не разрешают повтор. Сверяются точный новый
  ID, owner profile, category/node/type, все подготовленные business fields,
  manual mode и неизменность business snapshots остальных лотов через fake
  `get_user` / `get_lot_fields`. Нет поиска по title, edit/delete или rollback.
  Baseline существует только в памяти; после crash автоматического reconciliation
  нет. Таблица попыток хранит только scope, revision, timestamp, фиксированные
  state/reason и подтверждённый ID — без payload, HTML, токенов и exception text.

**Source evidence:** повторно прочитаны локальные locked FunPayAPI 1.1.0
`Account.method`, `get_lot_fields`, `save_lot`, `LotFields` и API профиля.
`get_lot_fields` читает существующий offer; `save_lot` не возвращает ID, а
`method` не предоставляет per-call запрет redirects. Поэтому они не используются
для отправки. `LotFields.lot_id` и `UserProfile.id` повторяют аргументы запроса:
это не независимая identity ответа. Harness дополнительно требует synthetic
`offer_id`/`node_id`/`user_id` в read-back; этот live-контракт тоже не доказан.
Cardinal main и его лицензия заново не получались: задача
выполняется offline. Сторонний код не копировался; прежняя ссылка на pinned
Cardinal source ниже — историческое evidence, не проверка текущего main.

**Перед отдельно одобренным owner experiment остаются конкретные proofs:**
точная NEW identity без заимствования edit identity; свежий create-CSRF и его
связь с Account/form; реальный набор полей/options node 986, currency и explicit
manual/visibility controls; HTTP transport без redirect/replay/retry; однозначный
response ID и свежий exact-ID read-back owner/node/business/manual состояния.
Три видимых SELECT и `candidate` этого не доказывают. Нужны независимый review
и отдельное разрешение владельца; этот код не содержит переключателя live test.

PR handoff: изменения ограничены новым publisher/test, additive таблицей в
`state.py` и двумя документами. `CURRENT_STATE` оценён, но не меняется: production
snapshot прежний, файл вне разрешённого scope. Commit/push/PR/merge/deploy здесь
не выполняются; PR создаёт trusted worker. Hosted Windows/Linux CI и Product/Core
QA остаются за worker.

Проверено 2026-10-21, source HEAD `683a101680fc8df0d02d7a5ee60c4113445713b2`.
**Решение: Publish остаётся OFF; publisher adapter skeleton пока не обоснован.**
Нового доказанного create-контракта сверх прежних документов не найдено.
Ниже — ограниченный результат аудита и условия отдельной будущей проверки,
не разрешение на запросы, deployment или публикацию. Исходный аудит не менял
runtime/UI; ниже отдельно описано локальное расширение существующей GET-кнопки.

## Диагностика структуры через существующую GET-кнопку

Source-only расширение от 2026-10-23 поверх `72ad5c04` (PR #116).
Production не менялась, live-запросы не выполнялись. По сообщению владельца
существующая GET-проверка Elden Ring, category 274 / node 986, показывала три
видимых SELECT; это не наблюдение новой диагностики и не доказательство NEW.

Используется только прежний путь **📦 Лоты → ⚙️ Дополнительно → 🗺 Категории и
маршруты → сохранённый GAME_KEY route → 🧪 Проверить форму раздела (только чтение)**.
`get_with_evidence(node_id)` использует тот же внутренний путь `NodeFormService`:
cache miss вызывает максимум один application-level `Account.method('get', ...)`,
cache hit — ноль. Данные берутся из уже полученного ответа после прежних
проверок `_parse`; вспомогательный разбор консервативно проверяет разметку
и controls. Дополнительного GET, retry, force refresh, второго Account, startup
hook или фоновой задачи нет. Одноразовый intent, owner/account/object/scope,
template/route revision и проверки до/после чтения под общим RLock сохранены.

Диагностика — отдельный словарь строго фиксированных enum, проверяемый также
на границе UI. Ни HTML, ни имена/значения скрытых controls, URLs, options,
идентификаторы аккаунта, тексты ошибок или хеши секретов в него не входят.
Набор ключей и типы проверяются целиком; некорректный результат становится
неизвестным. Он не логируется, не сохраняется в flow/cache/schema/fingerprint,
draft или SQLite. Обычный `get()` сохраняет прежние ключи и поведение.

| Ключ | Допустимые значения и смысл |
| --- | --- |
| `source` | `response` — ответ данного вызова, без гарантии свежести; `cache` — прежняя схема из кеша до 900 с; `unknown` — недостоверный диагностический результат. |
| `controls` | `recognized` — ограниченный набор controls распознан; `unknown` — неизвестная или неоднозначная структура. Это не verdict «безопасно отправлять». |
| `new_form_context` | `candidate` — ровно один matching node input и один пустой/нулевой offer input, явные согласованные action/method; иначе `unknown`. Серверная NEW-семантика не доказана. |
| `csrf_input_presence` | `present` / `absent` — наличие/отсутствие распознаваемого скрытого CSRF-like input в ограниченной структуре; `ambiguous` — структура неизвестна. Ни значение, ни свежесть, ни авторизация не проверяются. |
| `manual_delivery`, `submit_contract`, `response_id` | Только `unverified`, независимо от наличия auto-delivery или любых текстов формы. |
| `evidence_blocker` | Одна фиксированная причина неизвестной структуры либо `none`; значения и приоритет ниже. Это не новые правила допуска. |

### Причина UNKNOWN: source-only изменение от 2026-10-24

По сообщению владельца после deployment main `4d537933`, его явное нажатие
существующей GET-кнопки для сохранённого Elden Ring GAME_KEY route (каталог 274,
node 986) показало доступную форму, 3 видимых SELECT, источник «ответ этого GET
(свежесть не доказана)», неизвестные структуру, NEW и CSRF. Это наблюдение
владельца на единственном production runtime, не live-проверка в этом checkout.
HTML, сетевые данные и приватные логи не получались. Причина не установлена:
строгая проверка всего документа, неожиданная структура controls и неоднозначный
конечный URL остаются гипотезами. Ручная выдача, submit contract, ID результата
и публикация остаются UNVERIFIED/OFF.

Теперь при `source=response`, `controls=unknown` выводится одна строка
«Почему неизвестно: …». Enum обозначает первый класс препятствия по следующему
фиксированному приоритету, а не все проблемы и не подтверждённую первопричину:

| `evidence_blocker` | Смысл / строка владельцу |
| --- | --- |
| `response_url` | Неоднозначный адрес ответа: отсутствующий final URL или прежняя более строгая evidence-проверка URL не пройдена. |
| `duplicate_attributes` | Повторяющиеся атрибуты разметки где-либо в документе. |
| `document_self_closing` | Самозакрывающийся обычный тег HTML (не void element). |
| `document_text` | Неоднозначный текст HTML: parser получил текст с `<`, в том числе после декодирования entities или в script. |
| `document_tag_mismatch` | Несовпадение закрывающего тега со строгим стеком HTML. Это наблюдение алгоритма, не доказательство ошибки HTML. |
| `document_unclosed_tags` | Непустой строгий стек тегов в конце HTML, если более приоритетных препятствий нет. |
| `document_markup` | Общий fallback неизвестной разметки HTML; сохранён для совместимости диагностического контракта. |
| `editor_markup` | Неподдерживаемая разметка формы: вложенные формы, встроенные script/template и подобные элементы, обработчики событий, base или внешняя привязка controls. |
| `control_conflict` | Конфликт элементов формы: вложенные controls, overrides отправки, отсутствующее/повторное имя. |
| `unsupported_controls` | Неподдерживаемые элементы формы: неизвестный control, неподдерживаемый тип/форма значения, скрытие разметкой или disabled. |
| `ambiguous_context` | Неоднозначный контекст формы: несколько допустимых aliases identity. |
| `none` | Ограниченная структура распознана; предупреждения о причине UNKNOWN нет. NEW всё ещё может быть неизвестным. |
| `cache_unavailable` | Только cache hit: «структурные данные сетевого ответа недоступны в кеше». Не приписывает кешу причину прошлого ответа. |
| `unknown` | Причина не установлена; в том числе безопасный fallback при некорректном диагностическом объекте. |

Source-only уточнение от 2026-10-26: по сообщению владельца GET после `b567b561`
для category 274 / node 986 вернул доступную форму, 3 SELECT, `source=response`,
`evidence_blocker=document_markup`, неизвестные NEW/CSRF; Publish OFF.
Это не live-проверка данного checkout. Optional end tags не доказаны как причина.
Автоматическое закрытие тегов не добавлено: соответствие восстановленного дерева
BeautifulSoup браузерному DOM для безопасной формы не установлено. Даже законные
пропуски end tags вне editor остаются UNKNOWN. Новые enum уточняют только
наблюдение строгого стека, без вывода имён тегов, атрибутов или текста ответа.
Подприоритет разметки: self-closing → text → mismatch → unclosed → fallback;
duplicate attributes выше всех них. Acceptance, точные ключи и межполевые
инварианты `safe_form_evidence`, cache и единственный owner-triggered GET прежние.
Синтетические тесты охватывают omitted head/body/html, p, li, dt/dd, table tags,
malformed markup, приоритет, отсутствие утечек и UNKNOWN после cache hit.
Hosted Windows/Linux CI, Product/Core QA выполняются доверенным worker отдельно;
локальные проверки не подтверждают live fix и не заменяют отдельно одобренные
обновление и owner GET. Запись CURRENT_STATE не добавляется: snapshot production
не менялся, изменения документации ограничены разрешёнными файлами.

Приоритет между конфликтами и неподдерживаемыми controls не зависит от их
порядка в документе. Обычные parser/HTTP failures имеют прежний safe reason
вместо evidence; например, неверная identity или запрещённый URL не становятся
успешным ответом с блокером. Отсутствующие action/method или identity сами по себе
раньше не делали `controls` неизвестным: они по-прежнему оставляют NEW неизвестным,
без выдуманной причины UNKNOWN-структуры. Правила допуска не расширены.

Sanitizer проверяет точные ключи, строковые enum и согласованность всех полей:
`none` возможен только для распознанного ответа с present/absent CSRF-признаком;
неизвестная структура исключает candidate/present/absent; cache допускает только
`cache_unavailable` и неизвестные признаки. Любой некорректный объект целиком
заменяется неизвестным. Это валидация формата, не независимое доказательство
подлинности пользовательского diagnostic provider. Ни blocker, ни исходные
данные не сохраняются и не логируются; схемы, fingerprint, SQLite и draft прежние.

Следующий шаг — независимый ChatGPT review и четыре hosted проверки (Windows CI,
Linux CI, Product QA, Core QA), затем отдельно одобренные merge и deployment.
После этого владелец может выполнить одно обычное нажатие той же route-form
кнопки. Действующий кеш до 900 с не обходится: cache hit даст только unavailable,
а следующий обычный cache miss — причину для ответа этого GET. Запрашивать HTML,
cookies, headers, `.env` или приватные логи не нужно. Будущее наблюдение класса
блокера направит отдельную минимальную корректировку, если она обоснована;
оно не разрешает публикацию. В рамках этой задачи deployment и live-запросов нет.

Локальные проверки этого изменения: `pytest -q -p no:cacheprovider` для
`tests/test_node_form.py tests/test_telegram_products.py` — **279 passed**.
Полный `tests` до добавления последнего exception-canary теста — **1703 passed,
448 subtests passed, 3 failed**: Git Bash не смог создать signal pipe (Win32 error 5),
два installation/release smoke теста не смогли открыть кеш uv (access denied).
Использовался существующий `.venv/Scripts/python.exe`; только в тестовом процессе
`os.mkdir(..., 0o700)` заменялся на mode 0777, temp размещался под
`.pytest_cache/issue119-*`. Первоначальный запуск без обхода дал 170 passed и
72 setup errors из-за прав временных каталогов. Полный запуск был с `UV_OFFLINE=1`.
91 Python-файл проверен компиляцией в памяти, `git diff --check` прошёл.
Hosted CI/Linux и независимый ChatGPT review здесь не выполнялись.

Классификатор намеренно узкий: распознаёт context inputs, один известный
CSRF-like input, стандартные price/amount/checkbox и RU/EN text controls,
проверенные visible category SELECT и неназванные кнопки без overrides.
Неизвестные, повторяющиеся, disabled/скрытые через разметку, внешне связанные
controls, неоднозначные aliases, дубли HTML-атрибутов, нарушенная вложенность,
неполные теги, script/template внутри editor или handlers дают unknown/ambiguous.
Даже допустимые HTML с пропущенными закрывающими тегами могут дать unknown.
Иные имена CSRF не угадываются. Неполная identity не становится кандидатом.
Для candidate action должен быть root-relative или однозначно адресовать
HTTPS FunPay; неоднозначный относительный путь остаётся unknown, даже если
обычный parser его допускает.
Ненулевой offer, неверный node/user, login/error и прочие прежние parser failures
по-прежнему отклоняют весь ответ с прежним безопасным reason code.
Отсутствующий или неоднозначный final URL не даёт положительной диагностики.
Никакой неизвестный control не добавляется в write allowlist или schema.

При cache hit вся структурная диагностика неизвестна, даже если прошлый вызов
дал candidate. TTL 900 с, cache scope/LRU и generation не изменены; обычное
открытие/проверка не форсирует обновление. Следующее нажатие требует заново
открыть маршрут, но не гарантирует сетевой запрос, пока кеш действителен.

Пример формата успешного ответа (иллюстрация, не новый live-результат):

```text
🧪 Каталог 274 · node 986
Форма: доступна
Видимых SELECT-полей: 3 (метаданные).
Источник: ответ этого GET (свежесть не доказана).
Структура: распознана ограниченная структура.
Контекст NEW: кандидат NEW; создание НЕ доказано.
CSRF: признак есть; свежесть и авторизация НЕ доказаны.
Ручная выдача: НЕ проверена.
Контракт отправки: НЕ проверен.
ID результата: НЕ проверен.
публикация НЕ проверена
get может вернуть кеш до 900 с; клиент следует редиректам. Это не гарантированно одна ограниченная HTTP-транзакция.
```

При cache hit источник: «кеш до 900 с; текущая структура неизвестна»,
контекст NEW: «неизвестен», CSRF: «неизвестно / неоднозначно».
Провал чтения выводит только прежний safe reason, без структурной диагностики.

Ограничения [in-process diagnosis](NODE_FORM_IN_PROCESS_DIAGNOSIS.md) остаются:
redirect может породить дополнительные HTTP-запросы внутри requests, lock wait,
редиректы, parsing и транспорт не имеют общего доказанного time budget.
Timeout/redirect/cache/lock policy не менялась. Это не разрешение на boot probe
и не обещание одной wire-транзакции или отсутствия задержки других Account задач.
Распознана только статическая структура, не исполнение browser JavaScript.

После отдельно разрешённого наблюдения владельцем этих enum всё ещё нужно
независимо доказать write identity, manual mode, CSRF provenance и server response
ID. Затем отдельно проектируется и разрешается один opt-in lot experiment с
durable UNKNOWN и exact ID read-back по gates ниже. Publish остаётся OFF.
Перед merge обязательны независимый security review и четыре hosted проверки:
Windows CI, Linux CI, Product QA, Core QA. Локальные mocks не заменяют их.

Локальная проверка изменения: полный `pytest -q -p no:cacheprovider tests`
дал **1660 passed, 450 subtests passed, 2 failed**. Внутри этого запуска все
тесты наборов из `scripts/qa_smoke.py` прошли: Product — 505, Core — 134.
Два failure вне изменённых модулей: Git Bash не смог создать signal pipe
(Win32 error 5); release smoke не нашёл wheel `pydantic-core` в офлайн-кеше.
Отдельный packaging smoke заблокирован отсутствующим `hatchling` в кеше.
91 tracked Python-файл компилируется в памяти; `git diff --check` проходит.
После финального уточнения action отдельно прошли **299 целевых тестов**:
`test_node_form.py`, `test_telegram_products.py`, `test_node_form_diagnostics.py`,
`test_node_form_owner_diagnostics.py`.
Никаких загрузок зависимостей, live-запросов, production или GitHub действий
не выполнялось. Hosted Windows/Linux CI и независимый security review не запускались.

Все временные файлы, pytest `--basetemp`, TEMP/TMP и writable UV cache направлены
в ignored `tests/.offline-tmp/`; `UV_OFFLINE=1`, bytecode запись отключена.
Первый целевой запуск встретил известный sandbox отказ для каталогов mode 0700.
Повтор и полный запуск использовали только внутри тестового процесса замену
`os.mkdir(..., 0o700)` на 0777; application permissions и тестовые assertions
не менялись. Synthetic regression покрывает candidate/unknown/CSRF, aliases,
дубли controls/attributes, malformed/hostile HTML, private canaries, старые
parser failures, cache hit/TTL, один GET при конкуренции, stale owner/account/
route/template до и после ответа и неизменность временной SQLite.

## Источники и границы исходного аудита 2026-10-21

- **A — установленный FunPayAPI 1.1.0:** прочитаны `FunPayAPI/account.py`
  (`Account.method`, строки 89+, `get_lot_fields`, 1186+, `save_lot`, 1216+)
  и `FunPayAPI/types.py` (`LotFields`, 494+, `renew_fields`, 556+).
  Публичный [исходный архив 1.1.0](https://files.pythonhosted.org/packages/fd/c5/880876330bcf636e3dc06c34738b1411a502a12d1e5344e128d9f7ca80f5/FunPayAPI-1.1.0.tar.gz)
  закреплён в [uv.lock](../uv.lock), SHA-256
  `c09500b5b461ac13052b8663bfeffad38399f531e0d2d1ff955afa504eda36c5`;
  версия/хеш также доступны в [метаданных PyPI](https://pypi.org/pypi/FunPayAPI/1.1.0/json).
  Проверена установленная реализация; побайтовое сравнение с архивом не выполнялось.
  `pyproject.toml` задаёт `>=1.1.0`, точная версия закреплена lock-файлом.
- **B — сторонний публичный Cardinal:**
  [commit c09dd13, get_lot_fields / save_offer, строки 1611–1706](https://github.com/sidor0912/FunPayCardinal/blob/c09dd13be25072bb3d5cdbba33d1e0d70e477710/FunPayAPI/account.py#L1611-L1706).
  Там есть node-specific GET и разбор editor; это другая реализация, не API
  установленной зависимости и не подтверждение поведения сервера.
  Sigma `eabb45f` уже рассмотрена в [NODE_FORMS](NODE_FORMS.md): повторное
  получение raw source не удалось, GitHub view не раскрыл искомый метод.
  Новых выводов о Sigma этот аудит не делает.
- **C — checkout:** [lot_templates.py](../src/funpayflow/lot_templates.py)
  (`_business_fields`, `confirm`, `verify`), [node_form.py](../src/funpayflow/node_form.py)
  (`_parse_impl`, `NodeFormService._get`), [node_products.py](../src/funpayflow/node_products.py)
  (`selected_fields`, `node_setup`, `create_node_preview`).
  [PRODUCT_TEMPLATES](PRODUCT_TEMPLATES.md) описывает семь локальных типов;
  их `delivery_method` — текст предложения, не серверный режим выдачи.

| Вопрос | Подтверждено кодом | Только вывод / остаётся неизвестным |
| --- | --- | --- |
| (a) Identity fresh create-form | B умеет GET по node; C проверяет контекст формы, скрытые identity не сохраняет. | GET предполагает путь к форме создания; обязательные create identity и их серверная семантика не установлены. Ноль/пустое/отсутствующее `offer_id` нельзя выбирать наугад. |
| (b) Поля и options node | C сохраняет проверенные visible SELECT names/options, требует явных bindings и текущей schema. | Это кандидаты для draft, не write allowlist. Не доказаны полный набор business controls, зависимые options и применимость ко всем товарам node. Fixtures синтетические; успешная live schema для 976/986 не подтверждена. |
| (c) Manual / automatic | C не переносит goods/credential controls; NodeForm исключает известные service controls, включая `auto_delivery`. | Отсутствие goods или текст «ручная выдача» не доказывают manual mode. Его controls, defaults, обязательность и read-back неизвестны. |
| (d) Auth / CSRF | A.method добавляет session credentials; A.get_lot_fields копирует input controls существующей формы. C исключает session/CSRF из draft/schema/logs. | Точный источник, freshness и связь create-CSRF с формой/аккаунтом не доказаны. Нельзя переносить токен из чужого/старого edit form. |
| (e) NEW вместо edit | A получает `offerEdit?offer=<lot_id>`; `renew_fields()` сохраняет прочие поля dict; `save_lot()` отправляет их в `lots/offerSave`. | Изменение Python `lot_id` не очищает исходный `offer_id`. NEW contract не подтверждён; успешный save существующего лота его не устанавливает. |
| (f) ID / read-back | A.save_lot проверяет `error`, но возвращает `None`; C.verify сообщает `unsupported_schema`. | Это не доказывает отсутствие ID в raw server response. Формат ID, однозначная корреляция и проверка точного нового лота неизвестны. Совпадение title недостаточно. |
| (g) Timeout / дубли | В A.method один прямой вызов requests, без собственного retry-loop и без per-call redirect control. C.confirm не делает dispatch. | Серверная идемпотентность не доказана; redirect/transport поведение требует отдельной проверки. Локальная дедупликация batch и autobump cooldown не защищают create. Durable publish dispatch/reconciliation отсутствуют. |
| (h) Другие лоты | Текущий C не отправляет modifying HTTP; [mock-регрессия](../tests/test_lot_templates.py) сохраняет блокировку. | Для будущего write отсутствие изменений других лотов не доказано. Ошибка identity может изменить исходный лот; нужна проверка состояния до/после. |

Исторические причины отказов NodeForm и ограничения общего RLock изложены в
[NODE_FORMS](NODE_FORMS.md) и [in-process diagnosis](NODE_FORM_IN_PROCESS_DIAGNOSIS.md).
Ни публичный клиент, ни synthetic PASS не заменяют live owner proof.

## Будущий протокол: один лот под наблюдением владельца

Это спецификация последовательных gates. **Сейчас ни один live шаг не выполняется.**
Официальный FunPay sandbox проверенными источниками не установлен: staging бота
не изолирует реальный аккаунт от последствий записи и не гарантирует нулевой риск.

1. **Offline gate.** Сначала отдельный review минимальной диагностики через
   существующий authorized Account/RLock: auth/context, безопасная структура
   fresh form, разделение business/service controls, explicit manual mode,
   ожидаемый response ID и способ read-back. Секреты остаются только внутри
   существующей сессии; сырые HTML, payload и headers не захватываются.
   Пока любой из этих контрактов нельзя безопасно проверить — STOP до dispatch.
   Известные redirect/lock/runtime-budget блокеры диагностики сначала должны
   быть закрыты с offline tests; текущий runtime-hook отсутствует.
2. **Live read-only proof — отдельное согласие владельца.** Через обычный бот
   подтвердить один конкретный COMMON node, фактические visible fields/options,
   их обязательность и manual mode. Начать с existing allowlisted reason/counts,
   если форма отвергается. Для недостающей identity/CSRF/delivery структуры
   потребуется отдельно проверенная минимальная диагностика: выводить только
   фиксированные признаки структуры/соответствия, без значений service controls.
   Не ослаблять parser ради успешного результата. Другой node/тип товара требует
   своей проверки; семь шаблонов не получают общей write-поддержки.
3. **Защищённый staging и offline acceptance.** Проверенная сборка, publisher
   default OFF, один writer и тот же authorized Account; никакого второго
   Account, параллельного бота с той же сессией или копирования cookies.
   Отдельное разрешение на staging/переключение исполнения обязательно;
   инфраструктурные команды сюда не входят. До live write будущая реализация
   должна пройти fake/temporary SQLite сценарии: double confirm, concurrent claim,
   stale revision/schema/account, SAFE_MODE/late gate, crash до/после dispatch,
   timeout, потеря записи результата, неверный/отсутствующий ID и restart без resend.
4. **Отдельный opt-in на одну запись.** Владелец видит и подтверждает exact node,
   revision/fingerprint, все business поля/options, RU/EN тексты, цену/валюту,
   quantity=1 и manual mode. Только допустимое реальное предложение без секретов;
   никакой покупки/автовыдачи для проверки. Подтверждение одноразовое, с expiry,
   привязано к account/node/draft. Любое изменение требует нового preview/confirm.
   Заранее согласовать видимость и действия по завершению; не считать `active`
   гарантией безопасного непубличного режима без доказательства.
5. **Одна попытка.** До отправки сохранить durable attempt/claim и baseline IDs
   и сравнимые business-состояния собственных лотов в приватном локальном state.
   Ошибка SQLite запрещает dispatch. Под RLock повторить existing safety/late
   gates и сверить scope/schema. Подтверждённый transport должен исключать
   неявный повтор записи/redirect replay. Никаких bulk, edit исходного лота,
   автоматического reconnect retry или фонового повторного запуска publish.
6. **Live write/read-back proof.** После единственной разрешённой записи извлечь
   подтверждённый серверный ID и заново прочитать именно его через тот же бот:
   владелец/account, node, ID отсутствовал в baseline, все подтверждённые поля,
   quantity, manual mode и видимость совпадают. Сверить остальные собственные
   лоты с baseline. Человеческая проверка обязательна. Изменение другого лота
   или неоднозначность блокируют дальнейшие записи; автоматического rollback нет.
7. **UNKNOWN — STOP.** Timeout, crash после claim, непонятный ответ, отсутствие ID
   или failure read-back оставляют durable UNKNOWN даже после restart.
   Только read-only reconciliation: baseline difference и одобренные business
   признаки помогают найти кандидата, затем проверяется exact ID; один похожий
   title не доказывает результат. Ноль/несколько кандидатов или неполный snapshot
   сохраняют UNKNOWN. Новый submit запрещён, в том числе после истечения времени;
   спорный исход разрешает человек на основании доказательств, не кнопка Retry.
8. **Закрытие опыта.** Вернуть opt-in в OFF, сохранить результат проверки.
   Любое изменение/снятие тестового лота — отдельное подтверждение exact ID;
   неизвестный исход не исправлять массовым удалением. Один успех даёт evidence
   только для проверенного node/контракта, не разрешение на массовую публикацию.

Логирование будущего опыта: allowlist из local attempt ID, phase, timestamp,
фиксированного outcome/reason, HTTP status и boolean результатов проверок.
Remote ID/ссылка доступны владельцу в защищённом state/UI для exact read-back,
но не в публичном отчёте. Не логировать тексты предложения, raw URLs/query,
формы/payload, auth/CSRF/cookies, app-data, exception/response body или их хеши.
Sanitizer проверять synthetic canaries для каждого error path; неизвестные поля
отбрасывать. Это будущие требования, не реализованные гарантии.

## Рекомендация владельцу и следующий шаг

**Каркас отправки сейчас не добавлять:** выключенный флаг не исправляет
неизвестную identity и невозможность надёжно найти результат. Следующий маленький
шаг — отдельно согласованное наблюдение безопасных enum через существующую
GET-кнопку после review и проверок этого изменения. Это ещё не fresh-form proof:
documented transport/lock блокеры не закрыты, принудительного запроса нет.
Никаких новых шаблонов или фиктивной кнопки Publish.

Владельцу потребуется сначала подтвердить доступную форму одного выбранного
раздела и ручную выдачу через уже авторизованный бот. Затем, только после review
контракта и отдельного согласия, — одна контролируемая запись с точным ID,
read-back и сравнением остальных лотов. Для предлагаемого первого rollout
защищённый staging и подтверждение человеком обязательны; staging сам по себе
не защищает аккаунт от серверного эффекта. Сейчас гарантировать создание без
риска нельзя. Второй Account, SSH/browser, cookies, credentials или скриншоты
с секретами для этого не нужны и не запрашиваются.

Проверки этого аудита: существующий Product QA набор из `scripts/qa_smoke.py`
плюс `test_lot_templates.py` и `test_node_form_probe_feasibility.py` — **443 passed**.
Первый обычный запуск: 4 passed / 44 setup errors из-за Windows temp permissions;
повтор использовал `.pytest_cache/issue107-temp` и замену mkdir mode 0700 на 0777
только в тестовом процессе. Код приложения/тестов не менялся. Проверены локальные
ссылки, scope и whitespace; live/CI/Linux результаты не заявляются.
