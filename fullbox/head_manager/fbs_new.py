"""Read-only entry point for the parallel Fullbox WMS workspace."""

from __future__ import annotations

import csv
from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
from uuid import uuid4

from openpyxl import Workbook

from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View
from django.views.generic import TemplateView

from audit.models import AuditEntry, AuditJournal
from employees.access import (
    RoleRequiredMixin,
    get_request_employee,
    is_developer_login,
    request_has_any_role,
    resolve_cabinet_url,
)
from employees.models import Employee
from fbs.models import FbsBox, FbsPallet, FbsPickException, FbsStorageCell
from logistics.models import LogisticsTrip
from sklad.models import WarehouseLocation
from sku.models import Agency
from todo.models import Task
from wms_new.models import (
    WmsNewAssemblySession,
    WmsNewExtraFieldDefinition,
    WmsNewBox,
    WmsNewDocument,
    WmsNewInventorySession,
    WmsNewMarkingCode,
    WmsNewMarketplaceStock,
    WmsNewBillingItem,
    WmsNewInvoice,
    WmsNewPartnerProfile,
    WmsNewPrimaryDocument,
    WmsNewLogisticsManifest,
    WmsNewLogisticsOrder,
    WmsNewLogisticsPackage,
    WmsNewMovement,
    WmsNewOrder,
    WmsNewProduct,
    WmsNewRecord,
    WmsNewEvent,
    WmsNewReturn,
    WmsNewShipment,
    WmsNewTask,
    WmsNewWave,
    WmsNewWaveAllocation,
)
from wms_new.services.extra_fields import (
    ExtraFieldOperationError,
    create_definition,
    set_definition_active,
    set_product_value,
    update_definition,
)
from wms_new.services.boxes import (
    BoxOperationError,
    create_box,
    set_box_archived,
    update_box,
)
from wms_new.services.marking import (
    MarkingOperationError,
    add_codes,
    mark_printed,
    return_codes,
)
from wms_new.services.marking_excel import export_marking_codes
from wms_new.services.inventory import (
    InventoryOperationError,
    activate_inventory,
    approve_inventory,
    cancel_inventory,
    create_inventory,
    finish_inventory_count,
    record_inventory_scan,
)
from wms_new.services.orders import (
    OrderOperationError,
    apply_bulk_action,
    create_manual_order,
    launch_wave,
    update_order,
    update_wave,
)
from wms_new.services.tasks import (
    TaskOperationError,
    add_task_item,
    add_task_service,
    apply_board_action,
    assign_task_items_to_box,
    attach_task_file,
    create_task,
    create_task_box,
    finalize_task_tariff,
    record_task_item,
    remove_task_item,
    remove_task_service,
    save_task_details,
    set_task_box_status,
    set_task_client_confirmation,
    update_task,
)
from wms_new.services.assembly import (
    AssemblyOperationError,
    move_assembly_problem,
    scan_assembly_order_label,
    scan_assembly_product,
    start_assembly_session,
)
from wms_new.services.picking import (
    PickingOperationError,
    cancel_wave,
    finish_collection,
    remove_order_from_wave,
    scan_product as scan_picking_product,
    scan_source_place,
    skip_current_allocation,
    start_collection,
)
from wms_new.services.product_excel import export_products, import_products
from wms_new.services.shipments import (
    ShipmentOperationError,
    add_box as add_shipment_box,
    add_order as add_shipment_order,
    add_service as add_shipment_service,
    create_shipment,
    finalize_tariff,
    remove_box as remove_shipment_box,
    remove_order as remove_shipment_order,
    remove_service as remove_shipment_service,
    send_all_checked,
    transition_shipment,
    update_order_check as update_shipment_order_check,
    verify_order as verify_shipment_order,
)
from wms_new.services.returns import (
    ReturnOperationError,
    complete_return,
    create_return,
    record_return_line,
    start_receiving,
    update_returns,
)
from wms_new.services.bundles import (
    BundleOperationError,
    assemble_bundle,
    define_bundle,
    disassemble_bundle,
)
from wms_new.services.movements import (
    MovementOperationError,
    move_all_from_location,
    move_product,
)
from wms_new.services.logistics import (
    LogisticsOperationError,
    accept_order as accept_logistics_order,
    add_orders_to_package,
    create_manifest as create_logistics_manifest,
    create_package as create_logistics_package,
    delete_orders as delete_logistics_orders,
    import_fbs_shipments,
    measure_order as measure_logistics_order,
    remove_orders_from_package,
    save_route_rules,
    transition_manifest,
    update_packages,
)
from wms_new.services.documents import (
    DocumentOperationError,
    add_item as add_document_item,
    cancel_document,
    create_document,
    post_document,
    remove_item as remove_document_item,
    update_comments as update_document_comments,
)
from wms_new.services.reports import REPORT_TITLES, build_report
from wms_new.services.marketplace_analytics import (
    MarketplaceStockSyncError,
    refresh_marketplace_stocks,
)
from wms_new.services.partner_billing import (
    PartnerBillingOperationError,
    create_invoice,
    create_invoice_from_document,
    create_partner,
    update_billing_sources,
    update_invoice,
    update_partner,
)
from wms_new.services.products import (
    ProductOperationError,
    assign_category,
    create_product,
    merge_products,
    update_product,
)
from wms_new.services.settings import (
    SettingsOperationError,
    advance_print_job,
    archive_record,
    create_billing_request,
    create_development_task,
    create_print_job,
    create_printer_workstation,
    create_user_invitation,
    rotate_printer_token,
    save_directory_item,
    save_label_template,
    save_place_overlay,
    save_product_mapping,
    save_system_section,
    save_system_settings,
    save_user_overlay,
)

from .fbs_new_data import (
    build_fbs_order_detail_data,
    build_fbs_return_detail_data,
    build_fbs_shipment_detail_data,
    build_fbs_wave_detail_data,
    build_logistics_manifest_detail_data,
    build_logistics_package_detail_data,
    build_task_detail_data,
    build_section_data,
)


FBS_NEW_ACCESS_ROLE = "fbs_new"
INVENTORY_COUNTER_ROLES = ("storekeeper", "head_manager", "director", "admin")
INVENTORY_MANAGER_ROLES = ("head_manager", "director", "admin")


WMS_NEW_MODULES = (
    {
        "slug": "home",
        "label": "Главная",
        "icon": "mdi mdi-home-outline",
        "description": "Единая оперативная сводка новой WMS.",
        "legacy_url": "/head-manager/",
    },
    {
        "slug": "problems",
        "label": "Проблемы",
        "icon": "mdi mdi-alert-outline",
        "description": "Исключения, просрочки и блокировки складских процессов.",
        "legacy_url": "/todo/?status=backlog",
        "metric_key": "problems",
    },
    {
        "slug": "tasks",
        "label": "Задачи",
        "icon": "mdi mdi-format-list-checks",
        "description": "Очереди работ, исполнители и контроль выполнения.",
        "legacy_url": "/todo/",
        "metric_key": "tasks",
        "default_slug": "tasks-list",
        "children": (
            {"slug": "tasks-list", "label": "Список задач"},
            {"slug": "tasks-acceptance", "label": "Приемка"},
            {"slug": "tasks-processing", "label": "Обработка"},
            {"slug": "tasks-shipment", "label": "Отгрузка"},
            {"slug": "tasks-other", "label": "Прочие задачи"},
            {"slug": "tasks-multiacceptance", "label": "Мультиприемки"},
        ),
    },
    {
        "slug": "fbs",
        "label": "FBS",
        "icon": "mdi mdi-cart-outline",
        "description": "Заказы, волны, подбор, упаковка, передача и возвраты FBS.",
        "legacy_url": "/head-manager/fbs/",
        "metric_key": "fbs",
        "children": (
            {"slug": "fbs-orders", "label": "Заказы"},
            {"slug": "fbs-queue", "label": "Очередь"},
            {"slug": "fbs-waves", "label": "Волны"},
            {"slug": "fbs-assembly", "label": "Сборка заказов"},
            {"slug": "fbs-shipments", "label": "Отгрузки"},
            {"slug": "fbs-returns", "label": "Возвраты"},
        ),
    },
    {
        "slug": "warehouse",
        "label": "Склад",
        "icon": "mdi mdi-package-variant-closed",
        "description": "Остатки, места, короба, палеты, приемка и перемещения.",
        "legacy_url": "/head-manager/stock-editor/",
        "metric_key": "warehouse",
        "default_slug": "warehouse-goods",
        "children": (
            {"slug": "warehouse-goods", "label": "Товары"},
            {"slug": "warehouse-acceptances", "label": "Приемки"},
            {"slug": "warehouse-marking", "label": "Коды маркировки"},
            {"slug": "warehouse-extra-fields", "label": "Дополнительные поля"},
            {"slug": "warehouse-inventories", "label": "Инвентаризации"},
            {"slug": "warehouse-boxes", "label": "Коробы"},
            {"slug": "warehouse-bundles", "label": "Наборы"},
            {"slug": "warehouse-movement", "label": "Перемещение"},
            {"slug": "warehouse-history", "label": "История движений"},
        ),
    },
    {
        "slug": "logistics",
        "label": "Логистика",
        "icon": "mdi mdi-truck",
        "description": "Рейсы, погрузка, перевозчики и контроль доставки.",
        "legacy_url": "/logistics/trips/",
        "metric_key": "logistics",
        "default_slug": "logistics-orders",
        "children": (
            {"slug": "logistics-orders", "label": "Отправления"},
            {"slug": "logistics-shipments", "label": "Грузоместа"},
            {"slug": "logistics-trips", "label": "Рейсы"},
            {"slug": "logistics-settings", "label": "Настройки"},
        ),
    },
    {
        "slug": "documents",
        "label": "Документы",
        "icon": "mdi mdi-file-document-outline",
        "description": "Приходные, расходные, транспортные и контрольные документы.",
        "legacy_url": "/audit/orders/",
        "default_slug": "documents-receipt",
        "children": (
            {"slug": "documents-receipt", "label": "Приход"},
            {"slug": "documents-writeoff", "label": "Расход"},
        ),
    },
    {
        "slug": "reports",
        "label": "Отчеты",
        "icon": "mdi mdi-chart-bar",
        "description": "Операционная и управленческая отчетность.",
        "legacy_url": "/head-manager/reports/",
        "default_slug": "reports-goods",
        "children": (
            {"slug": "reports-goods", "label": "По товарам"},
            {"slug": "reports-goods-places", "label": "Товары по местам"},
            {"slug": "reports-places", "label": "Занятые места"},
            {"slug": "reports-returns", "label": "Возвраты с МП"},
            {"slug": "reports-shipments", "label": "Отгруженные товары"},
            {"slug": "reports-tasks", "label": "По задачам"},
            {"slug": "reports-fbs-status", "label": "Статус сборки"},
            {"slug": "reports-fbs-count", "label": "Количество заказов"},
            {"slug": "reports-fbs-orders", "label": "Отгруженные заказы"},
            {"slug": "reports-fbs-goods", "label": "Отгруженные товары (FBS)"},
            {"slug": "reports-invoices", "label": "Счета и оплаты"},
        ),
    },
    {
        "slug": "analytics",
        "label": "Аналитика",
        "icon": "mdi mdi-chart-bar",
        "description": "Динамика запасов, производительности и SLA.",
        "legacy_url": "/head-manager/reports/",
        "children": (
            {"slug": "analytics-marketplace-stocks", "label": "Остатки по складам МП"},
        ),
    },
    {
        "slug": "partners",
        "label": "Партнеры",
        "icon": "mdi mdi-account-group-outline",
        "description": "Клиенты, интеграции и параметры обслуживания.",
        "legacy_url": "/head-manager/clients/",
        "metric_key": "partners",
        "default_slug": "partners-list",
        "children": (
            {"slug": "partners-list", "label": "Партнеры"},
            {"slug": "partners-billing-tasks", "label": "Тарификация задач"},
            {"slug": "partners-billing-storage", "label": "Тарификация хранения"},
            {"slug": "partners-billing-fbs", "label": "Тарификация FBS"},
            {"slug": "partners-invoices", "label": "Счета"},
            {"slug": "partners-primary-docs", "label": "Первичные документы"},
        ),
    },
    {
        "slug": "crm",
        "label": "CRM",
        "icon": "mdi mdi-card-account-details-outline",
        "description": "Коммуникации и история взаимодействия с клиентами.",
        "legacy_url": "",
        "children": (
            {"slug": "crm-contacts", "label": "Контакты"},
            {"slug": "crm-suppliers", "label": "Поставщики"},
            {"slug": "crm-leads", "label": "Потенциальные клиенты"},
        ),
    },
    {
        "slug": "settings",
        "label": "Настройки",
        "icon": "mdi mdi-cog-outline",
        "description": "Оборудование, интеграции, роли и правила новой WMS.",
        "legacy_url": "/head-manager/fbs/settings/",
        "default_slug": "settings-system",
        "children": (
            {"slug": "settings-billing", "label": "Биллинг WMS"},
            {"slug": "settings-users", "label": "Пользователи"},
            {"slug": "settings-places", "label": "Места хранения"},
            {"slug": "settings-printers", "label": "Принтеры"},
            {"slug": "settings-directories", "label": "Справочники"},
            {"slug": "settings-system", "label": "Настройки системы"},
            {"slug": "settings-history", "label": "История операций"},
            {"slug": "settings-labels", "label": "Шаблоны этикеток"},
            {"slug": "settings-matching", "label": "Сопоставление товаров"},
            {"slug": "settings-pilot", "label": "Пилот FBS-NEW"},
        ),
    },
    {
        "slug": "help",
        "label": "Помощь",
        "icon": "mdi mdi-help-rhombus-outline",
        "description": "Поддержка, инструкции и задачи на доработку.",
        "legacy_url": "/development-journal/",
        "children": (
            {"slug": "support", "label": "Поддержка"},
            {"slug": "instructions", "label": "Инструкции"},
            {"slug": "development", "label": "Задачи на доработку"},
        ),
    },
)


