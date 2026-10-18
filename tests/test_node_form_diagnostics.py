"""Only synthetic responses; diagnostics must never contain response-derived text."""
import json

import pytest
from requests import Response, Request
from requests.exceptions import Timeout
from FunPayAPI.common.exceptions import RequestFailedError, UnauthorizedError

from funpayflow import node_form as module
from funpayflow.node_form import NodeFormError, parse_node_form
from test_node_form import service, form

PRIVATE = 'SYNTHETIC_PRIVATE_RESPONSE_VALUE'


@pytest.fixture
def logs(monkeypatch):
    lines = []
    monkeypatch.setattr(module.logger, 'warning', lines.append)
    return lines


def assert_failure(service, logs, reason):
    with pytest.raises(NodeFormError) as caught:
        service.get(976, force=True)
    error = caught.value
    assert error.reason == reason
    assert str(error) == 'Не удалось получить актуальные параметры раздела FunPay.'
    assert error.__suppress_context__
    assert len(logs) == 1
    diagnostic = json.loads(logs[0].removeprefix('NodeForm failure '))
    assert diagnostic == error.diagnostic
    assert PRIVATE not in logs[0] + str(error) + repr(error) + repr(error.__dict__)
    assert not service.client._node_form_cache
    return error


@pytest.mark.parametrize('html,reason', [
    ('', 'PARSER_INPUT'),
    ('<body data-app-data="broken">'+form()+'</body>', 'APP_DATA_JSON'),
    ("<body data-app-data='[]'>"+form()+'</body>', 'APP_DATA_SHAPE'),
    ("<body data-app-data='"+json.dumps({'userId':'10'})+"'>"+form()+'</body>', 'APP_USER_TYPE'),
    ("<body data-app-data='"+json.dumps({'userId':True})+"'>"+form()+'</body>', 'APP_USER_TYPE'),
    ("<body data-app-data='"+json.dumps({'userId':0})+"'>"+form()+'</body>', 'APP_USER_INVALID'),
    ("<body data-app-data='"+json.dumps({'userId':11})+"'>"+form()+'</body>', 'APP_USER_SCOPE'),
    ('<input type="password">'+form(), 'LOGIN_PAGE'),
    ('<p class="lead">'+PRIVATE+'</p>'+form(), 'ERROR_PAGE'),
    ('<body>'+PRIVATE+'</body>', 'FORM_COUNT'),
    (form()+form(), 'FORM_COUNT'),
    (form().replace('form-offer-editor', 'form-offer-editor hidden'), 'FORM_HIDDEN'),
    (form().replace('<form ', '<form action="/unknown" '), 'FORM_ACTION'),
    (form().replace('value="976"', 'value="986"'), 'NODE_CONTEXT'),
    (form().replace('</form>', '<input name="offer_id" value="1"></form>'), 'OFFER_CONTEXT'),
    (form().replace('class="form-group"', 'class="other"'), 'SELECT_GROUP'),
    (form().replace(' name="fields[x]"', ''), 'FIELD_NAME_MISSING'),
    (form().replace('fields[x]', 'unsupported'), 'FIELD_NAME_UNSUPPORTED'),
    (form().replace('</div>', '</div><div class="form-group"><label>X</label><select name="fields[x]"><option>A</option></select></div>'), 'FIELD_DUPLICATE'),
    (form().replace('<select ', '<select multiple '), 'SELECT_MULTIPLE'),
    (form().replace('<label>Поле</label>', ''), 'LABEL_COUNT'),
    (form().replace('<label>Поле</label>', '<label></label>'), 'LABEL_TEXT'),
    (form().replace('<option value="a">A</option>', ''), 'OPTIONS_COUNT'),
    (form().replace('<option value="a">A</option>', '<option value="a"></option>'), 'OPTION_VALUE_OR_LABEL'),
    (form().replace('<select ', '<select size="invalid" '), 'SELECT_SIZE'),
    ('<form class="form-offer-editor"></form>', 'EMPTY_EDITOR'),
])
def test_parser_reason_survives_get_and_is_private(service, logs, html, reason):
    service.client.account.response.content = html.encode()
    error = assert_failure(service, logs, reason)
    assert error.facts['http_status'] == 200
    if reason != 'PARSER_INPUT':
        assert 'form_count' in error.facts and 'visible_select_count' in error.facts


@pytest.mark.parametrize('url,reason,category', [
    ('https://funpay.com/account/login?session='+PRIVATE, 'URL_AUTH', 'auth'),
    ('https://other.invalid/'+PRIVATE, 'URL_HOST', 'external'),
    ('https://funpay.com/unknown?token='+PRIVATE, 'URL_PATH', 'other'),
    ('https://funpay.com/lots/offerEdit?node=986&token='+PRIVATE, 'URL_NODE', 'editor'),
    ('https://funpay.com/lots/offerEdit?node=976&offer='+PRIVATE, 'URL_OFFER', 'editor'),
    ('https://[invalid/'+PRIVATE, 'URL_INVALID', 'invalid'),
])
def test_safe_final_url_categories(service, logs, url, reason, category):
    service.client.account.response.url = url
    error = assert_failure(service, logs, reason)
    assert error.facts['url_category'] == category


