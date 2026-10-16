"""Read-only verified COMMON game routing; no offer forms or publication calls."""

import re
from urllib.parse import urlsplit

from FunPayAPI.common.enums import SubCategoryTypes

from .product_store import normalize_game


class RouteError(ValueError):
    """Fixed safe error suitable for display in Telegram."""


class RouteAmbiguityError(RouteError):
    """An import row requires explicit route selection."""


OVERRIDE_FIELDS = {"emoji", "title_ru", "title_en", "description_ru", "description_en", "variables"}


def parse_route_url(value):
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.netloc != "funpay.com"
                or parsed.query or parsed.fragment or "?" in value or "#" in value):
            raise ValueError
        match = re.fullmatch(r"/lots/([1-9][0-9]*)/(?:trade)?", parsed.path)
        if not match:
            raise ValueError
        if len(match[1]) > 19 or int(match[1]) > 2**63 - 1:
            raise ValueError
        return int(match[1])
    except (ValueError, TypeError, AttributeError):
        raise RouteError("Нужна ссылка https://funpay.com/lots/ID/ на обычный раздел игры.") from None


class RouteService:
    def __init__(self, client, store):
        self.client, self.store = client, store

    @staticmethod
    def _safe(subcategory):
        return dict(game_name=str(subcategory.category.name), node_id=subcategory.id,
                    section_name=str(subcategory.name),
                    metadata={"category_id": subcategory.category.id, "type": "COMMON"})

    def _node(self, node_id):
        with self.client._account_lock:
            if self.client.account.id != self.store.account_id:
                raise RouteError("Аккаунт FunPay изменился.")
            node = self.client.account.get_subcategory(SubCategoryTypes.COMMON, node_id)
            if (node is None or node.type is not SubCategoryTypes.COMMON
                    or type(node.id) is not int or node.id <= 0
                    or type(node.category.id) is not int or node.category.id <= 0):
                raise RouteError("Раздел не подтверждён в каталоге аккаунта FunPay.")
            return self._safe(node)

    def catalog(self, query="", limit=10, offset=0):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
            raise RouteError("Некорректная страница каталога.")
        needle = normalize_game(query)
        with self.client._account_lock:
            if self.client.account.id != self.store.account_id:
                raise RouteError("Аккаунт FunPay изменился.")
            result = [self._safe(node) for node in self.client.account.subcategories
                      if node.type is SubCategoryTypes.COMMON
                      and (not needle or needle in normalize_game(node.category.name))]
        result.sort(key=lambda row: (normalize_game(row["game_name"]), row["node_id"]))
        return result[max(0, offset):max(0, offset) + max(1, min(100, limit))]

    def own_lots(self):
        try:
            with self.client._account_lock:
                account_id = self.client.account.id
                if account_id != self.store.account_id:
                    raise RouteError("Аккаунт FunPay изменился.")
                profile = self.client.account.get_user(account_id)
                if profile.id != account_id:
                    raise RouteError("Не удалось подтвердить владельца лотов.")
                result = []
                for lot in profile.get_lots():
                    node = lot.subcategory
                    if node.type is SubCategoryTypes.COMMON:
                        safe = self._node(node.id)
                        if safe["metadata"]["category_id"] != node.category.id:
                            continue
                        result.append(dict(safe, source_id=lot.id))
                return result
        except RouteError:
            raise
        except Exception:
            raise RouteError("Не удалось прочитать собственные лоты FunPay.") from None

    def from_lot(self, template_id, source_id):
        lot = next((row for row in self.own_lots() if row["source_id"] == source_id), None)
        if lot is None:
            raise RouteError("Собственный лот не найден.")
        return self.from_url(template_id, f"https://funpay.com/lots/{lot['node_id']}/")

    def from_url(self, template_id, url, game_name=None, aliases=None):
        if not self.store.get_template(template_id):
            raise RouteError("Шаблон не найден.")
        safe = self._node(parse_route_url(url))
        aliases = aliases or []
        if (not isinstance(aliases, list) or any(not isinstance(a, str) or not normalize_game(a) for a in aliases)):
            raise RouteError("Некорректные псевдонимы игры.")
        name = game_name or safe["game_name"]
        # Alias authorization comes from an existing, verified mapping, not an untrusted import row.
        try:
            existing = self.store.find_route(template_id, name)
        except ValueError:
            raise RouteAmbiguityError("Название игры неоднозначно; выберите конкретный маршрут.") from None
        if normalize_game(name) != normalize_game(safe["game_name"]) and not (
                existing and existing["node_id"] == safe["node_id"]):
            raise RouteError("Название игры не соответствует разделу каталога.")
        safe.update(template_id=template_id, game_aliases=list(dict.fromkeys(aliases)), overrides={})
        return safe

    def resolve_route(self, route_id):
        route = self.store.get_route(route_id)
        if not route:
            raise RouteError("Маршрут игры не найден.")
        safe = self._node(route["node_id"])
        if (normalize_game(route["game_name"]) != normalize_game(safe["game_name"])
                or route.get("metadata") != safe["metadata"]
                or route.get("section_name") != safe["section_name"]):
            raise RouteError("Маршрут изменился в каталоге; подтвердите его заново.")
        return route

    def preview_bulk(self, template_id, text):
        if not isinstance(text, str) or len(text) > 100_000:
            raise RouteError("Слишком большой или некорректный список игр.")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not 1 <= len(lines) <= 100:
            raise RouteError("Введите от 1 до 100 непустых строк.")
        rows, bodies, seen = [], [], set()
        for index, line in enumerate(lines):
            name, separator, url = line.partition("|")
            name = name.strip()
            row = dict(index=index, game_name=name)
            try:
                if separator:
                    body = self.from_url(template_id, url.strip(), name)
                else:
                    candidates = [candidate for candidate in self.catalog(name, limit=100)
                                  if normalize_game(candidate["game_name"]) == normalize_game(name)]
                    if not candidates:
                        raise RouteError("Игра не найдена в каталоге аккаунта.")
                    if len(candidates) > 1:
                        rows.append(dict(row, status="warning", message="Несколько разделов игры; укажите Game | URL."))
                        continue
                    body = self.from_url(template_id, f"https://funpay.com/lots/{candidates[0]['node_id']}/", name)
                key = normalize_game(body["game_name"])
                try:
                    existing = self.store.find_route(template_id, body["game_name"])
                except ValueError:
                    rows.append(dict(row, status="warning", message="Название игры неоднозначно; выберите конкретный маршрут."))
                    continue
                if key in seen or existing:
                    rows.append(dict(row, status="duplicate", message="Маршрут для этого шаблона уже существует."))
                    continue
                seen.add(key)
                bodies.append(body)
                rows.append(dict(row, status="ready", body=body))
            except RouteAmbiguityError as exc:
                rows.append(dict(row, status="warning", message=str(exc)))
            except RouteError as exc:
                rows.append(dict(row, status="error", message=str(exc)))
        return dict(rows=rows, bodies=bodies)
