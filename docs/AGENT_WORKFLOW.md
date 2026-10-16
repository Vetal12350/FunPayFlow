# Codex workflow: роли и экономия контекста

Это проектная политика для будущих задач, а не отдельная агентная система. Нужны Codex, [AGENTS.md](../AGENTS.md), выбранные документы и при необходимости встроенные subagents. Не добавлять Agents API application, OpenAI API integration, MCP server, daemon, новую БД или отдельную CI ради этих ролей.

## Coordinator — основной агент

1. Прочитать AGENTS.md; проверить Git status/branch/HEAD и ограничения пользователя.
2. Классифицировать задачу и найти область по [ARCHITECTURE](ARCHITECTURE.md), если она неизвестна. Сначала `rg --files`/символы, затем нужные участки; не читать каждый файл.
3. Загрузить только документы из routing ниже. Текущую реализацию проверить даже при наличии handoff.
4. Решить, экономит ли делегирование контекст/время. Если нет — выполнить работу самостоятельно.
5. Согласовать границы файлов/ответственности, собрать результаты, проверить совокупный diff и acceptance criteria, выполнить итоговые проверки по [DEVELOPMENT](DEVELOPMENT.md).
6. Подготовить краткий отчёт и оценить необходимость обновить CURRENT_STATE.

Coordinator отвечает за интеграцию и финальный результат; summary субагента не заменяет проверку его diff. Эти логические роли можно последовательно выполнять одним агентом.

## Developer / Reviewer / Research

| Роль | Ограниченная задача | Вход | Возврат |
| --- | --- | --- | --- |
| Developer | Реализовать самостоятельную часть | Цель, relevant files/symbols, найденные факты, запреты, acceptance criteria и tests | Изменения, files, команды/results, риски/нерешённые вопросы |
| Reviewer | Проверить итоговый diff; по умолчанию read-only | Требования, diff, нужные файлы, tests/results | Конкретные regressions с file/line, нарушенный invariant, test gaps; либо отсутствие findings с границами проверки |
| Research | Получить внешний факт, которого нет в repo | Точный вопрос, проверенные локальные источники, нужная версия зависимости/API | Проверяемый вывод с первичными источниками, uncertainty и последствиями для реализации |

Developer не исследует заново весь repo после локализации Coordinator. Reviewer не реализует функцию с нуля и не меняет код без отдельного поручения. В review особенно проверить state/migrations, cancellation, network classifier, duplicate/non-idempotent operations, queues, business notifications, coverage и unrelated changes.

Research по умолчанию read-only. Сначала проверить repository/locked dependency source; внешнее исследование нужно для действительно неизвестного API, новой версии или ограничения платформы. Применять доступный профильный skill и актуальные первичные источники; не делать реальные операции FunPay для «исследования».

## Бюджет субагентов

Субагент должен экономить/изолировать контекст, а не повторять работу главного агента. Делегирование необязательно даже для сложной задачи.

| Класс | Начальный выбор | Бюджет subagents |
| --- | --- | --- |
| Trivial: текст кнопки, механическая правка | Coordinator | **0** |
| Small/normal: локальная настройка/функция | Coordinator; Developer только при отдельном scope | **0–1** |
| Complex: несколько слоёв, migration, concurrency | Coordinator; Developer; Reviewer если оправдан риском | **До 2 всего за задачу и одновременно** по умолчанию |
| Research-heavy | Coordinator + Research; затем Developer при необходимости | **До 2 всего**, зависимую реализацию не запускать параллельно с Research |
| Bugfix | Coordinator сначала воспроизводит/локализует | Обычно **0**; затем применить подходящий бюджет выше |

Не запускать автоматически Research + Developer + Reviewer для каждого бага. Третий субагент/дополнительная волна требует конкретного обоснования и явного расширения бюджета в запросе пользователя; не обходить лимит бесконечной цепочкой завершённых/spawned агентов. Если среда не поддерживает delegation, выполнить роли последовательно в основном чате, не создавать сервис или отдельные пользовательские задачи как обход.

Использовать native subagents только когда это разрешено текущими инструкциями/средой; этот workflow разрешает ограниченное делегирование по указанной политике, но не требует spawn. Субагентам передавать тот же бюджет, запрещать самостоятельное размножение команды без поручения Coordinator. Объяснять пользователю цель делегирования кратко, без запроса повторного разрешения на уже разрешённые read-only/local действия.

