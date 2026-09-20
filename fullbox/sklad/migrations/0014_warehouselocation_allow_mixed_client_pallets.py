from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sklad", "0013_operational_pr_otg_locations"),
    ]

    operations = [
        migrations.AddField(
            model_name="warehouselocation",
            name="allow_mixed_client_pallets",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Разрешает размещать в этом операционном месте FBS-паллеты "
                    "разных клиентов."
                ),
            ),
        ),
    ]
