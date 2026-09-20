from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0003_employee_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="facsimile",
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to="facsimiles/",
                verbose_name="Факсимиле",
            ),
        ),
    ]
