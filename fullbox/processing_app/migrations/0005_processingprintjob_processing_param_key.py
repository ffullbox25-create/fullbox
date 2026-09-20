from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("processing_app", "0004_processingprintjob_template_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="processingprintjob",
            name="processing_param_key",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
