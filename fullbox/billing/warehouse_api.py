"""API для кладовщика: каталог согласованных услуг и факты (без цен)."""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from employees.access import get_request_role
from sku.models import Agency

from .warehouse_services import (
    WAREHOUSE_ROLES,
    facts_payload,
    list_agreed_services_for_storekeeper,
    list_facts,
    normalize_order_type,
    replace_warehouse_facts,
    review_unlisted_warehouse_fact,
    shipping_pick_auto_service_suggestions,
)


WAREHOUSE_FACT_REVIEW_ROLES = {"manager", "head_manager", "admin", "director"}


def _ok(data, status: int = 200):
    return JsonResponse({"ok": True, "data": data}, status=status)


def _error(exc, status: int = 400):
    if isinstance(exc, PermissionDenied):
        status = 403
    message = str(exc)
    if hasattr(exc, "messages"):
        try:
            message = "; ".join(str(m) for m in exc.messages)
        except Exception:
            pass
    return JsonResponse({"ok": False, "error": message}, status=status)


def _json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception as exc:
        raise ValidationError("Некорректный JSON.") from exc
    if not isinstance(payload, dict):
        raise ValidationError("Ожидается JSON-объект.")
    return payload


def _require_warehouse_role(request):
    role = get_request_role(request) or ""
    if role not in WAREHOUSE_ROLES and not getattr(request.user, "is_superuser", False):
        raise PermissionDenied("Недостаточно прав для указания услуг склада.")
    return role


def _require_review_role(request):
    role = get_request_role(request) or ""
    if role not in WAREHOUSE_FACT_REVIEW_ROLES and not getattr(request.user, "is_superuser", False):
        raise PermissionDenied("Недостаточно прав для проверки услуги.")
    return role


def _resolve_client(request, data: dict | None = None, *, role: str = "") -> Agency:
    data = data or {}
    raw = (
        request.GET.get("client")
        or request.GET.get("agency")
        or data.get("client_id")
        or data.get("agency_id")
    )
    if not raw:
        raise ValidationError("Укажите клиента.")
    try:
        client_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Некорректный клиент.") from exc
    client = Agency.objects.filter(pk=client_id).first()
    if not client:
        raise ValidationError("Клиент не найден.")
    if role == "manager":
        from .manager_billing import can_access_client

        if not can_access_client(request, client):
            raise PermissionDenied("Нет доступа к клиенту.")
    return client


@csrf_exempt
@login_required
@require_http_methods(["GET"])
def warehouse_service_catalog(request):
    """GET: согласованные услуги клиента для раздела (без цен)."""
    try:
        role = _require_warehouse_role(request)
        client = _resolve_client(request, role=role)
        process_type = request.GET.get("process") or request.GET.get("order_type") or ""
        rows = list_agreed_services_for_storekeeper(client, process_type=process_type)
        return _ok({"services": rows, "client_id": client.id, "process": process_type})
    except (PermissionDenied, ValidationError) as exc:
        return _error(exc, status=403 if isinstance(exc, PermissionDenied) else 400)


@csrf_exempt
@login_required
@require_http_methods(["GET", "PUT", "POST"])
def warehouse_service_facts(request):
    """GET/PUT факты услуг по заявке. Тело PUT: {lines:[{service_id, quantity}]} без цен."""
    try:
        role = _require_warehouse_role(request)
        data = _json_body(request) if request.method != "GET" else {}
        client = _resolve_client(request, data, role=role)
        order_type = (
            request.GET.get("order_type")
            or request.GET.get("process")
            or data.get("order_type")
            or data.get("process")
            or ""
        )
        order_id = str(
            request.GET.get("order_id")
            or data.get("order_id")
            or ""
        ).strip()
        if not order_id:
            raise ValidationError("Укажите номер заявки.")
        if request.method == "GET":
            facts = list_facts(client=client, order_type=order_type, order_id=order_id)
            automatic = {
                "automatic_facts": [],
                "automatic_service_ids": [],
                "automatic_summary": {},
                "automatic_warnings": [],
            }
            if normalize_order_type(order_type) == "shipping":
                automatic = shipping_pick_auto_service_suggestions(
                    client=client,
                    order_id=order_id,
                )
            return _ok(
                {
                    "facts": facts_payload(facts),
                    **automatic,
                    "client_id": client.id,
                    "order_type": order_type,
                    "order_id": order_id,
                }
            )
        lines = data.get("lines") or data.get("facts") or []
        if not isinstance(lines, list):
            raise ValidationError("lines должен быть массивом.")
        # Защита: цены из склада игнорируем полностью. Остальные поля —
        # операционный факт и нужны менеджеру для проверки расхождений.
        sanitized = []
        for row in lines:
            if not isinstance(row, dict):
                continue
            sanitized.append(
                {
                    "service_id": row.get("service_id") or row.get("id"),
                    "custom_name": row.get("custom_name") or "",
                    "custom_key": row.get("custom_key") or "",
                    "quantity": row.get("quantity"),
                    "unit": row.get("unit") or "",
                    "comment": row.get("comment") or "",
                    "planned_quantity": row.get("planned_quantity"),
                    "auto_quantity": row.get("auto_quantity"),
                    "discrepancy_reason": row.get("discrepancy_reason") or "",
                    "source": row.get("source") or "warehouse_manual",
                    "warehouse_label": row.get("warehouse_label") or "",
                    "performed_at": row.get("performed_at"),
                    "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
                }
            )
        facts = replace_warehouse_facts(
            client=client,
            order_type=order_type,
            order_id=order_id,
            lines=sanitized,
            user=request.user,
        )
        return _ok(
            {
                "facts": facts_payload(facts),
                "client_id": client.id,
                "order_type": order_type,
                "order_id": order_id,
                "saved": len(facts),
            }
        )
    except (PermissionDenied, ValidationError) as exc:
        return _error(exc, status=403 if isinstance(exc, PermissionDenied) else 400)


@login_required
@require_http_methods(["POST"])
def warehouse_service_fact_review(request, pk: int):
    """Сопоставить услугу вне списка; начисление этим действием не создаётся."""
    try:
        role = _require_review_role(request)
        data = _json_body(request)
        from .models import WarehouseServiceFact

        fact = WarehouseServiceFact.objects.select_related("client").filter(pk=pk).first()
        if not fact:
            raise ValidationError("Факт услуги не найден.")
        if role == "manager":
            from .manager_billing import can_access_client

            if not can_access_client(request, fact.client):
                raise PermissionDenied("Нет доступа к клиенту.")
        reviewed = review_unlisted_warehouse_fact(
            fact_id=fact.pk,
            action=data.get("action"),
            user=request.user,
            service_id=data.get("service_id"),
            quantity=data.get("quantity"),
            unit=data.get("unit") or "",
            comment=data.get("comment") or "",
            manager_comment=data.get("manager_comment") or "",
        )
        return _ok({"fact": facts_payload([reviewed])[0]})
    except (PermissionDenied, ValidationError) as exc:
        return _error(exc, status=403 if isinstance(exc, PermissionDenied) else 400)
