# Продолжение: ответы и коммерческий допуск

Snapshot13.11.2026 UTC. Ветка `research/funpay-phase-3-focused-suppliers-2026-11-13`, от main `a9634a3baa61926df3dd0ce42312fa546a45c76d`. Phase1/2 не изменены; их источники pinned в SOURCES. Рабочий код, prod config и runtime не трогались.

## Завершённые этапы A–I

A: изучены PR157/158, suppliers/economics/next steps/email journals и доступная Gmail history. B: CONTACT_REGISTRY создан до отправок и обновлён после каждой. C–D:40 supplier rows, включая 16 новых к прежним 24 (10 web-discovered, 6 previously-contacted recovered), 41 contact identity groups с unresolved blocker. E–F:3 персонализированных запроса, каждый SENT verified. G–H:79 comparative rows с разделением public/confirmed/scenario, TOP10/TOP20 и 5 conditional pilots. I: документы подготовлены к commit/push/Draft PR; итоговый SHA/URL — в GitHub metadata и ответе владельцу.

## Первое действие следующего запуска: не отправлять повторно

1. Найти самый свежий CONTACT_REGISTRY/OUTREACH_STATUS **во всех соответствующих research branches/PR**, даже если main их не содержит. Текущий snapshot — этот Phase 3; не восстанавливать контактные статусы из старого phase 2, где 3 письма ещё drafts. Считать неопубликованный/старый реестр недостаточным основанием для send.
2. Проверить Gmail SENT+INBOX+DRAFTS+archive и company aliases/domains, с пагинацией. `in:anywhere` включает также spam/trash. Любой SENT/REPLIED/DRAFT_ONLY или SEND_IN_PROGRESS/unknown outcome блокирует новый primary запрос. Неизвестная identity→NEEDS_REVIEW. Digitalcodes.net/digital-codes.net не считать двумя свободными адресатами.
3. Запросы Bulk Epin/KORSIT/MooGold уже отправлены в Phase 3. Их не повторять и не отправлять follow-up без отдельного разрешения. Старые KRAB/GameCore/Dessly тоже уже SENT. NS.GIFTS ранее опрошен через Telegram.
4. Phase 3 отправлено 3 из 12; этот проход 3 из 6. Новый проход может использовать максимум 6 новых первичных писем при сохранении общего предела 12, после повторной проверки истории. Лимит — не план обязательной рассылки. Неподтверждённый mailbox, неполная история, bounced alias без нового официального адреса→skip. В случае API timeout перед новой попыткой сначала проверить SENT; не повторять вслепую.

## Какие ответы ожидать

- Bulk Epin: точный Steam credited amount/debit для RU/UA/KZ, whitelistTR/EU/US, Stars/UC totals, UA individual eligibility, minimum/funding/API fees, provenance, refunds и подтверждение polling-only contract.
- KORSIT/dundle: допустимый individual reseller, WoW60Europe stock/API, EUR Balance country/currency, Roblox issuer/result/RU compatibility, wholesale/USDT/VAT/marketplace scope.
- MooGold: GlobalID UC quotes без пароля, RU/UA/build eligibility, конкретные ML/FF routes, actual TRC20/funding, client-reference/error/refund behavior.
- Старые компании: только чтение ответов. GameCore уже дал fee/funding условия, а не SKU price; вопросы eligibility передал onboarding manager. Контактировать менеджера/посылать документы/делать follow-up отдельно не разрешено этим правилом follow-up. BuyCheapKeys подтвердил UA individuals+KYC, но не дал blanket FunPay permission и per-SKU provenance.

## Порядок получения экономики

1. Один exact SKU: SteamRU1000 или PUBG660. Прочитать timestamped quote, currency, account-level price, max debit/min credit, expiry и issuer/channel permission. Local Steam FX не умножать ещё на 3%, пока не ясно, где скидка уже включена.
2. Получить подтверждённую SellerRevenue отдельно от buyer display. Не использовать aggregatorUSD или nominal wallet amount вместо S. Market review windows/sales из прежних этапов остаются UNKNOWN, не обновлять их арифметически.
3. LandedCost: фактический USDT debit/funded balance, network+FX, supplier fee и обязательные paid features allocation. Не вносить депозит ради quote; регистрация/документы/оплата/договор передаются владельцу. Известный 0API fee GameCore не означает 0network/полную известную C.
4. Посчитать `P=.94*S−C−e`, marginP/S, break-even(C+e)/.94 и maxC=.94*S−e. При UNKNOWN не покупать. Прогноз заказов не строить из reviews/offers; 10/50/100 — только сценарии.
5. При допуске сохранить коммерческие доказательства приватно, в публичный слой только безопасные итоги. Не публиковать quote attachment с customer info, SKU codes или персональными реквизитами.

## Нерешённые продуктовые вопросы

AppleKZ: нет verified externalKZT card; Apple AddFunds не товар реселлера. Battle.net: EUR account currency/country и WoW Europe service region разные; RU/UA/KZ compatibility отдельных партий UNKNOWN. D7company register не proofBlizzard appointment, marketplaces consent separate. Roblox: issued-region Credit и fixedRobux не взаимозаменяемы; issuer/provenance/refund нужны. SteamAutoGifts: разрешение commercial automation и allowed source regions до любой разработки real bot. CODSGMY и KZ не RU, Standoff retail не API, Arena mobile не InfinitePC.

## Техническое продолжение

После коммерческого допуска — отдельная offline implementation задача по phase 2 AUTOMATION_STRATEGY: immutable order→SKU/recipient, quote/margin gate, durable idempotency/claims, unknown status reconciliation, signed callbacks или polling, отдельные supplier/FunPay refunds, private cost ledger. Fake provider и temporarySQLite с запретом реальной сети; не импортировать main ради introspection. На этом этапе ничего из этого не внедрено в работающий бот.

## Проверки перед публикацией

18 требуемых файлов; CSV schema/row widths/unique IDs, supplier references, source IDs и числовые cost-only formulas проверяются офлайн. Actual profit поля UNKNOWN, withdrawal0.06. LINK syntax/local targets проверяются без повторного массового crawl. Secret/private-mail identifier scan и ручной просмотр staged diff; полный текст писем и private receipt data вне Git. Сверить SHA256 phase 1/2 и что diff содержит только новую папку. Runtime tests не запускать для research-only изменения. CURRENT_STATE не меняется, так как runtime поведение не менялось.

Покупки, депозиты, активация кодов, платная регистрация, contracts, merge/deploy/restart и изменения объявлений не выполнялись. Перед отправкой в будущем перепроверить текущую пользовательскую авторизацию, company history и лимит; отсутствие временных private receipts не разрешает повтор.

Фактический итог проверки перед commit:18 файлов,40 уникальных supplier_id,16 новых строк,20 DEEP,41 уникальная contact group,79 ценовых строк,40 outreach decisions и3 SENT_VERIFIED. Проверены CSV, внешние ссылки по синтаксису (192 references), локальные цели, source IDs, арифметика и отсутствие типовых секретов/приватных mail identifiers. SHA256 всех27 прежних Markdown/CSV phase1/2 совпали. Последняя проверка ответов на3 новых запроса не обнаружила сообщений; SENT не трактуется как подтверждение доставки.
