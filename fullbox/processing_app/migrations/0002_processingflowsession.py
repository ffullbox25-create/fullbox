from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0005_alter_employee_role"),
        ("processing_app", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProcessingFlowSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order_id", models.CharField(max_length=64)),
                ("order_type", models.CharField(default="processing", max_length=32)),
                ("agent_id", models.CharField(blank=True, max_length=128)),
                ("flow_state", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "Открыта"), ("closed", "Закрыта")],
                        default="open",
                        max_length=16,
                    ),
                ),
                ("last_seen", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "employee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="processing_flow_sessions",
                        to="employees.employee",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="processing_flow_sessions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["order_id", "status"], name="processing__order_i_09d833_idx"),
                    models.Index(fields=["agent_id", "status"], name="processing__agent_i_1e3f7f_idx"),
                    models.Index(fields=["user", "status"], name="processing__user_id_6f2105_idx"),
                ],
            },
        ),
    ]
