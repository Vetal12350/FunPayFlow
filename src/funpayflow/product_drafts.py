"""Offline product previews and explicit, local-only draft batch confirmation."""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import re

from .product_templates import ProductTemplate, ProductValidationError, TemplateService, validate_overrides
from .product_variants import VariantService


def validate_price(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ProductValidationError("price: positive decimal required")
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,8})?", text):
        raise ProductValidationError("price: positive decimal required; currency is unchanged")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ProductValidationError("price: invalid decimal") from exc
    if not number.is_finite() or number <= 0:
        raise ProductValidationError("price: positive finite decimal required")
    return text


class DraftService:
    def __init__(self, store, routes=None):
        self.store = store
        self.routes = routes
        self.templates = TemplateService(store)
        self.variants = VariantService(store, routes)

    def _route(self, route_id, template_id):
        if self.routes is None:
            raise ProductValidationError("route registry is unavailable")
        route = self.routes.resolve_route(route_id)
        if route.get("template_id") != template_id:
            raise ProductValidationError("route belongs to another template")
        return route

    def create_preview(self, template_id, route_id, variables=None, price=None, overrides=None, variant_id=None):
        template = self.templates.get(template_id)
        route = self._route(route_id, template_id)
        variant = self.variants.get(variant_id) if variant_id is not None else None
        return self._render(template, route, {} if variables is None else variables, price,
                            {} if overrides is None else overrides, variant)

    def _render(self, template, route, variables, price, overrides, variant=None):
        model = ProductTemplate(template)
        allowed = template["allowed_variables"]
        if not isinstance(variables, dict):
            raise ProductValidationError("variables: object required")
        if variant is not None:
            route = self.variants.resolve_route(variant, route, template=template)
        route_overrides = validate_overrides(route.get("overrides") or {}, allowed)
        variant_overrides = validate_overrides(variant.get("overrides", {}) if variant else {}, allowed)
        draft_overrides = validate_overrides(overrides, allowed)
        if variant is not None and variables:
            # Explicit wizard/row values are draft overrides, above inherited defaults.
            draft_overrides["variables"] = {**variables, **draft_overrides.get("variables", {})}
        canonical_game = route["game_name"]
        for source in (variables, route_overrides.get("variables", {}), variant_overrides.get("variables", {}), draft_overrides.get("variables", {})):
            if "game" in source and source["game"] != canonical_game:
                raise ProductValidationError("game: must match the verified route; customize product_name instead")
        combined = {**route_overrides, **variant_overrides, **draft_overrides}
        combined["variables"] = {**route_overrides.get("variables", {}),
                                 **variant_overrides.get("variables", {}), **draft_overrides.get("variables", {})}
        values = {"product_name": canonical_game, **variables, "game": canonical_game}
        rendered = model.render(values, combined)
        quantity = rendered["variables"].get("quantity", "1")
        result = {
            "template_id": template["id"], "template_name": template["name"],
            "template_revision": template.get("revision", 1), "template_snapshot": model.to_dict(),
            "game": rendered["variables"]["game"], "route_id": route["id"],
            "node": route["node_id"], "section": route["section_name"],
            "emoji": rendered["emoji"], "variables": rendered["variables"],
            "overrides": deepcopy(draft_overrides),
            "payload": {key: rendered[key] for key in ("title_ru", "title_en", "description_ru", "description_en")}
                       | {"price": validate_price(price), "amount": int(quantity)},
        }
        if variant is not None:
            result.update(variant_id=variant["id"], variant_name=variant["display_name"],
                          variant_snapshot=deepcopy(variant))
        return result

    def rebuild(self, preview, *, price=None, overrides=None, variables=None):
        """Regenerate with the reviewed template snapshot and current verified route."""
        template = deepcopy(preview["template_snapshot"])
        route = self._route(preview["route_id"], preview["template_id"])
        merged = {**preview.get("overrides", {}), **(overrides or {})}
        merged["variables"] = {**preview.get("overrides", {}).get("variables", {}),
                               **(overrides or {}).get("variables", {})}
        values = {**preview["variables"], **(variables or {})}
        variant = preview.get("variant_snapshot")
        if variant is not None:
            # Inherited rendered values must not become explicit draft overrides on rebuild.
            values = {**merged.get("variables", {}), **(variables or {})}
        # An explicit variable edit replaces earlier variable overrides as well.
        for key in variables or {}:
            merged["variables"].pop(key, None)
        if not merged["variables"] and "variables" not in preview.get("overrides", {}) and "variables" not in (overrides or {}):
            merged.pop("variables")
        return self._render(template, route, values,
                            preview["payload"]["price"] if price is None else price, merged, variant)

    def preview_bulk(self, template_id, text, common=None):
        """Rows: Game | [explicit variant |] price [| variable=value | emoji=value]."""
        if not isinstance(text, str) or len(text) > 100000:
            raise ProductValidationError("bulk: text limit exceeded")
        common = {} if common is None else common
        if not isinstance(common, dict) or set(common) - {"variables", "overrides", "price"}:
            raise ProductValidationError("bulk: unknown common field")
        lines = [(index, row.strip()) for index, row in enumerate(text.splitlines(), 1) if row.strip()]
        if not lines or len(lines) > 100:
            raise ProductValidationError("bulk: expected 1 to 100 nonempty rows")
        template = self.templates.get(template_id)
        previews, errors, fingerprints = [], [], set()
        for line, row in lines:
            try:
                parts = [part.strip() for part in row.split("|")]
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    raise ProductValidationError("expected Game | price")
                variant_name = None
                if len(parts) >= 3 and "=" not in parts[2]:
                    variant_name, price, options = parts[1], parts[2], parts[3:]
                else:
                    price, options = parts[1], parts[2:]
                validate_price(price)
                variables = deepcopy(common.get("variables", {}))
                overrides = deepcopy(common.get("overrides", {}))
                seen = set()
                for option in options:
                    key, separator, value = option.partition("=")
                    if not separator or key in seen or not value:
                        raise ProductValidationError("invalid or repeated row option")
                    seen.add(key)
                    if key in template["allowed_variables"]:
                        variables[key] = value
                        if isinstance(overrides.get("variables"), dict):
                            overrides["variables"].pop(key, None)
                    elif key in {"emoji", "title_ru", "title_en", "description_ru", "description_en"}:
                        overrides[key] = value
                    else:
                        raise ProductValidationError("unknown row option")
                route = self.store.find_route(template_id, parts[0])
                if route is None:
                    raise ProductValidationError("game route not found")
                variant = self.variants.find(route["id"], variant_name) if variant_name is not None else None
                if variant_name is not None and variant is None:
                    raise ProductValidationError("variant not found or disabled")
                preview = self.create_preview(template_id, route["id"], variables, price, overrides,
                                              variant["id"] if variant else None)
                fingerprint = json.dumps({"route_id": preview["route_id"], "node": preview["node"],
                                          "variant_id": preview.get("variant_id"), "payload": preview["payload"]},
                                         sort_keys=True, ensure_ascii=False)
                if fingerprint in fingerprints:
                    errors.append({"line": line, "error": "duplicate row skipped", "status": "duplicate"})
                    continue
                fingerprints.add(fingerprint)
                previews.append(preview)
            except ValueError as exc:
                errors.append({"line": line, "error": str(exc)})
        return {"previews": previews, "errors": errors}

    def stage(self, previews):
        if not isinstance(previews, list) or not 1 <= len(previews) <= 100:
            raise ProductValidationError("batch: expected 1 to 100 previews")
        # A staged immutable snapshot is what confirmation saves, even after template edits.
        checked = []
        for preview in previews:
            regenerated = self.rebuild(preview)
            if regenerated != preview:
                raise ProductValidationError("preview changed; regenerate and review before staging")
            checked.append(deepcopy(preview))
        return self.store.stage_batch("drafts", checked)

    def confirm(self, token):
        return self.store.confirm_batch(token)

    def cancel(self, token):
        return self.store.cancel_batch(token)
