"""Static, built-in feature definitions and safe configuration resolution."""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


MODULES_VERSION = 1


@dataclass(frozen=True)
class FeatureDefinition:
    id: str
    title_ru: str
    description_ru: str
    default_enabled: bool = True  # Existing installations retain current behavior.
    restart_required: bool = True
    dependencies: tuple[str, ...] = ()


_FEATURES = (
    FeatureDefinition("autobump", "Автоподнятие", "Автоматически поднимает лоты."),
    FeatureDefinition("notifications", "Уведомления", "События для владельца в Telegram."),
    FeatureDefinition("night_mode", "Ночной режим", "Автоответы покупателям."),
    FeatureDefinition("review_request", "Запрос отзыва", "Сообщение после закрытия заказа."),
    FeatureDefinition("order_history", "История заказов", "Просмотр сохранённых заказов."),
    FeatureDefinition("statistics", "Статистика", "Сводка заказов и выводов."),
    FeatureDefinition("sales_analytics", "Аналитика продаж", "Анализ истории продаж."),
    FeatureDefinition("sales_import", "Импорт продаж", "Загрузка официального ZIP-экспорта."),
    FeatureDefinition("withdrawals", "Выводы", "Периодическая проверка выводов."),
    FeatureDefinition("logs_ui", "Логи", "Просмотр логов через Telegram."),
)


def _index(features: tuple[FeatureDefinition, ...]) -> dict[str, FeatureDefinition]:
    result = {}
    for feature in features:
        if feature.id in result:
            raise ValueError("Duplicate feature ID.")
        result[feature.id] = feature
    for feature in features:
        if any(dependency not in result for dependency in feature.dependencies):
            raise ValueError("Unknown feature dependency.")
    return result


_BY_ID = MappingProxyType(_index(_FEATURES))
SELLER_MODULES = frozenset({
    "notifications", "order_history", "statistics", "sales_analytics",
    "sales_import", "withdrawals", "logs_ui",
})
PROFILES = MappingProxyType({
    "minimal": frozenset({"notifications"}),
    "seller": SELLER_MODULES,
    "all": frozenset(_BY_ID),
})


def all_features() -> tuple[FeatureDefinition, ...]:
    return _FEATURES


def get_feature(feature_id: str) -> FeatureDefinition | None:
    return _BY_ID.get(feature_id)


def validate_modules_config(raw: object, *, fresh_default: bool = False) -> dict[str, bool]:
    """Ignore unknown IDs, but reject malformed values before changing runtime state."""
    if type(raw) is not dict:
        raise ValueError("Invalid modules configuration.")
    if any(type(value) is not bool for key, value in raw.items() if key in _BY_ID):
        raise ValueError("Invalid modules configuration.")
    result = {
        feature.id: raw.get(feature.id, False if fresh_default else feature.default_enabled)
        for feature in _FEATURES
    }
    for feature in _FEATURES:
        if result[feature.id] and any(not result[dependency] for dependency in feature.dependencies):
            raise ValueError("Unsatisfied feature dependency.")
    return result


def resolve_modules(raw: object, *, fresh_default: bool = False) -> Mapping[str, bool]:
    return MappingProxyType(validate_modules_config(raw, fresh_default=fresh_default))


def is_feature_enabled(snapshot: Mapping[str, bool], feature_id: str) -> bool:
    return feature_id in _BY_ID and snapshot.get(feature_id) is True


def profile_modules(profile: str) -> dict[str, bool]:
    selected = PROFILES[profile]
    return {feature.id: feature.id in selected for feature in _FEATURES}


def is_fresh_install(settings_path: Path, db_path: Path, legacy_stats_path: Path) -> bool:
    """Call before SQLite initialization creates a database for a new installation."""
    return not any(Path(path).exists() for path in (settings_path, db_path, legacy_stats_path))
