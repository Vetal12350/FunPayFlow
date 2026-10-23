"""Offline synthetic form parsing, GET-only contract, and scoped cache tests."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from requests.exceptions import Timeout

from funpayflow.node_form import NodeFormError, NodeFormService, parse_node_form, safe_form_evidence


FIXTURES = Path(__file__).parent / "fixtures" / "node_forms"


def fixture(name="cyberpunk"):
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def form(control='<select name="fields[x]"><option value="a">A</option></select>', label="Поле"):
    return ('<form class="form-offer-editor"><input type="hidden" name="node_id" value="976">'
            f'<div class="form-group"><label>{label}</label>{control}</div></form>')


def _reference_select_projection(html):
    """Model only the reported Sigma select loop and Cardinal select dict.

    Synthetic, selected options and visible form-groups only. This is not an
    execution of either Account implementation or a safe schema policy.
    """
    editor = BeautifulSoup(html, "html.parser").find("form", class_="form-offer-editor")
    selected, categories = {}, {}
    for control in editor.find_all("select"):
        if "hidden" in control.find_parent(class_="form-group").get("class", []):
            continue
        name = control["name"]
        selected[name] = control.find("option", selected=True)["value"]
        if name.startswith("fields["):
            categories[name] = [(option["value"], option.text)
                                for option in control.find_all("option")]
    return selected, categories


@pytest.mark.parametrize("extra_first", [False, True])
def test_reference_mixed_select_can_extract_category_without_plain_currency(service, monkeypatch, extra_first):
    category = ('<div class="form-group required"><label for="edition">Edition *</label>'
                '<select id="edition" name="fields[edition]" required>'
                '<option value="standard" selected>Standard</option>'
                '<option value="deluxe">Deluxe</option></select></div>')
    currency = ('<div class="form-group"><label for="currency">Currency</label>'
                '<select id="currency" name="currency"><option value="rub" selected>RUB</option>'
                '<option value="usd">USD</option></select></div>')
    editor = '<form class="form-offer-editor"><input name="node_id" value="976" type="hidden">'
    html = editor + (currency + category if extra_first else category + currency) + '</form>'
    selected, categories = _reference_select_projection(html)
    assert selected == {"fields[edition]": "standard", "currency": "rub"}
    assert categories == {"fields[edition]": [("standard", "Standard"), ("deluxe", "Deluxe")]}
    service.client.account.response.content = html.encode()
    logs = []
    monkeypatch.setattr('funpayflow.node_form.logger.warning', logs.append)
    result = service.get(976, force=True)
    assert logs == []
    assert [f['html_name'] for f in result['fields']] == ['fields[edition]']
    assert result['fields'][0]['required'] is True
    assert result['fields'][0]['value'] == 'standard'
    assert service.client.account.calls == [
        (("get", "lots/offerEdit?node=976", {"accept-language": "ru"}, {}), {"raise_not_200": True})]


def test_plain_required_category_is_retained_not_silently_omitted():
    # Real failure shape was 'plain', not a known exact technical name.
    html = form('<select name="edition" required><option value="deluxe" selected>Deluxe</option>'
                '<option value="standard">Standard</option></select>', label="Edition *")
    selected, categories = _reference_select_projection(html)
    assert selected == {'edition': 'deluxe'} and categories == {}
    schema = parse_node_form(html, 976)
    assert len(schema['fields']) == 1
    field = schema['fields'][0]
    assert field['html_name'] == 'edition'
    assert field['required'] is True
    assert field['value'] == 'deluxe'
    assert [x['value'] for x in field['options']] == ['deluxe', 'standard']


@pytest.mark.parametrize('name', ['category_type', 'edition', 'game-mode', 'region.us'])
def test_plain_named_optional_fields_preserve_identity(name):
    schema = parse_node_form(form(
        f'<select name="{name}"><option value="eu" selected>Europe</option>'
        '<option value="us">USA</option></select>'), 976)
    assert schema['fields'][0]['html_name'] == name
    assert schema['fields'][0]['options'][1]['value'] == 'us'


@pytest.mark.parametrize('name', [
    'account_type', 'authorization_code', 'seller_token', 'user_identifier',
    'fields', 'fieldsx', 'bad@field',
])
def test_plain_names_that_are_sensitive_or_malformed_still_fail_closed(name):
    with pytest.raises(NodeFormError) as caught:
        parse_node_form(form(
            f'<select name="{name}"><option value="x" selected>Private</option></select>'), 976)
    assert caught.value.reason == 'FIELD_NAME_UNSUPPORTED'
    assert name not in str(caught.value)


@pytest.mark.parametrize('name', ['api_key', 'form_created_at', 'csrf_token'])
def test_explicit_service_keys_are_excluded_not_saved(name):
    html = ('<form class="form-offer-editor"><input name="node_id" value="976">'
            '<div class="form-group"><label>Edition</label>'
            '<select name="fields[edition]"><option value="a" selected>A</option></select></div>'
            '<div class="form-group"><label>Service</label>'
            f'<select name="{name}"><option value="SYNTHETIC_PRIVATE">Secret</option></select></div>'
            '</form>')
    schema = parse_node_form(html, 976)
    assert [field['html_name'] for field in schema['fields']] == ['fields[edition]']
    assert 'SYNTHETIC_PRIVATE' not in repr(schema)


def test_plain_field_duplicate_and_multiple_still_rejected():
    html = form('<select name="edition"><option value="a" selected>A</option></select>')
    duplicate = html.replace('</form>',
        '<div class="form-group"><label>Edition 2</label>'
        '<select name="edition"><option value="b">B</option></select></div></form>')
    with pytest.raises(NodeFormError) as caught:
        parse_node_form(duplicate, 976)
    assert caught.value.reason == 'FIELD_DUPLICATE'
    with pytest.raises(NodeFormError) as caught:
        parse_node_form(html.replace('name="edition"', 'name="edition" multiple'), 976)
    assert caught.value.reason == 'SELECT_MULTIPLE'


@pytest.mark.parametrize('name,shape', [
    ('fields[]', 'fields_format'),
    ('fields[edition]suffix', 'fields_format'),
    ('fields[a][b][c][d][edition]', 'fields_deep'),
    ('fields[token_type]', 'service_like'),
])
def test_reference_prefix_is_not_category_validation(name, shape):
    html = form(f'<select name="{name}" required><option value="a" selected>A</option></select>')
    assert name in _reference_select_projection(html)[1]
    with pytest.raises(NodeFormError) as caught:
        parse_node_form(html, 976)
    assert caught.value.reason == 'FIELD_NAME_UNSUPPORTED'
    assert caught.value.diagnostic['unsupported_name_shape'] == shape


@pytest.mark.parametrize('duplicate_field', [False, True])
def test_reference_dicts_do_not_replace_duplicate_validation(duplicate_field):
    control = '<select name="fields[edition]"><option value="a" selected>A</option>'
    if duplicate_field:
        html = form(control + '</select>').replace('</form>',
            '<div class="form-group"><label>Other edition</label>'
            '<select name="fields[edition]"><option value="b" selected>B</option></select></div></form>')
        assert _reference_select_projection(html)[0] == {'fields[edition]': 'b'}
        reason = 'FIELD_DUPLICATE'
    else:
        html = form(control + '<option value="a">Other A</option></select>')
        assert len(_reference_select_projection(html)[1]['fields[edition]']) == 2
        reason = 'OPTION_VALUE_OR_LABEL'
    with pytest.raises(NodeFormError) as caught:
        parse_node_form(html, 976)
    assert caught.value.reason == reason


class Account:
    id = 10
    is_initiated = True

    def __init__(self):
        self.calls = []
        self.failure = None
        self.response = SimpleNamespace(status_code=200, content=fixture().encode(),
                                       url="https://funpay.com/lots/offerEdit?node=976")

    def method(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.failure:
            raise self.failure
        return self.response

    def save_lot(self, *args, **kwargs):
        pytest.fail("Schema fetch must never save a lot")

    def delete_lot(self, *args, **kwargs):
        pytest.fail("Schema fetch must never delete a lot")


@pytest.fixture
def service():
    client = SimpleNamespace(account=Account(), _account_lock=threading.RLock())
    store = SimpleNamespace(owner_id="1", account_id=10)
    clock = [1000.0]
    result = NodeFormService(client, store, clock=lambda: clock[0])
    result.test_clock = clock
    return result


def test_cyberpunk_complete_safe_structure_dom_order_defaults():
    schema = parse_node_form(fixture(), 976)
    assert schema["node_id"] == 976 and len(schema["fingerprint"]) == 64
    assert len(schema["generation"]) == 12 and schema["fetched_at"] > 0
    platform, edition = schema["fields"]
    assert [field["html_name"] for field in schema["fields"]] == ["fields[platform]", "fields[edition]"]
    assert platform["identity"] == platform["html_name"]
    assert platform["html_id"] == "platform" and platform["required"] is True
    assert platform["kind"] == "select" and platform["order"] == 0
    assert [option["label"] for option in platform["options"]] == ["PC", "PS", "Xbox"]
    assert [option["value"] for option in platform["options"]] == ["pc", "ps", "xbox"]
    assert [option["order"] for option in edition["options"]] == [0, 1, 2]
    assert [option["label"] for option in edition["options"]] == ["Standard Edition", "Ultimate Edition", "DLC"]
    assert platform["value"] == "pc" and platform["options"][0]["selected"] is True
    assert edition["default_value"] == "standard"
    serialized = json.dumps(schema)
    assert not any(marker in serialized for marker in ("SYNTHETIC", "csrf", "offer_id", "form_created_at", "Region"))


def test_elden_same_label_independent_identity_dom_order():
    fields = parse_node_form(fixture("elden_ring"), 986)["fields"]
    assert [field["display_label"] for field in fields] == [
        "Платформа", "Тип издания", "Тип издания (2)", "Способ получения", "Регион"]
    assert fields[1]["label"] == fields[2]["label"] == "Тип издания"
    assert fields[1]["html_name"] != fields[2]["html_name"]
    assert fields[1]["value"] == "base" and fields[2]["value"] == "standard"
    assert fields[3]["required"] is True
    assert fields[4]["options"][2]["disabled"] is True


@pytest.mark.parametrize("html", [
    '<form action="/account/login"><input type="password"></form>',
    '<p class="lead">private server error</p>',
    '<html><body>Unauthorized</body></html>',
    '<div class="form-group"><label>Поле</label><select name="fields[x]"><option>A</option></select></div>',
    form().replace('value="976"', 'value="986"'),
    form().replace('</form>', '<input name="offer_id" value="125"></form>'),
    form().replace('<label>Поле</label>', ''),
    form().replace('<label>Поле</label>', '<label>Поле</label><label>Другое поле</label>'),
    form('<select name="fields[x]"></select>'),
    form('<select><option>A</option></select>'),
    form('<select name="fields[x]" multiple><option>A</option></select>'),
    form('<select name="fields[x]"><option value="a" selected>A</option><option value="b" selected>B</option></select>'),
    form('<select name="fields[x]"><option value="a">A</option><option value="a">B</option></select>'),
    form('<select name="fields[x]"><option value="a">A</option><select name="fields[y]"><option>B</option></select></select>'),
    form() + form(),
    form().replace('</form>', '<div class="form-group"><label>Other</label><select name="fields[x]"><option>B</option></select></div></form>'),
])
def test_malformed_login_error_wrong_node_and_duplicate_names_fail_closed(html):
    with pytest.raises(NodeFormError, match="^Не удалось получить актуальные параметры раздела FunPay\\.$"):
        parse_node_form(html, 976)


@pytest.mark.parametrize("attr", ['hidden', 'class="hidden"', 'class="d-none"', 'style="display: none"',
                                  'style="visibility:hidden"', 'aria-hidden="true"'])
def test_hidden_ancestors_excluded(attr):
    html = form().replace('</form>', f'<div {attr}><div class="form-group"><select name="fields[hidden]"></select></div></div></form>')
    assert len(parse_node_form(html, 976)["fields"]) == 1


@pytest.mark.parametrize("name", ["csrf_token", "fields[session_token]", "fields[summary][ru]",
                                  "fields[desc][en]", "fields[payment_msg][ru]", "fields[images]"])
def test_service_even_if_select_is_excluded(name):
    html = form().replace('</form>', f'<div class="form-group"><label>Service</label><select name="{name}"><option value="SYNTHETIC_SECRET">Service</option></select></div></form>')
    assert "SYNTHETIC_SECRET" not in json.dumps(parse_node_form(html, 976))


def test_disabled_controls_options_optgroups_and_browser_default():
    html = form('<select name="fields[x]" disabled><option value="a" disabled>A</option>'
                '<optgroup label="Unavailable" disabled><option value="b">B</option></optgroup>'
                '<option value="c">C</option></select>')
    field = parse_node_form(html, 976)["fields"][0]
    assert field["disabled"] is True and field["value"] == "c"
    assert [option["disabled"] for option in field["options"]] == [True, True, False]
    assert not any(option["selected"] for option in field["options"])
    html = form('<select name="fields[x]"><option value="a" disabled selected>A</option><option value="b">B</option></select>')
    assert parse_node_form(html, 976)["fields"][0]["value"] == "a"
    html = form('<select name="fields[x]" size="3"><option>A</option></select>')
    assert parse_node_form(html, 976)["fields"][0]["value"] is None


def test_disabled_fieldset_and_required_marker():
    html = form(label='Поле <span class="text-danger">*</span>')
    html = html.replace('<div class="form-group">', '<fieldset disabled><div class="form-group">').replace('</div>', '</div></fieldset>')
    field = parse_node_form(html, 976)["fields"][0]
    assert field["required"] is True and field["disabled"] is True
    assert field["label"] == "Поле *" and field["display_label"] == "Поле"


def test_option_without_value_uses_text_and_placeholder_is_preserved():
    field = parse_node_form(form('<select name="fields[x]"><option value="" selected>Выберите</option>'
                                '<option> A   B </option></select>'), 976)["fields"][0]
    assert field["value"] == "" and field["options"][1]["value"] == "A B"


def test_fingerprint_ignores_hidden_values_and_selected_defaults():
    original = fixture()
    baseline = parse_node_form(original, 976)
    changed = parse_node_form(original.replace("SYNTHETIC_CSRF", "another-test-token").replace(
        'value="standard" selected', 'value="standard"').replace('value="ultimate"', 'value="ultimate" selected'), 976)
    assert baseline["fingerprint"] == changed["fingerprint"]
    assert baseline["generation"] != changed["generation"]
    assert baseline["fields"][1]["value"] != changed["fields"][1]["value"]
    changed = parse_node_form(original.replace("Ultimate Edition", "Ultimate"), 976)
    assert baseline["fingerprint"] != changed["fingerprint"]


def test_get_only_contract_no_save_post_or_other_requests(service):
    service.get(976)
    assert service.client.account.calls == [(("get", "lots/offerEdit?node=976", {"accept-language": "ru"}, {}),
                                              {"raise_not_200": True})]


@pytest.mark.parametrize("name,node_id,count", [("cyberpunk", 976, 2), ("elden_ring", 986, 5)])
@pytest.mark.parametrize("locale", ["ru", "en", "uk"])
def test_reported_nodes_committed_examples_through_scoped_get(service, name, node_id, count, locale):
    """Conceptual fixtures reproduce no failure; they do not prove live compatibility."""
    html = fixture(name)
    if "<body>" not in html:
        html = "<body>" + html + "</body>"
    html = html.replace('<body>', '<body data-app-data=\'{"userId":10}\'>')
    service.locale = locale
    response = service.client.account.response
    response.content = html.encode("utf-8")
    prefix = "" if locale == "ru" else f"/{locale}"
    response.url = f"https://funpay.com{prefix}/lots/offerEdit?node={node_id}"

    schema = service.get(node_id)

    assert schema["node_id"] == node_id and len(schema["fields"]) == count
    assert schema["fields"] == parse_node_form(fixture(name), node_id)["fields"]
    assert service.client.account.calls == [
        (("get", f"lots/offerEdit?node={node_id}", {"accept-language": locale}, {}),
         {"raise_not_200": True})]
    assert "SYNTHETIC" not in json.dumps(schema)


@pytest.mark.parametrize("node_id", [976, 986])
def test_namespaced_repeated_select_labels_keep_full_identity(node_id):
    """Synthetic namespace variants, not a claim about either live node's names."""
    html = ('<form class="form-offer-editor" action="/lots/offerSave">'
            f'<input type="hidden" name="node_id" value="{node_id}">'
            '<div class="form-group"><label for="base">Тип издания</label>'
            '<select id="base" name="fields[game][edition]">'
            '<option value="base" selected>Base</option></select></div>'
            '<div class="form-group"><label for="extra">Тип издания</label>'
            '<select id="extra" name="fields[extra][edition]">'
            '<option value="deluxe" selected>Deluxe</option></select></div></form>')

    fields = parse_node_form(html, node_id)["fields"]

    assert [field["identity"] for field in fields] == [
        "fields[game][edition]", "fields[extra][edition]"]
    assert [field["html_name"] for field in fields] == [field["identity"] for field in fields]
    assert [field["display_label"] for field in fields] == ["Тип издания", "Тип издания (2)"]
    assert [field["value"] for field in fields] == ["base", "deluxe"]


