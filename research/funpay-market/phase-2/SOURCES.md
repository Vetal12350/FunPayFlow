# Источники, происхождение данных и пределы проверки

Дата чтения/проверки: **2026-11-13 UTC**. Это дата исследования, а не гарантия обновления цены. LIVE_PUBLIC — страница прочитана, INDEXED — прочитан поисковый снимок; оба не доказывают выполнение заказа или достоверность рекламы поставщика. Цена из рекламы/каталога не становится подтверждённым B2B quote.

## Повторно используемые данные первого этапа

Неизменённый первый этап: [PR157](https://github.com/Vetal12350/FunPayFlow/pull/157), [исходники и источник IDs](https://github.com/Vetal12350/FunPayFlow/blob/ceb602e0c9702f834f7a56d717037d32fbfb30a4/research/funpay-market/SOURCES.md), [наблюдения](https://github.com/Vetal12350/FunPayFlow/blob/ceb602e0c9702f834f7a56d717037d32fbfb30a4/research/funpay-market/OBSERVATIONS.csv), [поставщики](https://github.com/Vetal12350/FunPayFlow/blob/ceb602e0c9702f834f7a56d717037d32fbfb30a4/research/funpay-market/SUPPLIERS.csv), [черновики и обезличенная почта](https://github.com/Vetal12350/FunPayFlow/blob/ceb602e0c9702f834f7a56d717037d32fbfb30a4/research/funpay-market/SUPPLIER_EMAIL_DRAFTS.md).

Префикс `P1:` в CSV ссылается на точный ID в источниках phase1. `P1:SUPPLIERS` означает прежний список 20. Проверки phase1 не перезапускались без необходимости. Наблюдения конкурентов помечены PHASE1_REUSED_NOT_REFRESHED.

Курс **P1:FX**: [ЦБ РФ, 13.11.2026](https://www.cbr.ru/eng/currency_base/daily/?UniDbQuery.Posted=True&UniDbQuery.To=13.11.2026): USD82.9803 RUB, EUR92.3322 RUB. Производный EUR/USD=1.1127. Это справочный cross-rate; не цена конвертации Binance/Bybit. Паритет USDT/USD не принят как подтверждённая сделка.

## Новые и уточняющие источники

| ID | URL | Доступ | Что именно подтверждает / ограничивает |
|---|---|---|---|
| RSC_STARS | [https://resell.codes/en/telegram-stars](https://resell.codes/en/telegram-stars) | LIVE_PUBLIC_SUPPLIER_CLAIM | Публично $0.015/star; 50–5000 указаны отдельно. Не персональный quote; на странице расходятся пределы 10000 и 100000. |
| RSC_ROBRU | [https://resell.codes/en/gift-cards/c/roblox_ru](https://resell.codes/en/gift-cards/c/roblox_ru) | LIVE_PUBLIC_SUPPLIER_CATALOG | RU-labelled Robux codes; происхождение/issuer mapping не доказаны. |
| RSC_MLRU | [https://resell.codes/en/game-top-ups/c/mobile_legends_ru](https://resell.codes/en/game-top-ups/c/mobile_legends_ru) | LIVE_PUBLIC_SUPPLIER_CATALOG | RU35/55/165/275/565; Weekly; отдельный канал от Global. |
| RSC_ARENA | [https://resell.codes/en/game-top-ups/c/arena_breakout](https://resell.codes/en/game-top-ups/c/arena_breakout) | LIVE_PUBLIC_SUPPLIER_CATALOG | Мобильный Arena: Bonds и разные pass, публичные USD bases. |
| RSC_BLOOD | [https://resell.codes/en/game-top-ups/c/blood_strike](https://resell.codes/en/game-top-ups/c/blood_strike) | LIVE_PUBLIC_SUPPLIER_CATALOG | BC-пакеты и passes; несовпадение с Gold+bonus официального магазина. |
| RSC_HOK | [https://resell.codes/en/game-top-ups/c/honor_of_kings](https://resell.codes/en/game-top-ups/c/honor_of_kings) | LIVE_PUBLIC_SUPPLIER_CATALOG | Tokens и Weekly; страна допуска не доказана. |
| PK_CODE | [https://packetuphub.com/en/catalog/pubg-mobile-prepaid-code](https://packetuphub.com/en/catalog/pubg-mobile-prepaid-code) | LIVE_PUBLIC_SUPPLIER_CLAIM | Отдельный UC-code каталог; цены закрыты; не подтверждение полномочий издателя. |
| PK_DELTA | [https://packetuphub.com/en/catalog/delta-force](https://packetuphub.com/en/catalog/delta-force) | LIVE_PUBLIC_SUPPLIER_CLAIM | Global UID; включает 320, что нельзя отождествлять с 300+44. |
| PK_COD | [https://packetuphub.com/en/catalog/call-of-duty-mobile](https://packetuphub.com/en/catalog/call-of-duty-mobile) | LIVE_PUBLIC_SUPPLIER_CLAIM | COD Mobile SG/MY UID; не RU/CIS route. |
| MOOG_API | [https://doc.moogold.com/](https://doc.moogold.com/) | PRIMARY_API_DOCS | Validate/product/partnerOrderId/status/callback/balance; USDT TRC20; аккаунтные цены не получены. |
| MOOG_PUBG | [https://moogold.com/product/pubg-mobile/](https://moogold.com/product/pubg-mobile/) | LIVE_PUBLIC_RETAIL | Global Player ID instructions; FROM retail price не quote. Парольные TH-варианты не допускаются. |
| MOOG_CODE | [https://moogold.com/product/pubg-m-prepaid-code/](https://moogold.com/product/pubg-m-prepaid-code/) | LIVE_PUBLIC_RETAIL | Prepaid UC-code product; не доказательство разрешения FunPay resale. |
| SEAGM_CODE | [https://www.seagm.com/pubgm-uc-global-code](https://www.seagm.com/pubgm-uc-global-code) | LIVE_PUBLIC_RETAIL_EUR | EUR prices; 660 sold out. Другие локали/валюты в одну выборку не смешаны. |
| SEAGM_HELP | [https://help.seagm.com/hc/en/articles/97-FAQs%3A-Pubg-Direct-Top-Up-Failed-Refunded%3F](https://help.seagm.com/hc/en/articles/97-FAQs%3A-Pubg-Direct-Top-Up-Failed-Refunded%3F) | INDEXED_PRIMARY_RETAILER_HELP | Midasbuy account linking, code expiry statement. Не гарантия срока конкретной партии. |
| CJS | [https://www.cjs-cdkeys.com/autokey/wholesale1/](https://www.cjs-cdkeys.com/autokey/wholesale1/) | LIVE_PUBLIC_B2B | Wholesale login, minGBP100, MOQ5, larger tiers20/100; API claim vs emailed-key workflow needs clarification. |
| ROB_REDEEM | [https://en.help.roblox.com/hc/en-us/articles/115005566223-How-to-redeem-and-spend-your-Gift-Card](https://en.help.roblox.com/hc/en-us/articles/115005566223-How-to-redeem-and-spend-your-Gift-Card) | LIVE_PRIMARY | Worldwide redemption of genuine codes; most credit local currency; issuer exceptions. |
| ROB_CARD_TERMS | [https://en.help.roblox.com/hc/en-us/articles/15062225302804-U-S-TERMS-AND-CONDITIONS-FOR-ROBLOX-GIFT-CARDS-AND-CREDITS](https://en.help.roblox.com/hc/en-us/articles/15062225302804-U-S-TERMS-AND-CONDITIONS-FOR-ROBLOX-GIFT-CARDS-AND-CREDITS) | LIVE_PRIMARY_US_TERMS | Authorized distribution and resale constraints; US terms не универсальный локальный контракт. |
| ROB_TERMS | [https://en.help.roblox.com/hc/en-us/articles/115004647846-Roblox-Terms-of-Use](https://en.help.roblox.com/hc/en-us/articles/115004647846-Roblox-Terms-of-Use) | INDEXED_PRIMARY | Ограничения вторичного обмена Robux; механика pass не даёт reseller authorization. |
| ROB_FEES | [https://create.roblox.com/docs/monetize-experiences](https://create.roblox.com/docs/monetize-experiences) | LIVE_PRIMARY | Own-experience pass share70%; affiliate sale creator60%/affiliate10%. |
| ROB_PASS | [https://en.help.roblox.com/hc/en-us/articles/203314040-Experience-Passes](https://en.help.roblox.com/hc/en-us/articles/203314040-Experience-Passes) | LIVE_PRIMARY | Pending escrow; точный срок не подтверждён. |
| ROB_REGIONAL | [https://create.roblox.com/docs/production/monetization/regional-pricing](https://create.roblox.com/docs/production/monetization/regional-pricing) | INDEXED_PRIMARY | Динамические региональные цены; номинальная цена pass не гарантирует фактическую оплату. |
| ROB_TRANSFER | [https://en.help.roblox.com/hc/en-us/articles/48510707796756-Sending-and-Receiving-Robux](https://en.help.roblox.com/hc/en-us/articles/48510707796756-Sending-and-Receiving-Robux) | LIVE_PRIMARY | Официальная Plus-передача: acceptance, age/parent checks, лимиты. Не wholesale API. |
| PUBG_SA_TERMS | [https://www.pubgmobile.com/gamecredits/redeem.html](https://www.pubgmobile.com/gamecredits/redeem.html) | LIVE_PRIMARY_COUNTRY_SPECIFIC | Официальные Game Credits, SA redemption, expiry на receipt, resale restriction; не RU/global contract. |
| MIDAS_RU | [https://www.midasbuy.com/midasbuy/ru/redeem/pubgm](https://www.midasbuy.com/midasbuy/ru/redeem/pubgm) | LIVE_PRIMARY_INTERFACE | RU code-redemption UI: Midasbuy login, Player ID, code. Партия, wholesale и право перепродажи этим не подтверждаются. |
| MIDAS_FAQ | [https://cdn.midasbuy.com/oversea_web/faq/faq.html](https://cdn.midasbuy.com/oversea_web/faq/faq.html) | LIVE_PRIMARY | UID top-up/redeem mechanism; country selection and support delays. |
| TG_STARS_API | [https://core.telegram.org/api/stars](https://core.telegram.org/api/stars) | INDEXED_PRIMARY | Telegram gift/purchase flows; Bot API не generic wholesale outbound-transfer API. |
| TG_STARS_TERMS | [https://telegram.org/tos/stars/](https://telegram.org/tos/stars/) | INDEXED_PRIMARY | Stars terms; только разрешённые purchase/gift flows, без доступа к покупательским секретам. |
| FRAGMENT | [https://fragment.com/stars/buy](https://fragment.com/stars/buy) | BLOCKED_403 | Не обходилось; текущие TON price и покупная комиссия UNKNOWN. |
| KRAB_PRICE | [https://api.krab.gg/v1/price?type=stars&qty=100](https://api.krab.gg/v1/price?type=stars&qty=100) | UNAVAILABLE | Не получен ответ с quote; причина авторизации не установлена; не повторять без необходимости. |
| ML_CODA | [https://www.codashop.com/en-ph/mobile-legends](https://www.codashop.com/en-ph/mobile-legends) | LIVE_PRIMARY_DISTRIBUTOR | ID+Zone и base+bonus, PH channel; не доказывает RU contract. |
| FF_GARENA | [https://shop.garena.ph/?app=100067](https://shop.garena.ph/?app=100067) | INDEXED_PRIMARY | Player ID пополнение в поддерживаемой стране; не универсальный CIS канал. |
| DF_MIDAS | [https://www.midasbuy.com/midasbuy/us/buy/dfm](https://www.midasbuy.com/midasbuy/us/buy/dfm) | LIVE_PRIMARY_US | Официальные PC/console/mobile предложения; base+bonus; US price не закупочная цена UA reseller. |
| ARENA_MIDAS | [https://www.midasbuy.com/midasbuy/nl/redeem/uamo](https://www.midasbuy.com/midasbuy/nl/redeem/uamo) | INDEXED_PRIMARY | Мобильный Arena = uamo; Infinite PC не тот SKU. RU eligibility UNKNOWN. |
| ARENA_PASS | [https://www.midasbuy.com/midasbuy/au/content/enjoy-a-fast-payment-experience-with-level-infinite-pass-b3ur81](https://www.midasbuy.com/midasbuy/au/content/enjoy-a-fast-payment-experience-with-level-infinite-pass-b3ur81) | INDEXED_PRIMARY | Arena в официальном Level Infinite PASS channel; не разрешение перепродажи. |
| BLOOD_NETEASE | [https://pay.neteasegames.com/BloodStrike/topup?c=hwyy_1.1.hpen&from=home](https://pay.neteasegames.com/BloodStrike/topup?c=hwyy_1.1.hpen&from=home) | INDEXED_PRIMARY | Gold+bonus и Elite/Premium passes; query-specific indexed snapshot, голый URL не показывает тот же каталог. |
| HOK_MIDAS | [https://www.midasbuy.com/celcomdigi/my/buy/hok](https://www.midasbuy.com/celcomdigi/my/buy/hok) | INDEXED_PRIMARY_MY | ID80+80 first-purchase bonus; не постоянный 160 SKU. |
| HOK_PASS | [https://www.midasbuy.com/btk/cm/shop/hok](https://www.midasbuy.com/btk/cm/shop/hok) | INDEXED_PRIMARY_CM | Weekly/WeeklyPlus, страна CM; не RU proof. |
| COD_COUNTRIES | [https://store.callofdutymobile.com/international](https://store.callofdutymobile.com/international) | LIVE_PRIMARY | KZ есть в списке; RU и UA в этом списке отсутствуют. |
| COD_KZ | [https://store.callofdutymobile.com/ru-kz/codm](https://store.callofdutymobile.com/ru-kz/codm) | LIVE_PRIMARY_CODA_ACTIVISION | KZ88CP499 KZT; специальные 460/960 CP одноразовые бонусные bundles. |
| COD_REGION | [https://support.activision.com/au/en/cod-mobile/articles/call-of-duty-mobile-region-changes](https://support.activision.com/au/en/cod-mobile/articles/call-of-duty-mobile-region-changes) | INDEXED_PRIMARY | Ограничения смены account region; регион не подменять. |
| STAND_HELP | [https://help.standoff2.com/ru/articles/5323902-как-получить-золото](https://help.standoff2.com/ru/articles/5323902-как-получить-золото) | LIVE_PRIMARY | Официальная справка ведёт в магазин; API реселлера не установлено. |
| STAND_STORE | [https://store.standoff2.com/en-RU](https://store.standoff2.com/en-RU) | LIVE_PRIMARY_RETAIL | PARANOIA pass/levels; не публичный B2B price или универсальный gold-by-ID API. |
| STEAM_KEYS | [https://partner.steamgames.com/doc/features/keys](https://partner.steamgames.com/doc/features/keys) | PRIMARY_STEAMWORKS | Release keys vs dev/beta, происхождение пакета и отзыв ключей; не открытая закупочная площадка. |
| FP_ARENA | [https://funpay.com/lots/1652/](https://funpay.com/lots/1652/) | PHASE2_SEARCH_INDEX | Skupshil и другие; exact offer IDs не раскрыты; диапазон 384–582 исключён из медианы из-за неоднозначного pass. |
| FP_GUDDI | [https://funpay.com/users/2436461/](https://funpay.com/users/2436461/) | UNAVAILABLE_CARRIED_LIMITATION | Ранее недоступен; агрегаторные наблюдения phase1 не выдаются за свежие прямые отзывы. |
| MAIL_COMMERCIAL | Не публикуется | PRIVATE_REVIEW_REDACTED | Повторно проверена существующая коммерческая почта: CodesWholesale/CJS условия и Reloadly. Без raw body, личных имён, mail IDs, URL и вложений. |
| CODE | [https://github.com/Vetal12350/FunPayFlow/tree/a9634a3baa61926df3dd0ce42312fa546a45c76d](https://github.com/Vetal12350/FunPayFlow/tree/a9634a3baa61926df3dd0ce42312fa546a45c76d) | LOCAL_PRIMARY_READ_ONLY | Исходники и docs прочитаны; runtime не импортировался и не запускался. |

## Недоступность и остановленные проверки

Новые попытки GGTide: профиль Guddi/offers и следующие страницы Stars/Roblox/PUBG не дали данных; серия остановлена. FunPay не дал свежих профилей/окон отзывов; поисковый индекс Arena доступен. RSC Delta URL недоступен; Steam Delta-code и GarenaID товары не подменяют Global UID. PacketUP Standoff недоступен. Midasbuy RU Delta/Arena попытки не дали надёжного каталога; вместо угадывания URL использованы доступные официальные country-specific источники. Fragment403 и недоступный KRAB quote не обходились. Один пакет веб-запросов не вернул результат инструмента: это отсутствие данных, не отрицательная цена/наличие.

Не проходились CAPTCHA, авторизация, KYC/KYB, не создавались аккаунты и реальные заказы. Исходные HTML-дампы, секреты API-примеров, покупательские данные и коды в репозиторий не сохранялись.

## Как читать три CSV

- UTF-8 с BOM, разделитель запятая, дробная точка. UNKNOWN — данных нет; не ноль и не отказ поставщика.
- COMPETITORS: строка — наблюдение предложения, а не уникальный продавец. Все seller revenue UNKNOWN. Колонки reviews/sales не взаимозаменяемы; свежие 7/30 дней неизвестны. Прямые URL объявлений унаследованы из агрегатора и не открывались. Где offer ID неизвестен, указана категория, без выдуманного ID.
- SKU_ECONOMICS: ровно 30 идентичностей SKU, 20 DEEP. Public base не landed cost. Все поля actual/confirmed UNKNOWN, кроме известного withdrawal rate0.06. Сценарные cost-only floors допускают нулевые неизвестные расходы исключительно как математическую нижнюю границу; это не коммерческий допуск. Референс EUR→USD не делает EUR retail benchmark оптовой закупкой.
- SUPPLIERS: прежние 20 плюс 4; для каждого сохранены контакты и условия, но не выполнен новый аудит старого утверждения. exact_sku_quote и landed_cost_confirmed UNKNOWN. Слова official/direct в рекламе не независимое доказательство происхождения.

## Проверка перед публикацией

Валидация CSV включает количество полей, уникальность SKU и поставщиков, обязательный UNKNOWN, числовые сценарные формулы и предел 20 глубоких проверок. Ссылки проверяются на синтаксис, локальные цели и связь source IDs; массовое повторное открытие внешних страниц не выполняется. Недоступные URL сохранены как ограничения, а не названы рабочими. Выполняется просмотр diff и поиск секретов/приватных mail identifiers. Подробности результата публикации — в описании Draft PR.
