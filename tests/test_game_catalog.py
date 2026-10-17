"""Offline metadata cache and saved route ranking; no runtime or HTTP calls."""
import threading
from types import SimpleNamespace

import pytest
from FunPayAPI.common.enums import SubCategoryTypes
from FunPayAPI.types import Category, SubCategory
from requests.exceptions import ConnectionError, SSLError, Timeout, HTTPError

from funpayflow.game_catalog import GameCatalog, saved_routes
from funpayflow.game_routes import RouteError
from funpayflow.product_store import ProductStore, initialize_schema
from funpayflow.state import ReviewReceiptStore


def node(category_id=5, game="Elden Ring", node_id=50, name="Аккаунты", kind=SubCategoryTypes.COMMON):
    return SubCategory(node_id, name, kind, Category(category_id, game))


class Account:
    id = 10

    def __init__(self, nodes=None):
        self.nodes = [node()] if nodes is None else nodes
        self.reads = 0
        self.failure = None

    @property
    def subcategories(self):
        self.reads += 1
        if self.failure:
            raise self.failure
        return self.nodes


@pytest.fixture
def catalog():
    client = SimpleNamespace(account=Account(), _account_lock=threading.RLock())
    store = SimpleNamespace(owner_id="1", account_id=10)
    clock = [1000.0]
    result = GameCatalog(client, store, clock=lambda: clock[0])
    result.test_clock = clock
    return result


def test_lazy_ttl_force_and_new_service_reuse(catalog):
    assert catalog.client.account.reads == 0
    first = catalog.snapshot()
    assert catalog.client.account.reads == 1
    other = GameCatalog(catalog.client, catalog.store, clock=catalog.clock)
    assert other.snapshot() == first
    assert catalog.client.account.reads == 1
    catalog.test_clock[0] += 899
    assert catalog.snapshot() == first
    catalog.test_clock[0] += 1
    refreshed = catalog.snapshot()
    assert refreshed["generation"] != first["generation"]
    assert catalog.client.account.reads == 2
    forced = catalog.snapshot(force=True)
    assert forced["generation"] != refreshed["generation"]
    assert catalog.client.account.reads == 3


def test_empty_cache_is_success_and_is_cached(catalog):
    catalog.client.account.nodes = []
    result = catalog.snapshot()
    assert result["rows"] == [] and result["stale"] is False
    assert catalog.snapshot() == result
    assert catalog.client.account.reads == 1


def test_safe_metadata_common_only_and_detached(catalog):
    catalog.client.account.nodes += [node(kind=SubCategoryTypes.CURRENCY), node(node_id=-1)]
    result = catalog.snapshot()
    assert result["rows"] == [dict(game_name="Elden Ring", node_id=50,
        section_name="Аккаунты", metadata={"category_id": 5, "type": "COMMON"})]
    result["rows"][0]["metadata"]["category_id"] = 999
    assert catalog.snapshot()["rows"][0]["metadata"]["category_id"] == 5


def test_owner_account_and_client_isolation(catalog):
    first = catalog.snapshot()
    another_owner = GameCatalog(catalog.client, SimpleNamespace(owner_id="2", account_id=10), clock=catalog.clock)
    assert another_owner.snapshot()["generation"] != first["generation"]
    assert catalog.client.account.reads == 2
    another_client = SimpleNamespace(account=catalog.client.account, _account_lock=threading.RLock())
    assert GameCatalog(another_client, catalog.store).snapshot()["generation"] != first["generation"]
    catalog.client.account.id = 11
    with pytest.raises(RouteError, match="Аккаунт FunPay изменился"):
        catalog.snapshot()
    assert catalog.client.account.reads == 3


def test_new_account_object_same_id_invalidates(catalog):
    first = catalog.snapshot()
    replacement = catalog.client.account = Account([node(game="Cyberpunk")])
    second = catalog.snapshot()
    assert second["generation"] != first["generation"]
    assert second["rows"][0]["game_name"] == "Cyberpunk"
    assert replacement.reads == 1
    with pytest.raises(RouteError, match="Каталог изменился"):
        catalog.candidate(5, 50, first["generation"])


def test_replaced_account_never_receives_old_stale_snapshot(catalog):
    catalog.snapshot()
    catalog.client.account = Account()
    catalog.client.account.failure = Timeout("secret")
    with pytest.raises(RouteError):
        catalog.snapshot()


@pytest.mark.parametrize("failure", [Timeout("private error"), ConnectionError("private error")])
def test_transient_stale_throttled_and_force_retries(catalog, failure):
    first = catalog.snapshot()
    catalog.client.account.failure = failure
    catalog.test_clock[0] += 900
    stale = catalog.snapshot()
    assert stale["stale"] is True and stale["warning"]
    assert stale["rows"] == first["rows"]
    assert stale["generation"] != first["generation"]
    assert "private" not in stale["warning"]
    assert catalog.snapshot() == stale
    assert catalog.client.account.reads == 2
    again = catalog.snapshot(force=True)
    assert again["generation"] != stale["generation"]
    assert catalog.client.account.reads == 3
    catalog.client.account.failure = None
    assert catalog.snapshot(force=True)["stale"] is False


