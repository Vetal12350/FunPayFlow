# PUBG Mobile: ID top-up против UC-кодов

## Решение

Для первого технического пилота предпочесть **прямое пополнение 660 UC по Player ID**, после допуска поставщика и расчёта полной прибыли. Коды оставить вторым каналом, пока не подтверждены конкретная RU/global партия, разрешение перепродажи и обработка invalid/used. Использовать обе модели сейчас оснований нет. Эта рекомендация основана на готовности процесса и поставки, а не на доказанной более высокой прибыли.

## Официально ли существуют Midasbuy UC-коды?

**Да, механизм официальных PUBG Game Credits/UC redemption существует.** PUBG_SA_TERMS — первичные условия Proxima Beta для Южной Африки: отдельный официальный redeem, одноразовость, срок на receipt и ограничения перепродажи. Это не доказательство официальности любого кода со словами Midasbuy и не универсальный RU контракт.

Дополнительная первичная проверка MIDAS_RU: существует именно RU-интерфейс Redeem PUBG с выбором страны, входом Midasbuy, Player ID и code. Он подтверждает возможность официального redeem flow в RU UI. **Не подтверждает**, что любая Global карта активируется для этого аккаунта, конкретного региона и партии, или что наш поставщик имеет право перепродавать её через FunPay. Разделять существование интерфейса и авторизацию stock.

Это не Google Play/Apple gift card и не PUBG redeem promo-code для скинов. Их цена и механика не включены в выборку.

## Номиналы и публичные ориентиры закупки

| UC | RSC ID public base USD, phase1 | SEAGM UC-code retail EUR | EUR→USD справочно | Доступность code | Реальная landed cost |
|---|---|---|---|---|---|
| 60 | .90 | .84 | 0.9347 | Public listing; live stock not reserved | UNKNOWN |
| 325 | 4.45 | 4.17 | 4.6400 | Public listing; live stock not reserved | UNKNOWN |
| 660 | 9.00 | 8.34 | 9.2799 | SOLD OUT | UNKNOWN |
| 1800 | 22.60 | 20.83 | 23.1775 | Public listing; live stock not reserved | UNKNOWN |
| 3850 | 45.20 | 41.66 | 46.3551 | Public listing; live stock not reserved | UNKNOWN |
| 8100 | 88.68 | 83.33 | 92.7213 | Public listing; live stock not reserved | UNKNOWN |

SEAGM_CODE прочитан в EUR, а не USD; конверсия P1:FX на 13.11.2026 (EUR/USD≈1.1127). Это сравнение **public retail vs public supplier base**, не сопоставимых оптовых quotes. Не переводить 8.34 EUR как 8.34 USD и не считать sold-out660 доступной закупкой. Price другого locale не подмешан. PacketUP PK_CODE перечисляет эти же 6 номиналов и instant code/API, но цены закрыты. MooGold MOOG_CODE — ещё один product channel без подтверждённой wholesale цены или дистрибьюторской цепочки.

Prime и Prime Plus — отдельные subscription SKU: phase1 RSC public base .88 и 8.80 USD за месяц. Bakhtiyar124 Prime88.78 RUB в индексируемом FunPay заголовке. Не эквивалентны немедленному 60/660 UC: ежедневные начисления, условия claim и совместимость подписки требуют отдельного product sheet. Подробно выбран только PrimePlus; actual C/P UNKNOWN.

## Сравнение бизнес-моделей

| Критерий | Прямое пополнение | UC-код для Midasbuy |
|---|---|---|
| Подтверждённая закупочная цена | UNKNOWN; RSC public bases известны | UNKNOWN; SEAGM retail benchmark, Packet quote закрыт |
| Цена продажи |60 UC display1.08/1.09/1.12 USD, регионы не сверены; прочие UNKNOWN | В phase1 агрегаторе 60 UC SMMonster1.13 USD; это одиночный code-label offer, не доказательство batch/region |
| Seller revenue / чистая прибыль | UNKNOWN / UNKNOWN | UNKNOWN / UNKNOWN |
| Спрос | Есть несколько ID предложений; продажи UNKNOWN | Есть предложение code60; доля кодов и продажи UNKNOWN |
| Скорость | Supplier promises minutes; фактический SLA UNKNOWN | Ключ можно выдать сразу из stock; активация покупателем отдельно |
| Удобство | Нужны корректный ID и проверка nickname | Нужны own Midasbuy login/link, ID и ввод кода; пароль продавцу не нужен |
| Автоматизация | Create→status; без stock; неизвестный исход покупки блокирует retry | Reserve code→deliver-once; API Packet заявлен; partner access не проверен |
| Риск | Ошибочный ID/region, уже выполненный заказ, pending | Used/invalid/expired, утечка stock, batch region, право resale |
| Возврат | Только подтверждённый supplier failed/refunded; wrongID обычно final | Раскрытый код обычно final; invalid/used claim отдельно; expired policy зависит от поставщика |
| Регион | Exact publisher build/account country и whitelist поставщика | RU UI есть, но требуется batch eligibility; Global retailer исключает CN/JP/KR/TW/VN |

Цена SMMonster не вошла в matched median для 660: другой номинал и только агрегаторный title, source P1:GGUC. Нет основания считать коды распространённее ID. Ни reviews7/30d, ни подтверждённых продаж по моделям не получено.

## Активация, срок, проверка статуса и рекламации

Покупатель сам открывает официальный Midasbuy для страны аккаунта, входит в собственный Midasbuy, при необходимости связывает Player ID, сверяет nickname, вводит код и подтверждает redeem. Точное требование linking зависит от route: SEAGM_HELP его описывает; не обещать выдачу без входа по одному Player ID. Продавец не запрашивает email password, Roblox/Telegram/PUBG session или код входа.

Срок: в SA terms он на receipt; SEAGM help заявляет 1 год с возможным меньшим остатком на партии. Это не гарантированный год от нашей продажи. Для закупки получить expiry каждой партии и запас времени до истечения. Публичного non-consuming check-unused API не обнаружено. Формат/длина code и supplier-issued status не доказывают, что он не использован; пробное погашение потребляет товар и здесь не выполнялось.

Нерабочий/used: зафиксировать supplier order reference, batch, момент выдачи и обезличенную ошибку; не публиковать код. Сопоставить supplier delivery/usage evidence. RSC требует bad-code claim в 24h; Packet24h по условиям. До решения не выдавать второй оплаченный код и не делать повторную покупку на UNKNOWN. Вернуть средства покупателю и провести replacement только по согласованной процедуре после подтверждения отказа/компенсации. Customer screenshot сам по себе не доказательство unused.

## Что подтвердить для запуска

Для 660ID: live quote → точная цена продавца → network/FX → issuer/build/region → UA individual approval → positive P. Для 660code: дополнительно commercial distribution authorization, exact redeem country/account type, batch expiry и support for invalid/used со сроком решения. Не принимать seller text «активация Midasbuy» вместо первичного/договорного подтверждения.
