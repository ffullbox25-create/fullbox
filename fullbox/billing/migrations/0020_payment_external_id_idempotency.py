# Generated manually for invoice payment idempotency.

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0019_charge_exclude_history_version"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="invoicepayment",
            constraint=models.UniqueConstraint(
                fields=["invoice", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_billing_payment_external_id",
            ),
        ),
    ]
