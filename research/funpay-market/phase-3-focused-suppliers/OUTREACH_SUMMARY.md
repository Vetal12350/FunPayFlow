# Outreach: 3 отправки, без повторов

Авторизация владельца в Phase 3 разрешает новые первичные коммерческие запросы, максимум 6 за этот проход и 12 за весь этап. Использовано **3/6 и 3/12**. Старые SENT13.11, включая ранее сохранённые drafts, в этот лимит новых Phase 3 не включены. Письма не подразумевают объём, компанию, покупку, платный аккаунт или договор.

| Компания | Официальный получатель | Отправлено UTC | Что запросили | Результат |
|---|---|---|---|---|
| Bulk Epin | support@bulkepin.com | 2026-11-13T18:28:17Z | Steam login top-up; Steam Auto Gifts; Telegram Stars; PUBG ID | SENT label + exact recipient/subject verified; reply pending |
| KORSIT / dundle | info@korsit.com | 2026-11-13T18:28:37Z | Roblox Credit; Battle.net EUR Balance; WoW60 Europe | SENT label + exact recipient/subject verified; reply pending |
| MooGold | sales@moogold.com | 2026-11-13T18:29:02Z | PUBG Global Player ID; MLBB RU; Free Fire CIS | SENT label + exact recipient/subject verified; reply pending |

Bulk Epin и KORSIT — новые компании в базе. MooGold был найден в phase 2, но не опрошен; это первое обращение, не повторное исследование контакта. KORSIT/dundle сведены в одну company history. Перед каждым send выполнен отдельный `in:anywhere` поиск по company/domain/aliases, включая SENT, INBOX, archive, drafts, spam/trash; next-page token отсутствовал. Прочитан общий DRAFTS list. Не найдено старого запроса/черновика этих компаний. Официальный контакт Bulk/KORSIT подтверждён текущей страницей; MooGold — однодневным первичным snapshot phase 2, текущий render docs пуст. Никаких угаданных email.

Перед каждым send реестр получил NEEDS_REVIEW/SEND_IN_PROGRESS; это блок на повтор при crash. После успеха SENT проверен, CONTACT_REGISTRY и OUTREACH_STATUS сразу сохранены. Неопределённых результатов отправки нет; повторная отправка не выполнялась. **SENT не равно подтверждённой доставке**, прочтению или коммерческому согласию. По последней проверке ответов на эти 3 письма нет; известный GameCore reply относится к прежнему запросу.

## Не отправлено повторно

KRAB.GG, PacketUP HUB, GameCore API, NS.GIFTS, DesslyHub, Yard API, Reloadly, FazerCards, Eyad Store, Luban Play, CodesWholesale, CJS CD Keys Wholesale, Payli, ASBIS Kazakhstan, STOFORY, Worldofcdkeys, epay / Euronet, BuyCheapKeys. Отдельная contact-only группа digitalcodes.net/digital-codes.net имеет NEEDS_REVIEW: обе уже получали запросы, юридическое тождество не установлено, ни одной новой отправки. NS.GIFTS — по сообщённому владельцем Telegram history, даже без Gmail thread. Dessly старый cscase bounce не основание ещё раз писать: исправленный запрос уже SENT.

## Пропущенные новые компании

- Mana Minds: registered-company-only; нет смысла отправлять заведомо неподходящий запрос физлица. CodesWholesale — существующий аналогичный барьер company bank/KYB, без нового письма.
- G2Bulk, D7MAN, TURGAME, Baxity: не установлен читаемый подтверждённый business mailbox. Адрес по паттерну не создавался; email protection/формы не обходились.
- Tillo, InComm, CY.SEND: в прочитанных источниках business/merchant form, нет подтверждённого релевантного mailbox и допуска физлица. Press emails не использовать для закупочной рассылки.
- Остальные прежние, но не опрошенные кандидаты: NEEDS_REVIEW или неполный email/contact history; автоматической отправки нет.

Неудачных вызовов отправки:0. Пропуск по качеству/адресу/допуску не выдаётся за техническую ошибку Gmail. Максимум 6 — предел, не квота: остальные слоты не расходуются на слабые контакты. Новые Gmail drafts не созданы; приватные полные тексты 3 исходящих и receipts сохранены отдельно от Git в рабочем каталоге временных материалов. Следующий запуск опирается на опубликованный реестр **и Gmail**, а не на сохранность временных файлов. Полные письма, sender identity, приватные IDs и raw ответы в публичный Git не включены.
