# Product Templates и Game Routes

Этот слой создаёт **только локальные черновики**. Publish отсутствует. Отдельный GET-only adapter читает create-form выбранного node; write/verification contract не реализован, FunPayAPI 1.1.0 не менялась. Никаких `save_lot`, modifying HTTP, AI-перевода или конвертации цен здесь нет.

## Telegram

Владелец открывает `📦 Лоты`:

- **Создать:** template → saved route или поиск игры в каталоге → явное подтверждение нового route → variant (если есть) → реальные Node Fields → content variables / quantity → цена → RU/EN preview → сохранить draft.
- **⚡ Быстрый черновик:** `📦 Лоты → ⚡ Быстрый черновик` сразу открывает выбор игры/маршрута для встроенного GAME_KEY. Например: Elden Ring → реальные Node Fields → quantity 1 → цена 29.99 → обычный RU/EN preview → сохранить локально. Это однокнопочный shortcut: он убирает экран выбора шаблона, переиспользуя весь мастер «Создать», его validators и scoped DraftService. Сохранённые изменения GAME_KEY, route/variant overrides и дополнительные content variables учитываются как прежде; варианты и Node Fields не пропускаются и defaults формы не подтверждаются автоматически. При отсутствии route используется прежний каталог с явным подтверждением маршрута. Отмена до Save не создаёт draft; уже подтверждённый новый route остаётся. Это LOCAL DRAFT, публикации нет: create/manual-delivery/write/verification contract ещё не подтверждён.
- **🧩 Тип товара:** семь вариантов на одном экране (семь строк и «Назад»): 🔑 игровой ключ (`GAME_KEY`), 🎁 гифт игры (`GIFT`), 🎮 игра PlayStation (`PLAYSTATION_GAME`), 🤖 подписка / доступ к ИИ-сервису (`AI_SUBSCRIPTION`), 🧩 DLC / дополнение (`DLC_CONTENT`), 💎 внутриигровая валюта (`GAME_CURRENCY`), 🎟️ игровая подписка / подарочная карта (`GAMING_SUBSCRIPTION`). Каждый ведёт в тот же одиночный мастер: маршрут → вариант → реальные Node Fields → публичные сведения о товаре → цена → RU/EN → локальное сохранение. `prd:quick` сохранён. Выбор типа привязан к текущему экрану, владельцу и аккаунту; повторные/устаревшие кнопки не заменяют активный ввод.
- **Массовые drafts:** новый ввод требует `needs_resolution` для Node Fields; скрытых fetch/confirm для каждой строки нет. Уже подготовленные legacy batches сохраняют прежнее подтверждение.
- **Game Routes:** template → собственный лот, URL или cached catalog → preview → подтвердить.
- **Product Templates:** список и полный JSON editor. При создании копии поменять internal `name`.
- **Черновики из собственного лота:** прежний flow и прежняя таблица без миграции/удаления записей.

Preview сокращает длинные поля, но «Полный RU/EN» показывает весь текст частями. Цена, emoji, RU/EN и variables редактируются до сохранения. Сохранённые drafts доступны после restart; редактирование использует revision check. Cancel не публикует и не удаляет исходные FunPay-лоты. Кнопка Cancel в карточке сохранённого draft оставляет локальную запись со статусом `cancelled`; отмена текстового ввода просто отбрасывает несохранённые правки.

Новый экран отменяет незавершённый ввод. Save/edit/cancel/full-text callbacks привязаны к поколению preview; confirmation variant — к своему preview. Старая кнопка не сохраняет и не отменяет другой draft и не сбрасывает текущий ввод. Старые confirmation callbacks после выхода/restart не восстанавливают сессию; готовые сохранённые drafts продолжают отображаться. Двойной confirm одного пакета возвращает уже сохранённый результат.

## Определение template

Встроенный `GAME_KEY` создаётся один раз для владельца/аккаунта; его пользовательские изменения не перезаписываются. Один template обслуживает любое число routes.

Остальные встроенные пресеты создаются **только при выборе соответствующего типа** через `TemplateService.ensure_builtin(kind)`, со стабильными ID. Повторное или конкурентное открытие сохраняет существующую запись целиком, включая изменённые владельцем internal name, display name, emoji и тексты. Миграции и повторного заполнения существующих шаблонов/черновиков нет. Стандартные переменные обязательны в определении всех семи типов; новые пресеты добавляют отдельные content variables и имеют только default quantity=1.

