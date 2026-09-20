from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from billing.models import ClientInvoice
from sklad.models import WarehouseLocation
from sku.models import Agency

from ..models import (
    WmsNewBox,
    WmsNewBoxItem,
    WmsNewDocument,
    WmsNewOrder,
    WmsNewProduct,
    WmsNewReturn,
    WmsNewTask,
)


REPORT_TITLES = {
    "reports-goods": ("Товары", "Отчет по товарам"),
    "reports-goods-places": ("Товары по местам хранения", "Отчет по товарам"),
    "reports-places": ("Занятые места", "Отчет по занятым местам"),
    "reports-returns": ("Возвраты с маркетплейсов", "Отчет по возвратам с маркетплейсов"),
    "reports-shipments": ("Отгруженные товары", "Отчет по отгруженным товарам"),
    "reports-tasks": ("Задачи", "Отчет по задачам"),
    "reports-fbs-status": ("Заказы FBS - статус сборки", "Отчет по статусам сборки FBS"),
    "reports-fbs-count": ("Количество FBS заказов", "Отчет по количеству отправленных FBS заказов"),
    "reports-fbs-orders": ("Отгруженные FBS заказы", "Отчет по количеству отправленных FBS заказов"),
    "reports-fbs-goods": ("Отгруженные товары (FBS)", "Отчет по товарам, отгруженным в FBS заказах"),
    "reports-invoices": ("Счета и оплаты", "Отчет по счетам и оплатам"),
}


