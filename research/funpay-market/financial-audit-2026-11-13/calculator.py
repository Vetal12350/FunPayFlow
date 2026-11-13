"""Offline USD unit economics. Decimal strings only; None means UNKNOWN.

No network, credentials, production imports or purchases. See README.md for units.
"""
import argparse
import json
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, getcontext

getcontext().prec = 40
ZERO = Decimal('0')
ONE = Decimal('1')


def dec(value):
    if value is None or value == 'UNKNOWN':
        return None
    if isinstance(value, (float, bool)):
        raise ValueError('Use decimal strings, never float/bool')
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError('Finite numbers required')
    return result


def nonnegative(value):
    value = dec(value)
    if value is None or value < ZERO:
        raise ValueError('Known nonnegative value required')
    return value


def positive(value):
    value = nonnegative(value)
    if value == ZERO:
        raise ValueError('Positive value required')
    return value


def rate(value):
    value = nonnegative(value)
    if value >= ONE:
        raise ValueError('Rate must be in [0,1), not a whole percentage')
    return value


def probability(value):
    value = nonnegative(value)
    if value > ONE:
        raise ValueError('Probability must be in [0,1]')
    return value


def round_money(value, mode='nearest', step='0.01'):
    value = dec(value)
    if value is None:
        return None
    rounding = {'nearest': ROUND_HALF_UP, 'down': ROUND_FLOOR,
                'up': ROUND_CEILING}[mode]
    step = positive(step)
    return (value / step).to_integral_value(rounding=rounding) * step


def seller_revenue(buyer, fee, model='markup'):
    """buyer excludes payment-method surcharge; fee basis is explicit."""
    if dec(buyer) is None or dec(fee) is None:
        return None
    buyer, fee = nonnegative(buyer), rate(fee)
    if model == 'markup':
        return buyer / (ONE + fee)
    if model == 'deduct':
        return buyer * (ONE - fee)
    if model == 'seller':
        return buyer  # caller supplied seller amount, no second category charge
    raise ValueError('Unknown fee model')


def buyer_price(seller, fee, model='markup'):
    if dec(seller) is None or dec(fee) is None:
        return None
    seller, fee = nonnegative(seller), rate(fee)
    if model == 'markup':
        return seller * (ONE + fee)
    if model == 'deduct':
        return seller / (ONE - fee)
    if model == 'seller':
        return seller
    raise ValueError('Unknown fee model')


def withdrawal(receipt, fee, minimum='0', fixed='0', mode='deducted'):
    """Apply to a WHOLE withdrawal batch. Minimum is max(), never added twice.

    deducted: net=receipt-max(receipt*fee,minimum)-fixed
    surcharge: receipt=net+max(net*fee,minimum)+fixed
    """
    if dec(receipt) is None or dec(fee) is None:
        return None
    receipt, fee = nonnegative(receipt), rate(fee)
    minimum, fixed = nonnegative(minimum), nonnegative(fixed)
    if mode == 'deducted':
        net = receipt - max(receipt * fee, minimum) - fixed
    elif mode == 'surcharge':
        available = receipt - fixed
        net = min(available / (ONE + fee), available - minimum)
    else:
        raise ValueError('Unknown withdrawal mode')
    if net < ZERO:
        raise ValueError('Withdrawal batch is too small for fees')
    return net


