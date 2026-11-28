"""Offline: the real NEW-form profile (no patched proof boundaries).

A realistic synthetic FunPay editor goes through the production
_fresh_contract/_require_live_contract path: one POST of the form's own
controls plus the draft's business fields, inactive + manual, confirmed only
by the independent inventory read-back. No network: requests.* are mocked.
"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from test_telegram_products import context
from test_publication_preview import saved, no_network
from test_own_offer_readback import inventory
from funpayflow import create_wire_contract as wire
from funpayflow.lot_publisher import PublisherError, TEXT_FIELDS

SELECTS = [('platform', 'Платформа', 'pc', 'PC'), ('region', 'Регион', 'eu', 'Европа'),
           ('edition', 'Издание', 'base', 'Elden Ring')]


def live_html(*, currency_marker='$', offer_id='0', node_id='986', csrf='LIVE-CSRF-CANARY',
              user_id=42, extra='', selects=SELECTS, price_block=None):
    texts = ''.join(
        f'<div class="form-group"><label>{key}</label>'
        f'<{"input" if field == "summary" else "textarea"} name="fields[{field}][{lang}]"'
        + (' value="">' if field == 'summary' else '></textarea>') + '</div>'
        for field in ('summary', 'desc', 'payment_msg') for lang, key in (('ru', 'RU'), ('en', 'EN')))
    price = price_block if price_block is not None else (
        '<div class="form-group has-feedback"><label for="price-input">Цена</label>'
        '<input id="price-input" class="form-control" name="price" value="">'
        + (f'<span class="form-control-feedback">{currency_marker}</span>' if currency_marker else '')
        + '</div>')
    return ('<html><head></head>'
        f'<body data-app-data=\'{{"userId":{user_id}}}\'>'
        '<form class="form-offer-editor" action="/lots/offerSave" method="post">'
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        f'<input type="hidden" name="offer_id" value="{offer_id}">'
        f'<input type="hidden" name="node_id" value="{node_id}">'
        '<input type="hidden" name="form_created_at" value="1700000000">'
        '<input type="hidden" name="location" value="offer">'
        + ''.join(f'<div class="form-group"><label for="{n}">{label}</label>'
                  f'<select id="{n}" name="fields[{n}]" required>'
                  f'<option value="{v}" selected>{t}</option></select></div>'
                  for n, label, v, t in selects)
        + texts + price
        + '<input name="amount" value="1">'
        '<textarea name="secrets"></textarea>'
        '<input type="checkbox" name="auto_delivery" value="on">'
        '<input type="checkbox" name="active" value="on" checked>'
        '<input type="checkbox" name="deactivate_after_sale" value="on">'
        + extra +
        '<button type="submit" class="btn">Сохранить</button></form></body></html>')


@pytest.fixture
def live(inventory, monkeypatch):
    c = inventory
    c.html = live_html()
    c.posted = []
    c.body = b'{"done":true}'
    c.create = True

    def post(url, **kwargs):
        assert c.client._account_lock._is_owned()
        assert url == 'https://funpay.com/lots/offerSave'
        assert kwargs['allow_redirects'] is False and kwargs['verify'] is True
        assert c.publisher.outcome(c.draft['id']) == {
            'state': 'UNKNOWN', 'reason': 'CLAIMED', 'remote_id': None}
        c.posted.append(kwargs['data'])
        if c.create:
            c.remote[72] = {}
        return SimpleNamespace(status_code=200, history=[], url=url,
                               iter_content=lambda size: [c.body], close=Mock())
    c.post = Mock(side_effect=post)
    monkeypatch.setattr(requests, 'post', c.post)
    return c


def test_real_profile_one_post_inactive_manual_confirmed_by_readback(live):
    c = live
    c.publisher.stage_new(c.review)
    assert c.publisher.confirm_new(c.review) == {
        'state': 'CONFIRMED', 'reason': 'CONFIRMED', 'remote_id': 72}
    assert c.post.call_count == 1
    data = c.posted[0]
    assert len(dict(data)) == len(data)
    data = dict(data)
    # The form's own controls pass through unchanged...
    assert data['csrf_token'] == 'LIVE-CSRF-CANARY' and data['form_created_at'] == '1700000000'
    assert data['offer_id'] == '0' and data['node_id'] == '986'
    assert data['fields[payment_msg][ru]'] == '' and data['location'] == 'trade'
    # ...the draft fills only business fields...
    for name, text in c.review.content:
        assert data[TEXT_FIELDS[name]] == text
    for name, value, *_ in c.review.selections:
        assert data[name] == value
    assert data['price'] == c.review.price and data['amount'] == '1'
    # ...and the lot is created INACTIVE with MANUAL delivery; currency not sent.
    for never in ('active', 'auto_delivery', 'deactivate_after_sale', 'secrets', 'currency'):
        assert never not in data
    with pytest.raises(PublisherError):
        c.publisher.confirm_new(c.review)
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'):
        c.publisher.stage_new(c.review)
    assert c.post.call_count == 1


def test_funpay_error_is_unknown_with_owner_readable_text_and_no_retry(live):
    c = live
    c.create = False
    c.body = json.dumps({'error': 1, 'msg': '<b>Укажите</b> цену   лота'}).encode()
    c.publisher.stage_new(c.review)
    assert c.publisher.confirm_new(c.review)['state'] == 'UNKNOWN'
    assert c.publisher.last_server_error == 'Укажите цену лота'
    assert c.post.call_count == 1


def test_envelope_from_another_transport_or_stale_form_never_passes(live):
    c = live
    first, second = wire.NewOfferTransport(), wire.NewOfferTransport()
    with c.client._account_lock:
        _, _, envelope = first._fresh_contract(c.publisher, c.review)
    with pytest.raises(PublisherError, match='CONTRACT_UNVERIFIED'):
        second._require_live_contract(envelope)
    first._require_live_contract(envelope)
    c.now += wire.FORM_TTL + 1
    with pytest.raises(PublisherError, match='FORM_STALE'):
        first._require_live_contract(envelope)
    c.post.assert_not_called()


def read(html, currency='USD'):
    return wire.read_live_form(html, fingerprint=FINGERPRINT[0], account_id=42, node_id=986,
                               selections=tuple((f'fields[{n}]', v) for n, _, v, _ in SELECTS),
                               currency=currency)


FINGERPRINT = []


@pytest.fixture
def fingerprint(saved):
    FINGERPRINT[:] = [saved.draft['node_schema']['fingerprint']]
    yield


@pytest.mark.parametrize('html,reason', [
    (live_html(currency_marker=''), 'PRICE'),
    (live_html(currency_marker='₽'), 'PRICE'),
    # Rejected already by the NodeForm parser (EDIT form, other node,
    # currency selector, duplicate identity) -> structure not recognized.
    (live_html(extra='<select name="currency"><option value="USD" selected>USD'
                     '</option></select>'), 'CONTRACT_UNVERIFIED'),
    (live_html(offer_id='123'), 'CONTRACT_UNVERIFIED'),
    (live_html(node_id='985'), 'CONTRACT_UNVERIFIED'),
    (live_html(extra='<input type="hidden" name="offer_id" value="0">'), 'CONTRACT_UNVERIFIED'),
    (live_html(csrf=''), 'CSRF'),
    (live_html(user_id=43), 'SCHEMA'),
    (live_html(extra='<input type="file" name="image">'), 'SCHEMA'),
    (live_html(extra='<input name="bad name">'), 'SCHEMA'),
    # A visible field the draft does not fill (other nodes may ask e.g. a server)
    # must block before any POST instead of being sent empty.
    (live_html(extra='<input name="fields[server]" value="">'), 'SCHEMA'),
    (live_html(extra='<textarea name="fields[activation]"></textarea>'), 'SCHEMA'),
    (live_html(extra='<input type="hidden" name="fields[server]" value="">'), 'SCHEMA'),
    (live_html(selects=SELECTS[:2]), 'SCHEMA'),
    (live_html().replace('method="post"', 'method="get"'), 'CONTRACT_UNVERIFIED'),
    (live_html().replace('name="amount" value="1"', 'name="amount_x" value="1"'), 'SCHEMA'),
])
def test_form_rejections(fingerprint, html, reason):
    with pytest.raises(PublisherError) as raised:
        read(html)
    assert raised.value.reason == reason


@pytest.mark.parametrize('marker', ['$', 'USD'])
def test_currency_marker_and_matching_selector_are_accepted(fingerprint, marker):
    schema, service = read(live_html(currency_marker=marker))
    assert schema['fingerprint'] == FINGERPRINT[0]
    names = dict(service)
    assert names['location'] == 'trade' and 'currency' not in names
    assert names['csrf_token'] == 'LIVE-CSRF-CANARY' and names['form_created_at'] == '1700000000'
    for never in ('active', 'auto_delivery', 'deactivate_after_sale', 'secrets'):
        assert never not in names


def test_offer_id_blank_also_means_new(fingerprint):
    _, service = read(live_html(offer_id=''))
    assert dict(service)['offer_id'] == ''