@pytest.mark.parametrize('status,reason', [(401,'HTTP_AUTH'), (403,'HTTP_AUTH'),
                                        (302,'HTTP_REDIRECT'), (500,'HTTP_STATUS')])
@pytest.mark.parametrize('raised', [False, True])
def test_http_status_with_real_installed_exception_contract(service, logs, status, reason, raised):
    if raised:
        response = Response(); response.status_code = status
        response._content = PRIVATE.encode()
        response.request = Request('GET', 'https://funpay.com/'+PRIVATE,
                                   headers={'X-Private': PRIVATE}).prepare()
        service.client.account.failure = (UnauthorizedError if status==403 else RequestFailedError)(response)
    else:
        service.client.account.response.status_code = status
    error = assert_failure(service, logs, reason)
    assert error.facts == {'http_status': status}


@pytest.mark.parametrize('content,reason', [('not bytes','CONTENT_TYPE'),
                                          (b'x'*2_000_001,'CONTENT_SIZE'), (b'\xff','CONTENT_ENCODING')],
                         ids=['wrong-type', 'oversized', 'invalid-utf8'])
def test_content_failure_categories(service, logs, content, reason):
    service.client.account.response.content = content
    assert_failure(service, logs, reason)


def test_timeout_invalidates_cached_schema_and_never_retries(service, logs):
    service.get(976)
    service.client.account.failure = Timeout(PRIVATE)
    assert_failure(service, logs, 'REQUEST_TRANSPORT')
    assert len(service.client.account.calls) == 2


def test_unexpected_request_does_not_format_exception(service, logs):
    class Unsafe(Exception):
        def __str__(self): pytest.fail('Exception text must not be inspected')
    service.client.account.failure = Unsafe(PRIVATE)
    assert_failure(service, logs, 'REQUEST_FAILED')


@pytest.mark.parametrize('scope,reason', [('wrong','ACCOUNT_SCOPE'), ('uninitialized','ACCOUNT_UNINITIALIZED')])
def test_account_scope_no_http(service, logs, scope, reason):
    service.client._node_form_cache = {}
    if scope == 'wrong': service.store.account_id = 11
    else: service.client.account.is_initiated = False
    assert_failure(service, logs, reason)
    assert service.client.account.calls == []


def test_pure_parser_keeps_reason_without_logging(logs):
    with pytest.raises(NodeFormError) as caught:
        parse_node_form('<body>'+PRIVATE+'</body>', 976)
    assert caught.value.reason == 'FORM_COUNT'
    assert caught.value.diagnostic == {'reason':'FORM_COUNT','form_count':0,'visible_select_count':0}
    assert logs == []


def test_facts_and_reason_are_allowlisted_even_after_attribute_changes():
    error = NodeFormError(PRIVATE, raw_html=PRIVATE, url_category=PRIVATE, http_status=True,
                          form_count=-1, visible_select_count=10**10)
    assert error.diagnostic == {'reason':'UNEXPECTED'}
    error.reason = PRIVATE
    error.facts = {'payload':PRIVATE, 'http_status':403, 'url_category':'auth'}
    assert error.diagnostic == {'reason':'UNEXPECTED','http_status':403,'url_category':'auth'}


@pytest.mark.parametrize('name,expected_shape', [
    ('unrecognized_dropdown', 'plain'),
    ('other[option]', 'other_namespace'),
    ('fields[one][two][three][four][five]', 'fields_deep'),
    ('fields[bad space]', 'fields_format'),
    ('fields[request_type]', 'service_like'),
    ('bad@field', 'other_format'),
])
def test_live_unsupported_name_format_is_classified_without_exposure(
        service, logs, name, expected_shape):
    # The rejected name and option label must not reach logs/UI/diagnostics.
    html = form().replace('fields[x]', name).replace('value="a"', 'value="SYNTHETIC_PRIVATE_RESPONSE_VALUE"')
    service.client.account.response.content = html.encode()
    error = assert_failure(service, logs, 'FIELD_NAME_UNSUPPORTED')
    from funpayflow.telegram_node_fields import safe_error_text
    ui = safe_error_text(error)
    assert error.diagnostic['unsupported_name_shape'] == expected_shape
    assert 'Формат проблемного поля: ' + expected_shape in ui
    combined = ui + logs[0] + repr(error.diagnostic)
    assert name not in combined
    assert PRIVATE not in combined


def test_unsupported_field_diagnostic_shape_is_fixed_allowlisted():
    bad = NodeFormError('FIELD_NAME_UNSUPPORTED', unsupported_name_shape='private-raw-value')
    assert bad.diagnostic == {'reason': 'FIELD_NAME_UNSUPPORTED'}
    good = NodeFormError('FIELD_NAME_UNSUPPORTED', unsupported_name_shape='other_namespace')
    assert good.diagnostic == {
        'reason': 'FIELD_NAME_UNSUPPORTED', 'unsupported_name_shape': 'other_namespace'
    }
    good.facts['unsupported_name_shape'] = 'SYNTHETIC_PRIVATE_RESPONSE_VALUE'
    assert good.diagnostic == {'reason': 'FIELD_NAME_UNSUPPORTED'}