def test_lazy_cache_ttl_force_generation_and_detached_schema(service):
    assert not service.client.account.calls
    first = service.get(976)
    other = NodeFormService(service.client, service.store, clock=service.clock)
    assert other.get(976) == first
    service.test_clock[0] += 899
    assert service.get(976) == first
    assert len(service.client.account.calls) == 1
    first["fields"][0]["options"][0]["label"] = "Changed"
    assert other.get(976)["fields"][0]["options"][0]["label"] == "PC"
    service.test_clock[0] += 1
    second = service.get(976)
    assert first["generation"] != second["generation"]
    third = service.get(976, force=True)
    assert third["generation"] != second["generation"] and third["fingerprint"] == second["fingerprint"]
    assert len(service.client.account.calls) == 3


def test_scoped_owner_locale_node_runtime_and_account_identity(service):
    first = service.get(976)
    owner = NodeFormService(service.client, SimpleNamespace(owner_id="2", account_id=10))
    assert owner.get(976)["generation"] != first["generation"]
    locale = NodeFormService(service.client, service.store, locale="en")
    assert locale.get(976)["generation"] != first["generation"]
    account = service.client.account
    other_client = SimpleNamespace(account=account, _account_lock=threading.RLock())
    assert NodeFormService(other_client, service.store).get(976)["generation"] != first["generation"]
    account.response.content = fixture("elden_ring").encode()
    account.response.url = "https://funpay.com/lots/offerEdit?node=986"
    assert service.get(986)["node_id"] == 986
    service.client.account = Account()
    assert service.get(976)["generation"] != first["generation"]
    service.client.account.id = 11
    with pytest.raises(NodeFormError):
        service.get(976)


