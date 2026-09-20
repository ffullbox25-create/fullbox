from __future__ import annotations

import json
from datetime import date
from decimal import InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from sku.models import Agency

from head_manager.models import OwnCompany

from .models import (
    ApplicationCharge,
    BillingAct,
    BillingApplication,
    BillingService,
    ClientBillingContract,
    ClientInvoice,
    ClientTariff,
    ClientTariffCondition,
    ClientTariffItem,
    ClientTariffVersion,
    StandardServicePrice,
    TariffCategory,
    TariffUnit,
)
from .permissions import (
    can_add_client_tariff,
    can_approve_client_tariff,
    can_archive_client_tariff,
    can_change_charge_vat,
    can_create_invoice,
    can_create_invoice_draft,
    can_delete_charge,
    can_financially_close,
    can_generate_act,
    can_mark_billing_external,
    can_manage_charges,
    can_override_billing_tariff,
    can_register_payment,
    can_review_billing_document,
    can_submit_document_review,
    can_view_billing,
    can_view_client_tariff,
    get_billing_role,
)
from .selectors import billing_applications_queryset, invoices_queryset
from .serializers import (
    application_charge_dict,
    application_charge_history_dict,
    billing_act_dict,
    billing_application_dict,
    billing_service_dict,
    client_contract_dict,
    client_invoice_dict,
    client_tariff_dict,
    client_tariff_condition_dict,
    client_tariff_item_dict,
    client_tariff_version_dict,
    standard_price_dict,
    tariff_category_dict,
    tariff_unit_dict,
)
from .services import BillingWorkflowService, ChargeVersionConflict
from .tariff_services import (
    activate_tariff_version,
    active_tariff_version,
    archive_tariff_version,
    copy_tariff_version,
    create_tariff_version,
    render_tariff_print_response,
)


def _json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValidationError("Некорректный JSON.")


def _ensure_storage_charges_automatic(application):
    if application.application_type == BillingApplication.TYPE_STORAGE:
        raise ValidationError(
            "Начисления за хранение формируются автоматически по данным склада и не редактируются вручную."
        )


def _ok(data=None, **extra):
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return JsonResponse(payload)


def _error(message, *, status=400):
    if isinstance(message, ChargeVersionConflict):
        status = 409
    elif getattr(message, "status_code", None) == 409:
        status = 409
    text = str(message)
    if hasattr(message, "messages"):
        try:
            text = "; ".join(str(m) for m in message.messages)
        except Exception:
            pass
    return JsonResponse({"ok": False, "error": text}, status=status)


def _paginate(qs, request, serialize):
    page_number = request.GET.get("page") or 1
    per_page = min(max(int(request.GET.get("per_page") or 50), 1), 200)
    page = Paginator(qs, per_page).get_page(page_number)
    return _ok(
        [serialize(item) for item in page.object_list],
        pagination={
            "page": page.number,
            "per_page": per_page,
            "pages": page.paginator.num_pages,
            "total": page.paginator.count,
            "has_next": page.has_next(),
            "has_previous": page.has_previous(),
        },
    )


def _guard(condition):
    if not condition:
        raise PermissionDenied("Недостаточно прав для действия.")


def _parse_required_date(value, field_name: str):
    parsed = parse_date(str(value or ""))
    if parsed is None:
        raise ValidationError(f"Некорректная дата {field_name}.")
    return parsed


@login_required
@require_http_methods(["GET"])
def manager_applications(request):
    try:
        qs = billing_applications_queryset(request.GET, user_or_request=request)
        return _paginate(qs, request, billing_application_dict)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_sync_applications(request):
    """Подтянуть приёмку/обработку/отгрузку/другие заявки в регистр биллинга."""
    try:
        _guard(can_view_billing(request))
        from .sync import sync_billing_applications

        data = _json_body(request)
        limit = int(data.get("limit") or 100000)
        limit = max(1000, min(limit, 200000))
        result = sync_billing_applications(limit=limit, user=request.user)
        return _ok(result)
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError, TypeError) as exc:
        return _error(exc)


@login_required
@require_http_methods(["GET"])
def manager_services(request):
    try:
        _guard(can_manage_charges(request))
        from .service_catalog import ensure_service_catalog

        ensure_service_catalog()
        qs = BillingService.objects.filter(is_active=True).order_by("code")
        return _ok([billing_service_dict(service) for service in qs])
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_http_methods(["GET"])
def manager_standard_prices(request):
    try:
        _guard(can_manage_charges(request))
        from .standard_price_catalog import seed_standard_prices

        seed_standard_prices()
        qs = StandardServicePrice.objects.select_related("service").filter(is_active=True).order_by("section_code", "line_no", "service__code")
        if request.GET.get("section"):
            qs = qs.filter(section_code=request.GET.get("section"))
        return _ok([standard_price_dict(price) for price in qs])
    except PermissionDenied as exc:
        return _error(exc, status=403)


@csrf_exempt
@login_required
@require_http_methods(["GET", "POST"])
def manager_client_contracts(request):
    try:
        _guard(can_manage_charges(request))
        if request.method == "GET":
            qs = ClientBillingContract.objects.select_related("client", "own_company").order_by("client__agn_name", "-valid_from", "-id")
            if request.GET.get("client"):
                qs = qs.filter(client_id=request.GET.get("client"))
            if request.GET.get("active") in {"1", "true", "True"}:
                qs = qs.filter(is_active=True)
            return _paginate(qs, request, client_contract_dict)

        data = _json_body(request)
        client = get_object_or_404(Agency, pk=data.get("client_id"))
        own_company = get_object_or_404(OwnCompany, pk=data.get("own_company_id"))
        contract = ClientBillingContract.objects.create(
            client=client,
            own_company=own_company,
            pricing_mode=data.get("pricing_mode") or ClientBillingContract.PRICING_BASE,
            valid_from=_parse_required_date(data.get("valid_from"), "valid_from"),
            valid_to=parse_date(str(data.get("valid_to") or "")) if data.get("valid_to") else None,
            is_active=bool(data.get("is_active", True)),
            comment=data.get("comment", ""),
        )
        return _ok(client_contract_dict(contract))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["PATCH"])
def manager_client_contract_detail(request, pk: int):
    contract = get_object_or_404(ClientBillingContract.objects.select_related("client", "own_company"), pk=pk)
    try:
        _guard(can_manage_charges(request))
        data = _json_body(request)
        if "own_company_id" in data:
            contract.own_company = get_object_or_404(OwnCompany, pk=data.get("own_company_id"))
        if "pricing_mode" in data:
            contract.pricing_mode = data.get("pricing_mode") or contract.pricing_mode
        if "valid_from" in data:
            contract.valid_from = _parse_required_date(data.get("valid_from"), "valid_from")
        if "valid_to" in data:
            contract.valid_to = parse_date(str(data.get("valid_to") or "")) if data.get("valid_to") else None
        if "is_active" in data:
            contract.is_active = bool(data.get("is_active"))
        if "comment" in data:
            contract.comment = data.get("comment") or ""
        contract.save()
        return _ok(client_contract_dict(contract))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["GET", "POST"])
def manager_tariffs(request):
    try:
        _guard(can_manage_charges(request))
        if request.method == "GET":
            qs = ClientTariff.objects.select_related("client", "legal_entity", "service").order_by("client__agn_name", "service__code", "-valid_from")
            if request.GET.get("client"):
                qs = qs.filter(client_id=request.GET.get("client"))
            if request.GET.get("service"):
                qs = qs.filter(service_id=request.GET.get("service"))
            if request.GET.get("active") in {"1", "true", "True"}:
                qs = qs.filter(is_active=True)
            return _paginate(qs, request, client_tariff_dict)

        data = _json_body(request)
        client = get_object_or_404(Agency, pk=data.get("client_id"))
        legal_entity = get_object_or_404(Agency, pk=data.get("legal_entity_id") or client.pk)
        service = get_object_or_404(BillingService, pk=data.get("service_id"))
        tariff = ClientTariff.objects.create(
            client=client,
            legal_entity=legal_entity,
            service=service,
            unit=data.get("unit") or service.unit,
            tariff=data.get("tariff"),
            vat_rate=data.get("vat_rate") or service.vat_rate,
            valid_from=_parse_required_date(data.get("valid_from"), "valid_from"),
            valid_to=parse_date(str(data.get("valid_to") or "")) if data.get("valid_to") else None,
            is_active=bool(data.get("is_active", True)),
        )
        return _ok(client_tariff_dict(tariff))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError, InvalidOperation) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["PATCH", "DELETE"])
