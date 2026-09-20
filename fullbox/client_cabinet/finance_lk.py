"""Client LK finance documents + billing aggregates."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Sum
from django.utils import timezone

from audit.models import OrderAuditEntry

from .models import ClientFinanceDocument, ClientServiceCharge


SERVICE_LABELS = {
    ClientServiceCharge.TYPE_STORAGE: "Хранение",
    ClientServiceCharge.TYPE_RECEIVING: "Приёмка",
    ClientServiceCharge.TYPE_SHIPPING: "Отгрузка",
    ClientServiceCharge.TYPE_EXTRA: "Доп. услуги",
}

DOC_KIND_LABELS = {
    ClientFinanceDocument.KIND_INVOICE: "Счёт",
    ClientFinanceDocument.KIND_UPD: "УПД",
    ClientFinanceDocument.KIND_ACT: "Акт",
}


def _period_from_date(value) -> str:
    if not value:
        return timezone.localdate().strftime("%Y-%m")
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m")
    text = str(value)[:7]
    return text if len(text) == 7 else timezone.localdate().strftime("%Y-%m")


def _fmt_date(value) -> str:
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y")
    return str(value)[:10]


def _fmt_money(value) -> float:
    try:
        return float(Decimal(value or 0))
    except Exception:
        return 0.0


def _client_suffix(agency) -> str:
    return f"?client={agency.id}" if agency else ""


def _receiving_act_status_from_payloads(payloads: list[dict[str, Any]]) -> tuple[str, str]:
    """Resolve client-facing status for a sent receiving act."""
    response = ""
    viewed = False
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        raw_response = str(payload.get("act_client_response") or "").strip().lower()
        if raw_response:
            response = raw_response
        if payload.get("act_viewed"):
            viewed = True
    if response == "confirmed":
        return "confirmed", "Подтверждён клиентом"
    if response == "dispute":
        return "dispute", "Разногласия по акту"
    if viewed:
        return "viewed", "Выполнена"
    return "sent", "Отправлен клиенту"


def _shipping_act_status(order, stage: dict[str, Any] | None = None) -> tuple[str, str]:
    """Resolve act document status (not shipping order status)."""
    stage = stage or {}
    if stage.get("act_sent") or stage.get("manager_signed"):
        return "sent", "Отправлен клиенту"
    if stage.get("logistician_signed"):
        return "signed_logistician", "Ожидает подписи менеджера"
    status = str(getattr(order, "status", "") or "").strip().lower()
    if status in {"shipped", "partial", "partial_shipped"}:
        # Goods left the warehouse, but formal dispatch act was not sent to the client.
        return "not_sent", "Акт не отправлен"
    if status == "packed":
        return "ready", "Готов к оформлению"
    return "pending", "Формируется"


def sync_shipping_transport_charges(agency) -> int:
    """Upsert billing lines from shipping transport notes with service_cost."""
    from shipping.models import ShippingTransportNote

    created = 0
    qs = (
        ShippingTransportNote.objects.filter(order__agency=agency, service_cost__isnull=False)
        .exclude(service_cost=0)
        .select_related("order")
        .order_by("-updated_at")[:200]
    )
    for note in qs:
        order = note.order
        amount = Decimal(note.service_cost or 0)
        if amount <= 0:
            continue
        source_key = f"shipping-tn:{note.id}"
        charged = note.updated_at.date() if note.updated_at else timezone.localdate()
        period = _period_from_date(charged)
        obj, was_created = ClientServiceCharge.objects.get_or_create(
            agency=agency,
            source_key=source_key,
            defaults={
                "service_type": ClientServiceCharge.TYPE_SHIPPING,
                "description": f"Транспортные услуги · ТН {note.document_number or note.id}",
                "amount": amount,
                "period": period,
                "charged_at": charged,
                "status": ClientServiceCharge.STATUS_OPEN,
                "order_type": "shipping",
                "order_id": str(order.number if order else note.id),
            },
        )
        if was_created:
            created += 1
        else:
            # Keep amount in sync if transport note cost changed.
            if obj.amount != amount:
                obj.amount = amount
                obj.description = f"Транспортные услуги · ТН {note.document_number or note.id}"
                obj.period = period
                obj.charged_at = charged
                obj.save(update_fields=["amount", "description", "period", "charged_at", "updated_at"])
    return created


def collect_warehouse_act_documents(agency) -> list[dict[str, Any]]:
    """Build document rows from sent receiving acts and shipping acts."""
    from .lk_requests import lk_request_hash

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    suffix = _client_suffix(agency)

    receiving_payloads: dict[str, list[dict[str, Any]]] = {}
    receiving_meta: dict[str, dict[str, Any]] = {}
    # Newest-first scan to discover acts with client response, then load full history per order.
    candidate_ids: list[str] = []
    for order_id, created_at, act_sent, client_response_raw in (
        OrderAuditEntry.objects.filter(agency=agency, order_type="receiving")
        .values_list(
            "order_id",
            "created_at",
            "payload__act_sent",
            "payload__act_client_response",
        )
        .order_by("-created_at")[:600]
    ):
        client_response = str(client_response_raw or "").strip().lower()
        if not act_sent and client_response != "confirmed":
            continue
        order_id = str(order_id)
        if order_id in receiving_meta:
            continue
        receiving_meta[order_id] = {
            "title": str(act_sent or f"Акт приёмки №{order_id}"),
            "issued": created_at.date() if created_at else timezone.localdate(),
        }
        candidate_ids.append(order_id)
        if len(candidate_ids) >= 120:
            break

    if candidate_ids:
        for order_id, created_at, act_sent, client_response, act_viewed in (
            OrderAuditEntry.objects.filter(
                agency=agency,
                order_type="receiving",
                order_id__in=candidate_ids,
            )
            .values_list(
                "order_id",
                "created_at",
                "payload__act_sent",
                "payload__act_client_response",
                "payload__act_viewed",
            )
            .order_by("created_at")
        ):
            payload = {
                "act_sent": act_sent,
                "act_client_response": client_response,
                "act_viewed": act_viewed,
            }
            order_id = str(order_id)
            receiving_payloads.setdefault(order_id, []).append(payload)
            if act_sent and created_at:
                # Prefer earliest send date for finance chronology.
                current = receiving_meta.get(order_id)
                if current is not None:
                    issued = created_at.date()
                    if issued < current["issued"]:
                        current["issued"] = issued
                    if not current.get("title"):
                        current["title"] = str(act_sent or f"Акт приёмки №{order_id}")

    for order_id in candidate_ids:
        meta = receiving_meta.get(order_id)
        if not meta:
            continue
        key = ("receiving", order_id)
        if key in seen:
            continue
        seen.add(key)
        status, status_label = _receiving_act_status_from_payloads(receiving_payloads.get(order_id) or [])
        if status != "confirmed":
            continue
        issued = meta["issued"]
        lk_url = lk_request_hash("receiving", order_id)
        print_url = f"/orders/receiving/{order_id}/act/print/{suffix}"
        rows.append(
            {
                "id": f"act-receiving-{order_id}",
                "title": meta["title"],
                "subtitle": f"Заявка на приёмку №{order_id}",
                "date": _fmt_date(issued),
                "issued_at": issued.isoformat(),
                "doc_kind": "act",
                "type": "act",
                "category": "receiving",
                "order_id": order_id,
                "order_type": "receiving",
                "period_id": _period_from_date(issued),
                "amount": None,
                "status": status,
                "status_label": status_label,
                "detail_url": lk_url,
                "pdf_url": print_url,
                "url": lk_url,
                "print_url": print_url,
                "source": "warehouse",
            }
        )

    try:
        from shipping.models import ShippingOrder
    except Exception:
        ShippingOrder = None  # type: ignore

    if ShippingOrder is not None:
        shipping_lk_fields = (
            "id",
            "agency_id",
            "number",
            "status",
            "created_at",
            "updated_at",
            "vehicle_type",
            "vehicle_number",
            "driver_phone",
            "destination_warehouse",
            "destination_address",
            "slot_date",
            "slot_time",
            "eta_at",
            "planned_ship_date",
        )
        shipping_orders = list(
            ShippingOrder.objects.filter(agency=agency)
            .filter(
                status__in={
                    ShippingOrder.STATUS_PACKED,
                    ShippingOrder.STATUS_SHIPPED,
                    ShippingOrder.STATUS_PARTIAL,
                }
            )
            .only(*shipping_lk_fields)
            .prefetch_related("items")
            .order_by("-updated_at")[:80]
        )
        shipping_act_by_number = {}
        shipping_numbers = [str(order.number) for order in shipping_orders]
        if shipping_numbers:
            for entry in (
                OrderAuditEntry.objects.filter(
                    order_id__in=shipping_numbers,
                    order_type="shipping",
                    payload__act="shipping_dispatch_act",
                )
                .only("id", "order_id", "payload", "created_at")
                .order_by("order_id", "-created_at", "-id")
            ):
                shipping_act_by_number.setdefault(str(entry.order_id), entry)
        for order in shipping_orders:
            key = ("shipping", str(order.pk))
            if key in seen:
                continue
            seen.add(key)
            act_entry = shipping_act_by_number.get(str(order.number))
            payload = act_entry.payload if act_entry and isinstance(act_entry.payload, dict) else {}
            stage = {
                "act_sent": bool(payload.get("act_sent")),
                "manager_signed": bool(payload.get("act_manager_signed")),
                "logistician_signed": bool(payload.get("act_logistician_signed")),
                "payload": payload,
            }
            status, status_label = _shipping_act_status(order, stage)
            if status != "sent":
                continue
            issued = (order.updated_at or order.created_at or timezone.now()).date()
            if isinstance(payload, dict) and payload.get("act_sent_at"):
                try:
                    from django.utils.dateparse import parse_datetime

                    parsed = parse_datetime(str(payload.get("act_sent_at")))
                    if parsed is not None:
                        issued = timezone.localtime(parsed).date() if timezone.is_aware(parsed) else parsed.date()
                except Exception:
                    pass
            lk_url = lk_request_hash("shipping", order.number)
            act_url = f"/shipping/{order.pk}/act/{suffix}"
            rows.append(
                {
                    "id": f"act-shipping-{order.pk}",
                    "title": f"Акт отгрузки №{order.number}",
                    "subtitle": f"Заявка на отгрузку №{order.number}",
                    "date": _fmt_date(issued),
                    "issued_at": issued.isoformat(),
                    "doc_kind": "act",
                    "type": "act",
                    "category": "shipping",
                    "order_id": str(order.number),
                    "order_type": "shipping",
                    "period_id": _period_from_date(issued),
                    "amount": None,
                    "status": status,
                    "status_label": status_label,
                    "detail_url": lk_url,
                    "pdf_url": act_url,
                    "url": lk_url,
                    "print_url": act_url,
                    "source": "warehouse",
                }
            )

    return rows


def list_registered_finance_documents(agency) -> list[dict[str, Any]]:
    suffix = _client_suffix(agency)
    rows = []
    for doc in ClientFinanceDocument.objects.filter(agency=agency).order_by("-issued_at", "-id")[:120]:
        file_url = ""
        if doc.file:
            file_url = f"/client/api/v1/finance/documents/{doc.id}/file/{suffix}"
        detail = doc.external_url or file_url or f"/client/dashboard/lk/{suffix}#/finance"
        rows.append(
            {
                "id": f"fin-{doc.id}",
                "title": doc.title or f"{DOC_KIND_LABELS.get(doc.doc_kind, 'Документ')} {doc.number}".strip(),
                "subtitle": doc.number or DOC_KIND_LABELS.get(doc.doc_kind, "Документ"),
                "date": _fmt_date(doc.issued_at),
                "issued_at": doc.issued_at.isoformat() if doc.issued_at else "",
                "doc_kind": doc.doc_kind,
                "type": doc.doc_kind,
                "category": "finance",
                "order_id": doc.order_id or None,
                "order_type": doc.order_type or None,
                "period_id": doc.period or _period_from_date(doc.issued_at),
                "amount": _fmt_money(doc.amount),
                "status": doc.status,
                "status_label": doc.get_status_display(),
                "detail_url": detail,
                "pdf_url": file_url or doc.external_url or "",
                "url": detail,
                "source": "registry",
            }
        )
    return rows


def list_contract_documents(agency) -> list[dict[str, Any]]:
    link = str(getattr(agency, "contract_link", "") or "").strip()
    number = str(getattr(agency, "contract_numb", "") or "").strip()
    if not link and not number:
        return []
    return [
        {
            "id": f"contract-{agency.id}",
            "title": f"Договор {number}".strip() if number else "Договор с FullBox",
            "subtitle": "Реквизиты клиента",
            "date": "",
            "issued_at": "",
            "doc_kind": "contract",
            "type": "contract",
            "category": "finance",
            "order_id": None,
            "order_type": None,
            "period_id": "",
            "amount": None,
            "status": "issued",
            "status_label": "Действует" if getattr(agency, "sign_oferta", False) else "В карточке клиента",
            "detail_url": link or f"/client/{agency.id}/edit/",
            "pdf_url": link,
            "url": link or f"/client/{agency.id}/edit/",
            "source": "contract",
        }
    ]


def list_billing_act_documents(agency) -> list[dict[str, Any]]:
    if not agency:
        return []
    try:
        from billing.models import BillingAct
        from billing.statuses import ActStatus
    except ImportError:
        return []
    rows = []
    qs = BillingAct.objects.filter(
        client=agency,
        status__in=[ActStatus.SENT, ActStatus.DISPUTED, ActStatus.CONFIRMED],
    ).select_related("application").order_by("-sent_at", "-id")[:100]
    for act in qs:
        rows.append(
            {
                "id": f"billing-act-{act.id}",
                "title": f"Billing-акт №{act.number}",
                "subtitle": f"{act.application.get_application_type_display()} {act.application.application_id}",
                "date": _fmt_date(act.act_date),
                "issued_at": act.sent_at.isoformat() if act.sent_at else "",
                "doc_kind": "act",
                "type": "billing_act",
                "category": "finance",
                "order_id": act.application.application_id,
                "order_type": act.application.application_type,
                "period_id": act.act_date.strftime("%Y-%m") if act.act_date else "",
                "amount": _fmt_money(act.total_amount),
                "status": act.status,
                "status_label": act.get_status_display(),
                "detail_url": "",
                "pdf_url": act.file.url if act.file else "",
                "url": act.file.url if act.file else "",
                "confirm_url": f"/client/api/v1/billing/acts/{act.id}/confirm/",
                "dispute_url": f"/client/api/v1/billing/acts/{act.id}/dispute/",
                "source": "billing",
            }
        )
    return rows


def list_billing_invoice_documents(agency) -> list[dict[str, Any]]:
    if not agency:
        return []
    try:
        from billing.models import ClientInvoice
        from billing.statuses import InvoiceStatus
    except ImportError:
        return []
    rows = []
    qs = (
        ClientInvoice.objects.filter(client=agency)
        .exclude(status__in=[InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED])
        .select_related("application", "act")
        .order_by("-invoice_date", "-id")[:100]
    )
    for invoice in qs:
        print_url = f"/team-manager/billing/invoices/{invoice.id}/print/"
        rows.append(
            {
                "id": f"billing-invoice-{invoice.id}",
                "title": f"Счёт на оплату №{invoice.number}",
                "subtitle": f"от {_fmt_date(invoice.invoice_date)} · {invoice.get_status_display()}",
                "date": _fmt_date(invoice.invoice_date),
                "issued_at": invoice.sent_at.isoformat() if invoice.sent_at else (invoice.invoice_date.isoformat() if invoice.invoice_date else ""),
                "doc_kind": "invoice",
                "type": "billing_invoice",
                "category": "finance",
                "order_id": invoice.application.application_id if invoice.application_id else None,
                "order_type": invoice.application.application_type if invoice.application_id else None,
                "period_id": invoice.billing_period.strftime("%Y-%m") if invoice.billing_period else "",
                "amount": _fmt_money(invoice.total_amount),
                "status": invoice.status,
                "status_label": invoice.get_status_display(),
                "detail_url": print_url,
                "pdf_url": print_url,
                "print_url": print_url,
                "url": print_url,
                "source": "billing",
            }
        )
    return rows


def list_finance_documents(agency) -> list[dict[str, Any]]:
    if not agency:
        return []
    rows = []
    rows.extend(list_registered_finance_documents(agency))
    rows.extend(list_billing_invoice_documents(agency))
    rows.extend(list_billing_act_documents(agency))
    rows.extend(collect_warehouse_act_documents(agency))
    rows.extend(list_contract_documents(agency))
    rows.sort(key=lambda item: item.get("issued_at") or item.get("date") or "", reverse=True)
    return rows


def list_billing_months(agency) -> list[dict[str, str]]:
    if not agency:
        return []
    periods = set(
        ClientServiceCharge.objects.filter(agency=agency)
        .exclude(status=ClientServiceCharge.STATUS_CANCELLED)
        .exclude(period="")
        .values_list("period", flat=True)
    )
    try:
        from billing.models import ApplicationCharge

        for day in ApplicationCharge.objects.filter(client=agency).values_list("billing_period", flat=True).distinct():
            if day:
                periods.add(day.strftime("%Y-%m"))
    except Exception:
        pass
    today = timezone.localdate()
    periods.add(today.strftime("%Y-%m"))
    months = []
    for period in sorted(periods, reverse=True):
        try:
            year, month = period.split("-")
            label = date(int(year), int(month), 1).strftime("%B %Y")
            # Russian-friendly short label without relying on locale
            month_names = {
                1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
                5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
                9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
            }
            label = f"{month_names.get(int(month), month)} {year}"
        except Exception:
            label = period
        months.append({"id": period, "label": label})
    return months


def _application_type_to_service(app_type: str) -> str:
    mapping = {
        "storage": ClientServiceCharge.TYPE_STORAGE,
        "receiving": ClientServiceCharge.TYPE_RECEIVING,
        "shipping": ClientServiceCharge.TYPE_SHIPPING,
        "processing": ClientServiceCharge.TYPE_EXTRA,
        "packing": ClientServiceCharge.TYPE_EXTRA,
    }
    return mapping.get(app_type or "", ClientServiceCharge.TYPE_EXTRA)


def _client_has_published_tariff(agency) -> bool:
    try:
        from billing.tariff_services import active_tariff_version

        return active_tariff_version(agency) is not None
    except Exception:
        return False


def _client_has_issued_billing_docs(agency) -> bool:
    """Есть ли у клиента выставленные/отправленные финансовые документы биллинга."""
    try:
        from billing.models import BillingAct, ClientInvoice
        from billing.statuses import ActStatus, InvoiceStatus

        has_invoice = (
            ClientInvoice.objects.filter(client=agency)
            .exclude(status__in=[InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED])
            .exists()
        )
        if has_invoice:
            return True
        return (
            BillingAct.objects.filter(client=agency)
            .exclude(status__in=[ActStatus.DRAFT, ActStatus.CANCELLED])
            .exists()
        )
    except Exception:
        return False


def build_billing_payload(agency, *, period_id: str | None = None) -> dict[str, Any] | None:
    if not agency:
        return None
    from django.db.models import Q

    from billing.charge_status import charge_has_agreed_price
    from billing.models import ApplicationCharge

    # В ЛК клиента — только строки из отправленного акта / выставленного счёта.
    # Черновики, default_price каталога и авто-транспорт без документов не показываем.
    months = list_billing_months(agency)
    period = (period_id or "").strip() or (months[0]["id"] if months else timezone.localdate().strftime("%Y-%m"))
    services: list[dict[str, Any]] = []
    breakdown = {
        "storage": 0.0,
        "receiving": 0.0,
        "shipping": 0.0,
        "extra": 0.0,
    }
    app_charge_total = Decimal("0")
    has_issued_docs = _client_has_issued_billing_docs(agency)
    has_published_tariff = _client_has_published_tariff(agency)

    if has_issued_docs:
        try:
            year, month = [int(x) for x in period.split("-")]
            period_start = date(year, month, 1)
            if month == 12:
                period_end = date(year + 1, 1, 1)
            else:
                period_end = date(year, month + 1, 1)
            app_charges = (
                ApplicationCharge.objects.filter(
                    client=agency,
                    billing_period__gte=period_start,
                    billing_period__lt=period_end,
                )
                .filter(Q(is_included_in_invoice=True) | Q(is_included_in_act=True))
                .select_related("service", "application", "client_tariff_version")
                .order_by("-performed_at", "-id")
            )
            for charge in app_charges:
                if not charge_has_agreed_price(charge):
                    continue
                amount = _fmt_money(charge.total_amount)
                app_charge_total += Decimal(str(charge.total_amount or 0))
                stype = _application_type_to_service(charge.application.application_type)
                breakdown[stype] = round(breakdown.get(stype, 0.0) + amount, 2)
                if charge.is_included_in_invoice:
                    status_label = "В счёте"
                else:
                    status_label = "В акте"
                services.append(
                    {
                        "id": f"app-charge-{charge.id}",
                        "date": _fmt_date(charge.performed_at or charge.billing_period),
                        "service_type": stype,
                        "service_label": charge.service_name_snapshot or charge.service.name,
                        "description": (
                            f"Заявка №{charge.application.application_id} · "
                            f"{charge.quantity} {charge.unit} × {charge.tariff}"
                        ),
                        "order_id": charge.application.application_id,
                        "order_type": charge.application.application_type,
                        "amount": amount,
                        "quantity": str(charge.quantity),
                        "unit": charge.unit,
                        "tariff": str(charge.tariff),
                        "tariff_source": charge.tariff_source_label or "",
                        "category": stype,
                        "billable_kind": stype,
                        "status": "open",
                        "status_label": status_label,
                        "detail_url": "#/billing",
                        "source": "agreed_tariff",
                        "read_only": True,
                    }
                )
        except Exception:
            app_charge_total = Decimal("0")
            services = []
            breakdown = {"storage": 0.0, "receiving": 0.0, "shipping": 0.0, "extra": 0.0}

    total_due = round(sum(breakdown.values()), 2)
    try:
        year, month = [int(x) for x in period.split("-")]
        if month == 12:
            due = date(year + 1, 1, 15)
        else:
            due = date(year, month + 1, 15)
    except Exception:
        due = timezone.localdate() + timedelta(days=15)
    due_days_left = (due - timezone.localdate()).days
    period_label = next((m["label"] for m in months if m["id"] == period), period)
    if not has_published_tariff and not has_issued_docs:
        tariff_note = (
            "Тарификация ещё не опубликована бухгалтером. "
            "Начисления появятся после настройки тарифа и выставления документов."
        )
    elif not has_issued_docs:
        tariff_note = (
            "Черновые начисления склада клиенту не показываются. "
            "Суммы появятся после отправки акта или выставления счёта."
        )
    else:
        tariff_note = (
            "Цены только из согласованных тарифов клиента. "
            "Детализация read-only; подтверждение актов — в разделе Финансы."
        )
    return {
        "period_id": period,
        "period_label": period_label,
        "total_due": total_due,
        "due_date": due.isoformat(),
        "due_days_left": due_days_left,
        "has_debt": app_charge_total > 0,
        "breakdown": breakdown,
        "services": services,
        "tariff_note": tariff_note,
        "services_count": len(services),
        "open_amount": _fmt_money(app_charge_total),
        "billing_acts": list_billing_act_documents(agency) if has_issued_docs else [],
        "tariff_published": has_published_tariff,
        "documents_issued": has_issued_docs,
    }


def build_finance_summary(agency) -> dict[str, Any]:
    documents = list_finance_documents(agency)
    billing = build_billing_payload(agency)
    counts = {"invoice": 0, "upd": 0, "act": 0, "all": len(documents)}
    for doc in documents:
        kind = str(doc.get("doc_kind") or "")
        if kind in counts:
            counts[kind] += 1
    has_issued = bool((billing or {}).get("documents_issued"))
    # Раздел биллинга в ЛК доступен, но суммы/строки — только после документов.
    billing_available = True
    return {
        "documents": documents,
        "document_counts": counts,
        "billing": billing,
        "billing_available": billing_available,
        "billing_has_amounts": has_issued and bool((billing or {}).get("services")),
        "months": list_billing_months(agency),
        "recent_documents": documents[:5],
        "recent_services": (billing or {}).get("services", [])[:5],
    }


def build_storage_billing_payload(agency, *, period_id: str | None = None, day: str | None = None) -> dict[str, Any] | None:
    """Детализация хранения для ЛК клиента (read-only)."""
    if not agency:
        return None
    from billing.models import BillingStorageDay
    from billing.storage_serializers import storage_day_dict

    months = list_billing_months(agency)
    period = (period_id or "").strip() or (months[0]["id"] if months else timezone.localdate().strftime("%Y-%m"))
    try:
        year, month = [int(x) for x in period.split("-")]
    except Exception:
        today = timezone.localdate()
        year, month = today.year, today.month
        period = f"{year:04d}-{month:02d}"

    qs = (
        BillingStorageDay.objects.filter(client=agency, day__year=year, day__month=month)
        .select_related("charge")
        .prefetch_related("lines")
        .order_by("day")
    )
    days = []
    total_amount = Decimal("0")
    total_l = Decimal("0")
    total_pallets = 0
    for row in qs:
        days.append(storage_day_dict(row, with_lines=False))
        total_amount += Decimal(str(row.amount or 0))
        total_l += Decimal(str(row.billable_volume_l or 0))
        total_pallets += int(row.pallet_count or 0)

    detail = None
    if day:
        detail_row = qs.filter(day=day).first()
        if detail_row:
            detail = storage_day_dict(detail_row, with_lines=True)

    return {
        "period_id": period,
        "period_label": next((m["label"] for m in months if m["id"] == period), period),
        "months": months,
        "days": days,
        "totals": {
            "amount": _fmt_money(total_amount),
            "billable_volume_l": str(total_l),
            "pallet_count": total_pallets,
            "days_count": len(days),
        },
        "day_detail": detail,
        "read_only": True,
    }