@pytest.mark.parametrize("force", [False, True])
def test_failed_refresh_invalidates_old_generation_never_falls_back(service, force):
    first = service.get(976)
    service.test_clock[0] += 900 if not force else 0
    service.client.account.failure = Timeout("private cookie csrf detail")
    for refresh in (force, False):
        with pytest.raises(NodeFormError) as exc:
            service.get(976, force=refresh)
        assert str(exc.value) == "Не удалось получить актуальные параметры раздела FunPay."
        assert exc.value.__suppress_context__ is True
    assert not service.client._node_form_cache
    service.client.account.failure = None
    assert service.get(976)["generation"] != first["generation"]


def test_force_invalidates_only_requested_scope(service):
    base = service.get(976)
    owner = NodeFormService(service.client, SimpleNamespace(owner_id="2", account_id=10), clock=service.clock)
    other = owner.get(976)
    service.client.account.failure = Timeout("synthetic")
    with pytest.raises(NodeFormError):
        service.get(976, force=True)
    assert owner.get(976)["generation"] == other["generation"]
    service.client.account.failure = None
    assert service.get(976)["generation"] != base["generation"]


@pytest.mark.parametrize("status", [401, 403, 302, 404, 500])
def test_unsuccessful_http_status_rejected(service, status):
    service.client.account.response.status_code = status
    with pytest.raises(NodeFormError):
        service.get(976)


