from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fbs", "0010_fbsmarketplacemetadatatransfer_unsupported"),
    ]

    operations = [
        migrations.AddField(
            model_name="fbsmarketplacecommand",
            name="request_source",
            field=models.CharField(
                choices=[
                    ("automatic", "Автоматически"),
                    ("metadata_control", "Экран контроля передач"),
                ],
                default="automatic",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="fbsmarketplacecommand",
            name="requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="requested_fbs_marketplace_commands",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
