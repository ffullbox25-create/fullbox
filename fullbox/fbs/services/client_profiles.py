from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Sum

from fbs.exceptions import FbsError
from fbs.models import (
    FbsClientStockPolicy,
    FbsIntegrationProfile,
    FbsStockBalance,
)
from sku.models import Agency

from .physical_locations import fbs_box_reservable_q

@dataclass(frozen=True)
class SelectedMarketplaceWarehouse:
    marketplace: str
    warehouse_id: str
    name: str
    external_account_id: str = ""


@dataclass(frozen=True)
class ClientProfileUpdateResult:
    enabled: bool
    stock_push_enabled: bool
    active_profile_ids: tuple[int, ...]
    stock_push_profile_ids: tuple[int, ...]
    created: int
    updated: int
    disabled: int


@dataclass(frozen=True)
class ClientStockOverview:
    actual_qty: int
    available_qty: int
    reserved_qty: int
    marketplace_qty: int
    sku_count: int
    safety_stock_qty: int


MAX_SAFETY_STOCK_QTY = 1_000_000


def normalize_safety_stock_qty(value) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        raise FbsError("Страховой остаток должен быть целым числом.")
    if not 0 <= quantity <= MAX_SAFETY_STOCK_QTY:
        raise FbsError(
            f"Страховой остаток должен быть от 0 до {MAX_SAFETY_STOCK_QTY} шт."
        )
    return quantity


def client_safety_stock_qty(*, agency_id: int) -> int:
    value = (
        FbsClientStockPolicy.objects.filter(agency_id=int(agency_id))
        .values_list("safety_stock_qty", flat=True)
        .first()
    )
    return max(int(value or 0), 0)


def client_stock_overview(*, agency: Agency) -> ClientStockOverview:
    balances = FbsStockBalance.objects.filter(
        agency=agency,
        sku_ref__isnull=False,
    ).filter(fbs_box_reservable_q())
    totals = balances.aggregate(
        actual_qty=Sum("qty"),
        available_qty=Sum("available_qty"),
        reserved_qty=Sum("reserved_qty"),
    )
    safety_stock_qty = client_safety_stock_qty(agency_id=agency.id)
    available_by_sku = balances.order_by().values("sku_ref_id").annotate(
        quantity=Sum("available_qty")
    )
    marketplace_qty = sum(
        max(int(row["quantity"] or 0) - safety_stock_qty, 0)
        for row in available_by_sku
    )
    return ClientStockOverview(
        actual_qty=int(totals["actual_qty"] or 0),
        available_qty=int(totals["available_qty"] or 0),
        reserved_qty=int(totals["reserved_qty"] or 0),
        marketplace_qty=marketplace_qty,
        sku_count=available_by_sku.count(),
        safety_stock_qty=safety_stock_qty,
    )


def update_client_safety_stock_qty(*, agency: Agency, quantity) -> int:
    safety_stock_qty = normalize_safety_stock_qty(quantity)
    with transaction.atomic():
        FbsClientStockPolicy.objects.update_or_create(
            agency=agency,
            defaults={"safety_stock_qty": safety_stock_qty},
        )

        def queue_stock_refresh():
            from .stock_sync import queue_agency_stock_exports

            queue_agency_stock_exports(agency_id=agency.id)

        transaction.on_commit(queue_stock_refresh, robust=True)
    return safety_stock_qty


def is_client_fbs_enabled(agency: Agency | None) -> bool:
    if agency is None or not agency.pk:
        return False
    return FbsIntegrationProfile.objects.filter(
        agency=agency,
        is_active=True,
        order_pull_enabled=True,
    ).exists()


