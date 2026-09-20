from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from client_cabinet.models import (
    OtherRequest,
    OtherRequestAttachment,
    OtherRequestCategory,
    OtherRequestComment,
)
from client_cabinet.other_request_workflow import (
    RESULT_KIND_CHECK,
    RESULT_KIND_MEASURE,
    RESULT_KIND_PHOTO,
    RESULT_KIND_RECOUNT,
    MANAGER_CANCEL_LOCKED_STATUSES,
    OtherRequestError,
    approve_and_close,
    approve_and_send_to_department,
    cancel_request,
    complete_by_executor,
    ensure_other_request_from_audit,
    request_client_info,
    request_warehouse_cancel_confirmation,
    result_form_kind,
    result_outcome_label,
    return_for_rework,
    take_in_progress,
    update_meta,
    validate_result_requirements,
    warehouse_cancel_request_payload,
)
from client_cabinet.other_requests import list_other_attachments, list_other_entries, save_other_attachments
from employees.access import RoleRequiredMixin, get_request_role
from employees.models import Employee

from .roles import CABINET_ROLES


def _sync_missing_from_audit(limit: int = 200) -> None:
    for entry in list_other_entries(limit=limit):
        ensure_other_request_from_audit(entry.order_id)


def _parse_decimal(value) -> Decimal | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _compose_result_payload(kind: str, post) -> dict:
    """Собирает result_outcome/result_comment/result_qty/result_unit из категорийной формы.

    Без новых полей: структурированные значения (габариты, вес, факт. количество)
    упаковываются читаемым текстом в result_comment, а единственное числовое
    значение — в result_qty/result_unit.
    """
    comment = (post.get("result_comment") or "").strip()
    result_outcome = post.get("result_outcome") or "done_full"
    result_qty: Decimal | None = None
    result_unit = ""

    if kind == RESULT_KIND_MEASURE:
        length_cm = (post.get("measure_length_cm") or "").strip()
        width_cm = (post.get("measure_width_cm") or "").strip()
        height_cm = (post.get("measure_height_cm") or "").strip()
        weight_kg = (post.get("measure_weight_kg") or "").strip()
        package_type = (post.get("measure_package_type") or "").strip()
        lines = []
        if length_cm:
            lines.append(f"Длина: {length_cm} см")
        if width_cm:
            lines.append(f"Ширина: {width_cm} см")
        if height_cm:
            lines.append(f"Высота: {height_cm} см")
        if weight_kg:
            lines.append(f"Вес: {weight_kg} кг")
            result_qty = _parse_decimal(weight_kg)
            result_unit = "кг"
        if package_type:
            lines.append(f"Тип упаковки: {package_type}")
        if comment:
            lines.append(f"Комментарий: {comment}")
        comment = "\n".join(lines)
    elif kind == RESULT_KIND_RECOUNT:
        actual_qty = (post.get("recount_actual_qty") or "").strip()
        unit = (post.get("recount_unit") or "шт").strip() or "шт"
        reason = (post.get("recount_reason") or "").strip()
        result_qty = _parse_decimal(actual_qty)
        result_unit = unit
        lines = []
        if actual_qty:
            lines.append(f"Фактическое количество: {actual_qty} {unit}")
        if reason:
            lines.append(f"Причина расхождения: {reason}")
        if comment:
            lines.append(comment)
        comment = "\n".join(lines)
    elif kind == RESULT_KIND_CHECK:
        matches = post.get("check_matches") or ""
        discrepancy = (post.get("check_discrepancy") or "").strip()
        if matches == "no":
            result_outcome = "mismatch"
        lines = []
        if matches == "yes":
            lines.append("Соответствует норме")
        elif matches == "no":
            lines.append("Обнаружено расхождение")
        if discrepancy:
            lines.append(f"Детали: {discrepancy}")
        if comment:
            lines.append(comment)
        comment = "\n".join(lines)
    elif kind == RESULT_KIND_PHOTO:
        photo_note = (post.get("photo_note") or "").strip()
        lines = []
        if photo_note:
            lines.append(photo_note)
        if comment:
            lines.append(comment)
        comment = "\n".join(lines)

    return {
        "result_outcome": result_outcome,
        "result_comment": comment,
        "result_qty": result_qty,
        "result_unit": result_unit,
    }


