from django.db import migrations, models


def copy_legacy_sequence(apps, schema_editor):
    sequence_model = apps.get_model("processing_app", "ProcessingContainerCodeSequence")
    for sequence in sequence_model.objects.all().iterator():
        legacy_number = int(sequence.last_number or 0)
        sequence.last_box_number = legacy_number
        sequence.last_pallet_number = legacy_number
        sequence.save(update_fields=["last_box_number", "last_pallet_number"])


def restore_legacy_sequence(apps, schema_editor):
    sequence_model = apps.get_model("processing_app", "ProcessingContainerCodeSequence")
    for sequence in sequence_model.objects.all().iterator():
        sequence.last_number = max(
            int(sequence.last_number or 0),
            int(sequence.last_box_number or 0),
            int(sequence.last_pallet_number or 0),
        )
        sequence.save(update_fields=["last_number"])


class Migration(migrations.Migration):

    dependencies = [
        ("processing_app", "0010_processingprintjob_label_png_base64_list"),
    ]

    operations = [
        migrations.AddField(
            model_name="processingcontainercodesequence",
            name="last_box_number",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="processingcontainercodesequence",
            name="last_pallet_number",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(copy_legacy_sequence, restore_legacy_sequence),
    ]