@pytest.mark.parametrize("url", ["https://funpay.com/account/login", "https://evil.example/lots/offerEdit?node=976",
                                 "https://funpay.com/lots/offerEdit?node=986",
                                 "https://funpay.com/lots/offerEdit?node=976&offer=123"])
def test_login_redirect_wrong_url_context_rejected(service, url):
    service.client.account.response.url = url
    with pytest.raises(NodeFormError):
        service.get(976)


def test_lru_cache_bounded(service):
    for owner in range(35):
        NodeFormService(service.client, SimpleNamespace(owner_id=owner, account_id=10)).get(976)
    assert len(service.client._node_form_cache) == 32
    assert next(iter(service.client._node_form_cache))[0] == "3"


@pytest.mark.parametrize("node", [True, 0, -1, "976", 2**64])
def test_invalid_node_never_dispatches(service, node):
    with pytest.raises(NodeFormError):
        service.get(node)
    assert not service.client.account.calls


def test_uninitialized_account_never_dispatches(service):
    service.client.account.is_initiated = False
    with pytest.raises(NodeFormError):
        service.get(976)
    assert not service.client.account.calls


@pytest.mark.parametrize("user_id", [0, 11, "10", None])
def test_embedded_unauthorized_or_wrong_account_context_rejected(service, user_id):
    app_data = json.dumps({"userId": user_id, "csrf-token": "SYNTHETIC_PRIVATE"})
    html = fixture().replace('<body>', f"<body data-app-data='{app_data}'>")
    service.client.account.response.content = html.encode()
    with pytest.raises(NodeFormError):
        service.get(976)


