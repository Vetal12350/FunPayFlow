"""Offline artifact checks and independent rational arithmetic, no live requests."""
import csv
import hashlib
import json
import re
import subprocess
import sys
from fractions import Fraction as F
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
TOL = F(1,10**9)


def close(actual, expected):
    assert abs(F(actual)-expected) < TOL, (actual, str(expected))


def read(name):
    with (HERE/name).open(encoding='utf-8-sig',newline='') as file:
        reader=csv.DictReader(file)
        assert len(reader.fieldnames) == len(set(reader.fieldnames)), name
        rows=list(reader)
    for row in rows:
        assert None not in row and None not in row.values(), name
        assert all(v != '' for v in row.values()), (name,row)
    return rows


def main():
    config=json.loads((HERE/'assumptions.json').read_text())
    denoms=len(config['rub_denominations'])
    costs=len(config['scenario_cost_usdt_per_1000'])
    expected={'FX_EQUIVALENTS.csv':denoms,'TARGET_PRICES.csv':len(config['scenario_targets_usd'])*costs,
              'STEAM_PROFIT_MATRIX.csv':denoms*(2+costs*len(config['scenario_price_deltas']))+3,'SENSITIVITY.csv':31,
              'SCALING_SCENARIOS.csv':len(config['scenario_average_rub'])*len(config['scenario_orders_day'])*2,
              'PUBG_SCENARIOS.csv':len(config['pubg_scenario_seller_usd'])*3,
              'SUPPLIER_QUOTES.csv':39}
    datasets={name:read(name) for name in expected}
    for name,count in expected.items():
        assert len(datasets[name]) == count, (name,len(datasets[name]),count)
    quotes=datasets['SUPPLIER_QUOTES.csv']
    assert len({r['quote_id'] for r in quotes})==39
    for row in quotes:
        for field in ['actual_debit','all_in_cost_usd','actual_seller_usd','actual_net_profit_usd']:
            assert row[field] == 'UNKNOWN', (row['quote_id'],field)
    for row in datasets['FX_EQUIVALENTS.csv']:
        close(row['official_equivalent_usd'],F(row['rub'])/F(config['official_rub_per_usd']))
        close(row['indicative_market_equivalent_usd'],F(row['rub'])/F(config['market_rub_per_usd']))
    withdraw=F(config['withdrawal_rate']); retained=1-withdraw
    n=F(config['buyer_usd_per_1000_rub'])/(1+F(config['category_fee']))*retained
    for row in datasets['TARGET_PRICES.csv']:
        target=F(row['target_profit_usd'])
        close(row['max_all_in_cost_usd'],n-target)
        close(row['min_seller_usd'],(F(row['assumed_cost_usd'])+target)/retained)
        assert F(row['max_all_in_cost_cent_floor']) <= n-target
        assert F(row['min_seller_cent_ceiling'])*retained-F(row['assumed_cost_usd']) >= target
    for row in datasets['SCALING_SCENARIOS.csv']:
        o=F(row['orders_per_day']); u=F(row['funded_usdt_per_order']); sub=F(row['monthly_subscription_usd'])
        gross=F(row['seller_cent_floor_usd'])*o
        days=F(config['scenario_month_days'])
        net=gross-max(gross*withdraw,F(config['scenario_withdrawal_batch_minimum_usd']))
        profit=net-o*u-sub/days
        close(row['daily_profit_usd'],profit)
        close(row['monthly_profit_usd'],profit*days)
        close(row['daily_procurement_usdt'],o*u)
        close(row['capital_cash2_deposit1_safety1_usdt'],4*o*u+sub)
        close(row['capital_cash7_deposit1_safety1_usdt'],9*o*u+sub)
        fail=F(config['scenario_failed_fraction']); recover=F(config['scenario_supplier_recovery_fraction']); duplicate=F(config['scenario_duplicate_fraction'])
        risk=(1-fail)*net/o-u+fail*recover*u-duplicate*u-sub/(days*o)
        close(row['stress_monthly_profit_usd'],risk*days*o)
    for row in datasets['PUBG_SCENARIOS.csv']:
        p=F(row['hypothetical_seller_usd'])*retained-F(row['scenario_landed_usd'])
        close(row['profit_usd'],p)
        for count in [10,50,100]:
            close(row[f'profit_at_{count}_orders_day_usd'],p*count)
    # Re-generation must be deterministic for the six generated calculation CSVs.
    generated=[p for p in HERE.glob('*.csv') if p.name!='SUPPLIER_QUOTES.csv']
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in generated}
    subprocess.run([sys.executable,str(HERE/'generate.py')],check=True,cwd=HERE)
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==before[p.name] for p in generated)
    # Source identifiers and link syntax; inaccessible pages remain disclosed sources.
    source_text=(HERE/'SOURCES.md').read_text()
    source_ids=set(re.findall(r'^\| ([A-Z][A-Z0-9_]*) \|',source_text,re.M))
    for name,rows in datasets.items():
        for row in rows:
            for source in row.get('source_ids','').split(';'):
                assert source in source_ids, (name,source)
    links=0
    for path in HERE.glob('*.md'):
        text=path.read_text()
        for url in re.findall(r'\]\(([^)]+)\)',text):
            links+=1
            if url.startswith('http'):
                parsed=urlsplit(url)
                assert parsed.scheme=='https' and parsed.netloc and not parsed.username, (path.name,url)
                assert not re.search(r'(token|password|authuser|signature)=',url,re.I)
            else:
                assert (path.parent/url.split('#')[0]).exists(), (path.name,url)
    # Targeted secrets/private identifiers. Not a substitute for human diff review.
    patterns=[r'gh[pousr]_[A-Za-z0-9]{20,}',r'github_pat_[A-Za-z0-9_]{20,}',
              r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',
              r'mail\.google\.com/',r'\b[\w.+-]+@gmail\.com\b',
              r'(?i)(?:access_token|refresh_token|session_cookie)\s*[=:]\s*[\"\'][A-Za-z0-9_-]{12,}']
    for path in HERE.iterdir():
        if path.suffix not in {'.md','.csv','.json','.py'}:
            continue
        text=path.read_text(encoding='utf-8-sig')
        for pattern in patterns:
            assert not re.search(pattern,text), ('Potential private data',path.name)
    print(f'PASS: 7 CSVs / {sum(expected.values())} rows, rational arithmetic, deterministic regeneration, {links} Markdown links, source IDs and targeted secret scan.')


if __name__=='__main__':
    main()
