# Источники и правила доказательности

Дата этапа: **13 ноября 2026 UTC**. Новый слой не изменяет отчёты phase 1/2. Публичный каталог/заявление поставщика, частный коммерческий ответ и исполнимая котировка — разные виды доказательств. Ни один endpoint создания заказа не вызывался.

## Предыдущие исследования и contact history

Проверены [PR157](https://github.com/Vetal12350/FunPayFlow/pull/157) и [PR158](https://github.com/Vetal12350/FunPayFlow/pull/158) через их отдельные ветки/локальные worktrees, а не по наличию в main.

- Phase1: [SUPPLIERS](https://github.com/Vetal12350/FunPayFlow/blob/ceb602e0c9702f834f7a56d717037d32fbfb30a4/research/funpay-market/SUPPLIERS.csv), [источники](https://github.com/Vetal12350/FunPayFlow/blob/ceb602e0c9702f834f7a56d717037d32fbfb30a4/research/funpay-market/SOURCES.md), [почтовый журнал](https://github.com/Vetal12350/FunPayFlow/blob/ceb602e0c9702f834f7a56d717037d32fbfb30a4/research/funpay-market/SUPPLIER_EMAIL_DRAFTS.md).
- Phase2: [SUPPLIERS](https://github.com/Vetal12350/FunPayFlow/blob/3d2e66cbfa01f3785fef5cd015980ea0e4378e4d/research/funpay-market/phase-2/SUPPLIERS.csv), [SKU_ECONOMICS](https://github.com/Vetal12350/FunPayFlow/blob/3d2e66cbfa01f3785fef5cd015980ea0e4378e4d/research/funpay-market/phase-2/SKU_ECONOMICS.csv), [NEXT_STEPS](https://github.com/Vetal12350/FunPayFlow/blob/3d2e66cbfa01f3785fef5cd015980ea0e4378e4d/research/funpay-market/phase-2/NEXT_STEPS.md), [источники](https://github.com/Vetal12350/FunPayFlow/blob/3d2e66cbfa01f3785fef5cd015980ea0e4378e4d/research/funpay-market/phase-2/SOURCES.md).
- Префикс P1:/P2: — ID или имя артефакта в соответствующем snapshot. Однодневные цены повторно не загружались без необходимости; их поля остаются non-executable.
- Обновление контактов: три drafts, описанные в phase 2, теперь найдены SENT. Phase 3 не отправлял их повторно. Широкий search не заменяет company-specific all-mail search непосредственно перед отправкой.

## Новые источники

| ID | Источник | Доступ | Что установлено / предел |
|---|---|---|---|
| APPLE_KZ_TERMS | [APPLE_KZ_TERMS](https://www.apple.com/legal/internet-services/itunes/giftcards/kz/terms-en.html) | LIVE_PRIMARY | KZ Account Balance/Add Funds terms, not wholesale inventory of externally transferable gift codes. |
| APPLE_PAYMENTS | [APPLE_PAYMENTS](https://support.apple.com/en-ie/111741) | INDEXED_PRIMARY; direct US/KZ variants429 | Kazakhstan: Apple Account balance from adding funds; not gift-card redemption entry. Indexed section retrieved; direct protection not bypassed. |
| STEAM_TERMS | [STEAM_TERMS](https://store.steampowered.com/subscriber_agreement/) | LIVE_PRIMARY | Commercial transfer, automation and country/price circumvention restrictions; provider API alone is not Valve permission. |
| STEAM_TR | [STEAM_TR](https://steamcommunity.com/groups/steamworks/announcements/detail/3728476412305766958) | PRIMARY_TITLE_ONLY; indexed linked announcement | Turkey historical switch to USD; current account/offer currency still needs confirmation. No TRY promise. |
| BN_SUPPORT | [BN_SUPPORT](https://eu.battle.net/support/en/article/25904) | REDIRECT_LOOP | Official support not readable; do not claim current RU/UA country compatibility from third-party guides. |
| WOW_SHOP | [WOW_SHOP](https://eu.shop.battle.net/en-gb/product/world-of-warcraft-game-time) | INDEXED_PRIMARY; direct shop authorization redirect not followed | EU store snapshot60DaysEUR25.98 retail, not wholesale quote/code-stock API or specific RU/UA redemption guarantee. |
| MAIL_HISTORY | Частная почта: только обезличенный итог | GMAIL_SENT_ALL_MAIL_DRAFTS | Business domains/status only. Prior inquiries recovered; no private sender details, raw headers, mail IDs or bodies exported. |
| MAIL_GAMECORE | Частная почта: только обезличенный итог | PRIVATE_COMMERCIAL_REPLY_2026-11-13 | USDT TRC20/BEP20 minUSD10; network fee borne by sender; no supplier funding/API fees or individual/volume pricing; no SKU quotes or UA approval. |
| MAIL_BCK | Частная почта: только обезличенный итог | PRIVATE_COMMERCIAL_REPLY_REVIEWED | UA individuals accepted subject KYC; EUR1 technical funding; VAT at checkout; no blanket FunPay permission and no per-SKU provenance certificates. |
| RSC_STEAM | [RSC_STEAM](https://resell.codes/en/steam-top-up) | LIVE_PUBLIC_SUPPLIER_CLAIM | Login-only CIS; public USD example and FX; local total requires quote to avoid double discount. No executed order. |
| RSC_GIFTS | [RSC_GIFTS](https://resell.codes/en/steam-gifts) | LIVE_PUBLIC_SUPPLIER_CLAIM | Profile/friend route, region/title quote; pricing examples not quotes and purchase/automation authorization unverified. |
| NERVIXY | [NERVIXY](https://www.nervixy.digital/en) | LIVE_PUBLIC_SUPPLIER_CLAIM | Login RUB/UAH/KZT/CIS-USD, gift API, Bybit USDT, advertised discount not all-in quote. |
| NS | [NS](https://ns.gifts/) | LIVE_PUBLIC_SUPPLIER_CLAIM | B2B API marketing; exact Steam route/quote not public; owner reports Telegram contact. |
| PLAYWALLET | [PLAYWALLET](https://playwallet.bot/api) | LIVE_PUBLIC_API | Startup2% fee; Bybit funding; individual/company general policy; recipient UA != seller eligibility. |
| PK_BNET | [PK_BNET](https://packetuphub.com/en/catalog/battle-net-eu) | LIVE_PUBLIC_SUPPLIER_CATALOG | EUR20/50/100 codes; API/USDT; account-country warning; no public wholesale prices. |
| BULK_CONTACT | [BULK_CONTACT](https://bulkepin.com/contact) | LIVE_OFFICIAL_CONTACT | support@bulkepin.com explicitly for partnerships. Official email verified before send. |
| BULK_API | [BULK_API](https://bulkepin.com/reseller-api) | LIVE_PUBLIC_API | Non-charging quote, idempotent create, statuses/transactions/polling; no public webhook/refund endpoint; funding rails advertised. |
| BULK_STEAM | [BULK_STEAM](https://bulkepin.com/steam-top-up) | LIVE_PUBLIC_SUPPLIER_CLAIM | Protected account-based service; codes separate; country/credit and account quotes not established. |
| BULK_STARS | [BULK_STARS](https://bulkepin.com/telegram-stars) | LIVE_PUBLIC_SUPPLIER_CLAIM | Username flow; pricing not fixed on landing; no password required; not official Telegram partner claim. |
| G2BULK | [G2BULK](https://g2bulk.com/) | LIVE_PUBLIC_SUPPLIER_CATALOG | Game ID products, US/Global Roblox and UC vouchers; no verified email or universal RU route. |
| G2BULK_API | [G2BULK_API](https://api.g2bulk.com/docs) | INDEXED_DOCS; open no readable lines | Reseller API indexed. g2b.com association indexed only; verify identity before contact. |
| MANA_PRODUCT | [MANA_PRODUCT](https://manaminds.com/products/battlenet-blizzard-digital-gift-cards-at-wholesale-price/) | LIVE_PUBLIC_SUPPLIER | Europe label but USD20/50 copy; no exact EUR quote; own Manastore portal is same company group. |
| MANA_ABOUT | [MANA_ABOUT](https://manaminds.com/about-us/) | LIVE_PUBLIC_ELIGIBILITY | Registered business entities only: not eligible under current individual/no-FOP model. |
| MANA_CONTACT | [MANA_CONTACT](https://manaminds.com/contact-us/) | INDEXED_OFFICIAL_EMAIL | info@manaminds.com in official indexed page; not sent because company-only gate. |
| D7_DISTRIBUTION | [D7_DISTRIBUTION](https://d7man.digital/authorized-reseller/) | LIVE_SELF_HOSTED_DOCUMENT_DISCLOSURE | Specific contract and direct-supply claims available on seller site; not independently authenticated by Blizzard. |
| D7_COMPANY | [D7_COMPANY](https://find-and-update.company-information.service.gov.uk/company/16941922) | LIVE_GOVERNMENT_REGISTER | Active D7MAN DIGITAL LTD legal registration; does not authenticate issuer agreement, stock or channel rights. |
| D7_CONTACT | [D7_CONTACT](https://d7man.digital/contact/) | LIVE_PUBLIC_TERMS; email obscured | USDT/USDC and manual quote; 48calendar-hour delivery after payment; not instant buying API. |
| D7_EU | [D7_EU](https://d7man.digital/regions/eu/) | INDEXED_SUPPLIER | EU/EUR scope and quantities; current price/stock/private reseller permission not confirmed. |
| KORSIT | [KORSIT](https://www.korsit.com/) | LIVE_OFFICIAL_CONTACT_AND_BRAND_LINK | Korsit B.V., Netherlands; official info@korsit.com; dundle webshop linked, one contact group. |
| DUNDLE_ROB | [DUNDLE_ROB](https://dundle.com/roblox/) | LIVE_RETAIL_DISPLAY | USD10/25 Credit display, not fixed-Robux quote or particular RU issuer confirmation. |
| DUNDLE_WOW | [DUNDLE_WOW](https://dundle.com/world-of-warcraft/) | LIVE_RETAIL_PAGE | 60-day product description; selected US locale has no products. Does not prove EU stock today. |
| TURGAME_B2B | [TURGAME_B2B](https://wholesale.turgame.com/) | LIVE_PUBLIC_B2B | Roblox/PUBG codes and API advertised; no exact eligible stock/price/email established. |
| TURGAME_TERMS | [TURGAME_TERMS](https://www.turgame.com/terms-of-service/) | INDEXED_PRIMARY_LEGAL | Gelsin Teknoloji Limited Şirketi; contact forms; unrelated-domain email not used. |
| BAXITY_WHOLESALE | [BAXITY_WHOLESALE](https://baxity.com/wholesale) | LIVE_PUBLIC_B2B | KYC/KYB, crypto/bank, secure code delivery; payment-dependent contracting entities; no verified email/API quote. |
| BAXITY_CONTACT | [BAXITY_CONTACT](https://baxity.com/contacts) | LIVE_CONTACT; email protection link unavailable | No mailbox inferred; no contact form submitted/terms accepted. |
| BAXITY_RETAIL | [BAXITY_RETAIL](https://baxity.com/store/) | LIVE_PUBLIC_RETAIL | PUBG60USD1.38 retail display, not wholesale ID quote; other excluded catalog categories ignored. |
| TILLO_GAMING | [TILLO_GAMING](https://www.tillo.com/gaming-platforms) | INDEXED_PRIMARY | Roblox and rewards API confirmed at brand level; specific RU code and FunPay channel not confirmed. |
| TILLO_CONTACT | [TILLO_CONTACT](https://www.tillo.com/contact-us) | LIVE_PRIMARY | Reward Cloud Limited, UK; official contact form, no business mailbox established. |
| INCOMM_ROBLOX | [INCOMM_ROBLOX](https://www.incomm.com/insights/incomm-payments-launches-roblox-gift-cards-in-hong-kong/) | INDEXED_PRIMARY_CORPORATE_RELEASE | HK Roblox distribution/issued values; no assumption RU B2B eligibility or terms. Press contacts not used. |
| INCOMM_API | [INCOMM_API](https://www.incomm.com/incentives/gift-card-api-integrations/) | PRIMARY_API_LANDING | Alder API distribution candidate; exact our-SKU access and resale channel unknown. |
| CYSEND_B2B | [CYSEND_B2B](https://www.cysend.com/business) | LIVE_PRIMARY | Enterprise rewards/REST API; not proof of Steam login, commercial resale or individual eligibility. |
| CYSEND_CONTACT | [CYSEND_CONTACT](https://www.cysend.com/contact) | LIVE_PRIMARY | CY.TALK SWITZERLAND S.A.; merchant account flow, no verified email in readable page. |
| CYSEND_ABOUT | [CYSEND_ABOUT](https://www.cysend.com/about) | INDEXED_PRIMARY | Roblox/Steam brands in corporate catalog; country/SKU approval required. |
| BCK_PARTNERS | [BCK_PARTNERS](https://www.buycheapkeys.com/en/partners) | SAME_DAY_INDEXED_SOURCE_FROM_EXISTING_RESEARCH | Steam Gifts/prepaid scope retained; ordinary Steam keys and account products excluded. |

## Ограничения доступа и сведения, которые не публикуются

Apple Support US/KZ429: далее использован индекс официального IE-раздела Kazakhstan, без обхода ограничений. Battle.net support дал redirect loop; shop переадресовал на авторизацию, она не выполнялась. В отчёт не переносились OAuth URL/state из ошибки. WoWEU product подтверждён индексом, не авторизованной покупкой. Steam announcement открылся только заголовком; currency account всё равно требует quote.

MooGold API повторное чтение не дало строк, contact-us404; официальный sales email использован из однодневного phase 2 snapshot. G2Bulk API — индекс/пустой render; contact route не дал адреса. Baxity/Manaminds/D7 email-protection ссылки не раскрывали почтовый ящик; адреса не угадывались и защита не обходилась. Manaminds email удалось установить отдельно в официальном индексе, но company-only делает запрос нецелесообразным. Неподтверждённый business@turgame из другого домена не принят за официальный контакт. Несколько предположительных URL вернули недоступность; вместо повторов записан пробел. RSC Steam Wallet найден через официальный navigation link, не приравнен к gift-card page.

Нет raw HTML/MIME, приватных Gmail URLs/IDs, имён сотрудников, документов/KYC, действующих кодов, API ключей, cookies или кошельков. Полные исходящие тексты и минимальные receipts лежат отдельно от репозитория; в Git только безопасные company subjects/statuses и краткие коммерческие выводы. Доступ к прежним runtime/private data FunPayFlow не использовался.

## CSV и классификация

UTF-8 BOM, comma, decimal point, UNKNOWN вместо отсутствующей величины. MASTER40 включает прежние 24, 6 компаний из уже существовавшей переписки и 10 новых веб-кандидатов. NEW16 = новые строки базы, **не 16 новых обращений**. CONTACT_REGISTRY41 включает отдельную unresolved contact group digitalcodes.net/digital-codes.net: юридическое тождество не доказано, в MASTER как проверенный новый поставщик не посчитана. Не считать 40 строк независимыми supply chains: upstream independence UNKNOWN, брендовые алиасы сведены.

AUTHORIZED_DISTRIBUTOR означает только указанный подтверждённый product/channel scope (например, официальный COD KZ канал Coda). VERIFIED_RESELLER — установленные retail/B2B identity и offerings/корреспонденция; не подтверждение issuer appointment или надёжности поставки. UNVERIFIED_INTERMEDIARY означает отсутствие доказательств для нужного SKU/канала, а не обвинение компании. RETAIL_ONLY — нет квалифицированного buying wholesale доступа. NOT_ELIGIBLE — обнаружено конкретное противоречие условиям текущего физлица. Неподтверждённые legal names/countries остаются UNKNOWN.

DEEP: предметно прочитаны текущие продуктовые/API/eligibility/refund сведения или коммерческий ответ для 20 поставщиков; это ограниченная документарная проверка, не аудит деятельности и не успешная покупка. Остальные 20 — ранее прочитанный snapshot/history/первичный screening. В цене разделены public base, actual all-in cost и scenario floor. Все actual S/C/P неизвестны; unknown fees не заменяются нулём. Exchange/network/activation расходы относятся к C единожды; отдельная комиссия FunPay не придумана.

Ссылки проверяются на URL syntax, source-ID соответствие и локальные цели. Статус недоступного URL не превращается в обещание работоспособности; массовый повторный crawl не выполняется. Код бота не менялся, runtime tests не требуются; проверки документов/CSV описаны в NEXT_STEPS.
