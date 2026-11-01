"""Transient NEW observations: synthetic HTML, fake Account, real temporary SQLite."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from test_publication_preview import (context, saved, no_network, new_html, preview, send, action)
from test_telegram_products import database_snapshot
from funpayflow import node_form, telegram_products as view
from funpayflow.lot_publisher import LotPublisher


URL = 'https://funpay.com/lots/offerEdit?node=986'
CANARY = 'SYNTHETIC-PRIVATE-CANARY'


def observe(html=None, url=URL):
    diagnostics = {'untrusted': CANARY}
    proof = node_form.prove_new_form_contract(html or new_html(), url, diagnostics=diagnostics)
    assert node_form.safe_new_form_diagnostics(diagnostics) == diagnostics
    assert proof['publish_ready'] is False
    assert all(proof[k] == 'unverified' for k in (
        'currency', 'manual_delivery', 'new_semantics', 'response_id', 'no_retry_post'))
    assert CANARY not in json.dumps((diagnostics, proof))
    return diagnostics


@pytest.mark.parametrize('price,currency,reason,count', [
    ('<label for="p">Цена, ₽</label><input id="p" name="price">', 'RUB', 'UNIQUE_ANCHORED_MARKER', 1),
    ('<label for="p">Price (USD)</label><input id="p" name="price">', 'USD', 'UNIQUE_ANCHORED_MARKER', 1),
    ('<label>Цена EUR<input name="price"></label>', 'EUR', 'UNIQUE_ANCHORED_MARKER', 1),
    ('<div class="input-group"><input name="price"><span class="input-group-addon">€</span></div>',
     'EUR', 'UNIQUE_ANCHORED_MARKER', 1),
    ('<input name="price">', 'UNKNOWN', 'NO_ANCHORED_MARKER', 0),
    ('<span>USD</span><input name="price">', 'UNKNOWN', 'NO_ANCHORED_MARKER', 0),
    ('<label for="other">Цена ₽</label><input id="p" name="price">', 'UNKNOWN', 'NO_ANCHORED_MARKER', 0),
    ('<label for="p" hidden>Цена ₽</label><input id="p" name="price">', 'UNKNOWN', 'NO_ANCHORED_MARKER', 0),
    ('<label for="p">Price USD USD</label><input id="p" name="price">', 'UNKNOWN', 'MULTIPLE_MARKERS', 2),
    ('<label for="p">Price $ / €</label><input id="p" name="price">', 'UNKNOWN', 'CONFLICTING_MARKERS', 2),
    ('<label for="p">USD</label><label for="p">EUR</label><input id="p" name="price">',
     'UNKNOWN', 'CONFLICTING_MARKERS', 2),
    ('<label for="p">USD</label><input id="p" name="price"><span id="p"></span>',
     'UNKNOWN', 'NO_ANCHORED_MARKER', 0),
    ('<label for="p">USD<input name="other"></label><input id="p" name="price">',
     'UNKNOWN', 'NO_ANCHORED_MARKER', 0),
    ('<div class="input-group"><input name="price"><input name="amount2">'
     '<span class="input-group-addon">€</span></div>', 'UNKNOWN', 'NO_ANCHORED_MARKER', 0),
    ('<input name="price" value="USD">', 'UNKNOWN', 'NO_ANCHORED_MARKER', 0),
    ('<input name="price" disabled>', 'UNKNOWN', 'PRICE_CONTROL_AMBIGUOUS', 0),
    ('<input name="price"><input name="price">', 'UNKNOWN', 'PRICE_CONTROL_AMBIGUOUS', 0),
    ('', 'UNKNOWN', 'PRICE_CONTROL_AMBIGUOUS', 0),
])
def test_price_markers_require_unique_narrow_anchor(price, currency, reason, count):
    d = observe(new_html().replace('<input name="price">', price))
    assert (d['price_currency'], d['price_reason'], d['marker_count']) == (currency, reason, count)


def selector(label='USD', *, selected=' selected'):
    return ('<div class="form-group"><select name="currency">'
            f'<option value="{CANARY}"{selected}>{label}</option>'
            '<option value="opaque">EUR</option></select></div>')


@pytest.mark.parametrize('extra,state,currency,relation', [
    ('', 'ABSENT', 'UNKNOWN', 'UNKNOWN'),
    (selector(), 'SELECT', 'USD', 'OBSERVED_MATCH'),
    (selector('RUB'), 'SELECT', 'RUB', 'OBSERVED_CONFLICT'),
    (selector(CANARY), 'SELECT', 'UNKNOWN', 'UNKNOWN'),
    (selector(selected=''), 'SELECT', 'UNKNOWN', 'UNKNOWN'),
    ('<input type="hidden" name="currency" value="USD">', 'OTHER_CONTROL', 'UNKNOWN', 'UNKNOWN'),
    (selector() + selector(), 'DUPLICATE_CONTROL', 'UNKNOWN', 'UNKNOWN'),
])
def test_selector_labels_only_and_account_never_supplies_default(extra, state, currency, relation):
    html = new_html().replace('<input name="price">', '<label for="p">USD</label><input id="p" name="price">')
    d = observe(html.replace('</form>', extra + '</form>'))
    assert (d['currency_selector'], d['selector_currency'], d['currency_relation']) == (state, currency, relation)
    assert d['account_currency'] == 'UNKNOWN'
    assert d['account_reason'] == 'PINNED_API_UNAVAILABLE'


def test_price_label_id_duplicate_outside_form_is_not_unique():
    html = new_html().replace('<input name="price">', '<label for="p">USD</label><input id="p" name="price">')
    html = html.replace('<form ', '<span id="p"></span><form ')
    assert observe(html)['price_currency'] == 'UNKNOWN'


@pytest.mark.parametrize('option_attrs,group_attrs', [(' disabled', ''), ('', ' disabled')])
def test_selector_disabled_option_or_optgroup_is_unknown(option_attrs, group_attrs):
    extra = (f'<div class="form-group"><select name="currency"><optgroup label="Currency"{group_attrs}>'
             f'<option selected{option_attrs}>USD</option><option>EUR</option></optgroup></select></div>')
    d = observe(new_html().replace('</form>', extra + '</form>'))
    assert d['selector_currency'] == 'UNKNOWN'
    assert d['selector_reason'] == 'UNRECOGNIZED_LABELS_OR_SELECTION'


@pytest.mark.parametrize('name', ['auto_delivery', 'active', 'secrets'])
@pytest.mark.parametrize('control,expected', [
    ('', 'ABSENT'),
    ('<input type="hidden" name="NAME" value="SYNTHETIC-PRIVATE-CANARY">', 'HIDDEN'),
    ('<input type="radio" name="NAME">', 'UNSUPPORTED_SHAPE'),
    ('<input type="checkbox" name="NAME" disabled>', 'HIDDEN_OR_DISABLED'),
    ('<input type="checkbox" name="NAME" hidden>', 'HIDDEN_OR_DISABLED'),
    ('<input type="checkbox" name="NAME"><input type="hidden" name="NAME">', 'DUPLICATE_CONTROL'),
    ('<input type="checkbox" name="NAME" formaction="/other">', 'OVERRIDE_OR_NESTED'),
])
def test_control_shapes_do_not_claim_delivery_or_visibility(name, control, expected):
    html = new_html().replace('<input type="checkbox" name="auto_delivery" value="on">', '')
    d = observe(html.replace('</form>', control.replace('NAME', name) + '</form>'))
    assert d[name] == expected


@pytest.mark.parametrize('name', ['auto_delivery', 'active'])
@pytest.mark.parametrize('checked,expected', [('', 'CHECKBOX_NO'), (' checked', 'CHECKBOX_YES')])
def test_checkbox_state_ignores_values(name, checked, expected):
    html = new_html().replace('<input type="checkbox" name="auto_delivery" value="on">', '')
    d = observe(html.replace('</form>', f'<input type="checkbox" name="{name}"{checked} value="{CANARY}"></form>'))
    assert d[name] == expected


@pytest.mark.parametrize('tag,expected', [('textarea', 'TEXTAREA'), ('input', 'INPUT')])
def test_secrets_only_allowlisted_name_and_shape(tag, expected):
    extra = f'<textarea name="secrets">{CANARY}</textarea>' if tag == 'textarea' else f'<input name="secrets" value="{CANARY}">'
    assert observe(new_html().replace('</form>', extra + '</form>'))['secrets'] == expected


@pytest.mark.parametrize('name', ['csrf_token', 'form_created_at', 'location', 'deleted'])
@pytest.mark.parametrize('shape,expected', [('hidden', 'PRESENT'), ('text', 'UNSUPPORTED_SHAPE'),
                                           ('absent', 'ABSENT'), ('duplicate', 'DUPLICATE_CONTROL')])
def test_recognized_hidden_presence_never_values(name, shape, expected):
    html = new_html().replace(f'<input type="hidden" name="{name}" value="{CANARY}">', '')
    extra = '' if shape == 'absent' else f'<input type="{"hidden" if shape == "duplicate" else shape}" name="{name}" value="{CANARY}">'
    if shape == 'duplicate':
        extra += extra
    d = observe(html.replace('</form>', extra + '</form>'))
    assert d[name + '_hidden'] == expected


@pytest.mark.parametrize('submit,expected,count', [
    ('', 'ABSENT', 0), ('<button>arbitrary remote text</button>', 'BUTTON_SUBMIT', 1),
    (f'<input type="submit" value="{CANARY}">', 'INPUT_SUBMIT', 1),
    ('<button type="button">Save</button>', 'ABSENT', 0),
    ('<button name="private">Save</button>', 'UNSUPPORTED_SHAPE', 1),
    ('<input type="image">', 'UNSUPPORTED_SHAPE', 1),
    ('<button formaction="/other">Save</button>', 'OVERRIDE_OR_NESTED', 1),
    ('<button disabled>Save</button>', 'HIDDEN_OR_DISABLED', 1),
    ('<button>Save</button><button>Save</button>', 'MULTIPLE_SUBMITS', 2),
])
def test_submit_shapes_are_observations_only(submit, expected, count):
    d = observe(new_html().replace('<button type="submit">Save</button>', submit))
    assert (d['submit'], d['submit_count']) == (expected, count)
    assert (d['action'], d['method']) == ('OFFER_SAVE_RELATIVE', 'POST')


@pytest.mark.parametrize('old,new,key,expected', [
    ('action="/lots/offerSave"', 'action="https://funpay.com/lots/offerSave"', 'action', 'OFFER_SAVE_HTTPS'),
    ('action="/lots/offerSave"', '', 'action', 'ABSENT'),
    ('method="post"', '', 'method', 'ABSENT'),
    ('method="post"', 'method="get"', 'method', 'GET'),
    ('method="post"', 'method="secret"', 'method', 'UNSUPPORTED_METHOD'),
])
def test_action_and_method_fixed_enums(old, new, key, expected):
    assert observe(new_html().replace(old, new))[key] == expected


@pytest.mark.parametrize('change', ['duplicate_attributes', 'script', 'outside_malformed', 'url'])
def test_unsafe_context_cannot_produce_positive_diagnostics(change):
    html, url = new_html(), URL
    if change == 'duplicate_attributes': html = html.replace('name="price"', 'name="price" name="private"')
    elif change == 'script': html = html.replace('</form>', f'<script>{CANARY}</script></form>')
    elif change == 'outside_malformed': html += '<div><span></div>'
    else: url += '&offer=71'
    d = observe(html, url)
    assert d['context'] in {'UNSAFE_STRUCTURE', 'RESPONSE_CONTEXT'}
    assert d['price_currency'] == d['active'] == d['csrf_token_hidden'] == 'UNKNOWN'


@pytest.mark.parametrize('change', [{'price_currency': CANARY}, {'marker_count': True},
                                   {'submit_count': 1002}, {'extra': CANARY}, {'context': []}])
def test_ui_boundary_rejects_non_allowlisted_data(change):
    d = dict(observe(), **change)
    assert node_form.safe_new_form_diagnostics(d) == node_form.safe_new_form_diagnostics()
    text = view._publication_diagnostics_text(d, CANARY)
    assert CANARY not in text


def test_owner_get_diagnostic_redacted_transient_and_single_use(saved, caplog):
    c = saved
    c.client.account.currency = CANARY  # Unsupported ad-hoc attribute must be ignored.
    c.html = c.html.replace('<input name="price">', '<label for="p">Цена ₽</label><input id="p" name="price">')
    c.html = c.html.replace('</form>', f'<textarea name="secrets">{CANARY}</textarea>'
        f'<input type="hidden" name="location" value="{CANARY}">'
        f'<input type="hidden" name="{CANARY}" value="{CANARY}"></form>')
    before = database_snapshot(c)

    async def run():
        await preview(c)
        request = action('inspect')
        cb = await send(request)
        text = cb.message.answer.call_args.args[0]
        assert 'Данные формы / диагностика' in text
        for expected in ('price=RUB', 'CHECKBOX_NO', 'secrets=TEXTAREA', 'active=ABSENT',
                         'account=UNKNOWN', 'OBSERVED_CONFLICT', 'location=PRESENT', 'POST OFF', 'скриншоты'):
            assert expected in text
        assert CANARY not in str(cb.message.answer.call_args_list) + str(cb.message.edit_text.call_args_list)
        assert 'inspect' not in str(cb.message.edit_text.call_args.kwargs['reply_markup'])
        replay = await send(request)
        assert 'Данные формы' not in str(replay.message.answer.call_args_list)
        assert c.get.call_count == 1
    asyncio.run(run())
    assert database_snapshot(c) == before
    assert CANARY not in caplog.text
    assert CANARY.encode() not in c.receipt.path.read_bytes()
    assert CANARY not in json.dumps(view._flows, default=str)


def test_late_failure_clears_observations(saved):
    c = saved
    publisher = LotPublisher(c.client, c.store, late_check=lambda: True)
    review = publisher.review(c.draft['id'], c.draft['revision'], 'USD')
    diagnostics = observe()
    c.get.side_effect = node_form.NodeFormError('REQUEST_TRANSPORT')
    with pytest.raises(node_form.NodeFormError):
        publisher.inspect_new(review, diagnostics=diagnostics)
    assert diagnostics == node_form.safe_new_form_diagnostics()


def test_account_replacement_during_get_withholds_diagnostic_even_from_inspector(saved):
    c = saved
    publisher = LotPublisher(c.client, c.store, late_check=lambda: True)
    review = publisher.review(c.draft['id'], c.draft['revision'], 'USD')
    diagnostics = observe()

    def get(account):
        c.client.account = SimpleNamespace(**vars(account))
        return c.html.encode()
    c.get.side_effect = get
    with pytest.raises(node_form.NodeFormError, match='Не удалось'):
        node_form.inspect_fresh_new_form(c.client, c.store, review.fingerprint,
            tuple((n, v) for n, v, _, _ in review.selections), diagnostics=diagnostics)
    assert diagnostics == node_form.safe_new_form_diagnostics()
    assert c.get.call_count == 1


def test_diagnostic_message_is_bounded_for_all_allowed_fields():
    d = {key: max(choices, key=len) for key, choices in node_form._DIAGNOSTIC_ENUMS.items()}
    d.update(dict.fromkeys(node_form._DIAGNOSTIC_COUNTS, 1001))
    assert len(view._publication_diagnostics_text(d, 'EUR').encode('utf-16-le')) // 2 < 2000


@pytest.mark.parametrize('old,new', [('"userId":42', '"userId":43'),
    ('name="offer_id" value=""', 'name="offer_id" value="71"'),
    ('name="node_id" value="986"', 'name="node_id" value="976"')])
def test_wrong_account_edit_and_node_never_emit_diagnostic(saved, old, new, caplog):
    c = saved
    c.html = c.html.replace(old, new)
    before = database_snapshot(c)

    async def run():
        await preview(c)
        intent = action('inspect')
        cb = await send(intent)
        assert 'Данные формы / диагностика' not in str(cb.message.answer.call_args_list)
        assert CANARY not in str(cb.message.answer.call_args_list) + caplog.text
        await send(intent)
        assert c.get.call_count == 1
    asyncio.run(run())
    assert database_snapshot(c) == before
