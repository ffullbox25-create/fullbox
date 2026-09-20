"""Передача черновиков документов менеджер → бухгалтер (этап 5)."""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from sku.models import Agency

from .models import BillingAct, BillingApplication, BillingAuditEvent, ClientInvoice, UpdRequest
from .permissions import filter_agencies_for_user, filter_applications_for_user
from .services import BillingWorkflowService
from .statuses import ActStatus, DocumentReviewStatus, InvoiceStatus


REVIEW_FIELDS = [
    "review_status",
    "submitted_at",
    "submitted_by",
    "reviewed_at",
    "reviewed_by",
    "accountant_comment",
    "updated_at",
]


def _app_for(doc) -> BillingApplication | None:
    return getattr(doc, "application", None)


@transaction.atomic
def submit_act_for_review(act: BillingAct, *, user=None, comment: str = "") -> BillingAct:
    if act.status == ActStatus.CANCELLED:
        raise ValidationError("Нельзя передать отменённый акт.")
    if act.review_status == DocumentReviewStatus.SUBMITTED:
        return act
    if comment:
        act.manager_comment = ((act.manager_comment or "").strip() + "\n" + comment).strip()
    act.review_status = DocumentReviewStatus.SUBMITTED
    act.submitted_at = timezone.now()
    act.submitted_by = user
    act.accountant_comment = ""
    fields = REVIEW_FIELDS + (["manager_comment"] if comment else [])
    act.save(update_fields=fields)
    BillingWorkflowService.audit(
        action="act_submitted_to_accountant",
        application=act.application,
        user=user,
        obj=act,
        comment=comment or "",
        new_value={"review_status": act.review_status},
    )
    from .models import BillingStaffNotification
    from .staff_notifications import notify_accountants

    notify_accountants(
        kind=BillingStaffNotification.KIND_DOC_SUBMITTED,
        title=f"Акт {act.number} на проверке",
        message=f"Клиент: {act.client.short_name or act.client.agn_name}",
        link_url="/team-manager/billing/document-drafts/?review=submitted",
        client=act.client,
        source_key=f"act-submitted:{act.id}:{act.submitted_at.isoformat() if act.submitted_at else act.id}",
        actor=user,
    )
    return act


@transaction.atomic
def submit_invoice_for_review(invoice: ClientInvoice, *, user=None, comment: str = "") -> ClientInvoice:
    if invoice.status not in {InvoiceStatus.DRAFT, InvoiceStatus.REQUIRED}:
        raise ValidationError("На проверку можно передать только черновик счёта.")
    if invoice.review_status == DocumentReviewStatus.SUBMITTED:
        return invoice
    invoice.review_status = DocumentReviewStatus.SUBMITTED
    invoice.submitted_at = timezone.now()
    invoice.submitted_by = user
    invoice.accountant_comment = comment or ""
    invoice.save(update_fields=REVIEW_FIELDS)
    BillingWorkflowService.audit(
        action="invoice_submitted_to_accountant",
        application=invoice.application,
        user=user,
        obj=invoice,
        comment=comment or "",
        new_value={"review_status": invoice.review_status},
    )
    from .models import BillingStaffNotification
    from .staff_notifications import notify_accountants

    notify_accountants(
        kind=BillingStaffNotification.KIND_DOC_SUBMITTED,
        title=f"Счёт {invoice.number} на проверке",
        message=f"Клиент: {invoice.client.short_name or invoice.client.agn_name}",
        link_url="/team-manager/billing/document-drafts/?review=submitted",
        client=invoice.client,
        source_key=f"invoice-submitted:{invoice.id}:{invoice.submitted_at.isoformat() if invoice.submitted_at else invoice.id}",
        actor=user,
    )
    return invoice


