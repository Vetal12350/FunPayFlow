"""Scoped reusable product options; route overrides use verified catalogue snapshots."""

from copy import deepcopy
from dataclasses import dataclass
import re

from .product_store import normalize_game
from .product_templates import ProductValidationError, safe_text, validate_overrides


_META = {"id", "revision", "created_at", "updated_at"}
_ROUTE_FIELDS = {"template_id", "game_name", "node_id", "section_name", "metadata"}


def validate_variant(body, template, parent_route):
    if not isinstance(body, dict) or not template or not parent_route:
        raise ProductValidationError("variant: template or route not found")
    fields = {"template_id", "route_id", "display_name", "aliases", "overrides", "route_override", "enabled", "node_values"}
    if set(body) - fields - _META or not {"template_id", "route_id", "display_name"} <= set(body):
        raise ProductValidationError("variant: unknown or missing fields")
    clean = deepcopy(body)
    for key in ("template_id", "route_id"):
        if not isinstance(clean[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", clean[key]):
            raise ProductValidationError("variant: invalid scope")
    if (clean["template_id"] != template["id"] or clean["route_id"] != parent_route["id"]
            or parent_route["template_id"] != template["id"]):
        raise ProductValidationError("variant: invalid scope")
    safe_text(clean["display_name"], "display_name", 120)
    aliases = clean.setdefault("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 100:
        raise ProductValidationError("variant: invalid aliases")
    keys = {normalize_game(clean["display_name"])}
    for alias in aliases:
        safe_text(alias, "alias", 120)
        key = normalize_game(alias)
        if key in keys:
            raise ProductValidationError("variant: duplicate alias")
        keys.add(key)
    clean["overrides"] = validate_overrides(clean.get("overrides", {}), template["allowed_variables"])
    if 'node_values' in clean:
        from .product_templates import validate_node_bindings
        values = clean['node_values']
        if not isinstance(values, dict) or len(values) > 50:
            raise ProductValidationError('variant: invalid node values')
        for key, value in values.items():
            validate_node_bindings({'field': key}, ['field'])
            safe_text(value, 'node option', 500, empty=True)
    if type(clean.setdefault("enabled", True)) is not bool:
        raise ProductValidationError("variant: enabled must be boolean")
    override = clean.get("route_override")
    if override is not None:
        if not isinstance(override, dict) or set(override) != _ROUTE_FIELDS:
            raise ProductValidationError("variant: invalid route override")
        metadata = override.get("metadata")
        if (override["template_id"] != template["id"]
                or not isinstance(override["game_name"], str)
                or normalize_game(override["game_name"]) != normalize_game(parent_route["game_name"])
                or type(override["node_id"]) is not int or not 0 < override["node_id"] <= 2**63 - 1
                or not isinstance(metadata, dict) or set(metadata) != {"category_id", "type"}
                or type(metadata.get("category_id")) is not int
                or metadata.get("type") != "COMMON"
                or metadata != parent_route["metadata"]):
            raise ProductValidationError("variant: route must belong to the same game")
        safe_text(override["section_name"], "section_name", 500)
    return clean


@dataclass(frozen=True)
class ProductVariant:
    definition: dict

    def __post_init__(self):
        object.__setattr__(self, "definition", deepcopy(self.definition))

    def to_dict(self):
        return deepcopy(self.definition)


class VariantService:
    def __init__(self, store, routes=None):
        self.store, self.routes = store, routes

    def validate_snapshot(self, body, parent_route, template=None):
        if template is None:
            template = self.store.get_template(parent_route["template_id"])
        return validate_variant(body, template, parent_route)

    def get(self, variant_id, enabled_only=True):
        if type(enabled_only) is not bool:
            raise ProductValidationError("variant: invalid enabled filter")
        body = self.store.get_variant(variant_id)
        if body is None or (enabled_only and not body["enabled"]):
            raise ProductValidationError("variant not found or disabled")
        parent = self.store.get_route(body["route_id"])
        return self.validate_snapshot(body, parent) if parent else self._missing_route()

    @staticmethod
    def _missing_route():
        raise ProductValidationError("variant: route not found")

    def list(self, route_id, limit=50, offset=0, enabled_only=True):
        return self.store.list_variants(route_id, limit, offset, enabled_only)

    def find(self, route_id, name_or_alias):
        safe_text(name_or_alias, "variant", 120)
        key, offset, match = normalize_game(name_or_alias), 0, None
        while True:
            page = self.list(route_id, limit=100, offset=offset)
            for body in page:
                if key in [normalize_game(body["display_name"]), *map(normalize_game, body["aliases"])]:
                    if match is not None:
                        raise ProductValidationError("variant name is ambiguous; choose an explicit variant")
                    match = body
            if len(page) < 100:
                return match
            offset += 100

    def resolve_route(self, variant, parent_route, template=None):
        clean = self.validate_snapshot(variant, parent_route, template)
        override = clean.get("route_override")
        effective = deepcopy(parent_route)
        if override is not None:
            if self.routes is None:
                raise ProductValidationError("variant: verified route service required")
            verified = self.routes.from_url(clean["template_id"], f"https://funpay.com/lots/{override['node_id']}/")
            if any(verified.get(field) != override[field] for field in _ROUTE_FIELDS):
                raise ProductValidationError("variant: route changed; verify it again")
            effective.update({field: deepcopy(override[field]) for field in _ROUTE_FIELDS})
        return effective

    def prepare(self, body):
        """Validate and normalize a confirmation preview without writing SQLite."""
        if not isinstance(body, dict):
            raise ProductValidationError("variant: object required")
        clean = deepcopy(body)
        url = clean.pop("route_override_url", None)
        if url is not None:
            if clean.get("route_override") is not None or self.routes is None:
                raise ProductValidationError("variant: verified route service required or duplicate override")
            verified = self.routes.from_url(clean.get("template_id"), url)
            clean["route_override"] = {field: verified[field] for field in _ROUTE_FIELDS}
        parent = self.store.get_route(clean.get("route_id"))
        if not parent:
            self._missing_route()
        clean = self.validate_snapshot(clean, parent)
        self.resolve_route(clean, parent)
        return clean

    def save(self, body, variant_id=None, expected_revision=None):
        return self.store.save_variant(self.prepare(body), variant_id, expected_revision)

    def disable(self, variant_id, expected_revision):
        return self.store.disable_variant(variant_id, expected_revision)
