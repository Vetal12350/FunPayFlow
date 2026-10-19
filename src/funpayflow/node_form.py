"""Read-only, bounded snapshots of visible FunPay create-form select fields.

Uses the runtime's existing Account and lock. No raw HTML or hidden values leave
the parser, and unsuccessful refreshes invalidate the previous generation.
"""

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from bs4 import BeautifulSoup
from requests.exceptions import RequestException
from FunPayAPI.common.exceptions import RequestFailedError, UnauthorizedError

from . import logger


_UNAVAILABLE = "Не удалось получить актуальные параметры раздела FunPay."
_TTL = 900
_MAX_SCOPES = 32
_MAX_HTML = 2_000_000
_STANDARD = {"summary", "desc", "payment_msg", "images"}
_SERVICE = re.compile(r"csrf|token|session|request|form_created|offer_id|node_id|password|secret", re.I)
_SERVICE_KEYS = {'csrf', 'csrf_token', 'session', 'session_id', 'session_token', 'request', 'request_id',
                 'request_token', 'form_created_at', 'offer_id', 'node_id', 'password', 'secret',
                 'token', 'access_token', 'refresh_token', 'api_key', 'cookies', 'cookie', 'authorization'}
_NAME = re.compile(r"fields(?:\[[A-Za-z0-9_.:-]{1,64}\]){1,4}\Z")
# A visible category select may also use a simple HTML name (reference editors
# first collect all selects). Accept only bounded, non-sensitive names, never
# request/account/service keys or malformed fields[...] namespaces.
_PLAIN_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")
_PLAIN_SERVICE = frozenset({
    "price", "amount", "auto_delivery", "active", "deactivate_after_sale",
    "query", "currency", "secrets", "images", "offer", "node", "language", "lang",
    "locale",
})
_SENSITIVE_PLAIN = re.compile(
    r"account|auth|captcha|cookie|credential|csrf|email|hash|nonce|password|"
    r"phone|private|request|secret|seller|session|signature|token|user_id|api_key", re.I
)
_REASONS = frozenset('''UNEXPECTED PARSER_UNEXPECTED REQUEST_INVALID ACCOUNT_SCOPE ACCOUNT_UNINITIALIZED
HTTP_AUTH HTTP_STATUS HTTP_REDIRECT REQUEST_TRANSPORT REQUEST_FAILED URL_INVALID URL_HOST URL_PATH URL_NODE URL_OFFER URL_AUTH
CONTENT_TYPE CONTENT_SIZE CONTENT_ENCODING PARSER_INPUT APP_DATA_JSON APP_DATA_SHAPE APP_USER_TYPE APP_USER_INVALID APP_USER_SCOPE
LOGIN_PAGE ERROR_PAGE FORM_COUNT FORM_HIDDEN FORM_ACTION NODE_CONTEXT OFFER_CONTEXT SELECT_GROUP FIELD_NAME_MISSING
FIELD_NAME_UNSUPPORTED FIELD_DUPLICATE SELECT_MULTIPLE SELECT_NESTED SELECT_STRUCTURE LABEL_COUNT LABEL_TEXT
TEXT_INVALID FIELD_ID OPTION_TEXT OPTION_VALUE OPTION_STRUCTURE OPTION_VALUE_OR_LABEL OPTIONS_COUNT SELECTED_COUNT SELECT_SIZE FIELDS_COUNT EMPTY_EDITOR'''.split())
_URL_CATEGORIES = frozenset({'missing', 'invalid', 'external', 'auth', 'other', 'editor'})
# Fixed categories only: never persist, log or echo the actual HTML name.
_UNSUPPORTED_NAME_SHAPES = frozenset({
    'service_like', 'fields_deep', 'fields_format', 'plain', 'other_namespace', 'other_format'
})