def manager_tariff_detail(request, pk: int):
    tariff = get_object_or_404(ClientTariff.objects.select_related("client", "legal_entity", "service"), pk=pk)
    try:
        _guard(can_manage_charges(request))
        if request.method == "DELETE":
            tariff.is_active = False
            tariff.save(update_fields=["is_active", "updated_at"])
            return _ok(client_tariff_dict(tariff))
        data = _json_body(request)
        if "unit" in data:
            tariff.unit = data.get("unit") or tariff.service.unit
        if "tariff" in data:
            tariff.tariff = data.get("tariff")
        if "vat_rate" in data:
            tariff.vat_rate = data.get("vat_rate") or tariff.service.vat_rate
        if "valid_from" in data:
            tariff.valid_from = _parse_required_date(data.get("valid_from"), "valid_from")
        if "valid_to" in data:
            tariff.valid_to = parse_date(str(data.get("valid_to") or "")) if data.get("valid_to") else None
        if "is_active" in data:
            tariff.is_active = bool(data.get("is_active"))
        tariff.save()
        return _ok(client_tariff_dict(tariff))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError, InvalidOperation) as exc:
        return _error(exc)


@login_required
@require_http_methods(["GET"])
def manager_tariff_catalog(request):
    try:
        _guard(can_add_client_tariff(request))
        return _ok(
            {
                "categories": [tariff_category_dict(item) for item in TariffCategory.objects.filter(is_active=True).order_by("sort_order", "name")],
                "units": [tariff_unit_dict(item) for item in TariffUnit.objects.filter(is_active=True).order_by("name")],
                "services": [billing_service_dict(item) for item in BillingService.objects.filter(is_active=True).order_by("name")],
            }
        )
    except PermissionDenied as exc:
        return _error(exc, status=403)


@csrf_exempt
@login_required
@require_http_methods(["GET", "POST"])
def manager_tariff_versions(request):
    try:
        client = get_object_or_404(Agency, pk=request.GET.get("client") or _json_body(request).get("client_id")) if (request.GET.get("client") or request.method == "POST") else None
        _guard(can_view_client_tariff(request, client))
        if request.method == "GET":
            qs = ClientTariffVersion.objects.select_related("client", "contract", "manager").order_by("client__agn_name", "-valid_from", "-version_number")
            if client:
                qs = qs.filter(client=client)
            if request.GET.get("status"):
                qs = qs.filter(status=request.GET.get("status"))
            return _paginate(qs, request, client_tariff_version_dict)

        _guard(can_add_client_tariff(request, client))
        data = _json_body(request)
        contract = ClientBillingContract.objects.filter(pk=data.get("contract_id"), client=client).first() if data.get("contract_id") else None
        version = create_tariff_version(
            client=client,
            contract=contract,
            name=data.get("name") or "",
            valid_from=_parse_required_date(data.get("valid_from"), "valid_from"),
            valid_to=parse_date(str(data.get("valid_to") or "")) if data.get("valid_to") else None,
            status=data.get("status") or ClientTariffVersion.STATUS_DRAFT,
            user=request.user,
        )
        return _ok(client_tariff_version_dict(version, include_children=True))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["GET", "PATCH"])
def manager_tariff_version_detail(request, pk: int):
    version = get_object_or_404(ClientTariffVersion.objects.select_related("client", "contract", "manager"), pk=pk)
    try:
        _guard(can_view_client_tariff(request, version.client))
        if request.method == "GET":
            return _ok(client_tariff_version_dict(version, include_children=True))
        _guard(can_add_client_tariff(request, version.client))
        if version.status != ClientTariffVersion.STATUS_DRAFT:
            raise ValidationError("Редактировать можно только черновик.")
        data = _json_body(request)
        for field in ("name", "vat_type", "currency", "contract_number", "additional_agreement_number", "general_comment"):
            if field in data:
                setattr(version, field, data.get(field) or "")
        if "valid_from" in data:
            version.valid_from = _parse_required_date(data.get("valid_from"), "valid_from")
        if "valid_to" in data:
            version.valid_to = parse_date(str(data.get("valid_to") or "")) if data.get("valid_to") else None
        if "contract_date" in data:
            version.contract_date = parse_date(str(data.get("contract_date") or "")) if data.get("contract_date") else None
        if "additional_agreement_date" in data:
            version.additional_agreement_date = parse_date(str(data.get("additional_agreement_date") or "")) if data.get("additional_agreement_date") else None
        version.save()
        return _ok(client_tariff_version_dict(version, include_children=True))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_tariff_version_copy(request, pk: int):
    source = get_object_or_404(ClientTariffVersion.objects.select_related("client", "contract"), pk=pk)
    try:
        _guard(can_add_client_tariff(request, source.client))
        data = _json_body(request)
        target = copy_tariff_version(
            source,
            valid_from=parse_date(str(data.get("valid_from") or "")) if data.get("valid_from") else None,
            user=request.user,
        )
        return _ok(client_tariff_version_dict(target, include_children=True))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_tariff_version_activate(request, pk: int):
    version = get_object_or_404(ClientTariffVersion.objects.select_related("client"), pk=pk)
    try:
        _guard(can_approve_client_tariff(request, version.client))
        version = activate_tariff_version(version, user=request.user)
        return _ok(client_tariff_version_dict(version, include_children=True))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_tariff_version_archive(request, pk: int):
    version = get_object_or_404(ClientTariffVersion.objects.select_related("client"), pk=pk)
    try:
        _guard(can_archive_client_tariff(request, version.client))
        version = archive_tariff_version(version, user=request.user)
        return _ok(client_tariff_version_dict(version, include_children=True))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_tariff_version_items(request, pk: int):
    version = get_object_or_404(ClientTariffVersion.objects.select_related("client"), pk=pk)
    try:
        _guard(can_add_client_tariff(request, version.client))
        if version.status != ClientTariffVersion.STATUS_DRAFT:
            raise ValidationError("Позиции можно менять только в черновике.")
        data = _json_body(request)
        service = get_object_or_404(BillingService, pk=data.get("service_id"))
        category = get_object_or_404(TariffCategory, pk=data.get("category_id"))
        unit = get_object_or_404(TariffUnit, pk=data.get("unit_id"))
        item = ClientTariffItem.objects.create(
            tariff_version=version,
            category=category,
            service=service,
            service_name=data.get("service_name") or service.name,
            description=data.get("description") or "",
            unit=unit,
            price=data.get("price") or "0",
            minimum_amount=data.get("minimum_amount") or None,
            minimum_quantity=data.get("minimum_quantity") or None,
            included_materials=data.get("included_materials") or "",
            conditions=data.get("conditions") or "",
            calculation_type=data.get("calculation_type") or ClientTariffItem.CALCULATION_BY_UNIT,
            coefficient=data.get("coefficient") or "1",
            sort_order=int(data.get("sort_order") or 100),
        )
        return _ok(client_tariff_item_dict(item))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError, InvalidOperation) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_tariff_version_conditions(request, pk: int):
    version = get_object_or_404(ClientTariffVersion.objects.select_related("client"), pk=pk)
    try:
        _guard(can_add_client_tariff(request, version.client))
        if version.status != ClientTariffVersion.STATUS_DRAFT:
            raise ValidationError("Условия можно менять только в черновике.")
        data = _json_body(request)
        unit = TariffUnit.objects.filter(pk=data.get("unit_id")).first() if data.get("unit_id") else None
        condition = ClientTariffCondition.objects.create(
            tariff_version=version,
            condition_type=data.get("condition_type") or ClientTariffCondition.TYPE_OTHER,
            name=data.get("name") or "Условие",
            description=data.get("description") or "",
            value=data.get("value") or "",
            unit=unit,
            valid_from=parse_date(str(data.get("valid_from") or "")) if data.get("valid_from") else None,
            valid_to=parse_date(str(data.get("valid_to") or "")) if data.get("valid_to") else None,
            sort_order=int(data.get("sort_order") or 100),
        )
        return _ok(client_tariff_condition_dict(condition))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@login_required
