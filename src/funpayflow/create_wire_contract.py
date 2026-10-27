"""Pure synthetic NEW-form adapter. No HTML parser, HTTP or live contract.

All controls must be supplied from the complete validated fixture, preserving
duplicates. The synthetic identity/manual/currency profile is NOT server proof.
Envelopes contain transient secrets: never serialize them or persist their body.
"""
from dataclasses import dataclass, field
import math
from typing import Literal

from .lot_publisher import FORM_TTL, TEXT_FIELDS, PreparedLot, PublisherError, require
from .node_form import is_safe_node_binding_name


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