def all_in_cost(quote, quote_currency, usd_per_usdt, wallet_usd_per_usdt,
                order_fee='0', order_fixed='0', funding_fee='0',
                funding_mode='deducted', network_usdt='0', batch_orders='1',
                monthly_usd='0', monthly_orders='1'):
    """Returns USD cost and gross USDT funding per order.

    Defaults of zero are SCENARIO assumptions, not confirmed absent expenses.
    quote: USDT debit or USD wallet debit. Fees already in quote MUST stay zero.
    monthly_usd is allocated once, never also in OtherCosts.
    """
    required = [quote, usd_per_usdt, wallet_usd_per_usdt, order_fee,
                order_fixed, funding_fee, network_usdt, batch_orders,
                monthly_usd, monthly_orders]
    if any(dec(v) is None for v in required):
        return None, None
    q = nonnegative(quote) * (ONE + rate(order_fee)) + nonnegative(order_fixed)
    if quote_currency == 'USDT':
        units = q
    elif quote_currency == 'USD_WALLET':
        units = q / positive(wallet_usd_per_usdt)
    else:
        raise ValueError('quote_currency must be USDT or USD_WALLET')
    funding_fee = rate(funding_fee)
    if funding_mode == 'deducted':
        units /= ONE - funding_fee
    elif funding_mode == 'added':
        units *= ONE + funding_fee
    else:
        raise ValueError('Unknown funding mode')
    units += nonnegative(network_usdt) / positive(batch_orders)
    cost = (units * positive(usd_per_usdt)
            + nonnegative(monthly_usd) / positive(monthly_orders))
    return cost, units


def economics(seller, cost, other, withdrawal_rate):
    if any(dec(v) is None for v in [seller, cost, other, withdrawal_rate]):
        return {k: None for k in ('net_receipt', 'profit', 'margin', 'return_on_cost')}
    seller, cost, other = map(nonnegative, (seller, cost, other))
    net = seller * (ONE - rate(withdrawal_rate))
    profit = net - cost - other
    return {'net_receipt': net, 'profit': profit,
            'margin': profit / seller if seller else None,
            'return_on_cost': profit / cost if cost else None}


def max_cost(seller, target, other, withdrawal_rate):
    if any(dec(v) is None for v in [seller, target, other, withdrawal_rate]):
        return None
    return (nonnegative(seller) * (ONE - rate(withdrawal_rate))
            - nonnegative(target) - nonnegative(other))


def min_seller(cost, target, other, withdrawal_rate):
    if any(dec(v) is None for v in [cost, target, other, withdrawal_rate]):
        return None
    return ((nonnegative(cost) + nonnegative(target) + nonnegative(other))
            / (ONE - rate(withdrawal_rate)))


def effective_rate(credited_rub, usdt_debit):
    if dec(usdt_debit) is None:
        return None
    return positive(credited_rub) / positive(usdt_debit)


def required_rate(credited_rub, cost_budget_usd, usd_per_usdt='1'):
    if any(dec(v) is None for v in [cost_budget_usd, usd_per_usdt]):
        return None
    if dec(cost_budget_usd) <= ZERO:
        return None  # no feasible positive purchase budget
    return positive(credited_rub) * positive(usd_per_usdt) / dec(cost_budget_usd)


def expected_profit(net_receipt, cost, other, failed='0', recovered='0', duplicate='0'):
    """Per attempted order: failures fully refund customer, recovery is supplier share.

    Duplicate is independent unrecoverable extra supplier debit probability.
    Does not add 'lost profit' again. All rates are scenarios unless measured.
    """
    net_receipt, cost, other = map(nonnegative, (net_receipt, cost, other))
    failed, duplicate = probability(failed), probability(duplicate)
    recovered = probability(recovered)
    return ((ONE - failed) * net_receipt - cost
            + failed * recovered * cost - duplicate * cost - other)