def test_embedded_account_metadata_is_validated_but_never_retained(service):
    app_data = json.dumps({"userId": 10, "csrf-token": "SYNTHETIC_PRIVATE"})
    html = fixture().replace('<body>', f"<body data-app-data='{app_data}'>")
    service.client.account.response.content = html.encode()
    schema = service.get(976)
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(schema)
    assert 'csrf' not in json.dumps(service.client._node_form_cache[next(iter(service.client._node_form_cache))]['schema'])


def test_known_editor_without_category_selects_is_valid():
    schema = parse_node_form('<form class="form-offer-editor"><input name="price"></form>', 976)
    assert schema["fields"] == []


@pytest.mark.parametrize('name',['fields[token_type]','fields[request_type]'])
def test_ambiguous_visible_service_like_names_fail_closed(name):
    html=form().replace('</form>',f'<div class="form-group"><label>Ambiguous</label><select name="{name}"><option value="a">A</option></select></div></form>')
    with pytest.raises(NodeFormError): parse_node_form(html,976)


def test_wrong_editor_action_rejected():
    html=form().replace('class="form-offer-editor"','class="form-offer-editor" action="https://example.invalid/delete"')
    with pytest.raises(NodeFormError): parse_node_form(html,976)


@pytest.mark.parametrize('name', ['fields[cookie]', 'fields[cookies]', 'fields[api_key]',
                                'fields[authorization]', 'fields[Cookie]'])
