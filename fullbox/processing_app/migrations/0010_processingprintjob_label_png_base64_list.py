from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("processing_app", "0009_processingprintjob_processing__status_622cce_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="processingprintjob",
            name="label_png_base64_list",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
