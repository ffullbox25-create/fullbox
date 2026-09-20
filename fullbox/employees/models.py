from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import models

from PIL import Image


class Employee(models.Model):
    ACCESS_ROLE_CHOICES = [
        ("head_manager", "Главный менеджер"),
        ("accountant", "Бухгалтер"),
        ("fbs_new", "Пилот FBS-NEW"),
    ]
    ACCESS_ROLE_KEYS = frozenset(key for key, _label in ACCESS_ROLE_CHOICES)

    ROLE_CHOICES = [
        ('admin', 'Администратор'),
        ('director', 'Директор'),
        ('accountant', 'Бухгалтер'),
        ('hr', 'Отдел кадров'),
        ('head_manager', 'Главный менеджер'),
        ('processing_head', 'Руководитель участка обработки'),
        ('packer', 'Упаковщица'),
        ('processing_worker', 'Обработчик'),
        ('manager', 'Менеджер'),
        ('storekeeper', 'Кладовщик'),
        ('fbs_controller', 'Оператор-контролер FBS'),
        ('logistician', 'Логист'),
        ('driver', 'Водитель'),
        ('reachtruck_driver', 'Водитель ричтрака'),
        ('super_car', '\u0421\u0443\u043f\u0435\u0440\u043a\u0430\u0440'),
        ('picker', 'Сборщик'),
        ('developer', 'Разработчик'),
    ]

    full_name = models.CharField('ФИО', max_length=255)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee_profile",
        verbose_name="Пользователь",
    )
    role = models.CharField('Роль', max_length=32, choices=ROLE_CHOICES)
    access_roles = models.JSONField(
        'Дополнительные роли',
        default=list,
        blank=True,
        help_text='Дополнительные кабинеты и полномочия без изменения основной роли сотрудника.',
    )
    email = models.EmailField('Email', blank=True, null=True)
    phone = models.CharField('Телефон', max_length=32, blank=True, null=True)
    facsimile = models.ImageField('Факсимиле', upload_to='facsimiles/', blank=True, null=True)
    is_active = models.BooleanField('Активен', default=True)
    qr_login_enabled = models.BooleanField('Вход по QR-коду', default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Сотрудник'
        verbose_name_plural = 'Сотрудники'
        ordering = ['full_name']

    def __str__(self):
        return f"{self.full_name} ({self.get_role_display()})"

    def normalized_access_roles(self) -> tuple[str, ...]:
        raw_roles = self.access_roles if isinstance(self.access_roles, (list, tuple, set)) else ()
        return tuple(
            key
            for key, _label in self.ACCESS_ROLE_CHOICES
            if key in raw_roles and key != self.role
        )

    def has_role(self, role: str) -> bool:
        return bool(role) and (self.role == role or role in self.normalized_access_roles())

    @property
    def access_role_labels(self) -> tuple[str, ...]:
        labels = dict(self.ACCESS_ROLE_CHOICES)
        return tuple(labels[key] for key in self.normalized_access_roles())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_facsimile_name = self.facsimile.name if self.facsimile else ""

    def save(self, *args, **kwargs):
        facsimile_changed = bool(self.facsimile) and (
            not self.pk
            or not getattr(self.facsimile, "_committed", True)
            or self.facsimile.name != self._original_facsimile_name
        )
        if facsimile_changed:
            normalized = self._normalize_facsimile(self.facsimile)
            base_name = Path(self.facsimile.name).stem or "facsimile"
            self.facsimile.save(f"{base_name}.png", normalized, save=False)
        super().save(*args, **kwargs)
        self._original_facsimile_name = self.facsimile.name if self.facsimile else ""

    @staticmethod
    def _normalize_facsimile(facsimile_file):
        target_size = (320, 120)
        resample = getattr(Image, "Resampling", Image).LANCZOS

        facsimile_file.seek(0)
        image = Image.open(facsimile_file)
        if image.format != "PNG":
            raise ValueError("Факсимиле должно быть в формате PNG.")

        image = image.convert("RGBA")
        image.thumbnail(target_size, resample)

        canvas = Image.new("RGBA", target_size, (255, 255, 255, 0))
        offset = (
            (target_size[0] - image.width) // 2,
            (target_size[1] - image.height) // 2,
        )
        canvas.paste(image, offset, image)

        out = BytesIO()
        canvas.save(out, format="PNG")
        return ContentFile(out.getvalue())


class EmployeeBadge(models.Model):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="badges",
        verbose_name="Сотрудник",
    )
    token_lookup = models.CharField(
        "Безопасный идентификатор токена",
        max_length=64,
        db_index=True,
    )
    token_digest = models.CharField("Хеш токена", max_length=255)
    issued_at = models.DateTimeField("Выдан", auto_now_add=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issued_employee_badges",
        verbose_name="Кем выдан",
    )
    revoked_at = models.DateTimeField("Отозван", null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_employee_badges",
        verbose_name="Кем отозван",
    )
    last_used_at = models.DateTimeField("Последний вход", null=True, blank=True)
    last_used_ip = models.GenericIPAddressField("IP последнего входа", null=True, blank=True)

    class Meta:
        verbose_name = "QR-бейдж сотрудника"
        verbose_name_plural = "QR-бейджи сотрудников"
        ordering = ("-issued_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("employee",),
                condition=models.Q(revoked_at__isnull=True),
                name="employees_one_active_badge_per_employee",
            ),
        ]

    def __str__(self):
        status = "активен" if self.revoked_at is None else "отозван"
        return f"{self.employee.full_name}: {status}"


class EmployeeBadgeEvent(models.Model):
    EVENT_ISSUED = "badge_issued"
    EVENT_REVOKED = "badge_revoked"
    EVENT_ACCESS_ENABLED = "qr_access_enabled"
    EVENT_ACCESS_DISABLED = "qr_access_disabled"
    EVENT_LOGIN_SUCCESS = "qr_login_success"
    EVENT_LOGIN_FAILURE = "qr_login_failure"
    EVENT_RATE_LIMITED = "qr_login_rate_limited"
    EVENT_CHOICES = (
        (EVENT_ISSUED, "Бейдж выдан"),
        (EVENT_REVOKED, "Бейдж отозван"),
        (EVENT_ACCESS_ENABLED, "Вход по QR разрешён"),
        (EVENT_ACCESS_DISABLED, "Вход по QR запрещён"),
        (EVENT_LOGIN_SUCCESS, "Успешный вход по QR"),
        (EVENT_LOGIN_FAILURE, "Неуспешный вход по QR"),
        (EVENT_RATE_LIMITED, "Превышен лимит входа по QR"),
    )

    event_type = models.CharField("Событие", max_length=32, choices=EVENT_CHOICES)
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="badge_events",
        verbose_name="Сотрудник",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee_badge_events",
        verbose_name="Инициатор",
    )
    ip_address = models.GenericIPAddressField("IP", null=True, blank=True)
    details = models.JSONField("Детали", default=dict, blank=True)
    created_at = models.DateTimeField("Время", auto_now_add=True)

    class Meta:
        verbose_name = "Событие QR-бейджа"
        verbose_name_plural = "События QR-бейджей"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("ip_address", "created_at"),
                name="employee_qr_ip_created_idx",
            ),
            models.Index(
                fields=("employee", "created_at"),
                name="employee_qr_emp_created_idx",
            ),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} · {self.created_at:%d.%m.%Y %H:%M:%S}"
