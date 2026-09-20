from django.db import migrations


WORKER_NAMES = ("Илья Соколов", "Алина Морозова")
PLACEHOLDER_NAMES = ("Обработчик 1", "Обработчик 2")


def ensure_processing_worker_names(apps, schema_editor):
    Employee = apps.get_model("employees", "Employee")
    for name in WORKER_NAMES:
        if Employee.objects.filter(role="processing_worker", full_name=name).exists():
            continue
        placeholder = (
            Employee.objects.filter(role="processing_worker", full_name__in=PLACEHOLDER_NAMES)
            .exclude(full_name__in=WORKER_NAMES)
            .order_by("id")
            .first()
        )
        if placeholder:
            placeholder.full_name = name
            placeholder.save(update_fields=["full_name"])
            continue
        Employee.objects.create(full_name=name, role="processing_worker", is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0006_processing_worker_role"),
    ]

    operations = [
        migrations.RunPython(ensure_processing_worker_names, migrations.RunPython.noop),
    ]
