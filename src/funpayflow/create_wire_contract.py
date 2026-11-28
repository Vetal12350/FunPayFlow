"""NEW-lot offerSave exchange for the draft's own node: one owner-confirmed POST.

The live profile (`_fresh_contract`/`read_live_form`) mirrors FunPayAPI 1.1.0
get_lot_fields + save_lot on ONE fresh NEW form; lots are created inactive with
manual delivery. `assemble_new_request` is the older synthetic test profile.
Envelopes contain transient secrets: never serialize them or persist their body.
"""
from dataclasses import dataclass, field
from importlib.metadata import version
import math
import json
import threading
import time
from typing import Literal

import re

from bs4 import BeautifulSoup
import requests
from FunPayAPI import Account

from . import node_form
from .lot_publisher import (FORM_TTL, TEXT_FIELDS, FormContext, PreparedLot, PublisherError,
                            SubmitReceipt, require)
from .node_form import is_safe_node_binding_name
from .runtime_control import automatic_action_gate



def live_url(node_id):
    return f'https://funpay.com/lots/offerEdit?node={node_id}'


LIVE_PROVENANCE = 'live-v1'
# Filled from the owner's draft; everything else comes from the form itself.
OWNER_FIELDS = frozenset({*TEXT_FIELDS.values(), 'price', 'amount'})
# Never sent: unchecked active => inactive lot; no auto_delivery/secrets =>
# manual delivery; currency is verified, not set.
NEVER_SENT = frozenset({'active', 'auto_delivery', 'deactivate_after_sale', 'secrets', 'currency'})
# Visible fields the bot knows how to handle. Any other visible field (another
# node may ask e.g. a server) would be sent empty, so the form is refused.
KNOWN_VISIBLE = frozenset({*OWNER_FIELDS, *NEVER_SENT, 'location',
                           'fields[payment_msg][ru]', 'fields[payment_msg][en]'})
_CONTROL_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,40}(?:\[[A-Za-z0-9_]{0,40}\]){0,3}')
_CURRENCY_TOKENS = {'₽': 'RUB', 'руб': 'RUB', 'руб.': 'RUB', 'RUB': 'RUB',
                    '$': 'USD', 'USD': 'USD', '€': 'EUR', 'EUR': 'EUR'}
# Structure only. Identity, CSRF and selects are re-checked below without the
# old allowlist: real forms carry extra site controls (e.g. form_created_at).
_STRUCTURE = ('editor_url', 'document_markup', 'form_subtree', 'submission')


@dataclass(frozen=True, repr=False)
class NewFormFacts:
    provenance: str
    url: str
    action: str
    method: str
    encoding: str
    fetched_at: float
    node_id: int
    fingerprint: str
    currency: str
    manual_mode: str
    csrf: str
    controls: tuple[tuple[str, str], ...]
    selected_options: tuple[tuple[str, str], ...]


@dataclass(frozen=True, repr=False)
class RequestEnvelope:
    service: tuple[tuple[str, str], ...]
    business: tuple[tuple[str, str], ...]
    path: Literal['lots/offerSave'] = field(default='lots/offerSave', init=False)
    method: Literal['POST'] = field(default='POST', init=False)
    encoding: Literal['application/x-www-form-urlencoded'] = field(
        default='application/x-www-form-urlencoded', init=False)
    allow_redirects: Literal[False] = field(default=False, init=False)
    max_attempts: Literal[1] = field(default=1, init=False)