def _date_value(value, default):
    text = str(value or "").strip()
    parsed = parse_date(text)
    if parsed:
        return parsed
    for fmt in ("%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return default


def _datetime_value(value, default):
    text = str(value or "").strip()
    parsed = parse_datetime(text)
    if parsed:
        return parsed
    for fmt in ("%d-%m-%Y %H:%M", "%d.%m.%Y %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            return timezone.make_aware(parsed, timezone.get_current_timezone())
        except ValueError:
            pass
    return default


def _agency_options(*, include_all=True):
    rows = [(str(item.id), str(item)) for item in Agency.objects.filter(archived=False).order_by("agn_name", "id")]
    return ([('', "Все партнеры")] if include_all else []) + rows


def _field(name, label, value, *, field_type="select", options=(), checked=False):
    return {
        "name": name,
        "label": label,
        "value": str(value or ""),
        "type": field_type,
        "options": tuple(options),
        "checked": bool(checked),
    }


def _common_dates(params):
    today = timezone.localdate()
    start = _date_value(params.get("date_from"), today - timedelta(days=30))
    end = _date_value(params.get("date_to"), today)
    if start > end:
        start, end = end, start
    return start, end


def report_filter_fields(slug: str, params) -> list[dict]:
    today = timezone.localdate()
    now = timezone.localtime()
    start, end = _common_dates(params)
    partner = str(params.get("partner") or "")
    if slug == "reports-goods":
        zones = [('', "Все зоны")] + [(z, z) for z in WmsNewBox.objects.exclude(zone_code="").values_list("zone_code", flat=True).distinct().order_by("zone_code")]
        return [
            _field("partner", "Партнер", partner, options=_agency_options()),
            _field("zero_stock", "Остатки", params.get("zero_stock") or "nonzero", options=(("nonzero", "Без нулевых остатков"), ("all", "С нулевыми остатками"))),
            _field("place_detail", "Детализация", params.get("place_detail") or "0", options=(("0", "Без детализации по местам"), ("1", "С детализацией по местам"))),
            _field("transit", "Места", params.get("transit") or "all", options=(("all", "Все места"), ("only", "Только транзитные"), ("exclude", "Кроме транзитных"))),
            _field("hide_disabled", "Без скрытых мест", "1", field_type="checkbox", checked=str(params.get("hide_disabled") or "1") == "1"),
            _field("barcode_mode", "Штрихкоды", params.get("barcode_mode") or "first", options=(("first", "Только первый ШК"), ("all", "Все ШК"))),
            _field("zone", "Зона", params.get("zone") or "", options=zones),
            _field("as_of", "По состоянию на", params.get("as_of") or now.strftime("%Y-%m-%dT%H:%M"), field_type="datetime-local"),
        ]
    if slug == "reports-goods-places":
        zones = [('', "Все зоны")] + [(z, z) for z in WmsNewBox.objects.exclude(zone_code="").values_list("zone_code", flat=True).distinct().order_by("zone_code")]
        return [
            _field("place_type", "Тип места", params.get("place_type") or "all", options=(("all", "Все типы мест"), ("notransit", "Без транзитных"))),
            _field("occupancy", "Заполненность", params.get("occupancy") or "any", options=(("any", "Любая заполненность"), ("empty", "Только полностью свободные места"), ("less25", "Занято менее 25%"), ("less50", "Занято менее 50%"), ("less75", "Занято менее 75%"), ("more75", "Занято более 75%"))),
            _field("product_detail", "Детализация", params.get("product_detail") or "0", options=(("0", "Без детализации по товарам"), ("1", "С детализацией по товарам"))),
            _field("transit", "Места", params.get("transit") or "all", options=(("all", "Все места"), ("only", "Только транзитные"), ("exclude", "Кроме транзитных"))),
            _field("hide_disabled", "Без скрытых мест", "1", field_type="checkbox", checked=str(params.get("hide_disabled") or "1") == "1"),
            _field("zone", "Зона", params.get("zone") or "", options=zones),
            _field("as_of", "По состоянию на", params.get("as_of") or now.strftime("%Y-%m-%dT%H:%M"), field_type="datetime-local"),
        ]
    if slug == "reports-places":
        return [
            _field("place_type", "Тип места", params.get("place_type") or "all", options=(("all", "Все типы мест"), ("notransit", "Без транзитных"))),
            _field("hide_disabled", "Без скрытых мест", "1", field_type="checkbox", checked=str(params.get("hide_disabled") or "1") == "1"),
            _field("date_from", "С", start.isoformat(), field_type="date"),
            _field("date_to", "По", end.isoformat(), field_type="date"),
        ]
    if slug == "reports-returns":
        return [
            _field("partner", "Партнер", partner, options=_agency_options()),
            _field("marketplace", "Маркетплейс", params.get("marketplace") or "all", options=(("all", "Все маркетплейсы"), ("wildberries", "Вайлдберриз"), ("ozon", "ОЗОН"), ("yandex", "Яндекс Маркет"))),
            _field("date_from", "Дата с", start.isoformat(), field_type="date"),
            _field("date_to", "По", end.isoformat(), field_type="date"),
            _field("status", "Статус", params.get("status") or "", options=(('', "Любой статус"),) + tuple(WmsNewReturn.STATUS_CHOICES)),
            _field("scheme", "Схема", params.get("scheme") or "all", options=(("all", "Любая схема"), ("fbo", "FBO"), ("fbs", "FBS"))),
        ]
    if slug == "reports-shipments":
        return [
            _field("partner", "Партнер", partner, options=_agency_options()),
            _field("shipment_type", "Тип отгрузки", params.get("shipment_type") or "all", options=(("all", "Все типы отгрузки"), ("orders", "Заказы"), ("tasks", "Задачи"), ("documents", "Документы без задач"))),
            _field("grouping", "Группировка", params.get("grouping") or "document", options=(("date", "Группировать по дате"), ("document", "Группировать по основанию"), ("goods", "Группировать за весь период"))),
            _field("date_from", "Дата с", start.isoformat(), field_type="date"),
            _field("date_to", "По", end.isoformat(), field_type="date"),
        ]
    if slug == "reports-tasks":
        return [
            _field("partner", "Партнер", partner, options=_agency_options()),
            _field("task_type", "Тип задачи", params.get("task_type") or "all", options=(("all", "Все задачи"), ("acceptance", "Приемка"), ("processing", "Обработка"), ("shipment", "Отгрузка"), ("full", "Полный цикл"), ("other", "Прочие"))),
            _field("as_of_date", "На дату", params.get("as_of_date") or today.isoformat(), field_type="date"),
        ]
    if slug == "reports-fbs-status":
        return [_field("as_of", "По состоянию на", params.get("as_of") or now.strftime("%Y-%m-%dT%H:%M"), field_type="datetime-local")]
    if slug in {"reports-fbs-count", "reports-fbs-orders"}:
        fields = []
        if slug == "reports-fbs-count":
            fields.append(_field("partner", "Партнер", partner, options=_agency_options()))
        fields.extend((_field("date_from", "С", start.isoformat(), field_type="date"), _field("date_to", "По", end.isoformat(), field_type="date")))
        return fields
    if slug == "reports-fbs-goods":
        return [
            _field("partner", "Партнер", partner, options=_agency_options()),
            _field("date_from", "С", start.isoformat(), field_type="date"),
            _field("date_to", "По", end.isoformat(), field_type="date"),
            _field("group_by_date", "Группировка", params.get("group_by_date") or "1", options=(("1", "Группировать по дате"), ("2", "Объединить все даты"))),
        ]
    return [
        _field("partner", "Партнер", partner, options=_agency_options()),
        _field("invoice_type", "Тип счета", params.get("invoice_type") or "finished", options=(("all", "Все типы счетов"), ("finished", "Выставленные счета"), ("draft", "Черновики счетов"))),
        _field("period_type", "Период", params.get("period_type") or "created", options=(("created", "Дата создания"), ("due", "Оплатить до"), ("paid", "Дата фактической оплаты"))),
        _field("date_from", "С", start.isoformat(), field_type="date"),
        _field("date_to", "По", end.isoformat(), field_type="date"),
    ]


def _partner_filter(queryset, partner):
    text = str(partner or "").strip()
    return queryset.filter(agency_id=int(text)) if text.isdigit() else queryset


def _product_volume_m3(product: WmsNewProduct) -> Decimal:
    return (product.width_cm * product.depth_cm * product.height_cm) / Decimal("1000000")


def _goods_rows(params):
    queryset = WmsNewProduct.objects.select_related("agency").filter(is_archived=False)
    queryset = _partner_filter(queryset, params.get("partner"))
    if (params.get("zero_stock") or "nonzero") == "nonzero":
        queryset = queryset.filter(stock_on_hand__gt=0)
    zone = str(params.get("zone") or "").strip()
    if zone:
        queryset = queryset.filter(box_items__box__zone_code=zone).distinct()
    rows = []
    for product in queryset.order_by("agency__agn_name", "name", "article"):
        rows.append((
            str(product.agency), product.name, product.barcode or "-", product.article,
            product.stock_on_hand, product.fbo_reserved, product.fbs_reserved,
            product.expected_qty, "-", f"{_product_volume_m3(product) * product.stock_on_hand:.6f} м3",
        ))
    return ("Партнер", "Товар", "ШК", "Артикул", "Всего на складе", "Резерв FBO", "Резерв FBS", "Ожидается по приемкам", "Заказы", "Объем"), rows


def _goods_places_rows(params):
    detail = str(params.get("product_detail") or "0") == "1"
    boxes = WmsNewBox.objects.filter(status=WmsNewBox.STATUS_ACTIVE).select_related("location")
    zone = str(params.get("zone") or "").strip()
    if zone:
        boxes = boxes.filter(zone_code=zone)
    transit = str(params.get("transit") or "all")
    if (params.get("place_type") == "notransit") or transit == "exclude":
        boxes = boxes.exclude(location__zone_kind=WarehouseLocation.ZONE_KIND_TRANSIT)
    elif transit == "only":
        boxes = boxes.filter(location__zone_kind=WarehouseLocation.ZONE_KIND_TRANSIT)
    if detail:
        items = WmsNewBoxItem.objects.filter(box__in=boxes, qty__gt=0).select_related("box__location", "product")
        rows = [(str(item.box.location or item.box.location_code or "-"), item.box.location.get_zone_kind_display() if item.box.location else "-", item.product_name, item.sku_code, item.qty, item.available_qty) for item in items.order_by("box__location_code", "product_name", "id")]
        return ("Место", "Тип", "Товар", "Артикул", "Количество", "Доступно"), rows
    grouped = defaultdict(lambda: {"location": None, "occupied": Decimal("0")})
    for item in WmsNewBoxItem.objects.filter(box__in=boxes, qty__gt=0).select_related("box__location", "product"):
        key = item.box.location_id or f"box:{item.box_id}"
        row = grouped[key]
        row["location"] = item.box.location
        if item.product_id:
            row["occupied"] += _product_volume_m3(item.product) * item.qty
    rows = []
    known_locations = {box.location_id: box.location for box in boxes if box.location_id}
    for location_id, location in known_locations.items():
        occupied = grouped[location_id]["occupied"] if location_id in grouped else Decimal("0")
        rows.append((str(location), location.get_zone_kind_display(), "-", "-", f"{occupied:.6f} м3", "-"))
    rows.sort(key=lambda row: row[0])
    return ("Место", "Тип", "Габариты места", "Объем места", "Объем занятого", "% занятого"), rows


def _occupied_places_rows(params):
    start, end = _common_dates(params)
    values = (
        WmsNewBox.objects.filter(status=WmsNewBox.STATUS_ACTIVE, stock_on_hand__gt=0)
        .values("agency__agn_name")
        .annotate(places=Count("location_id", distinct=True), units=Sum("stock_on_hand"))
        .order_by("agency__agn_name")
    )
    rows = [(row["agency__agn_name"], end.isoformat(), row["places"], int(row["units"] or 0)) for row in values]
    return ("Партнер", "Дата", "Кол-во мест всего", "Кол-во товаров"), rows


def _return_rows(params):
    start, end = _common_dates(params)
    queryset = WmsNewReturn.objects.select_related("order__agency").filter(source_created_at__date__range=(start, end))
    partner = str(params.get("partner") or "").strip()
    if partner.isdigit():
        queryset = queryset.filter(order__agency_id=int(partner))
    status = str(params.get("status") or "")
    if status in dict(WmsNewReturn.STATUS_CHOICES):
        queryset = queryset.filter(status=status)
    marketplace = str(params.get("marketplace") or "all")
    if marketplace != "all":
        queryset = queryset.filter(order__marketplace__iexact=marketplace)
    rows = [(str(item.order.agency), item.order.marketplace or "-", item.order.external_order_id, item.get_status_display(), item.reason or item.reason_code or "-", item.planned_qty, item.returned_qty, item.source_created_at or item.created_at, item.returned_at or "-") for item in queryset.order_by("-source_created_at", "-id")]
    return ("Партнер", "Маркетплейс", "Заказ", "Статус", "Причина", "Ожидается", "Возвращено", "Создан", "Возвращен"), rows


def _shipment_rows(params):
    start, end = _common_dates(params)
    partner = str(params.get("partner") or "")
    orders = WmsNewOrder.objects.select_related("agency").prefetch_related("items").filter(
        status__in=(WmsNewOrder.STATUS_HANDED_OVER, WmsNewOrder.STATUS_DONE),
        source_updated_at__date__range=(start, end),
    )
    orders = _partner_filter(orders, partner)
    rows = []
    for order in orders.order_by("-source_updated_at", "-id"):
        for item in order.items.all():
            rows.append((str(order.agency), item.product_name, item.external_sku, item.barcode, "Заказ", order.external_order_id, (order.source_updated_at or order.updated_at).date(), "-", order.source_updated_at or order.updated_at, order.delivery_type or order.marketplace, item.quantity))
    return ("Партнер", "Название товара", "Артикул товара", "ШК товара", "Тип отгрузки", "Номер основания", "Дата списания", "Номер отгрузки FBS", "Дата отгрузки FBS", "Маркетплейс", "Количество"), rows


def _task_rows(params):
    queryset = WmsNewTask.objects.select_related("agency", "assigned_to")
    queryset = _partner_filter(queryset, params.get("partner"))
    task_type = str(params.get("task_type") or "all")
    if task_type in dict(WmsNewTask.TYPE_CHOICES):
        queryset = queryset.filter(workflow_type=task_type)
    rows = [(item.source_task_id or item.id, str(item.agency or "-"), item.get_workflow_type_display(), item.title, item.get_status_display(), item.get_priority_display(), str(item.assigned_to or "-"), item.due_date or "-", item.created_at) for item in queryset.order_by("agency__agn_name", "status", "-id")]
    return ("№", "Партнер", "Тип", "Задача", "Статус", "Приоритет", "Исполнитель", "Срок", "Создана"), rows


def _status_rows(params):
    queryset = WmsNewOrder.objects.select_related("agency").exclude(status__in=(WmsNewOrder.STATUS_CANCELLED, WmsNewOrder.STATUS_RETURNED))
    grouped = defaultdict(lambda: [0, 0, 0, 0])
    for order in queryset:
        key = (str(order.agency), order.delivery_type or order.marketplace or "-")
        row = grouped[key]
        row[0] += 1
        if order.status in (WmsNewOrder.STATUS_NEW, WmsNewOrder.STATUS_AWAITING_STOCK, WmsNewOrder.STATUS_RESERVED, WmsNewOrder.STATUS_QUEUED, WmsNewOrder.STATUS_PICKING, WmsNewOrder.STATUS_EXCEPTION):
            row[1] += 1
        elif order.status == WmsNewOrder.STATUS_PICKED:
            row[2] += 1
        else:
            row[3] += 1
    rows = [(key[0], key[1], *values) for key, values in sorted(grouped.items())]
    return ("Партнер", "Доставка", "Всего", "Не собрано", "Собрано", "Готовы к отгрузке"), rows


def _order_count_rows(params):
    start, end = _common_dates(params)
    queryset = WmsNewOrder.objects.select_related("agency").filter(
        status__in=(WmsNewOrder.STATUS_HANDED_OVER, WmsNewOrder.STATUS_DONE),
        source_updated_at__date__range=(start, end),
    )
    queryset = _partner_filter(queryset, params.get("partner"))
    grouped = defaultdict(lambda: [0, 0, 0])
    for order in queryset:
        day = (order.source_updated_at or order.updated_at).date().isoformat()
        key = (str(order.agency), day)
        market = f"{order.marketplace} {order.delivery_type}".lower()
        if "ozon" in market or "озон" in market:
            grouped[key][1] += 1
        elif "yandex" in market or "яндекс" in market:
            grouped[key][2] += 1
        else:
            grouped[key][0] += 1
    rows = [(partner, day, counts[0], counts[1], counts[2], sum(counts)) for (partner, day), counts in sorted(grouped.items())]
    return ("Партнер", "Дата", "Wildberries", "OZON", "Yandex.Market", "Всего"), rows


def _fbs_goods_rows(params):
    start, end = _common_dates(params)
    queryset = WmsNewOrder.objects.select_related("agency").prefetch_related("items").filter(
        status__in=(WmsNewOrder.STATUS_HANDED_OVER, WmsNewOrder.STATUS_DONE),
        source_updated_at__date__range=(start, end),
    )
    queryset = _partner_filter(queryset, params.get("partner"))
    by_date = str(params.get("group_by_date") or "1") == "1"
    grouped = defaultdict(lambda: {"barcodes": set(), "counts": [0, 0, 0]})
    for order in queryset:
        day = (order.source_updated_at or order.updated_at).date().isoformat() if by_date else "Весь период"
        market = f"{order.marketplace} {order.delivery_type}".lower()
        index = 1 if "ozon" in market or "озон" in market else 2 if "yandex" in market or "яндекс" in market else 0
        for item in order.items.all():
            key = (day, str(order.agency), item.product_name, item.external_sku)
            grouped[key]["counts"][index] += item.quantity
            if item.barcode:
                grouped[key]["barcodes"].add(item.barcode)
    rows = [(day, partner, name, article, ", ".join(sorted(value["barcodes"])) or "-", *value["counts"], sum(value["counts"])) for (day, partner, name, article), value in sorted(grouped.items())]
    return ("Дата", "Партнер", "Товар", "Артикул", "Штрихкоды", "Wildberries", "OZON", "Yandex.Market", "Всего"), rows


def _invoice_rows(params):
    start, end = _common_dates(params)
    queryset = ClientInvoice.objects.select_related("client").order_by("-invoice_date", "-id")
    partner = str(params.get("partner") or "")
    if partner.isdigit():
        queryset = queryset.filter(client_id=int(partner))
    invoice_type = str(params.get("invoice_type") or "finished")
    if invoice_type == "draft":
        queryset = queryset.filter(status="draft")
    elif invoice_type == "finished":
        queryset = queryset.exclude(status="draft")
    period_type = str(params.get("period_type") or "created")
    field = "paid_at__date" if period_type == "paid" else "due_date" if period_type == "due" else "invoice_date"
    queryset = queryset.filter(**{f"{field}__range": (start, end)})
    rows = [(str(item.client), item.number, item.invoice_date, item.due_date, item.total_amount, item.paid_at or "-", item.paid_amount, item.get_status_display()) for item in queryset]
    return ("Партнер", "Номер счета", "Дата создания", "Крайний срок оплаты", "Сумма счета", "Дата платежа", "Сумма платежа", "Статус счета"), rows


def _grouped_report_rows(slug: str, rows) -> list[dict]:
    if slug == "reports-fbs-status":
        by_partner = defaultdict(list)
        by_delivery = defaultdict(lambda: [0, 0, 0, 0])
        for partner, delivery, total, unpicked, picked, ready in rows:
            values = (total, unpicked, picked, ready)
            by_partner[partner].append((delivery, *values))
            for index, value in enumerate(values):
                by_delivery[delivery][index] += value

        def status_group(title, detail_rows):
            totals = [sum(row[index] for row in detail_rows) for index in range(1, 5)]
            return {
                "title": title,
                "columns": ("", "Всего", "Не собрано", "Собрано", "Готовы к отгрузке"),
                "rows": [("Всего", *totals), *sorted(detail_rows)],
            }

        groups = []
        if rows:
            groups.append(
                status_group(
                    "Все партнеры",
                    [(delivery, *values) for delivery, values in by_delivery.items()],
                )
            )
        groups.extend(status_group(partner, detail) for partner, detail in sorted(by_partner.items()))
        return groups

    if slug in {"reports-fbs-count", "reports-fbs-orders"}:
        by_partner = defaultdict(list)
        for partner, day, wildberries, ozon, yandex, total in rows:
            by_partner[partner].append((day, wildberries, ozon, yandex, total))
        groups = []
        for partner, detail in sorted(by_partner.items()):
            totals = [sum(row[index] for row in detail) for index in range(1, 5)]
            groups.append(
                {
                    "title": partner,
                    "columns": ("Дата", "Wildberries", "OZON", "Yandex.Market", "ВСЕГО"),
                    "rows": [*sorted(detail), ("ВСЕГО", *totals)],
                }
            )
        return groups
    return []


def build_report(slug: str, params, *, limit: int | None = 1000) -> dict:
    title, result_title = REPORT_TITLES.get(slug, ("Отчет", "Отчет"))
    builders = {
        "reports-goods": _goods_rows,
        "reports-goods-places": _goods_places_rows,
        "reports-places": _occupied_places_rows,
        "reports-returns": _return_rows,
        "reports-shipments": _shipment_rows,
        "reports-tasks": _task_rows,
        "reports-fbs-status": _status_rows,
        "reports-fbs-count": _order_count_rows,
        "reports-fbs-orders": _order_count_rows,
        "reports-fbs-goods": _fbs_goods_rows,
        "reports-invoices": _invoice_rows,
    }
    generated = str(params.get("run") or "") == "1"
    columns, all_rows = builders[slug](params) if generated else ((), [])
    total_rows = len(all_rows)
    rows = all_rows if limit is None else all_rows[:limit]
    return {
        "slug": slug,
        "title": title,
        "result_title": result_title,
        "filter_fields": report_filter_fields(slug, params),
        "columns": columns,
        "rows": rows,
        "groups": _grouped_report_rows(slug, rows),
        "generated": generated,
        "total_rows": total_rows,
        "truncated": limit is not None and total_rows > limit,
    }