В новых пресетах `sale_name` — фактическое название игры/товара/сервиса, введённое владельцем. `game_name`/`game` остаётся внутренним legacy-именем маршрута из каталога, а не названием продаваемого товара. Интерфейс показывает его как route label / категорию каталога. Тип шаблона не определяет категорию: владелец выбирает подходящий COMMON-раздел текущего Account, подтверждает новый route и реальные поля формы. Если подходящей категории или безопасной формы нет, сохранить черновик через мастер нельзя. Отдельные content fields описывают предложение и не заменяют параметры FunPay; при необходимости владелец явно связывает их через `node_bindings`.

Гифт требует название, платформу, регион/доступность и фактический способ передачи. PlayStation требует название, PS4/PS5/обе платформы по выбору владельца, издание, регион/доступность, форму товара (код или законное право на игру) и законный способ передачи; привязанный личный аккаунт не подразумевается. ИИ — это продаваемая подписка/доступ, а не генерация текста: название сервиса, тариф, срок, регион/доступность, способ передачи и отдельное явное подтверждение разрешения провайдера. Разрешение перепродажи какого-либо сервиса по умолчанию не заявляется. При неизвестном регионе владелец может явно указать «не указано / unspecified»; тарифы, цены, доступность, сроки и способы передачи не подставляются автоматически. Публичные значения попадают в обе языковые версии без машинного перевода; тексты RU/EN можно редактировать перед Save.

Вторая волна использует следующие обязательные публичные поля (ни одно не выводится из названия категории):

| Тип | Сведения владельца |
| --- | --- |
| `DLC_CONTENT` | `sale_name` — фактическое дополнение, `base_game` — необходимая базовая игра, `sale_platform`, `availability`, `sale_edition`, `delivery_method`, `authorized_delivery`. Оба описания явно говорят, что базовая игра не включена. |
| `GAME_CURRENCY` | `sale_name` — фактическая игра, `currency_name`, `denomination` — объём валюты на единицу товара, `availability`, `delivery_method`, `authorized_delivery`. `quantity` — число таких единиц товара, отдельно от объёма валюты. Доставка не гарантируется шаблоном, разрешение пополнения не предполагается. |
| `GAMING_SUBSCRIPTION` | `sale_name` — фактический сервис / карта, `gaming_product_type` — подписка, подарочная карта или код (строгий выбор), `term_or_value` — явно введённый срок или номинал, `sale_platform`, `availability`, `delivery_method`, `authorized_delivery`. Нет встроенных предложений PS Plus/Game Pass, тарифов, сроков или номиналов. |

Во всех трёх пресетах `authorized_delivery` требует явного выбора «Подтверждаю разрешение провайдера / I confirm provider permission», без default. Все используемые дополнительные переменные обязательны и не могут быть пустыми. Имеющиеся route/variant overrides и правки владельца сохраняют прежний приоритет; встроенный пресет не перезаписывает пользовательский шаблон.

Ввод содержит только публичное описание товара. Пресеты не запрашивают данные покупателей, логины, пароли, токены, одноразовые коды, cookies или API keys и не предлагают общие/переданные личные аккаунты. Подтверждение владельца не является автоматической проверкой условий провайдера; владелец обязан убедиться в фактической доступности и допустимости выбранной передачи. Авторизованная доставка (официальный подарок/лицензия или разрешённое место/приглашение) указывается только когда она действительно разрешена и доступна.

Поля JSON: `name`, `display_name`, `default_emoji`, `title_ru`, `title_en`, `description_ru`, `description_en`, `allowed_variables`, `defaults`, `validation_rules`. `id` генерирует registry; `revision` нужен при обновлении. Все четыре текстовых RU/EN поля обязательны. Имя template уникально без учёта регистра/лишних пробелов внутри account scope.

В текстах поддерживается только `{{variable}}`: без expressions, attribute access, filters, `eval` и повторной интерпретации значений. Стандартные переменные: `game`, `platform`, `region`, `edition`, `product_name`, `quantity`. Дополнительные безопасные имена разрешены через `allowed_variables`; credential/service names запрещены. Для правил поддержаны `choices`, `min_length`, `max_length`.

Defaults GAME_KEY: platform Steam, region Global, edition Standard, quantity 1. `game` фиксируется route; `product_name` по умолчанию равен игре и редактируется отдельно. В wizard «-» оставляет default; цена обязательна.

Template-only content variables используют гибридный UI: `validation_rules.<variable>.choices` — уже существующий строгий enum и источник кнопок с пагинацией. Без enum можно кнопкой оставить известное inherited/default значение. Generic GAME_KEY Platform/Region/Edition больше не являются prompts нового create flow: их заменяют реальные Node Fields. Списки платформ/регионов не угадываются и не извлекаются из FunPay filters. Route/variant поддерживают defaults через `overrides.variables`, но отдельных choices/constraints у них сейчас нет; recent-value registry не добавлялся. «Другое» открывает text input с теми же enum/length/type checks; строгий enum не обходится custom вводом. Свободные поля без defaults/enum остаются текстовыми. Quantity — variable с editor ±1/10, presets, custom и Done, диапазон 1..999999999. Цена — прежний decimal input. Старый ввод «-» продолжает работать.

