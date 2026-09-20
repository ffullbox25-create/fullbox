from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fbs", "0044_controller_service_totes_and_restock_source"),
    ]

    operations = [
        migrations.CreateModel(
            name="FbsProblemToteItem",
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
                ("scanned_value", models.CharField(max_length=512)),
                ("reason", models.TextField()),
                (
                    "severity",
                    models.CharField(
                        choices=[
                            ("noncritical", "Некритическая проблема"),
                            ("critical", "Критическая проблема"),
                        ],
                        default="noncritical",
                        max_length=16,
                    ),
                ),
                ("quantity", models.PositiveIntegerField(default=1)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("in_tote", "В проблемной таре"),
                            ("returned", "Возвращен в тару проверки"),
                        ],
                        default="in_tote",
                        max_length=16,
                    ),
                ),
                ("reported_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="problem_tote_items",
                        to="fbs.fbsorder",
                    ),
                ),
                (
                    "order_item",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="problem_tote_items",
                        to="fbs.fbsorderitem",
                    ),
                ),
                (
                    "problem_tote",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="problem_items",
                        to="fbs.fbspickingcart",
                    ),
                ),
                (
                    "reported_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reported_fbs_problem_tote_items",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "resolved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="resolved_fbs_problem_tote_items",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="problem_items",
                        to="fbs.fbscontrollersession",
                    ),
                ),
                (
                    "source_check_tote",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="problem_items",
                        to="fbs.fbscontrollerchecktote",
                    ),
                ),
                (
                    "source_pick_tote",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="problem_items",
                        to="fbs.fbscontrollerpicktote",
                    ),
                ),
            ],
            options={
                "db_table": "fbs_problem_tote_item",
                "ordering": ["reported_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["problem_tote", "status", "severity"],
                        name="fbs_problem_tote_status_idx",
                    ),
                    models.Index(
                        fields=["scanned_value", "status"],
                        name="fbs_problem_item_scan_idx",
                    ),
                    models.Index(
                        fields=["order_item", "status"],
                        name="fbs_problem_item_order_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("quantity__gt", 0)),
                        name="fbs_problem_tote_item_qty_gt_zero",
                    )
                ],
            },
        ),
    ]