@require_http_methods(["GET"])
def tariff_version_download(request, pk: int):
    version = get_object_or_404(ClientTariffVersion.objects.select_related("client"), pk=pk)
    # Client profile access is checked by client_cabinet view before linking; manager API checks billing permission.
    if not can_view_client_tariff(request, version.client):
        from client_cabinet.web_ui import _check_agency_access

        if not _check_agency_access(request, version.client):
            return _error("Недостаточно прав для действия.", status=403)
    return render_tariff_print_response(version, as_attachment=request.GET.get("download") == "1")


@login_required
@require_http_methods(["GET"])
def invoice_print(request, pk: int):
    """Печатная форма счёта на оплату (как в 1С / образце FullBox)."""
    from .invoice_print import client_can_view_invoice, get_printable_invoice, render_invoice_print_response

    invoice = get_printable_invoice(pk)
    if can_view_billing(request, invoice.application):
        return render_invoice_print_response(invoice, as_attachment=request.GET.get("download") == "1")
    try:
        from client_cabinet.web_ui import _check_agency_access

        if _check_agency_access(request, invoice.client) and client_can_view_invoice(invoice):
            return render_invoice_print_response(invoice, as_attachment=request.GET.get("download") == "1")
    except Exception:
        pass
    return _error("Недостаточно прав для просмотра счёта.", status=403)


@login_required
@require_http_methods(["GET"])
def manager_application_detail(request, pk: int):
    application = get_object_or_404(BillingApplication.objects.select_related("client", "legal_entity", "manager"), pk=pk)
    try:
        _guard(can_view_billing(request, application))
        return _ok(billing_application_dict(application, include_children=True))
    except PermissionDenied as exc:
        return _error(exc, status=403)


@csrf_exempt
@login_required
@require_http_methods(["GET", "POST"])
def manager_application_charges(request, pk: int):
    application = get_object_or_404(BillingApplication, pk=pk)
    try:
        _guard(can_manage_charges(request, application))
        if request.method == "GET":
            from .application_detail_ui import charge_display_sort_key, tariff_order_map

            on_date = None
            if application.created_at_source:
                on_date = timezone.localdate(application.created_at_source)
            elif application.created_at:
                on_date = timezone.localdate(application.created_at)
            active_tariff = active_tariff_version(application.client, on_date=on_date)
            charge_order = tariff_order_map(active_tariff)
            charges = list(
                application.charges.select_related(
                    "service",
                    "client_tariff_version",
                    "client_tariff_item",
                    "client_tariff_item__category",
                ).all()
            )
            charges.sort(key=lambda charge: charge_display_sort_key(charge, charge_order))
            return _ok([application_charge_dict(charge) for charge in charges])
        _ensure_storage_charges_automatic(application)
        data = _json_body(request)
        service = get_object_or_404(BillingService, pk=data.get("service_id"))
        wants_override = "tariff" in data and data.get("tariff") is not None
        if wants_override:
            _guard(can_override_billing_tariff(request))
            charge = BillingWorkflowService.create_or_update_charge(
                application,
                service=service,
                quantity=data.get("quantity", "1"),
                tariff=data.get("tariff"),
                unit=data.get("unit"),
                vat_rate=data.get("vat_rate"),
                comment=data.get("comment", ""),
                user=request.user,
                is_manual_override=True,
                override_reason=data.get("override_reason") or "Ручное изменение цены",
                overridden_by=request.user,
                resolve_from_agreed_tariff=False,
            )
        else:
            charge = BillingWorkflowService.add_manual_charge(
                application,
                service=service,
                quantity=data.get("quantity", "1"),
                user=request.user,
                comment=data.get("comment", ""),
                basis=data.get("basis", ""),
                allow_without_tariff=True,
            )
        return _ok(application_charge_dict(charge))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError, InvalidOperation) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["PATCH", "DELETE"])
