"""Сервис биллинга менеджера: портфель клиентов, сводка, внимание (этап 2)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Count, Q, QuerySet, Sum
from django.utils import timezone

from sku.models import Agency

from .models import (
    ApplicationCharge,
    BillingAct,
    BillingApplication,
    BillingDiscrepancy,
    ClientBillingContract,
    ClientInvoice,
    ClientTariffItem,
    ClientTariffVersion,
    WarehouseServiceFact,
)
from .permissions import (
    _client_is_managers,
    _client_unassigned,
    filter_agencies_for_user,
    get_billing_role,
    get_employee,
)
from .statuses import ActStatus, BillingStatus, DiscrepancyStatus, DocumentReviewStatus, InvoiceStatus
from .tariff_services import active_tariff_version, tariff_history


@dataclass(frozen=True)
class AttentionItem:
    client_id: int
    client_name: str
    problem_type: str
    title: str
    href: str
    priority: str = "warn"
    amount: Decimal | None = None
    due: str = ""
    owner: str = ""


def get_manager_clients(user_or_request, *, q: str = "") -> QuerySet:
    qs = filter_agencies_for_user(
        Agency.objects.select_related("lifecycle", "lifecycle__serving_company").filter(archived=False),
        user_or_request,
    )
    if q:
        qs = qs.filter(
            Q(agn_name__icontains=q)
            | Q(short_name__icontains=q)
            | Q(inn__icontains=q)
        )
    return qs.order_by("agn_name", "id")


def can_access_client(user_or_request, client: Agency | None) -> bool:
    if not client:
        return False
    role = get_billing_role(user_or_request)
    if role in {"head_manager", "director", "admin", "accountant"}:
        return True
    lifecycle = getattr(client, "lifecycle", None)
    if lifecycle is None:
        try:
            lifecycle = client.lifecycle
        except Exception:
            lifecycle = None
    if getattr(client, "archived", False) or not lifecycle or getattr(lifecycle, "status", "") != "active":
        return False
    employee = get_employee(user_or_request)
    if role == "manager" and employee:
        return _client_is_managers(client, employee) or _client_unassigned(client)
    return False


def active_contract_for_client(client: Agency, on_date=None):
    on_date = on_date or timezone.localdate()
    return (
        ClientBillingContract.objects.select_related("own_company")
        .filter(client=client, is_active=True, valid_from__lte=on_date)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on_date))
        .order_by("-valid_from", "-id")
        .first()
    )


def get_client_billing_summary(client: Agency) -> dict:
    today = timezone.localdate()
    lifecycle = getattr(client, "lifecycle", None)
    if lifecycle is None:
        try:
            lifecycle = client.lifecycle
        except Exception:
            lifecycle = None
    contract = active_contract_for_client(client, today)
    tariff = active_tariff_version(client, today)
    invoices = ClientInvoice.objects.filter(client=client).exclude(status=InvoiceStatus.CANCELLED)
    unpaid = invoices.filter(
        status__in=[InvoiceStatus.ISSUED, InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.REQUIRED]
    ).exclude(debt_amount=0)
    overdue = unpaid.filter(due_date__lt=today)
    accrued = (
        ApplicationCharge.objects.filter(client=client, application__isnull=False)
        .exclude(application__billing_status=BillingStatus.CANCELLED)
        .aggregate(total=Sum("total_amount"))
        .get("total")
        or Decimal("0")
    )
    zero_price = ClientTariffItem.objects.filter(
        tariff_version__client=client,
        tariff_version__status=ClientTariffVersion.STATUS_DRAFT,
        price=0,
    ).count()
    published_zero = ClientTariffItem.objects.filter(
        tariff_version__client=client,
        tariff_version__status__in=[ClientTariffVersion.STATUS_ACTIVE, ClientTariffVersion.STATUS_SCHEDULED],
        price=0,
        is_active=True,
    ).count()
    apps_ready = BillingApplication.objects.filter(
        client=client,
        billing_status__in=[BillingStatus.INVOICE_REQUIRED, BillingStatus.ACT_CONFIRMED],
    ).count()
    return {
        "client": client,
        "lifecycle": lifecycle,
        "contract": contract,
        "tariff": tariff,
        "tariff_history": list(tariff_history(client)[:30]),
        "serving_company": getattr(lifecycle, "serving_company", None) or (contract.own_company if contract else None),
        "contract_number": (getattr(client, "contract_numb", None) or "")
        or (getattr(tariff, "contract_number", None) or "")
        or "",
        "commercial_offer": getattr(lifecycle, "commercial_offer_number", None) or "",
        "contract_date": getattr(lifecycle, "contract_date", None) or getattr(tariff, "contract_date", None),
        "vat_type": getattr(tariff, "vat_type", None) or "",
        "accrued_total": accrued,
        "invoiced_total": invoices.aggregate(total=Sum("total_amount")).get("total") or Decimal("0"),
        "paid_total": invoices.aggregate(total=Sum("paid_amount")).get("total") or Decimal("0"),
        "debt_total": unpaid.aggregate(total=Sum("debt_amount")).get("total") or Decimal("0"),
        "overdue_total": overdue.aggregate(total=Sum("debt_amount")).get("total") or Decimal("0"),
        "overdue_count": overdue.count(),
        "apps_ready": apps_ready,
        "draft_zero_price": zero_price,
        "published_zero_price": published_zero,
        "last_payment_at": invoices.exclude(paid_at__isnull=True).order_by("-paid_at").values_list("paid_at", flat=True).first(),
        "applications_count": BillingApplication.objects.filter(client=client).count(),
        "charges_count": ApplicationCharge.objects.filter(client=client).count(),
    }


def client_setup_status(summary: dict) -> dict:
    """
    Состояние финансовой настройки клиента для списка «Клиенты и договоры».
    Один главный статус (без дублей в нескольких бейджах).
    """
    client = summary.get("client")
    inn = (getattr(client, "inn", None) or "").strip() if client is not None else ""
    contract_ok = bool(summary.get("contract") or (summary.get("contract_number") or "").strip())
    tariff = summary.get("tariff")
    debt = summary.get("overdue_total") or Decimal("0")

    if not inn:
        return {"code": "requisites", "label": "есть ошибка реквизитов", "css": "red"}
    if debt and debt > 0:
        return {"code": "debt", "label": "есть задолженность", "css": "red"}
    if not contract_ok:
        return {"code": "no_contract", "label": "отсутствует договор", "css": "orange"}
    if tariff is None:
        if not (summary.get("commercial_offer") or "").strip():
            return {"code": "no_price", "label": "отсутствует прайс", "css": "orange"}
        return {"code": "no_tariff", "label": "не настроены тарифы", "css": "orange"}
    if getattr(tariff, "status", None) == ClientTariffVersion.STATUS_DRAFT:
        return {"code": "confirm", "label": "требуется подтверждение изменений", "css": "yellow"}
    if summary.get("published_zero_price") or summary.get("draft_zero_price"):
        return {"code": "no_tariff", "label": "не настроены тарифы", "css": "orange"}
    return {"code": "ok", "label": "всё настроено", "css": "green"}


def build_overview_action_board(user_or_request, *, limit: int = 40) -> dict:
    """
    Карточки-фильтры и единая таблица «Требуют действий» для обзора биллинга.
    Переиспользует существующие статусы/бейджи без новых моделей.
    """
    from .permissions import filter_applications_for_user

    app_qs = filter_applications_for_user(BillingApplication.objects.all(), user_or_request).distinct()
    invoice_qs = ClientInvoice.objects.filter(application__in=app_qs).exclude(status=InvoiceStatus.CANCELLED)
    today = timezone.localdate()

    to_invoice = app_qs.filter(billing_status=BillingStatus.INVOICE_REQUIRED).count()
    charges_review = (
        ApplicationCharge.objects.filter(application__in=app_qs, is_confirmed=False)
        .exclude(application__billing_status=BillingStatus.CANCELLED)
        .count()
    )
    invoices_not_sent = invoice_qs.filter(
        status=InvoiceStatus.DRAFT,
    ).count()
    acts_not_formed = app_qs.filter(
        billing_status__in=[BillingStatus.ACT_DRAFT, BillingStatus.ACT_CONFIRMED]
    ).count()
    acts_waiting_sign = app_qs.filter(billing_status=BillingStatus.ACT_SENT).count()
    overdue_pay = invoice_qs.filter(debt_amount__gt=0, due_date__lt=today).count()
    tariff_errors = ApplicationCharge.objects.filter(
        application__in=app_qs,
        client_tariff_version__isnull=True,
        is_manual_override=False,
    ).count()

    cards = [
        {
            "key": "to_invoice",
            "label": "К выставлению",
            "count": to_invoice,
            "href": "/team-manager/billing/requests/",
            "tone": "orange" if to_invoice else "neutral",
        },
        {
            "key": "charges_review",
            "label": "Начисления не проверены",
            "count": charges_review,
            "href": "/team-manager/billing/charges/?unconfirmed=1",
            "tone": "orange" if charges_review else "neutral",
        },
        {
            "key": "invoices_not_sent",
            "label": "Счета не отправлены",
            "count": invoices_not_sent,
            "href": "/team-manager/billing/invoices/?status=draft",
            "tone": "blue" if invoices_not_sent else "neutral",
        },
        {
            "key": "acts_not_formed",
            "label": "Акты не сформированы",
            "count": acts_not_formed,
            "href": "/team-manager/billing/acts/",
            "tone": "blue" if acts_not_formed else "neutral",
        },
        {
            "key": "acts_waiting_sign",
            "label": "Акты ждут подписи",
            "count": acts_waiting_sign,
            "href": "/team-manager/billing/acts/?status=sent",
            "tone": "purple" if acts_waiting_sign else "neutral",
        },
        {
            "key": "overdue_pay",
            "label": "Просроченная оплата",
            "count": overdue_pay,
            "href": "/team-manager/billing/overdue/",
            "tone": "danger" if overdue_pay else "neutral",
        },
        {
            "key": "tariff_errors",
            "label": "Ошибки тарификации",
            "count": tariff_errors,
            "href": "/team-manager/billing/charges/?no_price=1",
            "tone": "danger" if tariff_errors else "neutral",
        },
    ]

    next_action_by_problem = {
        "no_contract": "проверить договор",
        "no_tariff": "исправить тариф",
        "no_price": "исправить тариф",
        "charge_no_price": "исправить тариф",
        "qty_unconfirmed": "проверить начисления",
        "overdue": "напомнить клиенту",
        "doc_returned": "исправить документ",
        "discrepancy": "проверить расхождение",
    }
    action_rows = []
    for item in get_attention_items(user_or_request, limit=limit):
        action_rows.append(
            {
                "client_name": item.client_name,
                "basis": item.title,
                "period_or_request": "—",
                "problem": item.title,
                "amount": item.amount,
                "due": item.due or "—",
                "owner": item.owner or "—",
                "next_action": next_action_by_problem.get(item.problem_type, "проверить"),
                "href": item.href,
                "priority": item.priority,
                "problem_type": item.problem_type,
            }
        )

    # Добавим явные строки «выставить счёт» по заявкам без дубля с attention.
    for app in (
        app_qs.filter(billing_status=BillingStatus.INVOICE_REQUIRED)
        .select_related("client", "manager")
        .order_by("-updated_at")[:15]
    ):
        action_rows.append(
            {
                "client_name": getattr(app.client, "short_name", None)
                or getattr(app.client, "agn_name", None)
                or f"#{app.client_id}",
                "basis": f"{app.get_application_type_display()} {app.application_id}",
                "period_or_request": str(app.application_id),
                "problem": "Нужно выставить счёт",
                "amount": app.charges_total,
                "due": "—",
                "owner": getattr(getattr(app, "manager", None), "full_name", None) or "—",
                "next_action": "выставить счёт",
                "href": f"/team-manager/billing/applications/{app.pk}/",
                "priority": "warn",
                "problem_type": "invoice_required",
            }
        )
    for app in (
        app_qs.filter(billing_status=BillingStatus.ACT_SENT)
        .select_related("client", "manager")
        .order_by("-updated_at")[:10]
    ):
        action_rows.append(
            {
                "client_name": getattr(app.client, "short_name", None)
                or getattr(app.client, "agn_name", None)
                or f"#{app.client_id}",
                "basis": f"{app.get_application_type_display()} {app.application_id}",
                "period_or_request": str(app.application_id),
                "problem": "Акт ждёт подписи клиента",
                "amount": app.charges_total,
                "due": "—",
                "owner": getattr(getattr(app, "manager", None), "full_name", None) or "—",
                "next_action": "получить подпись",
                "href": f"/team-manager/billing/applications/{app.pk}/",
                "priority": "warn",
                "problem_type": "act_waiting_sign",
            }
        )

    return {"cards": cards, "action_rows": action_rows[:limit]}


def _client_has_active_tariff(app) -> bool:
    try:
        from .tariff_services import active_tariff_version

        return active_tariff_version(app.client) is not None
    except Exception:
        return False


def _billing_action_from_app(
    app,
    *,
    missing_tariff: bool = False,
    unconfirmed: bool = False,
    has_active_tariff: bool | None = None,
) -> str:
    status = app.billing_status
    if status in {BillingStatus.CANCELLED, BillingStatus.FINANCIALLY_CLOSED}:
        return ""
    if status == BillingStatus.NOT_CALCULATED:
        return "Рассчитать начисления"
    if status == BillingStatus.ACT_DRAFT:
        return "Отправить акт клиенту"
    if status == BillingStatus.ACT_SENT:
        return "Получить подпись"
    if status == BillingStatus.ACT_DISPUTED:
        return "Исправить акт"
    if status in {BillingStatus.ACT_CONFIRMED, BillingStatus.INVOICE_REQUIRED}:
        return "Выставить счёт"
    if status in {BillingStatus.INVOICE_DRAFT, BillingStatus.INVOICE_ISSUED}:
        return "Отправить счёт"
    if status == BillingStatus.INVOICE_SENT:
        return "Проверить оплату"
    if status == BillingStatus.PARTIALLY_PAID:
        return "Проверить оплату"
    if status == BillingStatus.PAID:
        return "Закрыть заявку"
    if missing_tariff:
        # Тариф уже опубликован — менеджеру нужен пересчёт, а не страница тарифов
        active = has_active_tariff if has_active_tariff is not None else _client_has_active_tariff(app)
        if active:
            return "Рассчитать начисления"
        return "Исправить тариф"
    if status in {BillingStatus.CALCULATION_DRAFT, BillingStatus.CALCULATED}:
        if unconfirmed:
            return "Проверить начисления"
        return "Сформировать акт"
    if unconfirmed:
        return "Проверить начисления"
    return ""


def billing_next_actions_bulk(
    refs: list[tuple[str, str, int | None]],
) -> dict[tuple[str, str], str]:
    """
    Пакетное следующее фин. действие для строк задач.
    Ключ: (application_type, application_id).
    """
    cleaned: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for application_type, application_id, _client_id in refs:
        aid = str(application_id or "").strip()
        if not application_type or not aid:
            continue
        key = (str(application_type), aid)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(key)
    if not cleaned:
        return {}

    q = Q()
    for application_type, application_id in cleaned:
        q |= Q(application_type=application_type, application_id=application_id)
    apps = list(
        BillingApplication.objects.filter(q)
        .select_related("client")
        .order_by("-updated_at", "-id")
    )
    app_by_key: dict[tuple[str, str], BillingApplication] = {}
    for app in apps:
        key = (str(app.application_type), str(app.application_id))
        if key not in app_by_key:
            app_by_key[key] = app

    app_ids = [app.id for app in app_by_key.values()]
    missing_tariff_ids = set(
        ApplicationCharge.objects.filter(
            application_id__in=app_ids,
            client_tariff_version__isnull=True,
            is_manual_override=False,
        ).values_list("application_id", flat=True)
    )
    unconfirmed_ids = set(
        ApplicationCharge.objects.filter(application_id__in=app_ids, is_confirmed=False)
        .exclude(is_included_in_invoice=True)
        .values_list("application_id", flat=True)
    )
    client_ids = {app.client_id for app in app_by_key.values() if app.client_id}
    active_tariff_clients: set[int] = set()
    if client_ids:
        today = timezone.localdate()
        active_tariff_clients = set(
            ClientTariffVersion.objects.filter(
                client_id__in=client_ids,
                status__in=[
                    ClientTariffVersion.STATUS_ACTIVE,
                    ClientTariffVersion.STATUS_SCHEDULED,
                ],
                valid_from__lte=today,
            )
            .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=today))
            .values_list("client_id", flat=True)
        )
    result: dict[tuple[str, str], str] = {}
    for key in cleaned:
        app = app_by_key.get(key)
        if app is None:
            result[key] = ""
            continue
        result[key] = _billing_action_from_app(
            app,
            missing_tariff=app.id in missing_tariff_ids,
            unconfirmed=app.id in unconfirmed_ids,
            has_active_tariff=app.client_id in active_tariff_clients,
        )
    return result


def billing_next_action_for_wms(
    *,
    application_type: str,
    application_id: str,
    client_id: int | None,
    cache: dict | None = None,
) -> str:
    """Display-only: следующее финансовое действие по BillingApplication."""
    if not application_id:
        return ""
    key = (str(application_type), str(application_id))
    if cache is not None and key in cache:
        return cache[key]
    qs = BillingApplication.objects.filter(
        application_type=application_type,
        application_id=str(application_id),
    )
    if client_id:
        qs = qs.filter(client_id=client_id)
    app = qs.order_by("-updated_at", "-id").first()
    if not app:
        action = ""
    else:
        missing_tariff = ApplicationCharge.objects.filter(
            application=app,
            client_tariff_version__isnull=True,
            is_manual_override=False,
        ).exists()
        unconfirmed = ApplicationCharge.objects.filter(application=app, is_confirmed=False).exclude(
            is_included_in_invoice=True
        ).exists()
        action = _billing_action_from_app(
            app,
            missing_tariff=missing_tariff,
            unconfirmed=unconfirmed,
            has_active_tariff=_client_has_active_tariff(app),
        )
    if cache is not None:
        cache[key] = action
    return action


def get_attention_items(user_or_request, *, limit: int = 40) -> list[AttentionItem]:
    """Портфельные проблемы менеджера — пакетные запросы (этап 7)."""
    items: list[AttentionItem] = []
    clients = list(get_manager_clients(user_or_request)[:200])
    if not clients:
        return []
    today = timezone.localdate()
    client_ids = [c.id for c in clients]
    names = {c.id: (c.short_name or c.agn_name or f"Клиент #{c.id}") for c in clients}

    contract_ids = set(
        ClientBillingContract.objects.filter(client_id__in=client_ids, is_active=True, valid_from__lte=today)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=today))
        .values_list("client_id", flat=True)
    )
    legacy_contract = {
        c.id
        for c in clients
        if getattr(c, "contract_numb", None)
        or (getattr(c, "lifecycle", None) and getattr(c.lifecycle, "commercial_offer_number", None))
    }

    tariff_qs = (
        ClientTariffVersion.objects.filter(
            client_id__in=client_ids,
            status=ClientTariffVersion.STATUS_ACTIVE,
            valid_from__lte=today,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=today))
        .order_by("client_id", "-valid_from", "-id")
    )
    tariff_by_client: dict[int, ClientTariffVersion] = {}
    for tv in tariff_qs:
        if tv.client_id not in tariff_by_client:
            tariff_by_client[tv.client_id] = tv

    zero_price = {
        row["tariff_version__client_id"]: row["cnt"]
        for row in ClientTariffItem.objects.filter(
            tariff_version_id__in=[t.id for t in tariff_by_client.values()],
            is_active=True,
            price=0,
        )
        .values("tariff_version__client_id")
        .annotate(cnt=Count("id"))
    }

    overdue_map = {
        row["client_id"]: row["total"] or Decimal("0")
        for row in ClientInvoice.objects.filter(client_id__in=client_ids, due_date__lt=today, debt_amount__gt=0)
        .exclude(status=InvoiceStatus.CANCELLED)
        .values("client_id")
        .annotate(total=Sum("debt_amount"))
    }
    unconfirmed_map = {
        row["client_id"]: row["cnt"]
        for row in ApplicationCharge.objects.filter(
            client_id__in=client_ids,
            is_confirmed=False,
            is_disputed=False,
        )
        .exclude(is_included_in_act=True)
        .exclude(is_included_in_invoice=True)
        .filter(Q(client_tariff_version__isnull=False) | Q(is_manual_override=True))
        .values("client_id")
        .annotate(cnt=Count("id"))
    }
    no_price_map = {
        row["client_id"]: row["cnt"]
        for row in ApplicationCharge.objects.filter(
            client_id__in=client_ids,
            client_tariff_version__isnull=True,
            is_manual_override=False,
        )
        .values("client_id")
        .annotate(cnt=Count("id"))
    }
    returned_merged: dict[int, int] = {}
    for row in (
        BillingAct.objects.filter(client_id__in=client_ids, review_status=DocumentReviewStatus.RETURNED)
        .exclude(status=ActStatus.CANCELLED)
        .values("client_id")
        .annotate(cnt=Count("id"))
    ):
        returned_merged[row["client_id"]] = returned_merged.get(row["client_id"], 0) + row["cnt"]
    for row in ClientInvoice.objects.filter(
        client_id__in=client_ids,
        review_status=DocumentReviewStatus.RETURNED,
        status=InvoiceStatus.DRAFT,
    ).values("client_id").annotate(cnt=Count("id")):
        returned_merged[row["client_id"]] = returned_merged.get(row["client_id"], 0) + row["cnt"]

    disc_map = {
        row["client_id"]: row["cnt"]
        for row in BillingDiscrepancy.objects.filter(client_id__in=client_ids)
        .exclude(status=DiscrepancyStatus.CLOSED)
        .values("client_id")
        .annotate(cnt=Count("id"))
    }

    for client in clients:
        cid = client.id
        name = names[cid]
        base = f"/team-manager/billing/clients/{cid}/"
        if cid not in contract_ids and cid not in legacy_contract:
            items.append(
                AttentionItem(cid, name, "no_contract", "Нет действующего договора / КП", base + "?tab=contracts", "warn")
            )
        tariff = tariff_by_client.get(cid)
        if not tariff:
            items.append(
                AttentionItem(cid, name, "no_tariff", "Нет опубликованного тарифа", base + "?tab=tariffs", "danger")
            )
        elif zero_price.get(cid):
            items.append(
                AttentionItem(
                    cid,
                    name,
                    "no_price",
                    f"Услуги без договорной цены: {zero_price[cid]}",
                    base + "?tab=tariffs",
                    "danger",
                )
            )
        if overdue_map.get(cid):
            items.append(
                AttentionItem(
                    cid,
                    name,
                    "overdue",
                    "Просроченные счета",
                    "/team-manager/billing/debts/",
                    "danger",
                    amount=overdue_map[cid],
                )
            )
        if unconfirmed_map.get(cid):
            items.append(
                AttentionItem(
                    cid,
                    name,
                    "qty_unconfirmed",
                    f"Количество не подтверждено: {unconfirmed_map[cid]}",
                    f"/team-manager/billing/charges/?client={cid}&unconfirmed=1",
                    "warn",
                )
            )
        if no_price_map.get(cid):
            items.append(
                AttentionItem(
                    cid,
                    name,
                    "charge_no_price",
                    f"Начисления без договорной цены: {no_price_map[cid]}",
                    f"/team-manager/billing/charges/?client={cid}&no_price=1",
                    "danger",
                )
            )
        if returned_merged.get(cid):
            items.append(
                AttentionItem(
                    cid,
                    name,
                    "doc_returned",
                    f"Документы возвращены бухгалтером: {returned_merged[cid]}",
                    "/team-manager/billing/document-drafts/?review=returned",
                    "warn",
                )
            )
        if disc_map.get(cid):
            items.append(
                AttentionItem(
                    cid,
                    name,
                    "discrepancy",
                    f"Открытые расхождения: {disc_map[cid]}",
                    "/team-manager/billing/disputes/",
                    "danger",
                )
            )
        if len(items) >= limit:
            break
    return items[:limit]


def applications_ready_to_invoice(user_or_request):
    """
    Заявки к выставлению: есть начисления, объёмы подтверждены,
    нет строк без цены, не закрыты финансово.
    """
    from .charge_status import charge_missing_price
    from .permissions import filter_applications_for_user

    app_qs = (
        filter_applications_for_user(BillingApplication.objects.all(), user_or_request)
        .filter(charges_total__gt=0)
        .exclude(
            billing_status__in=[
                BillingStatus.CANCELLED,
                BillingStatus.FINANCIALLY_CLOSED,
                BillingStatus.PAID,
                BillingStatus.NOT_CALCULATED,
            ]
        )
        .select_related("client", "manager", "own_company")
        .prefetch_related("charges")
        .distinct()
        .order_by("-updated_at", "-id")
    )
    rows = []
    for app in app_qs[:400]:
        charges = list(app.charges.all())
        if not charges:
            continue
        open_charges = [
            c
            for c in charges
            if not c.is_included_in_invoice and not c.is_disputed and not getattr(c, "is_excluded", False)
        ]
        if not open_charges and app.billing_status not in {
            BillingStatus.INVOICE_REQUIRED,
            BillingStatus.ACT_CONFIRMED,
        }:
            continue
        missing = [c for c in open_charges if charge_missing_price(c)]
        unconfirmed = [
            c
            for c in open_charges
            if not c.is_confirmed and not charge_missing_price(c) and not c.is_included_in_act
        ]
        contract = active_contract_for_client(app.client)
        tariff = active_tariff_version(app.client)
        blockers = []
        if missing:
            blockers.append(f"без цены: {len(missing)}")
        if unconfirmed:
            blockers.append(f"qty не подтверждено: {len(unconfirmed)}")
        if not contract and not (getattr(app.client, "contract_numb", None) or ""):
            blockers.append("нет договора")
        if not tariff:
            blockers.append("нет тарифа")
        ready = not blockers
        # В реестре показываем и почти готовые (с blockers), чтобы менеджер видел очередь
        rows.append(
            {
                "application": app,
                "client": app.client,
                "charges_count": len(charges),
                "open_charges": len(open_charges),
                "missing_price": len(missing),
                "unconfirmed": len(unconfirmed),
                "contract": contract,
                "tariff": tariff,
                "ready": ready,
                "blockers": blockers,
                "total": app.charges_total,
            }
        )
    rows.sort(key=lambda r: (not r["ready"], -float(r["total"] or 0)))
    return rows


def clients_table_rows(user_or_request, *, q: str = "", date_from=None, date_to=None) -> list[dict]:
    """Build the manager client register with a fixed number of grouped queries."""
    today = timezone.localdate()
    date_from = date_from or today.replace(day=1)
    date_to = date_to or today
    clients = list(get_manager_clients(user_or_request, q=q)[:300])
    client_ids = [client.id for client in clients]
    if not client_ids:
        return []

    work_by_client = {
        row["client_id"]: row["work_count"]
        for row in (
            WarehouseServiceFact.objects.filter(
                client_id__in=client_ids,
                reported_at__date__gte=date_from,
                reported_at__date__lte=date_to,
            )
            .values("client_id")
            .annotate(work_count=Count("id"))
        )
    }
    charge_by_client = {
        row["client_id"]: row
        for row in (
            ApplicationCharge.objects.filter(client_id__in=client_ids, application__isnull=False)
            .values("client_id")
            .annotate(
                accrued_total=Sum(
                    "total_amount",
                    filter=~Q(application__billing_status=BillingStatus.CANCELLED),
                ),
                missing_price=Count(
                    "id",
                    filter=Q(
                        is_excluded=False,
                        client_tariff_version__isnull=True,
                        is_manual_override=False,
                    ),
                ),
                waiting_confirmation=Count(
                    "id",
                    filter=Q(
                        is_excluded=False,
                        is_confirmed=False,
                        client_tariff_version__isnull=False,
                    ),
                ),
            )
        )
    }
    invoice_by_client = {
        row["client_id"]: row
        for row in (
            ClientInvoice.objects.filter(client_id__in=client_ids)
            .exclude(status=InvoiceStatus.CANCELLED)
            .values("client_id")
            .annotate(
                invoiced_total=Sum("total_amount"),
                paid_total=Sum("paid_amount"),
                debt_total=Sum(
                    "debt_amount",
                    filter=Q(
                        status__in=[
                            InvoiceStatus.ISSUED,
                            InvoiceStatus.SENT,
                            InvoiceStatus.PARTIALLY_PAID,
                            InvoiceStatus.REQUIRED,
                        ]
                    ),
                ),
            )
        )
    }
    tariffs_by_client = {}
    for tariff in (
        ClientTariffVersion.objects.filter(
            client_id__in=client_ids,
            status__in=[
                ClientTariffVersion.STATUS_ACTIVE,
                ClientTariffVersion.STATUS_SCHEDULED,
                ClientTariffVersion.STATUS_EXPIRED,
            ],
            valid_from__lte=today,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=today))
        .order_by("client_id", "-valid_from", "-version_number", "-id")
    ):
        tariffs_by_client.setdefault(tariff.client_id, tariff)

    rows = []
    for client in clients:
        charges = charge_by_client.get(client.id, {})
        invoices = invoice_by_client.get(client.id, {})
        rows.append(
            {
                "client": client,
                "tariff": tariffs_by_client.get(client.id),
                "work_count": work_by_client.get(client.id, 0),
                "missing_price": charges.get("missing_price", 0),
                "waiting_confirmation": charges.get("waiting_confirmation", 0),
                "accrued_total": charges.get("accrued_total") or Decimal("0"),
                "invoiced_total": invoices.get("invoiced_total") or Decimal("0"),
                "paid_total": invoices.get("paid_total") or Decimal("0"),
                "debt_total": invoices.get("debt_total") or Decimal("0"),
            }
        )
    return rows
