"""Regenerate ONLY this audit's named calculation artifacts, offline, from JSON."""
import csv
import json
from pathlib import Path
from calculator import (dec as D, round_money, seller_revenue, buyer_price,
                        economics, max_cost, min_seller, required_rate,
                        effective_rate, capital, expected_profit, withdrawal)

HERE = Path(__file__).resolve().parent
A = json.loads((HERE/'assumptions.json').read_text())
ONE, HUNDRED = D('1'), D('100')
FEE, W = D(A['category_fee']), D(A['withdrawal_rate'])
BUYER = D(A['buyer_usd_per_1000_rub'])
S = seller_revenue(BUYER, FEE)
N = S*(ONE-W)
DATE = A['checked_date']


def fmt(v):
    if v is None:
        return 'UNKNOWN'
    if isinstance(v, type(ONE)):
        return format(v.quantize(D('.000000000001')), 'f')
    return str(v)


def save(name, rows):
    with (HERE/name).open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]),lineterminator='\n')
        writer.writeheader()
        writer.writerows({k:fmt(v) for k,v in row.items()} for row in rows)


def matrix_row(n, scenario, buyer=None, cost=None, other=None, source='ASSUMPTIONS'):
    seller = seller_revenue(buyer,FEE)
    e = economics(seller,cost,other,W)
    return dict(sku='STEAM_RU_'+n, credited_rub=n, region='RU_RUB',
        scenario=scenario, status='ACTUAL_UNKNOWN' if scenario=='ACTUAL' else 'SCENARIO',
        buyer_usd=buyer, seller_usd=seller, all_in_cost_usd=cost, other_usd=other,
        withdrawal_rate=W, profit_usd=e['profit'], margin_fraction=e['margin'],
        return_on_cost_fraction=e['return_on_cost'], break_even_seller_usd=min_seller(cost,'0',other,W),
        max_all_in_cost_for_040_usd=max_cost(seller,'.4',other,W),
        source_ids=source, checked_date=DATE,
        assumptions='UNKNOWN actual inputs' if scenario=='ACTUAL' else f'USD/USDT=1; buyer markup={FEE}; withdrawal={W}; unrounded analytic model; see scenario label and assumptions.json')


