# Generated for the isolated FBS erroneous-pick restock flow.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0031_fbsworkstation_active_handover_box"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FbsPickRestockRequest",
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
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Ожидает подборщика"),
                            ("in_progress", "Возвращается"),
                            ("completed", "Возвращено"),
                            ("canceled", "Отменено"),
                        ],
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("reason", models.TextField()),
                ("planned_qty", models.PositiveIntegerField()),
                ("returned_qty", models.PositiveIntegerField(default=0)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("canceled_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "assigned_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assigned_fbs_pick_restock_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "batch",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pick_restock_request",
                        to="fbs.fbspickbatch",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_fbs_pick_restock_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "fbs_pick_restock_request",
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["status", "created_at"],
                        name="fbs_restock_status_created_idx",
                    ),
                    models.Index(
                        fields=["assigned_to", "status"],
                        name="fbs_restock_actor_stat_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("planned_qty__gt", 0)),
                        name="fbs_pick_restock_planned_gt_zero",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("planned_qty__gte", models.F("returned_qty"))),
                        name="fbs_pick_restock_returned_lte_planned",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="FbsPickRestockLine",
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
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает"),
                            ("in_progress", "Возвращается"),
                            ("completed", "Возвращено"),
                            ("canceled", "Отменено"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("planned_qty", models.PositiveIntegerField()),
                ("returned_qty", models.PositiveIntegerField(default=0)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "allocation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pick_restock_line",
                        to="fbs.fbsorderstockallocation",
                    ),
                ),
                (
                    "request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lines",
                        to="fbs.fbspickrestockrequest",
                    ),
                ),
                (
                    "source_balance",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pick_restock_lines",
                        to="fbs.fbsstockbalance",
                    ),
                ),
                (
                    "source_box",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pick_restock_lines",
                        to="fbs.fbsbox",
                    ),
                ),
                (
                    "source_cell",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pick_restock_lines",
                        to="fbs.fbsstoragecell",
                    ),
                ),
            ],
            options={
                "db_table": "fbs_pick_restock_line",
                "ordering": ["request_id", "source_cell_id", "source_box_id", "id"],
                "indexes": [
                    models.Index(
                        fields=["request", "status"],
                        name="fbs_restock_line_req_stat_idx",
                    ),
                    models.Index(
                        fields=["source_box", "status"],
                        name="fbs_restock_line_box_stat_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("planned_qty__gt", 0)),
                        name="fbs_pick_restock_line_planned_gt_zero",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("planned_qty__gte", models.F("returned_qty"))),
                        name="fbs_pick_restock_line_returned_lte_planned",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="FbsPickRestockScan",
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
                (
                    "stage",
                    models.CharField(
                        choices=[
                            ("cell", "Исходная ячейка"),
                            ("box", "Исходный короб"),
                            ("item", "Возврат товара"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "result",
                    models.CharField(
                        choices=[("success", "Успешно"), ("error", "Ошибка")],
                        max_length=16,
                    ),
                ),
                ("scan_value", models.CharField(blank=True, max_length=512)),
                ("expected_value", models.CharField(blank=True, max_length=512)),
                ("quantity_after", models.PositiveIntegerField(blank=True, null=True)),
                ("message", models.TextField(blank=True)),
                ("request_token", models.UUIDField(blank=True, null=True, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fbs_pick_restock_scans",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "line",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="scans",
                        to="fbs.fbspickrestockline",
                    ),
                ),
                (
                    "request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="scans",
                        to="fbs.fbspickrestockrequest",
                    ),
                ),
            ],
            options={
                "db_table": "fbs_pick_restock_scan",
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["request", "created_at"],
                        name="fbs_restock_scan_req_time_idx",
                    ),
                    models.Index(
                        fields=["line", "stage", "result"],
                        name="fbs_restock_scan_line_idx",
                    ),
                ],
            },
        ),
        migrations.AlterField(
            model_name="fbshandoverorderassignment",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Добавляется в поставку"),
                    ("confirmed", "Добавлен в поставку"),
                    ("error", "Ошибка добавления"),
                    ("canceled", "Отменено"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
    ]
