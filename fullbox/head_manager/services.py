from __future__ import annotations

import json

from django.contrib import messages
from django.shortcuts import redirect

from sku.models import Agency


def _views():
    from . import views as head_manager_views

    return head_manager_views


def build_reference_list_context(*, title: str, subtitle: str, create_url: str, edit_base_url: str, back_url: str, type_label: str, directory_mode: str) -> dict:
    return {
        "title": title,
        "subtitle": subtitle,
        "create_url": create_url,
        "edit_base_url": edit_base_url,
        "back_url": back_url,
        "type_label": type_label,
        "directory_mode": directory_mode,
    }


def build_reference_form_context(*, title: str, subtitle: str, back_url: str, submit_label: str) -> dict:
    return {
        "title": title,
        "subtitle": subtitle,
        "back_url": back_url,
        "submit_label": submit_label,
    }


def build_marketplace_warehouses_context(*, request, saved: bool = False, error: str = "") -> dict:
    data = _views()._load_marketplace_warehouses()
    return {
        "warehouse_marketplaces": [
            {"key": "wb", "label": "Wildberries", "rows": data.get("wb", [])},
            {"key": "ozon", "label": "Ozon", "rows": data.get("ozon", [])},
            {"key": "yandex", "label": "Яндекс Маркет", "rows": data.get("yandex", [])},
            {"key": "sber", "label": "Сбер", "rows": data.get("sber", [])},
        ],
        "warehouse_rows_json": data,
        "saved": saved,
        "error": error,
        "sync_info": request.session.pop("marketplace_sync_info", ""),
        "sync_errors": request.session.pop("marketplace_sync_errors", []),
        "sync_agency": request.session.pop("marketplace_sync_agency", ""),
        "sync_default_name": "Кейзи",
    }


def save_marketplace_warehouses_response(*, request):
    def normalize(key: str) -> list[dict]:
        raw = request.POST.get(key, "") or "[]"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return _views()._normalize_marketplace_warehouse_rows(payload)

    data = {
        "wb": normalize("wb"),
        "ozon": normalize("ozon"),
        "yandex": normalize("yandex"),
        "sber": normalize("sber"),
    }
    try:
        user = request.user.username if request.user.is_authenticated else ""
        _views()._save_marketplace_warehouses(data, user=user)
    except OSError:
        return None, "Не удалось сохранить список."
    return redirect("/head-manager/marketplace-warehouses/?saved=1"), ""


def sync_marketplace_warehouses_response(*, request):
    client_id = (request.POST.get("client_id") or "").strip()
    client_name = (request.POST.get("client_name") or "").strip()
    if not client_id and not client_name:
        client_name = "Кейзи"

    agency = _views()._find_agency(client_id, client_name)
    if not agency:
        request.session["marketplace_sync_errors"] = [
            "Клиент не найден. Укажите ID клиента или название.",
        ]
        return redirect("/head-manager/marketplace-warehouses/")

    data, errors = _views()._sync_marketplace_warehouses(agency)
    errors = [err for err in errors if err]

    try:
        user = request.user.username if request.user.is_authenticated else ""
        meta = {
            "source": "sync",
            "agency_id": agency.id,
            "agency_name": agency.agn_name or "",
        }
        _views()._save_marketplace_warehouses(data, user=user, meta=meta)
        wb_count = len(data.get("wb", []))
        ozon_count = len(data.get("ozon", []))
        request.session["marketplace_sync_info"] = (
            f"Синхронизация выполнена: WB {wb_count}, Ozon {ozon_count}."
        )
        request.session["marketplace_sync_agency"] = agency.agn_name or str(agency.id)
    except OSError:
        errors.append("Не удалось сохранить список.")

    if errors:
        request.session["marketplace_sync_errors"] = errors

    return redirect("/head-manager/marketplace-warehouses/")
