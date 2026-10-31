"""Saved GAME_KEY review and live read-only NEW inspection; POST stays disabled.

The service controls and receipt below are an OFFLINE protocol, not a claim
about FunPay's wire format. SyntheticTransport has no implementation. The dormant
create_wire_contract exchange also rejects unverified contracts; neither has a
setting, environment variable or Telegram hook enabling live submission.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import time

from FunPayAPI import Account
from FunPayAPI.common.enums import SubCategoryTypes

from .node_form import is_safe_node_binding_name, inspect_fresh_new_form, safe_contract_proof
from .node_products import selected_fields
from .product_drafts import validate_price
from .product_templates import GAME_KEY, safe_text
from .runtime_control import automatic_action_gate
from .game_routes import RouteService, RouteError


REASONS = frozenset({
    'LIVE_DISABLED', 'CONTRACT_UNVERIFIED', 'SCOPE', 'REVISION', 'SCHEMA',
    'FORM_STALE', 'PRICE', 'QUANTITY', 'CONTENT', 'NEW_IDENTITY', 'CSRF',
    'MANUAL_MODE', 'GATE', 'DATABASE', 'ALREADY_CLAIMED', 'CLAIMED',
    'TRANSPORT_UNKNOWN', 'RESPONSE_ID', 'TRANSPORT_POLICY', 'READBACK',
    'OTHER_LOTS_CHANGED', 'CONFIRMED', 'INVALID',
})
TEXT_FIELDS = {
    'title_ru': 'fields[summary][ru]', 'title_en': 'fields[summary][en]',
    'description_ru': 'fields[desc][ru]', 'description_en': 'fields[desc][en]',
}
FORM_TTL = 300


class PublisherError(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in REASONS else 'INVALID'
        super().__init__(self.reason)


def require(condition, reason):
    if not condition:
        raise PublisherError(reason)


@dataclass(frozen=True, repr=False)
class FormContext:
    """Explicit source/scope; a GET schema alone cannot provide this proof."""
    owner_id: str
    account_id: int
    account_instance: int
    schema: dict
    currency: str
    provenance: str = 'unverified'


@dataclass(frozen=True, repr=False)
class ServiceControls:
    # Deliberately NOT offer_id=0 / auto_delivery=... wire guesses.
    new_identity: str
    csrf: str = field(repr=False)
    manual_mode: str


@dataclass(frozen=True, repr=False)
class PreparedLot:
    owner_id: str
    account_id: int
    account_instance: int
    draft_id: str
    revision: int
    route_id: str
    route_revision: int
    template_id: str
    template_revision: int
    fingerprint: str
    fetched_at: float
    currency: str
    business: tuple

    def summary(self):
        """Fixed metadata only: safe for a report, never content or controls."""
        return {'mode': 'synthetic', 'live_enabled': False, 'node': 986,
                'quantity': 1, 'state': 'PREPARED'}


@dataclass(frozen=True, repr=False)
class PublicationReview:
    owner_id: str
    account_id: int
    account_instance: int
    draft_id: str
    revision: int
    route_id: str
    route_revision: int
    template_id: str
    template_revision: int
    fingerprint: str
    currency: str | None
    price: str
    selections: tuple
    content: tuple
    fetched_at: float = field(compare=False)


# Fixed diagnostics only. A complete structural observation still cannot prove
# server-side NEW semantics or an independent read-back in FunPayAPI 1.1.0.
PUBLICATION_BLOCKERS = {
    'NEW_SEMANTICS': 'Не доказано, что наблюдаемая identity создаёт NEW, а не изменяет лот.',
    'MANUAL_MAPPING': 'MANUAL_MAPPING: fpx не проверяет ручную выдачу; отсутствие secrets не доказывает режим выдачи.',
    'CURRENCY_MAPPING': 'CURRENCY_MAPPING: не доказана связь RUB/USD/EUR с price в NEW-форме; fpx не задаёт и не проверяет валюту цены.',
    'VISIBILITY_MAPPING': 'VISIBILITY_MAPPING: active=on в fpx не доказывает видимость с учётом настроек аккаунта.',
    'SUBMIT_CONTROLS': 'SUBMIT_CONTROLS: значения form_created_at/location/deleted и submit-controls требуют отдельной проверки; defaults из fpx не используются.',
    'RESPONSE_ID': 'RESPONSE_ID: fpx возвращает True по HTTP 200, без доказательства нового offer ID.',
    'OWNER_READBACK': 'OWNER_READBACK: нет независимого подтверждения владельца, данных нового ID и сохранности остальных лотов.',
    'SELECTS': 'Три явных выбора не совпадают с актуальной схемой NEW-формы.',
    'FORM_STRUCTURE': 'FORM_STRUCTURE: NEW-форма или её HTTPS action/method неоднозначны.',
    'CONTROL_INVENTORY': 'CONTROL_INVENTORY: есть неизвестные, повторные или неподдержанные controls.',
    'NEW_IDENTITY': 'NEW_IDENTITY: нет единственных проверенных blank offer_id и node_id=986.',
    'CSRF': 'CSRF: не наблюдается единственный непустой CSRF control.',
}


def publication_blockers(proof, matches):
    """Precise fixed structural failures plus the remaining semantic evidence.

    Source evidence establishes transport shape, never server-side defaults.
    No matrix (including a forged positive one) can enable a final POST button.
    """
    proof = safe_contract_proof(proof)
    blockers = [key for key in PUBLICATION_BLOCKERS if key in {
        'NEW_SEMANTICS', 'MANUAL_MAPPING', 'CURRENCY_MAPPING', 'VISIBILITY_MAPPING',
        'SUBMIT_CONTROLS', 'RESPONSE_ID', 'OWNER_READBACK'}]
    for code, predicates in (
            ('FORM_STRUCTURE', ('editor_url', 'form_subtree', 'submission')),
            ('CONTROL_INVENTORY', ('control_inventory',)),
            ('NEW_IDENTITY', ('blank_identity', 'node_identity')),
            ('CSRF', ('csrf_presence',))):
        if any(proof[key] != 'verified' for key in predicates):
            blockers.append(code)
    if not matches:
        blockers.append('SELECTS')
    return tuple(blockers)


@dataclass(frozen=True)
class SubmitReceipt:
    """Synthetic response contract; no inferred raw server response parser."""
    remote_id: int | None
    http_status: int = 200
    redirects: int = 0
    attempts: int = 1


class SyntheticTransport:
    def submit_once(self, business, controls, *, node_id, method, path,
                    allow_redirects, max_attempts):
        raise PublisherError('LIVE_DISABLED')


def _fresh(fetched_at, now):
    require(type(fetched_at) in (int, float) and type(now) in (int, float)
            and math.isfinite(fetched_at) and math.isfinite(now)
            and 0 <= now - fetched_at <= FORM_TTL, 'FORM_STALE')


def build_new_offer(draft, route, template, form, *, owner_id, account_id,
                    account_instance, revision, price, currency, now):
    """Pure allowlisted business builder. Never copies hidden form controls.

    Rows must come from the scoped ProductStore (checked transactionally again
    at claim). Price/currency are explicit review inputs: legacy drafts have no
    currency, so it must never be inferred from settings or an existing offer.
    """
    try:
        require(type(form) is FormContext and form.provenance == 'synthetic-v1',
                'CONTRACT_UNVERIFIED')
        require(form.owner_id == str(owner_id) and type(account_id) is int
                and account_id > 0 and type(form.account_id) is int
                and form.account_id == account_id
                and form.account_instance == account_instance, 'SCOPE')
        require(draft is not None and route is not None and template is not None, 'SCOPE')
        require(type(revision) is int and revision > 0 and draft['revision'] == revision
                and draft.get('status') != 'cancelled', 'REVISION')
        require(template['name'] == GAME_KEY and not draft.get('variant_id')
                and route['metadata'] == {'category_id': 274, 'type': 'COMMON'}
                and route['node_id'] == draft['node'] == 986
                and route['game_name'].casefold() == draft['game'].casefold() == 'elden ring'
                and draft['route_id'] == route['id']
                and draft['template_id'] == route['template_id'] == template['id'], 'SCOPE')
        require(draft['route_revision'] == route['revision']
                and draft['template_revision'] == template['revision']
                and draft['route_snapshot'] == route
                and draft['template_snapshot'] == template, 'REVISION')
        schema = form.schema
        _fresh(schema['fetched_at'], now)
        require(schema['node_id'] == 986 and draft['node_schema'] == {
            k: schema[k] for k in ('node_id', 'fields', 'fingerprint', 'fetched_at')}, 'SCHEMA')
        fields = schema['fields']
        names = [f['html_name'] for f in fields]
        require(bool(names) and len(names) == len(set(names))
                and all(is_safe_node_binding_name(n) for n in names)
                and all(f['kind'] == 'select' and f['disabled'] is False for f in fields), 'SCHEMA')
        safe = [dict(html_name=f['html_name'], kind=f['kind'], label=f['label'],
                     required=f['required'], disabled=f['disabled'], order=f['order'],
                     options=[{k: o[k] for k in ('value', 'label', 'order', 'disabled')}
                              for o in f['options']]) for f in fields]
        fingerprint = hashlib.sha256(json.dumps(safe, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()
        require(fingerprint == schema['fingerprint'], 'SCHEMA')
        rows = draft['node_fields']
        values = {row['html_name']: row['value'] for row in rows}
        # Require every field, even optional ones: no silent server defaults.
        require(set(values) == set(names) and len(rows) == len(names)
                and selected_fields(schema, values) == rows, 'SCHEMA')
        payload = draft['payload']
        require(set(payload) == {*TEXT_FIELDS, 'price', 'amount'}, 'CONTENT')
        require(type(payload['amount']) is int and payload['amount'] == 1, 'QUANTITY')
        try:
            checked_price = validate_price(payload['price'])
            require(validate_price(price) == checked_price and type(currency) is str
                    and currency in {'USD', 'EUR', 'RUB'} and currency == form.currency, 'PRICE')
        except ValueError:
            raise PublisherError('PRICE') from None
        business = {}
        for key, wire in TEXT_FIELDS.items():
            try:
                business[wire] = safe_text(payload[key], key, 200 if key.startswith('title') else 10000)
            except ValueError:
                raise PublisherError('CONTENT') from None
        business.update(values)
        business.update(price=checked_price, amount='1', currency=currency)
        return PreparedLot(str(owner_id), account_id, account_instance, draft['id'], revision,
                           route['id'], route['revision'], template['id'], template['revision'],
                           fingerprint, schema['fetched_at'], currency, tuple(business.items()))
    except PublisherError:
        raise
    except Exception:
        raise PublisherError('SCHEMA') from None


class LotPublisher:
    def __init__(self, client, store, *, late_check=None, clock=time.time):
        self.client, self.store = client, store
        self.late_check = late_check or (lambda: False)
        self.clock = clock

    def review(self, draft_id, revision, currency):
        """Review saved data without GET, synthetic provenance or currency defaults.

        Old schema timestamps are allowed for PREVIEW only. Confirmation must
        compare the schema/options against a fresh response, never the cache.
        """
        try:
            with closing(self.store.store._connect()) as db:
                db.execute('BEGIN')
                d = self.store._get(db, 'product_drafts', draft_id)
                require(d is not None, 'SCOPE')
                r = self.store._get(db, 'game_routes', d['route_id'])
                t = self.store._get(db, 'product_templates', d['template_id'])
                require(r is not None and t is not None and t['name'] == GAME_KEY
                    and not d.get('variant_id') and r['metadata'] == {'category_id': 274, 'type': 'COMMON'}
                    and r['node_id'] == d['node'] == 986
                    and r['game_name'].casefold() == d['game'].casefold() == 'elden ring'
                    and d['template_id'] == r['template_id'] == t['id'], 'SCOPE')
                require(type(revision) is int and d['revision'] == revision
                    and d.get('status') != 'cancelled' and d['route_snapshot'] == r
                    and d['template_snapshot'] == t and d['route_revision'] == r['revision']
                    and d['template_revision'] == t['revision'], 'REVISION')
                schema, rows, payload = d['node_schema'], d['node_fields'], d['payload']
                fields = schema['fields']
                names = [f['html_name'] for f in fields]
                require(schema['node_id'] == 986 and len(names) == len(set(names)) == len(rows) == 3
                    and all(is_safe_node_binding_name(n) for n in names)
                    and all(f['kind'] == 'select' and f['disabled'] is False for f in fields)
                    and {row['html_name'] for row in rows} == set(names)
                    and selected_fields(schema, {row['html_name']: row['value'] for row in rows}) == rows, 'SCHEMA')
                safe = [dict(html_name=f['html_name'], kind=f['kind'], label=f['label'],
                    required=f['required'], disabled=f['disabled'], order=f['order'],
                    options=[{k: o[k] for k in ('value', 'label', 'order', 'disabled')}
                             for o in f['options']]) for f in fields]
                digest = hashlib.sha256(json.dumps(safe, ensure_ascii=False, sort_keys=True,
                    separators=(',', ':')).encode('utf-8')).hexdigest()
                require(digest == schema['fingerprint'], 'SCHEMA')
                require(set(payload) == {*TEXT_FIELDS, 'price', 'amount'}, 'CONTENT')
                require(type(payload['amount']) is int and payload['amount'] == 1, 'QUANTITY')
                require(currency is None or (type(currency) is str and currency in {'RUB', 'USD', 'EUR'}), 'PRICE')
                price = validate_price(payload['price'])
                content = tuple((k, safe_text(payload[k], k,
                    200 if k.startswith('title') else 10000)) for k in TEXT_FIELDS)
                return PublicationReview(self.store.owner_id, self.store.account_id,
                    id(self.client.account), d['id'], revision, r['id'], r['revision'], t['id'],
                    t['revision'], digest, currency, price,
                    tuple((row['html_name'], row['value'], row['display_label'], row['option_label'])
                          for row in rows), content, self.clock())
        except PublisherError:
            raise
        except (KeyError, TypeError, ValueError):
            raise PublisherError('SCHEMA') from None
        except Exception:
            raise PublisherError('DATABASE') from None

    def inspect_new(self, review):
        """Consume an owner intent at the UI boundary; GET only, no claim/POST.

        The durable guard is consulted even though this path cannot submit.
        A read-only failure must not create a misleading UNKNOWN write attempt.
        """
        require(type(review) is PublicationReview, 'INVALID')
        require(review.currency in {'RUB', 'USD', 'EUR'}, 'PRICE')
        account = self.client.account
        def route_current():
            try:
                route = RouteService(self.client, self.store).resolve_route(review.route_id)
                require(route['revision'] == review.route_revision, 'REVISION')
            except RouteError:
                raise PublisherError('SCOPE') from None
        with self.client._account_lock, automatic_action_gate() as allowed:
            require(allowed, 'GATE')
            self._scope_gate(account, review)
            require(self.outcome(review.draft_id) is None, 'ALREADY_CLAIMED')
            require(self.review(review.draft_id, review.revision, review.currency) == review, 'REVISION')
            route_current()
            proof, matches = inspect_fresh_new_form(self.client, self.store, review.fingerprint,
                tuple((name, value) for name, value, _, _ in review.selections))
            self._scope_gate(account, review)
            require(self.review(review.draft_id, review.revision, review.currency) == review, 'REVISION')
            route_current()
            require(self.outcome(review.draft_id) is None, 'ALREADY_CLAIMED')
            proof = safe_contract_proof(proof)
            return proof, publication_blockers(proof, matches)

    def _build(self, db, draft_id, revision, form, price, currency):
        draft = self.store._get(db, 'product_drafts', draft_id)
        require(draft is not None, 'SCOPE')
        return build_new_offer(draft,
            self.store._get(db, 'game_routes', draft['route_id']),
            self.store._get(db, 'product_templates', draft['template_id']), form,
            owner_id=self.store.owner_id, account_id=self.store.account_id,
            account_instance=id(self.client.account),
            revision=revision, price=price, currency=currency, now=self.clock())

    def prepare(self, draft_id, revision, form, *, price, currency):
        try:
            with closing(self.store.store._connect()) as db, db:
                db.execute('BEGIN')
                return self._build(db, draft_id, revision, form, price, currency)
        except PublisherError:
            raise
        except Exception:
            raise PublisherError('DATABASE') from None

    def outcome(self, draft_id):
        try:
            with closing(self.store.store._connect()) as db:
                row = db.execute('SELECT state,reason,remote_id FROM lot_publish_attempts '
                                 'WHERE owner_id=? AND account_id=? AND draft_id=?',
                                 (*self.store._scope(), draft_id)).fetchone()
                return None if row is None else dict(zip(('state', 'reason', 'remote_id'), row))
        except Exception:
            raise PublisherError('DATABASE') from None

    def _claim(self, prepared, form):
        # A permanent per-draft claim, not per revision/token: an edit must never
        # turn an uncertain modifying attempt into a retry opportunity.
        try:
            with closing(self.store.store._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                require(db.execute('SELECT 1 FROM lot_publish_attempts WHERE owner_id=? '
                                   'AND account_id=? AND draft_id=?',
                                   (*self.store._scope(), prepared.draft_id)).fetchone() is None,
                        'ALREADY_CLAIMED')
                require(self._build(db, prepared.draft_id, prepared.revision, form,
                        dict(prepared.business)['price'], prepared.currency) == prepared, 'REVISION')
                db.execute("INSERT INTO lot_publish_attempts VALUES (?,?,?,?,'UNKNOWN','CLAIMED',NULL,?)",
                           (*self.store._scope(), prepared.draft_id, prepared.revision, self.clock()))
        except PublisherError:
            raise
        except Exception:
            raise PublisherError('DATABASE') from None

    def _finish(self, prepared, reason, remote_id=None):
        require(reason in REASONS, 'INVALID')
        try:
            with closing(self.store.store._connect()) as db, db:
                cursor = db.execute("UPDATE lot_publish_attempts SET state=?,reason=?,remote_id=? "
                    "WHERE owner_id=? AND account_id=? AND draft_id=? AND state='UNKNOWN' AND reason='CLAIMED'",
                    ('CONFIRMED' if reason == 'CONFIRMED' else 'UNKNOWN', reason, remote_id,
                     *self.store._scope(), prepared.draft_id))
                require(cursor.rowcount == 1, 'DATABASE')
        except Exception:
            # The already committed UNKNOWN claim remains a permanent barrier.
            raise PublisherError('DATABASE') from None
        return self.outcome(prepared.draft_id)

    def _scope_gate(self, account, prepared):
        require(self.client.account is account and account.id == prepared.account_id
                and id(account) == prepared.account_instance
                and self.store.account_id == prepared.account_id
                and self.store.owner_id == prepared.owner_id
                and self.client.review_state is self.store.store
                and getattr(account, 'is_initiated', False) is True, 'SCOPE')
        require(self.late_check() is True, 'GATE')
        _fresh(prepared.fetched_at, self.clock())

    @staticmethod
    def _snapshot(account):
        profile = account.get_user(account.id)
        require(profile.id == account.id, 'READBACK')
        result = {}
        for lot in profile.get_lots():
            require(type(lot.id) is int and lot.id > 0 and lot.id not in result, 'READBACK')
            fields = account.get_lot_fields(lot.id)
            require(fields.lot_id == lot.id, 'READBACK')
            # LotFields.lot_id and UserProfile.id in 1.1.0 echo request arguments;
            # they alone cannot authenticate the returned document. These raw
            # identity names/values are an explicit SYNTHETIC read-back contract,
            # still unproven for live creation. Never copy them into business.
            require(fields.fields.get('offer_id') == str(lot.id)
                    and fields.fields.get('node_id') == str(lot.subcategory.id)
                    and fields.fields.get('user_id') == str(account.id), 'READBACK')
            # Only business values in memory; never persist other lots or tokens.
            business = {k: v for k, v in fields.fields.items() if k in {
                *TEXT_FIELDS.values(), 'price', 'amount', 'currency', 'active',
                'deactivate_after_sale', 'auto_delivery'} or is_safe_node_binding_name(k)}
            result[lot.id] = (lot.subcategory.id, lot.subcategory.category.id,
                              lot.subcategory.type, deepcopy(business))
        return result

    def confirm_synthetic(self, prepared, form, controls, *, transport=None):
        """Offline harness only. Real Accounts and unimplemented transport fail closed.

        The claim is already UNKNOWN before the boundary. Even BaseException or
        process death needs no recovery rewrite and can never release the claim.
        """
        require(type(transport) is SyntheticTransport, 'LIVE_DISABLED')
        require(type(prepared) is PreparedLot, 'INVALID')
        account = self.client.account
        require(not isinstance(account, Account), 'LIVE_DISABLED')
        # Copy the form once so an external mutable dictionary cannot change the
        # checked schema while this attempt waits for the Account lock.
        form = deepcopy(form)
        try:
            with self.client._account_lock, automatic_action_gate() as allowed:
                require(allowed, 'GATE')
                self._scope_gate(account, prepared)
                require(type(controls) is ServiceControls, 'CONTRACT_UNVERIFIED')
                require(controls.new_identity == 'SYNTHETIC_NEW', 'NEW_IDENTITY')
                require(type(controls.csrf) is str and bool(controls.csrf.strip()), 'CSRF')
                require(controls.manual_mode == 'SYNTHETIC_MANUAL', 'MANUAL_MODE')
                require(self.outcome(prepared.draft_id) is None, 'ALREADY_CLAIMED')
                baseline = self._snapshot(account)
                self._scope_gate(account, prepared)
                self._claim(prepared, form)
                try:
                    require(self.prepare(prepared.draft_id, prepared.revision, form,
                            price=dict(prepared.business)['price'], currency=prepared.currency)
                            == prepared, 'REVISION')
                    self._scope_gate(account, prepared)
                    receipt = transport.submit_once(dict(prepared.business), controls,
                        node_id=986, method='POST', path='lots/offerSave', allow_redirects=False, max_attempts=1)
                    require(type(receipt) is SubmitReceipt and type(receipt.http_status) is int
                            and receipt.http_status == 200 and type(receipt.redirects) is int
                            and receipt.redirects == 0 and type(receipt.attempts) is int
                            and receipt.attempts == 1, 'TRANSPORT_POLICY')
                    remote_id = receipt.remote_id
                    require(type(remote_id) is int and remote_id > 0 and remote_id not in baseline,
                            'RESPONSE_ID')
                    require(self.client.account is account and account.id == prepared.account_id, 'SCOPE')
                    after = self._snapshot(account)
                    require(set(after) == set(baseline) | {remote_id}
                            and all(after[k] == v for k, v in baseline.items()), 'OTHER_LOTS_CHANGED')
                    node, category, kind, business = after[remote_id]
                    require(node == 986 and category == 274 and kind is SubCategoryTypes.COMMON
                            and business == dict(prepared.business, auto_delivery='SYNTHETIC_MANUAL'), 'READBACK')
                except PublisherError as error:
                    return self._finish(prepared, error.reason)
                except Exception:
                    return self._finish(prepared, 'TRANSPORT_UNKNOWN')
                return self._finish(prepared, 'CONFIRMED', remote_id)
        except PublisherError:
            raise
        except Exception:
            raise PublisherError('READBACK') from None
