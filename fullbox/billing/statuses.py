from django.db import models


class BillingStatus(models.TextChoices):
    NOT_CALCULATED = "not_calculated", "Расчет не сформирован"
    CALCULATION_DRAFT = "calculation_draft", "Расчет формируется"
    CALCULATED = "calculated", "Расчет сформирован"
    ACT_DRAFT = "act_draft", "Акт создан как черновик"
    ACT_SENT = "act_sent", "Акт отправлен клиенту"
    ACT_DISPUTED = "act_disputed", "Клиент указал разногласия"
    ACT_CONFIRMED = "act_confirmed", "Акт подтвержден клиентом"
    INVOICE_REQUIRED = "invoice_required", "Требуется выставить счет"
    INVOICE_DRAFT = "invoice_draft", "Счет создан как черновик"
    INVOICE_ISSUED = "invoice_issued", "Счет выставлен"
    INVOICE_SENT = "invoice_sent", "Счет отправлен клиенту"
    PARTIALLY_PAID = "partially_paid", "Частично оплачен"
    PAID = "paid", "Полностью оплачен"
    FINANCIALLY_CLOSED = "financially_closed", "Заявка финансово закрыта"
    CANCELLED = "cancelled", "Биллинг отменен"


class ActStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    SENT = "sent", "Отправлен клиенту"
    DISPUTED = "disputed", "Есть разногласия"
    CONFIRMED = "confirmed", "Подтвержден клиентом"
    CANCELLED = "cancelled", "Отменен"


class InvoiceStatus(models.TextChoices):
    REQUIRED = "required", "Требуется выставить"
    DRAFT = "draft", "Черновик"
    ISSUED = "issued", "Выставлен"
    SENT = "sent", "Отправлен клиенту"
    PARTIALLY_PAID = "partially_paid", "Частично оплачен"
    PAID = "paid", "Оплачен"
    CANCELLED = "cancelled", "Отменен"


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Ожидает подтверждения"
    CONFIRMED = "confirmed", "Подтверждена"
    CANCELLED = "cancelled", "Отменена"


class SequenceKind(models.TextChoices):
    ACT = "act", "Акт"
    INVOICE = "invoice", "Счет"


class DocumentReviewStatus(models.TextChoices):
    """Передача черновика менеджер → бухгалтер."""

    LOCAL = "local", "Черновик менеджера"
    SUBMITTED = "submitted", "На проверке у бухгалтера"
    RETURNED = "returned", "Возвращён менеджеру"
    ACCEPTED = "accepted", "Принят бухгалтером"


class PaymentPromiseStatus(models.TextChoices):
    PENDING = "pending", "Ожидается"
    FULFILLED = "fulfilled", "Выполнено"
    PARTIAL = "partial", "Частично выполнено"
    OVERDUE = "overdue", "Просрочено"
    CANCELLED = "cancelled", "Отменено"


class ReconciliationStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    SUBMITTED = "submitted", "Передан бухгалтеру"
    FORMED = "formed", "Сформирован"
    SENT = "sent", "Отправлен клиенту"
    CONFIRMED = "confirmed", "Подтверждён"
    DISPUTED = "disputed", "Есть расхождения"
    SIGNED = "signed", "Подписан"
    ARCHIVED = "archived", "Архивный"


class DiscrepancyStatus(models.TextChoices):
    NEW = "new", "Новое"
    MANAGER_REVIEW = "manager_review", "На проверке у менеджера"
    ACCOUNTANT_REVIEW = "accountant_review", "На проверке у бухгалтера"
    WAITING_CLIENT = "waiting_client", "Ожидает клиента"
    CONFIRMED = "confirmed", "Подтверждено"
    REJECTED = "rejected", "Отклонено"
    NEEDS_CORRECTION = "needs_correction", "Требует корректировки"
    CLOSED = "closed", "Закрыто"
