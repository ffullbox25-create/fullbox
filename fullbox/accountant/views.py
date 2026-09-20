from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q, Sum
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import CreateView, TemplateView, UpdateView

from billing.models import (
    ApplicationCharge,
    BillingApplication,
    BillingService,
    ClientTariffItem,
    ClientTariffVersion,
    ClientLogisticsTariff,
    ClientLogisticsTariffItem,
    LogisticsPriceLine,
    TariffCategory,
    TariffUnit,
    StorageAdjustment,
)
from billing.permissions import can_manage_charges, filter_applications_for_user
from billing.tariff_services import active_tariff_version, tariff_history
from employees.access import RoleRequiredMixin, get_request_effective_role
from head_manager.models import Carrier, OwnCompany
from sku.models import Agency

from .dashboard import AccountantDashboardService
from .forms import AccountantCarrierForm, AccountantOwnCompanyForm
from .menu import build_menu_context
from .models import ClientChangeLog, ClientLifecycle
from .section_permissions import can_see_menu_key
from .sections import get_section
from .selectors import ACCOUNTANT_ROLES, accountant_clients_queryset, ensure_lifecycle
from .services import client_delete_states_for_agencies, delete_draft_tariff_version, delete_empty_client, validate_activate_client


def _accountant_request_role(request):
    return get_request_effective_role(
        request,
        preferred_roles=("admin", "director", "accountant", "head_manager"),
    )


class AccountantRoleMixin(RoleRequiredMixin):
    allowed_roles = tuple(ACCOUNTANT_ROLES)
    active_nav = "overview"
    section_permission_key: str | None = None

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and request.user.is_superuser:
            return super(RoleRequiredMixin, self).dispatch(request, *args, **kwargs)
        response = super().dispatch(request, *args, **kwargs)
        return response

    def check_section_permission(self, request):
        perm_key = self.section_permission_key
        if not perm_key or request.user.is_superuser:
            return None
        role = _accountant_request_role(request)
        if role in {"admin", "director"}:
            return None
        if not can_see_menu_key(role, perm_key):
            return HttpResponseForbidden("Доступ запрещен")
        return None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        role = _accountant_request_role(self.request) or ("admin" if self.request.user.is_superuser else None)
        dash = AccountantDashboardService()
        counters = dash.counters()
        ctx["active_nav"] = getattr(self, "active_nav", "overview")
        ctx["role"] = role or "accountant"
        ctx["accountant_badges"] = {
            "drafts": counters.get("drafts", 0),
            "review": counters.get("review", 0),
            "active": counters.get("active", 0),
        }
        ctx["accountant_counters"] = counters
        ctx["accountant_menu"] = build_menu_context(
            request_path=self.request.path,
            counters=counters,
            role=role or "accountant",
        )
        return ctx


class AccountantBaseView(AccountantRoleMixin, TemplateView):
    def dispatch(self, request, *args, **kwargs):
        denied = self.check_section_permission(request)
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)


class AccountantOverviewView(AccountantBaseView):
    template_name = "accountant/overview.html"
    active_nav = "overview"
    section_permission_key = "overview"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        dash = AccountantDashboardService()
        counters = ctx.get("accountant_counters") or dash.counters()
        ctx.update(
            {
                "kpi_cards": dash.kpi_cards(counters),
                "attention_items": dash.attention_queue(counters),
                "quick_start_steps": dash.quick_start_steps(),
                "quick_actions": dash.quick_actions(),
            }
        )
        return ctx


class AccountantInvestorPreviewView(AccountantBaseView):
    """Безопасная демо-витрина будущего кабинета инвестора.

    Экран не создаёт отдельную роль и не даёт доступа к финансовым операциям.
    Значения намеренно демонстрационные, пока не согласованы источники данных и
    права инвестора.
    """

    template_name = "accountant/investor_preview.html"
    active_nav = "overview"
    section_permission_key = "overview"


