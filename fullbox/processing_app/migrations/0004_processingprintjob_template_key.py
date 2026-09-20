from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("processing_app", "0003_rename_processing__order_i_09d833_idx_processing__order_i_b04c0e_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="processingprintjob",
            name="template_key",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
