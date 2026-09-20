from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("processing_app", "0012_processing_work_event"),
    ]

    operations = [
        migrations.AddField(
            model_name="processingprintjob",
            name="attempt_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="processingprintjob",
            name="claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="processingprintjob",
            name="lease_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="processingprintjob",
            index=models.Index(
                fields=["status", "lease_until"],
                name="processing__status_923f2e_idx",
            ),
        ),
    ]