class HeadManagerFbsNewView(RoleRequiredMixin, TemplateView):
    """Parallel, non-mutating WMS shell used while the new workspace is tested."""

    template_name = "head_manager/fbs_new.html"
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_slug = str(self.kwargs.get("section") or "home").strip().lower()
        modules = deepcopy(WMS_NEW_MODULES)
        sections = {module["slug"]: module for module in modules}
        for module in modules:
            for child in module.get("children", ()):
                sections[child["slug"]] = {
                    **child,
                    "icon": module["icon"],
                    "description": module["description"],
                    "legacy_url": module["legacy_url"],
                    "parent_slug": module["slug"],
                }
        if active_slug not in sections:
            raise Http404("Раздел новой WMS не найден")

        stock = WmsNewProduct.objects.filter(is_archived=False).aggregate(
            total=Sum("stock_on_hand"), available=Sum("stock_free")
        )
        active_trip_statuses = (
            LogisticsTrip.STATUS_PLANNED,
            LogisticsTrip.STATUS_LOADING,
            LogisticsTrip.STATUS_DEPARTED,
        )
        metrics = {
            "problems": Task.objects.filter(status="backlog").count()
            + FbsPickException.objects.filter(status=FbsPickException.STATUS_OPEN).count(),
            "tasks": WmsNewTask.objects.exclude(status=WmsNewTask.STATUS_DONE).count(),
            "fbs": WmsNewOrder.objects.exclude(
                status__in=(
                    WmsNewOrder.STATUS_DONE,
                    WmsNewOrder.STATUS_CANCELLED,
                    WmsNewOrder.STATUS_RETURNED,
                )
            ).count(),
            "warehouse": int(stock.get("available") or 0),
            "warehouse_total": int(stock.get("total") or 0),
            "logistics": LogisticsTrip.objects.filter(status__in=active_trip_statuses).count(),
            "partners": Agency.objects.filter(archived=False).count(),
        }
        today = timezone.localdate()
        seven_days_ago = today - timedelta(days=6)
        open_acceptance = WmsNewTask.objects.filter(
            workflow_type=WmsNewTask.TYPE_ACCEPTANCE
        ).exclude(status=WmsNewTask.STATUS_DONE)
        controlled_orders = WmsNewOrder.objects.exclude(
            status__in=(
                WmsNewOrder.STATUS_DONE,
                WmsNewOrder.STATUS_CANCELLED,
                WmsNewOrder.STATUS_RETURNED,
            )
        )
        next_deadline = controlled_orders.filter(cutoff_at__gte=timezone.now()).order_by(
            "cutoff_at"
        ).values_list("cutoff_at", flat=True).first()
        minutes_to_deadline = None
        if next_deadline:
            minutes_to_deadline = max(
                0, int((next_deadline - timezone.now()).total_seconds() // 60)
            )
        home_widgets = (
            {
                "title": "SLA приемки поставок",
                "value": open_acceptance.count(),
                "hint": f"просрочено: {open_acceptance.filter(due_date__lt=timezone.now()).count()}",
                "tone": "blue",
            },
            {
                "title": "SLA по заказам",
                "value": controlled_orders.filter(cutoff_at__lt=timezone.now()).count(),
                "hint": "заказов с истекшим дедлайном",
                "tone": "orange",
            },
            {
                "title": "Заказы на контроле",
                "value": controlled_orders.filter(
                    status=WmsNewOrder.STATUS_EXCEPTION
                ).count(),
                "hint": "требуют решения",
                "tone": "red",
            },
            {
                "title": "Отмены заказов (сегодня)",
                "value": WmsNewOrder.objects.filter(
                    status=WmsNewOrder.STATUS_CANCELLED,
                    updated_at__date=today,
                ).count(),
                "hint": "по данным FBS-NEW",
                "tone": "gray",
            },
            {
                "title": "Отмены заказов (7 дн.)",
                "value": WmsNewOrder.objects.filter(
                    status=WmsNewOrder.STATUS_CANCELLED,
                    updated_at__date__gte=seven_days_ago,
                ).count(),
                "hint": "скользящее окно",
                "tone": "gray",
            },
            {
                "title": "Таймер дедлайна заказов",
                "value": "—" if minutes_to_deadline is None else f"{minutes_to_deadline // 60:02d}:{minutes_to_deadline % 60:02d}",
                "hint": "до ближайшего дедлайна",
                "tone": "purple",
            },
        )
        for module in modules:
            metric_key = module.get("metric_key")
            module["metric"] = metrics.get(metric_key) if metric_key else None
            module["state"] = "Работает"
            module["active"] = active_slug == module["slug"]
            child_slugs = {child["slug"] for child in module.get("children", ())}
            module["expanded"] = module["active"] or active_slug in child_slugs
            for child in module.get("children", ()):
                child["active"] = active_slug == child["slug"]

        current_section = sections[active_slug]
        request_employee = get_request_employee(self.request)
        can_manage_pilot = is_developer_login(self.request.user) or request_has_any_role(
            self.request, ("head_manager",)
        )
        order_id = self.kwargs.get("order_id")
        wave_id = self.kwargs.get("wave_id")
        shipment_id = self.kwargs.get("shipment_id")
        return_id = self.kwargs.get("return_id")
        package_id = self.kwargs.get("package_id")
        manifest_id = self.kwargs.get("manifest_id")
        task_id = self.kwargs.get("task_id")
        if active_slug == "fbs-orders" and order_id is not None:
            section_data = build_fbs_order_detail_data(int(order_id))
            if section_data is None:
                raise Http404("Заказ FBS-NEW не найден")
            current_section = {
                **current_section,
                "label": section_data["title"],
            }
        elif active_slug == "fbs-waves" and wave_id is not None:
            section_data = build_fbs_wave_detail_data(
                int(wave_id),
                collection_mode=bool(self.kwargs.get("collection_mode")),
            )
            if section_data is None:
                raise Http404("Волна FBS-NEW не найдена")
            current_section = {
                **current_section,
                "label": section_data["title"],
            }
        elif active_slug == "fbs-shipments" and shipment_id is not None:
            section_data = build_fbs_shipment_detail_data(int(shipment_id))
            if section_data is None:
                raise Http404("Отгрузка FBS-NEW не найдена")
            current_section = {
                **current_section,
                "label": section_data["title"],
            }
        elif active_slug == "fbs-returns" and return_id is not None:
            section_data = build_fbs_return_detail_data(int(return_id))
            if section_data is None:
                raise Http404("Возврат FBS-NEW не найден")
            current_section = {
                **current_section,
                "label": section_data["title"],
            }
        elif active_slug == "logistics-shipments" and package_id is not None:
            section_data = build_logistics_package_detail_data(int(package_id))
            if section_data is None:
                raise Http404("Грузоместо FBS-NEW не найдено")
            current_section = {**current_section, "label": section_data["title"]}
        elif active_slug == "logistics-trips" and manifest_id is not None:
            section_data = build_logistics_manifest_detail_data(int(manifest_id))
            if section_data is None:
                raise Http404("Рейс FBS-NEW не найден")
            current_section = {**current_section, "label": section_data["title"]}
        elif active_slug == "tasks-list" and task_id is not None:
            section_data = build_task_detail_data(int(task_id), request=self.request)
            if section_data is None:
                raise Http404("Задача FBS-NEW не найдена")
            current_section = {**current_section, "label": section_data["title"]}
        else:
            section_data = (
                build_section_data(active_slug, request=self.request)
                if active_slug != "home"
                else {}
            )
            if section_data.get("kind") in {"wms_report", "marketplace_analytics"}:
                current_section = {
                    **current_section,
                    "label": section_data["title"],
                }
            elif (
                section_data.get("kind") == "documents_registry"
                and section_data.get("selected_document") is not None
            ):
                current_section = {
                    **current_section,
                    "label": section_data["title"],
                }
        if section_data:
            section_data["actions"] = tuple(
                action
                for action in section_data.get("actions", ())
                if not isinstance(action, dict)
                or str(action.get("url") or "").startswith("/head-manager/fbs-new/")
            )
            for row in section_data.get("rows", ()):
                if not isinstance(row, dict):
                    continue
                if not str(row.get("url") or "").startswith("/head-manager/fbs-new/"):
                    row["url"] = ""
        pilot_access_rows = []
        if active_slug in {"settings", "settings-pilot"} and can_manage_pilot:
            role_labels = dict(Employee.ROLE_CHOICES)
            for employee in Employee.objects.filter(
                is_active=True,
                user__isnull=False,
            ).select_related("user").order_by("full_name", "id"):
                primary_access = employee.role == "head_manager"
                pilot_enabled = primary_access or employee.has_role(FBS_NEW_ACCESS_ROLE)
                pilot_access_rows.append(
                    {
                        "id": employee.id,
                        "full_name": employee.full_name,
                        "username": employee.user.username,
                        "role": role_labels.get(employee.role, employee.role),
                        "old_cabinet_url": resolve_cabinet_url(employee.role),
                        "primary_access": primary_access,
                        "pilot_enabled": pilot_enabled,
                        "can_toggle": not primary_access,
                    }
                )

        context.update(
            {
                "active_slug": active_slug,
                "current_section": current_section,
                "metrics": metrics,
                "home_widgets": home_widgets,
                "modules": modules,
                "section_data": section_data,
                "home_url": (
                    "/head-manager/"
                    if request_employee
                    and (
                        request_employee.role == "head_manager"
                        or is_developer_login(self.request.user)
                    )
                    else (
                        resolve_cabinet_url(request_employee.role)
                        if request_employee
                        else "/head-manager/"
                    )
                ),
                "is_pilot_user": bool(request_employee and request_employee.role != "head_manager"),
                "can_manage_pilot": can_manage_pilot,
                "can_count_inventory": bool(
                    is_developer_login(self.request.user)
                    or (
                        request_employee
                        and request_employee.role in INVENTORY_COUNTER_ROLES
                    )
                ),
                "can_manage_inventory": bool(
                    is_developer_login(self.request.user)
                    or (
                        request_employee
                        and request_employee.role in INVENTORY_MANAGER_ROLES
                    )
                ),
                "pilot_access_rows": pilot_access_rows,
                "user_display_name": self.request.user.get_full_name()
                or self.request.user.get_username(),
            }
        )
        return context


class HeadManagerFbsNewSettingsActionView(RoleRequiredMixin, View):
    """Mutate only isolated settings overlays used by the parallel contour."""

    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, section: str):
        action = str(request.POST.get("action") or "save").strip().lower()
        target = f"/head-manager/fbs-new/settings-{section}/"
        try:
            if section == "billing":
                create_billing_request(
                    request_type=action,
                    amount=request.POST.get("amount"),
                    actor=request.user,
                )
            elif section == "users":
                if action == "invite":
                    create_user_invitation(
                        name=request.POST.get("name"),
                        role=request.POST.get("role"),
                        email=request.POST.get("email"),
                        actor=request.user,
                    )
                else:
                    employee = get_object_or_404(Employee, pk=request.POST.get("employee_id"))
                    save_user_overlay(
                        employee=employee,
                        notifications=request.POST.get("notifications") == "1",
                        active=request.POST.get("active") == "1",
                        actor=request.user,
                    )
                target += "?tab=" + str(request.POST.get("return_tab") or "employees")
            elif section == "places":
                save_place_overlay(
                    source_key=request.POST.get("source_key"),
                    title=request.POST.get("title"),
                    active=request.POST.get("active") == "1",
                    payload={
                        "code": str(request.POST.get("code") or "").strip(),
                        "warehouse": str(request.POST.get("warehouse") or "MSK").strip(),
                        "zone": str(request.POST.get("zone") or "NEW").strip(),
                        "kind": str(request.POST.get("kind") or "Хранение").strip(),
                        "row": str(request.POST.get("row") or "0").strip(),
                        "section": str(request.POST.get("place_section") or "0").strip(),
                        "tier": str(request.POST.get("tier") or "0").strip(),
                        "cell": str(request.POST.get("cell") or "0").strip(),
                    },
                    actor=request.user,
                )
                target += "?tab=" + str(request.POST.get("return_tab") or "table")
            elif section == "printers":
                if action == "workstation":
                    create_printer_workstation(
                        name=request.POST.get("name"),
                        printer=request.POST.get("printer"),
                        actor=request.user,
                    )
                elif action == "token":
                    rotate_printer_token(actor=request.user)
                elif action == "test":
                    create_print_job(title=request.POST.get("title"), actor=request.user)
                elif action == "advance":
                    advance_print_job(record_id=int(request.POST.get("record_id") or 0), actor=request.user)
                else:
                    raise SettingsOperationError("Неизвестное действие принтера.")
            elif section == "directories":
                directory_type = str(request.POST.get("directory_type") or "units")
                if action == "archive":
                    archive_record(
                        record_id=int(request.POST.get("record_id") or 0),
                        entity_prefix="directory_",
                        actor=request.user,
                    )
                else:
                    save_directory_item(
                        directory_type=directory_type,
                        title=request.POST.get("title"),
                        code=request.POST.get("code"),
                        actor=request.user,
                    )
                target += "?directory=" + directory_type
            elif section == "system":
                system_tab = str(request.POST.get("system_tab") or "general")
                if system_tab == "general":
                    save_system_settings(values=request.POST, actor=request.user)
                else:
                    save_system_section(section=system_tab, values=request.POST, actor=request.user)
                target += "?tab=" + system_tab
            elif section == "labels":
                if action == "archive":
                    archive_record(
                        record_id=int(request.POST.get("record_id") or 0),
                        entity_prefix="label_template",
                        actor=request.user,
                    )
                else:
                    record = save_label_template(
                        record_id=int(request.POST.get("record_id")) if str(request.POST.get("record_id") or "").isdigit() else None,
                        name=request.POST.get("name"),
                        sheet_size=str(request.POST.get("sheet_size") or "58x40"),
                        elements=request.POST.get("elements"),
                        actor=request.user,
                    )
                    target += f"?template={record.pk}"
            elif section == "matching":
                product = get_object_or_404(WmsNewProduct, pk=request.POST.get("product_id"), is_archived=False)
                save_product_mapping(
                    product=product,
                    ozon=request.POST.get("ozon"),
                    wb=request.POST.get("wb"),
                    actor=request.user,
                )
                query = str(request.POST.get("return_query") or "").lstrip("?")
                if query:
                    target += "?" + query
            else:
                raise SettingsOperationError("Неизвестный раздел настроек.")
            messages.success(request, "Изменение сохранено только в FBS-NEW и записано в аудит.")
        except (SettingsOperationError, ValueError, WmsNewProduct.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Не удалось сохранить изменение FBS-NEW.")
        return redirect(target)


class HeadManagerFbsNewHelpActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            create_development_task(
                title=request.POST.get("title"),
                description=request.POST.get("description"),
                actor=request.user,
            )
            messages.success(request, "Задача создана в отдельном журнале FBS-NEW.")
        except SettingsOperationError as exc:
            messages.error(request, str(exc))
        return redirect("/head-manager/fbs-new/development/")


class HeadManagerFbsNewPilotAccessView(RoleRequiredMixin, View):
    """Opt one employee into or out of FBS-NEW without changing their primary role."""

    allowed_roles = ("head_manager",)

    def post(self, request, employee_id: int):
        action = str(request.POST.get("action") or "").strip().lower()
        if action not in {"enable", "disable"}:
            return HttpResponseBadRequest("Неизвестное действие пилотного доступа")

        with transaction.atomic():
            employee = get_object_or_404(
                Employee.objects.select_for_update().select_related("user"),
                pk=employee_id,
                is_active=True,
                user__isnull=False,
            )
            if employee.role == "head_manager" and action == "disable":
                return HttpResponseBadRequest("Основная роль уже дает доступ к FBS-NEW")

            before = list(employee.normalized_access_roles())
            selected = set(before)
            if action == "enable":
                selected.add(FBS_NEW_ACCESS_ROLE)
            else:
                selected.discard(FBS_NEW_ACCESS_ROLE)
            employee.access_roles = [
                key
                for key, _label in Employee.ACCESS_ROLE_CHOICES
                if key in selected and key != employee.role
            ]
            employee.save(update_fields=("access_roles", "updated_at"))

            journal, _created = AuditJournal.objects.get_or_create(
                code="fbs_new_pilot",
                defaults={
                    "name": "Пилотный доступ FBS-NEW",
                    "description": "Персональное включение нового интерфейса без изменения основной роли.",
                },
            )
            AuditEntry.objects.create(
                journal=journal,
                action="update",
                user=request.user,
                description=(
                    f"Пилотный доступ FBS-NEW {'включен' if action == 'enable' else 'отключен'} "
                    f"для сотрудника #{employee.id}."
                ),
                snapshot={
                    "employee_id": employee.id,
                    "primary_role": employee.role,
                    "before_access_roles": before,
                    "after_access_roles": list(employee.normalized_access_roles()),
                    "pilot_action": action,
                },
            )

        return redirect("/head-manager/fbs-new/settings/#pilot-access")


class HeadManagerFbsNewQueueActionView(RoleRequiredMixin, View):
    """Execute isolated queue mutations inside the WMS NEW tables only."""

    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        return_to = str(request.POST.get("return_to") or "").strip()
        if return_to not in {
            "/head-manager/fbs-new/fbs-orders/",
            "/head-manager/fbs-new/fbs-queue/",
        }:
            return_to = "/head-manager/fbs-new/fbs-queue/"
        action = str(request.POST.get("action") or "").strip()
        order_ids = []
        for raw_value in request.POST.getlist("order_ids"):
            try:
                order_ids.append(int(raw_value))
            except (TypeError, ValueError):
                messages.error(request, "В выборе заказов найден некорректный идентификатор.")
                return redirect(return_to)
        order_ids = list(dict.fromkeys(order_ids))
        if not order_ids:
            messages.error(request, "Выберите хотя бы один заказ.")
            return redirect(return_to)

        try:
            if action == "launch_wave":
                wave = launch_wave(order_ids=order_ids, actor=request.user)
                result = (wave.source_snapshot or {}).get("creation_result") or {}
                shortage = int(result.get("potentially_short_orders_count") or 0)
                skipped = int(result.get("orders_not_added_in_wave_count") or 0)
                messages.success(
                    request,
                    f"Создана волна {wave.number}: {wave.planned_orders} заказов, {wave.planned_units} шт.",
                )
                if shortage:
                    messages.warning(
                        request,
                        f"Спорные заказы: {shortage}. Они добавлены по настройке FBS с предупреждением.",
                    )
                if skipped:
                    messages.warning(request, f"Не добавлено в волну: {skipped} заказов.")
                return redirect(f"/head-manager/fbs-new/fbs-waves/{wave.id}/")
            changed = apply_bulk_action(
                order_ids=order_ids,
                action=action,
                actor=request.user,
                warehouse_code=str(request.POST.get("warehouse_code") or ""),
            )
        except OrderOperationError as exc:
            messages.error(request, str(exc))
            return redirect(return_to)
        messages.success(request, f"Операция выполнена для {changed} заказов FBS-NEW.")
        return redirect(return_to)


class HeadManagerFbsNewOrderCreateView(RoleRequiredMixin, View):
    """Create a manual order inside the isolated WMS NEW domain."""

    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        agency_id = str(request.POST.get("agency_id") or "").strip()
        external_order_id = str(request.POST.get("external_order_id") or "").strip()
        tracking_number = str(request.POST.get("tracking_number") or "").strip()
        cutoff_date = parse_date(str(request.POST.get("cutoff_date") or "").strip())
        if not agency_id.isdigit() or not external_order_id or cutoff_date is None:
            messages.error(request, "Укажите партнера, дату отгрузки и номер заказа.")
            return redirect("/head-manager/fbs-new/fbs-queue/?create=1")
        agency = get_object_or_404(Agency, pk=int(agency_id), archived=False)
        try:
            order = create_manual_order(
                agency=agency,
                external_order_id=external_order_id,
                cutoff_date=cutoff_date,
                tracking_number=tracking_number,
                auto_tracking=bool(request.POST.get("auto_tracking")),
                actor=request.user,
            )
        except OrderOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/fbs-queue/?create=1")

        messages.success(request, "Заказ создан в FBS-NEW.")
        return redirect(f"/head-manager/fbs-new/fbs-orders/{order.id}/")


class HeadManagerFbsNewOrderUpdateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, order_id: int):
        try:
            order = update_order(
                order_id=order_id,
                status=str(request.POST.get("status") or ""),
                warehouse_code=str(request.POST.get("warehouse_code") or ""),
                actor=request.user,
            )
        except OrderOperationError as exc:
            messages.error(request, str(exc))
            return redirect(f"/head-manager/fbs-new/fbs-orders/{order_id}/")
        messages.success(request, f"Заказ {order.external_order_id} обновлен в FBS-NEW.")
        return redirect(f"/head-manager/fbs-new/fbs-orders/{order.id}/")


class HeadManagerFbsNewWaveUpdateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, wave_id: int):
        try:
            wave = update_wave(
                wave_id=wave_id,
                status=str(request.POST.get("status") or ""),
                place_name=request.POST.get("place_name"),
                actor=request.user,
            )
        except OrderOperationError as exc:
            messages.error(request, str(exc))
            return redirect(f"/head-manager/fbs-new/fbs-waves/{wave_id}/")
        messages.success(request, f"Волна {wave.number}: {wave.get_status_display()}.")
        return redirect(f"/head-manager/fbs-new/fbs-waves/{wave.id}/")


class HeadManagerFbsNewWaveActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, wave_id: int):
        action = str(request.POST.get("action") or "").strip()
        if action == "collect":
            return redirect(f"/head-manager/fbs-new/fbs-waves/{wave_id}/collect/")
        try:
            if action == "start":
                wave = start_collection(
                    wave_id=wave_id,
                    place_scan=str(request.POST.get("scan") or ""),
                    actor=request.user,
                )
                messages.success(request, f"Волна {wave.number} запущена в подбор.")
            elif action == "place":
                allocation = scan_source_place(
                    wave_id=wave_id,
                    place_scan=str(request.POST.get("scan") or ""),
                    actor=request.user,
                )
                messages.success(
                    request,
                    f"Место {allocation.source_place_name or allocation.source_place_code} подтверждено.",
                )
            elif action == "product":
                allocation = scan_picking_product(
                    wave_id=wave_id,
                    barcode=str(request.POST.get("scan") or ""),
                    quantity=request.POST.get("quantity") or 1,
                    actor=request.user,
                )
                messages.success(
                    request,
                    f"Подобрано: {allocation.pick_line.order_item.product_name or allocation.pick_line.order_item.external_sku}.",
                )
            elif action == "skip":
                allocation = skip_current_allocation(wave_id=wave_id, actor=request.user)
                messages.warning(
                    request,
                    f"Позиция {allocation.pick_line.order_item.product_name} пропущена.",
                )
            elif action == "finish":
                wave = finish_collection(wave_id=wave_id, actor=request.user)
                if wave is None:
                    messages.info(request, "Пустая волна удалена; не начатые заказы возвращены в очередь.")
                    return redirect("/head-manager/fbs-new/fbs-waves/")
                messages.success(request, f"Подбор волны {wave.number} завершен.")
            elif action == "remove_order":
                order_id = int(request.POST.get("order_id") or 0)
                wave = remove_order_from_wave(
                    wave_id=wave_id,
                    order_id=order_id,
                    actor=request.user,
                )
                if wave is None:
                    messages.info(request, "Последний заказ удален; пустая волна удалена.")
                    return redirect("/head-manager/fbs-new/fbs-waves/")
                messages.success(request, "Заказ удален из волны и возвращен в очередь.")
                return redirect(f"/head-manager/fbs-new/fbs-waves/{wave_id}/")
            elif action == "cancel":
                wave = cancel_wave(wave_id=wave_id, actor=request.user)
                messages.warning(request, f"Волна {wave.number} отменена, резервы FBS-NEW сняты.")
                return redirect(f"/head-manager/fbs-new/fbs-waves/{wave_id}/")
            else:
                raise PickingOperationError("Неизвестное действие подбора FBS-NEW.")
        except (
            PickingOperationError,
            WmsNewWave.DoesNotExist,
            WmsNewWaveAllocation.DoesNotExist,
            ValueError,
        ) as exc:
            messages.error(request, str(exc))
        return redirect(f"/head-manager/fbs-new/fbs-waves/{wave_id}/collect/")


class HeadManagerFbsNewWaveCollectingListView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request, wave_id: int):
        data = build_fbs_wave_detail_data(wave_id)
        if data is None:
            raise Http404("Волна FBS-NEW не найдена")
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="fbs-new-wave-{data["display_id"]}-collecting-list.csv"'
        )
        response.write("\ufeff")
        writer = csv.writer(response, delimiter=";")
        writer.writerow(
            ("ID товара", "ID заказа", "№ заказа", "Товар", "Статус", "ШК", "Артикул", "Количество")
        )
        for item in data["items"]:
            writer.writerow(
                (
                    item["item_id"],
                    item["order_id"],
                    item["order_number"],
                    item["product"],
                    item["status"],
                    item["barcode"],
                    item["article"],
                    item["quantity"],
                )
            )
        return response


class HeadManagerFbsNewAssemblyStartView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            session = start_assembly_session(
                place_scan=str(request.POST.get("place_scan") or ""),
                actor=request.user,
            )
        except AssemblyOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/fbs-assembly/")
        messages.success(
            request,
            f"Место {session.place_name}: к сборке {session.planned_items} шт.",
        )
        return redirect(f"/head-manager/fbs-new/fbs-assembly/?session={session.id}")


