from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("employees", "0014_employee_fbs_controller_role"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="qr_login_enabled",
            field=models.BooleanField(default=False, verbose_name="Вход по QR-коду"),
        ),
        migrations.CreateModel(
            name="EmployeeBadge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_lookup", models.CharField(db_index=True, max_length=64, verbose_name="Безопасный идентификатор токена")),
                ("token_digest", models.CharField(max_length=255, verbose_name="Хеш токена")),
                ("issued_at", models.DateTimeField(auto_now_add=True, verbose_name="Выдан")),
                ("revoked_at", models.DateTimeField(blank=True, null=True, verbose_name="Отозван")),
                ("last_used_at", models.DateTimeField(blank=True, null=True, verbose_name="Последний вход")),
                ("last_used_ip", models.GenericIPAddressField(blank=True, null=True, verbose_name="IP последнего входа")),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="badges", to="employees.employee", verbose_name="Сотрудник")),
                ("issued_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="issued_employee_badges", to=settings.AUTH_USER_MODEL, verbose_name="Кем выдан")),
                ("revoked_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="revoked_employee_badges", to=settings.AUTH_USER_MODEL, verbose_name="Кем отозван")),
            ],
            options={
                "verbose_name": "QR-бейдж сотрудника",
                "verbose_name_plural": "QR-бейджи сотрудников",
                "ordering": ("-issued_at", "-id"),
                "constraints": [models.UniqueConstraint(condition=models.Q(("revoked_at__isnull", True)), fields=("employee",), name="employees_one_active_badge_per_employee")],
            },
        ),
        migrations.CreateModel(
            name="EmployeeBadgeEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(choices=(("badge_issued", "Бейдж выдан"), ("badge_revoked", "Бейдж отозван"), ("qr_access_enabled", "Вход по QR разрешён"), ("qr_access_disabled", "Вход по QR запрещён"), ("qr_login_success", "Успешный вход по QR"), ("qr_login_failure", "Неуспешный вход по QR"), ("qr_login_rate_limited", "Превышен лимит входа по QR")), max_length=32, verbose_name="Событие")),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True, verbose_name="IP")),
                ("details", models.JSONField(blank=True, default=dict, verbose_name="Детали")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Время")),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="employee_badge_events", to=settings.AUTH_USER_MODEL, verbose_name="Инициатор")),
                ("employee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="badge_events", to="employees.employee", verbose_name="Сотрудник")),
            ],
            options={
                "verbose_name": "Событие QR-бейджа",
                "verbose_name_plural": "События QR-бейджей",
                "ordering": ("-created_at", "-id"),
                "indexes": [models.Index(fields=["ip_address", "created_at"], name="employee_qr_ip_created_idx"), models.Index(fields=["employee", "created_at"], name="employee_qr_emp_created_idx")],
            },
        ),
    ]
