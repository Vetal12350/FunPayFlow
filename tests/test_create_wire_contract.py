"""Complete synthetic form facts only; existing fake publisher supplies the seam."""
from contextlib import closing
from dataclasses import FrozenInstanceError, replace

import pytest

from test_telegram_products import context
from test_lot_publisher import case, confirm, no_external_calls, restarted
from funpayflow.create_wire_contract import NewFormFacts, assemble_new_request
from funpayflow.lot_publisher import PublisherError, SubmitReceipt, TEXT_FIELDS
from funpayflow.node_form import is_safe_node_binding_name


@pytest.fixture
def form(case):
    business = dict(case.prepared.business)
    controls = dict(business, offer_id='', node_id='986', csrf_token=case.controls.csrf,
                    auto_delivery='SYNTHETIC_MANUAL', location='trade', active='',
                    deactivate_after_sale='')
    controls.update({k: '' for k in (*TEXT_FIELDS.values(), 'price')})
    return NewFormFacts('synthetic-new-v1', 'https://funpay.com/lots/offerEdit?node=986',
        'https://funpay.com/lots/offerSave', 'POST', 'application/x-www-form-urlencoded',
        case.prepared.fetched_at, 986, case.prepared.fingerprint, 'USD',
        'SYNTHETIC_MANUAL', case.controls.csrf, tuple(controls.items()),
        tuple((k, v) for k, v in business.items() if is_safe_node_binding_name(k)))


def test_valid_envelope_is_pure_immutable_and_usable_only_in_fake_seam(case, form):
    envelope = assemble_new_request(case.prepared, form, now=case.now)
    assert envelope.business == case.prepared.business
    assert dict(envelope.service) == dict(offer_id='', node_id='986', csrf_token=form.csrf,
        auto_delivery='SYNTHETIC_MANUAL', location='trade', active='', deactivate_after_sale='')
    assert (envelope.path, envelope.method, envelope.encoding, envelope.allow_redirects,
            envelope.max_attempts) == ('lots/offerSave', 'POST',
                                      'application/x-www-form-urlencoded', False, 1)
    with pytest.raises(FrozenInstanceError):
        envelope.max_attempts = 2
    with pytest.raises(TypeError):
        replace(envelope, allow_redirects=True)
    def fake_submit(business, controls, **policy):
        assert assemble_new_request(case.prepared, form, now=case.now) == envelope
        return case.submit(business, controls, **policy)
    case.send.side_effect = fake_submit
    assert confirm(case)['state'] == 'CONFIRMED'
    assert case.send.call_count == 1


@pytest.mark.parametrize('change', [
    {'provenance': 'live'}, {'url': 'https://funpay.com/lots/offerEdit?offer=71'},
    {'url': 'https://funpay.com/lots/offerEdit?node=986&offer='}, {'node_id': 976},
    {'action': 'https://other.invalid/lots/offerSave'}, {'method': 'GET'},
    {'encoding': 'application/json'}, {'fetched_at': 0}, {'fetched_at': float('nan')},
    {'fingerprint': 'different'}, {'currency': 'EUR'}, {'manual_mode': ''}, {'csrf': ''},
])
def test_rejects_unverified_scope_freshness_and_policy(case, form, change):
    with pytest.raises(PublisherError):
        assemble_new_request(case.prepared, replace(form, **change), now=case.now)
    case.send.assert_not_called()
    assert case.publisher.outcome(case.draft['id']) is None


def test_rejects_missing_duplicate_unknown_edit_and_mismatched_controls(case, form):
    controls = dict(form.controls)
    bad = [form.controls[1:], form.controls + (form.controls[0],),
           form.controls + (('unknown', 'PRIVATE-CANARY'),)]
    for key, value in [('offer_id', '71'), ('offer_id', '0'), ('node_id', '976'),
                       ('csrf_token', 'PRIVATE-CANARY'), ('auto_delivery', ''),
                       ('currency', 'EUR'), ('amount', '2'), ('location', '')]:
        bad.append(tuple(dict(controls, **{key: value}).items()))
    for rows in bad:
        with pytest.raises(PublisherError) as error:
            assemble_new_request(case.prepared, replace(form, controls=rows), now=case.now)
        assert 'PRIVATE-CANARY' not in str(error.value)
    for options in ((), form.selected_options + (form.selected_options[0],),
                    ((form.selected_options[0][0], 'unknown'),)):
        with pytest.raises(PublisherError):
            assemble_new_request(case.prepared, replace(form, selected_options=options), now=case.now)
    case.send.assert_not_called()


@pytest.mark.parametrize('response', [{'success': True}, SubmitReceipt(None),
                                    SubmitReceipt(72, redirects=1), SubmitReceipt(72, attempts=2)])
def test_raw_success_missing_id_redirect_or_replay_remains_unknown(case, form, response):
    def fake_submit(*args, **kwargs):
        assemble_new_request(case.prepared, form, now=case.now)
        return response
    case.send.side_effect = fake_submit
    assert confirm(case)['state'] == 'UNKNOWN'
    case.publisher = restarted(case)
    with pytest.raises(PublisherError, match='ALREADY_CLAIMED'):
        confirm(case)
    assert case.send.call_count == 1


def test_transient_secret_never_enters_repr_logs_or_publisher_persistence(case, form, caplog, capsys):
    envelope = assemble_new_request(case.prepared, form, now=case.now)
    case.send.side_effect = RuntimeError(form.csrf)
    result = confirm(case)
    with closing(case.receipt._connect()) as db:
        persisted = '\n'.join(db.iterdump())
    captured = capsys.readouterr()
    assert form.csrf not in (repr(envelope) + repr(form) + repr(result) + persisted
                             + caplog.text + captured.out + captured.err)
