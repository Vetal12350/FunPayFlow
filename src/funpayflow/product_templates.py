"""Local bilingual product templates. Only named ``{{variable}}`` substitution runs."""

from copy import deepcopy
from dataclasses import dataclass
import re

from .node_form import is_safe_node_binding_name


GAME_KEY = "GAME_KEY"
GIFT = "GIFT"
PLAYSTATION_GAME = "PLAYSTATION_GAME"
AI_SUBSCRIPTION = "AI_SUBSCRIPTION"
BUILTIN_TYPES = (
    (GAME_KEY, '🔑 Игровой ключ'),
    (GIFT, '🎁 Гифт игры'),
    (PLAYSTATION_GAME, '🎮 Игра PlayStation'),
    (AI_SUBSCRIPTION, '🤖 Подписка / доступ к ИИ-сервису'),
)
VARIABLES = ("game", "platform", "region", "edition", "product_name", "quantity")
TEXT_FIELDS = ("title_ru", "title_en", "description_ru", "description_en")
OVERRIDE_FIELDS = frozenset(("emoji", *TEXT_FIELDS, "variables"))
_TOKEN = re.compile(r"\{\{([a-z][a-z0-9_]{0,39})\}\}")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_RESERVED_VARIABLES = frozenset({
    "price", "amount", "account_id", "offer_id", "node_id", "publish_state",
    "golden_key", "csrf", "csrf_token", "form_created_at", "phpsessid", "cookie", "cookies",
    "password", "passwords", "bot_token", "api_key", "token", "delivery_secrets",
    "access_token", "refresh_token", "session_id", "api_secret", "secret_key", "authorization",
})


class ProductValidationError(ValueError):
    """Invalid user supplied product definition or rendering input."""