class AccountantSectionView(AccountantBaseView):
    """Stub / bridge / alias для новых разделов меню — без 404."""

    template_name = "accountant/section_page.html"

    def dispatch(self, request, *args, **kwargs):
        section_path = (kwargs.get("section_path") or "").strip("/")
        meta = get_section(section_path)
        if not meta:
            raise Http404("Раздел не найден")
        role = _accountant_request_role(request) or ("admin" if request.user.is_superuser else None)
        if not request.user.is_superuser and not can_see_menu_key(role, meta.permission_key):
            return HttpResponseForbidden("Доступ запрещен")
        if meta.kind == "alias" and meta.alias_url:
            return HttpResponseRedirect(meta.alias_url)
        if meta.kind == "bridge" and meta.bridge_url:
            return HttpResponseRedirect(meta.bridge_url)
        self.section_meta = meta
        self.active_nav = meta.permission_key
        self.section_permission_key = meta.permission_key
        return super().dispatch(request, *args, **kwargs)

    def get_template_names(self):
        if getattr(self, "section_meta", None) and self.section_meta.kind == "bridge":
            return ["accountant/section_bridge.html"]
        return ["accountant/section_page.html"]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        meta = self.section_meta
        ctx.update(
            {
                "section": meta,
                "page_title": meta.title,
                "page_sub": meta.group,
                "page_description": meta.description,
                "bridge_url": meta.bridge_url,
                "bridge_label": meta.bridge_label,
                "related_links": meta.related,
            }
        )
        return ctx


class AccountantServicesCatalogView(AccountantBaseView):
    template_name = "accountant/services_catalog.html"
    active_nav = "tariffs"
    section_permission_key = "tariffs_catalog"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["services"] = (
            BillingService.objects.select_related("category")
            .filter(is_active=True)
            .order_by("category__sort_order", "name")[:500]
        )
        return ctx


class AccountantTariffNoPriceView(AccountantBaseView):
    template_name = "accountant/tariff_no_price.html"
    active_nav = "tariffs"
    section_permission_key = "tariffs_no_price"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        items = (
            ClientTariffItem.objects.select_related("tariff_version", "tariff_version__client", "service", "category")
            .filter(tariff_version__status=ClientTariffVersion.STATUS_DRAFT, price=0)
            .order_by("tariff_version__client__agn_name", "sort_order")[:300]
        )
        ctx["zero_price_items"] = items
        return ctx


class AccountantChargesView(AccountantBaseView):
    """Реестр начислений для бухгалтера поверх единого контура биллинга."""

    template_name = "accountant/charges.html"
    active_nav = "charges"
    section_permission_key = "charges_all"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        applications = filter_applications_for_user(BillingApplication.objects.all(), self.request).distinct()
        charges = ApplicationCharge.objects.filter(application__in=applications).select_related(
            "application",
            "client",
            "service",
            "client_tariff_version",
        )
        params = self.request.GET
        scope = (params.get("scope") or "").strip()
        if client_id := params.get("client"):
            charges = charges.filter(client_id=client_id)
        if application_type := params.get("application_type"):
            charges = charges.filter(application__application_type=application_type)
        if date_from := params.get("date_from"):
            charges = charges.filter(performed_at__date__gte=date_from)
        if date_to := params.get("date_to"):
            charges = charges.filter(performed_at__date__lte=date_to)
        if params.get("unconfirmed") in {"1", "true", "True"}:
            charges = charges.filter(is_confirmed=False, is_disputed=False, is_excluded=False)
        if params.get("no_price") in {"1", "true", "True"}:
            charges = charges.filter(client_tariff_version__isnull=True, is_manual_override=False)
        if scope == "manual":
            charges = charges.filter(
                Q(source_type=ApplicationCharge.SOURCE_MANUAL) | Q(is_manual_override=True)
            )
        if query := (params.get("q") or "").strip():
            charges = charges.filter(
                Q(service__name__icontains=query)
                | Q(service_name_snapshot__icontains=query)
                | Q(application__application_id__icontains=query)
                | Q(client__agn_name__icontains=query)
                | Q(client__short_name__icontains=query)
                | Q(tariff_source_label__icontains=query)
            )

        totals = charges.aggregate(
            subtotal=Sum("amount"),
            vat=Sum("vat_amount"),
            total=Sum("total_amount"),
        )
        from billing.charge_status import charge_manager_status

        rows = []
        for charge in charges.order_by("-performed_at", "-id")[:300]:
            _, status_label, status_badge = charge_manager_status(charge)
            rows.append({"charge": charge, "status_label": status_label, "status_badge": status_badge})

        ctx.update(
            {
                "charges_title": "Ручные начисления" if scope == "manual" else "Начисления по заявкам" if scope == "applications" else "Все начисления",
                "charges_description": "Строки, добавленные или скорректированные вручную." if scope == "manual" else "Расчёты по складским заявкам и хранению. Данные обновляются из биллинга.",
                "charge_rows": rows,
                "charges_count": charges.count(),
                "charges_apps_count": charges.values("application_id").distinct().count(),
                "charges_total": totals["total"] or 0,
                "charges_vat": totals["vat"] or 0,
                "charges_unconfirmed": charges.filter(
                    is_confirmed=False, is_disputed=False, is_excluded=False
                ).count(),
                "charges_no_price": charges.filter(
                    client_tariff_version__isnull=True, is_manual_override=False
                ).count(),
                "charges_can_manage": can_manage_charges(self.request),
                "charge_clients": Agency.objects.filter(billing_charges__application__in=applications)
                .distinct()
                .order_by("agn_name", "short_name"),
            }
        )
        return ctx