def configure_client_fbs_profiles(
    *,
    agency: Agency,
    enabled: bool,
    selections: tuple[SelectedMarketplaceWarehouse, ...],
    stock_push_keys: frozenset[tuple[str, ...]] = frozenset(),
) -> ClientProfileUpdateResult:
    supported = dict(FbsIntegrationProfile.MARKETPLACE_CHOICES)
    normalized: dict[tuple[str, str, str], SelectedMarketplaceWarehouse] = {}
    for selection in selections:
        marketplace = str(selection.marketplace or "").strip()
        warehouse_id = str(selection.warehouse_id or "").strip()
        name = str(selection.name or warehouse_id).strip()
        external_account_id = str(selection.external_account_id or "").strip()
        if marketplace not in supported or not warehouse_id:
            raise FbsError("Выбран неизвестный склад маркетплейса.")
        if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON and not external_account_id:
            raise FbsError("Для склада Ozon не определён Client ID кабинета.")
        profile_key = (
            marketplace,
            warehouse_id,
            external_account_id
            if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
            else "",
        )
        normalized[profile_key] = SelectedMarketplaceWarehouse(
            marketplace=marketplace,
            warehouse_id=warehouse_id,
            name=name,
            external_account_id=external_account_id,
        )
    if enabled and not normalized:
        raise FbsError("Для включения FBS выберите хотя бы один склад WB или Ozon.")
    normalized_stock_push_keys = set()
    for raw_key in stock_push_keys:
        if len(raw_key) not in {2, 3}:
            raise FbsError("Некорректно указан FBS-склад для выгрузки остатков.")
        marketplace = str(raw_key[0] or "").strip()
        warehouse_id = str(raw_key[1] or "").strip()
        external_account_id = str(raw_key[2] or "").strip() if len(raw_key) == 3 else ""
        if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON and not external_account_id:
            matching_keys = {
                key for key in normalized if key[:2] == (marketplace, warehouse_id)
            }
            if len(matching_keys) == 1:
                normalized_stock_push_keys.update(matching_keys)
                continue
        normalized_stock_push_keys.add(
            (
                marketplace,
                warehouse_id,
                external_account_id
                if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
                else "",
            )
        )
    if not enabled:
        normalized_stock_push_keys.clear()
    unknown_stock_keys = normalized_stock_push_keys - set(normalized)
    if unknown_stock_keys:
        raise FbsError("Выгрузку остатков можно включить только для выбранного FBS-склада.")

    created = 0
    updated = 0
    disabled = 0
    active_profile_ids: list[int] = []
    stock_push_profile_ids: list[int] = []
    with transaction.atomic():
        existing = list(
            FbsIntegrationProfile.objects.select_for_update()
            .filter(agency=agency)
            .order_by("id")
        )
        if not enabled:
            for profile in existing:
                changed = []
                for field, value in (
                    ("is_active", False),
                    ("order_pull_enabled", False),
                    ("outbox_enabled", False),
                    ("stock_push_enabled", False),
                    ("stock_mode", FbsIntegrationProfile.STOCK_MODE_DISABLED),
                ):
                    if getattr(profile, field) != value:
                        setattr(profile, field, value)
                        changed.append(field)
                if changed:
                    profile.full_clean(exclude={"id"})
                    profile.save(update_fields=[*changed, "updated_at"])
                    disabled += 1
            return ClientProfileUpdateResult(False, False, (), (), 0, 0, disabled)

        profiles_by_key = {
            (
                profile.marketplace,
                str(profile.external_warehouse_id).strip(),
                str(profile.external_account_id or "").strip()
                if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
                else "",
            ): profile
            for profile in existing
        }
        for key, selection in normalized.items():
            profile = profiles_by_key.get(key)
            profile_name = f"{supported[selection.marketplace]} · {selection.name}"[:128]
            stock_push_enabled = key in normalized_stock_push_keys
            stock_mode = (
                FbsIntegrationProfile.STOCK_MODE_MANAGED
                if stock_push_enabled
                else FbsIntegrationProfile.STOCK_MODE_DISABLED
            )
            if profile is None:
                profile = FbsIntegrationProfile(
                    agency=agency,
                    marketplace=selection.marketplace,
                    name=profile_name,
                    external_account_id=selection.external_account_id,
                    external_warehouse_id=selection.warehouse_id,
                    stock_mode=stock_mode,
                    is_active=True,
                    order_pull_enabled=True,
                    status_pull_enabled=(
                        selection.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
                    ),
                    outbox_enabled=True,
                    marking_push_enabled=True,
                    stock_push_enabled=stock_push_enabled,
                )
                profile.full_clean(exclude={"id"})
                profile.save()
                created += 1
            else:
                changed = []
                for field, value in (
                    ("name", profile_name),
                    ("external_account_id", selection.external_account_id),
                    ("is_active", True),
                    ("order_pull_enabled", True),
                    ("outbox_enabled", True),
                    ("marking_push_enabled", True),
                    (
                        "status_pull_enabled",
                        profile.status_pull_enabled
                        or selection.marketplace == FbsIntegrationProfile.MARKETPLACE_WB,
                    ),
                    ("stock_mode", stock_mode),
                    ("stock_push_enabled", stock_push_enabled),
                ):
                    if getattr(profile, field) != value:
                        setattr(profile, field, value)
                        changed.append(field)
                if changed:
                    profile.full_clean(exclude={"id"})
                    profile.save(update_fields=[*changed, "updated_at"])
                    updated += 1
            active_profile_ids.append(profile.id)
            if stock_push_enabled:
                stock_push_profile_ids.append(profile.id)

        selected_keys = set(normalized)
        for profile in existing:
            key = (
                profile.marketplace,
                str(profile.external_warehouse_id).strip(),
                str(profile.external_account_id or "").strip()
                if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
                else "",
            )
            if key in selected_keys:
                continue
            changed = []
            for field, value in (
                ("is_active", False),
                ("order_pull_enabled", False),
                ("outbox_enabled", False),
                ("stock_push_enabled", False),
                ("stock_mode", FbsIntegrationProfile.STOCK_MODE_DISABLED),
            ):
                if getattr(profile, field) != value:
                    setattr(profile, field, value)
                    changed.append(field)
            if changed:
                profile.full_clean(exclude={"id"})
                profile.save(update_fields=[*changed, "updated_at"])
                disabled += 1

        if stock_push_profile_ids:
            def queue_initial_stock_export():
                from .stock_sync import queue_agency_stock_exports

                queue_agency_stock_exports(agency_id=agency.id)

            transaction.on_commit(queue_initial_stock_export, robust=True)

    return ClientProfileUpdateResult(
        True,
        bool(stock_push_profile_ids),
        tuple(active_profile_ids),
        tuple(stock_push_profile_ids),
        created,
        updated,
        disabled,
    )
