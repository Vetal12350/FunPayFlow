"""Offline tests, including exact independent Fraction/integer-cent oracles."""
import csv
import json
import subprocess
import sys
import unittest
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

import calculator as c

D = Decimal
HERE = Path(__file__).resolve().parent


class CalculatorTests(unittest.TestCase):
    def test_reference_with_independent_fraction(self):
        # No Decimal model functions in the oracle formula.
        expected = Fraction(1181, 100) / Fraction(102, 100) * Fraction(94, 100)
        actual = c.economics(c.seller_revenue('11.81', '.02'), '10.4', '0', '.06')
        self.assertEqual(expected, Fraction(55507, 5100))
        self.assertLess(abs(Fraction(actual['net_receipt']) - expected), Fraction(1, 10**35))
        self.assertEqual(c.round_money(actual['profit']), D('.48'))

    def test_target_forty_cents(self):
        seller = c.seller_revenue('11.81', '.02')
        cap = c.max_cost(seller, '.4', '0', '.06')
        self.assertEqual(c.round_money(cap, 'down'), D('10.48'))
        self.assertGreater(c.economics(seller, '10.48', '0', '.06')['profit'], D('.4'))
        self.assertLess(c.economics(seller, '10.49', '0', '.06')['profit'], D('.4'))
        self.assertEqual(c.round_money(c.required_rate('1000', cap), 'up', '.0001'), D('95.3860'))

    def test_rounded_seller_changes_budget(self):
        s = c.round_money(c.seller_revenue('11.81', '.02'), 'down')
        self.assertEqual(s, D('11.57'))
        self.assertEqual(c.max_cost(s, '.4', '0', '.06'), D('10.4758'))
        self.assertEqual(c.round_money(c.max_cost(s, '.4', '0', '.06'), 'down'), D('10.47'))

    def test_fee_models_and_inverse(self):
        self.assertEqual(c.seller_revenue('11.81', '.02', 'deduct'), D('11.5738'))
        self.assertEqual(c.seller_revenue('11.81', '.02', 'seller'), D('11.81'))
        for model in ['markup', 'deduct', 'seller']:
            for buyer in ['0', '1', '11.81', '1000']:
                self.assertLess(abs(c.buyer_price(c.seller_revenue(buyer, '.02', model), '.02', model)-D(buyer)), D('1e-35'))

    def test_break_even_inverse(self):
        for cost in ['0', '10.3', '10.5', '100.01']:
            for target in ['0', '.1', '.2', '.3', '.4', '.5', '1']:
                s = c.min_seller(cost, target, '.05', '.06')
                self.assertLess(abs(c.economics(s, cost, '.05', '.06')['profit']-D(target)), D('1e-35'))

    def test_return_denominators(self):
        r = c.economics('10', '8', '0', '.06')
        self.assertEqual(r['profit'], D('1.4'))
        self.assertEqual(r['margin'], D('.14'))
        self.assertEqual(r['return_on_cost'], D('.175'))

    def test_negative_and_zero_denominators(self):
        r = c.economics('0', '0', '.1', '0')
        self.assertEqual(r['profit'], D('-.1'))
        self.assertIsNone(r['margin'])
        self.assertIsNone(r['return_on_cost'])
        self.assertLess(c.economics('10', '10', '0', '.06')['profit'], 0)
        self.assertLess(c.max_cost('0', '.4', '0', '.06'), 0)
        self.assertIsNone(c.required_rate('1000', '-.4'))

    def test_unknown_is_not_zero(self):
        self.assertIsNone(c.economics('11', 'UNKNOWN', '0', '.06')['profit'])
        self.assertIsNone(c.seller_revenue('11', 'UNKNOWN'))
        self.assertEqual(c.all_in_cost('10', 'USDT', 'UNKNOWN', '1'), (None, None))
        self.assertIsNone(c.max_cost('11', '.4', 'UNKNOWN', '.06'))

    def test_float_and_nonfinite_rejected(self):
        for v in [0.1, True, 'NaN', 'Infinity', '-Infinity']:
            with self.subTest(v=v), self.assertRaises(ValueError):
                c.dec(v)

    def test_invalid_ranges(self):
        for v in ['-0.1', '1', '2', '6']:
            with self.subTest(v=v), self.assertRaises(ValueError):
                c.rate(v)
        for v in ['0', '-1']:
            with self.assertRaises(ValueError):
                c.effective_rate('1000', v)
        with self.assertRaises(ValueError):
            c.economics('-1', '1', '0', '.06')

    def test_rounding_ties_and_arbitrary_step(self):
        self.assertEqual(c.round_money('1.005'), D('1.01'))
        self.assertEqual(c.round_money('-1.005'), D('-1.01'))
        self.assertEqual(c.round_money('10.48372549', 'down'), D('10.48'))
        self.assertEqual(c.round_money('11.821', 'up'), D('11.83'))
        self.assertEqual(c.round_money('1.021', 'up', '.05'), D('1.05'))

    def test_no_double_discount_on_final_quote(self):
        cost, usdt = c.all_in_cost('9', 'USD_WALLET', '1', '1')
        self.assertEqual((cost, usdt), (D('9'), D('9')))

    def test_wallet_fx_and_usdt_acquisition_are_separate(self):
        cost, usdt = c.all_in_cost('10', 'USD_WALLET', '1.02', '.98')
        self.assertEqual(usdt, D('10')/D('.98'))
        self.assertEqual(cost, usdt*D('1.02'))

    def test_funding_fee_and_network_allocation(self):
        cost, units = c.all_in_cost('9.9', 'USDT', '1', '1', funding_fee='.01',
                                   network_usdt='1', batch_orders='20')
        self.assertEqual(cost, D('10.05'))
        self.assertEqual(units, cost)
        added, _ = c.all_in_cost('10', 'USDT', '1', '1', funding_fee='.01', funding_mode='added')
        self.assertEqual(added, D('10.10'))

    def test_subscription_allocation(self):
        cost, units = c.all_in_cost('10.4', 'USDT', '1', '1', monthly_usd='29.99', monthly_orders='300')
        self.assertEqual(units, D('10.4'))
        self.assertEqual(cost, D('10.4')+D('29.99')/300)
        with self.assertRaises(ValueError):
            c.all_in_cost('1', 'USDT', '1', '1', monthly_usd='29.99', monthly_orders='0')

    def test_withdrawal_minimum_applies_to_batch(self):
        self.assertEqual(c.withdrawal('10', '.06', '1.5'), D('8.5'))
        self.assertEqual(c.withdrawal('100', '.06', '1.5'), D('94'))
        self.assertEqual(c.withdrawal('25', '.06', '1.5'), D('23.5'))
        with self.assertRaises(ValueError):
            c.withdrawal('1', '.06', '1.5')

    def test_withdrawal_surcharge_basis(self):
        self.assertEqual(c.withdrawal('106', '.06', mode='surcharge'), D('100'))
        self.assertEqual(c.withdrawal('10', '.06', minimum='1.5', mode='surcharge'), D('8.5'))

    def test_risk_accounting(self):
        # 98 successes -> 980 receipts; 100 costs -> 900; 2 refunded supply ->18.
        expected = D('98')*10 - 100*9 + 2*9
        self.assertEqual(c.expected_profit('10','9','0','.02','1')*100, expected)
        self.assertEqual(c.expected_profit('10','9','0','.02','0','.01'), D('.71'))

    def test_complete_failure_and_complete_recovery(self):
        self.assertEqual(c.expected_profit('10','9','0','1','0'), D('-9'))
        self.assertEqual(c.expected_profit('10','9','.05','1','1'), D('-.05'))
        with self.assertRaises(ValueError):
            c.expected_profit('10','9','0','1.01','0')

    def test_capital_does_not_expense_deposit(self):
        self.assertEqual(c.capital('20','10.4','2','1',safety_days='1'), D('832'))
        self.assertEqual(c.capital('1','10','2','1','100'), D('120'))
        self.assertEqual(c.capital('0','10','2','1'), 0)

    def test_large_denomination_not_linear_when_fixed_cost(self):
        small = c.economics('10','9','.1','.06')['profit']
        big = c.economics('50','45','.1','.06')['profit']
        self.assertNotEqual(big, small*5)

    def test_cli_defaults_unknown(self):
        result = subprocess.run([sys.executable,str(HERE/'calculator.py')],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['profit'], 'UNKNOWN')

    def test_cli_rate_and_nominal(self):
        args = ['--buyer','1000','--buyer-currency','RUB','--sale-rub-per-usd','100',
                '--category-fee','0','--supplier-rub-per-usdt','100','--rub','500',
                '--usd-per-usdt','1','--funding-fee','0','--network-usdt','0',
                '--monthly-usd','0','--order-fee','0','--order-fixed','0','--other','0']
        result = subprocess.run([sys.executable,str(HERE/'calculator.py'),*args],capture_output=True,text=True,check=True)
        data = json.loads(result.stdout)
        self.assertEqual(D(data['landed_cost_usd']), D('5'))
        self.assertEqual(D(data['seller_usd']), D('10'))
        self.assertEqual(D(data['profit']), D('4.4'))

    def test_cli_missing_sale_fx_and_ambiguous_cost(self):
        result = subprocess.run([sys.executable,str(HERE/'calculator.py'),'--buyer','1000',
                                 '--buyer-currency','RUB','--category-fee','0'],
                                 capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['seller_usd'], 'UNKNOWN')
        failed = subprocess.run([sys.executable,str(HERE/'calculator.py'),
                    '--cost','10','--supplier-rub-per-usdt','100'],capture_output=True,text=True)
        self.assertNotEqual(failed.returncode, 0)


class PublishedDataTests(unittest.TestCase):
    def test_matrix_against_fraction_oracle(self):
        path = HERE/'STEAM_PROFIT_MATRIX.csv'
        if not path.exists():
            self.fail('Generate reports first: python generate.py')
        with path.open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            if row['status'] == 'ACTUAL_UNKNOWN':
                self.assertEqual(row['profit_usd'], 'UNKNOWN')
                continue
            s = Fraction(row['seller_usd'])
            cost = Fraction(row['all_in_cost_usd'])
            other = Fraction(row['other_usd'])
            oracle = s*(1-Fraction(row['withdrawal_rate']))-cost-other
            self.assertLess(abs(Fraction(row['profit_usd'])-oracle),Fraction(1,10**9))


if __name__ == '__main__':
    unittest.main()
