from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0002_orderauditentry"),
        ("sku", "0006_agency_archived_alter_agency_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditentry",
            name="agency",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="audit_entries",
                to="sku.agency",
            ),
        ),
    ]