def main():
    fx=[]
    for n in A['rub_denominations']:
        fx.append(dict(rub=n, official_equivalent_usd=D(n)/D(A['official_rub_per_usd']),
            indicative_market_equivalent_usd=D(n)/D(A['market_rub_per_usd']),
            cbr_times_indicative_usdt_rub_per_usdt=D(A['official_rub_per_usd'])*D(A['indicative_usd_per_usdt']),
            supplier_actual_cost_usdt=None, source_ids='CBR;INVESTING;BINANCE_SPOT',
            status='REFERENCE_EQUIVALENTS_NOT_PURCHASE_PRICES',checked_date=DATE))
    save('FX_EQUIVALENTS.csv',fx)
    targets=[]
    for target in A['scenario_targets_usd']:
        for cost in A['scenario_cost_usdt_per_1000']:
            cap=max_cost(S,target,'0',W)
            ms=min_seller(cost,target,'0',W)
            targets.append(dict(target_profit_usd=target,buyer_reference_usd=BUYER,
                seller_reference_usd=S,net_after_withdrawal_usd=N,max_all_in_cost_usd=cap,
                max_all_in_cost_cent_floor=round_money(cap,'down'),
                required_rub_per_usdt_at_parity=required_rate('1000',cap),
                assumed_cost_usd=cost,min_seller_usd=ms,min_buyer_usd=buyer_price(ms,FEE),
                min_seller_cent_ceiling=round_money(ms,'up'),
                buyer_from_cent_rounded_seller=round_money(buyer_price(round_money(ms,'up'),FEE),'up'),
                status='SCENARIO_NOT_QUOTATION',source_ids='USER_INPUT;ASSUMPTIONS',checked_date=DATE))
    save('TARGET_PRICES.csv',targets)
    matrix=[matrix_row(n,'ACTUAL',source='P3;USER_INPUT;FUNPAY_CATEGORY') for n in A['rub_denominations']]
    for n in A['rub_denominations']:
        ratio=D(n)/1000
        for cost in A['scenario_cost_usdt_per_1000']:
            for delta in A['scenario_price_deltas']:
                matrix.append(matrix_row(n,'HYPOTHETICAL_LINEAR_C'+cost+'_PRICE_DELTA'+delta,
                    BUYER*ratio*(ONE+D(delta)),D(cost)*ratio,D('0')))
        nonlinear=D(A['scenario_nonlinear_quotes_usdt'][n])
        extra=D(A['scenario_network_usdt_per_batch'])/D(A['scenario_orders_per_funding_batch'])+D(A['scenario_monthly_subscription_usd'])/600
        matrix.append(matrix_row(n,'HYPOTHETICAL_NONLINEAR_COST_PLUS_FIXED_ALLOC_600_MONTH',BUYER*ratio,nonlinear+extra,D('0')))
    rscbase=D('1000')/D(A['rsc_displayed_rub_per_usd'])
    matrix.append(matrix_row('1000','RSC_AMBIGUITY_IF_DISPLAYED_RATE_ALREADY_NET',BUYER,rscbase,D('0'),'RSC_STEAM;USER_INPUT'))
    matrix.append(matrix_row('1000','RSC_AMBIGUITY_IF_3PCT_SEPARATE',BUYER,rscbase*(ONE-D(A['rsc_displayed_discount'])),D('0'),'RSC_STEAM;USER_INPUT'))
    fazer=D('1000')/D(A['fazer_example_rub_per_usd'])*(ONE-D(A['fazer_gold_advertised_discount']))
    matrix.append(matrix_row('1000','FAZER_DOCUMENTATION_EXAMPLE_EXCLUDING_SUBSCRIPTION',BUYER,fazer,D('0'),'FAZER_STEAM;USER_INPUT'))
    save('STEAM_PROFIT_MATRIX.csv',matrix)
    sens=[]
    def add(variable,value,seller,cost,fee=W):
        e=economics(seller,cost,'0',fee)
        sens.append(dict(variable=variable,value=value,seller_usd=seller,all_in_cost_usd=cost,
            withdrawal_rate=fee,profit_usd=e['profit'],status='SCENARIO',source_ids='ASSUMPTIONS',checked_date=DATE))
    for q in ['.98','.99','1','1.01','1.02','1.04']:
        add('acquisition_USD_per_USDT',q,S,D('10.4')*D(q))
    for change in ['-.05','-.03','-.01','0','.01','.03','.05']:
        add('supplier_price_relative_change',change,S,D('10.4')*(ONE+D(change)))
    for r in ['82.9803','82.86','90','91.8803033852','94','95.3859389904','96','98','100']:
        add('credited_RUB_per_debited_USDT',r,S,D('1000')/D(r))
    for w in ['.05','.06','.07','.08']:
        add('withdrawal_rate',w,S,D('10.4'),D(w))
    for shift in ['-.05','-.02','0','.02','.05']:
        # Buyer RUB price anchored only as a hypothetical, actual withdrawal FX unknown.
        add('seller_RUB_to_USD_rate_change_FIXED_RUB_SALE',shift,S/(ONE+D(shift)),D('10.4'))
    save('SENSITIVITY.csv',sens)
    scale=[]
    for n in A['scenario_average_rub']:
        buyer=BUYER*D(n)/1000
        seller=round_money(seller_revenue(buyer,FEE),'down')
        quote=D(A['scenario_nonlinear_quotes_usdt'][n])
        funded=quote+D(A['scenario_network_usdt_per_batch'])/D(A['scenario_orders_per_funding_batch'])
        for count in A['scenario_orders_day']:
            orders=D(count)
            for monthly in ['0',A['scenario_monthly_subscription_usd']]:
                days=D(A['scenario_month_days'])
                sub=D(monthly)
                net_daily=withdrawal(seller*orders,W,A['scenario_withdrawal_batch_minimum_usd'])
                unit=net_daily/orders-funded-sub/(days*orders)
                risk=expected_profit(net_daily/orders,funded,sub/(days*orders),
                    A['scenario_failed_fraction'],A['scenario_supplier_recovery_fraction'],A['scenario_duplicate_fraction'])
                scale.append(dict(average_credit_rub=n,orders_per_day=count,buyer_usd=buyer,
                    seller_cent_floor_usd=seller,hypothetical_supplier_debit_usdt=quote,
                    funded_usdt_per_order=funded,monthly_subscription_usd=monthly,
                    daily_procurement_usdt=funded*orders,monthly_procurement_usdt=funded*orders*days,
                    profit_per_order_usd=unit,daily_profit_usd=unit*orders,monthly_profit_usd=unit*orders*days,
                    stress_profit_per_order_usd=risk,stress_monthly_profit_usd=risk*orders*days,
                    capital_cash2_deposit1_safety1_usdt=capital(orders,funded,'2','1',safety_days='1',upfront_fixed=sub),
                    capital_cash7_deposit1_safety1_usdt=capital(orders,funded,'7','1',safety_days='1',upfront_fixed=sub),
                    status='SCENARIO_NOT_DEMAND_FORECAST',
                    assumptions=f'q=1; nonlinear hypothetical tariff; funding {A["scenario_network_usdt_per_batch"]}USDT/{A["scenario_orders_per_funding_batch"]}orders; seller cents floored; daily withdrawal minimum {A["scenario_withdrawal_batch_minimum_usd"]}USD; failure/recovery/duplicate fractions={A["scenario_failed_fraction"]}/{A["scenario_supplier_recovery_fraction"]}/{A["scenario_duplicate_fraction"]}; {days}days',
                    source_ids='ASSUMPTIONS;USER_INPUT;SELLERIST',checked_date=DATE))
    save('SCALING_SCENARIOS.csv',scale)
    pubg=[]
    for seller in A['pubg_scenario_seller_usd']:
        for funding in ['0','.05','.15']:
            cost=D(A['pubg660_public_usd'])+D(funding)
            e=economics(seller,cost,'0',W)
            pubg.append(dict(sku='PUBG660_GLOBAL_PLAYER_ID',hypothetical_seller_usd=seller,
                buyer_usd=None,public_catalog_base_usd=A['pubg660_public_usd'],
                assumed_funding_overhead_usd=funding,scenario_landed_usd=cost,
                profit_usd=e['profit'],margin_fraction=e['margin'],return_on_cost_fraction=e['return_on_cost'],
                min_seller_for_040_usd=min_seller(cost,'.4','0',W),
                max_cost_for_040_usd=max_cost(seller,'.4','0',W),
                profit_at_10_orders_day_usd=e['profit']*10,profit_at_50_orders_day_usd=e['profit']*50,
                profit_at_100_orders_day_usd=e['profit']*100,status='SCENARIO_NOT_CONFIRMED_PROFIT',
                source_ids='RSC_PUBG;ASSUMPTIONS',checked_date=DATE))
    save('PUBG_SCENARIOS.csv',pubg)
    print('Generated 6 CSV calculation artifacts; no network or production access.')


if __name__ == '__main__':
    main()
