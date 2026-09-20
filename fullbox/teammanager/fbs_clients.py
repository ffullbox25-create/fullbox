from __future__ import annotations

from django.contrib import messages
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from billing.permissions import filter_agencies_for_user
from employees.access import RoleRequiredMixin
from fbs.exceptions import FbsError
from fbs.flags import feature_enabled
from fbs.integrations.warehouse_catalog import (
    client_marketplace_credentials,
    fetch_client_warehouse_catalog,
    marketplace_code,
)
from fbs.models import FbsIntegrationProfile, FbsSyncCursor
from fbs.services.client_profiles import (
    SelectedMarketplaceWarehouse,
    client_stock_overview,
    configure_client_fbs_profiles,
    normalize_safety_stock_qty,
    update_client_safety_stock_qty,
)
from fbs.services.sync import pull_profile_orders_manually
from sku.models import Agency, MarketCredential


FBS_MANAGER_ROLES = (
    "manager",
    "head_manager",
    "director",
    "admin",
    "developer",
)


def _visible_agencies(request):
    return filter_agencies_for_user(Agency.objects.all(), request)


def _visible_fbs_agency(request, agency_id: int) -> Agency:
    return get_object_or_404(
        _visible_agencies(request)
        .filter(
            Q(fbs_integration_profiles__isnull=False)
            | Q(market_credentials__market_key__isnull=False)
        )
        .distinct(),
        pk=agency_id,
    )


def _fbs_client_rows(request) -> list[dict]:
    visible_ids = set(_visible_agencies(request).values_list("id", flat=True))
    if not visible_ids:
        return []

    credentials = list(
        MarketCredential.objects.select_related("agency", "market")
        .filter(agency_id__in=visible_ids)
        .exclude(market_key__isnull=True)
        .exclude(market_key="")
        .order_by("agency__agn_name", "agency_id", "id")
    )
    agencies: dict[int, Agency] = {}
    marketplaces: dict[int, set[str]] = {}
    for credential in credentials:
        code = marketplace_code(credential.market.name)
        if not code:
            continue
        agencies[credential.agency_id] = credential.agency
        marketplaces.setdefault(credential.agency_id, set()).add(code)

    profile_agency_ids = set(
        FbsIntegrationProfile.objects.filter(agency_id__in=visible_ids).values_list(
            "agency_id", flat=True
        )
    )
    missing_agency_ids = profile_agency_ids - set(agencies)
    for agency in Agency.objects.filter(id__in=missing_agency_ids):
        agencies[agency.id] = agency

    profiles = list(
        FbsIntegrationProfile.objects.filter(agency_id__in=agencies)
        .prefetch_related(
            Prefetch(
                "sync_cursors",
                queryset=FbsSyncCursor.objects.filter(
                    stream=FbsSyncCursor.STREAM_ORDERS,
                    cursor_key="default",
                ),
                to_attr="order_sync_cursors",
            )
        )
        .order_by("agency_id", "marketplace", "name", "id")
    )
    profiles_by_agency: dict[int, list[FbsIntegrationProfile]] = {}
    for profile in profiles:
        profiles_by_agency.setdefault(profile.agency_id, []).append(profile)
        marketplaces.setdefault(profile.agency_id, set()).add(profile.marketplace)

    labels = dict(FbsIntegrationProfile.MARKETPLACE_CHOICES)
    result = []
    for agency in sorted(
        agencies.values(),
        key=lambda item: (str(item.agn_name or "").lower(), item.id),
    ):
        agency_profiles = profiles_by_agency.get(agency.id, [])
        active_profiles = [
            profile
            for profile in agency_profiles
            if profile.is_active and profile.order_pull_enabled
        ]
        cursors = [
            cursor
            for profile in active_profiles
            for cursor in getattr(profile, "order_sync_cursors", ())
        ]
        result.append(
            {
                "agency": agency,
                "marketplaces": [
                    {"code": code, "label": labels.get(code, code.upper())}
                    for code in sorted(marketplaces.get(agency.id, set()))
                ],
                "profile_count": len(agency_profiles),
                "active_profile_count": len(active_profiles),
                "fbs_enabled": bool(active_profiles),
                "warehouse_names": [profile.name for profile in active_profiles],
                "last_order_sync_at": max(
                    (cursor.last_success_at for cursor in cursors if cursor.last_success_at),
                    default=None,
                ),
                "last_order_sync_error": next(
                    (cursor.last_error for cursor in cursors if cursor.last_error),
                    "",
                ),
            }
        )
    return result


