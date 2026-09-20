# Generated manually for the FBS cart-to-workstation handover flow.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0029_fbshandoverorderassignment_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="fbspickbatch",
            name="uniq_active_fbs_wave_per_cart",
        ),
        migrations.AddConstraint(
            model_name="fbspickbatch",
            constraint=models.UniqueConstraint(
                fields=("cart",),
                condition=models.Q(
                    status__in=("in_progress", "verification"),
                    picking_completed_at__isnull=True,
                    cart__isnull=False,
                ),
                name="uniq_active_fbs_wave_per_cart",
            ),
        ),
    ]
