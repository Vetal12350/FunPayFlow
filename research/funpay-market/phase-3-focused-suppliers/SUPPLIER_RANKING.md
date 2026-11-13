# TOP-10 поставщиков, TOP-20 SKU и условный запуск

Это порядок квалификации и запросов, не рейтинг подтверждённой надёжности/доходности. Сопоставимых all-in quotes нет, поэтому «самый дешёвый» победитель не назначен. Не считать разных посредников независимыми upstream sources, пока invoice chain UNKNOWN.

## TOP-10

| Приоритет | Компания | Направление | Обоснование/блокер |
|---|---|---|---|
| 1 | ReSellCodes | Steam login / Stars / ID | Самая конкретная public price база; SKU quote, происхождение и UA approval не закрыты. |
| 2 | Nervixy | Steam login / Gifts | Прямой loginAPI, Bybit, 4currencies; exact debit/credit и partner rights UNKNOWN. |
| 3 | Play Wallet | Steam login | Открытый API, понятный startup fee/Bybit; FX и источник средств/UA seller acceptance проверить. |
| 4 | PacketUP HUB | PUBG / Balance EUR / mobile ID | Точные EUR20/50/100 и code/ID routes; existing email, live quote закрыт. |
| 5 | Bulk Epin | Steam login / Gifts / Stars / ID | Новый кандидат с quote/idempotency/polling, USDT/Binance Pay; первый email отправлен. |
| 6 | GameCore API | PUBG / mobile ID | Actual email уточнил USDT/minimum/no API fees; нет numeric quote/region/eligibility. |
| 7 | MooGold | PUBG Global Player ID | Новый первичный запрос по ранее найденному API-кандидату; no-password route обязателен. |
| 8 | KORSIT / dundle | WoW60EU / Roblox Credit | Международная группа с понятной identity, первый запрос; exact wholesale stock/permissions UNKNOWN. |
| 9 | NS.GIFTS | Steam Wallet candidate | Приоритет владельца, contact already made; пока только marketing API и нет quote. |
| 10 | DesslyHub | Steam login/Gifts / ID | Существующая переписка и API, но current commercial contract/quote не подтверждены. |

D7MAN отдельно интересен EUR sourcing и USDT, но self-hosted authorization, marketplace consent, ручная закупка 48h и отсутствие buying API оставляют его вне первого пилота. Mana Minds и CodesWholesale не подходят текущей модели без компании. InComm/Tillo/CY.SEND — upstream/API candidates, а не готовое открытое предложение физлицу. Крупный бренд не является доказательством низкой цены или разрешения FunPay.

## TOP-20 точных предметных запросов

