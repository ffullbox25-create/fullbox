"""Accountant-facing entry points for the three client tariff contours."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404

from billing.models import ClientTariffItem, ClientTariffVersion, FbsClientRate
from billing.permissions import get_billing_role
from billing.tariff_services import active_tariff_version, copy_tariff_version
from fbs.models import FbsClientStoragePolicy
from sku.models import Agency

from .services import create_tariff_from_catalog
from .views import AccountantBaseView, AccountantTariffsView


EDIT_ROLES = {"accountant", "admin", "director"}


def _ensure_fbs_storage_policy(rate: FbsClientRate) -> None:
    """Enable the FBS storage contour when an active storage rate is saved."""
    if rate.operation != FbsClientRate.OP_STORAGE or not rate.is_active:
        return
    unit = str(rate.unit or "").strip().lower()
    desired_mode = (
        FbsClientStoragePolicy.BILLING_PALLETS
        if "пал" in unit
        else FbsClientStoragePolicy.BILLING_LITERS
    )
    policy, created = FbsClientStoragePolicy.objects.get_or_create(
        agency=rate.client,
        defaults={"billing_mode": desired_mode, "is_active": True},
    )
    if created or policy.is_active:
        return
    policy.billing_mode = desired_mode
    policy.is_active = True
    policy.save(update_fields=["billing_mode", "is_active", "updated_at"])


def _accountant_can_edit(request) -> bool:
    return bool(request.user.is_superuser or get_billing_role(request) in EDIT_ROLES)


def _client_queryset():
    return Agency.objects.filter(lifecycle__isnull=False).order_by("agn_name", "short_name", "id")


def _selected_client(request):
    client_id = str(request.GET.get("client") or request.POST.get("client_id") or "").strip()
    if not client_id:
        return None
    return get_object_or_404(_client_queryset(), pk=client_id)


def _decimal_value(raw, *, label: str, allow_blank: bool = False):
    value = str(raw or "").strip().replace(",", ".")
    if not value and allow_blank:
        return None
    if not value:
        raise ValidationError(f"Заполните поле «{label}».")
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValidationError(f"Поле «{label}» должно содержать число.")


class AccountantGeneralTariffsView(AccountantTariffsView):
    """Open or create the editable general FBO/warehouse tariff for a client."""

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["selected_client"] = _selected_client(self.request)
        ctx["can_edit_client_tariffs"] = _accountant_can_edit(self.request)
        return ctx

    def post(self, request, *args, **kwargs):
        if not _accountant_can_edit(request):
            return HttpResponseForbidden("Изменять тарифы может бухгалтер.")
        client = _selected_client(request)
        if client is None:
            messages.error(request, "Выберите клиента.")
            return HttpResponseRedirect(request.path)
        draft = (
            ClientTariffVersion.objects.filter(client=client, status=ClientTariffVersion.STATUS_DRAFT)
            .order_by("-updated_at", "-id")
            .first()
        )
        try:
            if draft is not None:
                version = draft
                message = "Открыт существующий черновик общего тарифа."
            else:
                current = active_tariff_version(client)
                if current is None:
                    version = create_tariff_from_catalog(
                        client=client,
                        valid_from=request.POST.get("valid_from") or None,
                        user=request.user,
                    )
                    message = "Создан черновик общего тарифа FBO и складских услуг."
                elif current.can_edit_directly():
                    version = current
                    message = "Открыт действующий общий тариф: его ещё можно редактировать."
                else:
                    version = copy_tariff_version(
                        current,
                        valid_from=request.POST.get("valid_from") or date.today(),
                        user=request.user,
                    )
                    message = "Создана новая редакция общего тарифа из действующей."
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return HttpResponseRedirect(f"{request.path}?client={client.pk}")
        messages.success(request, message)
        return HttpResponseRedirect(f"/accountant/tariffs/{version.pk}/")


class AccountantFbsTariffsView(AccountantBaseView):
    template_name = "accountant/fbs_tariffs.html"
    active_nav = "tariffs"
    section_permission_key = "tariffs_clients"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        selected_client = _selected_client(self.request)
        rates = (
            list(
                FbsClientRate.objects.filter(client=selected_client)
                .order_by("operation", "valid_from", "liters_from", "id")
            )
            if selected_client
            else []
        )
        storage_policy = (
            FbsClientStoragePolicy.objects.filter(agency=selected_client, is_active=True).first()
            if selected_client
            else None
        )
        current_tariff = active_tariff_version(selected_client) if selected_client else None
        extra_fbs_items = (
            list(
                ClientTariffItem.objects.filter(
                    tariff_version=current_tariff,
                    is_active=True,
                    service__code="fbs_chz_scan_before_shipping",
                ).select_related("service", "unit")
            )
            if current_tariff
            else []
        )
        ctx.update(
            {
                "clients": _client_queryset()[:1000],
                "selected_client": selected_client,
                "rates": rates,
                "active_rates_count": sum(1 for rate in rates if rate.is_active),
                "operation_choices": FbsClientRate.OPERATION_CHOICES,
                "vat_type_choices": ClientTariffVersion.VAT_TYPE_CHOICES,
                "vat_rate_choices": ("0", "5", "7", "10", "20"),
                "today": date.today(),
                "storage_policy": storage_policy,
                "extra_fbs_items": extra_fbs_items,
                "can_edit_fbs_rates": _accountant_can_edit(self.request),
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        if not _accountant_can_edit(request):
            return HttpResponseForbidden("Изменять ставки FBS может бухгалтер.")
        client = _selected_client(request)
        if client is None:
            messages.error(request, "Выберите клиента.")
            return HttpResponseRedirect(request.path)
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "add":
                operation = str(request.POST.get("operation") or "").strip()
                if operation not in dict(FbsClientRate.OPERATION_CHOICES):
                    raise ValidationError("Выберите операцию FBS.")
                no_liter_band = operation in {FbsClientRate.OP_MARKING, FbsClientRate.OP_STORAGE}
                rate = FbsClientRate(
                    client=client,
                    operation=operation,
                    liters_from=(
                        Decimal("0")
                        if no_liter_band
                        else _decimal_value(request.POST.get("liters_from"), label="Больше литров")
                    ),
                    liters_to=(
                        None
                        if no_liter_band
                        else _decimal_value(
                            request.POST.get("liters_to"),
                            label="До литров",
                            allow_blank=True,
                        )
                    ),
                    price=_decimal_value(request.POST.get("price"), label="Цена"),
                    unit=(request.POST.get("unit") or "шт").strip(),
                    valid_from=request.POST.get("valid_from") or date.today(),
                    valid_to=request.POST.get("valid_to") or None,
                    vat_rate=(request.POST.get("vat_rate") or "5").strip(),
                    vat_type=(request.POST.get("vat_type") or ClientTariffVersion.VAT_EXTRA).strip(),
                    comment=(request.POST.get("comment") or "").strip(),
                    is_active=True,
                )
                rate.full_clean()
                rate.save()
                _ensure_fbs_storage_policy(rate)
                messages.success(request, "Ставка FBS добавлена.")
            elif action == "update":
                rate = get_object_or_404(FbsClientRate, pk=request.POST.get("rate_id"), client=client)
                rate.price = _decimal_value(request.POST.get("price"), label="Цена")
                rate.unit = (request.POST.get("unit") or rate.unit).strip()
                rate.valid_from = request.POST.get("valid_from") or rate.valid_from
                rate.valid_to = request.POST.get("valid_to") or None
                rate.vat_rate = (request.POST.get("vat_rate") or rate.vat_rate).strip()
                rate.vat_type = (request.POST.get("vat_type") or rate.vat_type).strip()
                rate.comment = (request.POST.get("comment") or "").strip()
                rate.is_active = bool(request.POST.get("is_active"))
                rate.full_clean()
                rate.save()
                _ensure_fbs_storage_policy(rate)
                messages.success(request, "Ставка FBS сохранена.")
            elif action == "deactivate":
                rate = get_object_or_404(FbsClientRate, pk=request.POST.get("rate_id"), client=client)
                rate.is_active = False
                rate.save(update_fields=["is_active", "updated_at"])
                messages.success(request, "Ставка FBS отключена и сохранена в истории.")
            else:
                raise ValidationError("Неизвестное действие с тарифом FBS.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return HttpResponseRedirect(f"{request.path}?client={client.pk}")
