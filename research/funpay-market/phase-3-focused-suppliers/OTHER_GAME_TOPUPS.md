# Другие игры по ID: сохранить фокус на действующих SKU

Прежняя матрица phase 2 переиспользована, не исследованы сотни новых игр. Supplier catalog presence — не доказательство спроса. HoYoverse/Nitro и неуказанные подписки не приоритизировались.

| Игра | Предметный SKU/вход | Публичный ориентир, USD | Candidate/API и региональный барьер |
|---|---|---|---|
| MLBB | RU165Diamonds и Weekly Pass; UserID+Zone |2.83 и 1.89 RSC | Отдельно от Global156+16; MooGold запрос RU SKU получил, поддержка пока UNKNOWN |
| Free Fire |341Diamonds и Weekly Membership CIS; PlayerID |2.36 и 1.54 RSC | CIS whitelist; MENA/VN/Global не замена; RSC/Packet/MooGold candidates |
| Arena Breakout mobile |335Bonds и Monthly Advanced; UID |3.97 и.86 RSC | Mobile не InfinitePC; pass type/region точное совпадение UNKNOWN |
| Blood Strike |Strike Pass Elite; UID+server |3.12 RSC | Привязать entitlement к official product; BC packages не автоматически Gold+bonus |
| Delta Force |60coins и 320coins, GlobalUID |UNKNOWN | Global/Garena/Steam-code разделены; 320 != official300+44bonus |
| Honor of Kings |80Tokens и Weekly Card; ID |.84 и.93 RSC | Страна, first-purchase bonus и постоянный номинал разделены |
| COD Mobile |SG/MY529 CP или KZ88 CP, ID |Wholesale UNKNOWN | Не RU маршрут; COD keys исключены; нельзя менять account region ради покупки |
| Standoff2 |PARANOIA Gold Pass |Wholesale UNKNOWN | Consumer retail599 RUB из phase 2 не доказанный buying API; gold-by-ID route не установлен |

Новые Bulk Epin/G2Bulk показывают broad game catalog/API, но не дают подтверждённую закупку этих exact RU/CIS SKU. MooGold получил запрос только поддерживаемых маршрутов и четко заданных ML/FF с оговоркой не заменять регионы. GameCore actual reply подтверждает funding/fee model, а не SKU support.

Добавлять после коммерческого допуска: FF Weekly CIS и ML Weekly RU. Arena Advanced — reserve из-за indexed auto offer86.03 RUB и неоднозначного mapping. Blood Elite — следующий после серверной проверки. Остальные HOLD до точной product identity, issuer/resale permission, UA eligibility и all-in quote.

Приём: строковые ID, отдельный Zone/server, official/provider lookup до покупки; точная country whitelist и subscription eligibility. Сохранить idempotency, max cost/min credited quantity, status lookup, failed refund и ручной разбор UNKNOWN. Не запрашивать пароль или account session. Все actual C/S/P UNKNOWN; невысокая catalog price не доказывает margin.
