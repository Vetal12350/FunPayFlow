# Создание лота FunPay: evidence и блокер

Проверено 2026-10-21, source HEAD `683a101680fc8df0d02d7a5ee60c4113445713b2`.
**Решение: Publish остаётся OFF; publisher adapter skeleton пока не обоснован.**
Нового доказанного create-контракта сверх прежних документов не найдено.
Ниже — ограниченный результат аудита и условия отдельной будущей проверки,
не разрешение на запросы, deployment или публикацию. Runtime и UI не меняются.

## Источники и границы доказательств

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
шаг — отдельно согласованная диагностика fresh form через существующий бот,
после закрытия documented transport/lock блокеров, с synthetic regression на
подтверждённую структуру. Никаких новых шаблонов или фиктивной кнопки Publish.

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