class AccountantChargeAdjustmentsView(AccountantBaseView):
    template_name = "accountant/charge_adjustments.html"
    active_nav = "charges"
    section_permission_key = "charges_adjustments"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        adjustments = StorageAdjustment.objects.select_related(
            "client", "period", "source_day", "source_charge"
        ).order_by("-created_at")
        if client_id := self.request.GET.get("client"):
            adjustments = adjustments.filter(client_id=client_id)
        if status := self.request.GET.get("status"):
            adjustments = adjustments.filter(status=status)
        ctx.update(
            {
                "adjustments": adjustments[:300],
                "adjustment_count": adjustments.count(),
                "clients": Agency.objects.filter(storage_adjustments__isnull=False).distinct().order_by("agn_name"),
                "status_choices": StorageAdjustment.STATUS_CHOICES,
            }
        )
        return ctx


class AccountantClientsView(AccountantBaseView):
    template_name = "accountant/clients.html"
    active_nav = "clients"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        status = self.request.GET.get("status") or ""
        q = self.request.GET.get("q") or ""
        clients = list(accountant_clients_queryset(status=status or None, q=q)[:300])
        delete_states = client_delete_states_for_agencies(clients)
        rows = []
        for agency in clients:
            lifecycle = ensure_lifecycle(agency)
            rows.append(
                {
                    "agency": agency,
                    "lifecycle": lifecycle,
                    "delete_state": delete_states.get(
                        agency.id,
                        {"can_delete": False, "reasons": ["не удалось проверить связи"], "reason_text": "не удалось проверить связи"},
                    ),
                }
            )
        ctx.update(
            {
                "client_rows": rows,
                "filter_status": status,
                "filter_q": q,
                "status_choices": ClientLifecycle.STATUS_CHOICES,
                "type_choices": ClientLifecycle.TYPE_CHOICES,
                "vat_type_choices": ClientLifecycle.VAT_TYPE_CHOICES,
                "serving_companies": OwnCompany.objects.filter(is_active=True).order_by("short_name", "name"),
            }
        )
        return ctx


class AccountantClientDeleteView(AccountantRoleMixin, View):
    active_nav = "clients"
    section_permission_key = "clients"

    def post(self, request, *args, **kwargs):
        denied = self.check_section_permission(request)
        if denied is not None:
            return denied
        agency = get_object_or_404(Agency, pk=kwargs["pk"])
        next_url = request.POST.get("next") or reverse_lazy("accountant-clients")
        if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            next_url = reverse_lazy("accountant-clients")
        try:
            result = delete_empty_client(agency, user=request.user)
        except ValidationError as exc:
            message = exc.message if hasattr(exc, "message") else "; ".join(exc.messages)
            messages.error(request, message)
        except ProtectedError as exc:
            messages.error(request, f"Нельзя удалить клиента: есть защищённые связанные записи ({len(exc.protected_objects)}).")
        else:
            suffix = " Логин ЛК клиента тоже удалён." if result.get("portal_user_deleted") else ""
            messages.success(request, f"Клиент «{result['name']}» удалён.{suffix}")
        return HttpResponseRedirect(next_url)


