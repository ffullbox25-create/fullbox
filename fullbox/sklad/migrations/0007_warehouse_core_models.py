from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("sku", "0009_agency_short_name"),
        ("sklad", "0006_stockpalletstate_marking_code"),
    ]

    operations = [
        migrations.CreateModel(
            name="WarehouseLocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("warehouse_code", models.CharField(default="MSK", max_length=32)),
                ("zone_code", models.CharField(max_length=16)),
                (
                    "zone_kind",
                    models.CharField(
                        choices=[
                            ("receiving", "Приемка"),
                            ("storage", "Хранение"),
                            ("processing", "Обработка"),
                            ("shipping", "Отгрузка"),
                            ("loading", "Погрузка"),
                            ("transit", "Транзит"),
                            ("vehicle", "Транспорт"),
                            ("virtual", "Виртуальная зона"),
                        ],
                        default="storage",
                        max_length=32,
                    ),
                ),
                ("row_no", models.PositiveIntegerField(default=0)),
                ("section_no", models.PositiveIntegerField(default=0)),
                ("tier_no", models.PositiveIntegerField(default=0)),
                ("cell_no", models.PositiveIntegerField(default=0)),
                ("location_code", models.CharField(blank=True, max_length=64)),
                ("display_name", models.CharField(blank=True, max_length=255)),
                ("is_active", models.BooleanField(default=True)),
                ("is_pickable", models.BooleanField(default=False)),
                ("is_storage", models.BooleanField(default=False)),
                ("is_processing", models.BooleanField(default=False)),
                ("is_shipping", models.BooleanField(default=False)),
                ("is_loading", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "warehouse_location"},
        ),
        migrations.CreateModel(
            name="WarehouseContainer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "container_type",
                    models.CharField(
                        choices=[("box", "Короб"), ("pallet", "Паллета"), ("mixed_pallet", "Смешанная паллета")],
                        max_length=32,
                    ),
                ),
                ("container_code", models.CharField(max_length=128)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Активен"),
                            ("merged", "Объединен"),
                            ("split", "Разделен"),
                            ("archived", "Архив"),
                        ],
                        default="active",
                        max_length=32,
                    ),
                ),
                ("source_context_type", models.CharField(blank=True, max_length=32)),
                ("source_context_id", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="warehouse_containers", to="sku.agency"),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_warehouse_containers",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "current_location",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="containers",
                        to="sklad.warehouselocation",
                    ),
                ),
                (
                    "parent_container",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="child_containers",
                        to="sklad.warehousecontainer",
                    ),
                ),
            ],
            options={"db_table": "warehouse_container"},
        ),
        migrations.CreateModel(
            name="WarehouseReserve",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "reserve_type",
                    models.CharField(
                        choices=[("processing", "Под обработку"), ("shipping", "Под отгрузку"), ("manual", "Ручной")],
                        max_length=32,
                    ),
                ),
                ("context_type", models.CharField(max_length=32)),
                ("context_id", models.CharField(max_length=64)),
                ("sku_code", models.CharField(max_length=64)),
                ("size", models.CharField(blank=True, max_length=64)),
                ("barcode", models.CharField(blank=True, max_length=64)),
                ("goods_type", models.CharField(blank=True, max_length=64)),
                ("marking_code", models.CharField(blank=True, max_length=128)),
                ("qty_reserved", models.PositiveIntegerField(default=0)),
                ("qty_allocated", models.PositiveIntegerField(default=0)),
                ("qty_satisfied", models.PositiveIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Активен"),
                            ("partially_allocated", "Частично аллоцирован"),
                            ("allocated", "Аллоцирован"),
                            ("partially_satisfied", "Частично выполнен"),
                            ("satisfied", "Выполнен"),
                            ("released", "Снят"),
                            ("canceled", "Отменен"),
                        ],
                        default="active",
                        max_length=32,
                    ),
                ),
                ("source_document_type", models.CharField(blank=True, max_length=32)),
                ("source_document_id", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="warehouse_reserves", to="sku.agency"),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_warehouse_reserves",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "released_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="released_warehouse_reserves",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "sku_ref",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="warehouse_reserves",
                        to="sku.sku",
                    ),
                ),
            ],
            options={"db_table": "warehouse_reserve"},
        ),
        migrations.CreateModel(
            name="WarehouseOperation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "operation_type",
                    models.CharField(
                        choices=[
                            ("putaway", "Размещение в хранение"),
                            ("processing", "Обработка"),
                            ("move_to_processing", "Перемещение в обработку"),
                            ("move_to_otg", "Перемещение в OTG"),
                            ("palletization", "Паллетизация"),
                            ("move_to_loading", "Перемещение в погрузку"),
                            ("load_to_vehicle", "Погрузка в машину"),
                            ("return_to_storage", "Возврат в хранение"),
                            ("internal_relocation", "Внутреннее перемещение"),
                        ],
                        max_length=32,
                    ),
                ),
                ("context_type", models.CharField(max_length=32)),
                ("context_id", models.CharField(max_length=64)),
                ("source_document_type", models.CharField(blank=True, max_length=32)),
                ("source_document_id", models.CharField(blank=True, max_length=64)),
                ("source_zone_code", models.CharField(blank=True, max_length=16)),
                ("destination_zone_code", models.CharField(blank=True, max_length=16)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("created", "Создана"),
                            ("planned", "Запланирована"),
                            ("in_progress", "В работе"),
                            ("partial", "Частично выполнена"),
                            ("done", "Выполнена"),
                            ("blocked", "Заблокирована"),
                            ("canceled", "Отменена"),
                        ],
                        default="created",
                        max_length=32,
                    ),
                ),
                ("priority", models.PositiveSmallIntegerField(default=0)),
                ("requested_by_role", models.CharField(blank=True, max_length=32)),
                ("assigned_executor_role", models.CharField(blank=True, max_length=32)),
                ("comment", models.TextField(blank=True)),
                ("planned_qty", models.PositiveIntegerField(default=0)),
                ("done_qty", models.PositiveIntegerField(default=0)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="warehouse_operations", to="sku.agency"),
                ),
                (
                    "destination_location",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="destination_operations",
                        to="sklad.warehouselocation",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="requested_warehouse_operations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reserve",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="operations",
                        to="sklad.warehousereserve",
                    ),
                ),
                (
                    "source_location",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="source_operations",
                        to="sklad.warehouselocation",
                    ),
                ),
            ],
            options={"db_table": "warehouse_operation"},
        ),
        migrations.CreateModel(
            name="WarehouseOperationTask",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "task_type",
                    models.CharField(
                        choices=[
                            ("pallet_move", "Перемещение паллеты"),
                            ("box_move", "Перемещение короба"),
                            ("partial_pick", "Частичный отбор"),
                            ("palletization_step", "Шаг паллетизации"),
                            ("loading_step", "Шаг погрузки"),
                        ],
                        max_length=32,
                    ),
                ),
                ("from_zone_code", models.CharField(blank=True, max_length=16)),
                ("to_zone_code", models.CharField(blank=True, max_length=16)),
                ("qty_planned", models.PositiveIntegerField(default=0)),
                ("qty_done", models.PositiveIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("created", "Создана"),
                            ("in_progress", "В работе"),
                            ("done", "Выполнена"),
                            ("failed", "Ошибка"),
                            ("canceled", "Отменена"),
                        ],
                        default="created",
                        max_length=32,
                    ),
                ),
                ("assigned_to_name", models.CharField(blank=True, max_length=255)),
                ("executor_role", models.CharField(blank=True, max_length=32)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "assigned_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="warehouse_operation_tasks",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "container",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="operation_tasks",
                        to="sklad.warehousecontainer",
                    ),
                ),
                (
                    "from_location",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="from_operation_tasks",
                        to="sklad.warehouselocation",
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tasks", to="sklad.warehouseoperation"),
                ),
                (
                    "to_location",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="to_operation_tasks",
                        to="sklad.warehouselocation",
                    ),
                ),
            ],
            options={"db_table": "warehouse_operation_task"},
        ),
        migrations.CreateModel(
            name="WarehouseEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(max_length=64)),
                ("stock_context_type", models.CharField(blank=True, max_length=32)),
                ("stock_context_id", models.CharField(blank=True, max_length=64)),
                ("source_document_type", models.CharField(blank=True, max_length=32)),
                ("source_document_id", models.CharField(blank=True, max_length=64)),
                ("from_zone_code", models.CharField(blank=True, max_length=16)),
                ("to_zone_code", models.CharField(blank=True, max_length=16)),
                ("qty", models.PositiveIntegerField(default=0)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("performed_by_role", models.CharField(blank=True, max_length=32)),
                ("occurred_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "agency",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="warehouse_events", to="sku.agency"),
                ),
                (
                    "container",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to="sklad.warehousecontainer",
                    ),
                ),
                (
                    "from_location",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events_from",
                        to="sklad.warehouselocation",
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to="sklad.warehouseoperation",
                    ),
                ),
                (
                    "operation_task",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to="sklad.warehouseoperationtask",
                    ),
                ),
                (
                    "performed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="performed_warehouse_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reserve",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to="sklad.warehousereserve",
                    ),
                ),
                (
                    "to_location",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events_to",
                        to="sklad.warehouselocation",
                    ),
                ),
            ],
            options={"db_table": "warehouse_event"},
        ),
        migrations.CreateModel(
            name="WarehouseStockSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stock_unit_type", models.CharField(blank=True, default="item", max_length=32)),
                ("source_context_type", models.CharField(blank=True, max_length=32)),
                ("source_context_id", models.CharField(blank=True, max_length=64)),
                ("sku_code", models.CharField(max_length=64)),
                ("name", models.CharField(blank=True, max_length=255)),
                ("size", models.CharField(blank=True, max_length=64)),
                ("barcode", models.CharField(blank=True, max_length=64)),
                ("goods_type", models.CharField(blank=True, max_length=64)),
                ("marking_code", models.CharField(blank=True, max_length=128)),
                ("qty", models.PositiveIntegerField(default=0)),
                ("available_qty", models.PositiveIntegerField(default=0)),
                ("processing_reserved_qty", models.PositiveIntegerField(default=0)),
                ("shipping_reserved_qty", models.PositiveIntegerField(default=0)),
                ("other_reserved_qty", models.PositiveIntegerField(default=0)),
                ("container_code", models.CharField(blank=True, max_length=128)),
                ("zone_code", models.CharField(blank=True, max_length=16)),
                ("zone_kind", models.CharField(blank=True, max_length=32)),
                ("warehouse_state_code", models.CharField(blank=True, max_length=64)),
                ("active_operation_type", models.CharField(blank=True, max_length=32)),
                ("current_trip_id", models.CharField(blank=True, max_length=64)),
                ("is_in_vehicle", models.BooleanField(default=False)),
                ("is_archived", models.BooleanField(default=False)),
                ("snapshot_version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "active_operation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="active_snapshots",
                        to="sklad.warehouseoperation",
                    ),
                ),
                (
                    "agency",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="warehouse_snapshots", to="sku.agency"),
                ),
                (
                    "container",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="snapshots",
                        to="sklad.warehousecontainer",
                    ),
                ),
                (
                    "last_event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="snapshots",
                        to="sklad.warehouseevent",
                    ),
                ),
                (
                    "location",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="snapshots",
                        to="sklad.warehouselocation",
                    ),
                ),
                (
                    "parent_container",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="child_snapshots",
                        to="sklad.warehousecontainer",
                    ),
                ),
                (
                    "sku_ref",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="warehouse_snapshots",
                        to="sku.sku",
                    ),
                ),
            ],
            options={"db_table": "warehouse_stock_snapshot"},
        ),
        migrations.AddIndex(
            model_name="warehouselocation",
            index=models.Index(fields=["zone_code"], name="warehouse_l_zone_co_edc588_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouselocation",
            index=models.Index(fields=["zone_kind"], name="warehouse_l_zone_ki_506997_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouselocation",
            index=models.Index(fields=["warehouse_code", "zone_code"], name="warehouse_l_warehou_f3cce4_idx"),
        ),
        migrations.AddConstraint(
            model_name="warehouselocation",
            constraint=models.UniqueConstraint(
                fields=("warehouse_code", "zone_code", "row_no", "section_no", "tier_no", "cell_no"),
                name="uniq_warehouse_location_slot",
            ),
        ),
        migrations.AddIndex(
            model_name="warehousecontainer",
            index=models.Index(fields=["agency", "container_type"], name="warehouse_c_agency__8a61bb_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousecontainer",
            index=models.Index(fields=["current_location"], name="warehouse_c_current_69ee81_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousecontainer",
            index=models.Index(fields=["parent_container"], name="warehouse_c_parent__5de8cd_idx"),
        ),
        migrations.AddConstraint(
            model_name="warehousecontainer",
            constraint=models.UniqueConstraint(fields=("agency", "container_code"), name="uniq_warehouse_container_code"),
        ),
        migrations.AddIndex(
            model_name="warehousereserve",
            index=models.Index(fields=["agency", "reserve_type", "context_type", "context_id"], name="warehouse_r_agency__5d4415_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousereserve",
            index=models.Index(fields=["agency", "sku_code", "size", "barcode", "goods_type"], name="warehouse_r_agency__caeedc_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousereserve",
            index=models.Index(fields=["status"], name="warehouse_r_status_e82740_idx"),
        ),
        migrations.AddConstraint(
            model_name="warehousereserve",
            constraint=models.CheckConstraint(condition=models.Q(("qty_reserved__gt", 0)), name="warehouse_reserve_qty_reserved_gt_zero"),
        ),
        migrations.AddConstraint(
            model_name="warehousereserve",
            constraint=models.CheckConstraint(condition=models.Q(("qty_allocated__gte", 0)), name="warehouse_reserve_qty_allocated_gte_zero"),
        ),
        migrations.AddConstraint(
            model_name="warehousereserve",
            constraint=models.CheckConstraint(condition=models.Q(("qty_satisfied__gte", 0)), name="warehouse_reserve_qty_satisfied_gte_zero"),
        ),
        migrations.AddConstraint(
            model_name="warehousereserve",
            constraint=models.CheckConstraint(condition=models.Q(("qty_allocated__lte", models.F("qty_reserved"))), name="warehouse_reserve_allocated_lte_reserved"),
        ),
        migrations.AddConstraint(
            model_name="warehousereserve",
            constraint=models.CheckConstraint(condition=models.Q(("qty_satisfied__lte", models.F("qty_reserved"))), name="warehouse_reserve_satisfied_lte_reserved"),
        ),
        migrations.AddIndex(
            model_name="warehouseoperation",
            index=models.Index(fields=["context_type", "context_id"], name="warehouse_o_context_78a87d_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseoperation",
            index=models.Index(fields=["operation_type"], name="warehouse_o_operati_0b4d44_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseoperation",
            index=models.Index(fields=["status"], name="warehouse_o_status_a4b59a_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseoperation",
            index=models.Index(fields=["agency", "destination_zone_code"], name="warehouse_o_agency__321061_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseoperation",
            index=models.Index(fields=["reserve"], name="warehouse_o_reserve_739430_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseoperationtask",
            index=models.Index(fields=["operation"], name="warehouse_o_operati_308e21_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseoperationtask",
            index=models.Index(fields=["status"], name="warehouse_o_status_83eb93_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseoperationtask",
            index=models.Index(fields=["assigned_to"], name="warehouse_o_assigne_47c31e_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseoperationtask",
            index=models.Index(fields=["container"], name="warehouse_o_contain_b36ae2_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseevent",
            index=models.Index(fields=["event_type"], name="warehouse_e_event_t_69112c_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseevent",
            index=models.Index(fields=["stock_context_type", "stock_context_id"], name="warehouse_e_stock_c_fdec76_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseevent",
            index=models.Index(fields=["operation"], name="warehouse_e_operati_2d9d81_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseevent",
            index=models.Index(fields=["reserve"], name="warehouse_e_reserve_69e0a3_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseevent",
            index=models.Index(fields=["container"], name="warehouse_e_contain_431b14_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseevent",
            index=models.Index(fields=["occurred_at"], name="warehouse_e_occurre_916408_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousestocksnapshot",
            index=models.Index(fields=["agency", "sku_code", "size", "barcode", "goods_type"], name="warehouse_s_agency__4fda56_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousestocksnapshot",
            index=models.Index(fields=["warehouse_state_code"], name="warehouse_s_warehou_90719e_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousestocksnapshot",
            index=models.Index(fields=["zone_code"], name="warehouse_s_zone_co_61b042_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousestocksnapshot",
            index=models.Index(fields=["location"], name="warehouse_s_locatio_8cf1d9_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousestocksnapshot",
            index=models.Index(fields=["container"], name="warehouse_s_contain_d604d8_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousestocksnapshot",
            index=models.Index(fields=["active_operation"], name="warehouse_s_active__69fa2e_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousestocksnapshot",
            index=models.Index(fields=["current_trip_id"], name="warehouse_s_current_c20ef2_idx"),
        ),
        migrations.AddConstraint(
            model_name="warehousestocksnapshot",
            constraint=models.CheckConstraint(condition=models.Q(("available_qty__gte", 0)), name="warehouse_snapshot_available_gte_zero"),
        ),
        migrations.AddConstraint(
            model_name="warehousestocksnapshot",
            constraint=models.CheckConstraint(condition=models.Q(("processing_reserved_qty__gte", 0)), name="warehouse_snapshot_processing_reserved_gte_zero"),
        ),
        migrations.AddConstraint(
            model_name="warehousestocksnapshot",
            constraint=models.CheckConstraint(condition=models.Q(("shipping_reserved_qty__gte", 0)), name="warehouse_snapshot_shipping_reserved_gte_zero"),
        ),
        migrations.AddConstraint(
            model_name="warehousestocksnapshot",
            constraint=models.CheckConstraint(condition=models.Q(("other_reserved_qty__gte", 0)), name="warehouse_snapshot_other_reserved_gte_zero"),
        ),
        migrations.AddConstraint(
            model_name="warehousestocksnapshot",
            constraint=models.CheckConstraint(condition=models.Q(("qty__gte", 0)), name="warehouse_snapshot_qty_gte_zero"),
        ),
        migrations.AddConstraint(
            model_name="warehousestocksnapshot",
            constraint=models.CheckConstraint(condition=models.Q(("qty__gte", models.F("available_qty"))), name="warehouse_snapshot_qty_gte_available"),
        ),
    ]
