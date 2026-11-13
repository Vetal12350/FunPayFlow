# Steam Wallet по логину: главное коммерческое направление

Здесь рассматривается **прямое зачисление по account login**. Steam Wallet code и Steam Auto Gift — другие товары. Ни один поставщик не предоставил подтверждённую текущую пару точного credited amount и полной закупки для нашего аккаунта.

## Сравнение доступных маршрутов

| Поставщик | Валюты/страны | Идентификатор, API, скорость | Цена/минимум/финансирование | Допуск и возвраты |
|---|---|---|---|---|
| ReSellCodes | Заявлены RU, RUB; UA, UAH; KZ, KZT; BY/UZ, CIS-USD | Login без password; validation/API; 30–60sec claimed | Public example97 USD debit→100 USD credit; min order$.15, deposit$3; USDT4networks | Individuals18+ general, UA отдельно UNKNOWN; failed→balance, credited wrong-login final |
| Nervixy | RUB/UAH/KZT/USD для CIS; USD не доказывает US | Login/check/status; gifts отдельно; REST/IP whitelist | Advertised3%→4.75% discount; no entry threshold; Bybit UID USDT | Application required; UA seller eligibility UNKNOWN; failed→balance |
| Play Wallet | Перечислен CIS, включая RU/UA/KZ; не TR/EU/US | Login, create→pay→status; rates API; claimed DEV | Startup2% supplier fee; 1%/0.5% only at real turnover thresholds; Bybit | General individual allowed; specific UA acceptance/issuer path UNKNOWN |
| NS.GIFTS | Exact route/currencies UNKNOWN | B2B API marketing, contract UNKNOWN | Quote/min/USDT UNKNOWN | Telegram уже был; не писать ещё раз |
| Bulk Epin | Protected account-based service; конкретный whitelist UNKNOWN | Documented quote/order/polling; recipient fields from offer | USDT6networks/Binance Pay; exact deposit/quote UNKNOWN | Новый запрос SENT; eligibility/refund questions pending |
| Dessly/Yard/Fazer/Eyad/Luban/Payli | Старые запросы на RU/UA/KZ; SKU contract нужен | Разная степень API evidence; не весь каталог — login | Exact quotes UNKNOWN; Plan discounts не подставлять за wholesale price | Уже опрошены; ответа с полной ценой нет |

Источник RSC_STEAM также показывает 82.86 RUB/45.48 UAH/444.60 KZT за USD. **Не применяем скидку 3% повторно:** описание не даёт однозначно установить, включена ли она уже в конкретный local FX/debit. RUB1000/5000/10000, UAH500 и KZT5000 в CSV — запрашиваемые точные суммы зачисления, их actual cost UNKNOWN. Нужны подписанная quote/payload и два отдельных поля: reseller debit и recipient credit.

## Проверка экономики скидки

Публичный USD пример даёт математический сценарий C=97, S=100, e=0: `P=94−97=−3 USD`. Это не реальная прибыль SKU и не предположение, что S всегда равно номиналу. При тех же условных затратах break-even S=97/.94≈103.1915 USD; для 10% margin по S требуется 97/.84≈115.4762 USD. Расходы funding/FX увеличат порог.

В общем виде при одинаковой базе оценки кошелька скидка d должна превышать 6%, чтобы продажа по этой базе покрывала только вывод; либо нужен markup продавца. Nervixy максимум 4.75% сам по себе 6% не покрывает. Play Wallet2% fee требует ещё большего markup. Курсовые различия могут изменить результат, но их нельзя объявлять прибылью без executable quote. Скидка — не net margin.

## География без подмены валют

| Покупатель | Требуемый credited amount/currency | Подтверждённый buying route |
|---|---|---|
| Россия | RUB1000/5000/10000 | Supplier claimsRSC/Nervixy/PlayWallet; account quote UNKNOWN |
| Украина | UAH500 | Те же, UA seller eligibility отдельно UNKNOWN |
| Казахстан | KZT5000 | Те же, FX/full quote UNKNOWN |
| Турция | Уточнить фактическую USD wallet currency; не обещать TRY | Новый запрос Bulk Epin; route UNKNOWN |
| EUR страны | EUR10, конкретная страна | Не подтверждён; Steam cardEU не заменяет login top-up |
| США | USD10 US account | Не подтверждён; CIS-USD для BY/UZ не US proof |
| Другие | Индивидуальный country/currency whitelist | Не расширять до «worldwide» по слову Steam |

## Допуск интеграции

Login — имя входа, не display name, profile URL или пароль. Supplier validation должна подтвердить eligibility/account currency и ожидаемый nickname, если доступен; не доказывает владение. Требуются buyer confirmation, quote expiry, max debit и min credited amount. Failed/timeout не повторять новым client reference; сначала status/reconcile. Refund to supplier balance и возврат покупателю FunPay — разные записи.

Steam Subscriber Agreement ограничивает account sharing, commercial transfers, automation и country/price circumvention. Наличие supplier REST API не доказывает разрешённый funding/gifting channel. До запуска получить sourcing/permission, UA individual acceptance и положительный `0.94*S−C−e`. Не использовать чужие cards/accounts или географическую подмену.

Лучшие **кандидаты по конкретности механики**, не подтверждённой цене: RSC/Nervixy/PlayWallet; международная альтернатива Bulk Epin пока на проверке. NS.GIFTS сравнить можно только после ответа в уже существующем Telegram-контакте. В первом commercial shortlist — SteamRU1000, но его запуск условный.