def manager_charge_detail(request, pk: int):
    charge = get_object_or_404(ApplicationCharge.objects.select_related("application", "service"), pk=pk)
    try:
        _guard(can_manage_charges(request, charge.application))
        _ensure_storage_charges_automatic(charge.application)
        if request.method == "DELETE":
            _guard(can_delete_charge(request, charge.application))
            if charge.is_included_in_act or charge.is_included_in_invoice:
                raise ValidationError("Нельзя удалить начисление, уже включенное в акт или счет.")
            application = charge.application
            charge.delete()
            BillingWorkflowService.recalculate_application_totals(application)
            BillingWorkflowService.audit(action="charge_deleted", application=application, user=request.user)
            return _ok({"deleted": True})
        data = _json_body(request)
        role = get_billing_role(request)
        is_manager_only = role == "manager" and not can_override_billing_tariff(request)

        # Менеджер не может менять цену / НДС / юрлицо / версию тарифа
        if is_manager_only:
            forbidden = [k for k in ("tariff", "vat_rate", "client_tariff_version_id", "legal_entity_id") if k in data]
            if forbidden:
                raise PermissionDenied(
                    "Менеджер не может менять договорную цену, НДС или тариф. "
                    "Доступно изменение количества, услуги и комментария."
                )
            qty = data.get("quantity", charge.quantity)
            comment = data.get("comment", charge.comment)
            service = charge.service
            if data.get("service_id") is not None:
                service = get_object_or_404(BillingService, pk=data.get("service_id"), is_active=True)
            expected = data.get("edit_version")
            if data.get("service_id") is not None and int(service.pk) != int(charge.service_id):
                updated = BillingWorkflowService.change_charge_service(
                    charge,
                    service=service,
                    quantity=qty,
                    comment=comment,
                    user=request.user,
                    expected_version=expected,
                    reason=data.get("reason", ""),
                )
            elif str(qty) != str(charge.quantity):
                updated = BillingWorkflowService.update_charge_quantity(
                    charge,
                    quantity=qty,
                    user=request.user,
                    basis=data.get("qty_change_basis") or data.get("basis") or "",
                    comment=data.get("qty_change_comment") or comment or "",
                    expected_version=expected,
                )
            else:
                updated = BillingWorkflowService.change_charge_service(
                    charge,
                    service=service,
                    quantity=qty,
                    comment=comment,
                    user=request.user,
                    expected_version=expected,
                )
            return _ok(application_charge_dict(updated))

        service = charge.service
        expected = data.get("edit_version")
        if data.get("service_id") is not None:
            service = get_object_or_404(BillingService, pk=data.get("service_id"), is_active=True)
            if int(service.pk) != int(charge.service_id):
                updated = BillingWorkflowService.change_charge_service(
                    charge,
                    service=service,
                    quantity=data.get("quantity", charge.quantity),
                    comment=data.get("comment", charge.comment),
                    user=request.user,
                    expected_version=expected,
                    reason=data.get("reason", ""),
                )
                return _ok(application_charge_dict(updated))
        # qty/comment without service change — тоже снимаем черновик акта при необходимости
        BillingWorkflowService.ensure_charge_editable(charge, user=request.user)
        charge.refresh_from_db()
        BillingWorkflowService.assert_charge_version(charge, expected)
        service = charge.service

        price_changed = "tariff" in data and str(data.get("tariff")) != str(charge.tariff)
        if price_changed:
            _guard(can_override_billing_tariff(request))
            if not (data.get("override_reason") or "").strip():
                raise ValidationError("Укажите причину ручного изменения цены.")
            updated = BillingWorkflowService.create_or_update_charge(
                charge.application,
                service=service,
                quantity=data.get("quantity", charge.quantity),
                tariff=data.get("tariff"),
                unit=data.get("unit", charge.unit),
                vat_rate=data.get("vat_rate", charge.vat_rate),
                source_key=charge.source_key or f"charge:{charge.pk}",
                comment=data.get("comment", charge.comment),
                user=request.user,
                client_tariff_version=charge.client_tariff_version,
                client_tariff_item=charge.client_tariff_item,
                tariff_price=charge.tariff_price if charge.tariff_price is not None else charge.tariff,
                tariff_source_label=charge.tariff_source_label,
                tariff_basis=charge.tariff_basis,
                service_name_snapshot=charge.service_name_snapshot or charge.service.name,
                is_manual_override=True,
                override_reason=data.get("override_reason"),
                overridden_by=request.user,
                resolve_from_agreed_tariff=False,
            )
            BillingWorkflowService.audit(
                action="charge_price_overridden",
                application=charge.application,
                user=request.user,
                obj=updated,
                old_value={"tariff": str(charge.tariff)},
                new_value={"tariff": str(updated.tariff), "reason": data.get("override_reason")},
            )
        elif "quantity" in data and str(data.get("quantity")) != str(charge.quantity):
            updated = BillingWorkflowService.update_charge_quantity(
                charge,
                quantity=data.get("quantity"),
                user=request.user,
                basis=data.get("qty_change_basis") or data.get("basis") or "",
                comment=data.get("qty_change_comment") or data.get("comment") or "",
                expected_version=expected,
            )
        else:
            vat_rate = charge.vat_rate
            if "vat_rate" in data:
                _guard(can_change_charge_vat(request))
                vat_rate = data.get("vat_rate")
            updated = BillingWorkflowService.create_or_update_charge(
                charge.application,
                service=service,
                quantity=data.get("quantity", charge.quantity),
                unit=data.get("unit", charge.unit),
                vat_rate=vat_rate,
                source_key=charge.source_key or f"charge:{charge.pk}",
                comment=data.get("comment", charge.comment),
                user=request.user,
                resolve_from_agreed_tariff=True,
            )
        if not charge.source_key and charge.pk != updated.pk:
            try:
                charge.delete()
            except Exception:
                pass
        return _ok(application_charge_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_charge_confirm(request, pk: int):
    charge = get_object_or_404(ApplicationCharge.objects.select_related("application", "service"), pk=pk)
    try:
        _guard(can_manage_charges(request, charge.application))
        data = _json_body(request)
        updated = BillingWorkflowService.confirm_charge(
            charge,
            user=request.user,
            comment=data.get("comment", ""),
            expected_version=data.get("edit_version"),
        )
        return _ok(application_charge_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_charge_unconfirm(request, pk: int):
    charge = get_object_or_404(ApplicationCharge.objects.select_related("application", "service"), pk=pk)
    try:
        _guard(can_manage_charges(request, charge.application))
        data = _json_body(request)
        updated = BillingWorkflowService.unconfirm_charge(
            charge, user=request.user, expected_version=data.get("edit_version")
        )
        return _ok(application_charge_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_charge_exclude(request, pk: int):
    charge = get_object_or_404(ApplicationCharge.objects.select_related("application", "service"), pk=pk)
    try:
        _guard(can_manage_charges(request, charge.application))
        _ensure_storage_charges_automatic(charge.application)
        data = _json_body(request)
        updated = BillingWorkflowService.exclude_charge(
            charge,
            reason=str(data.get("reason") or ""),
            comment=str(data.get("comment") or ""),
            user=request.user,
            expected_version=data.get("edit_version"),
        )
        return _ok(application_charge_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_charge_restore(request, pk: int):
    charge = get_object_or_404(ApplicationCharge.objects.select_related("application", "service"), pk=pk)
    try:
        _guard(can_manage_charges(request, charge.application))
        _ensure_storage_charges_automatic(charge.application)
        data = _json_body(request)
        updated = BillingWorkflowService.restore_charge(
            charge, user=request.user, expected_version=data.get("edit_version")
        )
        return _ok(application_charge_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_GET
def manager_charge_history(request, pk: int):
    charge = get_object_or_404(ApplicationCharge.objects.select_related("application"), pk=pk)
    try:
        _guard(can_view_billing(request, charge.application))
        rows = [
            application_charge_history_dict(e)
            for e in charge.history_entries.select_related("user").all()[:100]
        ]
        return _ok(rows)
    except PermissionDenied as exc:
        return _error(exc, status=403)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_charge_recalc(request, pk: int):
    charge = get_object_or_404(ApplicationCharge.objects.select_related("application", "service"), pk=pk)
    try:
        _guard(can_manage_charges(request, charge.application))
        _ensure_storage_charges_automatic(charge.application)
        data = _json_body(request)
        updated = BillingWorkflowService.recalculate_charge(
            charge, user=request.user, expected_version=data.get("edit_version")
        )
        return _ok(application_charge_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_application_charges_bulk(request, pk: int):
    application = get_object_or_404(BillingApplication, pk=pk)
    try:
        _guard(can_manage_charges(request, application))
        data = _json_body(request)
        if application.application_type == BillingApplication.TYPE_STORAGE and str(data.get("action") or "") != "confirm":
            _ensure_storage_charges_automatic(application)
        result = BillingWorkflowService.bulk_charge_action(
            application,
            action=str(data.get("action") or ""),
            charge_ids=[int(x) for x in (data.get("charge_ids") or [])],
            user=request.user,
            service_id=data.get("service_id"),
            reason=str(data.get("reason") or ""),
            comment=str(data.get("comment") or ""),
            expected_versions=data.get("expected_versions") or {},
        )
        return _ok(result)
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def manager_confirm_application_charges(request, pk: int):
    application = get_object_or_404(BillingApplication, pk=pk)
    try:
        _guard(can_manage_charges(request, application))
        count = BillingWorkflowService.confirm_application_charges(application, user=request.user)
        return _ok({"confirmed": count, "application": billing_application_dict(application, include_children=True)})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_POST
def manager_calculate_application(request, pk: int):
    application = get_object_or_404(BillingApplication, pk=pk)
    try:
        _guard(can_manage_charges(request, application))
        if application.application_type == BillingApplication.TYPE_STORAGE:
            raise ValidationError(
                "Хранение рассчитывается автоматически по дневным снимкам склада. "
                "Используйте расчёт хранения или корректировку по фактической дате приёмки."
            )
        charges = BillingWorkflowService.calculate_charges(application, user=request.user)
        return _ok({"created": len(charges), "application": billing_application_dict(application)})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_POST
def manager_generate_act(request, pk: int):
    application = get_object_or_404(BillingApplication, pk=pk)
    try:
        _guard(can_generate_act(request, application))
        act, invoice = BillingWorkflowService.generate_act_and_invoice(application, user=request.user)
        payload = billing_act_dict(act, include_lines=True)
        payload["invoice"] = client_invoice_dict(invoice)
        return _ok(payload)
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_POST
def manager_send_act(request, pk: int):
    act = get_object_or_404(BillingAct, pk=pk)
    try:
        _guard(can_generate_act(request, act.application))
        return _ok(billing_act_dict(BillingWorkflowService.send_act(act, user=request.user)))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_POST
def manager_cancel_act(request, pk: int):
    act = get_object_or_404(BillingAct, pk=pk)
    try:
        _guard(can_generate_act(request, act.application))
        cancelled = BillingWorkflowService.cancel_draft_act(act, user=request.user)
        return _ok(billing_act_dict(cancelled))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_http_methods(["GET"])
def act_print(request, pk: int):
    """Просмотр/печать акта для проверки менеджером (без отправки клиенту)."""
    from django.shortcuts import render

    from .permissions import can_view_billing

    act = get_object_or_404(
        BillingAct.objects.select_related("application", "client", "legal_entity").prefetch_related("lines"),
        pk=pk,
    )
    try:
        _guard(can_view_billing(request, act.application))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    return render(
        request,
        "billing/act_print.html",
        {
            "act": act,
            "lines": list(act.lines.all()),
            "application": act.application,
            "client": act.client,
        },
    )


@login_required
@require_http_methods(["GET"])
def manager_invoices(request):
    try:
        qs = invoices_queryset(request.GET, user_or_request=request)
        return _paginate(qs, request, client_invoice_dict)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@login_required
@require_POST
def manager_create_invoice(request, pk: int):
    application = get_object_or_404(BillingApplication, pk=pk)
    try:
        _guard(can_create_invoice_draft(request, application))
        invoice = BillingWorkflowService.create_invoice(application, user=request.user)
        return _ok(client_invoice_dict(invoice))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_create_invoice_from_acts(request):
    """Создать черновик счёта по одному или нескольким подтверждённым актам."""
    try:
        data = _json_body(request)
        raw_ids = data.get("act_ids") or data.get("acts") or []
        if isinstance(raw_ids, str):
            raw_ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
        act_ids = []
        for item in raw_ids:
            try:
                act_ids.append(int(item))
            except (TypeError, ValueError):
                raise ValidationError("Некорректный список act_ids.")
        if not act_ids:
            raise ValidationError("Укажите act_ids — список актов для счёта.")
        acts = list(
            BillingAct.objects.filter(pk__in=act_ids)
            .select_related("application", "client", "legal_entity")
            .order_by("act_date", "id")
        )
        if len(acts) != len(set(act_ids)):
            raise ValidationError("Один или несколько актов не найдены.")
        # Preserve requested order
        by_id = {a.id: a for a in acts}
        ordered = [by_id[i] for i in act_ids if i in by_id]
        _guard(can_create_invoice_draft(request, ordered[0].application))
        for act in ordered:
            if not can_view_billing(request, act.application):
                raise PermissionDenied("Нет доступа к одной из заявок.")
        due_raw = data.get("due_date")
        due_date = parse_date(str(due_raw)) if due_raw else None
        invoice = BillingWorkflowService.create_grouped_invoice(
            acts=ordered,
            due_date=due_date,
            user=request.user,
        )
        return _ok(client_invoice_dict(invoice))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_create_invoice_from_shipping_applications(request):
    """Создать отдельные акты по отгрузкам и один общий черновик счёта."""
    try:
        data = _json_body(request)
        raw_ids = data.get("application_ids") or data.get("applications") or []
        if isinstance(raw_ids, str):
            raw_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]

        application_ids = []
        seen = set()
        for item in raw_ids:
            try:
                application_id = int(item)
            except (TypeError, ValueError):
                raise ValidationError("Некорректный список application_ids.")
            if application_id not in seen:
                seen.add(application_id)
                application_ids.append(application_id)
        if len(application_ids) < 2:
            raise ValidationError("Выберите минимум две отгрузки для общего счёта.")

        applications = list(
            BillingApplication.objects.filter(pk__in=application_ids)
            .select_related("client", "legal_entity")
        )
        if len(applications) != len(application_ids):
            raise ValidationError("Одна или несколько отгрузок не найдены.")
        by_id = {application.id: application for application in applications}
        ordered = [by_id[application_id] for application_id in application_ids]

        for application in ordered:
            if application.application_type != BillingApplication.TYPE_SHIPPING:
                raise ValidationError("Выбранная заявка не является отгрузкой.")
            _guard(can_generate_act(request, application))
            _guard(can_create_invoice_draft(request, application))

        due_raw = data.get("due_date")
        due_date = parse_date(str(due_raw)) if due_raw else None
        invoice = BillingWorkflowService.generate_grouped_shipping_invoice(
            ordered,
            due_date=due_date,
            user=request.user,
        )
        return _ok(client_invoice_dict(invoice))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_POST
def manager_check_invoice(request, pk: int):
    invoice = get_object_or_404(ClientInvoice, pk=pk)
    try:
        _guard(can_create_invoice(request, invoice.application))
        return _ok(client_invoice_dict(BillingWorkflowService.check_invoice(invoice, user=request.user)))
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_POST
def manager_send_invoice(request, pk: int):
    invoice = get_object_or_404(ClientInvoice, pk=pk)
    try:
        _guard(can_create_invoice(request, invoice.application))
        return _ok(client_invoice_dict(BillingWorkflowService.send_invoice(invoice, user=request.user)))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_register_payment(request, pk: int):
    invoice = get_object_or_404(ClientInvoice, pk=pk)
    try:
        _guard(can_register_payment(request, invoice))
        data = _json_body(request)
        payment = BillingWorkflowService.register_payment(
            invoice,
            amount=data.get("amount"),
            source=data.get("source", "manual"),
            comment=data.get("comment", ""),
            user=request.user,
        )
        invoice.refresh_from_db()
        return _ok({"payment_id": payment.id, "invoice": client_invoice_dict(invoice)})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_POST
def manager_financial_close(request, pk: int):
    application = get_object_or_404(BillingApplication, pk=pk)
    try:
        _guard(can_financially_close(request, application))
        return _ok(billing_application_dict(BillingWorkflowService.close_financially(application, user=request.user)))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_POST
def manager_mark_external(request, pk: int):
    """Пометить заявку: счета вне системы / не выставлять в Fullbox."""
    application = get_object_or_404(BillingApplication, pk=pk)
    try:
        _guard(can_mark_billing_external(request, application))
        body = _json_body(request)
        comment = str(body.get("comment") or "").strip()
        updated = BillingWorkflowService.mark_billed_outside(
            application, user=request.user, comment=comment
        )
        data = billing_application_dict(updated)
        data["billing_external"] = (updated.source_payload or {}).get("billing_external") or {}
        return _ok(data)
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_POST
def manager_manual_invoice_marker(request, pk: int):
    """Справочная отметка ручного счета в отчетах без изменения биллинг-статуса."""
    application = get_object_or_404(BillingApplication, pk=pk)
    try:
        _guard(can_create_invoice_draft(request, application))
        body = _json_body(request)
        number = str(body.get("number") or "").strip()[:64]
        is_issued = bool(body.get("is_issued"))
        payload = dict(application.source_payload or {})
        if number or is_issued:
            marker = {
                "number": number,
                "is_issued": is_issued,
                "updated_at": timezone.now().isoformat(),
                "updated_by_id": request.user.id,
                "updated_by": request.user.get_username(),
            }
            payload["shipping_pick_manual_invoice"] = marker
        else:
            marker = {"number": "", "is_issued": False}
            payload.pop("shipping_pick_manual_invoice", None)
        application.source_payload = payload
        application.save(update_fields=["source_payload", "updated_at"])
        return _ok({"manual_invoice": marker})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_POST
def manager_manual_invoice_marker_bulk(request):
    """Массовая справочная отметка ручного счета без изменения начислений и статусов."""
    try:
        body = _json_body(request)
        raw_ids = body.get("application_ids") or []
        if not isinstance(raw_ids, list):
            raise ValidationError("Передайте список заявок.")
        application_ids = []
        for value in raw_ids:
            try:
                app_id = int(value)
            except (TypeError, ValueError):
                raise ValidationError("Некорректный список заявок.")
            if app_id not in application_ids:
                application_ids.append(app_id)
        if not application_ids:
            raise ValidationError("Выберите хотя бы одну заявку.")
        if len(application_ids) > 200:
            raise ValidationError("За один раз можно отметить не больше 200 заявок.")

        applications = list(
            BillingApplication.objects.filter(id__in=application_ids).select_related("client")
        )
        if len(applications) != len(application_ids):
            raise ValidationError("Одна или несколько заявок биллинга не найдены.")
        for application in applications:
            _guard(can_create_invoice_draft(request, application))

        number = str(body.get("number") or "").strip()[:64]
        is_issued = bool(body.get("is_issued"))
        now = timezone.now().isoformat()
        marker = {
            "number": number,
            "is_issued": is_issued,
            "updated_at": now,
            "updated_by_id": request.user.id,
            "updated_by": request.user.get_username(),
        }

        with transaction.atomic():
            for application in applications:
                payload = dict(application.source_payload or {})
                if number or is_issued:
                    payload["shipping_pick_manual_invoice"] = marker
                else:
                    payload.pop("shipping_pick_manual_invoice", None)
                application.source_payload = payload
                application.save(update_fields=["source_payload", "updated_at"])

        return _ok({"manual_invoice": marker, "updated_count": len(applications)})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_record_storage_day(request, pk: int):
    from datetime import date

    from django.utils.dateparse import parse_date

    from .storage_billing import StorageBillingService

    application = get_object_or_404(BillingApplication, pk=pk)
    try:
        _guard(can_manage_charges(request, application))
        if application.application_type != BillingApplication.TYPE_STORAGE:
            raise ValidationError("Пересчёт хранения доступен только для заявок типа «Хранение».")
        body = _json_body(request)
        day = None
        raw_day = str(body.get("date") or "").strip()
        if raw_day:
            day = parse_date(raw_day)
            if not isinstance(day, date):
                raise ValidationError("Некорректная дата.")
        day_row = StorageBillingService.record_storage_day(
            application.client,
            day=day,
            user=request.user,
        )
        return _ok(
            {
                "day": day_row.day.isoformat(),
                "pallet_count": day_row.pallet_count,
                "zone_counts": day_row.zone_counts,
                "application_id": day_row.application_id,
                "charge_total": str(day_row.charge.total_amount) if day_row.charge_id else "0",
            }
        )
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_recalculate_open_storage(request):
    from .permissions import filter_agencies_for_user
    from .storage_billing import StorageBillingService

    try:
        _guard(can_view_billing(request))
        body = _json_body(request)
        today = timezone.localdate()
        year = int(body.get("year") or today.year)
        month = int(body.get("month") or today.month)
        client_id = body.get("client_id") or body.get("client") or None
        portfolio = filter_agencies_for_user(Agency.objects.all(), request)
        if client_id:
            client = get_object_or_404(Agency, pk=client_id)
            if not portfolio.filter(pk=client.pk).exists():
                raise PermissionDenied("Недостаточно прав для клиента.")
            stats = StorageBillingService.recalculate_open_period(
                year=year,
                month=month,
                client_id=client.pk,
                user=request.user,
            )
        else:
            stats = StorageBillingService.recalculate_open_period(
                year=year,
                month=month,
                client_ids=list(portfolio.values_list("pk", flat=True)),
                user=request.user,
            )
        return _ok(stats)
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, TypeError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_late_receiving_storage(request, pk: int):
    from .storage_billing import StorageBillingService
    try:
        application = get_object_or_404(BillingApplication, pk=pk)
        _guard(can_manage_charges(request, application))
        body = _json_body(request)
        return _ok(StorageBillingService.apply_late_receiving_storage(application, user=request.user, dry_run=bool(body.get("dry_run"))))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_GET
def manager_storage_days(request):
    """Детализация хранения для менеджера (только портфель)."""
    from .manager_billing import can_access_client
    from .models import BillingStorageDay
    from .permissions import filter_agencies_for_user
    from .storage_serializers import storage_day_dict

    try:
        _guard(can_view_billing(request))
        client_id = request.GET.get("client_id")
        year = int(request.GET.get("year") or timezone.localdate().year)
        month = int(request.GET.get("month") or timezone.localdate().month)
        portfolio = filter_agencies_for_user(Agency.objects.all(), request)
        qs = BillingStorageDay.objects.select_related("client", "charge", "application").filter(
            day__year=year, day__month=month, client__in=portfolio
        )
        if client_id:
            client = get_object_or_404(Agency, pk=client_id)
            _guard(can_access_client(request, client))
            qs = qs.filter(client_id=client_id)
        return _ok([storage_day_dict(d) for d in qs.order_by("-day")[:500]])
    except (PermissionDenied, TypeError, ValueError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_GET
def manager_storage_export_xlsx(request):
    """Manager-only Excel report for one client and selected storage period."""
    from calendar import monthrange

    from .manager_billing import can_access_client
    from .manager_storage_export import build_manager_storage_export
    from .models import BillingStorageDay
    from .permissions import filter_agencies_for_user

    try:
        _guard(can_view_billing(request))
        client_id = request.GET.get("client") or request.GET.get("client_id")
        if not client_id:
            raise ValidationError("Для выгрузки выберите клиента.")
        client = get_object_or_404(Agency, pk=client_id)
        _guard(can_access_client(request, client))
        raw_from = (request.GET.get("date_from") or "").strip()
        raw_to = (request.GET.get("date_to") or "").strip()
        if raw_from or raw_to:
            start = parse_date(raw_from) if raw_from else None
            end = parse_date(raw_to) if raw_to else None
            if not start or not end:
                raise ValidationError("Укажите корректный период выгрузки.")
        else:
            year = int(request.GET.get("year") or timezone.localdate().year)
            month = int(request.GET.get("month") or timezone.localdate().month)
            if not 1 <= month <= 12:
                raise ValidationError("Укажите месяц от 1 до 12.")
            start = date(year, month, 1)
            end = date(year, month, monthrange(year, month)[1])
        if end < start:
            start, end = end, start
        portfolio = filter_agencies_for_user(Agency.objects.all(), request)
        storage_days = BillingStorageDay.objects.select_related("client", "application", "charge").filter(
            client=client, client__in=portfolio, day__range=(start, end)
        )
        status = (request.GET.get("status") or "").strip()
        if status:
            storage_days = storage_days.filter(status=status)
        if request.GET.get("needs_review") in {"1", "true", "True"}:
            storage_days = storage_days.filter(status=BillingStorageDay.STATUS_NEEDS_REVIEW)
        return build_manager_storage_export(
            client=client, start=start, end=end, storage_days=storage_days.order_by("day", "id")
        )
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, TypeError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_GET
def manager_storage_day_detail(request, pk: int):
    from .manager_billing import can_access_client
    from .models import BillingStorageDay
    from .storage_serializers import storage_day_dict

    try:
        _guard(can_view_billing(request))
        day = get_object_or_404(
            BillingStorageDay.objects.select_related("client", "application", "charge").prefetch_related("lines"),
            pk=pk,
        )
        _guard(can_access_client(request, day.client))
        return _ok(storage_day_dict(day, with_lines=True))
    except PermissionDenied as exc:
        return _error(exc, status=403)


def _extra_service_dict(req) -> dict:
    return {
        "id": req.id,
        "client_id": req.client_id,
        "client_name": req.client.short_name or req.client.agn_name,
        "service_id": req.service_id,
        "service_name": req.service.name,
        "service_code": req.service.code,
        "service_date": req.service_date.isoformat() if req.service_date else None,
        "quantity": str(req.quantity),
        "unit": req.unit,
        "status": req.status,
        "status_label": req.get_status_display(),
        "price_note": req.price_note or "",
        "application_id": req.application_id,
        "charge_id": req.charge_id,
        "description": req.description or "",
        "reason": req.reason or "",
    }


@csrf_exempt
@login_required
@require_http_methods(["GET", "POST"])
def manager_extra_services(request):
    from .extra_services import create_extra_service_request, list_extra_service_requests
    from .manager_billing import can_access_client
    from .models import ExtraServiceRequest

    try:
        _guard(can_manage_charges(request))
        if request.method == "GET":
            status = request.GET.get("status") or ""
            client_id = request.GET.get("client_id") or ""
            rows = list_extra_service_requests(request, status=status, client_id=client_id)[:200]
            return _ok([_extra_service_dict(r) for r in rows])

        data = _json_body(request)
        client = get_object_or_404(Agency, pk=data.get("client_id"))
        _guard(can_access_client(request, client))
        service = get_object_or_404(BillingService, pk=data.get("service_id"))
        application = None
        if data.get("application_id"):
            application = get_object_or_404(BillingApplication, pk=data.get("application_id"))
            _guard(can_manage_charges(request, application))
        req = create_extra_service_request(
            user=request.user,
            client=client,
            service=service,
            quantity=data.get("quantity", "1"),
            service_date=data.get("service_date") or timezone.localdate().isoformat(),
            application=application,
            unit=data.get("unit") or "",
            warehouse_label=data.get("warehouse_label") or "",
            description=data.get("description") or "",
            reason=data.get("reason") or "",
            performer=data.get("performer") or "",
            internal_comment=data.get("internal_comment") or "",
            client_comment=data.get("client_comment") or "",
        )
        return _ok(_extra_service_dict(req))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError, InvalidOperation) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_extra_service_retry(request, pk: int):
    from .extra_services import try_materialize_extra_service_request
    from .manager_billing import can_access_client
    from .models import ExtraServiceRequest

    try:
        req = get_object_or_404(ExtraServiceRequest.objects.select_related("client", "service"), pk=pk)
        _guard(can_manage_charges(request) and can_access_client(request, req.client))
        updated = try_materialize_extra_service_request(req, user=request.user)
        return _ok(_extra_service_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_extra_service_cancel(request, pk: int):
    from .extra_services import cancel_extra_service_request
    from .manager_billing import can_access_client
    from .models import ExtraServiceRequest

    try:
        req = get_object_or_404(ExtraServiceRequest.objects.select_related("client", "service", "charge"), pk=pk)
        _guard(can_manage_charges(request) and can_access_client(request, req.client))
        updated = cancel_extra_service_request(req, user=request.user)
        return _ok(_extra_service_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_submit_act(request, pk: int):
    from .document_review import submit_act_for_review

    act = get_object_or_404(BillingAct.objects.select_related("application"), pk=pk)
    try:
        _guard(can_submit_document_review(request, act.application))
        data = _json_body(request)
        updated = submit_act_for_review(act, user=request.user, comment=data.get("comment", ""))
        return _ok(billing_act_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_submit_invoice(request, pk: int):
    from .document_review import submit_invoice_for_review

    invoice = get_object_or_404(ClientInvoice.objects.select_related("application"), pk=pk)
    try:
        _guard(can_submit_document_review(request, invoice.application))
        data = _json_body(request)
        updated = submit_invoice_for_review(invoice, user=request.user, comment=data.get("comment", ""))
        return _ok(client_invoice_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_return_act(request, pk: int):
    from .document_review import return_act_to_manager

    act = get_object_or_404(BillingAct.objects.select_related("application"), pk=pk)
    try:
        _guard(can_review_billing_document(request))
        data = _json_body(request)
        updated = return_act_to_manager(act, user=request.user, comment=data.get("comment", ""))
        return _ok(billing_act_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_return_invoice(request, pk: int):
    from .document_review import return_invoice_to_manager

    invoice = get_object_or_404(ClientInvoice.objects.select_related("application"), pk=pk)
    try:
        _guard(can_review_billing_document(request))
        data = _json_body(request)
        updated = return_invoice_to_manager(invoice, user=request.user, comment=data.get("comment", ""))
        return _ok(client_invoice_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_accept_act(request, pk: int):
    from .document_review import accept_act_review

    act = get_object_or_404(BillingAct.objects.select_related("application"), pk=pk)
    try:
        _guard(can_review_billing_document(request))
        data = _json_body(request)
        updated = accept_act_review(act, user=request.user, comment=data.get("comment", ""))
        return _ok(billing_act_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_accept_invoice(request, pk: int):
    from .document_review import accept_invoice_review

    invoice = get_object_or_404(ClientInvoice.objects.select_related("application"), pk=pk)
    try:
        _guard(can_review_billing_document(request))
        data = _json_body(request)
        updated = accept_invoice_review(invoice, user=request.user, comment=data.get("comment", ""))
        return _ok(client_invoice_dict(updated))
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["GET", "POST"])
def manager_upd_requests(request):
    from .document_review import create_upd_request, list_upd_requests
    from .manager_billing import can_access_client
    from .models import UpdRequest

    try:
        _guard(can_view_billing(request))
        if request.method == "GET":
            rows = list_upd_requests(
                request,
                status=request.GET.get("status") or "",
                client_id=request.GET.get("client_id") or "",
            )[:200]
            return _ok(
                [
                    {
                        "id": r.id,
                        "client_id": r.client_id,
                        "client_name": r.client.short_name or r.client.agn_name,
                        "status": r.status,
                        "status_label": r.get_status_display(),
                        "comment": r.comment,
                        "accountant_comment": r.accountant_comment,
                        "invoice_id": r.invoice_id,
                        "act_id": r.act_id,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ]
            )
        _guard(can_submit_document_review(request))
        data = _json_body(request)
        client = get_object_or_404(Agency, pk=data.get("client_id"))
        _guard(can_access_client(request, client))
        invoice = None
        act = None
        application = None
        if data.get("invoice_id"):
            invoice = get_object_or_404(ClientInvoice, pk=data.get("invoice_id"))
            _guard(can_view_billing(request, invoice.application))
            application = invoice.application
        if data.get("act_id"):
            act = get_object_or_404(BillingAct, pk=data.get("act_id"))
            _guard(can_view_billing(request, act.application))
            application = application or act.application
        if data.get("application_id"):
            application = get_object_or_404(BillingApplication, pk=data.get("application_id"))
            _guard(can_view_billing(request, application))
        req = create_upd_request(
            user=request.user,
            client=client,
            comment=data.get("comment") or "",
            application=application,
            invoice=invoice,
            act=act,
            period_from=parse_date(str(data.get("period_from"))) if data.get("period_from") else None,
            period_to=parse_date(str(data.get("period_to"))) if data.get("period_to") else None,
        )
        return _ok({"id": req.id, "status": req.status, "status_label": req.get_status_display()})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@login_required
@require_GET
def manager_audit_timeline(request):
    from .document_review import list_audit_timeline

    try:
        _guard(can_view_billing(request))
        rows = list_audit_timeline(
            request,
            application_id=request.GET.get("application_id") or "",
            limit=int(request.GET.get("limit") or 100),
        )
        return _ok(
            [
                {
                    "id": e.id,
                    "action": e.action,
                    "comment": e.comment,
                    "object_type": e.object_type,
                    "object_id": e.object_id,
                    "application_id": e.application_id,
                    "client": (
                        (e.application.client.short_name or e.application.client.agn_name)
                        if e.application_id
                        else ""
                    ),
                    "user": getattr(e.user, "username", "") if e.user_id else "",
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                    "new_value": e.new_value,
                }
                for e in rows
            ]
        )
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (TypeError, ValueError) as exc:
        return _error(exc, status=400)


@login_required
@require_GET
def manager_payments_ledger(request):
    from .payment_ops import list_portfolio_payments

    try:
        _guard(can_view_billing(request))
        qs = list_portfolio_payments(
            request,
            client_id=request.GET.get("client_id") or request.GET.get("client") or "",
            date_from=request.GET.get("date_from") or "",
            date_to=request.GET.get("date_to") or "",
        )[:300]
        return _ok(
            [
                {
                    "id": p.id,
                    "amount": str(p.amount),
                    "paid_at": p.paid_at.isoformat() if p.paid_at else None,
                    "source": p.source,
                    "comment": p.comment,
                    "invoice_id": p.invoice_id,
                    "invoice_number": p.invoice.number,
                    "client_id": p.invoice.client_id,
                    "client_name": p.invoice.client.short_name or p.invoice.client.agn_name,
                    "registered_by": getattr(p.registered_by, "username", "") if p.registered_by_id else "",
                }
                for p in qs
            ]
        )
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_GET
def manager_debts_summary(request):
    from .payment_ops import debt_summary

    try:
        _guard(can_view_billing(request))
        summary = debt_summary(request, client_id=request.GET.get("client_id") or request.GET.get("client") or "")
        return _ok(
            {
                "open_count": summary["open_count"],
                "open_debt": str(summary["open_debt"]),
                "overdue_count": summary["overdue_count"],
                "overdue_debt": str(summary["overdue_debt"]),
                "invoices": [
                    {
                        "id": inv.id,
                        "number": inv.number,
                        "client_id": inv.client_id,
                        "client_name": inv.client.short_name or inv.client.agn_name,
                        "debt_amount": str(inv.debt_amount),
                        "total_amount": str(inv.total_amount),
                        "due_date": inv.due_date.isoformat() if inv.due_date else None,
                        "status": inv.status,
                    }
                    for inv in summary["invoices"]
                ],
            }
        )
    except PermissionDenied as exc:
        return _error(exc, status=403)


@csrf_exempt
@login_required
@require_http_methods(["GET", "POST"])
def manager_payment_promises(request):
    from .manager_billing import can_access_client
    from .payment_ops import create_payment_promise, list_payment_promises

    try:
        _guard(can_view_billing(request))
        if request.method == "GET":
            rows = list_payment_promises(
                request,
                status=request.GET.get("status") or "",
                client_id=request.GET.get("client_id") or request.GET.get("client") or "",
            )[:200]
            return _ok(
                [
                    {
                        "id": p.id,
                        "client_id": p.client_id,
                        "client_name": p.client.short_name or p.client.agn_name,
                        "invoice_id": p.invoice_id,
                        "amount": str(p.amount),
                        "promised_date": p.promised_date.isoformat() if p.promised_date else None,
                        "status": p.status,
                        "status_label": p.get_status_display(),
                        "contact_person": p.contact_person,
                        "manager_comment": p.manager_comment,
                    }
                    for p in rows
                ]
            )
        _guard(can_submit_document_review(request))
        data = _json_body(request)
        client = get_object_or_404(Agency, pk=data.get("client_id"))
        _guard(can_access_client(request, client))
        invoice = None
        if data.get("invoice_id"):
            invoice = get_object_or_404(ClientInvoice, pk=data.get("invoice_id"))
            _guard(can_view_billing(request, invoice.application))
        promise = create_payment_promise(
            user=request.user,
            client=client,
            amount=data.get("amount"),
            promised_date=data.get("promised_date"),
            invoice=invoice,
            contact_person=data.get("contact_person") or "",
            client_comment=data.get("client_comment") or "",
            manager_comment=data.get("manager_comment") or "",
        )
        return _ok({"id": promise.id, "status": promise.status, "status_label": promise.get_status_display()})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["PATCH", "POST"])
def manager_payment_promise_detail(request, pk: int):
    from .manager_billing import can_access_client
    from .models import PaymentPromise
    from .payment_ops import update_payment_promise_status

    try:
        _guard(can_view_billing(request))
        promise = get_object_or_404(PaymentPromise.objects.select_related("client", "invoice"), pk=pk)
        _guard(can_access_client(request, promise.client))
        data = _json_body(request)
        updated = update_payment_promise_status(
            promise,
            status=data.get("status") or "",
            user=request.user,
            result_note=data.get("result_note") or "",
        )
        return _ok({"id": updated.id, "status": updated.status, "status_label": updated.get_status_display()})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["GET", "POST"])
def manager_reconciliation_requests(request):
    from .manager_billing import can_access_client
    from .payment_ops import create_reconciliation_request, list_reconciliation_requests

    try:
        _guard(can_view_billing(request))
        if request.method == "GET":
            rows = list_reconciliation_requests(
                request,
                status=request.GET.get("status") or "",
                client_id=request.GET.get("client_id") or request.GET.get("client") or "",
            )[:200]
            return _ok(
                [
                    {
                        "id": r.id,
                        "client_id": r.client_id,
                        "client_name": r.client.short_name or r.client.agn_name,
                        "period_from": r.period_from.isoformat() if r.period_from else None,
                        "period_to": r.period_to.isoformat() if r.period_to else None,
                        "opening_balance": str(r.opening_balance),
                        "status": r.status,
                        "status_label": r.get_status_display(),
                        "manager_comment": r.manager_comment,
                    }
                    for r in rows
                ]
            )
        _guard(can_submit_document_review(request))
        data = _json_body(request)
        client = get_object_or_404(Agency, pk=data.get("client_id"))
        _guard(can_access_client(request, client))
        req = create_reconciliation_request(
            user=request.user,
            client=client,
            period_from=data.get("period_from"),
            period_to=data.get("period_to"),
            opening_balance=data.get("opening_balance") or 0,
            manager_comment=data.get("manager_comment") or "",
            submit=bool(data.get("submit")),
        )
        return _ok({"id": req.id, "status": req.status, "status_label": req.get_status_display()})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_POST
def manager_reconciliation_submit(request, pk: int):
    from .manager_billing import can_access_client
    from .models import ReconciliationRequest
    from .payment_ops import submit_reconciliation_request

    try:
        _guard(can_view_billing(request))
        req = get_object_or_404(ReconciliationRequest.objects.select_related("client"), pk=pk)
        _guard(can_access_client(request, req.client))
        updated = submit_reconciliation_request(req, user=request.user)
        return _ok({"id": updated.id, "status": updated.status, "status_label": updated.get_status_display()})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["GET", "POST"])
def manager_discrepancies(request):
    from .manager_billing import can_access_client
    from .models import BillingDiscrepancy
    from .payment_ops import create_discrepancy, list_discrepancies

    try:
        _guard(can_view_billing(request))
        if request.method == "GET":
            rows = list_discrepancies(
                request,
                status=request.GET.get("status") or "",
                client_id=request.GET.get("client_id") or request.GET.get("client") or "",
            )[:200]
            return _ok(
                [
                    {
                        "id": d.id,
                        "client_id": d.client_id,
                        "client_name": d.client.short_name or d.client.agn_name,
                        "discrepancy_type": d.discrepancy_type,
                        "type_label": d.get_discrepancy_type_display(),
                        "description": d.description,
                        "disputed_amount": str(d.disputed_amount) if d.disputed_amount is not None else None,
                        "status": d.status,
                        "status_label": d.get_status_display(),
                        "act_dispute_id": d.act_dispute_id,
                    }
                    for d in rows
                ]
            )
        _guard(can_submit_document_review(request))
        data = _json_body(request)
        client = get_object_or_404(Agency, pk=data.get("client_id"))
        _guard(can_access_client(request, client))
        row = create_discrepancy(
            user=request.user,
            client=client,
            description=data.get("description") or "",
            discrepancy_type=data.get("discrepancy_type") or BillingDiscrepancy.TYPE_OTHER,
            disputed_amount=data.get("disputed_amount"),
            manager_comment=data.get("manager_comment") or "",
        )
        return _ok({"id": row.id, "status": row.status, "status_label": row.get_status_display()})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (ValidationError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
@login_required
@require_http_methods(["PATCH", "POST"])
def manager_discrepancy_detail(request, pk: int):
    from .manager_billing import can_access_client
    from .models import BillingDiscrepancy
    from .payment_ops import update_discrepancy_status

    try:
        _guard(can_view_billing(request))
        row = get_object_or_404(BillingDiscrepancy.objects.select_related("client"), pk=pk)
        _guard(can_access_client(request, row.client))
        data = _json_body(request)
        updated = update_discrepancy_status(
            row,
            status=data.get("status") or "",
            user=request.user,
            result_note=data.get("result_note") or "",
        )
        return _ok({"id": updated.id, "status": updated.status, "status_label": updated.get_status_display()})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc)


@login_required
@require_GET
def manager_notifications(request):
    from .staff_notifications import KIND_LABELS, list_staff_notifications, sync_portfolio_alerts

    try:
        _guard(can_view_billing(request))
        sync_portfolio_alerts(request, limit=20)
        unread_only = (request.GET.get("unread") or "") == "1"
        rows = list_staff_notifications(request, unread_only=unread_only)[:200]
        return _ok(
            [
                {
                    "id": n.id,
                    "kind": n.kind,
                    "kind_label": KIND_LABELS.get(n.kind, n.get_kind_display()),
                    "title": n.title,
                    "message": n.message,
                    "link_url": n.link_url,
                    "is_read": n.is_read,
                    "client_id": n.client_id,
                    "client_name": (n.client.short_name or n.client.agn_name) if n.client_id else "",
                    "created_at": n.created_at.isoformat() if n.created_at else None,
                }
                for n in rows
            ]
        )
    except PermissionDenied as exc:
        return _error(exc, status=403)


@csrf_exempt
@login_required
@require_POST
def manager_notification_read(request, pk: int):
    from .models import BillingStaffNotification
    from .staff_notifications import mark_notification_read

    try:
        _guard(can_view_billing(request))
        row = get_object_or_404(BillingStaffNotification, pk=pk, recipient=request.user)
        updated = mark_notification_read(row, user=request.user)
        return _ok({"id": updated.id, "is_read": updated.is_read})
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except PermissionError as exc:
        return _error(exc, status=403)


@csrf_exempt
@login_required
@require_POST
def manager_notifications_read_all(request):
    from .staff_notifications import mark_all_read

    try:
        _guard(can_view_billing(request))
        updated = mark_all_read(request)
        return _ok({"updated": updated})
    except PermissionDenied as exc:
        return _error(exc, status=403)
