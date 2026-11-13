# Продолжение после phase2: коммерческий допуск, затем offline интеграция

## Что завершено

- Этап A: использованы phase1 CSV/почта/источники без повторного исследования; выбран набор 30 SKU. Неизменённый phase1 — PR157/commitceb602e0c9702f834f7a56d717037d32fbfb30a4.
- Этап B:20 deep reviews, первичные механики Roblox/PUBG/игр, public prices и 24 suppliers. Неустановленные S/C/e оставлены UNKNOWN.
- Этап C: TOP20/TOP10/TOP5/TOP3 моделей и архитектура. Коммерческий допуск пока 0 SKU. Это завершённый этап с пробелами, не рекомендация начать закупки.
- Исходная main a9634a3baa61926df3dd0ce42312fa546a45c76d. Ветка research/funpay-market-phase-2-2026-11-13. Рабочий код/phase1 не менялись. Публикация выполняется только для этой папки; итоговый SHA и PR доступны в GitHub metadata.

## Действия следующего ассистента в порядке ценности

1. Прочитать EXECUTIVE_SUMMARY, SKU_ECONOMICS, SUPPLIERS и source registry; не повторять все поиски. Сверить новые ответы в существующей supplier переписке с последней датой исследования. Никогда не публиковать личные имена/адрес отправителя, Gmail IDs/URLs, raw письма, коды и секреты.
2. Получить **по одному полному коммерческому пакету** для PUBG660, FF Weekly CIS, ML Weekly RU, Arena Monthly Advanced и Stars 500. Поля ниже. Если поставщик требует компанию/не принимает UA individual, закрыть кандидат с причиной, не обходить KYB.
3. Снять 3–5 свежих сопоставимых retail offers на те же SKU/метод/region. Важно получить именно SellerRevenue и отдельно BuyerPrice/валюту. Просить у владельца обезличенные суммы/официальный экспорт только если имеющихся разрешённых данных недостаточно; не выводить order/customer records в публичные CSV. Если FunPay закрыт, оставить UNKNOWN и записать новую дату, не повторять серию блокировок.
4. Пересчитать реальную landed cost по фактическому funding quote Binance/Bybit: paid amount, полученный balance, сеть, FX, fee, активация/подписка allocation. Только тогда заполнить actual P/маржу/10-50-100. Не переносить cost-only floor в confirmed поле.
5. Первый допуск — один точный SKU, один одобренный supplier, explicit positive P и приемлемый error/refund SLA. Нет положительной проверенной экономики — не запускать; расширить сравнительный quote только на ближайшую альтернативу, а не 100 новых товаров.
6. После коммерческого допуска отдельно реализовать offline adapter contract/state ledger по AUTOMATION_STRATEGY. Fake transport deny network, temp SQLite, crash/replay/UNKNOWN тесты. Реальные orders/launch требуют отдельного решения владельца, исследование их не разрешает.

## Поля обязательного commercial quote

Supplier legal identity/official contact; individual Ukraine without company eligibility; exact product_id/SKU/issuer/build; receiver region whitelist; resale via FunPay permission; authorization/invoice chain; quote timestamp+expiry+currency; base cost; order fee; funding rail/network and fees; actual FX/USDT conversion; min deposit vs API unlock; volume tiers; subscription allocation; delivery/SLA; wrong-ID and failed-order refund; unused-balance withdrawal; code expiry/used claim; idempotency/reference lookup and signed callback. API example, FROM price и обещанная скидка не quote.

Для Stars: отдельно all-in500/2500/100 в одно время. Для Roblox: issuer credit/directRobux mapping и RU compatibility; не подставлять 199/1785/5250 под 200/1700/5000. Для PUBGcode: country/batch expiration, non-consuming status availability и письменное commercial distribution permission; officialRU UI недостаточно.

## Почта и отсутствие дубликатов

Проверены доступные Gmail threads и drafts. Не создавать новый запрос, пока не просмотрены текущая ветка и SENT. Уже были обращения к PacketUP, Yard, FazerCards, Eyad, Luban и CJS; Reloadly ответил без quote. NS.GIFTS: пользователь сообщил прошлый контакт, поиск его не обнаружил — не считать разрешением написать повторно.

Три существующих **неотправленных** drafts: hello@krab.gg; support@gamecore-api.tech; partners@desslyhub.com. Во втором этапе не изменялись и не отправлялись. Старый адрес Dessly partners@cscase.com дал DSN; не повторять его. Перед любой отправкой представить владельцу адресатов и полный согласованный текст. Метка «FunPayFlow / Поставщики B2B» — только если поддерживается и отправка одобрена. Gmail password/OAuth/secret не запрашивать.

CodesWholesale существующий KYB/company-bank ответ — причина отложить, а не новый запрос на исключение. CJS есть wholesale workflow, но USDT/API/provenance не подтверждены. MooGold — новый кандидат, можно сначала продолжить read-only qualification; не перечислять деньги для разблокировки quote.

## Что не повторять

Не обходить FunPay/GGTide/Fragment защиту; повторять запрос только если появится конкретный новый доступ/источник. Не сравнивать Roblox разных методов, ML Global/RU, Blood BC/Gold+bonus, Delta Global/Garena/Steam, Arena mobile/Infinite, COD SG/MY/KZ. Не выдавать monthly leaderboard за rolling30d и reviews за продажи. Не использовать runtime private files/secrets ради расчёта.

Сохранить изменения следующего этапа отдельно, не перезаписывать phase1/phase2 snapshot. Не делать merge/deploy/restart/реальные покупки/публикацию лотов. На этом этапе нет отправленных писем или покупок.

## Проверки snapshot перед публикацией

CSV:30 уникальных SKU,20 DEEP,24 уникальных поставщика,4 новых к прежнему списку,29 наблюдений предложений. Проверены одинаковая длина строк/заголовков, обязательные UNKNOWN и арифметика cost-only scenarios. Проверены ссылки на локальные документы, source IDs и синтаксис165 URL references; внешние URL повторно массово не загружались. Поиск типовых secret patterns/private Gmail identifiers и ручной просмотр не выявили секретов или приватных покупательских данных. Публичные business contacts и seller handles оставлены для проверки источников. SHA256 всех13 прежних отчётов/CSV phase1 не изменились. Runtime tests не запускались: изменены только исследовательские документы.