@pytest.mark.parametrize("failure", [Timeout("secret"), ConnectionError("secret"), SSLError("secret"),
    HTTPError("secret"), RuntimeError("secret")])
def test_cold_failures_are_safe(catalog, failure):
    catalog.client.account.failure = failure
    with pytest.raises(RouteError) as caught:
        catalog.snapshot()
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("failure", [SSLError("certificate secret"), HTTPError("auth secret"),
    RuntimeError("secret")])
def test_nontransient_failures_never_fall_back(catalog, failure):
    first = catalog.snapshot()
    catalog.client.account.failure = failure
    with pytest.raises(RouteError) as caught:
        catalog.snapshot(force=True)
    assert "secret" not in str(caught.value)
    with pytest.raises(RouteError):
        catalog.candidate(5, 50, first["generation"])
    catalog.client.account.failure = None
    with pytest.raises(RouteError, match="Каталог изменился"):
        catalog.candidate(5, 50, first["generation"])
    assert catalog.snapshot()["generation"] != first["generation"]


def test_games_group_normalize_paginate_and_sections(catalog):
    catalog.client.account.nodes = [node(), node(node_id=51, name="Предметы"),
        node(6, "Cyberpunk", 60), node(7, "Straße  Game", 70),
        node(8, "Other", 80, kind=SubCategoryTypes.CURRENCY)]
    games = catalog.games()
    assert [row["category_id"] for row in games["games"]] == [6, 5, 7]
    assert catalog.games("  STRASSE   GAME ")["games"] == [dict(game_name="Straße  Game", category_id=7)]
    assert catalog.games("", limit=1, offset=1)["games"] == [dict(game_name="Elden Ring", category_id=5)]
    sections = catalog.sections(5, games["generation"], limit=1, offset=1)
    assert [row["node_id"] for row in sections["rows"]] == [51]
    assert catalog.candidate(5, 50, games["generation"])["node_id"] == 50
    assert catalog.client.account.reads == 1
    with pytest.raises(RouteError):
        catalog.candidate(6, 50, games["generation"])
    with pytest.raises(RouteError):
        catalog.sections(999, games["generation"])
    catalog.test_clock[0] += 900
    with pytest.raises(RouteError):
        catalog.candidate(5, 50, games["generation"])


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (True, 0), (1, -1), (1, True)])
def test_bad_pagination_does_not_read(catalog, limit, offset):
    with pytest.raises(RouteError):
        catalog.games(limit=limit, offset=offset)
    assert catalog.client.account.reads == 0


def test_cache_scope_limit(catalog):
    first = catalog.snapshot()
    for owner_id in range(2, 40):
        GameCatalog(catalog.client, SimpleNamespace(owner_id=str(owner_id), account_id=10),
                    clock=catalog.clock).snapshot()
    assert len(catalog.client._game_catalog_cache) == 32
    assert catalog.snapshot()["generation"] != first["generation"]


def test_snapshot_property_read_is_under_shared_lock(catalog):
    lock = catalog.client._account_lock
    class LockedAccount(Account):
        @property
        def subcategories(self):
            assert lock._is_owned()
            return super().subcategories
    catalog.client.account = LockedAccount()
    assert catalog.snapshot()["rows"]


def test_saved_exact_priority_past_store_page():
    rows = [dict(game_name="Game Extended", game_aliases=[], id=str(i)) for i in range(101)]
    rows += [dict(game_name="Other", game_aliases=["Game"], id="alias"),
             dict(game_name="Game", game_aliases=[], id="exact")]
    class Store:
        def list_routes(self, **kwargs):
            assert kwargs["template_id"] == "template"
            return rows[kwargs["offset"]:kwargs["offset"] + kwargs["limit"]]
    assert [row["id"] for row in saved_routes(Store(), "template", "GAME", limit=2)] == ["exact", "alias"]


def test_saved_route_priority_and_unicode_without_catalog(tmp_path):
    receipt = ReviewReceiptStore(tmp_path / "state.sqlite3")
    receipt.initialize()
    with receipt._connect() as db:
        initialize_schema(db)
    store = ProductStore(receipt, "1", 10)
    template_id = store.save_template({"name": "T"})["id"]
    def save(game, aliases, node_id):
        return store.save_route(dict(template_id=template_id, game_name=game, game_aliases=aliases,
            node_id=node_id, section_name="Аккаунты", metadata={"category_id": node_id, "type": "COMMON"},
            overrides={}))
    canonical = save("Straße Game", [], 1)
    alias = save("Other", ["STRASSE GAME"], 2)
    partial = save("Straße Game Extended", [], 3)
    rows = saved_routes(store, template_id, " STRASSE   GAME ")
    assert [row["id"] for row in rows] == [canonical["id"], alias["id"], partial["id"]]
    assert saved_routes(store, template_id, "strasse game", limit=1, offset=1)[0]["id"] == alias["id"]
    other_store = ProductStore(receipt, "2", 10)
    assert saved_routes(other_store, template_id, "strasse game") == []
    assert saved_routes(store, template_id, recent=True) == store.list_routes(template_id=template_id, recent=True, limit=9)
