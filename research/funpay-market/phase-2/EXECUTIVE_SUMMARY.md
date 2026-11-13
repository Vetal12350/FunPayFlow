# FunPayFlow: коммерческая проверка, этап 2

**13.11.2026 UTC. Готовых к запуску SKU с подтверждённой положительной прибылью: 0.** Это не оценка отсутствия рынка: по большинству товаров есть публичные цены и API, но нет одновременно подтверждённых seller revenue, landed cost и коммерческого допуска поставщика. Прибыль оставлена UNKNOWN. Первый условный пилот — PUBG660 по Player ID после закрытия этих пробелов; покупать сейчас отчёт не рекомендует.

Создан отдельный отчётный слой: 30 screened SKU, 20 глубоких проверок, 24 поставщика (20 прежних +4), без изменения первого этапа и рабочего кода. [TOP20 и TOP10 suppliers](TOP_20.md), [экономика](SKU_ECONOMICS.csv), [наблюдения конкурентов](COMPETITORS.csv), [поставщики](SUPPLIERS.csv), [источники](SOURCES.md).

## Основные новые результаты

- Stars: RSC публикует .015 USD/star, 500 Stars=$7.50. Это public base, не all-in quote. Cost-only seller floor≈$7.98; retail S пока неизвестна. Самого выгодного фактического поставщика определить нельзя.
- Roblox: primary support подтверждает worldwide redemption genuine codes. RU label посредника не доказывает issuer/product и разрешение перепродажи. Game Pass30% creator fee, Plus transfer и gift credit — разные механизмы; не включать их в одну цену Robux.
- PUBG: официальные Game Credits и RU Midasbuy redeem UI существуют. Существование интерфейса не подтверждает легальность и совместимость конкретной оптовой партии. ID-пополнение сейчас лучше готово к проверке, code660 retail benchmark ещё и sold out.
- Mobile Legends: найден отдельный RU каталог 35/55/165/275 и Weekly. Global156+16 нельзя подставлять вместо RU165.
- Новое предложение Arena Monthly Advanced86.03 RUB auto headline перспективно для точной сверки, но не подтверждает продажи; другие Advanced/Premium предложения не сопоставимы без версии.
- CodesWholesale: существующий ответ требует KYB, company bank account и EUR30 activation; под текущую модель физлица не квалифицирован. CJS публично требует GBP100 deposit/MOQ5, crypto route UNKNOWN. MooGold интересен подписанным API и USDT TRC20, не доказанной ценой.

## TOP-5 условных товаров для пилота

### 1. PUBG660 UC по Player ID

1. **Спрос:** несколько предложений ID60 UC у заметных продавцов; спрос именно 660 и продажи UNKNOWN.
2. **Конкуренты:**60 UC$1.08/1.09/1.12, sample median1.09 при несверенных регионах; 660 min/median UNKNOWN.
3. **Поставщики:** RSC, PacketUP и MooGold; выбор после account/region quotes, ни один не одобрен.
4. **Закупка:** RSC public base$9.00; quote и полная C UNKNOWN.
5. **Прибыль:** UNKNOWN; cost-only seller floor$9.5745 без финансирования/прочих затрат. Для 10% margin сценарный floor$10.7143, не рыночная рекомендация.
6. **Автоматизация:** средняя сложность, UID lookup→quote→durable order→status; хороший первый offline adapter.
7. **Риски:** wrong ID, publisher/build/region, pending debit, UA eligibility и reseller authorization.
8. **Следующий шаг:** получить exact660 Global/RU eligibility и live account quote; снять подтверждённую S с сопоставимого заказа/продавецкого расчёта.

### 2. Free Fire Weekly Membership CIS

1. **Спрос:** конкретные membership offers и auto headlines; продажи и rolling reviews UNKNOWN.
2. **Конкуренты:**152.20/152.91/194.30 RUB; CIS явно лишь timurka075152.91, строгая CIS median UNKNOWN.
3. **Поставщики:** RSC CIS, PacketUP как запрос сравнения; не Eyad MENA вместо CIS.
4. **Закупка:** public base$1.54, C UNKNOWN.
5. **Прибыль:** UNKNOWN; cost-only floor$1.6383, реальные комиссии финансирования не включены.
6. **Автоматизация:** средняя; ID и membership eligibility, финальный статус и ledger.
7. **Риски:** country whitelist, состав/длительность pass, повторная подписка и irreversible wrongID.
8. **Следующий шаг:** сверить exact membership с CIS retail152.91 и получить цену продавца/тариф аккаунта.

### 3. Mobile Legends Weekly Diamond Pass RU

