# Финансовый аудит Steam RU / PUBG660

Начать с [FINANCIAL_AUDIT.md](FINANCIAL_AUDIT.md) и [DECISION.md](DECISION.md). Дата: 13 ноября 2026, Europe/Kyiv.

Это автономное исследование: Python standard library, без FunPayFlow imports, сетевых вызовов, секретов, регистрации, заказов или платежей. Старые PR157–159 служат источниками и не изменяются.

```bash
cd research/funpay-market/financial-audit-2026-11-13
python generate.py
python -m unittest -v
python validate.py
```

`assumptions.json` содержит параметры и статусы. Все денежные значения — **строки**, проценты — доли (`0.06`, не `6`). Измените параметры и запустите `generate.py`: он обновляет только шесть CSV расчётов. Markdown — датированный вывод аудита, автоматически не переписывается; при смене входных данных сравнивайте с новыми CSV. `SUPPLIER_QUOTES.csv` и `SOURCES.md` — записи доказательств, генератор их не меняет.

Базовые матрицы и масштабирование намеренно фиксируют паритет USD/USDT=1 для сопоставимого контрольного примера. Изменение стоимости USDT рассматривается в отдельном SENSITIVITY.csv; произвольный полный расчёт с другим q выполняется через CLI `--usd-per-usdt`. Это не автоматическая подстановка рыночного USDT-индикатора в подтверждённую закупку.

Контрольный полностью условный расчёт:

```bash
python calculator.py --buyer 11.81 --category-fee 0.02 --withdrawal 0.06 \
  --cost 10.40 --quote-currency USDT --usd-per-usdt 1 \
  --funding-fee 0 --network-usdt 0 --monthly-usd 0 \
  --order-fee 0 --order-fixed 0 --other 0 --target 0.40 --rub 1000
```

По умолчанию незаданные расходы и стоимость USDT — `UNKNOWN`, прибыль не рассчитывается. Явно переданный ноль — ваше предположение, не автоматическое доказательство отсутствия расхода. Вывод всегда `SCENARIO_NOT_VERIFIED_QUOTE`: числовой калькулятор не удостоверяет рыночные факты. Для известной seller price используйте `--fee-model seller --category-fee 0`. Buyer input — цена до надбавки за метод оплаты, **не** автоматически $12.18.

Для котировки курсом вместо debit: замените `--cost 10.40` на `--supplier-rub-per-usdt 96.153846153846`, оставив `--rub 1000`; меняя `--rub`, пересчитаете debit. Скидка уже должна входить в этот effective supplier rate. Для продажи в RUB задайте `--buyer-currency RUB --sale-rub-per-usd <исполняемый курс>`; без последнего выручка и прибыль останутся UNKNOWN. Пересчитываются USD-эквиваленты, а не предполагаемые правила округления рублёвого checkout.

`--quote-currency USD_WALLET` требует `--wallet-usd-per-usdt`: сколько единиц USD-баланса поставщика получено за 1 USDT после конвертации, но до отдельно указанной funding fee. `--usd-per-usdt` — фактическая USD-себестоимость приобретения USDT (при UAH funding: исполненная UAH/USDT цена делится на выбранную документированную UAH/USD базу). Если final quote уже включает сбор, не вводите его вторично. Subscription/monthly-orders и network/batch-orders распределяются по отдельным базам. Не используйте число будущих заказов как доказанный объём.

CLI использует базовую формулу вычета 6%. Альтернативная база вывода и minimum/fixed fees реализованы функцией `withdrawal` для всей пачки; это отдельно проверено тестами. Валюту продавца сначала привести к USD по **исполнимому** курсу вывода, если исходная выручка RUB. Нельзя подставить вместо него CBR; если 6% уже измеряет всю потерю до полученных USDT, не списывать тот же FX spread второй раз.

Состав CSV:

| Файл | Содержание |
|---|---|
| SUPPLIER_QUOTES.csv | 35 требуемых Steam точек (7×5), 4 PUBG660; действительные котировки UNKNOWN |
| STEAM_PROFIT_MATRIX.csv | 8 actual UNKNOWN; линейные сценарии трёх закупок и ±1/2/3% цены; нелинейный stress; public-rate interpretations |
| TARGET_PRICES.csv | Бюджеты для $0/.10/.20/.30/.40/.50/1; минимальные seller/buyer prices, консервативные центы |
| FX_EQUIVALENTS.csv | 8 номиналов; справочные эквиваленты, отдельно от закупки |
| SENSITIVITY.csv | USDT acquisition, supplier tariff, effective RUB/USDT, withdrawal, RUB revenue FX |
| SCALING_SCENARIOS.csv | 5 средних чеков × 6 объёмов × 2 subscription-сценария; прибыль, оборот, капитал, риск |
| PUBG_SCENARIOS.csv | Seller-price scenarios, overhead, цели, 10/50/100 заказов в день |

Аналитические дроби сохраняются с 12 знаками, внутренний Decimal precision40. Это точность расчёта, не точность котировок. `margin_fraction=P/S`, `return_on_cost_fraction=P/C`; умножить на 100 для процентов. Безубыточность по seller price; `max_all_in_cost` уже оставляет место всем затратам поставщика. `OtherCosts` не дублирует их. UNKNOWN не пустое поле и не ноль. Предполагаемые риски не названы комиссиями FunPay.

Для actual GO необходимы данные quote без персонального login/PlayerID: точное зачисление, debit, валюта, timestamp/expiry, fee inclusions, funding conversion/fees, собственная seller receipt и withdrawal preview. Сам калькулятор решений о покупке не принимает.