class NewOfferTransport:
    """Bounded dispatch, unavailable until a runtime live profile is verified.

    fpx proves the endpoint/headers, but neither currency/manual semantics nor
    an ID receipt. No current envelope can pass the live contract gate.
    Telegram/LotPublisher integration preserves the permanent claim and gates.
    Neither a synthetic envelope nor a public proof dict can enable dispatch.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._consumed = False
        self._claimed = False
        self._live = None
        # Sanitized FunPay error text from the single response, for the owner only.
        self.server_error = None

    def _fresh_contract(self, publisher, review):
        """ONE fresh authenticated NEW GET -> prepared lot, FormContext, envelope.

        Mirrors FunPayAPI 1.1.0 get_lot_fields/save_lot: the form's own controls
        are submitted unchanged, only owner business fields are filled in. The
        lot is created INACTIVE with manual delivery (active/auto_delivery are
        never sent). Nothing raw is stored beyond this transport instance.
        """
        self._live = None
        try:
            with publisher.client._account_lock:
                account = publisher.client.account
                publisher._scope_gate(account, review)
                try:
                    html = node_form._fresh_new_editor(account, review.node_id).decode('utf-8')
                except Exception:
                    raise PublisherError('CONTRACT_UNVERIFIED') from None
                now = publisher.clock()
                schema, service = read_live_form(
                    html, fingerprint=review.fingerprint, account_id=review.account_id,
                    selections=tuple((n, v) for n, v, *_ in review.selections),
                    currency=review.currency, node_id=review.node_id)
                schema['fetched_at'] = now
                form = FormContext(review.owner_id, review.account_id, review.account_instance,
                                   schema, review.currency, LIVE_PROVENANCE)
                prepared = publisher.prepare(review.draft_id, review.revision, form,
                                             price=review.price, currency=review.currency)
                envelope = RequestEnvelope(service, prepared.business)
                self._live = (envelope, prepared.fetched_at, publisher.clock)
                return prepared, form, envelope
        except PublisherError:
            raise
        except Exception:
            raise PublisherError('CONTRACT_UNVERIFIED') from None

    def submit_once(self, publisher, prepared, form, envelope):
        armed = getattr(publisher, '_dispatch_prepared', None)
        publisher._dispatch_prepared = None
        require(armed is prepared, 'CONTRACT_UNVERIFIED')
        return self._exchange(publisher, prepared, form, envelope)

    def _require_live_contract(self, envelope):
        # Only the envelope built by THIS instance's own fresh GET passes, and
        # only while that form is fresh. Never a provenance string, a synthetic
        # envelope, another transport's envelope or a green GET matrix.
        live = getattr(self, '_live', None)
        require(live is not None and type(envelope) is RequestEnvelope
                and envelope is live[0], 'CONTRACT_UNVERIFIED')
        age = live[2]() - live[1]
        require(math.isfinite(age) and 0 <= age <= FORM_TTL, 'FORM_STALE')

    def _exchange(self, publisher, prepared, form, envelope):
        """Private test seam; the permanent claim and late gates still apply."""
        with publisher.client._account_lock, automatic_action_gate() as allowed:
            require(allowed, 'GATE')
            account = publisher.client.account
            publisher._scope_gate(account, prepared)
            require(publisher.outcome(prepared.draft_id) is None, 'ALREADY_CLAIMED')
            self._require_live_contract(envelope)
            require(publisher.prepare(prepared.draft_id, prepared.revision, form,
                price=dict(prepared.business)['price'], currency=prepared.currency) == prepared,
                'REVISION')
            require(type(envelope) is RequestEnvelope and envelope.business == prepared.business,
                    'SCHEMA')
            publisher._scope_gate(account, prepared)
            publisher._claim(prepared, form)
            self._claimed = True
            require(publisher.prepare(prepared.draft_id, prepared.revision, form,
                price=dict(prepared.business)['price'], currency=prepared.currency) == prepared,
                'REVISION')
            publisher._scope_gate(account, prepared)
            return self.__send(account, envelope)

    def __send(self, account, envelope):
        """Exactly one fixed-origin exchange; even HTTP 200 proves no new ID.

        Only mocked tests can reach this seam currently. It does not authorize publication
        or replace the permanent SQLite claim. Never log/return response bytes.
        """
        response = None
        try:
            with self._lock:
                require(not self._consumed, 'ALREADY_CLAIMED')
                # Consume before any possible dispatch, including failures.
                self._consumed = True
            require(type(account) is Account and version('FunPayAPI') == '1.1.0',
                    'TRANSPORT_POLICY')
            require(account.is_initiated is True, 'SCOPE')
            require(type(envelope) is RequestEnvelope
                    and envelope.path == 'lots/offerSave' and envelope.method == 'POST'
                    and envelope.encoding == 'application/x-www-form-urlencoded'
                    and envelope.allow_redirects is False and envelope.max_attempts == 1,
                    'TRANSPORT_POLICY')
            # Currency is verified against the form, never sent: FunPayAPI's
            # save_lot has no currency field; the price is in the form's currency.
            pairs = envelope.service + tuple(p for p in envelope.business if p[0] != 'currency')
            require(type(envelope.service) is tuple and type(envelope.business) is tuple
                    and 0 < len(pairs) <= 64
                    and all(type(p) is tuple and len(p) == 2
                            and all(type(v) is str and len(v) <= 10000 for v in p) for p in pairs)
                    and len(dict(pairs)) == len(pairs)
                    and sum(len(v.encode('utf-8')) for p in pairs for v in p) <= 200_000,
                    'SCHEMA')
            node = dict(envelope.service).get('node_id', '')
            require(type(node) is str and node.isascii() and node.isdigit()
                    and 0 < int(node) < 2**63, 'SCOPE')
            headers = {
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': live_url(int(node)),
                'Origin': 'https://funpay.com',
                'cookie': f'golden_key={account.golden_key}',
            }
            if account.phpsessid:
                headers['cookie'] += f'; PHPSESSID={account.phpsessid}'
            if account.user_agent:
                headers['user-agent'] = account.user_agent
            # requests.post uses the existing runtime cookie wrapper and the
            # default zero-retry adapter, as does Account.method in 1.1.0.
            response = requests.post('https://funpay.com/lots/offerSave', data=pairs,
                headers=headers, proxies=account.proxy or {}, timeout=(5, 10),
                allow_redirects=False, verify=True, stream=True)
            require(type(response.status_code) is int and response.status_code == 200
                    and not response.history
                    and response.url == 'https://funpay.com/lots/offerSave', 'TRANSPORT_POLICY')
            chunks, size, started = [], 0, time.monotonic()
            for chunk in response.iter_content(65536):
                size += len(chunk)
                require(size <= 262_144 and time.monotonic() - started <= 15,
                        'TRANSPORT_UNKNOWN')
                chunks.append(chunk)
            # An error (or unknown receipt shape) cannot become success merely
            # because an unrelated concurrent lot appears in the inventory.
            receipt = json.loads(b''.join(chunks).decode('utf-8'))
            require(type(receipt) is dict, 'TRANSPORT_UNKNOWN')
            # Same rule as FunPayAPI save_lot: an explicit error rejects. A
            # missing error is NOT success either: only the independent
            # inventory read-back can confirm a new lot.
            if receipt.get('error') or receipt.get('errors'):
                self.server_error = _server_error_text(receipt)
                raise PublisherError('TRANSPORT_UNKNOWN')
            # Do not infer an ID from JSON keys, URLs, HTML links or a boolean.
            return SubmitReceipt(None)
        except PublisherError:
            raise
        except Exception:
            raise PublisherError('TRANSPORT_UNKNOWN') from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


def assemble_new_request(prepared: PreparedLot, facts: NewFormFacts, *, now: float) -> RequestEnvelope:
    """Assemble only the explicit synthetic-v1 profile; fixed errors omit inputs.

    This is data, not a submission capability. Rebuilding an envelope grants no
    second attempt: the publisher's permanent claim remains the authority.
    No response ID parser is supplied; raw success/redirect responses are UNKNOWN.
    """
    try:
        require(type(prepared) is PreparedLot and type(facts) is NewFormFacts
                and facts.provenance == 'synthetic-new-v1', 'CONTRACT_UNVERIFIED')
        require(type(facts.node_id) is int and facts.node_id == 986
                and facts.url == 'https://funpay.com/lots/offerEdit?node=986'
                and facts.action == 'https://funpay.com/lots/offerSave', 'SCOPE')
        require(facts.method == 'POST'
                and facts.encoding == 'application/x-www-form-urlencoded', 'TRANSPORT_POLICY')
        for timestamp in (facts.fetched_at, prepared.fetched_at):
            require(type(timestamp) in (int, float) and type(now) in (int, float)
                    and math.isfinite(timestamp) and math.isfinite(now)
                    and 0 <= now - timestamp <= FORM_TTL, 'FORM_STALE')
        require(facts.fetched_at == prepared.fetched_at
                and facts.fingerprint == prepared.fingerprint, 'SCHEMA')
        # Tuple-only inputs ensure the frozen envelope cannot retain mutable data.
        for pairs in (facts.controls, facts.selected_options, prepared.business):
            require(type(pairs) is tuple and len(pairs) <= 32
                    and all(type(p) is tuple and len(p) == 2
                            and all(type(v) is str and len(v) <= 10000 for v in p)
                            for p in pairs)
                    and len(dict(pairs)) == len(pairs), 'SCHEMA')
        controls, options, business = map(dict, (
            facts.controls, facts.selected_options, prepared.business))
        require(bool(options) and all(is_safe_node_binding_name(k) for k in options), 'SCHEMA')
        require(set(business) == {*TEXT_FIELDS.values(), 'price', 'amount', 'currency', *options}
                and all(business[k] == v for k, v in options.items()), 'SCHEMA')
        service_names = {'offer_id', 'node_id', 'csrf_token', 'auto_delivery',
                         'location', 'active', 'deactivate_after_sale'}
        require(set(controls) == service_names | set(business), 'SCHEMA')
        require(controls['offer_id'] == '', 'NEW_IDENTITY')
        require(controls['node_id'] == '986', 'SCOPE')
        require(type(facts.csrf) is str and 0 < len(facts.csrf) <= 4096
                and bool(facts.csrf.strip()) and controls['csrf_token'] == facts.csrf, 'CSRF')
        require(facts.manual_mode == controls['auto_delivery'] == 'SYNTHETIC_MANUAL', 'MANUAL_MODE')
        require(facts.currency == prepared.currency == controls['currency'] == business['currency']
                and facts.currency in {'USD', 'EUR', 'RUB'}, 'PRICE')
        require(controls['location'] == 'trade' and controls['active'] == ''
                and controls['deactivate_after_sale'] == '', 'CONTRACT_UNVERIFIED')
        require(controls['amount'] == business['amount'] == '1', 'QUANTITY')
        require(all(controls[k] == v for k, v in options.items())
                and all(controls[k] == '' for k in (*TEXT_FIELDS.values(), 'price')), 'SCHEMA')
        return RequestEnvelope(tuple((k, v) for k, v in facts.controls if k in service_names),
                               prepared.business)
    except PublisherError:
        raise
    except Exception:
        raise PublisherError('CONTRACT_UNVERIFIED') from None


def _server_error_text(receipt):
    """Short plain text of FunPay's own error message; never HTML or our input."""
    parts = []
    for key in ('error', 'msg', 'message'):
        value = receipt.get(key)
        if type(value) is str:
            parts.append(value)
    errors = receipt.get('errors')
    if type(errors) is dict:
        parts += [v for v in errors.values() if type(v) is str]
    elif type(errors) is list:
        parts += [v for v in errors if type(v) is str]
    text = ' '.join(BeautifulSoup(' '.join(parts), 'html.parser').get_text(' ').split())
    return text[:300] or None