## Создать похожий

После сохранения и в карточке сохранённого draft доступно «📑 Создать похожий». Общий `DraftService.clone_preview` проверяет scope/source revision и current route/template/enabled variant, затем создаёт только transient preview. Новый ID/revision появляются при Save; исходник, его status и timestamps не меняются. При stale/conflicting route UI предлагает выбрать актуальный маршрут через существующее «Создать».

Compact editor показывает реальные Node Fields отдельно и строит content-variable buttons из `allowed_variables` текущего template (кроме фиксированного `game`, generic node variables и explicit bindings), содержит variant selector при наличии вариантов, цену, preview, save и cancel. Variant switch заново рендерит current canonical template → route → variant → explicit draft overrides. Новое JSON metadata `input_variables` отличает explicit inputs от resolved `variables`; `route_snapshot`/revision позволяют отличать наследование. Rendered RU/EN тексты и inherited emoji/defaults не становятся draft overrides. Явные emoji/text/variable overrides сохраняются. Schema SQLite не меняется.

Legacy drafts без input provenance продолжают открываться/редактироваться и staging сохраняет старую форму данных. При clone переносим явно маркированные overrides и значения, отличающиеся от доступных inherited defaults; равные defaults оставляем inherited. Полностью восстановить историю ввода без старого route snapshot невозможно, поэтому UI показывает предупреждение проверить variables. Каждый clone использует current definitions, а не старый rendered snapshot; UI сообщает об этом. Отмена несохранённого clone ничего не записывает и не отменяет source. Save идемпотентен в рамках flow/batch; можно снова создать похожий с новым ID. Saved-list labels показывают короткие game/edition/region/platform/price/quantity summaries. Stateful picker/variant/clone callbacks проверяют token/context до сброса активного ввода; старые безtoken variant buttons отклоняются.

Emoji: **draft override > variant override > route override > template default**. Встроенный default — 🔑. Emoji входит в оба итоговых заголовка, а не только в UI. Явные текстовые overrides имеют тот же приоритет. Draft хранит template snapshot: последующее изменение template не переписывает уже подготовленный текст.

## Product Variants / Editions

Variant — необязательная scoped запись для **template + родительский route**, без копирования всего template. Хранит stable ID, `display_name`, `aliases`, `enabled`, `overrides`, необязательный `route_override`, timestamps и revision. `overrides` поддерживает `variables` (defaults), `emoji` и отдельные `title_ru`, `title_en`, `description_ru`, `description_en` с теми же безопасными placeholders.

Наследование: **draft > variant > parent route > template**. Если variant не переопределяет текст, используется базовый текст с variant variables. Оба языка обязательны в итоговом draft. Явно введённые wizard/row variables выше variant defaults; «-» оставляет наследуемое значение. Черновик сохраняет variant snapshot: изменение/отключение variant не переписывает подготовленный draft. Проверка node по актуальному кешу сохраняется при редактировании.

Пример: один GAME_KEY и один Elden Ring route. Без variant действуют прежние defaults (edition Standard, emoji 🔑). Deluxe variant задаёт `edition=Deluxe Edition`, `emoji=⭐`; тот же template/route формирует Deluxe RU/EN. Draft override `emoji=🎁` имеет приоритет над ⭐. Отдельные GameRoute и ProductTemplate для Standard/Deluxe не нужны.

В Telegram: Game Routes → route → «Варианты / Editions». Создание/редактирование через небольшой JSON editor, preview и явное сохранение. `enabled=false` либо ⏸ отключает variant для новых drafts без удаления старых записей. При выборе игры показываются enabled variants с пагинацией и кнопкой «Без варианта / defaults». Без variants дополнительного шага нет. Поиск имени/alias в массовом вводе точный, Unicode case-insensitive; конфликтующие aliases дают ошибку строки, без угадывания.

Для другого раздела **той же игры** editor принимает `route_override_url` — явный URL подтверждённого COMMON node. Он преобразуется в безопасный snapshot `template_id/game_name/node_id/section_name/metadata`; исходный route остаётся единственным и не меняется. При замене уже сохранённого override удалить прежний `route_override` и указать новый URL; `route_override=null` возвращает родительский node. Другие игры, неподтверждённые nodes и service/hidden fields отклоняются. При stale template/catalogue variant можно безопасно отключить отдельной кнопкой ⏸.

