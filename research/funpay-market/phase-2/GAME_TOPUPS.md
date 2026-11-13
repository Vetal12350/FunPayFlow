# Игры по ID: конкретные SKU и реальные ограничения

Публичные цены ниже — ориентиры каталога или официальной розницы, не подтверждённая полная закупка. UNKNOWN не означает отсутствия товара. В каждой игре 3–5 SKU рассматриваются как ассортиментная матрица; только строки с идентификаторами из SKU_ECONOMICS включены в ограниченный отбор 30. Остальные не проходили отдельное глубокое сравнение.

| Игра | 3–5 предметных кандидатов | Метод и первичный канал | Публичная база / ограничения | Решение |
|---|---|---|---|---|
| Mobile Legends RU |35/55/165/275 Diamonds; Weekly Diamond Pass | ID+Zone; RSC_MLRU. Официальный Codashop PH подтверждает такой способ в PH, а не RU-reseller право | .60/.94/2.83/4.71/1.89 USD соответственно; Global78+8/156+16/234+23 не тот SKU | В резерв MLWEEKRU и ML165RU после region/quote |
| Free Fire CIS |110/341/572 Diamonds; Weekly; Weekly Lite | Player ID; официальный Garena по конкретной стране. Supplier CIS matrix требует whitelist | RSC phase1 .78/2.36/3.84/1.54 USD; Lite purchase UNKNOWN. Membership ≠ instant Diamonds | FFWEEKCIS и FF341CIS; строгая CIS retail выборка только 1 |
| Delta Force |60 coins; 320 coins; Weekly Pass | PacketUP Global UID; официальный Midasbuy US dfm поддерживает PC/console/mobile. GarenaID и Steam-code отдельны | Packet prices UNKNOWN. Официальный US60+5=.99 USD, 300+44=4.99, weekly=.99; не wholesale и не RU proof | DF60/DF320 HOLD: нельзя заменить 320 на 300+44 |
| Arena Breakout mobile |66/335/675 Bonds; Monthly Advanced; Premium Monthly | ID; mobile uamo, не InfinitePC abi; official Level Infinite/Midasbuy channel | RSC .79/3.97/7.94/.86/3.45 USD; роли Advanced/Premium сверить. Country scope UNKNOWN | ARENA_ADV/ARENA335; pass совпадение нужно подтвердить |
| Blood Strike |51/105/540BC; Strike Pass Elite; Strike Pass Premium | UID+server; официальный NetEase store имеет Gold+bonus, не тот же BC label | RSC .36/.73/3.64/3.12/7.05 USD. NetEase Elite3.99/Premium8.99 USD — retail | BLOOD_ELITE условный кандидат; BLOOD540 нельзя свести к official500+94 Gold |
| Honor of Kings |80/240/400 Tokens; Weekly; Weekly Plus | ID; официальный Midasbuy MY/CM подтверждает country-specific flow | RSC .84/2.51/4.19/.93/2.84 USD. Official MY80+80 first bonus не постоянные 160 | HOK80/HOKWEEK HOLD, RU supply UNKNOWN |
| COD Mobile |KZ88 CP; KZ460 CP promo; KZ960 CP promo; SG/MY529 CP | KZ official Coda/Activision store; SG/MY Packet UID — другой region/build | KZ499 KZT/от 1618.50 KZT/от 3243.50 KZT; 460/960 one-time offer. SG/MY quote UNKNOWN | COD88KZ/COD529SGMY не пилот для RU; KZ аккаунты отдельный будущий сегмент |
| Standoff2 |PARANOIA Gold Pass; Gold Pass+10levels; 10levels | Официальная help→store ссылка, consumer account/ID flow; B2B API не подтверждён |599/1099/699 RUB retail; не gold100/500/1000, которые в primary snapshot не найдены | STANDPASS HOLD; не обещать автоматическое API gold-by-ID |

## Какие игры добавлять

Сначала PUBG660 поID из отдельного отчёта; затем Free Fire Weekly CIS и Mobile Legends Weekly RU при коммерческом допуске. Arena Advanced — следующий интересный кандидат: есть новый indexed auto offer, но exact pass/region надо сверить. Blood Strike Elite лучше пригоден для product mapping, чем неясный 540BC. Delta/HoK — резерв до региональной матрицы, COD — отдельная KZ/SGMY возможность с неподтверждённым FunPay SKU спросом, Standoff пока без готового buying API.

Существенный спрос вне этих игр не подтверждён, дополнительные сотни товаров не собирались. Phase1 Whiteout99 не включён в deep phase2. HoYoverse и Nitro не приоритизируются.

## Обязательные данные и ошибки

Получать строковый Player ID с сохранением ведущих нулей; ограничения длины/символов брать у конкретного publisher/provider, универсального regex на все игры нет. ML требует отдельный Zone ID, Blood — server, PUBG — publisher build и country, COD — build/регион регистрации. Nickname lookup только через одобренный API, не через аккаунт покупателя. Повторная подписка/однократный bundle могут быть недоступны уже подписанному аккаунту; проверка должна происходить до оплаты поставщику.

Wrong ID, wrong zone, unsupported country → stop до dispatch. Supplier unavailable, insufficient balance → pause, не искать другой маршрут после неопределённого исхода. Order pending/timeout → reconcile по той же ссылке заказа, не покупать повторно. Final failed → refund claim; provider refund back to balance не равен уже проведённому возврату FunPay.

Источники: RSC_MLRU/ARENA/BLOOD/HOK, P1:RSCFF, PK_DELTA/PK_COD, ML_CODA, FF_GARENA, DF_MIDAS, ARENA_MIDAS/PASS, BLOOD_NETEASE, HOK_MIDAS/PASS, COD_COUNTRIES/KZ/REGION, STAND_HELP/STORE. Цены и ссылки — [SOURCES.md](SOURCES.md), selected economy — [SKU_ECONOMICS.csv](SKU_ECONOMICS.csv).
