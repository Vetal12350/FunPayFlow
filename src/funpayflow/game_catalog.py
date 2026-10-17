"""Lazy, scoped snapshots of Account's read-only COMMON category metadata.

FunPayAPI 1.1.0 populates ``subcategories`` during Account.get(); reading the
property makes no request. Refresh here only copies that existing metadata.
"""

from collections import OrderedDict
from copy import deepcopy
import time
import uuid

from FunPayAPI.common.enums import SubCategoryTypes
from requests.exceptions import ConnectionError, SSLError, Timeout

from .game_routes import RouteError, RouteService
from .product_store import normalize_game


_TTL = 900
_MAX_SCOPES = 32
_UNAVAILABLE = "Не удалось прочитать каталог FunPay. Повторите позже."
_STALE = "Каталог временно недоступен; показана сохранённая в памяти версия."
_EXPIRED = "Каталог изменился. Повторите поиск игры."


def _page(limit, offset):
    if (type(limit) is not int or type(offset) is not int
            or not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000):
        raise RouteError("Некорректная страница каталога.")


def _positive(value):
    return type(value) is int and 0 < value <= 2**63 - 1


class GameCatalog:
    def __init__(self, client, store, clock=None):
        self.client, self.store = client, store
        self.clock = clock or time.monotonic

    def snapshot(self, force=False):
        # The client survives short-lived RouteService/UI instances. Account's
        # lock protects both metadata reads and the attached bounded LRU.
        with self.client._account_lock:
            account = self.client.account
            if account.id != self.store.account_id:
                raise RouteError("Аккаунт FunPay изменился.")
            cache = getattr(self.client, "_game_catalog_cache", None)
            if cache is None:
                cache = self.client._game_catalog_cache = OrderedDict()
            key = (str(self.store.owner_id), self.store.account_id, id(account))
            entry = cache.get(key)
            now = self.clock()
            if entry is not None and entry["account"] is account:
                cache.move_to_end(key)
                if not force and now < entry["expires"]:
                    return deepcopy(entry["snapshot"])
            else:
                entry = None
            try:
                rows = []
                for node in account.subcategories:
                    if (node.type is SubCategoryTypes.COMMON and _positive(node.id)
                            and _positive(node.category.id)):
                        rows.append(RouteService._safe(node))
                rows.sort(key=lambda row: (normalize_game(row["game_name"]),
                                          row["metadata"]["category_id"], row["node_id"]))
                result = dict(rows=rows, generation=uuid.uuid4().hex[:12],
                              stale=False, warning="")
            except Exception as exc:
                temporary = isinstance(exc, (Timeout, ConnectionError)) and not isinstance(exc, SSLError)
                if not temporary or entry is None:
                    # A rejected refresh must not leave an earlier generation
                    # available as fresh metadata on the next callback.
                    cache.pop(key, None)
                    raise RouteError(_UNAVAILABLE) from None
                result = dict(rows=entry["snapshot"]["rows"],
                              generation=uuid.uuid4().hex[:12], stale=True, warning=_STALE)
            # Throttle failed lazy refreshes too; force=True explicitly retries.
            cache[key] = dict(account=account, expires=now + _TTL, snapshot=result)
            cache.move_to_end(key)
            while len(cache) > _MAX_SCOPES:
                cache.popitem(last=False)
            return deepcopy(result)

    def games(self, query="", limit=9, offset=0, force=False):
        _page(limit, offset)
        snapshot = self.snapshot(force=force)
        needle = normalize_game(query)
        grouped = {}
        for row in snapshot.pop("rows"):
            if not needle or needle in normalize_game(row["game_name"]):
                category_id = row["metadata"]["category_id"]
                grouped.setdefault(category_id, dict(game_name=row["game_name"], category_id=category_id))
        games = sorted(grouped.values(), key=lambda row: (
            normalize_game(row["game_name"]) != needle,
            normalize_game(row["game_name"]), row["category_id"]))
        return dict(snapshot, games=games[offset:offset + limit])

    def _current(self, generation):
        snapshot = self.snapshot()
        if not isinstance(generation, str) or generation != snapshot["generation"]:
            raise RouteError(_EXPIRED)
        return snapshot

    def sections(self, category_id, generation, limit=9, offset=0):
        _page(limit, offset)
        if not _positive(category_id):
            raise RouteError(_EXPIRED)
        snapshot = self._current(generation)
        rows = [row for row in snapshot["rows"] if row["metadata"]["category_id"] == category_id]
        if not rows:
            raise RouteError(_EXPIRED)
        snapshot["rows"] = rows[offset:offset + limit]
        return snapshot

    def candidate(self, category_id, node_id, generation):
        if not _positive(category_id) or not _positive(node_id):
            raise RouteError(_EXPIRED)
        for row in self._current(generation)["rows"]:
            if row["metadata"]["category_id"] == category_id and row["node_id"] == node_id:
                return row
        raise RouteError(_EXPIRED)


def saved_routes(store, template_id, query="", limit=9, offset=0, recent=False):
    """Rank canonical names, exact aliases, then partial saved matches only."""
    _page(limit, offset)
    needle = normalize_game(query)
    if not needle or recent:
        return store.list_routes(template_id=template_id, query="" if recent else query,
                                 limit=limit, offset=offset, recent=recent)
    # Store's API streams bounded pages; consume all matches so exact matches on
    # later pages retain priority. No catalog or Account access is involved.
    matches, position = [], 0
    while True:
        page = store.list_routes(template_id=template_id, query=query, limit=100, offset=position)
        matches.extend(page)
        if len(page) < 100:
            break
        position += len(page)
    def rank(row):
        if normalize_game(row["game_name"]) == needle:
            return 0
        if needle in [normalize_game(alias) for alias in row.get("game_aliases", [])]:
            return 1
        return 2
    matches.sort(key=rank)
    return matches[offset:offset + limit]
