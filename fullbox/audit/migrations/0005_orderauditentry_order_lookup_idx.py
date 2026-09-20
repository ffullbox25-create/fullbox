from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0004_merge_0003_auditentry_agency_0003_merge_0002_auditentry_journal_auditjournal_0002_orderauditentry"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="orderauditentry",
            index=models.Index(
                fields=["order_type", "order_id", "created_at", "id"],
                name="audit_order_lookup_idx",
            ),
        ),
    ]