def _control_value(control):
    """Value a browser would submit, or None when the control is not submitted."""
    if control.name == 'textarea':
        return control.get_text()
    if control.name == 'select':
        options = [o for o in control.find_all('option') if not o.has_attr('disabled')]
        selected = [o for o in options if o.has_attr('selected')]
        require(len(selected) <= 1 and options, 'SCHEMA')
        chosen = (selected or options)[0]
        return chosen.get('value', chosen.get_text())
    kind = control.get('type', 'text').lower()
    if kind in {'checkbox', 'radio'}:
        return control.get('value', 'on') if control.has_attr('checked') else None
    if kind in {'submit', 'button', 'reset', 'image'}:
        return None
    require(kind != 'file', 'SCHEMA')
    return control.get('value', '')


def _price_currency(form, price):
    """Currency markers attached to the price control (Cardinal reads the
    span.form-control-feedback next to it). Returns the set of currencies."""
    found = set()
    holders = [price.parent] if price.parent is not None else []
    if price.get('id'):
        holders += form.find_all('label', attrs={'for': price['id']})
    for holder in holders:
        for tag in [holder, *holder.find_all(True)]:
            classes = tag.get('class', [])
            if not ({'form-control-feedback', 'input-group-addon', 'input-group-text'} & set(classes)
                    or tag.name == 'label'):
                continue
            text = ' '.join(tag.get_text(' ', strip=True).split())
            text = re.sub(r'^(?:Цена|Стоимость|Price)\s*[:,]?\s*', '', text, flags=re.I).strip(' ()[]:,')
            if text in _CURRENCY_TOKENS:
                found.add(_CURRENCY_TOKENS[text])
    return found