@transaction.atomic
def return_act_to_manager(act: BillingAct, *, user=None, comment: str = "") -> BillingAct:
    if act.review_status != DocumentReviewStatus.SUBMITTED:
        raise ValidationError("Вернуть можно только акт на проверке.")
    if not (comment or "").strip():
        raise ValidationError("Укажите причину возврата.")
    act.review_status = DocumentReviewStatus.RETURNED
    act.reviewed_at = timezone.now()
    act.reviewed_by = user
    act.accountant_comment = comment.strip()
    act.save(update_fields=REVIEW_FIELDS)
    BillingWorkflowService.audit(
        action="act_returned_to_manager",
        application=act.application,
        user=user,
        obj=act,
        comment=comment,
        new_value={"review_status": act.review_status},
    )
    from .models import BillingStaffNotification
    from .staff_notifications import notify_client_manager

    notify_client_manager(
        act.client,
        kind=BillingStaffNotification.KIND_DOC_RETURNED,
        title=f"Акт {act.number} возвращён",
        message=comment.strip(),
        link_url="/team-manager/billing/document-drafts/?review=returned",
        source_key=f"act-returned-event:{act.id}:{act.reviewed_at.isoformat() if act.reviewed_at else act.id}",
        actor=user,
    )
    return act


@transaction.atomic
def return_invoice_to_manager(invoice: ClientInvoice, *, user=None, comment: str = "") -> ClientInvoice:
    if invoice.review_status != DocumentReviewStatus.SUBMITTED:
        raise ValidationError("Вернуть можно только счёт на проверке.")
    if not (comment or "").strip():
        raise ValidationError("Укажите причину возврата.")
    invoice.review_status = DocumentReviewStatus.RETURNED
    invoice.reviewed_at = timezone.now()
    invoice.reviewed_by = user
    invoice.accountant_comment = comment.strip()
    invoice.save(update_fields=REVIEW_FIELDS)
    BillingWorkflowService.audit(
        action="invoice_returned_to_manager",
        application=invoice.application,
        user=user,
        obj=invoice,
        comment=comment,
        new_value={"review_status": invoice.review_status},
    )
    from .models import BillingStaffNotification
    from .staff_notifications import notify_client_manager

    notify_client_manager(
        invoice.client,
        kind=BillingStaffNotification.KIND_DOC_RETURNED,
        title=f"Счёт {invoice.number} возвращён",
        message=comment.strip(),
        link_url="/team-manager/billing/document-drafts/?review=returned",
        source_key=f"invoice-returned-event:{invoice.id}:{invoice.reviewed_at.isoformat() if invoice.reviewed_at else invoice.id}",
        actor=user,
    )
    return invoice


@transaction.atomic
def accept_act_review(act: BillingAct, *, user=None, comment: str = "") -> BillingAct:
    if act.review_status not in {DocumentReviewStatus.SUBMITTED, DocumentReviewStatus.LOCAL}:
        raise ValidationError("Принять можно акт на проверке или локальный черновик.")
    act.review_status = DocumentReviewStatus.ACCEPTED
    act.reviewed_at = timezone.now()
    act.reviewed_by = user
    if comment:
        act.accountant_comment = comment
    act.save(update_fields=REVIEW_FIELDS)
    BillingWorkflowService.audit(
        action="act_accepted_by_accountant",
        application=act.application,
        user=user,
        obj=act,
        comment=comment or "",
        new_value={"review_status": act.review_status},
    )
    from .models import BillingStaffNotification
    from .staff_notifications import notify_client_manager

    notify_client_manager(
        act.client,
        kind=BillingStaffNotification.KIND_DOC_ACCEPTED,
        title=f"Акт {act.number} принят бухгалтером",
        message=comment or "",
        link_url=f"/team-manager/billing/applications/{act.application_id}/",
        source_key=f"act-accepted:{act.id}:{act.reviewed_at.isoformat() if act.reviewed_at else act.id}",
        actor=user,
    )
    return act


@transaction.atomic
def accept_invoice_review(invoice: ClientInvoice, *, user=None, comment: str = "") -> ClientInvoice:
    """Принять черновик и провести (issued) — шаг бухгалтера."""
    if invoice.status != InvoiceStatus.DRAFT:
        raise ValidationError("Принять можно только черновик счёта.")
    invoice.review_status = DocumentReviewStatus.ACCEPTED
    invoice.reviewed_at = timezone.now()
    invoice.reviewed_by = user
    if comment:
        invoice.accountant_comment = comment
    invoice.save(update_fields=REVIEW_FIELDS)
    BillingWorkflowService.check_invoice(invoice, user=user)
    BillingWorkflowService.audit(
        action="invoice_accepted_by_accountant",
        application=invoice.application,
        user=user,
        obj=invoice,
        comment=comment or "",
        new_value={"review_status": invoice.review_status, "status": invoice.status},
    )
    from .models import BillingStaffNotification
    from .staff_notifications import notify_client_manager

    notify_client_manager(
        invoice.client,
        kind=BillingStaffNotification.KIND_DOC_ACCEPTED,
        title=f"Счёт {invoice.number} проведён",
        message=comment or "",
        link_url=f"/team-manager/billing/invoices/{invoice.id}/",
        source_key=f"invoice-accepted:{invoice.id}:{invoice.reviewed_at.isoformat() if invoice.reviewed_at else invoice.id}",
        actor=user,
    )
    return invoice