class HeadManagerFbsNewAssemblyScanView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, session_id: int):
        action = str(request.POST.get("action") or "product")
        scan = str(request.POST.get("scan") or "")
        try:
            if action == "order":
                line = scan_assembly_order_label(
                    session_id=session_id,
                    barcode=scan,
                    actor=request.user,
                )
                messages.success(request, f"Заказ {line.order.external_order_id} собран.")
            else:
                line = scan_assembly_product(
                    session_id=session_id,
                    barcode=scan,
                    extra_data={
                        key: request.POST.get(key)
                        for key in (
                            "marking_code",
                            "imei",
                            "uin",
                            "gtin",
                            "sgtin",
                            "expiration_date",
                        )
                    },
                    actor=request.user,
                )
                if line.status == line.STATUS_AWAITING_ORDER:
                    messages.info(
                        request,
                        f"Товар найден. Отсканируйте этикетку заказа {line.order.external_order_id}.",
                    )
                else:
                    messages.success(request, f"Товар {line.order_item.product_name} собран.")
        except (AssemblyOperationError, WmsNewAssemblySession.DoesNotExist) as exc:
            messages.error(request, str(exc))
        return redirect(f"/head-manager/fbs-new/fbs-assembly/?session={session_id}")


class HeadManagerFbsNewAssemblyProblemView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, session_id: int):
        try:
            line = move_assembly_problem(
                session_id=session_id,
                line_id=int(request.POST.get("line_id") or 0) or None,
                problem_place=str(request.POST.get("problem_place") or ""),
                reason=str(request.POST.get("reason") or ""),
                actor=request.user,
            )
            messages.warning(
                request,
                f"Заказ {line.order.external_order_id} отложен в {line.problem_place}.",
            )
        except (AssemblyOperationError, WmsNewAssemblySession.DoesNotExist, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect(f"/head-manager/fbs-new/fbs-assembly/?session={session_id}")


class HeadManagerFbsNewShipmentCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        agency_id = str(request.POST.get("agency_id") or "").strip()
        if not agency_id.isdigit():
            messages.error(request, "Выберите партнера.")
            return redirect("/head-manager/fbs-new/fbs-shipments/?create=1")
        agency = get_object_or_404(Agency, pk=int(agency_id), archived=False)
        try:
            shipment = create_shipment(
                agency=agency,
                delivery_type=str(request.POST.get("delivery_type") or ""),
                integration_name=str(request.POST.get("integration_name") or ""),
                actor=request.user,
            )
        except ShipmentOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/fbs-shipments/?create=1")
        messages.success(request, "Отгрузка создана в отдельном контуре FBS-NEW.")
        return redirect(f"/head-manager/fbs-new/fbs-shipments/{shipment.id}/")


class HeadManagerFbsNewShipmentBulkView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            changed = send_all_checked(actor=request.user)
        except ShipmentOperationError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Отправлено отгрузок FBS-NEW: {changed}.")
        return redirect("/head-manager/fbs-new/fbs-shipments/")


class HeadManagerFbsNewShipmentActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, shipment_id: int):
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "add_order":
                add_shipment_order(
                    shipment_id=shipment_id,
                    order_id=int(request.POST.get("order_id") or 0),
                    actor=request.user,
                )
                message = "Заказ добавлен в отгрузку FBS-NEW."
            elif action == "add_box":
                add_shipment_box(
                    shipment_id=shipment_id,
                    qr_code=str(request.POST.get("qr_code") or ""),
                    actor=request.user,
                )
                message = "Короб добавлен в отгрузку FBS-NEW."
            elif action == "verify_order":
                box_value = str(request.POST.get("box_id") or "").strip()
                verify_shipment_order(
                    shipment_id=shipment_id,
                    shipment_order_id=int(request.POST.get("shipment_order_id") or 0),
                    box_id=int(box_value) if box_value.isdigit() else None,
                    in_supply=request.POST.get("in_supply", "1") == "1",
                    actor=request.user,
                )
                message = "Заказ проверен в FBS-NEW."
            elif action in {"unverify_order", "error_order", "move_order"}:
                box_value = str(request.POST.get("box_id") or "").strip()
                update_shipment_order_check(
                    shipment_id=shipment_id,
                    shipment_order_id=int(request.POST.get("shipment_order_id") or 0),
                    action={
                        "unverify_order": "unverify",
                        "error_order": "error",
                        "move_order": "move",
                    }[action],
                    box_id=int(box_value) if box_value.isdigit() else None,
                    actor=request.user,
                )
                message = "Проверка и размещение заказа обновлены в FBS-NEW."
            elif action == "remove_order":
                remove_shipment_order(
                    shipment_id=shipment_id,
                    shipment_order_id=int(request.POST.get("shipment_order_id") or 0),
                    actor=request.user,
                )
                message = "Заказ удален из отгрузки FBS-NEW."
            elif action == "remove_box":
                remove_shipment_box(
                    shipment_id=shipment_id,
                    box_id=int(request.POST.get("box_id") or 0),
                    actor=request.user,
                )
                message = "Короб удален из отгрузки FBS-NEW."
            elif action == "add_service":
                add_shipment_service(
                    shipment_id=shipment_id,
                    name=str(request.POST.get("name") or ""),
                    unit_price=request.POST.get("unit_price"),
                    quantity=request.POST.get("quantity"),
                    actor=request.user,
                )
                message = "Услуга добавлена в FBS-NEW."
            elif action == "finalize_tariff":
                finalize_tariff(shipment_id=shipment_id, actor=request.user)
                message = "Тарификация отгрузки завершена в FBS-NEW."
            elif action == "remove_service":
                remove_shipment_service(
                    shipment_id=shipment_id,
                    service_id=int(request.POST.get("service_id") or 0),
                    actor=request.user,
                )
                message = "Услуга удалена из отгрузки FBS-NEW."
            else:
                transition_shipment(
                    shipment_id=shipment_id,
                    action=action,
                    actor=request.user,
                )
                message = "Статус отгрузки FBS-NEW обновлен."
        except (ShipmentOperationError, WmsNewShipment.DoesNotExist, ValueError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, message)
        return redirect(f"/head-manager/fbs-new/fbs-shipments/{shipment_id}/")


class HeadManagerFbsNewShipmentDocumentsView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request, shipment_id: int | None = None):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        suffix = str(shipment_id or "all")
        response["Content-Disposition"] = f'attachment; filename="fbs-new-shipments-{suffix}.csv"'
        response.write("\ufeff")
        writer = csv.writer(response, delimiter=";")
        writer.writerow(("ID", "Партнер", "Создана", "Доставка", "Интеграция", "Статус", "Заказов", "Товаров"))
        queryset = WmsNewShipment.objects.select_related("agency").order_by("-source_created_at", "-id")
        if shipment_id is not None:
            queryset = queryset.filter(pk=shipment_id)
        for shipment in queryset:
            writer.writerow(
                (
                    shipment.source_batch_id or shipment.id,
                    shipment.agency,
                    shipment.source_created_at or shipment.created_at,
                    shipment.delivery_type,
                    shipment.integration_name,
                    shipment.get_status_display(),
                    shipment.order_count,
                    shipment.item_count,
                )
            )
        return response


class HeadManagerFbsNewReturnCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            item = create_return(
                scan=str(request.POST.get("scan") or ""),
                reason=str(request.POST.get("reason") or ""),
                actor=request.user,
            )
        except ReturnOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/fbs-returns/?create=1")
        messages.success(
            request,
            f"Возврат по заказу {item.order.external_order_id} добавлен в FBS-NEW.",
        )
        return redirect("/head-manager/fbs-new/fbs-returns/")


class HeadManagerFbsNewReturnActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            changed = update_returns(
                return_ids=[int(value) for value in request.POST.getlist("return_ids")],
                action=str(request.POST.get("action") or ""),
                actor=request.user,
            )
        except (ReturnOperationError, ValueError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Обновлено возвратов FBS-NEW: {changed}.")
        return redirect("/head-manager/fbs-new/fbs-returns/")


class HeadManagerFbsNewReturnDetailActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, return_id: int):
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "start_receiving":
                start_receiving(
                    return_id=return_id,
                    location_id=int(request.POST.get("location_id") or 0),
                    box_code=str(request.POST.get("box_code") or ""),
                    actor=request.user,
                )
                message = "Приемка возврата начата в FBS-NEW."
            elif action == "inspect_line":
                record_return_line(
                    return_id=return_id,
                    line_id=int(request.POST.get("line_id") or 0),
                    received_qty=request.POST.get("received_qty"),
                    accepted_qty=request.POST.get("accepted_qty"),
                    condition=str(request.POST.get("condition") or ""),
                    marking_codes=str(request.POST.get("marking_codes") or ""),
                    actor=request.user,
                )
                message = "Результат осмотра сохранен в FBS-NEW."
            elif action == "complete_receiving":
                complete_return(return_id=return_id, actor=request.user)
                message = "Возврат принят, годный товар оприходован в FBS-NEW."
            else:
                raise ReturnOperationError("Неизвестное действие с возвратом.")
        except (ReturnOperationError, WmsNewReturn.DoesNotExist, ValueError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, message)
        return redirect(f"/head-manager/fbs-new/fbs-returns/{return_id}/")


class HeadManagerFbsNewReturnExportView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="fbs-new-returns.csv"'
        response.write("\ufeff")
        writer = csv.writer(response, delimiter=";")
        writer.writerow(("ID", "ID заказа", "Номер", "Партнер", "Создан", "Дата отгрузки", "Дата возврата", "Тип доставки", "Трэк номер", "Номер задачи", "Статус задачи"))
        for item in WmsNewReturn.objects.select_related("order__agency").order_by(
            "-source_created_at", "-id"
        ):
            writer.writerow(
                (
                    item.source_request_id or item.id,
                    item.order.source_order_id or item.order_id,
                    item.order.external_order_id,
                    item.order.agency,
                    item.source_created_at or item.created_at,
                    item.shipped_at or "",
                    item.returned_at or "",
                    item.order.delivery_type,
                    item.order.tracking_number,
                    item.source_request_id or item.id,
                    item.get_status_display(),
                )
            )
        return response


class HeadManagerFbsNewBundleActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "define":
                component_ids = request.POST.getlist("component_id")
                quantities = request.POST.getlist("component_quantity")
                components = [
                    (int(product_id), int(quantity))
                    for product_id, quantity in zip(component_ids, quantities)
                    if str(product_id).isdigit() and str(quantity).strip()
                ]
                bundle = define_bundle(
                    bundle_id=int(request.POST.get("bundle_id") or 0),
                    components=components,
                    actor=request.user,
                )
                messages.success(request, f"Состав набора «{bundle.name}» сохранен в WMS NEW.")
                return redirect("/head-manager/fbs-new/warehouse-bundles/?mode=assemble")
            if action == "assemble":
                operation = assemble_bundle(
                    bundle_id=int(request.POST.get("bundle_id") or 0),
                    quantity=request.POST.get("quantity"),
                    location_id=int(request.POST.get("location_id") or 0),
                    actor=request.user,
                )
                messages.success(request, f"Собрано наборов: {operation.quantity}.")
                return redirect("/head-manager/fbs-new/warehouse-bundles/?mode=assemble")
            if action == "disassemble":
                operation = disassemble_bundle(
                    bundle_id=int(request.POST.get("bundle_id") or 0),
                    quantity=request.POST.get("quantity"),
                    location_id=int(request.POST.get("location_id") or 0),
                    actor=request.user,
                )
                messages.success(request, f"Разобрано наборов: {operation.quantity}.")
                return redirect("/head-manager/fbs-new/warehouse-bundles/?mode=disassemble")
            raise BundleOperationError("Неизвестное действие с набором.")
        except (
            BundleOperationError,
            WmsNewProduct.DoesNotExist,
            WarehouseLocation.DoesNotExist,
            ValueError,
        ) as exc:
            messages.error(request, str(exc))
            return redirect(f"/head-manager/fbs-new/warehouse-bundles/?mode={action or 'assemble'}")


class HeadManagerFbsNewMovementActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        action = str(request.POST.get("action") or "product").strip()
        try:
            if action == "all":
                records = move_all_from_location(
                    source_location_id=int(request.POST.get("source_location_id") or 0),
                    target_location_id=int(request.POST.get("target_location_id") or 0),
                    actor=request.user,
                )
            elif action == "product":
                records = move_product(
                    product_id=int(request.POST.get("product_id") or 0),
                    source_location_id=int(request.POST.get("source_location_id") or 0),
                    target_location_id=int(request.POST.get("target_location_id") or 0),
                    units=request.POST.get("units") or 0,
                    box_ids=[
                        int(value)
                        for value in request.POST.getlist("box_ids")
                        if str(value).isdigit()
                    ],
                    actor=request.user,
                )
            else:
                raise MovementOperationError("Неизвестный режим перемещения.")
        except (
            MovementOperationError,
            WmsNewProduct.DoesNotExist,
            WarehouseLocation.DoesNotExist,
            ValueError,
        ) as exc:
            messages.error(request, str(exc))
            return redirect(f"/head-manager/fbs-new/warehouse-movement/?mode={action}")
        messages.success(
            request,
            f"Перемещение выполнено в WMS NEW. Строк истории: {len(records)}.",
        )
        return redirect("/head-manager/fbs-new/warehouse-history/")


