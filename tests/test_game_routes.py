"""Offline verified route discovery and strict import checks."""
import threading
from types import SimpleNamespace

import pytest
from FunPayAPI.common.enums import SubCategoryTypes
from FunPayAPI.types import Category, SubCategory

from funpayflow.game_routes import RouteError, RouteService, parse_route_url
from funpayflow.product_store import ProductStore, initialize_schema
from funpayflow.state import ReviewReceiptStore


@pytest.fixture
def service(tmp_path):
    receipt = ReviewReceiptStore(tmp_path / "state.sqlite3")
    receipt.initialize()
    with receipt._connect() as db:
        initialize_schema(db)
    store = ProductStore(receipt, "1", 10)
    category = Category(5, "Игра")
    nodes = [SubCategory(50, "Аккаунты", SubCategoryTypes.COMMON, category),
             SubCategory(51, "Предметы", SubCategoryTypes.COMMON, category),
             SubCategory(60, "Валюта", SubCategoryTypes.CURRENCY, category),
             SubCategory(70, "Аккаунты", SubCategoryTypes.COMMON, Category(6, "Other"))]
    class Account:
        id = 10
        subcategories = nodes
        def get_subcategory(self, kind, node_id):
            return next((n for n in self.subcategories if n.id == node_id and n.type is kind), None)
        def get_user(self, user_id):
            assert user_id == self.id
            return SimpleNamespace(id=10, get_lots=lambda: [
                SimpleNamespace(id=555, subcategory=nodes[0]), SimpleNamespace(id=777, subcategory=nodes[2])])
    client = SimpleNamespace(account=Account(), _account_lock=threading.RLock())
    return RouteService(client, store)


@pytest.mark.parametrize("value", ["http://funpay.com/lots/50/", "https://evil.funpay.com/lots/50/",
    "https://funpay.com@evil.com/lots/50/", "https://funpay.com:443/lots/50/",
    "https://funpay.com/lots/50/?q=1", "https://funpay.com/lots/50/#x",
    "https://funpay.com/chips/50/", "https://funpay.com/lots/0/", "https://funpay.com/lots/50/trade?",
    "https://funpay.com/lots/50/%74rade", "https://funpay.com/lots/50/\n"])
def test_url_rejected(value):
    with pytest.raises(RouteError):
        parse_route_url(value)


def test_verified_url_and_game_match(service):
    template = service.store.save_template({"name": "T"})
    assert parse_route_url("https://funpay.com/lots/50/trade") == 50
    route = service.from_url(template["id"], "https://funpay.com/lots/50/", "ИГРА", ["Game"])
    saved = service.store.save_route(route)
    assert service.resolve_route(saved["id"])["node_id"] == 50
    assert service.from_url(template["id"], "https://funpay.com/lots/50/", "Game")["game_name"] == "Игра"
    for url, name in [("https://funpay.com/lots/60/", "Игра"),
                      ("https://funpay.com/lots/999/", "Игра"),
                      ("https://funpay.com/lots/50/", "Other")]:
        with pytest.raises(RouteError):
            service.from_url(template["id"], url, name)
    service.client.account.subcategories = [n for n in service.client.account.subcategories if n.id != 50]
    with pytest.raises(RouteError):
        service.resolve_route(saved["id"])


def test_own_lots_excludes_currency_and_source_snapshot(service):
    template = service.store.save_template({"name": "T"})
    rows = service.own_lots()
    assert len(rows) == 1 and rows[0]["source_id"] == 555
    body = service.from_lot(template["id"], 555)
    assert "source_id" not in body and body["metadata"] == {"category_id": 5, "type": "COMMON"}


def test_bulk_ambiguity_partial_and_confirm(service):
    template = service.store.save_template({"name": "T"})
    preview = service.preview_bulk(template["id"], "Игра\nOther\nИгра | https://funpay.com/lots/50/\nИгра | https://funpay.com/lots/50/\nWrong | https://funpay.com/lots/50/")
    assert [r["status"] for r in preview["rows"]] == ["warning", "ready", "ready", "duplicate", "error"]
    assert not service.store.list_routes()
    batch = service.store.stage_batch("routes", preview["bodies"])
    assert len(service.store.confirm_batch(batch["token"])["results"]) == 2
    assert service.catalog("игр", limit=1, offset=1)[0]["node_id"] == 51


def test_alias_collision_bulk_warns_and_keeps_other_rows(service):
    template = service.store.save_template({"name": "T"})
    one = service.from_url(template["id"], "https://funpay.com/lots/50/", aliases=["Shared"])
    two = service.from_url(template["id"], "https://funpay.com/lots/70/", aliases=["Shared"])
    service.store.save_route(one)
    service.store.save_route(two)
    service.client.account.subcategories = [*service.client.account.subcategories,
        SubCategory(80, "Аккаунты", SubCategoryTypes.COMMON, Category(8, "Third"))]
    result = service.preview_bulk(template["id"], "Shared | https://funpay.com/lots/50/\nThird")
    assert [row["status"] for row in result["rows"]] == ["warning", "ready"]
    assert len(result["bodies"]) == 1
    with pytest.raises(RouteError):
        service.preview_bulk(template["id"], "A" * 100001)
    with pytest.raises(RouteError):
        parse_route_url("https://funpay.com/lots/99999999999999999999999/")