def test_visible_service_selects_are_never_retained(name):
    original = parse_node_form(form(), 976)
    html = form().replace('</form>', f'<div class="form-group"><label>Service</label>'
                        f'<select name="{name}"><option value="SYNTHETIC_PRIVATE">Private</option>'
                        '</select></div></form>')
    actual = parse_node_form(html, 976)
    assert actual['fields'] == original['fields']
    assert actual['fingerprint'] == original['fingerprint']
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(actual)


def evidence_form(extra='', offer='0'):
    return form().replace('class="form-offer-editor"',
        'class="form-offer-editor" action="/lots/offerSave" method="post"').replace(
        '</form>', f'<input type="hidden" name="offer_id" value="{offer}">{extra}</form>')


@pytest.mark.parametrize('offer', ['', '0'])
@pytest.mark.parametrize('csrf', [False, True])
def test_transient_form_evidence_single_get_and_no_hidden_cache_data(service, monkeypatch, offer, csrf):
    private = 'SYNTHETIC_PRIVATE_VALUE'
    extra = f'<input type="hidden" name="csrf_token" value="{private}">' if csrf else ''
    html = ('<body data-app-data=\'{"userId":10,"csrf-token":"SYNTHETIC_APP_PRIVATE"}\'>'
            + evidence_form(extra, offer) + '</body>')
    service.client.account.response.content = html.encode()
    logs = []
    monkeypatch.setattr('funpayflow.node_form.logger.warning', logs.append)
    schema, evidence = service.get_with_evidence(976)
    assert evidence == dict(source='response', controls='recognized', new_form_context='candidate',
        csrf_input_presence='present' if csrf else 'absent', manual_delivery='unverified',
        submit_contract='unverified', response_id='unverified')
    ordinary = parse_node_form(html, 976)
    assert schema.keys() == ordinary.keys()
    assert schema['fields'] == ordinary['fields'] and schema['fingerprint'] == ordinary['fingerprint']
    assert service.get(976) == schema
    cached_schema, cached = service.get_with_evidence(976)
    assert cached_schema == schema and cached == dict(safe_form_evidence(), source='cache')
    evidence['new_form_context'] = private
    assert service.get_with_evidence(976)[1] == cached
    entry = next(iter(service.client._node_form_cache.values()))
    assert set(entry) == {'account', 'expires', 'schema'}
    retained = json.dumps([schema, entry['schema'], cached, logs])
    for forbidden in (private, 'SYNTHETIC_APP_PRIVATE', 'csrf_token', 'offer_id', 'controls', 'new_form_context'):
        assert forbidden not in json.dumps([schema, entry['schema'], logs])
    assert private not in retained
    assert len(service.client.account.calls) == 1 and logs == []
    service.test_clock[0] += 899
    assert service.get_with_evidence(976)[1] == cached
    service.test_clock[0] += 1
    assert service.get_with_evidence(976)[1]['source'] == 'response'
    assert len(service.client.account.calls) == 2