def capital(daily_orders, cost_usdt, cash_days, deposit_days, minimum_deposit='0',
            safety_days='0', held_refunds='0', upfront_fixed='0'):
    """Cost-replacement bridge + supplier reserve, NOT revenue + cost twice.

    Cash days include customer release, withdrawal, batching and transfer delay.
    Minimum deposit is a reserve floor; it is not also expensed.
    """
    daily = nonnegative(daily_orders) * nonnegative(cost_usdt)
    bridge = daily * nonnegative(cash_days)
    reserve = max(daily * (nonnegative(deposit_days) + nonnegative(safety_days)),
                  nonnegative(minimum_deposit))
    return bridge + reserve + nonnegative(held_refunds) + nonnegative(upfront_fixed)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--buyer', default='UNKNOWN', help='USD price excluding payment surcharge')
    p.add_argument('--buyer-currency', choices=['USD', 'RUB'], default='USD')
    p.add_argument('--sale-rub-per-usd', default='UNKNOWN', help='executable sale/withdrawal conversion, not CBR by default')
    p.add_argument('--category-fee', default='UNKNOWN', help='fraction, e.g. 0.02')
    p.add_argument('--fee-model', choices=['markup', 'deduct', 'seller'], default='markup')
    p.add_argument('--withdrawal', default='0.06', help='owner assumption; deducted from seller')
    p.add_argument('--cost', default='UNKNOWN', help='supplier debit, not automatically landed')
    p.add_argument('--supplier-rub-per-usdt', default='UNKNOWN', help='alternative to --cost: exact credited RUB per debited USDT')
    p.add_argument('--quote-currency', choices=['USDT', 'USD_WALLET'], default='USDT')
    p.add_argument('--usd-per-usdt', default='UNKNOWN', help='actual acquisition cost, not spot by default')
    p.add_argument('--wallet-usd-per-usdt', default='1', help='explicit scenario default, verify provider')
    p.add_argument('--funding-fee', default='UNKNOWN')
    p.add_argument('--funding-mode', choices=['added', 'deducted'], default='deducted')
    p.add_argument('--network-usdt', default='UNKNOWN', help='whole funding batch fee')
    p.add_argument('--batch-orders', default='1')
    p.add_argument('--monthly-usd', default='UNKNOWN')
    p.add_argument('--monthly-orders', default='1')
    p.add_argument('--order-fee', default='UNKNOWN')
    p.add_argument('--order-fixed', default='UNKNOWN')
    p.add_argument('--other', default='UNKNOWN')
    p.add_argument('--target', default='0.40')
    p.add_argument('--rub', default='1000', help='exact credited RUB, used only for effective rate')
    a = p.parse_args()
    try:
        buyer = dec(a.buyer)
        if a.buyer_currency == 'RUB':
            buyer = (None if buyer is None or dec(a.sale_rub_per_usd) is None
                     else buyer / positive(a.sale_rub_per_usd))
        quote = a.cost
        if dec(a.supplier_rub_per_usdt) is not None:
            if dec(a.cost) is not None or a.quote_currency != 'USDT':
                raise ValueError('Supplier RUB/USDT requires USDT quote currency and no --cost')
            quote = positive(a.rub) / positive(a.supplier_rub_per_usdt)
        seller = seller_revenue(buyer, a.category_fee, a.fee_model)
        cost, units = all_in_cost(quote, a.quote_currency, a.usd_per_usdt,
            a.wallet_usd_per_usdt, a.order_fee, a.order_fixed, a.funding_fee,
            a.funding_mode, a.network_usdt, a.batch_orders, a.monthly_usd, a.monthly_orders)
        result = economics(seller, cost, a.other, a.withdrawal)
        cap = max_cost(seller, a.target, a.other, a.withdrawal)
        minimum = min_seller(cost, a.target, a.other, a.withdrawal)
        result.update(seller_usd=seller, landed_cost_usd=cost, funding_usdt=units,
            effective_rub_per_funded_usdt=effective_rate(a.rub, units),
            max_all_in_cost_usd=cap,
            minimum_seller_usd=minimum,
            minimum_buyer_usd=buyer_price(minimum, a.category_fee, a.fee_model),
            status='SCENARIO_NOT_VERIFIED_QUOTE')
    except (ValueError, ArithmeticError) as exc:
        p.error(str(exc))
    print(json.dumps({k: 'UNKNOWN' if v is None else str(v) for k, v in result.items()}, indent=2))


if __name__ == '__main__':
    main()
