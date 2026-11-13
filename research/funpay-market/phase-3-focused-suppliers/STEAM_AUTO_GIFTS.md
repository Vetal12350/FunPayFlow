# Steam Auto Gifts: механизм и разрешение важнее рекламного spread

| Кандидат | Механизм/API | География | Закупка/возврат | Текущий статус |
|---|---|---|---|---|
| Nervixy | Invite link, REST, catalog/status | Заявлены десятки регионов; конкретные game/package нужно сверить | Exact quote UNKNOWN; failed gift→balance | Bybit USDT; first-contact email не установлен |
| ReSellCodes | Steam profile, friend request или direct gift по route; API | Только gifting-compatible country combinations | Exact quote UNKNOWN; public pricing examples не quote | USDT; official channel/provenance permission UNKNOWN |
| Bulk Epin | Steam gift product metadata, quote/idempotent order/poll | Exact whitelist UNKNOWN | Quote/refund/rejection terms запрошены | Новый запрос SENT; не утверждать наличие webhook |
| FazerCards | Friend invite и API ранее найдены |40+ advertised; exact SKU needs quote | Actual cost UNKNOWN; plan Wallet discount не использовать | Уже опрошен; повтор запрещён |
| DesslyHub | Steam gift merchant API | Game/edition/region-specific | Current auth/contract и quote UNKNOWN | Исправленный email уже SENT; не повторять |
| BuyCheapKeys | Partner platform имеет gift category | Exact authorized title/region UNKNOWN | Email: no blanket marketplace permission; quote/provenance gaps | Уже REPLIED; обычные keys/accounts исключены |

Конкретный commercial candidate: **The Exit8, UA, Steam Gift** — topplaygames phase 1 display$2.99 и autosend headline. Это не доказательство продаж, seller revenue или supplier price; RU$2.80/KZ$3.54 — другие региональные offers. Другие game keys и DLC не искались. Наличие international supplier не даёт разрешение на cross-region gift.

Steam Subscriber Agreement ограничивает коммерческую передачу и automation/account use, а также location/price circumvention. Supplier statement «official Steam purchase» подтверждает только собственное описание, не право коммерческого бота. Нужен письменно допустимый supply/partner channel; создавать/управлять Steam consumer accounts для обхода ограничений не предлагается.

До order: exact app/package/edition, recipient account store country, permitted sending country, profile/invite identifier, eligibility lookup и quote expiry. Supplier product_id не равен Steam appid. Во время исполнения WAITING_FRIEND/WAITING_ACCEPT не failed. Не заменять gift после timeout новым заказом; reconcile same reference. Rejection/expiry/partial fulfillment и предел refund согласовать заранее; refund to supplier balance не cash refund и не возврат FunPay.

Покупателю не нужны технические внутренности, только правильный регион, понятный invite/accept flow и SLA. Пароль/Steam Guard не требуется. Издержки funding, возможный fee, FX, возвраты и право FunPay resale неизвестны — actual profit UNKNOWN. Запуск gifts позже одного допущенного login/ID SKU.