class AccountantClientDetailView(AccountantBaseView):
    template_name = "accountant/client_detail.html"
    active_nav = "clients"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        agency = Agency.objects.filter(pk=kwargs["pk"]).select_related("lifecycle", "lifecycle__serving_company").first()
        if not agency:
            ctx["agency"] = None
            return ctx
        lifecycle = ensure_lifecycle(agency)
        current_tariff = active_tariff_version(agency)
        current_tariff_can_edit = current_tariff.can_edit_directly() if current_tariff else False
        current_tariff_edit_block_reason = (
            current_tariff.edit_block_reason() if current_tariff and not current_tariff_can_edit else ""
        )
        tariff_draft = (
            ClientTariffVersion.objects.filter(client=agency, status=ClientTariffVersion.STATUS_DRAFT)
            .order_by("-updated_at", "-id")
            .first()
        )
        history = tariff_history(agency)[:20]
        activation_errors = validate_activate_client(agency) if lifecycle.status != ClientLifecycle.STATUS_ACTIVE else []
        ctx.update(
            {
                "agency": agency,
                "lifecycle": lifecycle,
                "current_tariff": current_tariff,
                "current_tariff_can_edit": current_tariff_can_edit,
                "current_tariff_edit_block_reason": current_tariff_edit_block_reason,
                "tariff_draft": tariff_draft,
                "tariff_history": history,
                "activation_errors": activation_errors,
                "change_logs": ClientChangeLog.objects.filter(agency=agency).select_related("user")[:50],
                "serving_companies": OwnCompany.objects.filter(is_active=True).order_by("short_name", "name"),
                "type_choices": ClientLifecycle.TYPE_CHOICES,
                "status_choices": ClientLifecycle.STATUS_CHOICES,
                "vat_type_choices": ClientLifecycle.VAT_TYPE_CHOICES,
            }
        )
        return ctx


class AccountantTariffsView(AccountantBaseView):
    template_name = "accountant/tariffs.html"
    active_nav = "tariffs"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = ClientTariffVersion.objects.select_related("client", "contract__own_company").order_by("-valid_from", "-id")
        if self.request.GET.get("status"):
            qs = qs.filter(status=self.request.GET["status"])
        if self.request.GET.get("client"):
            qs = qs.filter(client_id=self.request.GET["client"])
        ctx["tariff_versions"] = qs[:200]
        ctx["clients"] = Agency.objects.filter(lifecycle__isnull=False).order_by("agn_name")[:500]
        return ctx


class AccountantTariffDetailView(AccountantBaseView):
    template_name = "accountant/tariff_detail.html"
    active_nav = "tariffs"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        version = (
            ClientTariffVersion.objects.select_related("client", "contract", "contract__own_company")
            .prefetch_related("items__category", "items__service", "items__unit")
            .filter(pk=kwargs["pk"])
            .first()
        )
        ctx["version"] = version
        if version:
            lifecycle = ensure_lifecycle(version.client)
            ctx["lifecycle"] = lifecycle
            items = list(
                version.items.select_related("category", "service", "service__standard_price", "unit").order_by(
                    "sort_order", "category__sort_order", "id"
                )
            )
            ctx["items"] = items
            ctx["billing_services"] = (
                BillingService.objects.filter(is_active=True, used_in_billing=True)
                .select_related("category")
                .order_by("sort_order", "name", "id")[:1000]
            )
            ctx["tariff_categories"] = TariffCategory.objects.filter(is_active=True).order_by("sort_order", "name")
            ctx["tariff_units"] = TariffUnit.objects.filter(is_active=True).order_by("name")
            ctx["tariff_can_edit"] = version.can_edit_directly()
            # Использованный тариф не редактируется, но может получить новую строку услуги.
            ctx["tariff_can_add_services"] = version.status != ClientTariffVersion.STATUS_ARCHIVED
            ctx["tariff_usage"] = version.billing_usage_summary()
            ctx["tariff_edit_block_reason"] = version.edit_block_reason() if not version.can_edit_directly() else ""
            from .services import validate_publish_tariff

            ctx["publish_errors"] = validate_publish_tariff(version) if version.status == ClientTariffVersion.STATUS_DRAFT else []
            ctx["serving_company"] = lifecycle.serving_company
            ctx["client_display"] = version.client.short_name or version.client.agn_name or f"Клиент #{version.client_id}"
            from billing.storage_serializers import rule_for_tariff_version, storage_rule_choices, storage_rule_dict

            rule = rule_for_tariff_version(version)
            ctx["storage_rule"] = rule
            ctx["storage_rule_data"] = storage_rule_dict(rule)
            ctx["storage_rule_choices"] = storage_rule_choices()
            storage_items = [
                i
                for i in items
                if (i.service and str(i.service.code or "").startswith("storage_"))
                or "хранен" in (i.service_name or "").lower()
            ]
            ctx["storage_items"] = storage_items
        return ctx


def _logistics_decimal(value, *, default="0.00") -> Decimal:
    try:
        return Decimal(str(value or default).replace(",", "."))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _logistics_int(value, *, default=0) -> int:
    try:
        return max(0, int(value or default))
    except (TypeError, ValueError):
        return default