def list_document_drafts(user_or_request, *, review: str = "", client_id: str = ""):
    app_qs = filter_applications_for_user(BillingApplication.objects.all(), user_or_request).distinct()
    acts = BillingAct.objects.filter(application__in=app_qs).exclude(status=ActStatus.CANCELLED)
    invoices = ClientInvoice.objects.filter(application__in=app_qs).exclude(status=InvoiceStatus.CANCELLED)

    # Черновики: акт draft/local/returned/submitted; счёт draft
    acts = acts.filter(
        Q(status=ActStatus.DRAFT)
        | Q(review_status__in=[DocumentReviewStatus.LOCAL, DocumentReviewStatus.SUBMITTED, DocumentReviewStatus.RETURNED])
    ).select_related("client", "application", "submitted_by", "reviewed_by", "created_by")
    invoices = invoices.filter(status=InvoiceStatus.DRAFT).select_related(
        "client", "application", "submitted_by", "reviewed_by", "created_by", "act"
    )

    if review:
        acts = acts.filter(review_status=review)
        invoices = invoices.filter(review_status=review)
    if client_id:
        acts = acts.filter(client_id=client_id)
        invoices = invoices.filter(client_id=client_id)

    rows = []
    for act in acts.order_by("-updated_at")[:200]:
        rows.append({"kind": "act", "obj": act, "review_status": act.review_status, "updated_at": act.updated_at})
    for inv in invoices.order_by("-updated_at")[:200]:
        rows.append({"kind": "invoice", "obj": inv, "review_status": inv.review_status, "updated_at": inv.updated_at})
    rows.sort(key=lambda r: r["updated_at"] or timezone.now(), reverse=True)
    return rows


def list_audit_timeline(user_or_request, *, application_id: str = "", limit: int = 100):
    app_qs = filter_applications_for_user(BillingApplication.objects.all(), user_or_request).distinct()
    qs = BillingAuditEvent.objects.filter(application__in=app_qs).select_related(
        "application", "application__client", "user"
    )
    if application_id:
        qs = qs.filter(application_id=application_id)
    return list(qs.order_by("-created_at", "-id")[:limit])


@transaction.atomic
def create_upd_request(
    *,
    user,
    client: Agency,
    comment: str = "",
    application=None,
    invoice=None,
    act=None,
    period_from=None,
    period_to=None,
) -> UpdRequest:
    req = UpdRequest.objects.create(
        client=client,
        application=application,
        invoice=invoice,
        act=act,
        period_from=period_from,
        period_to=period_to,
        comment=comment or "",
        created_by=user,
        status=UpdRequest.STATUS_REQUESTED,
    )
    app = application or (invoice.application if invoice else None) or (act.application if act else None)
    BillingWorkflowService.audit(
        action="upd_requested",
        application=app,
        user=user,
        obj=req,
        comment=comment or "",
        new_value={"client_id": client.id, "status": req.status},
    )
    from .models import BillingStaffNotification
    from .staff_notifications import notify_accountants

    notify_accountants(
        kind=BillingStaffNotification.KIND_UPD,
        title="Запрос УПД",
        message=f"{client.short_name or client.agn_name}: {comment or 'без комментария'}",
        link_url="/team-manager/billing/upd/",
        client=client,
        source_key=f"upd-requested:{req.id}",
        actor=user,
    )
    return req


def list_upd_requests(user_or_request, *, status: str = "", client_id: str = ""):
    portfolio = filter_agencies_for_user(Agency.objects.all(), user_or_request)
    qs = (
        UpdRequest.objects.filter(client__in=portfolio)
        .select_related("client", "application", "invoice", "act", "created_by", "processed_by")
        .order_by("-created_at")
    )
    if status:
        qs = qs.filter(status=status)
    if client_id:
        qs = qs.filter(client_id=client_id)
    return qs
