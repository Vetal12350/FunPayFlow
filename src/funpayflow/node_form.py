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


class NodeFormError(ValueError):
    def __init__(self):
        super().__init__(_UNAVAILABLE)


def _positive(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def _text(value, limit=512, empty=False, normalize=True):
    if not isinstance(value, str) or len(value) > limit:
        raise NodeFormError()
    if any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise NodeFormError()
    if normalize:
        value = " ".join(value.split())
    elif any(char in value for char in "\t\r\n"):
        raise NodeFormError()
    if not empty and not value:
        raise NodeFormError()
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


def _parse(html, node_id, account_id=None):
    if not _positive(node_id) or not isinstance(html, str) or not 0 < len(html) <= _MAX_HTML:
        raise NodeFormError()
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if body is not None and body.has_attr("data-app-data"):
        # Account.get() uses this public user ID to establish authentication.
        # Any other app-data members (including service tokens) stay transient.
        app_data = json.loads(body["data-app-data"])
        user_id = app_data.get("userId")
        if not _positive(user_id) or (account_id is not None and user_id != account_id):
            raise NodeFormError()
    if (soup.select_one('input[type="password"], form[action*="login"], form[action*="account/auth"]')
            or soup.select_one("p.lead, .alert-danger, .error-page")):
        raise NodeFormError()
    forms = soup.select("form.form-offer-editor")
    if len(forms) != 1 or _hidden(forms[0]):
        raise NodeFormError()
    form = forms[0]
    if form.get('action'):
        action = urlsplit(form['action'])
        if (action.query or action.fragment or (action.netloc and action.netloc != 'funpay.com')
                or action.path not in {'/lots/offerSave', 'lots/offerSave', '/en/lots/offerSave', '/uk/lots/offerSave'}):
            raise NodeFormError()
    # Values here only confirm the response context; they never enter schema.
    for name in ("node_id", "node"):
        controls = form.find_all("input", attrs={"name": name})
        if len(controls) > 1 or any(str(control.get("value", "")) != str(node_id) for control in controls):
            raise NodeFormError()
    for name in ("offer_id", "offer"):
        controls = form.find_all("input", attrs={"name": name})
        if len(controls) > 1 or any(str(control.get("value", "")) not in {"", "0"} for control in controls):
            raise NodeFormError()
    fields, names, counts = [], set(), {}
    for control in form.find_all("select"):
        if _hidden(control):
            continue
        group = control.find_parent(class_="form-group")
        if group is None:
            raise NodeFormError()
        name = control.get("name")
        if not name:
            raise NodeFormError()
        if not _field_name(name):
            # Standard content and service controls are intentionally excluded.
            parts = re.findall(r'\[([^\]]+)\]', name) or [name]
            if (any(part.casefold() in _SERVICE_KEYS for part in parts)
                    or (_NAME.fullmatch(name) and parts[0] in _STANDARD)) or name in {
                    "price", "amount", "auto_delivery", "active", "deactivate_after_sale", "query"}:
                continue
            raise NodeFormError()
        if name in names or control.has_attr("multiple") or control.find("select") or control.find_parent("select"):
            raise NodeFormError()
        names.add(name)
        html_id = control.get("id")
        if html_id is not None:
            html_id = _text(html_id, 256, normalize=False)
        labels = [label for label in group.find_all("label") if not _hidden(label)]
        matched = [label for label in labels if html_id and label.get("for") == html_id]
        if not matched:
            matched = [label for label in labels if control in label.descendants]
        if not matched:
            matched = [label for label in labels if not label.has_attr("for")]
        if len(matched) != 1:
            raise NodeFormError()
        label_tag = matched[0]
        label = _text(label_tag.get_text(" ", strip=True), 256)
        display = label.rstrip(" *").strip()
        if not display:
            raise NodeFormError()
        options, values = [], set()
        for option in control.find_all("option"):
            if option.find_parent("select") is not control or option.find("option"):
                raise NodeFormError()
            option_label = _text(option.get_text(" ", strip=True), empty=True)
            value = _text(option.get("value", option_label), empty=True, normalize=False)
            if value in values or (value and not option_label):
                raise NodeFormError()
            values.add(value)
            optgroup = option.find_parent("optgroup")
            disabled = option.has_attr("disabled") or bool(optgroup and optgroup.has_attr("disabled"))
            options.append(dict(value=value, label=option_label, selected=option.has_attr("selected"),
                                disabled=disabled, order=len(options)))
        if not options or len(options) > 1000:
            raise NodeFormError()
        selected = [option for option in options if option["selected"]]
        if len(selected) > 1:
            raise NodeFormError()
        default = selected[0] if selected else None
        size = control.get("size", "1")
        if not re.fullmatch(r"\d{1,4}", size):
            raise NodeFormError()
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
            raise NodeFormError()
    # A recognized editor with standard controls can legitimately have no selects.
    if not fields and not form.select_one('[name="price"], [name="fields[summary][ru]"]'):
        raise NodeFormError()
    safe = [dict(html_name=field["html_name"], kind=field["kind"], label=field["label"],
                 required=field["required"], disabled=field["disabled"], order=field["order"],
                 options=[{key: option[key] for key in ("value", "label", "order", "disabled")}
                          for option in field["options"]]) for field in fields]
    fingerprint = hashlib.sha256(json.dumps(safe, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
    return dict(node_id=node_id, fields=fields, fetched_at=time.time(), fingerprint=fingerprint,
                generation=uuid.uuid4().hex[:12])


def parse_node_form(html, node_id):
    """Parse safe metadata only; never expose server text in an exception."""
    try:
        return _parse(html, node_id)
    except Exception:
        raise NodeFormError() from None


class NodeFormService:
    def __init__(self, client, store, locale="ru", clock=None):
        self.client, self.store, self.locale = client, store, locale
        self.clock = clock or time.monotonic

    def get(self, node_id, force=False):
        if not _positive(node_id) or self.locale not in {"ru", "en", "uk"}:
            raise NodeFormError()
        with self.client._account_lock:
            account = self.client.account
            if account.id != self.store.account_id or getattr(account, "is_initiated", True) is not True:
                raise NodeFormError()
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
            try:
                response = account.method("get", f"lots/offerEdit?node={node_id}",
                                          {"accept-language": self.locale}, {}, raise_not_200=True)
                if response.status_code != 200:
                    raise NodeFormError()
                final_url = getattr(response, "url", None)
                if final_url:
                    url = urlsplit(final_url)
                    query = parse_qs(url.query)
                    if (url.hostname not in {"funpay.com", "www.funpay.com"}
                            or url.path.rstrip("/") not in {"/lots/offerEdit", "/en/lots/offerEdit", "/uk/lots/offerEdit"}
                            or query.get("node") != [str(node_id)] or "offer" in query):
                        raise NodeFormError()
                content = response.content
                if not isinstance(content, bytes) or len(content) > _MAX_HTML:
                    raise NodeFormError()
                schema = _parse(content.decode("utf-8"), node_id, account_id=account.id)
            except Exception:
                raise NodeFormError() from None
            cache[key] = dict(account=account, expires=self.clock() + _TTL, schema=schema)
            cache.move_to_end(key)
            while len(cache) > _MAX_SCOPES:
                cache.popitem(last=False)
            return deepcopy(schema)