## Routing: читать выборочно

| Задача | Документы и первая область кода |
| --- | --- |
| Telegram UI/auth/settings | Нужная часть ARCHITECTURE; `telegram.py`, соответствующие UI tests; FUNPAY_BEHAVIOR только если затронуты actions/recovery |
| Autobump/network/UNKNOWN | FUNPAY_BEHAVIOR; `main.auto_bump_loop`, `FunPayClient.bump_lots`/classifier, `state.autobump_*`, relevant settings/tests |
| Orders/reviews/import/analytics | Нужная часть ARCHITECTURE; `runtime_events.py`, consumer, соответствующие методы `state.py`/import engine/UI/tests |
| Tests/install/build | DEVELOPMENT; CI, соответствующие test files, installer/build scripts |
| Deployment/rollback | PRODUCTION + существующий VPS_INSTALL; live actions только с прямым разрешением |
| Непонятный внешний API | Сначала relevant source/lock; затем scoped Research при необходимости |
| Продолжение большой работы | CURRENT_STATE + фактический status/diff; не перечитывать весь исторический чат |

Обычная feature не требует загрузки PRODUCTION.md. AGENT_WORKFLOW нужен при решении о делегировании/планировании, а не как обязательное большое чтение при каждой текстовой правке.

## Пакет поручения и совместные изменения

В handoff достаточно:

```text
Goal / acceptance criteria:
Base commit + workspace/branch:
Relevant files and symbols; verified facts:
Allowed edits / no-touch areas; safety constraints:
Dependencies on other work; required tests:
Return: summary, changed files, tests/results, risks; no commit/push.
```

Передавать извлечённый контекст и file references; полный fork истории — только если он действительно нужен. Не копировать секреты, реальные логи, весь repo или бессодержательные длинные tool outputs. Developer должен дочитать недостающий локальный контекст, а не доверять summary вместо кода.

Обычно один writer на пересекающуюся область. Параллельные edits допустимы только в независимых файлах с ясным владельцем; Reviewer read-only получает финальный diff после интеграции. Не запускать зависимые задачи одновременно. Повторно использовать одного подходящего субагента, если это дешевле нового исследования. Остановить ненужную работу после изменения scope; ждать через штатные механизмы, не busy-poll.

## Модель и reasoning

Это рекомендации по сложности, не жёсткие model IDs и не обещание цены/доступности. Учитывать выбранную пользователем модель, разрешения и уровни, поддерживаемые текущим клиентом; не переключать их самовольно и не строить API-интеграцию для выбора.

| Работа | Рекомендация |
| --- | --- |
| Механическая | Доступная fast/standard model, low reasoning |
| Обычная разработка | Основная coding model, medium |
| Сложный баг, архитектура, опасная migration | Сильная доступная coding/reasoning model, high |
| Review | Обычно medium; high для сложных safety/concurrency рисков |
| Research | Обычно medium; high только для действительно сложного вопроса |

Не использовать максимальную модель/High автоматически. Если нужен выбор модели вне текущей конфигурации, сформулировать рекомендацию; применить только разрешённые пользователем/средой overrides. Роли не привязаны к четырём отдельным моделям или постоянно живущим агентам.

## Завершение и handoff следующему сеансу

Coordinator проверяет весь diff, scope, реальные test results и отсутствие приватных данных. После значимого завершённого пакета обновляет [CURRENT_STATE](CURRENT_STATE.md), только если следующий сеанс иначе потеряет полезное состояние: capability, invariant, blocker, agreed WIP или следующий шаг. Записывать base commit/date и отличать проверенный код от historical production snapshot. Не превращать файл в changelog и не обновлять его ради каждой мелкой правки.

В финальном отчёте: результат, изменённые файлы, проверки, оставшиеся риски. При незавершённости — точный следующий шаг и проверенный diff, без утверждения о готовности. Commit/push/merge/release/deployment не включаются автоматически.

Основа использования AGENTS.md и изоляции контекста сверена 2026-10-16 с официальной документацией OpenAI: [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md), [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents). Роли, лимит 0/1/2 и routing выше — политика FunPayFlow, а не гарантированные ограничения платформы. Настраивать глобальный Codex config в рамках этой документации не требуется.