def test_evidence_does_not_refresh_an_ordinary_cached_schema(service):
    first = service.get(976)
    schema, evidence = service.get_with_evidence(976)
    assert schema == first and evidence == dict(safe_form_evidence(), source='cache')
    assert len(service.client.account.calls) == 1


@pytest.mark.parametrize('extra', [
    '<input type="hidden" name="SYNTHETIC_PRIVATE_NAME" value="SYNTHETIC_PRIVATE_VALUE">',
    '<input type="hidden" name="csrf_token" value="x"><input type="hidden" name="csrf_token" value="y">',
    '<input type="hidden" name="token" value="SYNTHETIC_PRIVATE_VALUE">',
    '<input type="hidden" value="SYNTHETIC_PRIVATE_VALUE">',
    '<input type="hidden" name="csrf_token">',
    '<input type="text" name="csrf_token" value="x">',
    '<input type="hidden" name="node" value="976">',
    '<input type="hidden" name="offer" value="0">',
    '<input type="hidden" name="csrf_token" value="x" disabled>',
    '<input type="hidden" name="csrf_token" value="x" form="other">',
    '<input type="hidden" name="csrf_token" name="csrf_token" value="x">',
    '<button formaction="https://example.invalid/SYNTHETIC_PRIVATE_VALUE">Send</button>',
    '<script>SYNTHETIC_PRIVATE_VALUE</script>',
    '<template><input type="hidden" name="csrf_token" value="x"></template>',
    '<input name="price" onclick="SYNTHETIC_PRIVATE_VALUE">',
    '<input name="price"/><input name="price">',
    '<fieldset><input name="price"></div>',
    '<object name="SYNTHETIC_PRIVATE_NAME"></object>',
])
def test_unknown_duplicate_malformed_controls_fail_closed_without_changing_schema(service, extra):
    html = evidence_form(extra)
    ordinary = parse_node_form(html, 976)
    service.client.account.response.content = html.encode()
    schema, evidence = service.get_with_evidence(976)
    assert evidence == dict(safe_form_evidence(), source='response')
    assert schema['fields'] == ordinary['fields'] and schema['fingerprint'] == ordinary['fingerprint']
    assert 'SYNTHETIC_PRIVATE' not in json.dumps([schema, evidence])
    assert len(service.client.account.calls) == 1


@pytest.mark.parametrize('old,new', [
    (' action="/lots/offerSave"', ''), (' method="post"', ''),
    ('name="offer_id"', 'name="offer_id_missing"'),
    ('<input type="hidden" name="offer_id" value="0">', ''),
    ('<input type="hidden" name="node_id" value="976">', ''),
    ('</form>', ''), ('method="post"', 'method="get"'),
    ('action="/lots/offerSave"', 'action="http://funpay.com/lots/offerSave"'),
    ('action="/lots/offerSave"', 'action="lots/offerSave"'),
    ('action="/lots/offerSave"', 'action="https:/lots/offerSave"'),
])
def test_missing_or_ambiguous_new_identity_is_unknown(service, old, new):
    service.client.account.response.content = evidence_form().replace(old, new).encode()
    _, evidence = service.get_with_evidence(976)
    assert evidence['new_form_context'] == 'unknown'


@pytest.mark.parametrize('extra', ['', '<input type="checkbox" name="auto_delivery">',
                                 '<input type="checkbox" name="auto_delivery" checked>'])
def test_delivery_presence_never_proves_manual_mode(service, extra):
    service.client.account.response.content = evidence_form(extra).encode()
    _, evidence = service.get_with_evidence(976)
    assert evidence['manual_delivery'] == evidence['submit_contract'] == evidence['response_id'] == 'unverified'