1. **Спрос:** наличие RU ассортимента; существующий Global конкурент не доказывает спрос RU pass.
2. **Конкуренты:** сопоставимый RU Weekly min/median UNKNOWN; 927.97 RUB за Global565 не используется.
3. **Поставщики:** RSC RU; PacketUP/YuPay только после подтверждения RU route.
4. **Закупка:** public$1.89, full C UNKNOWN.
5. **Прибыль:** UNKNOWN; cost-only floor$2.0106.
6. **Автоматизация:** средняя, ID+Zone и SKU-specific entitlement check.
7. **Риски:** RU vs Global, неверный Zone, ограничения активных passes, reseller origin.
8. **Следующий шаг:** получить 3 retail RU Weekly offers и supplier account quote/UA approval без реальной покупки.

### 4. Arena Breakout mobile Monthly Advanced

1. **Спрос:** indexed Skupshil offer с auto24/7; подтверждённых продаж нет.
2. **Конкуренты:**86.03 RUB, n1; другие 384.93/582.39 исключены как неоднозначные, median UNKNOWN.
3. **Поставщики:** RSC/PacketUP с проверкой exact monthly pass.
4. **Закупка:** public$.86, C UNKNOWN.
5. **Прибыль:** UNKNOWN; cost-only floor$.9149. Не объявлять разницу с 86.03 RUB реальной прибылью.
6. **Автоматизация:** средняя/высокая, mobile UID и однозначный entitlement mapping.
7. **Риски:** mobile/InfinitePC, Advanced/Premium translation, длительность/страна, слабое evidence спроса.
8. **Следующий шаг:** сверить publisher SKU и состав pass у конкурента/поставщика; только затем pricing.

### 5. Telegram500 Stars по username

1. **Спрос:** Guddi в Telegram category leaderboard; спрос прямых 500 Stars UNKNOWN.
2. **Конкуренты:** direct500 retail min/median UNKNOWN; 13 Stars gift не comparable.
3. **Поставщики:** RSC, KRAB, TgSuperstars и PLATFORM iFragment; cheapest all-in UNKNOWN.
4. **Закупка:** RSC public$7.50, остальные точные quotes отсутствуют; C UNKNOWN.
5. **Прибыль:** UNKNOWN; public-base-only floor$7.9787, 10% margin floor$8.9286.
6. **Автоматизация:** средняя, username resolve→confirmed recipient→quote→order→status.
7. **Риски:** irreversible wrong username, deposit liquidity, источник Stars и UA eligibility.
8. **Следующий шаг:** сопоставить одновременно quotes500/2500 и подтверждённую S; старый KRAB draft не отправлялся.

## Ответы на девять вопросов

1. **Где больше заработать?** Доказанного победителя нет. PUBG1800/3850 и Stars 2500 дают больший потенциальный абсолютный чек, но P и спрос UNKNOWN; начинать с 660 ради меньшего риска, а не максимальной маржи.
2. **Самые убедительные признаки спроса?** Из доступного — несколько предложений PUBG60ID, FF memberships и присутствие Guddi в Roblox/Telegram рейтингах. Это косвенные признаки; продаж/7–30d отзывов по SKU нет.
3. **PUBG ID или коды?** ID первым условным пилотом; codes только после batch/authorization/region/refund проверки. Сравнение реальной прибыли пока невозможно.
4. **Roblox RU?** Авторизованные genuine codes после issuer/credit mapping; Game Pass/Plus transfers не считать готовой допустимой B2B моделью.
5. **Лучший Stars supplier?** Не установлен. RSC .0150 — лучший доступный конкретный public benchmark; KRAB+1% и iFragment«от» не одновременные quotes.
6. **Игры по ID?** FF CIS и ML RU следующими; Arena pass условно; Blood Elite резерв. COD KZ/SGMY отдельные сегменты, Standoff без подтверждённого API не добавлять.
7. **Какие keys?** Ни одного нового KEY SKU не допущено. The Exit 8 UA gift — конкретный наблюдаемый кандидат при quote/provenance; Terraria/WoW60EU пока watchlist.
8. **Что автоматизировать?** Offline fulfillment/ledger/state machine на PUBG660 с fake supplier; затем ещё один одобренный ID SKU. Реальные закупки и запуск бота не выполнялись.
9. **Что подтвердить?** S/B, timestamped all-in quote, реальные funding/FX, UA individual eligibility, issuer/reseller permission, exact region/SKU, refund/SLA и надёжный lookup неизвестного исхода.

Gmail проверен: новых пригодных закупочных котировок не получено; существующие 3 drafts сохранены без отправки/дубликатов. Переписка опубликована только как обезличенные коммерческие выводы. Дальнейшая точная работа — [NEXT_STEPS.md](NEXT_STEPS.md), архитектура — [AUTOMATION_STRATEGY.md](AUTOMATION_STRATEGY.md).
