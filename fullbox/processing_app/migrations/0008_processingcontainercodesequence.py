from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("processing_app", "0007_processingorderattachment"),
        ("sku", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProcessingContainerCodeSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("last_number", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="processing_container_code_sequence",
                        to="sku.agency",
                    ),
                ),
            ],
            options={
                "verbose_name": "Счетчик кодов коробов обработки",
                "verbose_name_plural": "Счетчики кодов коробов обработки",
            },
        ),
    ]
