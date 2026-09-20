from django.conf import settings
from django.db import models
from django.utils import timezone

from fullbox.order_numbers import format_order_number


class AuditJournal(models.Model):
    code = models.CharField("Код", max_length=64, unique=True)
    name = models.CharField("Название", max_length=255)
    description = models.TextField("Описание", blank=True)

    class Meta:
        verbose_name = "Журнал"
        verbose_name_plural = "Журналы"
        ordering = ["code"]

    def __str__(self):
        return self.name


def sku_snapshot(sku):
    """Минимальный слепок SKU для лога."""
    if not sku:
        return {}
    return {
        "id": sku.id,
        "sku_code": sku.sku_code,
        "name": sku.name,
        "brand": sku.brand,
        "agency_id": getattr(sku.agency, "id", None),
        "market_id": getattr(sku.market, "id", None),
        "color": sku.color,
        "size": sku.size,
        "honest_sign": sku.honest_sign,
        "use_nds": sku.use_nds,
        "updated_at": sku.updated_at.isoformat() if sku.updated_at else None,
    }


class AuditEntry(models.Model):
    ACTION_CHOICES = [
        ("create", "Создание"),
        ("update", "Изменение"),
        ("delete", "Удаление"),
        ("clone", "Клонирование"),
    ]

    journal = models.ForeignKey(
        AuditJournal, on_delete=models.CASCADE, related_name="entries", verbose_name="Журнал"
    )
    action = models.CharField("Действие", max_length=32, choices=ACTION_CHOICES)
    sku = models.ForeignKey("sku.SKU", on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_entries")
    agency = models.ForeignKey(
        "sku.Agency", on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_entries"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Пользователь"
    )
    description = models.TextField("Описание", blank=True)
    snapshot = models.JSONField("Снимок", null=True, blank=True)
    created_at = models.DateTimeField("Когда", auto_now_add=True)

    class Meta:
        verbose_name = "Журнал изменений SKU"
        verbose_name_plural = "Журналы изменений SKU"
        ordering = ["-created_at"]

    def __str__(self):
        subject = self.sku or self.agency or "-"
        return f"{self.get_action_display()} {subject} ({self.created_at:%Y-%m-%d %H:%M})"


def get_sku_journal():
    return AuditJournal.objects.get_or_create(
        code="sku",
        defaults={
            "name": "Изменения номенклатуры",
            "description": "Фиксация всех операций с номенклатурой (создание, изменение, удаление, клонирование).",
        },
    )[0]


def log_sku_change(action: str, sku, user=None, description: str = "", snapshot: dict | None = None):
    """Утилита для записи события по SKU."""
    snap = snapshot if snapshot is not None else sku_snapshot(sku)
    journal = get_sku_journal()
    AuditEntry.objects.create(
        journal=journal,
        action=action,
        sku=sku,
        agency=None,
        user=user,
        description=description,
        snapshot=snap,
    )


def agency_snapshot(agency):
    if not agency:
        return {}
    return {
        "id": agency.id,
        "agn_name": agency.agn_name,
        "short_name": getattr(agency, "short_name", None),
        "pref": agency.pref,
        "inn": agency.inn,
        "kpp": agency.kpp,
        "ogrn": agency.ogrn,
        "phone": agency.phone,
        "email": agency.email,
        "adres": agency.adres,
        "fakt_adres": agency.fakt_adres,
        "fio_agn": agency.fio_agn,
        "sign_oferta": agency.sign_oferta,
        "use_nds": agency.use_nds,
        "contract_numb": agency.contract_numb,
        "contract_link": agency.contract_link,
        "archived": agency.archived,
        "portal_user_id": agency.portal_user_id,
    }


def get_agency_journal():
    return AuditJournal.objects.get_or_create(
        code="agency",
        defaults={
            "name": "Изменения клиентов",
            "description": "Фиксация операций с клиентами (создание, изменение, архивирование).",
        },
    )[0]


def log_agency_change(action: str, agency, user=None, description: str = "", snapshot: dict | None = None):
    snap = snapshot if snapshot is not None else agency_snapshot(agency)
    journal = get_agency_journal()
    AuditEntry.objects.create(
        journal=journal,
        action=action,
        agency=agency,
        user=user,
        description=description,
        snapshot=snap,
    )


def get_staff_overactions_journal():
    return AuditJournal.objects.get_or_create(
        code="staff_overactions",
        defaults={
            "name": "Избыточные действия сотрудников",
            "description": "Логи ручных корректировок в потоковой приемке (редактирование/удаление коробов).",
        },
    )[0]


def log_staff_overaction(
    action: str,
    user=None,
    agency=None,
    description: str = "",
    snapshot: dict | None = None,
):
    journal = get_staff_overactions_journal()
    AuditEntry.objects.create(
        journal=journal,
        action=action,
        agency=agency,
        user=user,
        description=description,
        snapshot=snapshot or {},
    )


def get_stock_move_journal():
    return AuditJournal.objects.get_or_create(
        code="stock_move",
        defaults={
            "name": "Перемещение товара",
            "description": "Фиксация перемещений паллет между местами хранения.",
        },
    )[0]


def log_stock_move(
    action: str,
    user=None,
    agency=None,
    description: str = "",
    snapshot: dict | None = None,
):
    journal = get_stock_move_journal()
    AuditEntry.objects.create(
        journal=journal,
        action=action,
        agency=agency,
        user=user,
        description=description,
        snapshot=snapshot or {},
    )


def get_order_external_number(order_type: str | None, order_id: str | None) -> str:
    try:
        return format_order_number(order_type, order_id)
    except Exception:
        return str(order_id or "").strip() or "-"


class OrderAuditEntry(models.Model):
    ACTION_CHOICES = [
        ("create", "Создание"),
        ("update", "Изменение"),
        ("status", "Статус"),
        ("comment", "Комментарий"),
        ("upload", "Файлы"),
    ]

    order_id = models.CharField("ID заявки", max_length=128)
    order_type = models.CharField("Тип заявки", max_length=64, default="receiving")
    action = models.CharField("Действие", max_length=32, choices=ACTION_CHOICES)
    agency = models.ForeignKey(
        "sku.Agency", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Клиент"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Пользователь"
    )
    description = models.TextField("Описание", blank=True)
    payload = models.JSONField("Данные", null=True, blank=True)
    created_at = models.DateTimeField("Когда", default=timezone.now)

    class Meta:
        verbose_name = "Аудит заявки"
        verbose_name_plural = "Аудит заявок"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["order_type", "order_id", "created_at", "id"],
                name="audit_order_lookup_idx",
            ),
            models.Index(
                fields=["agency", "order_type", "order_id", "-created_at", "-id"],
                name="audit_mgr_req_latest_idx",
            ),
            models.Index(
                fields=["order_type", "-created_at", "-id"],
                name="audit_mgr_type_latest_idx",
            ),
        ]

    def __str__(self):
        return f"{self.order_type} {self.order_id} [{self.get_action_display()}]"

    @property
    def payload_status_display(self) -> str:
        payload = self.payload if isinstance(self.payload, dict) else {}
        return str(payload.get("status_label") or payload.get("status") or "-")

    def save(self, *args, **kwargs):
        from orders.title_truth import build_entry_title_payload

        history_entries = []
        if str(self.order_type or "").strip().lower() == "receiving":
            history_entries = list(
                type(self).objects.filter(
                    order_type=self.order_type,
                    order_id=self.order_id,
                )
                .exclude(pk=self.pk)
                .only("payload", "created_at", "id")
                .order_by("created_at", "id")
            )
        self.payload = build_entry_title_payload(
            self.order_type,
            self.order_id,
            self.payload,
            history_entries,
            created_at=self.created_at or timezone.now(),
        )
        super().save(*args, **kwargs)


def log_order_action(
    action: str,
    order_id: str,
    order_type: str = "receiving",
    user=None,
    agency=None,
    description: str = "",
    payload: dict | None = None,
):
    OrderAuditEntry.objects.create(
        order_id=order_id,
        order_type=order_type,
        action=action,
        user=user,
        agency=agency,
        description=description,
        payload=payload or {},
    )
    # Единый чат: треды заявки / зеркало комментариев. Не влияет на складскую логику.
    try:
        from client_cabinet.chat_bridge import on_order_audit

        on_order_audit(
            action=action,
            order_id=order_id,
            order_type=order_type,
            user=user,
            agency=agency,
            description=description,
            payload=payload or {},
        )
    except Exception:
        # Аудит уже записан — сбой чата не откатывает операцию.
        import logging

        logging.getLogger(__name__).exception(
            "chat_bridge failed for %s %s/%s", action, order_type, order_id
        )

# Create your models here.
