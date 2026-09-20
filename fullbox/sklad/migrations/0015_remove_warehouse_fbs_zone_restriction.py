from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sklad", "0014_warehouselocation_allow_mixed_client_pallets"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="warehouselocation",
            name="warehouse_fbs_visible_pr_otg_only",
        ),
    ]