def _copy_logistics_line(*, tariff: ClientLogisticsTariff, line: LogisticsPriceLine) -> ClientLogisticsTariffItem:
    return ClientLogisticsTariffItem.objects.create(
        tariff=tariff,
        source_line=line,
        marketplace=line.marketplace,
        warehouse_name=line.warehouse_name,
        pickup_days=line.pickup_days,
        delivery_days=line.delivery_days,
        price_per_pallet=line.price_per_pallet,
        minimum_pallets=line.minimum_pallets,
        discount_percent=line.discount_percent,
        comment=line.comment,
        sort_order=line.sort_order,
        is_active=line.is_active,
    )


class AccountantLogisticsPriceView(AccountantBaseView):
    template_name = "accountant/logistics_price.html"
    active_nav = "tariffs"
    section_permission_key = "tariffs_clients"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["lines"] = LogisticsPriceLine.objects.all()
        ctx["marketplace_choices"] = LogisticsPriceLine.MARKETPLACE_CHOICES
        return ctx

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        if action == "create":
            line = LogisticsPriceLine(
                marketplace=request.POST.get("marketplace") or LogisticsPriceLine.MARKETPLACE_OTHER,
                warehouse_name=(request.POST.get("warehouse_name") or "").strip(),
                pickup_days=(request.POST.get("pickup_days") or "").strip(),
                delivery_days=(request.POST.get("delivery_days") or "").strip(),
                price_per_pallet=_logistics_decimal(request.POST.get("price_per_pallet")),
                minimum_pallets=max(1, _logistics_int(request.POST.get("minimum_pallets"), default=1)),
                discount_percent=_logistics_decimal(request.POST.get("discount_percent")),
                comment=(request.POST.get("comment") or "").strip(),
                sort_order=_logistics_int(request.POST.get("sort_order"), default=100),
                is_active=bool(request.POST.get("is_active")),
            )
            try:
                line.full_clean()
                line.save()
                messages.success(request, "Направление добавлено в основной прайс логистики.")
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
        elif action == "update":
            line = get_object_or_404(LogisticsPriceLine, pk=request.POST.get("line_id"))
            for field in ("warehouse_name", "pickup_days", "delivery_days", "comment"):
                setattr(line, field, (request.POST.get(field) or "").strip())
            line.marketplace = request.POST.get("marketplace") or line.marketplace
            line.price_per_pallet = _logistics_decimal(request.POST.get("price_per_pallet"))
            line.minimum_pallets = max(1, _logistics_int(request.POST.get("minimum_pallets"), default=1))
            line.discount_percent = _logistics_decimal(request.POST.get("discount_percent"))
            line.sort_order = _logistics_int(request.POST.get("sort_order"), default=100)
            line.is_active = bool(request.POST.get("is_active"))
            try:
                line.full_clean()
                line.save()
                messages.success(request, "Основной прайс логистики сохранён.")
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
        elif action == "delete":
            get_object_or_404(LogisticsPriceLine, pk=request.POST.get("line_id")).delete()
            messages.success(request, "Направление удалено из основного прайса.")
        return HttpResponseRedirect(request.path)


class AccountantClientLogisticsTariffsView(AccountantBaseView):
    template_name = "accountant/client_logistics_tariffs.html"
    active_nav = "tariffs"
    section_permission_key = "tariffs_clients"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["tariffs"] = ClientLogisticsTariff.objects.select_related("client").prefetch_related("items").all()[:300]
        ctx["clients"] = Agency.objects.filter(lifecycle__isnull=False).order_by("agn_name")[:500]
        return ctx

    def post(self, request, *args, **kwargs):
        client = get_object_or_404(Agency, pk=request.POST.get("client_id"))
        latest = ClientLogisticsTariff.objects.filter(client=client).aggregate(max_version=Max("version_number"))["max_version"] or 0
        tariff = ClientLogisticsTariff.objects.create(
            client=client,
            name=(request.POST.get("name") or f"Логистика с {date.today():%d.%m.%Y}").strip(),
            version_number=latest + 1,
            valid_from=request.POST.get("valid_from") or date.today(),
            comment=(request.POST.get("comment") or "").strip(),
            created_by=request.user,
        )
        with transaction.atomic():
            for line in LogisticsPriceLine.objects.filter(is_active=True):
                _copy_logistics_line(tariff=tariff, line=line)
        messages.success(request, "Создан черновик логистического тарифа: в него скопированы активные строки основного прайса.")
        return HttpResponseRedirect(f"/accountant/logistics/tariffs/{tariff.id}/")