Пример JSON (идентификаторы template/route берутся из editor):

```json
{"template_id":"GAME_KEY","route_id":"example-route","display_name":"Deluxe","aliases":["Deluxe Edition","DE"],"enabled":true,"overrides":{"variables":{"edition":"Deluxe Edition"},"emoji":"⭐"}}
```

## Routes и каталог

Ключ соответствия: **owner + account + template + canonical game**. Одна игра может иметь отдельные GAME_KEY/DLC_KEY/другие пользовательские templates. Aliases ищутся через Unicode casefold; конфликтующие aliases дают ambiguity, а не выбор первой записи.

Поддержаны только COMMON nodes, подтверждённые в каталоге текущего Account. У установленной библиотеки `SubCategory.public_link` имеет вид `https://funpay.com/lots/ID/`, а private link — тот же путь с `trade`. URL с чужим host, credentials, port, query, fragment или `/chips/` не принимается. Произвольные URL не загружаются.

Каталог — уже инициализированный cache `Account.subcategories`; новый scraper/дополнительный Account не создаётся. Поиск каталога не делает HTTP. Из собственного лота read-only читаются game/category, node и section. Source lot ID используется только для выбора, не сохраняется как будущий target. Формы, CSRF, delivery secrets и hidden поля не читаются и не копируются.

В одиночном «Создать» сначала ищутся сохранённые routes: canonical name, alias, затем partial match. Поиск нормализует регистр Unicode, края и повторные пробелы. При найденном route каталог не читается. Если соответствия нет, `game_catalog.py` лениво копирует `Account.subcategories` в память на **900 секунд**; scope — runtime client + owner + account ID + объект Account, максимум 32 scope. Каталог в SQLite не сохраняется, фонового poller нет. В FunPayAPI 1.1.0 getter возвращает список, заполненный при `Account.get()`: «Обновить каталог» перечитывает именно этот список, а не загружает новый каталог с сайта. Он повторяет текущий поиск и меняет generation; прежние кнопки не действуют.

Несколько игр и разделов показываются с пагинацией и фактическими названиями. COMMON/category metadata не доказывают пригодность раздела для GAME_KEY, поэтому автоподбора по названию товара нет. Даже единственный результат требует «✅ Использовать». Перед записью повторно проверяются node, игра, template и account; совместимый concurrent route переиспользуется, конфликт отклоняется без перезаписи. Query не становится alias. После confirm мастер сразу продолжает variant/defaults и draft; отмена draft сохраняет подтверждённый route. До confirm Cancel/Back ничего не записывают. Кнопки привязаны к текущим session/context/token и generation каталога.

При временной Timeout/ConnectionError можно показать прежний snapshot с явным предупреждением; подтверждение всё равно обязательно. Ошибка без snapshot или нетранзиентная ошибка даёт безопасный Retry UI без текста исключения. Force refresh позволяет повторить чтение; TTL ограничивает повторные lazy попытки. Existing Game Routes editor и names-only route import сохраняют прежний read-only путь. **Legacy mass engine использует saved routes**: неизвестная игра — ошибка строки, без автоматического подтверждения или цепочки диалогов; сначала создайте route одиночным flow либо существующим route import. Recent остаётся общей существующей системой.

При нескольких разделах игры names-only import даёт warning и требует явного URL. Registry подтверждает существование node, но не обещает поддержку будущего publish или пригодность раздела для всех типов товаров. Назначение product type владелец проверяет в preview. Изменение каталога требует повторного подтверждения route.

## Массовый ввод

Routes, пример с условными ID из каталога:

```text
Game A | https://funpay.com/lots/123/
Game B | https://funpay.com/lots/456/
```

Один URL можно отправить отдельно. В массовом списке допустимы `Game | URL` либо точное имя игры, когда каталог даёт однозначный результат. Смешанные/повторные/невалидные строки классифицируются отдельно; до confirm registry не меняется. Существующий route не переназначается молча — для обновления используется отдельный editor с revision check. Aliases/overrides editor принимает, например, `{"game_aliases":["Alias"],"overrides":{"emoji":"⚔️"}}`.

Drafts:

```text
Game A | 29.99
Game B | 24.99 | region=EU | emoji=🎁
Game A | Deluxe | 39.99
```

Общие variables берутся из wizard; per-row значения имеют приоритет. Форматы: `Game | price` и `Game | variant name/alias | price`, затем optional `key=value`. Без variant сохраняется прежнее поведение, даже если у игры есть variants. Неизвестный/disabled/неоднозначный variant даёт ошибку строки, остальные строки продолжают обрабатываться. Идентичные route+variant+node+payload внутри пакета пропускаются как duplicate; разные цены/варианты/nodes остаются отдельными drafts. Цена сохраняется десятичной строкой, без валютного пересчёта и без влияния на продажи/оборот.