def _safe_facts(facts):
    clean = {}
    if not isinstance(facts, dict):
        return clean
    for key, maximum in (('http_status', 599), ('form_count', 1001), ('visible_select_count', 1001)):
        value = facts.get(key)
        minimum = 100 if key == 'http_status' else 0
        if type(value) is int and minimum <= value <= maximum:
            clean[key] = value
    category = facts.get('url_category')
    if type(category) is str and category in _URL_CATEGORIES:
        clean['url_category'] = category
    shape = facts.get('unsupported_name_shape')
    if type(shape) is str and shape in _UNSUPPORTED_NAME_SHAPES:
        clean['unsupported_name_shape'] = shape
    return clean


def _http_reason(status, unauthorized=False):
    if unauthorized or status in {401, 403}:
        return 'HTTP_AUTH'
    return 'HTTP_REDIRECT' if type(status) is int and 300 <= status < 400 else 'HTTP_STATUS'


class NodeFormError(ValueError):
    def __init__(self, reason='UNEXPECTED', **facts):
        super().__init__(_UNAVAILABLE)
        self.reason = reason if type(reason) is str and reason in _REASONS else 'UNEXPECTED'
        self.facts = _safe_facts(facts)

    @property
    def diagnostic(self):
        # Re-sanitize even if a caller has changed the attributes.
        return {'reason': self.reason if type(self.reason) is str and self.reason in _REASONS else 'UNEXPECTED',
                **_safe_facts(self.facts)}


def _positive(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def _text(value, limit=512, empty=False, normalize=True, reason='TEXT_INVALID'):
    if not isinstance(value, str) or len(value) > limit:
        raise NodeFormError(reason)
    if any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise NodeFormError(reason)
    if normalize:
        value = " ".join(value.split())
    elif any(char in value for char in "\t\r\n"):
        raise NodeFormError(reason)
    if not empty and not value:
        raise NodeFormError(reason)
    return value


def _hidden(tag):
    for parent in [tag, *tag.parents]:
        if getattr(parent, "attrs", None) is None:
            continue
        if (parent.name in {"script", "template", "noscript"}
                or parent.has_attr("hidden")
                or str(parent.get("aria-hidden", "")).lower() == "true"
                or set(parent.get("class", [])) & {"hidden", "hide", "d-none"}
                or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
                             parent.get("style", ""), re.I)):
            return True
    return False


def _disabled(tag):
    if tag.has_attr("disabled"):
        return True
    for parent in tag.parents:
        if parent.name == "fieldset" and parent.has_attr("disabled"):
            # HTML exempts descendants of the disabled fieldset's first legend.
            legend = parent.find("legend", recursive=False)
            if legend is None or legend not in tag.parents:
                return True
    return False


def _field_name(name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return False
    first = name.split("[", 1)[1].split("]", 1)[0]
    parts = re.findall(r'\[([^\]]+)\]', name)
    return (first not in _STANDARD and not _SERVICE.search(name)
            and not any(part.casefold() in _SERVICE_KEYS for part in parts))


def _unsupported_name_shape(name):
    """Classify a rejected select name without retaining any response text."""
    if not isinstance(name, str):
        return 'other_format'
    if _SERVICE.search(name) or any(
            part.casefold() in _SERVICE_KEYS for part in re.findall(r'\[([^\]]+)\]', name)):
        return 'service_like'
    if name.startswith('fields'):
        if name.startswith('fields[') and name.count('[') > 4:
            return 'fields_deep'
        return 'fields_format'
    if re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', name):
        return 'plain'
    if re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}(?:\[[A-Za-z0-9_.:-]{1,64}\]){1,4}', name):
        return 'other_namespace'
    return 'other_format'