class HeadManagerFbsNewMovementExportView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request):
        queryset = WmsNewMovement.objects.select_related("agency", "product", "actor")
        query = str(request.GET.get("q") or "").strip()
        partner = str(request.GET.get("partner") or "").strip()
        action = str(request.GET.get("action") or "").strip()
        if query:
            search = (
                Q(product_name__icontains=query)
                | Q(article__icontains=query)
                | Q(product__barcode__icontains=query)
            )
            if query.isdigit():
                search |= Q(id=int(query))
            queryset = queryset.filter(search)
        if partner.isdigit():
            queryset = queryset.filter(agency_id=int(partner))
        if action in dict(WmsNewMovement.ACTION_CHOICES):
            queryset = queryset.filter(action=action)
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("История движений")
        sheet.append(
            (
                "Название",
                "Партнер",
                "Артикул",
                "Дата операции",
                "Место Источник",
                "Место размещения",
                "Кол-во",
                "Остаток",
                "Информация",
                "Пользователь",
            )
        )
        for item in queryset.order_by("-occurred_at", "-id").iterator(chunk_size=2000):
            occurred_at = timezone.localtime(item.occurred_at).replace(tzinfo=None)
            sheet.append(
                (
                    item.product_name or (item.product.name if item.product else ""),
                    str(item.agency),
                    item.article or (item.product.article if item.product else ""),
                    occurred_at,
                    item.source_location_name,
                    item.target_location_name,
                    item.quantity,
                    item.balance_after,
                    item.information or item.get_action_display(),
                    item.actor_name or str(item.actor or ""),
                )
            )
        output = BytesIO()
        workbook.save(output)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="wms-new-movements.xlsx"'
        return response


class HeadManagerFbsNewLogisticsOrderCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            item = accept_logistics_order(
                number=str(request.POST.get("number") or ""), actor=request.user
            )
        except LogisticsOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/logistics-orders/?create=1")
        messages.success(request, f"Отправление {item.number} принято в FBS-NEW.")
        return redirect(f"/head-manager/fbs-new/logistics-orders/?order_id={item.id}")


class HeadManagerFbsNewLogisticsOrderImportView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        order_ids = [
            int(value)
            for value in request.POST.getlist("order_ids")
            if str(value).isdigit()
        ]
        measurements = {
            order_id: {
                "weight_g": request.POST.get(f"weight_g_{order_id}"),
                "width_mm": request.POST.get(f"width_mm_{order_id}"),
                "height_mm": request.POST.get(f"height_mm_{order_id}"),
                "depth_mm": request.POST.get(f"depth_mm_{order_id}"),
            }
            for order_id in order_ids
        }
        try:
            rows = import_fbs_shipments(
                shipment_ids=[
                    int(value)
                    for value in request.POST.getlist("shipment_ids")
                    if str(value).isdigit()
                ],
                order_ids=order_ids or None,
                measurements=measurements,
                actor=request.user,
            )
        except LogisticsOperationError as exc:
            messages.error(request, str(exc))
            shipment_id = next(
                (
                    value
                    for value in request.POST.getlist("shipment_ids")
                    if str(value).isdigit()
                ),
                "",
            )
            suffix = f"&import_shipment={shipment_id}" if shipment_id else ""
            return redirect(f"/head-manager/fbs-new/logistics-orders/?import=1{suffix}")
        messages.success(request, f"Загружено отправлений из FBS: {len(rows)}.")
        return redirect("/head-manager/fbs-new/logistics-orders/")


class HeadManagerFbsNewLogisticsOrderActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, order_id: int | None = None):
        action = str(request.POST.get("action") or "measure").strip()
        try:
            if action == "measure" and order_id is not None:
                item = measure_logistics_order(
                    order_id=order_id,
                    weight_g=request.POST.get("weight_g"),
                    width_mm=request.POST.get("width_mm"),
                    height_mm=request.POST.get("height_mm"),
                    depth_mm=request.POST.get("depth_mm"),
                    actor=request.user,
                )
                messages.success(request, f"Параметры отправления {item.number} сохранены.")
            elif action == "delete":
                count = delete_logistics_orders(
                    order_ids=[
                        int(value)
                        for value in request.POST.getlist("order_ids")
                        if str(value).isdigit()
                    ],
                    actor=request.user,
                )
                messages.success(request, f"Удалено отправлений: {count}.")
            else:
                raise LogisticsOperationError("Неизвестное действие с отправлением.")
        except (LogisticsOperationError, WmsNewLogisticsOrder.DoesNotExist) as exc:
            messages.error(request, str(exc))
        return redirect("/head-manager/fbs-new/logistics-orders/")


class HeadManagerFbsNewLogisticsPackageCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            item = create_logistics_package(
                carrier_code=str(request.POST.get("carrier_code") or ""),
                delivery_service=str(request.POST.get("delivery_service") or ""),
                warehouse_code=str(request.POST.get("warehouse_code") or ""),
                actor=request.user,
            )
        except LogisticsOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/logistics-shipments/?create=1")
        messages.success(request, f"Грузоместо {item.number} создано.")
        return redirect("/head-manager/fbs-new/logistics-shipments/")


class HeadManagerFbsNewLogisticsPackageActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "add_orders":
                count = add_orders_to_package(
                    package_id=int(request.POST.get("package_id") or 0),
                    order_ids=[
                        int(value)
                        for value in request.POST.getlist("order_ids")
                        if str(value).isdigit()
                    ],
                    actor=request.user,
                )
            elif action == "remove_orders":
                count = remove_orders_from_package(
                    package_id=int(request.POST.get("package_id") or 0),
                    order_ids=[
                        int(value)
                        for value in request.POST.getlist("order_ids")
                        if str(value).isdigit()
                    ],
                    actor=request.user,
                )
            else:
                count = update_packages(
                    package_ids=[
                        int(value)
                        for value in request.POST.getlist("package_ids")
                        if str(value).isdigit()
                    ],
                    action=action,
                    manifest_id=request.POST.get("manifest_id"),
                    actor=request.user,
                )
        except (
            LogisticsOperationError,
            WmsNewLogisticsPackage.DoesNotExist,
            WmsNewLogisticsManifest.DoesNotExist,
            ValueError,
        ) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Обновлено грузомест: {count}.")
        package_id = str(request.POST.get("package_id") or "").strip()
        if package_id.isdigit():
            return redirect(f"/head-manager/fbs-new/logistics-shipments/{package_id}/")
        return redirect("/head-manager/fbs-new/logistics-shipments/")


class HeadManagerFbsNewLogisticsPackageLabelsView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request):
        ids = [
            int(value)
            for value in request.GET.getlist("package_ids")
            if str(value).isdigit()
        ]
        queryset = WmsNewLogisticsPackage.objects.filter(id__in=ids or [-1]).order_by("id")
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="wms-new-package-labels.csv"'
        response.write("\ufeff")
        writer = csv.writer(response, delimiter=";")
        writer.writerow(("Грузоместо", "Трек", "ТК", "Источник", "Вес, г"))
        for item in queryset:
            writer.writerow(
                (
                    item.number,
                    item.tracking_number or item.number,
                    item.get_carrier_code_display(),
                    item.delivery_service,
                    item.weight_g,
                )
            )
        return response


class HeadManagerFbsNewLogisticsManifestCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        arrival = parse_date(str(request.POST.get("expected_arrival_date") or ""))
        try:
            item = create_logistics_manifest(
                name=str(request.POST.get("name") or ""),
                total_weight_g=request.POST.get("total_weight_g"),
                departure_airport=str(request.POST.get("departure_airport") or ""),
                destination_airport=str(request.POST.get("destination_airport") or ""),
                expected_arrival_date=arrival,
                actor=request.user,
            )
        except LogisticsOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/logistics-trips/?create=1")
        messages.success(request, f"Рейс {item.name} добавлен в FBS-NEW.")
        return redirect("/head-manager/fbs-new/logistics-trips/")


class HeadManagerFbsNewLogisticsManifestActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, manifest_id: int):
        try:
            item = transition_manifest(
                manifest_id=manifest_id,
                action=str(request.POST.get("action") or ""),
                actor=request.user,
            )
        except (LogisticsOperationError, WmsNewLogisticsManifest.DoesNotExist) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Статус рейса {item.name} обновлен.")
        return redirect(f"/head-manager/fbs-new/logistics-trips/{manifest_id}/")


class HeadManagerFbsNewLogisticsSettingsView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        active = set(request.POST.getlist("active"))
        rows = [
            {
                "delivery_service": service,
                "international_carrier": international,
                "local_carrier": local,
                "is_active": service in active,
            }
            for service, international, local in zip(
                request.POST.getlist("delivery_service"),
                request.POST.getlist("international_carrier"),
                request.POST.getlist("local_carrier"),
            )
        ]
        try:
            count = save_route_rules(rows=rows, actor=request.user)
        except LogisticsOperationError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Сохранено правил маршрутизации: {count}.")
        return redirect("/head-manager/fbs-new/logistics-settings/")


def _document_registry_url(document_type: str, document_id: int | None = None) -> str:
    suffix = "receipt" if document_type == WmsNewDocument.TYPE_RECEIPT else "writeoff"
    url = f"/head-manager/fbs-new/documents-{suffix}/"
    return f"{url}?document_id={document_id}" if document_id else url


class HeadManagerFbsNewDocumentCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        document_type = str(request.POST.get("document_type") or "").strip()
        agency_id = str(request.POST.get("agency_id") or "").strip()
        try:
            if not agency_id.isdigit():
                raise DocumentOperationError("Выберите партнера.")
            agency = Agency.objects.get(pk=int(agency_id), archived=False)
            document = create_document(
                document_type=document_type,
                agency_id=agency.id,
                actor=request.user,
            )
        except (DocumentOperationError, Agency.DoesNotExist) as exc:
            messages.error(request, str(exc))
            return redirect(_document_registry_url(document_type) + "?create=1")
        messages.success(request, f"Документ №{document.id} создан в FBS-NEW.")
        return redirect(_document_registry_url(document_type, document.id))


class HeadManagerFbsNewDocumentActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, document_id: int):
        document = get_object_or_404(WmsNewDocument, pk=document_id)
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "add_item":
                add_document_item(
                    document_id=document.id,
                    product_id=int(request.POST.get("product_id") or 0),
                    quantity=request.POST.get("quantity"),
                    location_id=(
                        int(request.POST.get("location_id"))
                        if str(request.POST.get("location_id") or "").isdigit()
                        else None
                    ),
                    unit_price=request.POST.get("unit_price") or 0,
                    actor=request.user,
                )
                messages.success(request, "Товар добавлен в документ.")
            elif action == "remove_item":
                remove_document_item(
                    document_id=document.id,
                    item_id=int(request.POST.get("item_id") or 0),
                    actor=request.user,
                )
                messages.success(request, "Товар удален из документа.")
            elif action == "comments":
                update_document_comments(
                    document_id=document.id,
                    comment=str(request.POST.get("comment") or ""),
                    internal_comment=str(request.POST.get("internal_comment") or ""),
                    actor=request.user,
                )
                messages.success(request, "Комментарии сохранены.")
            elif action == "post":
                post_document(document_id=document.id, actor=request.user)
                messages.success(request, f"Документ №{document.id} проведен.")
            elif action == "cancel":
                cancel_document(document_id=document.id, actor=request.user)
                messages.success(request, f"Документ №{document.id} отменен.")
            else:
                raise DocumentOperationError("Неизвестное действие с документом.")
        except (
            DocumentOperationError,
            WmsNewDocument.DoesNotExist,
            WmsNewProduct.DoesNotExist,
            ValueError,
        ) as exc:
            messages.error(request, str(exc))
        return redirect(_document_registry_url(document.document_type, document.id))


class HeadManagerFbsNewDocumentActView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request, document_id: int):
        document = get_object_or_404(
            WmsNewDocument.objects.select_related("agency", "created_by", "posted_by"),
            pk=document_id,
        )
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="wms-new-document-{document.id}.csv"'
        )
        response.write("\ufeff")
        writer = csv.writer(response, delimiter=";")
        writer.writerow(("Документ", document.id))
        writer.writerow(("Тип", document.get_document_type_display()))
        writer.writerow(("Партнер", str(document.agency)))
        writer.writerow(("Основание", document.basis))
        writer.writerow(("Статус", document.get_status_display()))
        writer.writerow(())
        writer.writerow(("#", "Название", "Артикул", "Место", "Количество"))
        for item in document.items.order_by("id"):
            writer.writerow(
                (item.id, item.product_name, item.article, item.location_name, item.quantity)
            )
        return response


class HeadManagerFbsNewReportExportView(RoleRequiredMixin, View):
    """Export a generated FBS-NEW report without touching legacy data."""

    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request, section: str):
        if section not in REPORT_TITLES:
            raise Http404("Отчет FBS-NEW не найден")
        params = request.GET.copy()
        params["run"] = "1"
        report = build_report(section, params, limit=None)

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = str(report["title"])[:31]
        worksheet.append(list(report["columns"]))
        for row in report["rows"]:
            values = []
            for value in row:
                if isinstance(value, datetime) and timezone.is_aware(value):
                    value = timezone.make_naive(
                        timezone.localtime(value), timezone.get_current_timezone()
                    )
                values.append(value)
            worksheet.append(values)
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.font = cell.font.copy(bold=True)

        output = BytesIO()
        workbook.save(output)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="fbs-new-{section.removeprefix("reports-")}.xlsx"'
        )
        return response


class HeadManagerFbsNewMarketplaceStockSyncView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        agency_text = str(request.POST.get("agency_id") or "").strip()
        agency_id = int(agency_text) if agency_text.isdigit() else None
        try:
            result = refresh_marketplace_stocks(actor=request.user, agency_id=agency_id)
        except MarketplaceStockSyncError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                "Остатки маркетплейсов обновлены в FBS-NEW: "
                f"создано {result['imported']}, обновлено {result['updated']}.",
            )
            for error in result["errors"][:2]:
                messages.warning(request, error)
        suffix = f"?partner={agency_id}" if agency_id else ""
        return redirect(f"/head-manager/fbs-new/analytics-marketplace-stocks/{suffix}")