| Приоритет | SKU | Товар | Обоснование/ограничение |
|---|---|---|---|
| 1 | `STEAM_RU1000` | Steam Wallet1000 RUB по login | Первый запрос debit/credit; без точного quote/P запуск заблокирован. |
| 2 | `PUBG660` | 660 UC Global по PlayerID | Средний пакет и несколько API кандидатов; RU build/country должны совпасть. |
| 3 | `STARS500` | 500 Stars по username | Простой recipient flow; retail S и all-in quote UNKNOWN. |
| 4 | `FFWEEKCIS` | Free Fire Weekly CIS | Есть конкретный indexed CIS offer; проверить membership eligibility. |
| 5 | `MLWEEKRU` | MLBB Weekly Diamond Pass RU | Отдельный RU route, ID+Zone; не Global substitute. |
| 6 | `STEAM_RU5000` | Steam Wallet5000 RUB | Больше абсолютный оборот и риск средств; той же скидки 3% недостаточно. |
| 7 | `STEAM_UA500` | Steam Wallet500 UAH | Локальная валюта получателя, отдельный FX quote и seller eligibility. |
| 8 | `STEAM_KZ5000` | Steam Wallet5000 KZT | Прямой login product, не gift-card код; полная цена UNKNOWN. |
| 9 | `PUBG1800` | 1800 UC Global по ID | Более высокий чек; actual margin/спрос UNKNOWN. |
| 10 | `STARS2500` | 2500 Stars | Единая public rate не volume discount; проверить оборотный капитал. |
| 11 | `BN_EUR20` | Battle.net EUR20 Balance code | Packet точный SKU; country/currency compatibility и quote UNKNOWN. |
| 12 | `BN_EUR50` | Battle.net EUR50 Balance code | Крупнее чек; EU scope не означает RU/UA acceptance. |
| 13 | `WOW60EU` | WoW60 Days Game Time Europe | Обязательный предметный sourcing; exact EU stock/API/quote UNKNOWN. |
| 14 | `ROB1000RU` | RU-labelled1000 Robux code | Public denomination есть; issuer/result и resale блокируют запуск. |
| 15 | `ROB800RU` | RU-labelled800 Robux code | Не USD10Credit и не GamePass; дальнейший supplier verification. |
| 16 | `ARENA_ADV` | Arena mobile Monthly Advanced | Indexed offer есть, но exact pass mapping ещё неизвестно. |
| 17 | `BLOOD_ELITE` | Blood Strike Elite Pass | Удобнее identity mapping, чем смешанные BC/Gold nominal packs. |
| 18 | `HOKWEEK` | Honor of Kings Weekly Card | Простой product candidate, RU/CIS whitelist не доказан. |
| 19 | `DF60` | Delta Force60coins UID | Нужны publisher/Global region и base/bonus distinction. |
| 20 | `EXIT8UA` | The Exit8 Steam Auto Gift UA | Конкретный retail auto headline, buying quote и permitted automation UNKNOWN. |

## TOP-5 кандидатов для первого коммерческого запуска

Первые пять строк — **условные**, все HOLD. SteamRU1000 соответствует самому высокому business priority, но его rate/spread пока не посчитан. Если exact debit/credit не даёт положительной P, первым перейти к PUBG660 после коммерческого допуска, а не увеличивать оборот убыточного Wallet. Stars500, FFWeeklyCIS и MLWeeklyRU — резервы. AppleKZ не попал в TOP20 из-за отсутствия подтверждённого выпуска; WoW60EU исследован обязательно, но пока не готов к API pilot.

Допуск для любого SKU: valid company/person eligibility→explicit FunPay resale/provenance→country/build/recipient validation→свежий executable quote и реальные funding/FX→verified SellerRevenue→positive `0.94*S−C−e` с установленным владельцем запасом→SLA/refund/status contract. Низкая публичная цена не заменяет ни один этап.

## Общая экономика

```text
NetProfit = .94*SellerRevenue - AllInSupplierCost - OtherConfirmedCosts
Withdrawal = .06*SellerRevenue
Margin = NetProfit/SellerRevenue
BreakEvenS = (AllInSupplierCost+OtherConfirmedCosts)/.94
MaxC_zero = .94*SellerRevenue-OtherConfirmedCosts
Daily scenarios = 10/50/100 * NetProfit
```

В CSV все actual S/C/P/margin/break-even/дневные сценарии UNKNOWN. Supplier API fee=0 заполнен только для GameCore на основании email; полная C остаётся UNKNOWN. Cost-only floor — отдельное допущение, e/funding условно 0, не рекомендация прайса. ЦБ из phase 1/2 не исполнимый funding FX; новые local Steam rates — supplier displays, без нового currency conversion в confirmed profit. USDT/USD parity не доказана автоматически.

Чувствительность: рост C на 5% уменьшает P на.05*C, 10% — на.10*C. При фиксированном RUB SellerRevenue и USD закупке рост RUB/USD на 5% уменьшает долларовую выручку до S/1.05. Для Stars500 публичная base7.50 даёт лишь floor7.9787 USD, при base+5% floor8.3777. Прибыль 10/50/100 заказов рассчитывается только после подтверждения P; например условные.25 USD/order означают 2.5/12.5/25 USD/day, не прогноз или значение какого-либо SKU.
