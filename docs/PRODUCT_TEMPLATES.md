# Product Templates и Game Routes

Этот слой создаёт **только локальные черновики**. Publish отсутствует; installed FunPayAPI 1.1.0 по-прежнему не подтверждает безопасный create-form contract. Никаких `save_lot`, modifying HTTP, AI-перевода или конвертации цен здесь нет.

## Telegram

Владелец открывает `📦 Лоты`:

- **Создать:** template → поиск/страница/recent game route → variant (если есть) → переменные → цена → RU/EN preview → сохранить draft.
- **Массовые drafts:** template → общие переменные → строки `Game | price` → summary → подтвердить готовые строки.
- **Game Routes:** template → собственный лот, URL или cached catalog → preview → подтвердить.
- **Product Templates:** список и полный JSON editor. При создании копии поменять internal `name`.
- **Черновики из собственного лота:** прежний flow и прежняя таблица без миграции/удаления записей.

Preview сокращает длинные поля, но «Полный RU/EN» показывает весь текст частями. Цена, emoji, RU/EN и variables редактируются до сохранения. Сохранённые drafts доступны после restart; редактирование использует revision check. Cancel не публикует и не удаляет исходные FunPay-лоты. Кнопка Cancel в карточке сохранённого draft оставляет локальную запись со статусом `cancelled`; отмена текстового ввода просто отбрасывает несохранённые правки.

Новый экран отменяет незавершённый ввод. Save/edit/cancel/full-text callbacks привязаны к поколению preview; confirmation variant — к своему preview. Старая кнопка не сохраняет и не отменяет другой draft и не сбрасывает текущий ввод. Старые confirmation callbacks после выхода/restart не восстанавливают сессию; готовые сохранённые drafts продолжают отображаться. Двойной confirm одного пакета возвращает уже сохранённый результат.

## Определение template

Встроенный `GAME_KEY` создаётся один раз для владельца/аккаунта; его пользовательские изменения не перезаписываются. Один template обслуживает любое число routes.

Поля JSON: `name`, `display_name`, `default_emoji`, `title_ru`, `title_en`, `description_ru`, `description_en`, `allowed_variables`, `defaults`, `validation_rules`. `id` генерирует registry; `revision` нужен при обновлении. Все четыре текстовых RU/EN поля обязательны. Имя template уникально без учёта регистра/лишних пробелов внутри account scope.

В текстах поддерживается только `{{variable}}`: без expressions, attribute access, filters, `eval` и повторной интерпретации значений. Стандартные переменные: `game`, `platform`, `region`, `edition`, `product_name`, `quantity`. Дополнительные безопасные имена разрешены через `allowed_variables`; credential/service names запрещены. Для правил поддержаны `choices`, `min_length`, `max_length`.

Defaults GAME_KEY: platform Steam, region Global, edition Standard, quantity 1. `game` фиксируется route; `product_name` по умолчанию равен игре и редактируется отдельно. В wizard «-» оставляет default; цена обязательна.

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