class HeadManagerFbsNewMarketplaceStockExportView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request):
        data = build_section_data("analytics-marketplace-stocks", request=request)
        recommendations = str(request.GET.get("recommendations") or "") == "1"
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Рекомендации" if recommendations else "Остатки МП"
        columns = data["recommendation_columns"] if recommendations else data["columns"]
        rows = data["recommendation_rows"] if recommendations else data["rows"]
        worksheet.append(list(columns))
        for row in rows:
            worksheet.append(list(row))
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.font = cell.font.copy(bold=True)
        output = BytesIO()
        workbook.save(output)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = (
            'attachment; filename="fbs-new-marketplace-recommendations.xlsx"'
            if recommendations
            else 'attachment; filename="fbs-new-marketplace-stocks.xlsx"'
        )
        return response


class HeadManagerFbsNewPartnerCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            partner = create_partner(name=request.POST.get("name"), actor=request.user)
        except PartnerBillingOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/partners-list/?create=1")
        messages.success(request, f"Партнер «{partner.name}» создан в FBS-NEW.")
        return redirect(f"/head-manager/fbs-new/partners-list/?partner_id={partner.pk}")


class HeadManagerFbsNewPartnerUpdateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, profile_id: int):
        try:
            partner = update_partner(
                profile_id=profile_id,
                actor=request.user,
                name=request.POST.get("name"),
                balance=request.POST.get("balance"),
                requisites=request.POST.get("requisites"),
                is_active=request.POST.get("is_active"),
                wb_products_enabled=request.POST.get("wb_products_enabled"),
                wb_orders_enabled=request.POST.get("wb_orders_enabled"),
                ozon_products_enabled=request.POST.get("ozon_products_enabled"),
                ozon_orders_enabled=request.POST.get("ozon_orders_enabled"),
            )
        except (PartnerBillingOperationError, WmsNewPartnerProfile.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Партнер FBS-NEW не найден.")
            return redirect("/head-manager/fbs-new/partners-list/")
        messages.success(request, f"Настройки партнера «{partner.name}» сохранены в FBS-NEW.")
        return redirect("/head-manager/fbs-new/partners-list/")


class HeadManagerFbsNewPartnerBillingActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, kind: str):
        slug_by_kind = {
            WmsNewBillingItem.KIND_TASK: "partners-billing-tasks",
            WmsNewBillingItem.KIND_STORAGE: "partners-billing-storage",
            WmsNewBillingItem.KIND_FBS: "partners-billing-fbs",
        }
        slug = slug_by_kind.get(kind)
        if not slug:
            return HttpResponseBadRequest("Некорректный вид тарификации")
        source_ids = request.POST.getlist("source_ids")
        try:
            changed = update_billing_sources(
                kind=kind,
                source_ids=source_ids,
                action=str(request.POST.get("action") or ""),
                actor=request.user,
            )
        except PartnerBillingOperationError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Обновлено строк тарификации: {changed}.")
        tab = str(request.POST.get("return_tab") or "unpriced")
        partner = str(request.POST.get("partner") or "")
        return redirect(f"/head-manager/fbs-new/{slug}/?tab={tab}&partner={partner}")


class HeadManagerFbsNewInvoiceCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        agency_id = str(request.POST.get("agency_id") or "").strip()
        if not agency_id.isdigit():
            messages.error(request, "Выберите партнера.")
            return redirect("/head-manager/fbs-new/partners-invoices/?create=services")
        agency = get_object_or_404(Agency, pk=int(agency_id), archived=False)
        invoice_type = str(request.POST.get("invoice_type") or WmsNewInvoice.TYPE_SERVICES)
        try:
            invoice = create_invoice(
                agency=agency,
                invoice_type=invoice_type,
                requisites=request.POST.get("requisites"),
                amount=request.POST.get("amount"),
                actor=request.user,
            )
        except PartnerBillingOperationError as exc:
            messages.error(request, str(exc))
            return redirect(f"/head-manager/fbs-new/partners-invoices/?create={invoice_type}")
        messages.success(request, f"Счет {invoice.number} создан в FBS-NEW.")
        return redirect(f"/head-manager/fbs-new/partners-invoices/?invoice_id={invoice.pk}")


class HeadManagerFbsNewInvoiceActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, invoice_id: int):
        try:
            invoice = update_invoice(
                invoice_id=invoice_id,
                action=str(request.POST.get("action") or ""),
                amount=request.POST.get("amount"),
                actor=request.user,
            )
        except (PartnerBillingOperationError, WmsNewInvoice.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Счет FBS-NEW не найден.")
            return redirect("/head-manager/fbs-new/partners-invoices/")
        messages.success(request, f"Счет {invoice.number} обновлен.")
        return redirect(f"/head-manager/fbs-new/partners-invoices/?invoice_id={invoice.pk}")


class HeadManagerFbsNewPrimaryDocumentDownloadView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request, document_id: int):
        document = get_object_or_404(
            WmsNewPrimaryDocument.objects.select_related("agency", "invoice"),
            pk=document_id,
        )
        invoice_line = (
            f"<p>Счет: {document.invoice.number}</p>" if document.invoice_id else ""
        )
        html = (
            "<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<title>Первичный документ FBS-NEW</title>"
            "<style>body{font:16px Arial;margin:48px;color:#30304a}h1{font-size:24px}"
            "table{border-collapse:collapse;width:100%;margin-top:24px}td{border:1px solid #aaa;padding:10px}</style>"
            f"<h1>{document.get_document_type_display()} № {document.number}</h1>"
            f"<p>Период: {document.period:%m.%Y}</p><p>Партнер: {document.agency}</p>"
            f"{invoice_line}<table><tr><td>Услуги Fullbox</td><td>{document.amount:.2f} руб.</td></tr></table>"
            "<p>Документ сформирован независимым контуром FBS-NEW.</p></html>"
        )
        response = HttpResponse(html, content_type="text/html; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="fbs-new-{document.number}.html"'
        )
        return response


class HeadManagerFbsNewPrimaryDocumentInvoiceView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, document_id: int):
        try:
            invoice = create_invoice_from_document(document_id=document_id, actor=request.user)
        except (PartnerBillingOperationError, WmsNewPrimaryDocument.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Документ FBS-NEW не найден.")
            return redirect("/head-manager/fbs-new/partners-primary-docs/")
        messages.success(request, f"Счет {invoice.number} связан с первичным документом.")
        return redirect(f"/head-manager/fbs-new/partners-invoices/?invoice_id={invoice.pk}")


class HeadManagerFbsNewTaskCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)
    return_sections = {
        "tasks-acceptance",
        "tasks-processing",
        "tasks-shipment",
        "tasks-other",
    }

    def post(self, request):
        return_section = str(request.POST.get("return_section") or "").strip()
        target = (
            f"/head-manager/fbs-new/{return_section}/"
            if return_section in self.return_sections
            else "/head-manager/fbs-new/tasks-list/"
        )
        agency_id = str(request.POST.get("agency_id") or "").strip()
        due_date = parse_date(str(request.POST.get("due_date") or "").strip())
        if not agency_id.isdigit() or due_date is None:
            messages.error(request, "Укажите партнера и срок выполнения задачи.")
            return redirect(target + "?create=1")
        agency = get_object_or_404(Agency, pk=int(agency_id), archived=False)
        try:
            task = create_task(
                agency=agency,
                workflow_type=str(request.POST.get("workflow_type") or ""),
                title=str(request.POST.get("title") or ""),
                due_date=due_date,
                description=str(request.POST.get("description") or ""),
                priority=str(request.POST.get("priority") or WmsNewTask.PRIORITY_NORMAL),
                actor=request.user,
            )
        except TaskOperationError as exc:
            messages.error(request, str(exc))
            return redirect(target + "?create=1")
        messages.success(request, f"Задача №{task.id} создана в FBS-NEW.")
        if return_section in self.return_sections:
            return redirect(target)
        return redirect(f"/head-manager/fbs-new/tasks/{task.id}/")


class HeadManagerFbsNewTaskUpdateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, task_id: int):
        employee_id = str(request.POST.get("assigned_to_id") or "").strip()
        assigned_to = None
        if employee_id:
            if not employee_id.isdigit():
                return HttpResponseBadRequest("Некорректный исполнитель")
            assigned_to = get_object_or_404(Employee, pk=int(employee_id), is_active=True)
        try:
            task = update_task(
                task_id=task_id,
                status=str(request.POST.get("status") or ""),
                priority=str(request.POST.get("priority") or ""),
                assigned_to=assigned_to,
                actor=request.user,
            )
        except (TaskOperationError, WmsNewTask.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Задача FBS-NEW не найдена.")
            return redirect("/head-manager/fbs-new/tasks-list/")
        messages.success(request, f"Задача №{task.id} обновлена.")
        return redirect(f"/head-manager/fbs-new/tasks/{task.id}/")


class HeadManagerFbsNewTaskDetailActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, task_id: int):
        action = str(request.POST.get("action") or "").strip().lower()
        target = f"/head-manager/fbs-new/tasks/{task_id}/"
        try:
            if action == "details":
                due_date_raw = str(request.POST.get("due_date") or "").strip()
                due_date = parse_date(due_date_raw) if due_date_raw else None
                if due_date_raw and due_date is None:
                    raise TaskOperationError("Некорректная плановая дата.")
                save_task_details(
                    task_id=task_id,
                    actor=request.user,
                    title=request.POST.get("title"),
                    description=request.POST.get("description"),
                    internal_comment=request.POST.get("internal_comment"),
                    delivery_address=request.POST.get("delivery_address"),
                    contact_name=request.POST.get("contact_name"),
                    contact_phone=request.POST.get("contact_phone"),
                    vehicle_model=request.POST.get("vehicle_model"),
                    vehicle_number=request.POST.get("vehicle_number"),
                    driver_name=request.POST.get("driver_name"),
                    driver_phone=request.POST.get("driver_phone"),
                    warehouse_code=request.POST.get("warehouse_code"),
                    due_date=due_date,
                )
            elif action == "assignment":
                employee_id = str(request.POST.get("assigned_to_id") or "").strip()
                assigned_to = None
                if employee_id:
                    if not employee_id.isdigit():
                        raise TaskOperationError("Некорректный исполнитель.")
                    assigned_to = Employee.objects.get(pk=int(employee_id), is_active=True)
                update_task(
                    task_id=task_id,
                    status=str(request.POST.get("status") or ""),
                    priority=str(request.POST.get("priority") or ""),
                    assigned_to=assigned_to,
                    actor=request.user,
                )
            elif action == "add_item":
                add_task_item(
                    task_id=task_id,
                    product_id=int(request.POST.get("product_id") or 0),
                    planned_qty=request.POST.get("planned_qty"),
                    unit_price=request.POST.get("unit_price"),
                    technical_requirement=request.POST.get("technical_requirement"),
                    actor=request.user,
                )
            elif action == "remove_item":
                remove_task_item(
                    task_id=task_id,
                    item_id=int(request.POST.get("item_id") or 0),
                    actor=request.user,
                )
            elif action == "item_qty":
                record_task_item(
                    task_id=task_id,
                    item_id=int(request.POST.get("item_id") or 0),
                    processed_qty=request.POST.get("processed_qty"),
                    actor=request.user,
                )
            elif action == "create_box":
                create_task_box(
                    task_id=task_id,
                    code=str(request.POST.get("code") or ""),
                    actor=request.user,
                )
            elif action == "assign_box":
                box_id_raw = str(request.POST.get("box_id") or "").strip()
                assign_task_items_to_box(
                    task_id=task_id,
                    box_id=int(box_id_raw) if box_id_raw.isdigit() else None,
                    item_ids=[int(value) for value in request.POST.getlist("item_ids") if value.isdigit()],
                    actor=request.user,
                )
            elif action == "box_status":
                set_task_box_status(
                    task_id=task_id,
                    box_id=int(request.POST.get("box_id") or 0),
                    status=str(request.POST.get("status") or ""),
                    actor=request.user,
                )
            elif action == "add_service":
                item_id_raw = str(request.POST.get("item_id") or "").strip()
                add_task_service(
                    task_id=task_id,
                    name=str(request.POST.get("name") or ""),
                    unit_price=request.POST.get("unit_price"),
                    quantity=request.POST.get("quantity"),
                    item_id=int(item_id_raw) if item_id_raw.isdigit() else None,
                    actor=request.user,
                )
            elif action == "remove_service":
                remove_task_service(
                    task_id=task_id,
                    service_id=int(request.POST.get("service_id") or 0),
                    actor=request.user,
                )
            elif action == "finalize_tariff":
                finalize_task_tariff(task_id=task_id, actor=request.user)
            elif action == "client_confirmation":
                set_task_client_confirmation(
                    task_id=task_id,
                    confirmed=str(request.POST.get("confirmed") or "") == "1",
                    actor=request.user,
                )
            elif action == "upload":
                attach_task_file(
                    task_id=task_id,
                    uploaded_file=request.FILES.get("file"),
                    actor=request.user,
                )
            elif action in {"advance", "complete", "cancel"}:
                apply_board_action(task_id=task_id, action=action, actor=request.user)
            else:
                raise TaskOperationError("Неизвестное действие с задачей.")
        except (TaskOperationError, ObjectDoesNotExist, TypeError, ValueError) as exc:
            messages.error(request, str(exc) or "Операция с задачей не выполнена.")
        else:
            messages.success(request, "Изменения задачи сохранены в FBS-NEW.")
        return redirect(target)


class HeadManagerFbsNewTaskBoardActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)
    board_sections = {
        WmsNewTask.TYPE_ACCEPTANCE: "tasks-acceptance",
        WmsNewTask.TYPE_PROCESSING: "tasks-processing",
        WmsNewTask.TYPE_SHIPMENT: "tasks-shipment",
        WmsNewTask.TYPE_OTHER: "tasks-other",
    }

    def post(self, request, task_id: int):
        task = get_object_or_404(WmsNewTask, pk=task_id)
        target = f"/head-manager/fbs-new/{self.board_sections.get(task.workflow_type, 'tasks-other')}/"
        return_query = str(request.POST.get("return_query") or "").lstrip("?")
        if return_query:
            target += "?" + return_query
        try:
            changed = apply_board_action(
                task_id=task.id,
                action=str(request.POST.get("action") or "advance"),
                actor=request.user,
            )
            messages.success(request, f"Стадия задачи №{changed.id} обновлена в FBS-NEW.")
        except (TaskOperationError, WmsNewTask.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Задача FBS-NEW не найдена.")
        return redirect(target)


class HeadManagerFbsNewMultiacceptanceActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)
    statuses = {"created", "in_progress", "done", "cancelled"}

    def post(self, request):
        target = "/head-manager/fbs-new/tasks-multiacceptance/"
        return_query = str(request.POST.get("return_query") or "").lstrip("?")
        if return_query:
            target += "?" + return_query
        action = str(request.POST.get("action") or "create").strip().lower()
        if action == "create":
            title = str(request.POST.get("title") or "").strip()
            partner_ids = [int(value) for value in request.POST.getlist("partner_ids") if value.isdigit()]
            partners = list(Agency.objects.filter(pk__in=partner_ids, archived=False).order_by("agn_name"))
            if not title or not partners:
                messages.error(request, "Укажите название и хотя бы одного партнера.")
                return redirect(target + ("&" if "?" in target else "?") + "create=1")
            try:
                sku_count = max(0, int(request.POST.get("sku_count") or 0))
                unit_count = max(0, int(request.POST.get("unit_count") or 0))
            except (TypeError, ValueError):
                messages.error(request, "Количество SKU и товара должно быть числом.")
                return redirect(target + ("&" if "?" in target else "?") + "create=1")
            with transaction.atomic():
                record = WmsNewRecord.objects.create(
                    module="tasks",
                    entity_type="multiacceptance",
                    source_key=f"manual:{uuid4().hex}",
                    title=title,
                    status="created",
                    agency=partners[0] if len(partners) == 1 else None,
                    payload={
                        "partner_ids": [item.id for item in partners],
                        "partner_names": [str(item.agn_name or item) for item in partners],
                        "partner_count": len(partners),
                        "sku_count": sku_count,
                        "unit_count": unit_count,
                        "comment": str(request.POST.get("comment") or "").strip(),
                    },
                    pilot_revision=1,
                )
                WmsNewEvent.objects.create(
                    entity_type="multiacceptance",
                    entity_id=record.id,
                    action="create",
                    actor=request.user,
                    after={"status": record.status, **record.payload},
                )
            messages.success(request, f"Мультиприемка №{record.id} создана в FBS-NEW.")
            return redirect(target)
        record = get_object_or_404(
            WmsNewRecord,
            pk=request.POST.get("record_id"),
            module="tasks",
            entity_type="multiacceptance",
        )
        next_status = str(request.POST.get("status") or "").strip()
        if next_status not in self.statuses:
            return HttpResponseBadRequest("Неизвестный статус мультиприемки")
        with transaction.atomic():
            before = {"status": record.status, "pilot_revision": record.pilot_revision}
            record.status = next_status
            record.pilot_revision += 1
            record.save(update_fields=("status", "pilot_revision", "updated_at"))
            WmsNewEvent.objects.create(
                entity_type="multiacceptance",
                entity_id=record.id,
                action="status",
                actor=request.user,
                before=before,
                after={"status": record.status, "pilot_revision": record.pilot_revision},
            )
        messages.success(request, f"Мультиприемка №{record.id} обновлена.")
        return redirect(target)


class HeadManagerFbsNewProblemActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        source_key = str(request.POST.get("source_key") or "").strip()
        next_status = str(request.POST.get("status") or "").strip()
        if not source_key or next_status not in {"open", "resolved"}:
            return HttpResponseBadRequest("Некорректное изменение проблемы")
        with transaction.atomic():
            record, created = WmsNewRecord.objects.select_for_update().get_or_create(
                module="problems",
                entity_type="problem_state",
                source_key=source_key,
                defaults={"title": source_key, "status": next_status, "pilot_revision": 1},
            )
            before = {} if created else {"status": record.status, "pilot_revision": record.pilot_revision}
            payload = dict(record.payload or {})
            payload["resolved_at"] = timezone.now().isoformat() if next_status == "resolved" else ""
            if not created:
                record.status = next_status
                record.payload = payload
                record.pilot_revision += 1
                record.save(update_fields=("status", "payload", "pilot_revision", "updated_at"))
            else:
                record.payload = payload
                record.save(update_fields=("payload", "updated_at"))
            WmsNewEvent.objects.create(
                entity_type="problem",
                entity_id=record.id,
                action="resolve" if next_status == "resolved" else "reopen",
                actor=request.user,
                before=before,
                after={"status": record.status, "pilot_revision": record.pilot_revision},
                metadata={"source_key": source_key},
            )
        messages.success(request, "Статус изменен только в FBS-NEW.")
        target = "/head-manager/fbs-new/problems/"
        return_query = str(request.POST.get("return_query") or "").lstrip("?")
        return redirect(target + (("?" + return_query) if return_query else ""))


def _product_form_values(request) -> dict:
    return {
        "name": request.POST.get("name"),
        "article": request.POST.get("article"),
        "barcode": request.POST.get("barcode"),
        "color": request.POST.get("color"),
        "weight_grams": request.POST.get("weight_grams"),
        "size": request.POST.get("size"),
        "category": request.POST.get("category"),
        "width_cm": request.POST.get("width_cm"),
        "depth_cm": request.POST.get("depth_cm"),
        "height_cm": request.POST.get("height_cm"),
        "image_url": request.POST.get("image_url"),
        "internal_notes": request.POST.get("internal_notes"),
        "description": request.POST.get("description"),
        "is_bundle": bool(request.POST.get("is_bundle")),
    }


def _product_queryset_from_request(request):
    queryset = WmsNewProduct.objects.filter(is_archived=False)
    search = str(request.GET.get("q") or "").strip()
    if search:
        query = Q(name__icontains=search) | Q(article__icontains=search) | Q(barcode__icontains=search)
        if search.isdigit():
            query |= Q(pk=int(search))
        queryset = queryset.filter(query)
    partner = str(request.GET.get("partner") or "").strip()
    if partner.isdigit():
        queryset = queryset.filter(agency_id=int(partner))
    category = str(request.GET.get("category") or "").strip()
    if category:
        queryset = queryset.filter(category=category)
    bundle = str(request.GET.get("bundle") or "all").strip()
    if bundle == "without":
        queryset = queryset.filter(is_bundle=False)
    elif bundle == "only":
        queryset = queryset.filter(is_bundle=True)
    if request.GET.get("nonzero"):
        queryset = queryset.filter(stock_on_hand__gt=0)
    if request.GET.get("reserved"):
        queryset = queryset.filter(
            Q(fbo_reserved__gt=0) | Q(fbs_reserved__gt=0) | Q(internal_reserved__gt=0)
        )
    return queryset


class HeadManagerFbsNewProductCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        agency_id = str(request.POST.get("agency_id") or "").strip()
        if not agency_id.isdigit():
            messages.error(request, "Выберите партнера.")
            return redirect("/head-manager/fbs-new/warehouse-goods/?create=1")
        agency = get_object_or_404(Agency, pk=int(agency_id), archived=False)
        try:
            product = create_product(agency=agency, actor=request.user, **_product_form_values(request))
        except ProductOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/warehouse-goods/?create=1")
        messages.success(request, f"Товар №{product.id} создан в WMS NEW.")
        return redirect(f"/head-manager/fbs-new/warehouse-goods/?product_id={product.id}")


class HeadManagerFbsNewProductUpdateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, product_id: int):
        try:
            product = update_product(
                product_id=product_id,
                actor=request.user,
                **_product_form_values(request),
            )
        except (ProductOperationError, WmsNewProduct.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Товар WMS NEW не найден.")
            return redirect("/head-manager/fbs-new/warehouse-goods/")
        messages.success(request, f"Товар №{product.id} обновлен.")
        return redirect(f"/head-manager/fbs-new/warehouse-goods/?product_id={product.id}")


class HeadManagerFbsNewProductActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            product_ids = [int(value) for value in request.POST.getlist("product_ids")]
        except (TypeError, ValueError):
            product_ids = []
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "assign_category":
                changed = assign_category(
                    product_ids=product_ids,
                    category=str(request.POST.get("category") or ""),
                    actor=request.user,
                )
                messages.success(request, f"Категория назначена для {changed} товаров.")
                return redirect("/head-manager/fbs-new/warehouse-goods/")
            if action == "merge":
                target_id = str(request.POST.get("target_id") or "").strip()
                target = merge_products(
                    product_ids=product_ids,
                    target_id=int(target_id) if target_id.isdigit() else None,
                    actor=request.user,
                )
                messages.success(request, f"Товары объединены в карточку №{target.id}.")
                return redirect(f"/head-manager/fbs-new/warehouse-goods/?product_id={target.id}")
            raise ProductOperationError("Неизвестное действие с товарами.")
        except ProductOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/warehouse-goods/")


class HeadManagerFbsNewProductExportView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request):
        payload = export_products(_product_queryset_from_request(request))
        response = HttpResponse(
            payload,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="wms-new-products.xlsx"'
        return response


class HeadManagerFbsNewProductImportView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        uploaded = request.FILES.get("file")
        if not uploaded:
            messages.error(request, "Выберите Excel-файл.")
            return redirect("/head-manager/fbs-new/warehouse-goods/?import=1")
        try:
            result = import_products(uploaded, actor=request.user)
        except ProductOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/warehouse-goods/?import=1")
        messages.success(
            request,
            f"Импорт завершен: создано {result['created']}, обновлено {result['updated']}.",
        )
        return redirect("/head-manager/fbs-new/warehouse-goods/")


def _marking_queryset_from_request(request):
    queryset = WmsNewMarkingCode.objects.select_related("agency", "product")
    search = str(request.GET.get("q") or "").strip()
    if search:
        queryset = queryset.filter(code__icontains=search)
    partner = str(request.GET.get("partner") or "").strip()
    if partner.isdigit():
        queryset = queryset.filter(agency_id=int(partner))
    product = str(request.GET.get("product") or "").strip()
    if product.isdigit():
        queryset = queryset.filter(product_id=int(product))
    return queryset


class HeadManagerFbsNewMarkingCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        agency_id = str(request.POST.get("agency_id") or "").strip()
        product_id = str(request.POST.get("product_id") or "").strip()
        if not agency_id.isdigit() or not product_id.isdigit():
            messages.error(request, "Выберите партнера и товар.")
            return redirect("/head-manager/fbs-new/warehouse-marking/?create=1")
        agency = get_object_or_404(Agency, pk=int(agency_id), archived=False)
        product = get_object_or_404(
            WmsNewProduct,
            pk=int(product_id),
            is_archived=False,
        )
        raw_codes = str(request.POST.get("codes") or "")
        uploaded = request.FILES.get("file")
        file_name = ""
        if uploaded:
            file_name = str(uploaded.name or "")[:255]
            payload = uploaded.read()
            try:
                raw_codes = payload.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    raw_codes = payload.decode("cp1251")
                except UnicodeDecodeError:
                    messages.error(request, "Файл должен быть текстовым в UTF-8 или Windows-1251.")
                    return redirect("/head-manager/fbs-new/warehouse-marking/?create=1")
        try:
            created = add_codes(
                agency=agency,
                product=product,
                codes=raw_codes,
                code_type=str(request.POST.get("code_type") or WmsNewMarkingCode.TYPE_UNIT),
                file_name=file_name,
                actor=request.user,
            )
        except MarkingOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/warehouse-marking/?create=1")
        messages.success(request, f"В WMS NEW добавлено кодов: {len(created)}.")
        return redirect("/head-manager/fbs-new/warehouse-marking/")


class HeadManagerFbsNewMarkingActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            code_ids = [int(value) for value in request.POST.getlist("code_ids")]
        except (TypeError, ValueError):
            code_ids = []
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "print":
                items = mark_printed(code_ids=code_ids, actor=request.user)
                return render(
                    request,
                    "head_manager/wms_new_marking_print.html",
                    {"items": items, "printed_at": timezone.now()},
                )
            if action == "return":
                changed = return_codes(code_ids=code_ids, actor=request.user)
                messages.success(request, f"Возвращено кодов: {changed}.")
                return redirect("/head-manager/fbs-new/warehouse-marking/")
            raise MarkingOperationError("Неизвестное действие с кодами маркировки.")
        except MarkingOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/warehouse-marking/")


class HeadManagerFbsNewMarkingExportView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request):
        payload = export_marking_codes(_marking_queryset_from_request(request))
        response = HttpResponse(
            payload,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="wms-new-marking-codes.xlsx"'
        return response


class HeadManagerFbsNewExtraFieldExportView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request):
        data = build_section_data("warehouse-extra-fields", request=request)
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Дополнительные поля"
        worksheet.append(list(data["export_columns"]))
        for row in data["export_rows"]:
            worksheet.append(list(row))
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.font = cell.font.copy(bold=True)
        output = BytesIO()
        workbook.save(output)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = (
            'attachment; filename="fbs-new-extra-field-values.xlsx"'
        )
        return response


