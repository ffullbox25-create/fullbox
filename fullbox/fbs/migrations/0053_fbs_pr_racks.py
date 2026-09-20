from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0052_fbs_controller_policy"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FbsRack",
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
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_fbs_racks",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "location",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fbs_rack",
                        to="sklad.warehouselocation",
                    ),
                ),
            ],
            options={
                "db_table": "fbs_rack",
                "ordering": ["location__location_code", "id"],
            },
        ),
        migrations.CreateModel(
            name="FbsRackCell",
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
                ("position", models.PositiveSmallIntegerField()),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "rack",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cells",
                        to="fbs.fbsrack",
                    ),
                ),
                (
                    "storage_cell",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="rack_cell",
                        to="fbs.fbsstoragecell",
                    ),
                ),
            ],
            options={
                "db_table": "fbs_rack_cell",
                "ordering": ["rack_id", "position", "id"],
                "indexes": [
                    models.Index(
                        fields=["rack", "is_active", "position"],
                        name="fbs_rack_act_pos_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("rack", "position"),
                        name="uniq_fbs_rack_cell_position",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("position__gt", 0)),
                        name="fbs_rack_cell_position_gt_zero",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="FbsRackCellBinding",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fbs_rack_cell_bindings",
                        to="sku.agency",
                    ),
                ),
                (
                    "box",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="rack_cell_binding",
                        to="fbs.fbsbox",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_fbs_rack_cell_bindings",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "pallet",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="rack_cell_binding",
                        to="fbs.fbspallet",
                    ),
                ),
                (
                    "rack_cell",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="bindings",
                        to="fbs.fbsrackcell",
                    ),
                ),
            ],
            options={
                "db_table": "fbs_rack_cell_binding",
                "ordering": ["rack_cell_id", "agency_id", "id"],
                "indexes": [
                    models.Index(
                        fields=["agency", "rack_cell"],
                        name="fbs_rbind_ag_cell_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("rack_cell", "agency"),
                        name="uniq_fbs_rack_cell_binding_agency",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="FbsRackStagingBox",
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
                            ("awaiting_placement", "Ожидает размещения в ячейку"),
                            ("placed", "Размещен в ячейке"),
                            ("canceled", "Отменен"),
                        ],
                        default="awaiting_placement",
                        max_length=24,
                    ),
                ),
                ("arrived_at", models.DateTimeField(auto_now_add=True)),
                ("placed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "arrival_operation",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fbs_rack_arrival",
                        to="sklad.warehouseoperation",
                    ),
                ),
                (
                    "arrived_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="arrived_fbs_rack_staging_boxes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "box",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="rack_staging_records",
                        to="fbs.fbsbox",
                    ),
                ),
                (
                    "placed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="placed_fbs_rack_staging_boxes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "rack",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="staging_boxes",
                        to="fbs.fbsrack",
                    ),
                ),
            ],
            options={
                "db_table": "fbs_rack_staging_box",
                "ordering": ["arrived_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["rack", "status", "arrived_at"],
                        name="fbs_stage_rack_time_idx",
                    ),
                    models.Index(
                        fields=["box", "status"],
                        name="fbs_stage_box_status_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("status", "awaiting_placement")),
                        fields=("box",),
                        name="uniq_awaiting_fbs_rack_staging_box",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="FbsRackContentMovement",
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
                ("idempotency_key", models.CharField(max_length=64, unique=True)),
                ("moved_qty", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "agency",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fbs_rack_content_movements",
                        to="sku.agency",
                    ),
                ),
                (
                    "operation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fbs_rack_content_movement",
                        to="sklad.warehouseoperation",
                    ),
                ),
                (
                    "performed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="performed_fbs_rack_content_movements",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "source_box",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="outgoing_rack_content_movements",
                        to="fbs.fbsbox",
                    ),
                ),
                (
                    "target_binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="content_movements",
                        to="fbs.fbsrackcellbinding",
                    ),
                ),
                (
                    "target_cell",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="incoming_content_movements",
                        to="fbs.fbsrackcell",
                    ),
                ),
            ],
            options={
                "db_table": "fbs_rack_content_movement",
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["agency", "created_at"],
                        name="fbs_rmove_ag_time_idx",
                    ),
                    models.Index(
                        fields=["target_cell", "created_at"],
                        name="fbs_rmove_cell_time_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("moved_qty__gt", 0)),
                        name="fbs_rack_content_moved_qty_gt_zero",
                    )
                ],
            },
        ),
        migrations.RemoveConstraint(
            model_name="fbspallet",
            name="uniq_active_fbs_pallet_per_cell",
        ),
        migrations.AddField(
            model_name="fbspallet",
            name="is_rack_binding",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="fbspallet",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("is_rack_binding", False),
                    ("status__in", ["planned", "active"]),
                ),
                fields=("cell",),
                name="uniq_active_fbs_pallet_per_cell",
            ),
        ),
        migrations.AddConstraint(
            model_name="fbspallet",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("is_rack_binding", True),
                    ("status__in", ["planned", "active"]),
                ),
                fields=("cell", "agency"),
                name="uniq_active_fbs_rack_pallet_cell_agency",
            ),
        ),
    ]