Ограничения: 100 строк, batch до 2 MB, title до 200 символов с emoji, description до 10 000, variable до 500. Telegram имеет собственный лимит одного входящего сообщения; большие списки следует разбивать на пакеты. Favorites/pinned UI не реализован; есть search, pagination и recent по последним сохранённым drafts.

## Persistence и compatibility

Таблицы `product_templates`, `game_routes`, `product_drafts`, `product_batches`, `product_variants` добавляются в существующую `state.sqlite3` через `ReviewReceiptStore.initialize()`. Variant migration добавляет только таблицу/индексы; старые templates/routes/drafts и pending batches не переписываются. Новая БД не создаётся. Существующие таблицы/данные и старые lot drafts не заменяются. Подтверждение пакета атомарно, с per-row savepoints и сохранённым результатом для повторного callback. SQLite/network транзакции не смешиваются: новые batch writes вообще не вызывают HTTP.

Для настоящего Publish отдельно нужны доказанный fresh create-form contract, безопасная сборка только разрешённых пользовательских полей, durable dispatch/UNKNOWN handling и строгая remote verification. Этот пакет их не включает.


## Динамические параметры node

### Проверка формы сохранённого GAME_KEY route

Владелец может открыть **📦 Лоты → 🗺 Game Routes → GAME_KEY → сохранённый
маршрут → 🧪 Проверить форму раздела (только чтение)**. Открытие экрана не
загружает форму. Только явное нажатие вызывает существующий
`NodeFormService(client, store).get(node_id)` с текущим Account и общим RLock.
Кнопка одноразовая, привязана к текущим owner/account, объекту Account,
шаблону и версии сохранённого маршрута; навигация делает её устаревшей.
Недоступный в текущем каталоге маршрут отклоняется. Отдельного флага
enabled/disabled для GameRoute в существующей модели нет.

Короткий ответ содержит только подтверждённые числовые ID каталога и node,
статус «доступна / не удалось прочитать», число видимых SELECT-полей,
разрешённых парсером (0–100), при успехе и безопасный код диагностики при ошибке.
Технические имена, options, скрытые поля, HTML и сетевые данные не показываются.
Результат — только метаданные: **публикация НЕ проверена**. Ручная выдача,
идентичность NEW/create/edit, CSRF и write/readback contract этим не проверяются.

`get` может вернуть кеш возрастом до **900 секунд**; принудительного обновления
нет. Текущий клиент следует редиректам, поэтому это **не гарантированно одна
ограниченная HTTP-транзакция**. Проверки GET/parser/redirect не меняются.
Нет второго Account, фонового опроса, публикации или параллельного publisher.
На успехе и ошибке не сохраняются drafts, routes, variants, settings или лоты;
меняется только временное UI-состояние и существующий кеш в памяти. Повторная
проверка требует заново открыть маршрут. Старые кнопки не сбрасывают активный
мастер черновика. Live-проверка и deployment этой возможностью не разрешены.

Новый create flow использует [NODE_FORMS](NODE_FORMS.md): authenticated GET create-form, реальные visible selects по HTML name, DOM order, полный набор options и отдельное persistence. Duplicate labels не объединяются. Cache — память, 900s, без фонового worker. Если форма недоступна/неожиданна, generic Steam/Global/Standard fallback запрещён. Product name/game заполняются из GameRoute.

Content binding задаётся **явно** через optional `node_bindings` в ProductTemplate: variable → подтверждённый technical select name. Names в fixtures синтетические: их нельзя копировать как реальные настройки без проверки формы. Неизвестные legacy generic placeholders рендерятся как «—» с предупреждением, а не как выдуманные параметры. У real selects нет arbitrary custom value. Remaining template-only fields сохраняют гибридный picker/text UX.

Variant может хранить optional `node_values` (technical name → technical option value), а также прежние emoji/text overrides. Legacy variable preset допускается только через explicit binding и однозначное точное совпадение option value/label. Конфликт с реальным выбранным параметром не скрывается. Create Similar и reopen проверяют current schema; пропавший option требует нового выбора, изменение только label обновляет display без изменения source. Legacy drafts открываются с предупреждением и не преобразуются автоматически; при clone владелец заново подтверждает реальные поля.

Новые mass rows всегда `needs_resolution`: полноценный bulk node-field editor отложен. Старые drafts/batches, renderer и legacy service tests остаются совместимыми, schema SQLite не менялась.