class AccountantClientLogisticsTariffDetailView(AccountantBaseView):
    template_name = "accountant/client_logistics_tariff_detail.html"
    active_nav = "tariffs"
    section_permission_key = "tariffs_clients"

    def _tariff(self, pk):
        return get_object_or_404(ClientLogisticsTariff.objects.select_related("client").prefetch_related("items"), pk=pk)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        tariff = self._tariff(kwargs["pk"])
        ctx.update({"tariff": tariff, "items": tariff.items.all(), "marketplace_choices": LogisticsPriceLine.MARKETPLACE_CHOICES})
        return ctx

    def post(self, request, *args, **kwargs):
        tariff = self._tariff(kwargs["pk"])
        action = request.POST.get("action")
        if action == "publish":
            if not tariff.items.filter(is_active=True).exists():
                messages.error(request, "Нельзя опубликовать пустой тариф: добавьте хотя бы одно активное направление.")
            else:
                with transaction.atomic():
                    ClientLogisticsTariff.objects.filter(client=tariff.client, status=ClientLogisticsTariff.STATUS_ACTIVE).exclude(pk=tariff.pk).update(status=ClientLogisticsTariff.STATUS_ARCHIVED)
                    tariff.status = ClientLogisticsTariff.STATUS_ACTIVE
                    tariff.save(update_fields=["status", "updated_at"])
                recalculated = 0
                from billing.services import BillingWorkflowService

                # Счета и акты не меняем: новый тариф применяется сразу к новым
                # заявкам, а открытые черновики безопасно пересчитываем здесь.
                applications = (
                    BillingApplication.objects.filter(
                        client=tariff.client,
                        application_type=BillingApplication.TYPE_SHIPPING,
                    )
                    .order_by("-created_at")[:250]
                )
                for application in applications:
                    if application.charges.filter(is_included_in_act=True).exists() or application.charges.filter(is_included_in_invoice=True).exists():
                        continue
                    try:
                        BillingWorkflowService.calculate_charges(application, user=request.user)
                    except ValidationError:
                        # Неготовый к биллингу черновик не должен отменять публикацию тарифа.
                        continue
                    recalculated += 1
                messages.success(request, f"Логистический тариф опубликован. Открытых заявок пересчитано: {recalculated}.")
        elif action == "meta":
            tariff.name = (request.POST.get("name") or tariff.name).strip()
            tariff.valid_from = request.POST.get("valid_from") or tariff.valid_from
            tariff.valid_to = request.POST.get("valid_to") or None
            tariff.comment = (request.POST.get("comment") or "").strip()
            try:
                tariff.full_clean()
                tariff.save()
                messages.success(request, "Параметры тарифа сохранены.")
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
        elif action == "add":
            item = ClientLogisticsTariffItem(
                tariff=tariff,
                marketplace=request.POST.get("marketplace") or LogisticsPriceLine.MARKETPLACE_OTHER,
                warehouse_name=(request.POST.get("warehouse_name") or "").strip(),
                pickup_days=(request.POST.get("pickup_days") or "").strip(),
                delivery_days=(request.POST.get("delivery_days") or "").strip(),
                price_per_pallet=_logistics_decimal(request.POST.get("price_per_pallet")),
                minimum_pallets=max(1, _logistics_int(request.POST.get("minimum_pallets"), default=1)),
                discount_percent=_logistics_decimal(request.POST.get("discount_percent")),
                comment=(request.POST.get("comment") or "").strip(),
                is_active=bool(request.POST.get("is_active")),
            )
            try:
                item.full_clean(); item.save()
                messages.success(request, "Направление добавлено в тариф клиента.")
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
        elif action == "update":
            item = get_object_or_404(ClientLogisticsTariffItem, pk=request.POST.get("item_id"), tariff=tariff)
            for field in ("warehouse_name", "pickup_days", "delivery_days", "comment"):
                setattr(item, field, (request.POST.get(field) or "").strip())
            item.marketplace = request.POST.get("marketplace") or item.marketplace
            item.price_per_pallet = _logistics_decimal(request.POST.get("price_per_pallet"))
            item.minimum_pallets = max(1, _logistics_int(request.POST.get("minimum_pallets"), default=1))
            item.discount_percent = _logistics_decimal(request.POST.get("discount_percent"))
            item.is_active = bool(request.POST.get("is_active"))
            try:
                item.full_clean(); item.save()
                messages.success(request, "Строка тарифа клиента сохранена.")
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
        elif action == "delete":
            get_object_or_404(ClientLogisticsTariffItem, pk=request.POST.get("item_id"), tariff=tariff).delete()
            messages.success(request, "Строка удалена из тарифа клиента.")
        return HttpResponseRedirect(request.path)