def _billing_context(obj: OtherRequest) -> dict:
    ctx = {
        "billing_app": None,
        "billing_url": "",
        "billing_status_label": obj.billing_status or "—",
    }
    try:
        from billing.models import BillingApplication

        app = (
            BillingApplication.objects.filter(
                application_type=BillingApplication.TYPE_OTHER,
                application_id=obj.public_number,
                client=obj.agency,
            )
            .order_by("-id")
            .first()
        )
        if app:
            ctx["billing_app"] = app
            ctx["billing_url"] = f"/team-manager/billing/applications/{app.pk}/"
            ctx["billing_status_label"] = app.get_billing_status_display() if hasattr(app, "get_billing_status_display") else app.billing_status
    except Exception:
        pass
    return ctx


def _time_left_label(obj: OtherRequest) -> str:
    if not obj.due_at:
        return ""
    if obj.status in {
        OtherRequest.STATUS_CLOSED,
        OtherRequest.STATUS_CANCELLED,
        OtherRequest.STATUS_REJECTED,
    }:
        return ""
    delta = obj.due_at - timezone.now()
    total_minutes = int(delta.total_seconds() // 60)
    overdue = total_minutes < 0
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if not days and minutes:
        parts.append(f"{minutes} мин")
    label = " ".join(parts) or "0 мин"
    return f"Просрочено на {label}" if overdue else f"Осталось {label}"


def _history_kind(action: str) -> str:
    action = str(action or "")
    if action == "attachment":
        return "file"
    if action in {"status", "submit"}:
        return "status"
    return "other"


def _notify_client_comment(obj: OtherRequest, text: str) -> None:
    try:
        from client_cabinet.lk_requests import lk_request_hash
        from client_cabinet.messaging_lk import create_notification

        create_notification(
            agency=obj.agency,
            title=f"Комментарий по заявке {obj.public_number}",
            text=text[:500],
            notif_type="comment",
            detail_url=lk_request_hash("other", obj.public_number),
            source_key=f"other-comment:{obj.public_number}:{timezone.now().timestamp()}",
        )
    except Exception:
        pass


class TeamManagerOtherRequestsListView(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/other_requests_list.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _sync_missing_from_audit()
        qs = OtherRequest.objects.select_related("agency", "category", "assignee", "manager").annotate(
            comments_count=Count("comments", distinct=True),
            attachments_count=Count("attachments", distinct=True),
            links_count=Count("links", distinct=True),
        )
        q = (self.request.GET.get("q") or "").strip()
        status = (self.request.GET.get("status") or "").strip()
        department = (self.request.GET.get("department") or "").strip()
        priority = (self.request.GET.get("priority") or "").strip()
        category = (self.request.GET.get("category") or "").strip()
        focus = (self.request.GET.get("focus") or "").strip()
        if category:
            qs = qs.filter(category_code=category)
        if q:
            qs = qs.filter(
                Q(public_number__icontains=q)
                | Q(title__icontains=q)
                | Q(description__icontains=q)
                | Q(agency__agn_name__icontains=q)
                | Q(agency__short_name__icontains=q)
                | Q(assignee__full_name__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)
        if department:
            qs = qs.filter(department=department)
        if priority:
            qs = qs.filter(priority=priority)
        now = timezone.now()
        if focus == "overdue":
            qs = qs.filter(due_at__lt=now).exclude(
                status__in={
                    OtherRequest.STATUS_CLOSED,
                    OtherRequest.STATUS_CANCELLED,
                    OtherRequest.STATUS_REJECTED,
                    OtherRequest.STATUS_DRAFT,
                }
            )
        elif focus == "new":
            qs = qs.filter(status=OtherRequest.STATUS_AWAITING_MANAGER)
        elif focus == "department":
            qs = qs.filter(
                status__in={
                    OtherRequest.STATUS_AWAITING_DEPARTMENT,
                    OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
                }
            )
        elif focus == "in_progress":
            qs = qs.filter(
                status__in={
                    OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
                    OtherRequest.STATUS_IN_PROGRESS,
                    OtherRequest.STATUS_PAUSED,
                    OtherRequest.STATUS_REWORK,
                }
            )
        elif focus == "check":
            qs = qs.filter(
                status__in={
                    OtherRequest.STATUS_AWAITING_MANAGER_CHECK,
                    OtherRequest.STATUS_DONE_BY_DEPARTMENT,
                }
            )
        elif focus == "closed":
            qs = qs.filter(
                status__in={
                    OtherRequest.STATUS_CLOSED,
                    OtherRequest.STATUS_CANCELLED,
                    OtherRequest.STATUS_REJECTED,
                }
            )

        counters = OtherRequest.objects.aggregate(
            all=Count("id"),
            new=Count("id", filter=Q(status=OtherRequest.STATUS_AWAITING_MANAGER)),
            department=Count(
                "id",
                filter=Q(
                    status__in={
                        OtherRequest.STATUS_AWAITING_DEPARTMENT,
                        OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
                    }
                ),
            ),
            in_progress=Count(
                "id",
                filter=Q(
                    status__in={
                        OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
                        OtherRequest.STATUS_IN_PROGRESS,
                        OtherRequest.STATUS_PAUSED,
                        OtherRequest.STATUS_REWORK,
                    }
                ),
            ),
            check=Count(
                "id",
                filter=Q(
                    status__in={
                        OtherRequest.STATUS_AWAITING_MANAGER_CHECK,
                        OtherRequest.STATUS_DONE_BY_DEPARTMENT,
                    }
                ),
            ),
            overdue=Count(
                "id",
                filter=Q(due_at__lt=now)
                & ~Q(
                    status__in={
                        OtherRequest.STATUS_CLOSED,
                        OtherRequest.STATUS_CANCELLED,
                        OtherRequest.STATUS_REJECTED,
                        OtherRequest.STATUS_DRAFT,
                    }
                ),
            ),
            closed=Count(
                "id",
                filter=Q(
                    status__in={
                        OtherRequest.STATUS_CLOSED,
                        OtherRequest.STATUS_CANCELLED,
                        OtherRequest.STATUS_REJECTED,
                    }
                ),
            ),
        )

        rows = list(
            qs.order_by("-priority", "due_at", "-created_at")[:300]
        )
        rows.sort(
            key=lambda r: (
                0 if r.is_overdue else 1,
                0 if r.priority == OtherRequest.PRIORITY_URGENT else 1 if r.priority == OtherRequest.PRIORITY_HIGH else 2,
                r.due_at or now,
                -(r.created_at.timestamp() if r.created_at else 0),
            )
        )

        ctx.update(
            {
                "role": get_request_role(self.request),
                "active_nav": "other_requests",
                "title": "Другие заявки",
                "rows": rows,
                "counters": counters,
                "filters": {
                    "q": q,
                    "status": status,
                    "focus": focus,
                    "department": department,
                    "priority": priority,
                    "category": category,
                },
                "status_choices": OtherRequest.STATUS_CHOICES,
                "department_choices": OtherRequest.DEPARTMENT_CHOICES,
                "priority_choices": OtherRequest.PRIORITY_CHOICES,
                "category_choices": list(
                    OtherRequestCategory.objects.filter(is_active=True)
                    .order_by("sort_order", "title")
                    .values_list("code", "title")
                ),
            }
        )
        return ctx


class TeamManagerOtherRequestDetailView(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/other_request_detail.html"
    allowed_roles = CABINET_ROLES

    def _get_object(self) -> OtherRequest:
        number = str(self.kwargs.get("number") or "").strip()
        obj = ensure_other_request_from_audit(number) or OtherRequest.objects.filter(public_number=number).first()
        if not obj:
            raise Http404("Другая заявка не найдена")
        return obj

    def post(self, request, *args, **kwargs):
        obj = self._get_object()
        action = (request.POST.get("action") or "").strip().lower()
        reason = (request.POST.get("reason") or "").strip()
        try:
            if action in {"send_to_department", "approve_send"}:
                approve_and_send_to_department(obj, user=request.user)
                messages.success(request, "Заявка передана в подразделение")
            elif action == "take_in_work":
                if get_request_role(request) in {"manager", "head_manager", "director", "admin", "developer"}:
                    raise OtherRequestError(
                        "В работу заявку принимает склад в складской карточке.",
                        code="forbidden",
                    )
                take_in_progress(obj, user=request.user)
                messages.success(request, "Заявка взята в работу")
            elif action == "complete_work":
                if get_request_role(request) in {"manager", "head_manager", "director", "admin", "developer"}:
                    raise OtherRequestError(
                        "Работу по заявке завершает склад. Менеджер подтверждает результат после выполнения.",
                        code="forbidden",
                    )
                kind = result_form_kind(obj.category_code)
                payload = _compose_result_payload(kind, request.POST)
                complete_by_executor(
                    obj,
                    user=request.user,
                    result_outcome=payload["result_outcome"],
                    result_comment=payload["result_comment"],
                    result_qty=payload["result_qty"],
                    result_unit=payload["result_unit"],
                )
                messages.success(request, "Работа передана на проверку менеджеру")
            elif action == "approve_close":
                approve_and_close(obj, user=request.user)
                messages.success(request, "Заявка закрыта, биллинг обновлён")
            elif action == "return_rework":
                return_for_rework(obj, user=request.user, reason=reason)
                messages.success(request, "Заявка возвращена на доработку")
            elif action == "request_client_info":
                request_client_info(obj, user=request.user, reason=reason)
                messages.success(request, "Клиенту запрошено уточнение")
            elif action == "cancel":
                if obj.status in MANAGER_CANCEL_LOCKED_STATUSES:
                    raise OtherRequestError(
                        "Заявка уже передана на склад. Менеджер не может отменить её самостоятельно.",
                        code="forbidden",
                    )
                cancel_request(obj, user=request.user, reason=reason)
                messages.success(request, "Заявка отменена")
            elif action == "request_warehouse_cancel":
                request_warehouse_cancel_confirmation(obj, user=request.user, reason=reason)
                messages.success(request, "Запрос на отмену отправлен складу")
            elif action == "add_comment":
                text = (request.POST.get("comment") or "").strip()
                visibility = (request.POST.get("visibility") or "internal").strip()
                if text:
                    OtherRequestComment.objects.create(
                        request=obj,
                        author=request.user,
                        visibility=visibility
                        if visibility in {"client", "internal"}
                        else "internal",
                        text=text,
                    )
                    if visibility == "client":
                        _notify_client_comment(obj, text)
                    messages.success(request, "Комментарий добавлен")
            elif action == "update_meta":
                from django.utils.dateparse import parse_datetime

                assignee_raw = (request.POST.get("assignee_id") or "").strip()
                assignee = Employee.objects.filter(pk=assignee_raw, is_active=True).first() if assignee_raw else None
                due_raw = (request.POST.get("due_at") or "").strip()
                due_at = None
                if due_raw:
                    parsed = parse_datetime(due_raw)
                    if parsed and timezone.is_naive(parsed):
                        parsed = timezone.make_aware(parsed)
                    due_at = parsed
                update_meta(
                    obj,
                    user=request.user,
                    assignee=assignee,
                    assignee_provided="assignee_id" in request.POST,
                    department=(request.POST.get("department") or "").strip(),
                    priority=(request.POST.get("priority") or "").strip(),
                    due_at=due_at,
                    due_at_provided="due_at" in request.POST,
                )
                messages.success(request, "Параметры заявки обновлены")
            elif action == "upload_attachment":
                files = request.FILES.getlist("files") or []
                purpose = (request.POST.get("purpose") or "internal").strip()
                saved = save_other_attachments(
                    order_id=obj.public_number,
                    agency=obj.agency,
                    user=request.user,
                    files=files,
                    purpose=purpose,
                    request_obj=obj,
                )
                if saved:
                    messages.success(request, f"Загружено файлов: {len(saved)}")
                else:
                    messages.error(request, "Выберите файл для загрузки")
            else:
                messages.error(request, "Неизвестное действие")
        except OtherRequestError as exc:
            messages.error(request, exc.message)
        return redirect(obj.detail_url)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj = self._get_object()
        role = get_request_role(self.request)
        primary = None
        if obj.status in {OtherRequest.STATUS_AWAITING_MANAGER, OtherRequest.STATUS_APPROVED}:
            primary = ("send_to_department", "Подтвердить и отправить на склад")
        elif obj.status in {
            OtherRequest.STATUS_AWAITING_MANAGER_CHECK,
            OtherRequest.STATUS_DONE_BY_DEPARTMENT,
        }:
            primary = ("approve_close", "Подтвердить и закрыть")

        history = list(
            __import__("audit.models", fromlist=["OrderAuditEntry"]).OrderAuditEntry.objects.filter(
                order_type="other", order_id=obj.public_number
            )
            .select_related("user")
            .order_by("-created_at")[:50]
        )
        for entry in history:
            entry.timeline_kind = _history_kind(entry.action)
        comments = list(obj.comments.select_related("author").order_by("created_at"))
        attachments = list_other_attachments(order_id=obj.public_number, agency=obj.agency)
        client_files = [a for a in attachments if a["purpose"] == OtherRequestAttachment.PURPOSE_CLIENT]
        internal_files = [a for a in attachments if a["purpose"] == OtherRequestAttachment.PURPOSE_INTERNAL]
        result_files = [a for a in attachments if a["purpose"] == OtherRequestAttachment.PURPOSE_RESULT]

        result_kind = result_form_kind(obj.category_code)
        missing_result = (
            validate_result_requirements(obj) if obj.status == OtherRequest.STATUS_IN_PROGRESS else []
        )

        manager_roles = {"manager", "head_manager", "director", "admin", "developer"}
        cancel_blocked_by_warehouse = obj.status in MANAGER_CANCEL_LOCKED_STATUSES
        warehouse_cancel_request = warehouse_cancel_request_payload(obj)
        other_service_facts = []
        if obj.agency_id:
            from billing.warehouse_services import facts_payload, list_facts

            try:
                other_service_facts = facts_payload(
                    list_facts(
                        client=obj.agency,
                        order_type="other",
                        order_id=obj.public_number,
                    )
                )
            except ValidationError:
                other_service_facts = []

        ctx.update(
            {
                "role": role,
                "active_nav": "other_requests",
                "title": f"Другая заявка № {obj.public_number}",
                "item": obj,
                "primary_action": primary,
                "history": history,
                "comments": comments,
                "attachments": attachments,
                "client_files": client_files,
                "internal_files": internal_files,
                "result_files": result_files,
                "links": list(obj.links.all()),
                "result_kind": result_kind,
                "result_outcome_label": result_outcome_label(obj.result_outcome) if obj.result_outcome else "",
                "missing_result": missing_result,
                "time_left_label": _time_left_label(obj),
                "assignee_choices": Employee.objects.filter(is_active=True).order_by("full_name"),
                "can_edit_meta": role in manager_roles,
                "cancel_blocked_by_warehouse": cancel_blocked_by_warehouse,
                "warehouse_cancel_request_pending": bool(warehouse_cancel_request),
                "warehouse_cancel_reason": str(
                    warehouse_cancel_request.get("cancel_reason") or ""
                ).strip(),
                "can_request_warehouse_cancel": bool(
                    role in manager_roles
                    and cancel_blocked_by_warehouse
                    and not warehouse_cancel_request
                ),
                "other_service_facts": other_service_facts,
                "wsfm_order_type": "other",
                "wsfm_order_id": obj.public_number,
                "wsfm_client_id": obj.agency_id,
                "wsfm_source": "other_manual",
                "wsfm_title": "Фактически оказанные услуги",
                "wsfm_cancel_label": "Вернуться к заявке",
                "wsfm_items_label": "Обработано товара",
                **_billing_context(obj),
            }
        )
        return ctx
