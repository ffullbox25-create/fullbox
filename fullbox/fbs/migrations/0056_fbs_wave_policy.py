from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("fbs", "0055_inventory_assignment_workflow"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FbsWavePolicy",
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
                    "max_orders_per_wave",
                    models.PositiveSmallIntegerField(
                        verbose_name="Максимум заказов в волне"
                    ),
                ),
                (
                    "max_units_per_wave",
                    models.PositiveSmallIntegerField(
                        verbose_name="Максимум единиц в волне"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "profile",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="wave_policy",
                        to="fbs.fbsintegrationprofile",
                        verbose_name="Кабинет FBS",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="updated_fbs_wave_policies",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кем изменено",
                    ),
                ),
            ],
            options={
                "verbose_name": "Настройка волны FBS",
                "verbose_name_plural": "Настройки волн FBS",
                "db_table": "fbs_wave_policy",
                "ordering": [
                    "profile__agency_id",
                    "profile__marketplace",
                    "profile_id",
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="fbswavepolicy",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("max_orders_per_wave__gte", 1))
                    & models.Q(("max_orders_per_wave__lte", 100))
                ),
                name="fbs_wave_policy_orders_1_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="fbswavepolicy",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("max_units_per_wave__gte", 1))
                    & models.Q(("max_units_per_wave__lte", 100))
                ),
                name="fbs_wave_policy_units_1_100",
            ),
        ),
    ]
