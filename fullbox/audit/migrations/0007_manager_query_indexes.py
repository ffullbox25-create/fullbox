from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0006_orderexternalnumber"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="orderauditentry",
            index=models.Index(
                fields=["agency", "order_type", "order_id", "-created_at", "-id"],
                name="audit_mgr_req_latest_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="orderauditentry",
            index=models.Index(
                fields=["order_type", "-created_at", "-id"],
                name="audit_mgr_type_latest_idx",
            ),
        ),
    ]
