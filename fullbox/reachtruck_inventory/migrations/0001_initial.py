import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("inventory", "0001_initial"),
        ("sklad", "0010_warehousecontainer_dimensions_weight"),
    ]

    operations = [
        migrations.CreateModel(
            name="InventoryTask",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("created", "Ожидает исполнителя"), ("in_progress", "В работе"), ("completed", "Выполнено"), ("canceled", "Отменено")], default="created", max_length=16)),
                ("assigned_to_name", models.CharField(blank=True, max_length=255)),
                ("location_verified_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("canceled_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assigned_to", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reachtruck_inventory_tasks", to=settings.AUTH_USER_MODEL)),
                ("inventory", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="execution_tasks", to="inventory.inventory")),
                ("location", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reachtruck_inventory_tasks", to="sklad.warehouselocation")),
                ("scope_location", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="execution_task", to="inventory.inventorylocation")),
            ],
            options={
                "verbose_name": "Задание ричтракеру на инвентаризацию",
                "verbose_name_plural": "Задания ричтрактерам на инвентаризацию",
                "db_table": "reachtruck_inventory_task",
                "ordering": ["status", "created_at", "id"],
            },
        ),
        migrations.AddIndex(model_name="inventorytask", index=models.Index(fields=["status", "created_at"], name="reachtruck__status_b9e990_idx")),
        migrations.AddIndex(model_name="inventorytask", index=models.Index(fields=["assigned_to", "status"], name="reachtruck__assigne_27e1de_idx")),
        migrations.AddIndex(model_name="inventorytask", index=models.Index(fields=["inventory", "status"], name="reachtruck__invento_6bd5e0_idx")),
        migrations.AddIndex(model_name="inventorytask", index=models.Index(fields=["location", "status"], name="reachtruck__locatio_821caf_idx")),
    ]
