from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fbs", "0045_problem_severity"),
    ]

    operations = [
        migrations.CreateModel(
            name="FbsHandoverVerificationOverride",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("reason", models.TextField()),
                ("snapshot", models.JSONField(default=dict)),
                ("is_active", models.BooleanField(default=True)),
                ("approved_at", models.DateTimeField(auto_now_add=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approved_fbs_handover_verification_overrides",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="verification_overrides",
                        to="fbs.fbshandoverbatch",
                    ),
                ),
                (
                    "used_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="used_fbs_handover_verification_overrides",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "fbs_handover_verification_override",
                "ordering": ["-approved_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["batch", "is_active"],
                        name="fbs_hov_batch_active_idx",
                    ),
                    models.Index(
                        fields=["approved_at"],
                        name="fbs_hov_approved_at_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("is_active", True)),
                        fields=("batch",),
                        name="uniq_active_fbs_handover_verify_override",
                    )
                ],
            },
        ),
    ]
