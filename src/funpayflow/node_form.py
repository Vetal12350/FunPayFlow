"""Read-only, bounded snapshots of visible FunPay create-form select fields.

Uses the runtime's existing Account and lock. No raw HTML or hidden values leave
the parser, and unsuccessful refreshes invalidate the previous generation.
"""

from collections import OrderedDict
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
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
_FORM_EVIDENCE = {
    'source': ('unknown', 'response', 'cache'),
    'controls': ('unknown', 'recognized'),
    'new_form_context': ('unknown', 'candidate'),
    'csrf_input_presence': ('ambiguous', 'present', 'absent'),
    'manual_delivery': ('unverified',),
    'submit_contract': ('unverified',),
    'response_id': ('unverified',),
    'evidence_blocker': ('unknown', 'none', 'cache_unavailable', 'response_url',
                         'duplicate_attributes', 'document_markup', 'document_tag_mismatch',
                         'document_unclosed_tags', 'document_self_closing', 'document_text', 'editor_markup',
                         'control_conflict', 'unsupported_controls', 'ambiguous_context'),
}


def safe_form_evidence(value=None):
    """Fixed enums only, revalidated at the UI boundary; never schema metadata."""
    unknown = {key: choices[0] for key, choices in _FORM_EVIDENCE.items()}
    if (type(value) is not dict or value.keys() != _FORM_EVIDENCE.keys()
            or any(type(value[key]) is not str or value[key] not in choices
                   for key, choices in _FORM_EVIDENCE.items())):
        return unknown
    # Reject inconsistent objects wholesale, including invented positive claims.
    if value['source'] != 'response':
        expected = dict(unknown, source=value['source'], evidence_blocker=(
            'cache_unavailable' if value['source'] == 'cache' else 'unknown'))
        return expected if value == expected else unknown
    if value['controls'] == 'recognized':
        if value['evidence_blocker'] != 'none' or value['csrf_input_presence'] == 'ambiguous':
            return unknown
    elif (value['evidence_blocker'] in {'none', 'cache_unavailable'}
          or value['new_form_context'] != 'unknown' or value['csrf_input_presence'] != 'ambiguous'):
        return unknown
    return dict(value)