class HeadManagerFbsNewInventoryExportView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def get(self, request, inventory_id: int):
        query = request.GET.copy()
        query["inventory_id"] = str(inventory_id)
        request.GET = query
        data = build_section_data("warehouse-inventories", request=request)
        if not data["selected_inventory"]:
            raise Http404("Инвентаризация не найдена.")
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Результаты"
        worksheet.append(list(data["export_columns"]))
        for row in data["export_rows"]:
            worksheet.append(list(row))
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.font = cell.font.copy(bold=True)
        output = BytesIO()
        workbook.save(output)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="fbs-new-inventory-{inventory_id}.xlsx"'
        )
        return response


def _extra_definition_values(request) -> dict:
    return {
        "name": request.POST.get("name"),
        "code": request.POST.get("code"),
        "field_type": str(
            request.POST.get("field_type") or WmsNewExtraFieldDefinition.TYPE_TEXT
        ),
        "sort_order": request.POST.get("sort_order"),
        "options": [],
    }


class HeadManagerFbsNewExtraFieldCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        try:
            item = create_definition(actor=request.user, **_extra_definition_values(request))
        except ExtraFieldOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/warehouse-extra-fields/?create=1")
        messages.success(request, f"Дополнительное поле «{item.name}» создано в WMS NEW.")
        return redirect(f"/head-manager/fbs-new/warehouse-extra-fields/?field_id={item.id}")


class HeadManagerFbsNewExtraFieldUpdateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, definition_id: int):
        try:
            item = update_definition(
                definition_id=definition_id,
                actor=request.user,
                **_extra_definition_values(request),
            )
        except (ExtraFieldOperationError, WmsNewExtraFieldDefinition.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Дополнительное поле не найдено.")
            return redirect("/head-manager/fbs-new/warehouse-extra-fields/")
        messages.success(request, f"Поле «{item.name}» обновлено.")
        return redirect(f"/head-manager/fbs-new/warehouse-extra-fields/?field_id={item.id}")


class HeadManagerFbsNewExtraFieldActiveView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, definition_id: int):
        active = str(request.POST.get("active") or "").strip() == "1"
        try:
            item = set_definition_active(
                definition_id=definition_id,
                active=active,
                actor=request.user,
            )
        except WmsNewExtraFieldDefinition.DoesNotExist:
            messages.error(request, "Дополнительное поле не найдено.")
            return redirect("/head-manager/fbs-new/warehouse-extra-fields/")
        messages.success(
            request,
            f"Поле «{item.name}» {'включено' if active else 'отключено'}.",
        )
        return redirect(f"/head-manager/fbs-new/warehouse-extra-fields/?field_id={item.id}")


class HeadManagerFbsNewExtraFieldValueView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, definition_id: int):
        product_id = str(request.POST.get("product_id") or "").strip()
        if not product_id.isdigit():
            messages.error(request, "Выберите товар.")
            return redirect(
                f"/head-manager/fbs-new/warehouse-extra-fields/?field_id={definition_id}"
            )
        try:
            set_product_value(
                definition_id=definition_id,
                product_id=int(product_id),
                raw_value=request.POST.get("value"),
                actor=request.user,
            )
        except (
            ExtraFieldOperationError,
            WmsNewExtraFieldDefinition.DoesNotExist,
            WmsNewProduct.DoesNotExist,
        ) as exc:
            messages.error(request, str(exc) or "Не удалось сохранить значение.")
        else:
            messages.success(request, "Значение сохранено в WMS NEW.")
        return redirect(
            f"/head-manager/fbs-new/warehouse-extra-fields/?field_id={definition_id}"
        )


def _inventory_employee_role(request) -> str:
    employee = get_request_employee(request)
    return str(employee.role if employee else "")


def _inventory_can_count(request) -> bool:
    return is_developer_login(request.user) or _inventory_employee_role(request) in INVENTORY_COUNTER_ROLES


def _inventory_can_manage(request) -> bool:
    return is_developer_login(request.user) or _inventory_employee_role(request) in INVENTORY_MANAGER_ROLES


class HeadManagerFbsNewInventoryCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        if not _inventory_can_manage(request):
            return HttpResponseForbidden("Создать инвентаризацию может только начальник склада.")
        scope_type = str(request.POST.get("scope_type") or "").strip()
        object_id = str(request.POST.get("scope_object_id") or "").strip()
        kwargs = {}
        try:
            if scope_type == WmsNewInventorySession.SCOPE_ALL:
                pass
            elif not object_id.isdigit():
                raise InventoryOperationError("Выберите объект инвентаризации.")
            elif scope_type == WmsNewInventorySession.SCOPE_AGENCY:
                kwargs["agency"] = Agency.objects.get(pk=int(object_id), archived=False)
            elif scope_type == WmsNewInventorySession.SCOPE_CELL:
                kwargs["cell"] = FbsStorageCell.objects.get(pk=int(object_id), is_active=True)
            elif scope_type == WmsNewInventorySession.SCOPE_PALLET:
                kwargs["pallet"] = FbsPallet.objects.exclude(
                    status=FbsPallet.STATUS_ARCHIVED
                ).get(pk=int(object_id))
                kwargs["agency"] = kwargs["pallet"].agency
            elif scope_type == WmsNewInventorySession.SCOPE_BOX:
                kwargs["box"] = FbsBox.objects.exclude(status=FbsBox.STATUS_ARCHIVED).get(
                    pk=int(object_id)
                )
                kwargs["agency"] = kwargs["box"].agency
            elif scope_type == WmsNewInventorySession.SCOPE_PRODUCT:
                kwargs["product"] = WmsNewProduct.objects.get(
                    pk=int(object_id),
                    is_archived=False,
                )
                kwargs["agency"] = kwargs["product"].agency
            else:
                raise InventoryOperationError("Выберите поддерживаемую область инвентаризации.")
            session = create_inventory(
                scope_type=scope_type,
                mode=str(
                    request.POST.get("mode")
                    or WmsNewInventorySession.MODE_AUDIT
                ),
                scan_mode=str(
                    request.POST.get("scan_mode")
                    or WmsNewInventorySession.SCAN_MODE_BARCODE
                ),
                actor=request.user,
                **kwargs,
            )
        except (
            InventoryOperationError,
            Agency.DoesNotExist,
            FbsStorageCell.DoesNotExist,
            FbsPallet.DoesNotExist,
            FbsBox.DoesNotExist,
            WmsNewProduct.DoesNotExist,
        ) as exc:
            messages.error(request, str(exc) or "Не удалось создать инвентаризацию.")
            return redirect("/head-manager/fbs-new/warehouse-inventories/?create=1")
        messages.success(request, f"Инвентаризация {session.number} создана в WMS NEW.")
        return redirect(
            f"/head-manager/fbs-new/warehouse-inventories/?inventory_id={session.id}"
        )


class HeadManagerFbsNewInventoryActionView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, inventory_id: int):
        action = str(request.POST.get("action") or "").strip()
        if action in {"activate", "approve", "cancel"} and not _inventory_can_manage(request):
            return HttpResponseForbidden("Операция доступна только начальнику склада.")
        if action in {"scan", "finish"} and not _inventory_can_count(request):
            return HttpResponseForbidden("Пересчет доступен только сотруднику склада.")
        try:
            if action == "activate":
                activate_inventory(session_id=inventory_id, actor=request.user)
                messages.success(request, "Пересчет начат в WMS NEW.")
            elif action == "scan":
                line = record_inventory_scan(
                    session_id=inventory_id,
                    scan_code=request.POST.get("scan_code"),
                    actor=request.user,
                )
                messages.success(
                    request,
                    f"Скан принят: {line.sku_code}, пересчитано {line.first_count_qty or line.second_count_qty or 0}.",
                )
            elif action == "finish":
                session = finish_inventory_count(
                    session_id=inventory_id,
                    actor=request.user,
                )
                messages.success(request, f"Пересчет завершен: {session.get_status_display()}.")
            elif action == "approve":
                final_counts = {}
                for key, value in request.POST.items():
                    if not key.startswith("final_") or not str(value).strip():
                        continue
                    try:
                        final_counts[int(key.removeprefix("final_"))] = int(value)
                    except (TypeError, ValueError):
                        raise InventoryOperationError(
                            "Итоговые количества должны быть целыми числами."
                        )
                approve_inventory(
                    session_id=inventory_id,
                    actor=request.user,
                    final_counts=final_counts,
                )
                messages.success(request, "Инвентаризация утверждена в WMS NEW.")
            elif action == "cancel":
                cancel_inventory(session_id=inventory_id, actor=request.user)
                messages.success(request, "Инвентаризация отменена в WMS NEW.")
            else:
                raise InventoryOperationError("Неизвестная команда инвентаризации.")
        except (InventoryOperationError, WmsNewInventorySession.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Инвентаризация WMS NEW не найдена.")
        return redirect(
            f"/head-manager/fbs-new/warehouse-inventories/?inventory_id={inventory_id}"
        )


def _box_form_location(request):
    location_id = str(request.POST.get("location_id") or "").strip()
    if not location_id:
        return None
    if not location_id.isdigit():
        raise BoxOperationError("Выберите место хранения.")
    try:
        return WarehouseLocation.objects.get(pk=int(location_id), is_active=True)
    except WarehouseLocation.DoesNotExist as exc:
        raise BoxOperationError("Место хранения не найдено.") from exc


def _box_form_values(request) -> dict:
    return {
        "code": request.POST.get("code"),
        "location": _box_form_location(request),
        "gross_weight_g": request.POST.get("gross_weight_g"),
        "width_mm": request.POST.get("width_mm"),
        "height_mm": request.POST.get("height_mm"),
        "depth_mm": request.POST.get("depth_mm"),
    }


class HeadManagerFbsNewBoxCreateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request):
        if not _inventory_can_manage(request):
            return HttpResponseForbidden("Создать короб может только начальник склада.")
        agency_id = str(request.POST.get("agency_id") or "").strip()
        if not agency_id.isdigit():
            messages.error(request, "Выберите партнера.")
            return redirect("/head-manager/fbs-new/warehouse-boxes/?create=1")
        agency = get_object_or_404(Agency, pk=int(agency_id), archived=False)
        container_type = str(request.POST.get("container_type") or "box").strip()
        try:
            quantity = int(request.POST.get("quantity") or 1)
        except (TypeError, ValueError):
            quantity = 0
        if quantity < 1 or quantity > 100:
            messages.error(request, "Количество контейнеров должно быть от 1 до 100.")
            return redirect("/head-manager/fbs-new/warehouse-boxes/?create=1")
        values = _box_form_values(request)
        try:
            created = []
            with transaction.atomic():
                for index in range(quantity):
                    code = str(values.get("code") or "").strip()
                    if not code or quantity > 1:
                        prefix = "PAL" if container_type == "pallet" else "BOX"
                        code = f"FBN-{prefix}-{uuid4().hex[:10].upper()}"
                    created.append(
                        create_box(
                            agency=agency,
                            actor=request.user,
                            container_type=container_type,
                            **{**values, "code": code},
                        )
                    )
        except BoxOperationError as exc:
            messages.error(request, str(exc))
            return redirect("/head-manager/fbs-new/warehouse-boxes/?create=1")
        label = "паллета" if container_type == "pallet" else "короб"
        messages.success(request, f"Создано: {quantity}, тип: {label}, только в WMS NEW.")
        if quantity == 1:
            return redirect(f"/head-manager/fbs-new/warehouse-boxes/?box_id={created[0].id}")
        return redirect("/head-manager/fbs-new/warehouse-boxes/")


class HeadManagerFbsNewBoxUpdateView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, box_id: int):
        if not _inventory_can_count(request):
            return HttpResponseForbidden("Изменить место короба может только сотрудник склада.")
        try:
            box = update_box(
                box_id=box_id,
                actor=request.user,
                **_box_form_values(request),
            )
        except (BoxOperationError, WmsNewBox.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Короб WMS NEW не найден.")
            return redirect("/head-manager/fbs-new/warehouse-boxes/")
        messages.success(request, f"Короб {box.code} обновлен в WMS NEW.")
        return redirect(f"/head-manager/fbs-new/warehouse-boxes/?box_id={box.id}")


class HeadManagerFbsNewBoxArchiveView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", FBS_NEW_ACCESS_ROLE)

    def post(self, request, box_id: int):
        if not _inventory_can_manage(request):
            return HttpResponseForbidden("Архивировать короб может только начальник склада.")
        archived = str(request.POST.get("archived") or "").strip() == "1"
        try:
            box = set_box_archived(box_id=box_id, archived=archived, actor=request.user)
        except (BoxOperationError, WmsNewBox.DoesNotExist) as exc:
            messages.error(request, str(exc) or "Короб WMS NEW не найден.")
            return redirect(f"/head-manager/fbs-new/warehouse-boxes/?box_id={box_id}")
        messages.success(
            request,
            f"Короб {box.code} {'архивирован' if archived else 'восстановлен'} в WMS NEW.",
        )
        return redirect(f"/head-manager/fbs-new/warehouse-boxes/?box_id={box.id}")