def _client_catalog(agency: Agency) -> dict[str, dict]:
    catalog = fetch_client_warehouse_catalog(agency)
    profiles = list(
        FbsIntegrationProfile.objects.filter(agency=agency).order_by(
            "marketplace", "name", "id"
        )
    )
    profiles_by_marketplace: dict[str, list[FbsIntegrationProfile]] = {}
    for profile in profiles:
        profiles_by_marketplace.setdefault(profile.marketplace, []).append(profile)
    for marketplace, entry in catalog.items():
        profile_rows = profiles_by_marketplace.get(marketplace, [])
        warehouse_map = {
            str(row["warehouse_id"]): row for row in entry.get("warehouses", [])
        }
        for profile in profile_rows:
            warehouse_id = str(profile.external_warehouse_id or "").strip()
            if warehouse_id and warehouse_id not in warehouse_map:
                warehouse_map[warehouse_id] = {
                    "marketplace": marketplace,
                    "warehouse_id": warehouse_id,
                    "name": profile.name,
                    "status": "Сохранён в Fullbox",
                }
        selected_ids = {
            str(profile.external_warehouse_id or "").strip()
            for profile in profile_rows
            if profile.is_active and profile.order_pull_enabled
        }
        stock_push_ids = {
            str(profile.external_warehouse_id or "").strip()
            for profile in profile_rows
            if (
                profile.is_active
                and profile.order_pull_enabled
                and profile.stock_push_enabled
                and profile.stock_mode == FbsIntegrationProfile.STOCK_MODE_MANAGED
            )
        }
        entry["warehouses"] = sorted(
            (
                {
                    **row,
                    "selected": warehouse_id in selected_ids,
                    "stock_push_selected": warehouse_id in stock_push_ids,
                }
                for warehouse_id, row in warehouse_map.items()
            ),
            key=lambda row: (str(row.get("name") or "").lower(), row["warehouse_id"]),
        )
        entry["configured_profiles"] = len(profile_rows)
        entry["active_profiles"] = len(selected_ids)
    return catalog


