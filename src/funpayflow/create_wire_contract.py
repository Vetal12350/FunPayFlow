"""Synthetic NEW-form adapter and dormant, bounded offerSave exchange.

All controls must be supplied from the complete validated fixture, preserving
duplicates. The synthetic identity/manual/currency profile is NOT server proof.
Envelopes contain transient secrets: never serialize them or persist their body.
"""
from dataclasses import dataclass, field
from importlib.metadata import version
import math
import threading
import time
from typing import Literal

import requests
from FunPayAPI import Account

from .lot_publisher import FORM_TTL, TEXT_FIELDS, PreparedLot, PublisherError, SubmitReceipt, require
from .node_form import is_safe_node_binding_name
from .runtime_control import automatic_action_gate


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
    """Not a live capability: submit_once rejects every current envelope.

    fpx proves the endpoint/headers, but neither currency/manual semantics nor
    an ID receipt. No current envelope can pass the live contract gate.
    _exchange is a private transport unit seam, with no application call sites.
    A future integration must first implement the verified contract and keep
    LotPublisher's durable claim, lock, scope/revision and late gates around it.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._consumed = False

    def submit_once(self, *args, **kwargs):
        raise PublisherError('CONTRACT_UNVERIFIED')

    def _require_live_contract(self, envelope):
        # No verified live profile exists. Never accept a provenance string,
        # a synthetic envelope or a green GET matrix as submit authorization.
        raise PublisherError('CONTRACT_UNVERIFIED')

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
            require(publisher.prepare(prepared.draft_id, prepared.revision, form,
                price=dict(prepared.business)['price'], currency=prepared.currency) == prepared,
                'REVISION')
            publisher._scope_gate(account, prepared)
            return self.__send(account, envelope)

    def __send(self, account, envelope):
        """Exactly one fixed-origin exchange; even HTTP 200 proves no new ID.

        Only synthetic tests call this seam. It does not authorize publication
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
            pairs = envelope.service + envelope.business
            require(type(envelope.service) is tuple and type(envelope.business) is tuple
                    and 0 < len(pairs) <= 64
                    and all(type(p) is tuple and len(p) == 2
                            and all(type(v) is str and len(v) <= 10000 for v in p) for p in pairs)
                    and len(dict(pairs)) == len(pairs)
                    and sum(len(v.encode('utf-8')) for p in pairs for v in p) <= 200_000,
                    'SCHEMA')
            headers = {
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': 'https://funpay.com/lots/offerEdit?node=986',
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
            size, started = 0, time.monotonic()
            for chunk in response.iter_content(65536):
                size += len(chunk)
                require(size <= 262_144 and time.monotonic() - started <= 15,
                        'TRANSPORT_UNKNOWN')
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
