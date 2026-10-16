# FunPayFlow: инструкции для Codex

Существующий production-проект для продавцов FunPay; Python-пакет в `src/funpayflow/`.

- Перед правками проверь `git status --short`, ветку и HEAD; найди релевантные файлы и прочитай текущую реализацию. Не предполагай API по памяти.
- Сохраняй чужие изменения и untracked-файлы. Не делай массовый рефакторинг ради локальной задачи.
- Без прямого разрешения пользователя не выполняй commit, push, merge, release, deployment, доступ/изменения production и разрушительные Git-операции. Опубликованный `v1.0.0` не пересоздавать.
- Не открывай `.env` и приватные runtime-данные ради исследования; не выводи и не сохраняй секреты в коде, документации, тестах или отчётах. Не проси секреты у пользователя.
- Проверки выполняй офлайн с mocks/fakes и временной SQLite. Реальный запуск подключается к FunPay/Telegram и требует отдельного разрешения.
- Сохраняй order/event processing, Telegram, autobump, persistent state и network recovery. Потенциально неидемпотентный запрос с результатом UNKNOWN нельзя blindly retry. Не обходи SAFE_MODE и late action gates.
- По умолчанию работай одним агентом. Делегируй только ограниченную независимую часть, когда это экономит/изолирует контекст: trivial — 0 субагентов, normal — максимум 1, complex — максимум 2. Подробности в AGENT_WORKFLOW; не запускай агентов только ради ролей.
- Проверяй итоговый diff и релевантные тесты. Отчёт: изменения, проверки, риски. После существенной задачи оцени необходимость обновить `CURRENT_STATE`; мелкие правки не требуют новой записи.

Читай **только нужные** документы, затем соответствующий код:

| Вопрос | Документ |
| --- | --- |
| Карта модулей и lifecycle | [ARCHITECTURE](docs/ARCHITECTURE.md) |
| Разработка и проверки | [DEVELOPMENT](docs/DEVELOPMENT.md) |
| FunPay, recovery, autobump, UNKNOWN | [FUNPAY_BEHAVIOR](docs/FUNPAY_BEHAVIOR.md) |
| Production и deployment | [PRODUCTION](docs/PRODUCTION.md) |
| Текущий snapshot и планы | [CURRENT_STATE](docs/CURRENT_STATE.md) |
| Роли, делегирование, handoff | [AGENT_WORKFLOW](docs/AGENT_WORKFLOW.md) |