class TeamManagerFbsClientsView(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/fbs_clients.html"
    allowed_roles = FBS_MANAGER_ROLES

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = str(self.request.GET.get("q") or "").strip().lower()
        rows = _fbs_client_rows(self.request)
        if query:
            rows = [
                row
                for row in rows
                if query in str(row["agency"].agn_name or "").lower()
                or query in str(row["agency"].short_name or "").lower()
                or query in str(row["agency"].inn or "").lower()
                or query in str(row["agency"].id)
            ]
        context.update(
            {
                "active_nav": "fbs_clients",
                "rows": rows,
                "query": str(self.request.GET.get("q") or "").strip(),
                "summary": {
                    "clients": len(rows),
                    "active_clients": sum(bool(row["fbs_enabled"]) for row in rows),
                    "active_profiles": sum(row["active_profile_count"] for row in rows),
                },
            }
        )
        return context


class TeamManagerFbsClientSettingsView(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/fbs_client_settings.html"
    allowed_roles = FBS_MANAGER_ROLES

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agency = _visible_fbs_agency(self.request, kwargs["agency_id"])
        catalog = _client_catalog(agency)
        fbs_enabled = FbsIntegrationProfile.objects.filter(
            agency=agency,
            is_active=True,
            order_pull_enabled=True,
        ).exists()
        stock_overview = client_stock_overview(agency=agency)
        context.update(
            {
                "active_nav": "fbs_clients",
                "agency": agency,
                "catalog": [catalog[key] for key in ("wb", "ozon")],
                "fbs_enabled": fbs_enabled,
                "stock_overview": stock_overview,
                "profile_count": sum(
                    entry["configured_profiles"] for entry in catalog.values()
                ),
                "global_order_pull_enabled": feature_enabled("order_pull"),
            }
        )
        return context


class TeamManagerFbsClientSettingsUpdateView(RoleRequiredMixin, View):
    allowed_roles = FBS_MANAGER_ROLES

    def post(self, request, agency_id: int):
        agency = _visible_fbs_agency(request, agency_id)

        def redirect_to_settings():
            return redirect("team-manager-fbs-client-settings", agency_id=agency.id)

        action = str(request.POST.get("action") or "save").strip()
        if action == "sync":
            self._sync_orders(request, agency)
            return redirect_to_settings()

        enabled = request.POST.get("fbs_enabled") == "1"
        try:
            safety_stock_qty = normalize_safety_stock_qty(
                request.POST.get("safety_stock_qty", "0")
            )
        except FbsError as exc:
            messages.error(request, str(exc))
            return redirect_to_settings()
        catalog = _client_catalog(agency)
        credentials = client_marketplace_credentials(agency)
        selections = []
        stock_push_keys = set()
        for marketplace in ("wb", "ozon"):
            selected_warehouse_ids = {
                str(value or "").strip()
                for value in request.POST.getlist(f"warehouses_{marketplace}")
                if str(value or "").strip()
            }
            selected_stock_warehouse_ids = {
                str(value or "").strip()
                for value in request.POST.getlist(f"stock_push_warehouses_{marketplace}")
                if str(value or "").strip()
            }
            if selected_stock_warehouse_ids - selected_warehouse_ids:
                messages.error(
                    request,
                    "Выгрузку остатков можно включить только для выбранного FBS-склада.",
                )
                return redirect_to_settings()
            if selected_warehouse_ids:
                entry = catalog[marketplace]
                if not entry["credentials_configured"]:
                    messages.error(request, "Для выбранного маркетплейса не настроен API-ключ.")
                    return redirect_to_settings()
                if marketplace == "ozon" and not entry["client_id_configured"]:
                    messages.error(request, "Для Ozon не настроен Client ID.")
                    return redirect_to_settings()
            warehouse_map = {
                str(row["warehouse_id"]): row
                for row in catalog[marketplace].get("warehouses", [])
            }
            for warehouse_id in selected_warehouse_ids:
                row = warehouse_map.get(warehouse_id)
                if row is None:
                    messages.error(request, "Список складов изменился. Обновите страницу.")
                    return redirect_to_settings()
                credential = credentials.get(marketplace)
                external_account_id = (
                    str(credential.client_id or "").strip()
                    if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON and credential
                    else ""
                )
                selections.append(
                    SelectedMarketplaceWarehouse(
                        marketplace=marketplace,
                        warehouse_id=row["warehouse_id"],
                        name=row["name"],
                        external_account_id=external_account_id,
                    )
                )
                if warehouse_id in selected_stock_warehouse_ids:
                    stock_push_keys.add((marketplace, warehouse_id))

        try:
            result = configure_client_fbs_profiles(
                agency=agency,
                enabled=enabled,
                selections=tuple(selections),
                stock_push_keys=frozenset(stock_push_keys),
            )
        except FbsError as exc:
            messages.error(request, str(exc))
            return redirect_to_settings()

        update_client_safety_stock_qty(
            agency=agency,
            quantity=safety_stock_qty,
        )

        messages.success(
            request,
            "Настройки FBS сохранены: "
            f"активных складов {len(result.active_profile_ids)}, "
            f"создано {result.created}, обновлено {result.updated}, выключено {result.disabled}; "
            f"выгрузка остатков включена для {len(result.stock_push_profile_ids)} складов; "
            f"страховой остаток {safety_stock_qty} шт. на каждый SKU.",
        )
        if action == "save_sync" and result.active_profile_ids:
            self._sync_orders(request, agency)
        return redirect_to_settings()

    @staticmethod
    def _sync_orders(request, agency: Agency) -> None:
        profiles = list(
            FbsIntegrationProfile.objects.filter(
                agency=agency,
                is_active=True,
                order_pull_enabled=True,
            ).order_by("marketplace", "name", "id")
        )
        if not profiles:
            messages.warning(
                request,
                "Сначала включите FBS и выберите хотя бы один склад клиента.",
            )
            return
        totals = {
            "received": 0,
            "created": 0,
            "updated": 0,
            "duplicate": 0,
            "skipped": 0,
        }
        failures = []
        for profile in profiles:
            try:
                sync_result = pull_profile_orders_manually(
                    profile_id=profile.id,
                    actor=request.user,
                    limit=1000,
                )
            except FbsError as exc:
                failures.append(f"{profile.get_marketplace_display()}: {exc}")
                continue
            for key in totals:
                totals[key] += getattr(sync_result, key)
        if failures:
            messages.warning(
                request,
                f"Обновление выполнено частично. Ошибок: {len(failures)}. {failures[0]}",
            )
            return
        messages.success(
            request,
            "Заказы обновлены вручную: "
            f"получено {totals['received']}, новых {totals['created']}, "
            f"обновлено {totals['updated']}, без изменений {totals['duplicate']}, "
            f"пропущено {totals['skipped']}.",
        )
