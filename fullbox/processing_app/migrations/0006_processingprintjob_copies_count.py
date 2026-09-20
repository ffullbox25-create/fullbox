from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("processing_app", "0005_processingprintjob_processing_param_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="processingprintjob",
            name="copies_count",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