class AccountantTariffDeleteView(AccountantRoleMixin, View):
    active_nav = "tariffs"
    section_permission_key = "tariffs_clients"
    success_url = reverse_lazy("accountant-tariffs")

    def dispatch(self, request, *args, **kwargs):
        denied = self.check_section_permission(request)
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)

    def _next_url(self) -> str:
        next_url = self.request.POST.get("next") or self.request.GET.get("next") or ""
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return next_url
        if next_url.startswith("/accountant/"):
            return next_url
        return str(self.success_url)

    def post(self, request, *args, **kwargs):
        version = get_object_or_404(
            ClientTariffVersion.objects.select_related("client"),
            pk=kwargs["pk"],
        )
        client_name = version.client.short_name or version.client.agn_name or f"Клиент #{version.client_id}"
        try:
            result = delete_draft_tariff_version(version, user=request.user)
        except ValidationError as exc:
            messages.error(request, "; ".join(str(m) for m in exc.messages))
        else:
            messages.success(
                request,
                f"Черновик прайса «{result['name']}» для {client_name} удалён.",
            )
        return HttpResponseRedirect(self._next_url())


class AccountantHistoryView(AccountantBaseView):
    template_name = "accountant/history.html"
    active_nav = "history"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["change_logs"] = ClientChangeLog.objects.select_related("agency", "user").order_by("-changed_at")[:200]
        return ctx


class AccountantStorageView(AccountantBaseView):
    template_name = "accountant/storage.html"
    active_nav = "storage"

    def get_context_data(self, **kwargs):
        from django.utils import timezone

        from billing.models import BillingStorageDay, StorageBillingError, StorageCalculationRule

        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        year = int(self.request.GET.get("year") or today.year)
        month = int(self.request.GET.get("month") or today.month)
        client_id = self.request.GET.get("client") or ""
        days = BillingStorageDay.objects.select_related("client", "charge").filter(day__year=year, day__month=month)
        if client_id:
            days = days.filter(client_id=client_id)
        ctx.update(
            {
                "year": year,
                "month": month,
                "filter_client": client_id,
                "storage_days": days.order_by("-day", "client__agn_name")[:300],
                "open_errors": StorageBillingError.objects.filter(resolved_at__isnull=True).select_related("client")[:100],
                "mode_choices": StorageCalculationRule.MODE_CHOICES,
                "clients": Agency.objects.order_by("agn_name")[:500],
            }
        )
        return ctx


class AccountantTariffCheckView(AccountantBaseView):
    template_name = "accountant/tariff_check.html"
    active_nav = "tariff_check"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        problems = []
        for lifecycle in ClientLifecycle.objects.filter(
            status__in=[
                ClientLifecycle.STATUS_ACTIVE,
                ClientLifecycle.STATUS_TARIFFS_READY,
                ClientLifecycle.STATUS_REQUISITES_READY,
            ]
        ).select_related("agency", "serving_company")[:300]:
            errs = validate_activate_client(lifecycle.agency)
            if errs or not active_tariff_version(lifecycle.agency):
                problems.append({"agency": lifecycle.agency, "lifecycle": lifecycle, "errors": errs})
        ctx["problems"] = problems
        return ctx


class AccountantContractsView(AccountantBaseView):
    template_name = "accountant/contracts.html"
    active_nav = "contracts"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows = []
        for agency in accountant_clients_queryset()[:300]:
            lifecycle = ensure_lifecycle(agency)
            rows.append(
                {
                    "agency": agency,
                    "lifecycle": lifecycle,
                    "contract": agency.contract_numb,
                    "kp": lifecycle.commercial_offer_number,
                    "company": lifecycle.serving_company,
                }
            )
        ctx["contract_rows"] = rows
        return ctx


class AccountantOwnCompaniesView(AccountantBaseView):
    template_name = "accountant/own_companies.html"
    active_nav = "own_companies"
    section_permission_key = "own_companies"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["companies"] = OwnCompany.objects.order_by("-is_default", "-is_active", "name")
        return ctx