class _EvidenceMarkup(HTMLParser):
    """Reject repaired/ambiguous markup for evidence, without changing _parse.

    Even legal omitted end tags are unknown in this deliberately narrow path.
    Diagnoses describe strict-stack observations, not HTML conformance or cause.
    Only tag names are stacked, never attribute names/values or response text.
    """
    _void = frozenset('area base br col embed hr img input link meta param source track wbr'.split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.valid, self.stack = True, []
        self.duplicate_attributes = False
        self.tag_mismatch = self.self_closing = self.ambiguous_text = False

    def handle_starttag(self, tag, attrs):
        names = [name for name, _ in attrs]
        if len(names) != len(set(names)):
            self.valid = False
            self.duplicate_attributes = True
        if tag not in self._void:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.valid = False  # HTML does not self-close ordinary elements.
            self.self_closing = True

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.valid = False
            self.tag_mismatch = True
        else:
            self.stack.pop()

    def handle_data(self, data):
        if '<' in data:
            self.valid = False
            self.ambiguous_text = True


def _form_evidence(html, soup, form, schema):
    """Conservative structural observation, never a write/authentication verdict.

    The ordinary parser has already validated the response. This narrower
    allowlist deliberately reports unknown for controls it does not understand.
    Hidden values/names and the DOM remain local to parsing, including failures.
    """
    result = safe_form_evidence()
    result['source'] = 'response'
    markup = _EvidenceMarkup()
    markup.feed(html)
    markup.close()
    # Fixed precedence: URL (in _get), duplicate attributes, document markup,
    # editor markup, control conflicts, unsupported controls, context aliases.
    if markup.duplicate_attributes:
        return dict(result, evidence_blocker='duplicate_attributes')
    # Fixed sub-priority, independent of encounter order. No repair/pop on a
    # mismatch: optional end tags still fail closed, including outside the form.
    for blocked, reason in (
            (markup.self_closing, 'document_self_closing'),
            (markup.ambiguous_text, 'document_text'),
            (markup.tag_mismatch, 'document_tag_mismatch'),
            (bool(markup.stack), 'document_unclosed_tags'),
            (not markup.valid, 'document_markup')):
        if blocked:
            return dict(result, evidence_blocker=reason)
    if (form.find_parent('form') or form.find('form')
            or form.select_one('script, template, noscript, iframe, object, output, keygen')
            or soup.find('base')
            or any(tag.has_attr('form') for tag in soup.find_all(['input', 'select', 'textarea', 'button']))
            or any(key.startswith('on') for tag in [form, *form.find_all(True)] for key in tag.attrs)):
        return dict(result, evidence_blocker='editor_markup')
    inputs = {
        'node_id': {'hidden'}, 'node': {'hidden'},
        'offer_id': {'hidden'}, 'offer': {'hidden'},
        'csrf_token': {'hidden'},
        'price': {'text', 'number'}, 'amount': {'text', 'number'},
        'active': {'checkbox'}, 'auto_delivery': {'checkbox'},
        'deactivate_after_sale': {'checkbox'},
    }
    texts = {f'fields[{field}][{locale}]' for field in ('summary', 'desc', 'payment_msg')
             for locale in ('ru', 'en')}
    selects = {field['html_name'] for field in schema['fields']}
    controls = {}
    blockers = set()
    for control in form.find_all(['input', 'select', 'textarea', 'button']):
        name = control.get('name')
        if (control.find_parent(['input', 'select', 'textarea', 'button'])
                or any(key in control.attrs for key in ('formaction', 'formmethod', 'formenctype', 'formtarget'))):
            blockers.add('control_conflict')
        if _hidden(control) or _disabled(control):
            blockers.add('unsupported_controls')
        if control.name == 'button' and name is None and control.get('type', 'submit') in {'button', 'submit'}:
            continue
        if type(name) is not str or not name or name in controls:
            blockers.add('control_conflict')
            continue
        controls[name] = control
        if control.name == 'select' and name in selects and is_safe_node_binding_name(name):
            continue
        if control.name == 'textarea' and name in texts:
            continue
        if control.name == 'input':
            kind = control.get('type', 'text')
            if name in texts and kind == 'text':
                continue
            if (name in inputs and kind in inputs[name]
                    and (kind != 'hidden' or type(control.get('value')) is str)):
                continue
        blockers.add('unsupported_controls')
    for blocker in ('control_conflict', 'unsupported_controls'):
        if blocker in blockers:
            return dict(result, evidence_blocker=blocker)
    nodes = [controls[key] for key in ('node_id', 'node') if key in controls]
    offers = [controls[key] for key in ('offer_id', 'offer') if key in controls]
    if len(nodes) > 1 or len(offers) > 1:
        return dict(result, evidence_blocker='ambiguous_context')
    result.update(controls='recognized', evidence_blocker='none', csrf_input_presence=(
        'present' if 'csrf_token' in controls else 'absent'))
    # Matching action + empty/zero identity only suggest NEW, never prove it.
    action = urlsplit(form.get('action', ''))
    if (len(nodes) == len(offers) == 1 and form.get('method', '').lower() == 'post'
            and action.path in {'/lots/offerSave', '/en/lots/offerSave', '/uk/lots/offerSave'}
            and (not action.netloc or action.netloc == 'funpay.com')
            and (not action.scheme or (action.scheme == 'https' and action.netloc == 'funpay.com'))
            and not action.query and not action.fragment):
        result['new_form_context'] = 'candidate'
    return safe_form_evidence(result)


_PROOF_PREDICATES = (
    'editor_url', 'document_markup', 'form_subtree', 'submission', 'blank_identity',
    'new_semantics', 'csrf_presence', 'manual_control', 'manual_delivery', 'node_identity',
    'currency_control', 'currency',
    'control_inventory', 'select_names', 'response_id', 'no_retry_post',
)
_PROOF_COUNTS = ('control_count', 'hidden_count', 'duplicate_count', 'unknown_count')
_PROOF_STATES = frozenset({'verified', 'absent', 'ambiguous', 'unverified'})


def safe_contract_proof(value=None):
    """Bounded observations only. No GET can establish a publishing capability."""
    empty = dict(availability='unavailable', publish_ready=False,
                 **dict.fromkeys(_PROOF_PREDICATES, 'unverified'),
                 **dict.fromkeys(_PROOF_COUNTS, 0))
    if (type(value) is not dict or value.keys() != empty.keys()
            or type(value['availability']) is not str
            or value['availability'] not in ('unavailable', 'response') or value['publish_ready'] is not False
            or any(type(value[k]) is not str or value[k] not in _PROOF_STATES for k in _PROOF_PREDICATES)
            or any(type(value[k]) is not int or not 0 <= value[k] <= 1001 for k in _PROOF_COUNTS)
            or any(value[k] != 'unverified' for k in (
                'new_semantics', 'manual_delivery', 'currency', 'response_id', 'no_retry_post'))
            or (value['availability'] == 'unavailable' and value != empty)):
        return empty
    return dict(value)


class _EditorMarkup(_EvidenceMarkup):
    """Strict original subtree, never a BeautifulSoup-repaired serialization."""
    def __init__(self):
        super().__init__()
        self.active = self.complete = False
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'form' and 'form-offer-editor' in (dict(attrs).get('class') or '').split():
            self.count += 1
            self.active = True
        if self.active:
            if tag == 'form' and self.stack:
                self.valid = False
            super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.active:
            super().handle_endtag(tag)
            if tag == 'form':
                self.active = False
                self.complete = True

    def handle_startendtag(self, tag, attrs):
        if self.active or tag == 'form':
            super().handle_startendtag(tag, attrs)

    def handle_data(self, data):
        if self.active:
            super().handle_data(data)


def prove_new_form_contract(html, response_url, node_id=986):
    """Pure offline proof of observations for node 986, not NEW wire semantics.

    The three names come from the ordinary safe SELECT parser, never a guessed
    live mapping. Blank identity is literal presence, not a NEW authorization.
    All DOM/values are transient; the result contains only fixed enums/counts.
    """
    result = safe_contract_proof()
    if node_id != 986 or type(node_id) is not int:
        return result
    result['availability'] = 'response'
    try:
        url = urlsplit(response_url)
        result['editor_url'] = 'verified' if (
            type(response_url) is str and not re.search(r'[\s\\\x00-\x1f\x7f]', response_url)
            and url.scheme == 'https' and url.netloc in {'funpay.com', 'www.funpay.com'}
            and url.path in {'/lots/offerEdit', '/en/lots/offerEdit', '/uk/lots/offerEdit'}
            and not url.fragment
            and parse_qs(url.query, keep_blank_values=True) == {'node': ['986']}) else 'ambiguous'
        # Preserve all ordinary parser rejections, including EDIT/nonmatching node.
        schema = _parse(html, node_id)
        soup = BeautifulSoup(html, 'html.parser')
        form = soup.select_one('form.form-offer-editor')
        document, subtree = _EvidenceMarkup(), _EditorMarkup()
        for markup in (document, subtree):
            markup.feed(html)
            markup.close()
        result['document_markup'] = 'verified' if document.valid and not document.stack else 'ambiguous'
        valid = (subtree.valid and not subtree.stack and subtree.complete and subtree.count == 1
                 and not form.find_parent('form') and not form.find('form')
                 and not _hidden(form) and not _disabled(form)
                 and not soup.find('base')
                 and not form.select_one('script, style, link, meta, template, noscript, iframe, object, '
                                         'output, keygen, svg, math')
                 and not any(tag.has_attr('form') for tag in soup.find_all(['input', 'select', 'textarea', 'button']))
                 and not any(k.startswith('on') for tag in [form, *form.find_all(True)] for k in tag.attrs))
        result['form_subtree'] = 'verified' if valid else 'ambiguous'
        if not valid:
            return safe_contract_proof(result)
        controls = form.find_all(['input', 'select', 'textarea', 'button'])
        names = [c.get('name') for c in controls]
        for key, name in (('manual_control', 'auto_delivery'), ('currency_control', 'currency')):
            count = names.count(name)
            result[key] = 'absent' if not count else 'unverified' if count == 1 else 'ambiguous'
        result['control_count'] = min(len(controls), 1001)
        result['hidden_count'] = min(sum(c.get('type') == 'hidden' or _hidden(c) for c in controls), 1001)
        result['duplicate_count'] = min(len(names) - len(set(names)), 1001)
        category_names = {f['html_name'] for f in schema['fields']}
        inputs = {'node_id': {'hidden'}, 'offer_id': {'hidden'}, 'csrf_token': {'hidden'},
                  'price': {'text', 'number'}, 'amount': {'text', 'number'},
                  'auto_delivery': {'checkbox'}, 'active': {'checkbox'},
                  'deactivate_after_sale': {'checkbox'}}
        texts = {f'fields[{field}][{locale}]' for field in ('summary', 'desc', 'payment_msg')
                 for locale in ('ru', 'en')}

        def recognized(c):
            name = c.get('name')
            if (_hidden(c) or _disabled(c) or c.find_parent(['input', 'select', 'textarea', 'button'])
                    or any(k in c.attrs for k in ('formaction', 'formmethod', 'formenctype', 'formtarget'))):
                return False
            return ((c.name == 'select' and name in category_names
                     and not c.find(lambda tag: tag.name not in {'option', 'optgroup'})
                     and all(not option.find(True) for option in c.find_all('option')))
                    or (c.name == 'textarea' and name in texts)
                    or (c.name == 'input' and c.get('type', 'text') in inputs.get(name, set()))
                    or (c.name == 'input' and name in texts and c.get('type', 'text') == 'text')
                    or (c.name == 'button' and name is None and c.get('type', 'submit') in {'button', 'submit'}))
        result['unknown_count'] = min(sum(not recognized(c) for c in controls), 1001)
        clean = not result['duplicate_count'] and not result['unknown_count']
        result['control_inventory'] = 'verified' if clean else 'ambiguous'
        action = urlsplit(form.get('action', ''))
        result['submission'] = 'verified' if (
            result['editor_url'] == 'verified'
            and not re.search(r'[\s\\\x00-\x1f\x7f]', form.get('action', ''))
            and action.path in {'/lots/offerSave', '/en/lots/offerSave', '/uk/lots/offerSave'}
            and (not action.scheme or action.scheme == 'https')
            and (not action.netloc or action.netloc == 'funpay.com')
            and (not action.scheme or action.netloc == 'funpay.com')
            and not action.query and not action.fragment
            and form.get('method', '').lower() == 'post'
            and form.get('enctype', 'application/x-www-form-urlencoded').lower() == 'application/x-www-form-urlencoded'
            and not any(c.has_attr(k) for c in controls for k in (
                'formaction', 'formmethod', 'formenctype', 'formtarget'))
        ) else 'ambiguous'
        for key, name, expected in (('blank_identity', 'offer_id', ''),
                                    ('node_identity', 'node_id', '986'),
                                    ('csrf_presence', 'csrf_token', None)):
            found = [c for c in controls if c.get('name') == name]
            result[key] = 'absent' if not found else 'ambiguous'
            if (clean and len(found) == 1 and found[0].name == 'input'
                    and found[0].get('type') == 'hidden'
                    and type(found[0].get('value')) is str
                    and (bool(found[0]['value'].strip()) if expected is None else found[0]['value'] == expected)):
                result[key] = 'verified'
        result['select_names'] = 'verified' if (
            clean and len(category_names) == 3 and all(is_safe_node_binding_name(n) for n in category_names)
            and len(form.find_all('select')) == 3) else 'ambiguous'
    except Exception:
        # Never expose input or exception messages, including malformed URLs.
        return dict(safe_contract_proof(), availability='response', editor_url=result['editor_url'],
                    form_subtree='ambiguous')
    return safe_contract_proof(result)


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


def is_safe_plain_field_name(name):
    """Whether a plain SELECT name is a retained category candidate."""
    return bool(isinstance(name, str) and _PLAIN_NAME.fullmatch(name)
                and not name.casefold().startswith('fields')
                and name.casefold() not in _PLAIN_SERVICE | _SERVICE_KEYS
                and not _SENSITIVE_PLAIN.search(name))


def is_safe_node_binding_name(name):
    """Accept category identities only; schema presence is checked separately.

    Bracket names retain their grammar and service checks, with the same
    sensitive/service exclusions as plain bindings applied to each component.
    """
    if is_safe_plain_field_name(name):
        return True
    if not _field_name(name):
        return False
    parts = re.findall(r'\[([^\]]+)\]', name)
    return not (_SENSITIVE_PLAIN.search(name)
                or any(part.casefold() in _PLAIN_SERVICE for part in parts))


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


def _parse_impl(html, node_id, account_id, facts, evidence=None):
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
            if not is_safe_plain_field_name(name):
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
    schema = dict(node_id=node_id, fields=fields, fetched_at=time.time(), fingerprint=fingerprint,
                  generation=uuid.uuid4().hex[:12])
    if evidence is not None:
        evidence.update(_form_evidence(html, soup, form, schema))
    return schema


def _parse(html, node_id, account_id=None, *, evidence=None):
    facts = {}
    try:
        return _parse_impl(html, node_id, account_id, facts, evidence)
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
        return self._read(node_id, force=force)

    def get_with_evidence(self, node_id, *, contract_proof=None):
        """Same cached GET, plus transient enums; no force, retry or extra read.

        Cache hits intentionally carry no structural evidence. A response means
        obtained by this call, not proof of freshness, NEW or a bounded request.
        """
        if type(contract_proof) is dict:
            contract_proof.clear()
            contract_proof.update(safe_contract_proof())
        return self._read(node_id, include_evidence=True, contract_proof=contract_proof)

    def _read(self, node_id, force=False, include_evidence=False, contract_proof=None):
        try:
            return self._get(node_id, force=force, include_evidence=include_evidence,
                             contract_proof=contract_proof)
        except NodeFormError as error:
            logger.warning('NodeForm failure ' + json.dumps(error.diagnostic, sort_keys=True))
            raise NodeFormError(error.reason, **error.facts) from None
        except Exception:
            logger.warning('NodeForm failure ' + json.dumps({'reason': 'UNEXPECTED'}))
            raise NodeFormError('UNEXPECTED') from None

    def _get(self, node_id, force=False, include_evidence=False, contract_proof=None):
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
                if include_evidence:
                    return deepcopy(entry['schema']), dict(
                        safe_form_evidence(), source='cache', evidence_blocker='cache_unavailable')
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
                evidence = {} if include_evidence else None
                schema = _parse(html, node_id, account_id=account.id, evidence=evidence)
                if include_evidence and (not final_url or url.scheme != 'https'
                        or url.netloc not in {'funpay.com', 'www.funpay.com'} or url.fragment
                        or parse_qs(url.query, keep_blank_values=True) != {'node': [str(node_id)]}):
                    evidence = dict(safe_form_evidence(), source='response', evidence_blocker='response_url')
                if include_evidence and type(contract_proof) is dict:
                    contract_proof.update(prove_new_form_contract(html, final_url, node_id))
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
            if include_evidence:
                return deepcopy(schema), safe_form_evidence(evidence)
            return deepcopy(schema)
