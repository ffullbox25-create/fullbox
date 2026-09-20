from django.conf import settings


FEATURE_SETTINGS = {
    "module": "FBS_MODULE_ENABLED",
    "order_pull": "FBS_ORDER_PULL_ENABLED",
    "status_pull": "FBS_STATUS_PULL_ENABLED",
    "outbox": "FBS_OUTBOX_ENABLED",
    "warehouse_writes": "FBS_WAREHOUSE_WRITES_ENABLED",
    "marking_push": "FBS_MARKING_PUSH_ENABLED",
    "stock_push": "FBS_STOCK_PUSH_ENABLED",
    "billing": "FBS_BILLING_ENABLED",
}


def module_enabled() -> bool:
    return bool(getattr(settings, FEATURE_SETTINGS["module"], False))


def feature_enabled(feature: str) -> bool:
    setting_name = FEATURE_SETTINGS.get(feature)
    if setting_name is None:
        raise ValueError(f"Unknown FBS feature: {feature}")
    if feature == "module":
        return module_enabled()
    return module_enabled() and bool(getattr(settings, setting_name, False))


def feature_snapshot() -> dict[str, bool]:
    return {feature: feature_enabled(feature) for feature in FEATURE_SETTINGS}