class AccountantOwnCompanyCreateView(AccountantRoleMixin, CreateView):
    template_name = "accountant/own_company_form.html"
    form_class = AccountantOwnCompanyForm
    model = OwnCompany
    success_url = reverse_lazy("accountant-own-companies")
    active_nav = "own_companies"
    section_permission_key = "own_companies"

    def dispatch(self, request, *args, **kwargs):
        denied = self.check_section_permission(request)
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        messages.success(self.request, "Компания сохранена.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Новая компания"
        ctx["page_sub"] = "Реквизиты юрлица FullBox и режим НДС для договоров и счетов."
        ctx["submit_label"] = "Создать компанию"
        return ctx


class AccountantOwnCompanyUpdateView(AccountantRoleMixin, UpdateView):
    template_name = "accountant/own_company_form.html"
    form_class = AccountantOwnCompanyForm
    model = OwnCompany
    success_url = reverse_lazy("accountant-own-companies")
    active_nav = "own_companies"
    section_permission_key = "own_companies"

    def dispatch(self, request, *args, **kwargs):
        denied = self.check_section_permission(request)
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        messages.success(self.request, "Компания обновлена.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Редактирование компании"
        ctx["page_sub"] = "Реквизиты и формат НДС (ИП без НДС / ООО с НДС)."
        ctx["submit_label"] = "Сохранить"
        return ctx


def _own_company_usage_reasons(company: OwnCompany) -> list[str]:
    reasons: list[str] = []
    for relation in company._meta.related_objects:
        accessor_name = relation.get_accessor_name()
        if not accessor_name:
            continue
        related = getattr(company, accessor_name, None)
        if related is None or not hasattr(related, "count"):
            continue
        count = related.count()
        if not count:
            continue
        label = relation.related_model._meta.verbose_name_plural or relation.related_model._meta.verbose_name
        reasons.append(f"{label}: {count}")
    return reasons


class AccountantOwnCompanyDeleteView(AccountantRoleMixin, View):
    active_nav = "own_companies"
    section_permission_key = "own_companies"
    success_url = reverse_lazy("accountant-own-companies")

    def dispatch(self, request, *args, **kwargs):
        denied = self.check_section_permission(request)
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        company = get_object_or_404(OwnCompany, pk=kwargs["pk"])
        company_name = company.short_name or company.name
        reasons = _own_company_usage_reasons(company)
        if reasons:
            messages.error(
                request,
                "Нельзя удалить компанию «{}»: она уже связана с данными. Причины: {}.".format(
                    company_name,
                    "; ".join(reasons),
                ),
            )
            return HttpResponseRedirect(self.success_url)
        try:
            company.delete()
        except ProtectedError as exc:
            messages.error(request, f"Нельзя удалить компанию «{company_name}»: есть защищённые связанные записи ({len(exc.protected_objects)}).")
        else:
            messages.success(request, f"Компания «{company_name}» удалена.")
        return HttpResponseRedirect(self.success_url)


class AccountantCarriersView(AccountantBaseView):
    template_name = "accountant/carriers.html"
    active_nav = "carriers"
    section_permission_key = "carriers"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["carriers"] = Carrier.objects.order_by("-is_active", "name", "id")
        return ctx


class AccountantCarrierCreateView(AccountantRoleMixin, CreateView):
    template_name = "accountant/carrier_form.html"
    form_class = AccountantCarrierForm
    model = Carrier
    success_url = reverse_lazy("accountant-carriers")
    active_nav = "carriers"
    section_permission_key = "carriers"

    def dispatch(self, request, *args, **kwargs):
        denied = self.check_section_permission(request)
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        messages.success(self.request, "Перевозчик сохранён. Он появится в выборе рейса логистики.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Новый перевозчик"
        ctx["page_sub"] = "Транспортная компания или ИП для рейсов логистики."
        ctx["submit_label"] = "Создать перевозчика"
        return ctx


class AccountantCarrierUpdateView(AccountantRoleMixin, UpdateView):
    template_name = "accountant/carrier_form.html"
    form_class = AccountantCarrierForm
    model = Carrier
    success_url = reverse_lazy("accountant-carriers")
    active_nav = "carriers"
    section_permission_key = "carriers"

    def dispatch(self, request, *args, **kwargs):
        denied = self.check_section_permission(request)
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        messages.success(self.request, "Перевозчик обновлён.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Редактирование перевозчика"
        ctx["page_sub"] = "Реквизиты и статус. Неактивные не показываются в рейсах."
        ctx["submit_label"] = "Сохранить"
        return ctx
