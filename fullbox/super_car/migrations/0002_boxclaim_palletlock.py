from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("super_car", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="BoxClaim",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("box_code", models.CharField(db_index=True, max_length=128)),
                ("pallet_code", models.CharField(blank=True, db_index=True, max_length=128)),
                (
                    "claim_kind",
                    models.CharField(
                        choices=[("box", "Короб целиком"), ("partial", "Частичный отбор")],
                        default="box",
                        max_length=16,
                    ),
                ),
                ("shipping_order_id", models.CharField(blank=True, db_index=True, max_length=64)),
                ("shipping_order_pk", models.PositiveIntegerField(blank=True, null=True)),
                ("payload", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("claimed", "Взят в работу"),
                            ("delivered", "Доставлен"),
                            ("cancelled", "Отменен"),
                        ],
                        default="claimed",
                        max_length=16,
                    ),
                ),
                ("claimed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("released_at", models.DateTimeField(blank=True, null=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="super_car_box_claims",
                        to="sku.agency",
                    ),
                ),
                (
                    "claimed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="super_car_box_claims",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "move_task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="box_claims",
                        to="super_car.movetask",
                    ),
                ),
            ],
            options={
                "verbose_name": "Выбор короба ричтраком",
                "verbose_name_plural": "Выборы коробов ричтраком",
            },
        ),
        migrations.CreateModel(
            name="PalletLock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("pallet_code", models.CharField(db_index=True, max_length=128)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Активна"), ("released", "Снята"), ("cancelled", "Отменена")],
                        default="active",
                        max_length=16,
                    ),
                ),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("locked_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("released_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="super_car_pallet_locks",
                        to="sku.agency",
                    ),
                ),
                (
                    "locked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="super_car_pallet_locks",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "move_task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pallet_locks",
                        to="super_car.movetask",
                    ),
                ),
            ],
            options={
                "verbose_name": "Блокировка паллеты ричтраком",
                "verbose_name_plural": "Блокировки паллет ричтраком",
            },
        ),
        migrations.AddIndex(
            model_name="boxclaim",
            index=models.Index(fields=["agency", "status", "box_code"], name="super_car__agency__61a8d9_idx"),
        ),
        migrations.AddIndex(
            model_name="boxclaim",
            index=models.Index(fields=["agency", "status", "pallet_code"], name="super_car__agency__1399e5_idx"),
        ),
        migrations.AddIndex(
            model_name="boxclaim",
            index=models.Index(fields=["move_task", "status"], name="super_car__move_ta_3b310e_idx"),
        ),
        migrations.AddIndex(
            model_name="boxclaim",
            index=models.Index(fields=["shipping_order_id", "status"], name="super_car__shippin_dde26c_idx"),
        ),
        migrations.AddConstraint(
            model_name="boxclaim",
            constraint=models.UniqueConstraint(
                condition=models.Q(status="claimed"),
                fields=("agency", "box_code"),
                name="uniq_active_super_car_box_claim",
            ),
        ),
        migrations.AddIndex(
            model_name="palletlock",
            index=models.Index(fields=["agency", "status", "pallet_code"], name="super_car__agency__715fd2_idx"),
        ),
        migrations.AddIndex(
            model_name="palletlock",
            index=models.Index(fields=["move_task", "status"], name="super_car__move_ta_fa7db5_idx"),
        ),
        migrations.AddConstraint(
            model_name="palletlock",
            constraint=models.UniqueConstraint(
                condition=models.Q(status="active"),
                fields=("agency", "pallet_code"),
                name="uniq_active_super_car_pallet_lock",
            ),
        ),
    ]