def _parse_impl(html, node_id, account_id, facts):
    if not _positive(node_id) or not isinstance(html, str) or not 0 < len(html) <= _MAX_HTML:
        raise NodeFormError("PARSER_INPUT")
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.select("form.form-offer-editor")
    facts.update(form_count=min(len(forms), 1001), visible_select_count=min(
        sum(not _hidden(control) for form in forms for control in form.find_all('select')), 1001))
    body = soup.find("body")
    if body is not None and body.has_attr("data-app-data"):
        # Account.get() uses this public user ID to establish authentication.
        # Any other app-data members (including service tokens) stay transient.
        try:
            app_data = json.loads(body["data-app-data"])
        except (ValueError, TypeError):
            raise NodeFormError('APP_DATA_JSON') from None
        if not isinstance(app_data, dict):
            raise NodeFormError('APP_DATA_SHAPE')
        user_id = app_data.get("userId")
        if type(user_id) is not int:
            raise NodeFormError("APP_USER_TYPE")
        if not _positive(user_id):
            raise NodeFormError('APP_USER_INVALID')
        if account_id is not None and user_id != account_id:
            raise NodeFormError('APP_USER_SCOPE')
    if soup.select_one('input[type="password"], form[action*="login"], form[action*="account/auth"]'):
        raise NodeFormError('LOGIN_PAGE')
    if soup.select_one("p.lead, .alert-danger, .error-page"):
        raise NodeFormError('ERROR_PAGE')
    if len(forms) != 1:
        raise NodeFormError("FORM_COUNT")
    if _hidden(forms[0]):
        raise NodeFormError('FORM_HIDDEN')
    form = forms[0]
    if form.get('action'):
        try:
            action = urlsplit(form['action'])
        except ValueError:
            raise NodeFormError('FORM_ACTION') from None
        if (action.query or action.fragment or (action.netloc and action.netloc != 'funpay.com')
                or action.path not in {'/lots/offerSave', 'lots/offerSave', '/en/lots/offerSave', '/uk/lots/offerSave'}):
            raise NodeFormError("FORM_ACTION")
    # Values here only confirm the response context; they never enter schema.
    for name in ("node_id", "node"):
        controls = form.find_all("input", attrs={"name": name})
        if len(controls) > 1 or any(str(control.get("value", "")) != str(node_id) for control in controls):
            raise NodeFormError("NODE_CONTEXT")
    for name in ("offer_id", "offer"):
        controls = form.find_all("input", attrs={"name": name})
        if len(controls) > 1 or any(str(control.get("value", "")) not in {"", "0"} for control in controls):
            raise NodeFormError("OFFER_CONTEXT")
    fields, names, counts = [], set(), {}
    for control in form.find_all("select"):
        if _hidden(control):
            continue
        group = control.find_parent(class_="form-group")
        if group is None:
            raise NodeFormError("SELECT_GROUP")
        name = control.get("name")
        if not name:
            raise NodeFormError("FIELD_NAME_MISSING")
        if not _field_name(name):
            # Safe plain category names are supported in addition to fields[...].
            # Known controls stay excluded; unexpected private/service-like names
            # fail closed rather than being copied into persisted draft metadata.
            parts = re.findall(r'\[([^\]]+)\]', name) or [name]
            if (any(part.casefold() in _SERVICE_KEYS for part in parts)
                    or (_NAME.fullmatch(name) and parts[0] in _STANDARD)
                    or (type(name) is str and name.casefold() in _PLAIN_SERVICE)):
                continue
            plain_category = (type(name) is str and _PLAIN_NAME.fullmatch(name)
                              and not name.casefold().startswith("fields")
                              and not _SENSITIVE_PLAIN.search(name))
            if not plain_category:
                facts['unsupported_name_shape'] = _unsupported_name_shape(name)
                raise NodeFormError("FIELD_NAME_UNSUPPORTED")
        if name in names:
            raise NodeFormError('FIELD_DUPLICATE')
        if control.has_attr('multiple'):
            raise NodeFormError('SELECT_MULTIPLE')
        if control.find('select') or control.find_parent('select'):
            raise NodeFormError('SELECT_NESTED')
        names.add(name)
        html_id = control.get("id")
        if html_id is not None:
            html_id = _text(html_id, 256, normalize=False, reason='FIELD_ID')
        labels = [label for label in group.find_all("label") if not _hidden(label)]
        matched = [label for label in labels if html_id and label.get("for") == html_id]
        if not matched:
            matched = [label for label in labels if control in label.descendants]
        if not matched:
            matched = [label for label in labels if not label.has_attr("for")]
        if len(matched) != 1:
            raise NodeFormError("LABEL_COUNT")
        label_tag = matched[0]
        label = _text(label_tag.get_text(" ", strip=True), 256, reason='LABEL_TEXT')
        display = label.rstrip(" *").strip()
        if not display:
            raise NodeFormError("LABEL_TEXT")
        options, values = [], set()
        for option in control.find_all("option"):
            if option.find_parent("select") is not control or option.find("option"):
                raise NodeFormError("OPTION_STRUCTURE")
            option_label = _text(option.get_text(" ", strip=True), empty=True, reason='OPTION_TEXT')
            value = _text(option.get("value", option_label), empty=True, normalize=False, reason='OPTION_VALUE')
            if value in values or (value and not option_label):
                raise NodeFormError("OPTION_VALUE_OR_LABEL")
            values.add(value)
            optgroup = option.find_parent("optgroup")
            disabled = option.has_attr("disabled") or bool(optgroup and optgroup.has_attr("disabled"))
            options.append(dict(value=value, label=option_label, selected=option.has_attr("selected"),
                                disabled=disabled, order=len(options)))
        if not options or len(options) > 1000:
            raise NodeFormError("OPTIONS_COUNT")
        selected = [option for option in options if option["selected"]]
        if len(selected) > 1:
            raise NodeFormError("SELECTED_COUNT")
        default = selected[0] if selected else None
        size = control.get("size", "1")
        if not re.fullmatch(r"\d{1,4}", size):
            raise NodeFormError("SELECT_SIZE")
        if default is None and int(size) <= 1:
            default = next((option for option in options if not option["disabled"]), None)
        # selected preserves the source attribute; value records browser default.
        value = default["value"] if default is not None else None
        counts[display] = counts.get(display, 0) + 1
        display_label = display if counts[display] == 1 else f"{display} ({counts[display]})"
        fields.append(dict(identity=name, html_name=name, html_id=html_id, label=label,
                           display_label=display_label, kind="select", required=(
                               control.has_attr("required") or "required" in group.get("class", [])
                               or label_tag.select_one(".text-danger") is not None or "*" in label),
                           disabled=_disabled(control), order=len(fields), options=options,
                           value=value, default_value=value))
        if len(fields) > 100:
            raise NodeFormError("FIELDS_COUNT")
    # A recognized editor with standard controls can legitimately have no selects.
    if not fields and not form.select_one('[name="price"], [name="fields[summary][ru]"]'):
        raise NodeFormError("EMPTY_EDITOR")
    safe = [dict(html_name=field["html_name"], kind=field["kind"], label=field["label"],
                 required=field["required"], disabled=field["disabled"], order=field["order"],
                 options=[{key: option[key] for key in ("value", "label", "order", "disabled")}
                          for option in field["options"]]) for field in fields]
    fingerprint = hashlib.sha256(json.dumps(safe, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
    return dict(node_id=node_id, fields=fields, fetched_at=time.time(), fingerprint=fingerprint,
                generation=uuid.uuid4().hex[:12])


def _parse(html, node_id, account_id=None):
    facts = {}
    try:
        return _parse_impl(html, node_id, account_id, facts)
    except NodeFormError as error:
        raise NodeFormError(error.reason, **{**facts, **error.facts}) from None
    except Exception:
        raise NodeFormError('PARSER_UNEXPECTED', **facts) from None


def parse_node_form(html, node_id):
    """Parse safe metadata only; never expose server text in an exception."""
    return _parse(html, node_id)


class NodeFormService:
    def __init__(self, client, store, locale="ru", clock=None):
        self.client, self.store, self.locale = client, store, locale
        self.clock = clock or time.monotonic

    def get(self, node_id, force=False):
        try:
            return self._get(node_id, force=force)
        except NodeFormError as error:
            logger.warning('NodeForm failure ' + json.dumps(error.diagnostic, sort_keys=True))
            raise NodeFormError(error.reason, **error.facts) from None
        except Exception:
            logger.warning('NodeForm failure ' + json.dumps({'reason': 'UNEXPECTED'}))
            raise NodeFormError('UNEXPECTED') from None

    def _get(self, node_id, force=False):
        if not _positive(node_id) or self.locale not in {"ru", "en", "uk"}:
            raise NodeFormError('REQUEST_INVALID')
        with self.client._account_lock:
            account = self.client.account
            if account.id != self.store.account_id:
                raise NodeFormError('ACCOUNT_SCOPE')
            if getattr(account, "is_initiated", True) is not True:
                raise NodeFormError('ACCOUNT_UNINITIALIZED')
            cache = getattr(self.client, "_node_form_cache", None)
            if cache is None:
                cache = self.client._node_form_cache = OrderedDict()
            key = (str(self.store.owner_id), self.store.account_id, id(account), node_id, self.locale)
            now = self.clock()
            entry = cache.get(key)
            if not force and entry is not None and entry["account"] is account and now < entry["expires"]:
                cache.move_to_end(key)
                return deepcopy(entry["schema"])
            # Invalidate before dispatch so failed refresh can never expose old context.
            cache.pop(key, None)
            facts = {}
            try:
                response = account.method("get", f"lots/offerEdit?node={node_id}",
                                          {"accept-language": self.locale}, {}, raise_not_200=True)
                facts.update(_safe_facts({'http_status': response.status_code}))
                if response.status_code != 200:
                    raise NodeFormError(_http_reason(response.status_code))
                final_url = getattr(response, "url", None)
                facts['url_category'] = 'missing'
                if final_url:
                    facts['url_category'] = 'invalid'
                    try:
                        url = urlsplit(final_url)
                    except (ValueError, TypeError):
                        raise NodeFormError('URL_INVALID') from None
                    query = parse_qs(url.query)
                    if url.hostname not in {'funpay.com', 'www.funpay.com'}:
                        facts['url_category'] = 'external'
                        raise NodeFormError('URL_HOST')
                    if re.search(r'/(?:login|account/(?:login|auth))(?:/|$)', url.path):
                        facts['url_category'] = 'auth'
                        raise NodeFormError('URL_AUTH')
                    facts['url_category'] = 'other'
                    if url.path.rstrip('/') not in {'/lots/offerEdit', '/en/lots/offerEdit', '/uk/lots/offerEdit'}:
                        raise NodeFormError('URL_PATH')
                    facts['url_category'] = 'editor'
                    if query.get('node') != [str(node_id)]:
                        raise NodeFormError('URL_NODE')
                    if 'offer' in query:
                        raise NodeFormError('URL_OFFER')
                content = response.content
                if not isinstance(content, bytes):
                    raise NodeFormError('CONTENT_TYPE')
                if len(content) > _MAX_HTML:
                    raise NodeFormError('CONTENT_SIZE')
                try:
                    html = content.decode('utf-8')
                except UnicodeError:
                    raise NodeFormError('CONTENT_ENCODING') from None
                schema = _parse(html, node_id, account_id=account.id)
            except NodeFormError as error:
                raise NodeFormError(error.reason, **{**facts, **error.facts}) from None
            except RequestFailedError as error:
                raise NodeFormError(_http_reason(error.status_code, isinstance(error, UnauthorizedError)),
                                    http_status=error.status_code) from None
            except RequestException:
                raise NodeFormError('REQUEST_TRANSPORT') from None
            except Exception:
                raise NodeFormError('REQUEST_FAILED', **facts) from None
            cache[key] = dict(account=account, expires=self.clock() + _TTL, schema=schema)
            cache.move_to_end(key)
            while len(cache) > _MAX_SCOPES:
                cache.popitem(last=False)
            return deepcopy(schema)
