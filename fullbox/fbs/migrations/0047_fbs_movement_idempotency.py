from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0046_handover_verification_override"),
    ]

    operations = [
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="idempotency_key",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddConstraint(
            model_name="fbsclientmovementrequest",
            constraint=models.UniqueConstraint(
                condition=~Q(idempotency_key=""),
                fields=("agency", "idempotency_key"),
                name="uniq_fbs_mov_agency_idempotency",
            ),
        ),
    ]