@pytest.mark.parametrize('failure,reason', [
    ('edit', 'OFFER_CONTEXT'), ('duplicate_offer', 'OFFER_CONTEXT'),
    ('node', 'NODE_CONTEXT'), ('user', 'APP_USER_SCOPE'), ('login', 'LOGIN_PAGE'),
    ('url_auth', 'URL_AUTH'), ('redirect', 'HTTP_REDIRECT'), ('url_node', 'URL_NODE'),
    ('url_offer', 'URL_OFFER'), ('action', 'FORM_ACTION'), ('transport', 'REQUEST_TRANSPORT'),
])
def test_evidence_preserves_existing_rejections_and_safe_errors(service, monkeypatch, failure, reason):
    private = 'SYNTHETIC_PRIVATE_VALUE'
    response = service.client.account.response
    html = evidence_form(f'<input type="hidden" name="csrf_token" value="{private}">')
    if failure == 'edit':
        html = html.replace('name="offer_id" value="0"', 'name="offer_id" value="123"')
    elif failure == 'duplicate_offer':
        html = html.replace('</form>', '<input name="offer_id" value="0"></form>')
    elif failure == 'node':
        html = html.replace('value="976"', 'value="986"')
    elif failure == 'user':
        html = '<body data-app-data=\'{"userId":11}\'>' + html + '</body>'
    elif failure == 'login':
        html += '<input type="password">'
    elif failure == 'url_auth':
        response.url = 'https://funpay.com/account/login?' + private
    elif failure == 'redirect':
        response.status_code = 302
    elif failure == 'url_node':
        response.url = 'https://funpay.com/lots/offerEdit?node=986'
    elif failure == 'url_offer':
        response.url += '&offer=' + private
    elif failure == 'action':
        html = html.replace('/lots/offerSave', 'https://example.invalid/' + private)
    else:
        service.client.account.failure = Timeout(private)
    response.content = html.encode()
    logs = []
    monkeypatch.setattr('funpayflow.node_form.logger.warning', logs.append)
    with pytest.raises(NodeFormError) as exc:
        service.get_with_evidence(976)
    assert exc.value.reason == reason
    assert exc.value.__suppress_context__ is True
    assert private not in str(exc.value) + repr(exc.value) + json.dumps(exc.value.diagnostic) + str(logs)
    assert service.client._node_form_cache == {} and len(service.client.account.calls) == 1


@pytest.mark.parametrize('url', [None, 'http://funpay.com/lots/offerEdit?node=976',
    'https://funpay.com/lots/offerEdit?node=976&private=SYNTHETIC_PRIVATE_VALUE'])
def test_evidence_stricter_url_context_does_not_change_ordinary_parser(service, url):
    service.client.account.response.url = url
    service.client.account.response.content = evidence_form().encode()
    schema, evidence = service.get_with_evidence(976)
    assert schema['fields'] and evidence == dict(safe_form_evidence(), source='response')


@pytest.mark.parametrize('invalid', [None, [], {'source': 'response'},
    dict(safe_form_evidence(), controls=True), dict(safe_form_evidence(), response_id='SYNTHETIC_PRIVATE_VALUE'),
    dict(safe_form_evidence(), secret='SYNTHETIC_PRIVATE_VALUE')])
def test_form_evidence_is_a_strict_fixed_enum_contract(invalid):
    assert safe_form_evidence(invalid) == safe_form_evidence()


def test_form_evidence_cannot_upgrade_cache_or_unknown_controls():
    for update in ({'source': 'cache', 'controls': 'recognized'}, {'source': 'response'}):
        value = dict(safe_form_evidence(), new_form_context='candidate', csrf_input_presence='present', **update)
        clean = safe_form_evidence(value)
        assert clean['new_form_context'] == 'unknown' and clean['csrf_input_presence'] == 'ambiguous'


def test_concurrent_evidence_calls_share_one_account_get(service):
    service.client.account.response.content = evidence_form().encode()
    barrier = threading.Barrier(2)
    method = service.client.account.method

    def locked_get(*args, **kwargs):
        assert service.client._account_lock._is_owned()
        return method(*args, **kwargs)

    service.client.account.method = locked_get

    def read():
        barrier.wait(timeout=5)
        return NodeFormService(service.client, service.store, clock=service.clock).get_with_evidence(976)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(read) for _ in range(2)]
        results = [future.result(timeout=5) for future in pending]
    assert results[0][0] == results[1][0]
    assert {result[1]['source'] for result in results} == {'response', 'cache'}
    assert service.client.account.calls == [
        (('get', 'lots/offerEdit?node=976', {'accept-language': 'ru'}, {}), {'raise_not_200': True})]


def test_failed_evidence_read_after_expiry_does_not_fall_back_or_retry(service):
    schema = service.get(976)
    service.test_clock[0] += 900
    service.client.account.failure = Timeout('SYNTHETIC_PRIVATE')
    with pytest.raises(NodeFormError):
        service.get_with_evidence(976)
    assert len(service.client.account.calls) == 2 and not service.client._node_form_cache
    service.client.account.failure = None
    assert service.get(976)['generation'] != schema['generation']
