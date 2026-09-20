from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


RESPONSIBLE_USERNAMES = ("AJuravleva", "LMorozova")


def seed_responsibles(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Responsibility = apps.get_model("fbs", "FbsStorekeeperResponsible")
    for user in User.objects.filter(username__in=RESPONSIBLE_USERNAMES, is_active=True):
        Responsibility.objects.get_or_create(user_id=user.id)


def unseed_responsibles(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Responsibility = apps.get_model("fbs", "FbsStorekeeperResponsible")
    user_ids = User.objects.filter(username__in=RESPONSIBLE_USERNAMES).values_list(
        "id", flat=True
    )
    Responsibility.objects.filter(user_id__in=user_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fbs", "0060_fbspickingcart_owner_agency"),
    ]

    operations = [
        migrations.CreateModel(
            name="FbsStorekeeperResponsible",
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
                    "assigned_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assigned_fbs_storekeeper_responsibilities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fbs_storekeeper_responsibility",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "fbs_storekeeper_responsible",
                "ordering": ["user_id"],
            },
        ),
        migrations.CreateModel(
            name="FbsStorekeeperAlertAcknowledgement",
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
                ("alert_key", models.CharField(max_length=128, unique=True)),
                ("alert_kind", models.CharField(max_length=32)),
                ("acknowledged_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "responsible",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="acknowledged_fbs_storekeeper_alerts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "fbs_storekeeper_alert_ack",
                "ordering": ["-acknowledged_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["alert_kind", "acknowledged_at"],
                        name="fbs_store_alert_kind_ack_idx",
                    )
                ],
            },
        ),
        migrations.RunPython(seed_responsibles, unseed_responsibles),
    ]
