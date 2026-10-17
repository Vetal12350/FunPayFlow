"""Offline synthetic form parsing, GET-only contract, and scoped cache tests."""

import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from requests.exceptions import Timeout

from funpayflow.node_form import NodeFormError, NodeFormService, parse_node_form


FIXTURES = Path(__file__).parent / "fixtures" / "node_forms"


def fixture(name="cyberpunk"):
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def form(control='<select name="fields[x]"><option value="a">A</option></select>', label="Поле"):
    return ('<form class="form-offer-editor"><input type="hidden" name="node_id" value="976">'
            f'<div class="form-group"><label>{label}</label>{control}</div></form>')


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