def read_live_form(html, *, fingerprint, account_id, selections, currency, node_id):
    """Pure: validate one fresh NEW form and return (schema, service controls).

    Service controls are the form's own submitted values (hidden fields, CSRF,
    node/offer identity, any other site fields) minus owner business fields and
    NEVER_SENT, plus location=trade as FunPayAPI save_lot does.
    """
    require(currency in {'RUB', 'USD', 'EUR'}, 'PRICE')
    require(type(node_id) is int and 0 < node_id < 2**63, 'SCOPE')
    proof = node_form.prove_new_form_contract(html, live_url(node_id), node_id)
    require(all(proof.get(key) == 'verified' for key in _STRUCTURE), 'CONTRACT_UNVERIFIED')
    try:
        schema = node_form._parse(html, node_id, account_id=account_id)
    except node_form.NodeFormError:
        raise PublisherError('SCHEMA') from None
    fields = schema['fields']
    require(schema['fingerprint'] == fingerprint and len(fields) == len(selections) > 0
            and all(not f['disabled'] and f['html_name'] == name
                    and any(o['value'] == value and not o['disabled'] for o in f['options'])
                    for f, (name, value) in zip(fields, selections)), 'SCHEMA')
    soup = BeautifulSoup(html, 'html.parser')
    identity = json.loads(soup.find('body')['data-app-data']).get('userId')
    require(type(identity) is int and identity == account_id, 'SCOPE')
    forms = soup.select('form.form-offer-editor')
    require(len(forms) == 1, 'CONTRACT_UNVERIFIED')
    form = forms[0]
    pairs, prices = [], []
    for control in form.find_all(['input', 'select', 'textarea']):
        name = control.get('name')
        if not name or control.has_attr('disabled'):
            continue
        require(_CONTROL_NAME.fullmatch(name) is not None, 'SCHEMA')
        if name == 'price':
            prices.append(control)
        value = _control_value(control)
        if value is None:
            continue
        require(type(value) is str and len(value) <= 10000, 'SCHEMA')
        pairs.append((name, value))
    names = [n for n, _ in pairs]
    require(len(names) == len(set(names)) and len(names) <= 48, 'SCHEMA')
    select_names = {f['html_name'] for f in fields}
    controls = [c for c in form.find_all(['input', 'select', 'textarea'])
                if c.get('name') and not c.has_attr('disabled')]
    hidden = {c['name'] for c in controls
              if c.name == 'input' and c.get('type', '').lower() == 'hidden'}
    visible = {c['name'] for c in controls} - hidden
    require(visible <= KNOWN_VISIBLE | select_names, 'SCHEMA')
    # A hidden lot parameter (e.g. filled by a script) would also go out empty.
    require(not {n for n in hidden if n.startswith('fields[')} - select_names - {'fields[images]'},
            'SCHEMA')
    values = dict(pairs)
    require(values.get('offer_id') in {'', '0'}, 'NEW_IDENTITY')
    require(values.get('node_id') == str(node_id), 'SCOPE')
    require(bool(values.get('csrf_token', '').strip()), 'CSRF')
    require(OWNER_FIELDS <= values.keys() and select_names <= values.keys(), 'SCHEMA')
    require(len(prices) == 1, 'PRICE')
    # A currency <select> is already rejected by the NodeForm parser above.
    observed = _price_currency(form, prices[0])
    # The visible form must show exactly the currency the owner chose.
    require(observed == {currency}, 'PRICE')
    service = [(n, v) for n, v in pairs
               if n not in OWNER_FIELDS and n not in select_names and n not in NEVER_SENT
               and n != 'location']
    service.append(('location', 'trade'))
    return schema, tuple(service)