def safe_text(value, field, limit=10000, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ProductValidationError(f"{field}: required text")
    if len(value) > limit or any(ord(c) < 32 and c != "\n" for c in value):
        raise ProductValidationError(f"{field}: text exceeds limits or contains control characters")
    return value


def default_template():
    return {
        "name": GAME_KEY,
        "display_name": "Ключ игры / Game key",
        "default_emoji": "🔑",
        "title_ru": "{{game}} — {{platform}} | {{region}} | {{edition}}",
        "title_en": "{{game}} — {{platform}} | {{region}} | {{edition}}",
        "description_ru": "Ключ {{product_name}}. Платформа: {{platform}}. Регион: {{region}}. Издание: {{edition}}. Количество: {{quantity}}.",
        "description_en": "{{product_name}} key. Platform: {{platform}}. Region: {{region}}. Edition: {{edition}}. Quantity: {{quantity}}.",
        "allowed_variables": list(VARIABLES),
        "defaults": {"platform": "Steam", "region": "Global", "edition": "Standard", "quantity": "1"},
        "validation_rules": {},
    }


def builtin_template(kind=GAME_KEY):
    """Opt-in content presets; category and delivery are never inferred."""
    if kind == GAME_KEY:
        return default_template()
    body = default_template()
    body.update(name=kind, defaults={'quantity': '1'}, validation_rules={})
    if kind == GIFT:
        extra = ['sale_name', 'sale_platform', 'availability', 'delivery_method']
        body.update(display_name='Гифт игры / Game gift', default_emoji='🎁',
            title_ru='{{sale_name}} — гифт | {{sale_platform}}',
            title_en='{{sale_name}} — gift | {{sale_platform}}',
            description_ru='Гифт игры {{sale_name}}. Платформа: {{sale_platform}}. Регион / доступность: {{availability}}. Способ передачи, указанный владельцем: {{delivery_method}}. Количество: {{quantity}}.',
            description_en='Game gift: {{sale_name}}. Platform: {{sale_platform}}. Region / availability: {{availability}}. Owner-specified delivery method: {{delivery_method}}. Quantity: {{quantity}}.')
    elif kind == PLAYSTATION_GAME:
        extra = ['sale_name', 'ps_platform', 'sale_edition', 'availability', 'product_form', 'delivery_method']
        body.update(display_name='Игра PlayStation / PlayStation game', default_emoji='🎮',
            title_ru='{{sale_name}} — {{ps_platform}} | {{sale_edition}}',
            title_en='{{sale_name}} — {{ps_platform}} | {{sale_edition}}',
            description_ru='Игра {{sale_name}}. Платформа: {{ps_platform}}. Издание: {{sale_edition}}. Регион / доступность: {{availability}}. Форма товара: {{product_form}}. Законный способ передачи, подтверждённый владельцем: {{delivery_method}}. Передача личного аккаунта не предлагается. Количество: {{quantity}}.',
            description_en='Game: {{sale_name}}. Platform: {{ps_platform}}. Edition: {{sale_edition}}. Region / availability: {{availability}}. Product form: {{product_form}}. Owner-confirmed legitimate delivery method: {{delivery_method}}. No personal account transfer offered. Quantity: {{quantity}}.',
            validation_rules={'ps_platform': {'choices': ['PS4', 'PS5', 'PS4 / PS5']},
                'product_form': {'choices': ['Код / Code', 'Законное право на игру / Legitimate entitlement']}})
    elif kind == AI_SUBSCRIPTION:
        extra = ['sale_name', 'plan', 'duration', 'availability', 'delivery_method', 'authorized_delivery']
        body.update(display_name='Подписка / доступ к ИИ-сервису', default_emoji='🤖',
            title_ru='{{sale_name}} — {{plan}} | {{duration}}',
            title_en='{{sale_name}} — {{plan}} | {{duration}}',
            description_ru='Подписка / доступ к сервису {{sale_name}}. Тариф: {{plan}}. Срок: {{duration}}. Регион / доступность: {{availability}}. Способ передачи: {{delivery_method}}. Подтверждение владельца: {{authorized_delivery}}. Только при фактической доступности и разрешении провайдера; перепродажа не считается разрешённой по умолчанию. Личные или общие аккаунты не предлагаются. Количество: {{quantity}}.',
            description_en='Subscription / service access: {{sale_name}}. Plan: {{plan}}. Duration: {{duration}}. Region / availability: {{availability}}. Delivery method: {{delivery_method}}. Owner confirmation: {{authorized_delivery}}. Only when actually available and permitted by the provider; resale permission is not assumed. No personal or shared accounts offered. Quantity: {{quantity}}.',
            validation_rules={'authorized_delivery': {'choices': [
                'Подтверждаю разрешение провайдера / I confirm provider permission']}})
    else:
        raise ProductValidationError('Unknown built-in product type')
    body['allowed_variables'] = [*VARIABLES, *extra]
    return body


def _variables(values, allowed):
    if not isinstance(values, dict) or set(values) - set(allowed):
        raise ProductValidationError("variables: unknown variable or invalid mapping")
    clean = {}
    for key, value in values.items():
        if key == "quantity" and isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        value = safe_text(value, key, 500)
        if "{" in value or "}" in value:
            raise ProductValidationError(f"{key}: braces are not allowed in variable values")
        if key == "quantity" and (not re.fullmatch(r"[0-9]{1,9}", value) or int(value) < 1):
            raise ProductValidationError("quantity: positive integer required")
        clean[key] = value
    return clean


def validate_overrides(overrides, allowed=VARIABLES):
    if not isinstance(overrides, dict) or set(overrides) - OVERRIDE_FIELDS:
        raise ProductValidationError("overrides: unknown field")
    clean = deepcopy(overrides)
    for key, value in clean.items():
        if key == "variables":
            clean[key] = _variables(value, allowed)
        else:
            safe_text(value, key, 64 if key == "emoji" else 200 if key.startswith("title_") else 10000)
            if key == "emoji" and ("\n" in value or "{" in value or "}" in value):
                raise ProductValidationError("emoji: invalid value")
            if key != "emoji":
                _validate_tokens(value, allowed)
    return clean


def validate_template(body):
    if not isinstance(body, dict):
        raise ProductValidationError("template: object required")
    fields = {"name", "display_name", "default_emoji", *TEXT_FIELDS, "allowed_variables", "defaults", "validation_rules"}
    metadata = {"id", "revision", "created_at", "updated_at"}
    if set(body) - fields - metadata - {'node_bindings'} or not fields <= set(body):
        raise ProductValidationError("template: unknown or missing fields")
    clean = deepcopy(body)
    safe_text(clean["name"], "name", 80)
    safe_text(clean["display_name"], "display_name", 120)
    validate_overrides({"emoji": clean["default_emoji"]})
    allowed = clean["allowed_variables"]
    if (not isinstance(allowed, list) or len(allowed) > 30
            or any(not isinstance(v, str) or not _NAME.fullmatch(v) for v in allowed)
            or len(set(allowed)) != len(allowed) or not set(VARIABLES) <= set(allowed)
            or set(allowed) & _RESERVED_VARIABLES):
        raise ProductValidationError("allowed_variables: unique names including standard variables required")
    clean["defaults"] = _variables(clean["defaults"], allowed)
    if 'node_bindings' in clean:
        clean['node_bindings'] = validate_node_bindings(clean['node_bindings'], allowed)
    for field in TEXT_FIELDS:
        text = safe_text(clean[field], field, 200 if field.startswith("title_") else 10000)
        _validate_tokens(text, allowed)
    rules = clean["validation_rules"]
    if not isinstance(rules, dict) or set(rules) - set(allowed):
        raise ProductValidationError("validation_rules: unknown variable")
    for variable, rule in rules.items():
        if not isinstance(rule, dict) or set(rule) - {"min_length", "max_length", "choices"}:
            raise ProductValidationError(f"{variable}: unsupported validation rule")
        for bound in ("min_length", "max_length"):
            if bound in rule and (type(rule[bound]) is not int or not 0 <= rule[bound] <= 500):
                raise ProductValidationError(f"{variable}: invalid length bound")
        if rule.get("min_length", 0) > rule.get("max_length", 500):
            raise ProductValidationError(f"{variable}: inconsistent length bounds")
        if "choices" in rule:
            if not isinstance(rule["choices"], list) or not 1 <= len(rule["choices"]) <= 100:
                raise ProductValidationError(f"{variable}: invalid choices")
            for choice in rule["choices"]:
                _variables({variable: choice}, allowed)
    return clean


def _validate_tokens(text, allowed):
    names = set(_TOKEN.findall(text))
    if names - set(allowed) or any(c in _TOKEN.sub("", text) for c in "{}"):
        raise ProductValidationError("template text: only controlled {{variable}} placeholders are allowed")
    return names


def validate_node_bindings(bindings, allowed):
    """Explicit content-variable -> technical field name, never a label guess."""
    if not isinstance(bindings, dict) or set(bindings) - set(allowed) or set(bindings) & {'game', 'quantity', 'product_name'}:
        raise ProductValidationError('node_bindings: invalid variable')
    for name in bindings.values():
        if not is_safe_node_binding_name(name):
            raise ProductValidationError('node_bindings: technical select name required')
    return deepcopy(bindings)


def validate_variable_value(template, variable, value):
    """Validate one picker/custom input against the same renderer constraints."""
    if variable == 'game' or variable not in template['allowed_variables']:
        raise ProductValidationError('variable: not editable')
    value = _variables({variable: value}, template['allowed_variables'])[variable]
    rule = template['validation_rules'].get(variable, {})
    if not rule.get('min_length', 0) <= len(value) <= rule.get('max_length', 500):
        raise ProductValidationError('variable: length rule failed')
    if 'choices' in rule and value not in rule['choices']:
        raise ProductValidationError('variable: value is not an allowed choice')
    return value


@dataclass(frozen=True)
class ProductTemplate:
    definition: dict

    def __post_init__(self):
        object.__setattr__(self, "definition", validate_template(self.definition))

    def to_dict(self):
        return deepcopy(self.definition)

    def render(self, variables=None, overrides=None):
        body = self.definition
        allowed = body["allowed_variables"]
        overrides = validate_overrides({} if overrides is None else overrides, allowed)
        values = dict(body["defaults"])
        values.update(_variables({} if variables is None else variables, allowed))
        values.update(overrides.get("variables", {}))
        for variable, rule in body["validation_rules"].items():
            if variable not in values:
                continue
            value = values[variable]
            if not rule.get("min_length", 0) <= len(value) <= rule.get("max_length", 500):
                raise ProductValidationError(f"{variable}: length rule failed")
            if "choices" in rule and value not in rule["choices"]:
                raise ProductValidationError(f"{variable}: value is not an allowed choice")
        rendered = {}
        for field in TEXT_FIELDS:
            text = overrides.get(field, body[field])
            required = _validate_tokens(text, allowed)
            if required - set(values):
                raise ProductValidationError("missing variables: " + ", ".join(sorted(required - set(values))))
            rendered[field] = safe_text(_TOKEN.sub(lambda m: values[m[1]], text), field,
                                        200 if field.startswith("title_") else 10000)
        emoji = overrides.get("emoji", body["default_emoji"])
        for field in ("title_ru", "title_en"):
            title = rendered[field]
            if not title.startswith(emoji):
                title = f"{emoji} {title}"
            rendered[field] = safe_text(title, field, 200)
        return {"variables": values, "emoji": emoji, **rendered}


class TemplateService:
    def __init__(self, store):
        self.store = store

    def ensure_builtin(self, kind=GAME_KEY):
        definition = builtin_template(kind)  # Fixed allowlist, never arbitrary IDs.
        # Prefer the stable scoped ID, preserving all owner edits including names.
        builtin = self.store.get_template(kind)
        if builtin is not None:
            return builtin
        offset = 0
        while True:
            page = self.store.list_templates(limit=100, offset=offset)
            for body in page:
                if body["name"] == kind:
                    return body
            if len(page) < 100:
                break
            offset += 100
        try:
            return self.save(definition, template_id=kind)
        except ValueError:
            # Concurrent first visits use the same scoped ID; keep the winner's definition.
            builtin = self.store.get_template(kind)
            if builtin is None:
                raise
            return builtin

    def list(self, **kwargs):
        return self.store.list_templates(**kwargs)

    def get(self, template_id):
        body = self.store.get_template(template_id)
        if body is None:
            raise ProductValidationError("template not found")
        return validate_template(body)

    def save(self, body, template_id=None, expected_revision=None):
        clean = validate_template(body)
        clean = {key: value for key, value in clean.items() if key not in {"id", "revision", "created_at", "updated_at"}}
        return self.store.save_template(clean, template_id=template_id, expected_revision=expected_revision)

    def delete(self, template_id, expected_revision=None):
        return self.store.delete_template(template_id, expected_revision=expected_revision)
